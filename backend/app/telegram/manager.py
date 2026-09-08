"""Single MTProto client for the whole app (Telethon, bot-token login).

Why MTProto and not the HTTP Bot API: the HTTP API caps bot uploads at 50 MB
and downloads at 20 MB. Over MTProto a bot can send 2000 MiB per message and
download without limit, which is what makes a hosting tool viable.

The client stays connected while the app runs so it receives channel posts;
that is how the setup wizard discovers the channel the user added the bot to
(bots cannot list their chats, but they do get updates from channels they
administer)."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.bots import GetBotInfoRequest, SetBotInfoRequest
from telethon.tl.functions.channels import GetChannelsRequest
from telethon.tl.types import DocumentAttributeFilename, InputChannel

from app.telegram.fast_upload import BIG_FILE_THRESHOLD, upload_parallel

from app.db import async_session as _session_factory_ref  # noqa: F401  (kept for type hints)
from app import db as _db
from app import settings_store

logger = logging.getLogger(__name__)

TEST_MESSAGE = "OpenTelegramStorage connected to this channel ✅ (this message will be deleted)"


class TelegramNotConfigured(Exception):
    pass


@dataclass
class DiscoveredChannel:
    chat_id: int
    title: str


@dataclass
class TelegramStatus:
    configured: bool = False
    connected: bool = False
    bot_username: str | None = None
    bot_id: int | None = None
    channel_id: int | None = None
    channel_title: str | None = None
    # Telegram "auto-delete messages" period on the channel, in seconds (None = off).
    # Any value here means stored files are destroyed after that time.
    auto_delete_seconds: int | None = None
    error: str | None = None
    discovered: list[DiscoveredChannel] = field(default_factory=list)


class TelegramManager:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self.status = TelegramStatus()
        self._discovered: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._entity_cache: dict[int, object] = {}

    # ------------------------------------------------------------------ setup
    async def load_from_settings(self) -> None:
        """Reconnect at startup using the stored (encrypted) credentials."""
        async with _db.async_session() as db:
            api_id = await settings_store.get(db, "telegram.api_id")
            api_hash = await settings_store.get(db, "telegram.api_hash")
            bot_token = await settings_store.get(db, "telegram.bot_token")
            session = await settings_store.get(db, "telegram.session")
            channel_id = await settings_store.get(db, "telegram.channel_id")
            channel_title = await settings_store.get(db, "telegram.channel_title")
        if not (api_id and api_hash and bot_token):
            self.status = TelegramStatus()
            return
        self.status.configured = True
        if channel_id:
            self.status.channel_id = int(channel_id)
            self.status.channel_title = channel_title
        try:
            await self._connect(int(api_id), api_hash, bot_token, session)
            if channel_id:
                await self.check_auto_delete()
                await self.write_bot_info_channel(int(channel_id))  # idempotent; lets other servers find it
        except Exception as e:  # noqa: BLE001 - surface any startup failure in status
            logger.exception("Telegram reconnect failed")
            self.status.connected = False
            self.status.error = str(e)

    async def configure(self, api_id: int, api_hash: str, bot_token: str) -> TelegramStatus:
        async with self._lock:
            await self._disconnect()
            self._discovered.clear()
            await self._connect(api_id, api_hash, bot_token, None)
            async with _db.async_session() as db:
                await settings_store.set(db, "telegram.api_id", str(api_id))
                await settings_store.set(db, "telegram.api_hash", api_hash)
                await settings_store.set(db, "telegram.bot_token", bot_token)
                await settings_store.set(db, "telegram.session", self.client.session.save())
                await settings_store.delete(db, "telegram.channel_id")
                await settings_store.delete(db, "telegram.channel_title")
                await db.commit()
            self.status.configured = True
            self.status.channel_id = None
            self.status.channel_title = None
            await self.discover_from_bot_info()
            return self.status

    async def reconnect(self) -> None:
        async with self._lock:
            await self._disconnect()
            await self.load_from_settings()

    def needs_reconnect(self) -> bool:
        return self.status.configured and not self.status.connected

    async def disconnect_and_forget(self) -> None:
        async with self._lock:
            await self._disconnect()
            async with _db.async_session() as db:
                for key in ("telegram.api_id", "telegram.api_hash", "telegram.bot_token",
                            "telegram.session", "telegram.channel_id", "telegram.channel_title"):
                    await settings_store.delete(db, key)
                await db.commit()
            self.status = TelegramStatus()
            self._discovered.clear()

    async def _connect(self, api_id: int, api_hash: str, bot_token: str, session: str | None) -> None:
        client = TelegramClient(StringSession(session or None), api_id, api_hash,
                                connection_retries=5, retry_delay=2, auto_reconnect=True)
        await client.start(bot_token=bot_token)
        me = await client.get_me()
        self.client = client
        self.status.connected = True
        self.status.error = None
        self.status.bot_username = me.username
        self.status.bot_id = me.id
        client.add_event_handler(self._on_channel_post, events.NewMessage())
        client.add_event_handler(self._on_chat_action, events.ChatAction())
        logger.info("Telegram connected as @%s", me.username)

    async def _disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self._entity_cache.clear()
        self.status.connected = False

    async def _remember_chat(self, chat) -> None:
        if chat is None or not getattr(chat, "broadcast", False):
            return
        cid = int(f"-100{chat.id}")
        if cid not in self._discovered:
            self._discovered[cid] = chat.title or str(cid)
            logger.info("Discovered channel %s (%s)", chat.title, cid)
            await self._persist_session()

    async def _on_channel_post(self, event) -> None:
        try:
            await self._remember_chat(await event.get_chat())
            msg = event.message
            if self.status.channel_id is not None and msg is not None and event.chat_id == self.status.channel_id:
                from app import sync
                doc_size = msg.document.size if msg.document is not None else None
                await sync.handle_post(msg.id, msg.message or "", doc_size)
        except Exception:  # noqa: BLE001
            logger.debug("channel post handler failed", exc_info=True)

    async def _on_chat_action(self, event) -> None:
        try:
            await self._remember_chat(await event.get_chat())
        except Exception:  # noqa: BLE001
            logger.debug("chat action handler failed", exc_info=True)

    async def _persist_session(self) -> None:
        """The StringSession carries the entity cache; save it after we learn
        about a channel so the bot can address it after a restart."""
        if self.client is None:
            return
        async with _db.async_session() as db:
            await settings_store.set(db, "telegram.session", self.client.session.save())
            await db.commit()

    # ---------------------------------------------------------------- channel
    def discovered(self) -> list[DiscoveredChannel]:
        return [DiscoveredChannel(cid, title) for cid, title in self._discovered.items()]

    async def _resolve_channel(self, chat_id: int):
        """Input entity for a channel id. A fresh session has no entity cache,
        but bots may address channels they belong to with access_hash 0; the
        response also teaches Telethon the real hash."""
        client = self._require_client()
        try:
            return await client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            bare = int(str(chat_id).replace("-100", "", 1)) if str(chat_id).startswith("-100") else abs(int(chat_id))
            res = await client(GetChannelsRequest([InputChannel(bare, 0)]))
            chat = res.chats[0]
            self._discovered.setdefault(chat_id, chat.title)
            return InputChannel(chat.id, chat.access_hash)

    async def read_bot_info_channel(self) -> int | None:
        """Channel id another server of this bot left in the bot's description."""
        try:
            client = self._require_client()
            info = await client(GetBotInfoRequest(bot=None, lang_code=""))
            for line in (info.description or "").splitlines():
                if line.strip().startswith(BOTINFO_KEY):
                    return int(line.strip()[len(BOTINFO_KEY):].split()[0])
        except Exception as e:  # noqa: BLE001
            logger.debug("bot info read failed: %s", e)
        return None

    async def write_bot_info_channel(self, chat_id: int) -> None:
        """Leave the channel id in the bot's description so a second server
        with the same token finds the channel without anyone posting."""
        try:
            client = self._require_client()
            info = await client(GetBotInfoRequest(bot=None, lang_code=""))
            keep = [l for l in (info.description or "").splitlines() if not l.strip().startswith(BOTINFO_KEY)]
            text = "\n".join(keep + [f"{BOTINFO_KEY}{chat_id}"]).strip()[:512]
            if text != (info.description or ""):
                await client(SetBotInfoRequest(lang_code="", bot=None, description=text))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not store channel id in bot info: %s", e)

    async def discover_from_bot_info(self) -> None:
        cid = await self.read_bot_info_channel()
        if cid and cid not in self._discovered:
            try:
                ent = await self._resolve_channel(cid)
                res = await self._require_client()(GetChannelsRequest([ent]))
                self._discovered[cid] = res.chats[0].title
                logger.info("Discovered channel from bot info: %s (%s)", res.chats[0].title, cid)
            except Exception as e:  # noqa: BLE001
                logger.debug("bot-info channel %s not usable: %s", cid, e)

    async def set_channel(self, chat_id: int) -> TelegramStatus:
        client = self._require_client()
        entity = await self._resolve_channel(chat_id)
        msg = await client.send_message(entity, TEST_MESSAGE)
        await client.delete_messages(entity, [msg.id])
        title = self._discovered.get(chat_id)
        if title is None:
            try:
                res = await client(GetChannelsRequest([entity]))
                title = getattr(res.chats[0], "title", None)
            except Exception:  # noqa: BLE001
                title = None
        async with _db.async_session() as db:
            await settings_store.set(db, "telegram.channel_id", str(chat_id))
            await settings_store.set(db, "telegram.channel_title", title or str(chat_id))
            await settings_store.set(db, "telegram.session", client.session.save())
            await db.commit()
        self.status.channel_id = chat_id
        self.status.channel_title = title or str(chat_id)
        self._entity_cache[chat_id] = entity
        await self.check_auto_delete()
        await self.write_bot_info_channel(chat_id)
        return self.status

    async def check_auto_delete(self) -> int | None:
        """Read the channel's auto-delete period. Files in a channel with
        auto-delete on are lost after that period, so this is checked at
        channel selection, at startup and hourly."""
        try:
            client = self._require_client()
            entity = await self._channel_entity()
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await client(GetFullChannelRequest(entity))
            ttl = getattr(full.full_chat, "ttl_period", None) or None
        except TelegramNotConfigured:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read channel auto-delete setting: %s", e)
            return self.status.auto_delete_seconds
        self.status.auto_delete_seconds = ttl
        if ttl:
            logger.error("CHANNEL AUTO-DELETE IS ON (%s s): every stored file will be destroyed after that time. "
                         "Turn it off in the channel settings.", ttl)
        return ttl

    async def _channel_entity(self):
        client = self._require_client()
        if self.status.channel_id is None:
            raise TelegramNotConfigured("No channel configured")
        ent = self._entity_cache.get(self.status.channel_id)
        if ent is None:
            ent = await client.get_input_entity(self.status.channel_id)
            self._entity_cache[self.status.channel_id] = ent
        return ent

    def _require_client(self) -> TelegramClient:
        if self.client is None or not self.status.connected:
            raise TelegramNotConfigured("Telegram is not connected")
        return self.client

    def ready(self) -> bool:
        return self.client is not None and self.status.connected and self.status.channel_id is not None

    # -------------------------------------------------------------- transfers
    async def upload_part(self, stream, size: int, file_name: str, caption: dict,
                          progress: Callable[[int, int], None] | None = None,
                          connections: int = 1) -> int:
        """Upload one part as a document message; returns the message id.
        With connections > 1 and a big enough file the multi-connection path is
        used; any failure there falls back to Telethon's standard uploader."""
        client = self._require_client()
        entity = await self._channel_entity()
        uploaded = None
        if connections > 1 and size > BIG_FILE_THRESHOLD:
            try:
                uploaded = await upload_parallel(client, stream, size, file_name,
                                                 connections=connections, progress=progress)
            except Exception as e:  # noqa: BLE001
                logger.warning("Parallel upload failed (%s); falling back to single connection", e)
                stream.seek(0)
                uploaded = None
        if uploaded is None:
            uploaded = await client.upload_file(
                stream, file_size=size, file_name=file_name, part_size_kb=512,
                progress_callback=progress,
            )
        msg = await client.send_file(
            entity, uploaded, force_document=True,
            caption=json.dumps(caption, separators=(",", ":")),
            attributes=[DocumentAttributeFilename(file_name=file_name)],
        )
        from app import sync
        sync.note_message_id(msg.id)
        return msg.id

    async def get_document(self, message_id: int):
        client = self._require_client()
        entity = await self._channel_entity()
        msg = await client.get_messages(entity, ids=message_id)
        if msg is None or msg.document is None:
            raise FileNotFoundError(f"Message {message_id} has no document")
        return msg.document

    async def iter_download(self, document, offset: int, length: int, chunk: int = 1024 * 1024) -> AsyncIterator[bytes]:
        """Yield exactly `length` bytes of `document` starting at `offset`."""
        client = self._require_client()
        remaining = length
        async for block in client.iter_download(document, offset=offset, request_size=chunk):
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining]
            remaining -= len(block)
            yield bytes(block)
            if remaining <= 0:
                break

    async def probe_last_message_id(self) -> int:
        """Bots cannot read channel history, but they can fetch messages by id.
        Posting and deleting a marker gives the current upper bound."""
        client = self._require_client()
        entity = await self._channel_entity()
        msg = await client.send_message(entity, "OpenTelegramStorage index scan marker (auto-deleted)")
        await client.delete_messages(entity, [msg.id])
        return msg.id

    async def send_text(self, text: str) -> int:
        client = self._require_client()
        entity = await self._channel_entity()
        msg = await client.send_message(entity, text, link_preview=False)
        from app import sync
        sync.note_message_id(msg.id)
        return msg.id

    async def fetch_messages(self, ids: list[int], include_text: bool = False) -> list[dict]:
        """Return [{id, caption, size, file_name}] for messages that exist and
        carry a document (or, with include_text, any text message with size
        None). Deleted ids are simply absent."""
        client = self._require_client()
        entity = await self._channel_entity()
        out: list[dict] = []
        msgs = await client.get_messages(entity, ids=ids)
        for m in msgs or []:
            if m is None:
                continue
            if m.document is None:
                if include_text and m.message:
                    out.append({"id": m.id, "caption": m.message, "size": None, "file_name": None})
                continue
            name = None
            for attr in m.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    name = attr.file_name
            out.append({"id": m.id, "caption": m.message or "", "size": m.document.size, "file_name": name})
        return out

    async def delete_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        client = self._require_client()
        entity = await self._channel_entity()
        for i in range(0, len(message_ids), 100):
            await client.delete_messages(entity, message_ids[i:i + 100])

    async def shutdown(self) -> None:
        await self._disconnect()


manager = TelegramManager()

__all__ = ["manager", "TelegramManager", "TelegramNotConfigured", "FloodWaitError", "TelegramStatus"]
