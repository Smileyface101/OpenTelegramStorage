"""Background transfer worker.

One asyncio task per process picks queued files and moves them, part by part,
into the Telegram channel. Progress of the part in flight is kept in memory
and exposed through the transfers API. Each part is its own message, so a
crash mid-file resumes at the next un-uploaded part."""
import asyncio
import logging
import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import config, crypto, settings_store
from app import db as _db
from app.models import File, FilePart, FileStatus, Folder, User
from app.transfers.io import RangeReader, hash_ranges, part_name, plan_parts
from app.transfers.staging import part_path

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
RECONNECT_INTERVAL = 60.0
CLEANUP_INTERVAL = 10 * 60.0
SYNC_CATCHUP_INTERVAL = 60.0
SYNC_RECONCILE_INTERVAL = 60 * 60.0


class Progress:
    """In-memory progress of parts currently uploading: file_id -> dict."""

    def __init__(self) -> None:
        self.active: dict[str, dict] = {}

    def set(self, file_id: str, part_index: int, sent: int, total: int) -> None:
        self.active[file_id] = {"part_index": part_index, "sent": sent, "total": total,
                                "updated_at": datetime.utcnow().isoformat()}

    def clear(self, file_id: str) -> None:
        self.active.pop(file_id, None)


progress = Progress()


async def folder_path(db, folder_id: int | None) -> str | None:
    """"a/b/c" for the file's folder, so a rebuild can restore the tree."""
    names: list[str] = []
    seen: set[int] = set()
    while folder_id is not None and folder_id not in seen:
        seen.add(folder_id)
        folder = await db.get(Folder, folder_id)
        if folder is None:
            break
        names.append(folder.name)
        folder_id = folder.parent_id
    return "/".join(reversed(names)) or None


def caption_for(file: File, part: FilePart, total_parts: int, path: str | None = None, by: str | None = None) -> dict:
    """Self-describing caption so the channel alone can rebuild the index."""
    cap = {
        "ots": 1,
        "id": file.id,
        "name": file.name,
        "size": file.size,
        "part": part.index + 1,
        "of": total_parts,
        "psize": part.size,
        "sha256": part.sha256,
        "archive": file.is_archive,
        "path": path,
    }
    if file.encrypted:
        cap["enc"] = {"v": 1, "kid": file.key_id, "salt": part.enc_salt, "ct": part.enc_size}
    if by:
        cap["by"] = by
    return cap


async def uploader_label(db, file: File) -> str | None:
    if file.uploaded_by:
        return file.uploaded_by
    from app import sync
    user = await db.get(User, file.owner_id)
    return await sync.label(db, user.username if user else "?")


class TransferWorker:
    def __init__(self, manager, poll_interval: float = POLL_INTERVAL) -> None:
        self.manager = manager
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="transfer-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    def kick(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        # Anything left mid-flight by a previous process goes back to the queue.
        await self._recover_stale()
        loop = asyncio.get_event_loop()
        next_reconnect = loop.time() + RECONNECT_INTERVAL
        next_cleanup = loop.time() + 60.0
        next_catchup = loop.time() + 15.0
        next_reconcile = loop.time() + SYNC_RECONCILE_INTERVAL
        handshake_done = False
        while not self._stop.is_set():
            worked = False
            try:
                now = loop.time()
                if now >= next_reconnect:
                    next_reconnect = now + RECONNECT_INTERVAL
                    if getattr(self.manager, "needs_reconnect", lambda: False)():
                        logger.info("Telegram is configured but offline; reconnecting")
                        try:
                            await self.manager.reconnect()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("Telegram reconnect failed: %s", e)
                if now >= next_cleanup:
                    next_cleanup = now + CLEANUP_INTERVAL
                    from app import maintenance
                    await maintenance.cleanup(self.manager)
                if not handshake_done and self.manager.ready():
                    handshake_done = True
                    next_catchup = now + SYNC_CATCHUP_INTERVAL
                    from app import sync
                    await sync.startup_handshake(self.manager)
                elif now >= next_catchup and self.manager.ready():
                    next_catchup = now + SYNC_CATCHUP_INTERVAL
                    from app import sync
                    await sync.catch_up(self.manager)
                from app import sync as _sync
                if _sync.take_hello_request() and self.manager.ready():
                    async with _db.async_session() as db:
                        name = (await settings_store.get(db, "workspace.name")) or "server"
                    await _sync.publish_layout(self.manager, f"{name}/system")
                if now >= next_reconcile and self.manager.ready():
                    next_reconcile = now + SYNC_RECONCILE_INTERVAL
                    from app import sync
                    await sync.reconcile(self.manager)
                if self.manager.ready():
                    worked = await self.process_one()
            except Exception:  # noqa: BLE001
                logger.exception("transfer worker loop error")
            if not worked:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _recover_stale(self) -> None:
        async with _db.async_session() as db:
            rows = (await db.execute(select(File).where(
                File.status.in_([FileStatus.HASHING, FileStatus.UPLOADING])))).scalars().all()
            for f in rows:
                f.status = FileStatus.QUEUED
            await db.commit()

    async def process_one(self) -> bool:
        """Do one unit of work. Streaming parts (browser still sending, or
        finished) come first so uploads overlap; then whole-file staged jobs
        (archives). Returns True if something was done."""
        if await self._process_streaming_part():
            return True
        if await self._process_whole_file():
            return True
        return await self._finalize_ready()

    async def _finalize_ready(self) -> bool:
        """Safety net for the race between "last part landed" (worker) and
        "browser/import finished sending" (API): whichever side saw a stale
        status leaves the file QUEUED with every part in the channel. Flip
        such files to READY."""
        async with _db.async_session() as db:
            rows = (await db.execute(
                select(File).options(selectinload(File.parts))
                .where(File.status.in_([FileStatus.QUEUED, FileStatus.UPLOADING]), File.staging_path.is_(None))
            )).scalars().all()
            changed = False
            for f in rows:
                if f.parts and all(p.message_id is not None for p in f.parts):
                    f.status = FileStatus.READY
                    f.ready_at = f.ready_at or datetime.utcnow()
                    f.error = None
                    changed = True
                    logger.info("File %s (%s) is in the channel: %d part(s)", f.id, f.name, len(f.parts))
            if changed:
                await db.commit()
            return changed

    async def _process_streaming_part(self) -> bool:
        async with _db.async_session() as db:
            part = (await db.execute(
                select(FilePart).join(File, File.id == FilePart.file_id)
                .options(selectinload(FilePart.file).selectinload(File.parts))
                .where(File.status.in_([FileStatus.RECEIVING, FileStatus.QUEUED, FileStatus.UPLOADING]),
                       File.staging_path.is_(None),
                       FilePart.message_id.is_(None),
                       FilePart.staging_path.is_not(None),
                       FilePart.received >= FilePart.size)
                .order_by(File.created_at, FilePart.index).limit(1)
            )).scalar_one_or_none()
            if part is None:
                return False
            file = part.file
            file_id, part_index = file.id, part.index
            if not os.path.exists(part.staging_path):
                # Staging vanished (disk cleaned?) — ask for that range again.
                part.received = 0
                part.sha256 = None
                file.error = f"Part {part.index + 1} was lost from staging; resume the upload to resend it"
                await db.commit()
                return True
            max_retries = await settings_store.get_int(db, "transfer.max_retries", config.MAX_UPLOAD_RETRIES)
            try:
                await self._send_part(db, file, part)
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                file = await db.get(File, file_id, options=[selectinload(File.parts)])
                await self._handle_failure(db, file, e, max_retries)
                progress.clear(file_id)
                return True
            return True

    async def _send_part(self, db, file: File, part: FilePart) -> None:
        total = len(file.parts)
        name = part_name(file.name, part.index, total)
        path = await folder_path(db, file.folder_id)
        by = await uploader_label(db, file)
        if file.uploaded_by is None:
            file.uploaded_by = by
        connections = await settings_store.get_int(db, "transfer.upload_connections", 4)
        if file.status == FileStatus.QUEUED:
            file.status = FileStatus.UPLOADING
            await db.commit()

        def _cb(sent: int, size: int, _idx=part.index) -> None:
            progress.set(file.id, _idx, sent, size)

        progress.set(file.id, part.index, 0, part.size)
        with RangeReader(part.staging_path, 0, part.size, name=name) as raw:
            reader, size = await self._maybe_encrypt(db, file, part, raw)
            message_id = await self.manager.upload_part(
                reader, size, name, caption_for(file, part, total, path, by), progress=_cb,
                connections=max(1, min(16, connections)))
        part.message_id = message_id
        part.uploaded_at = datetime.utcnow()
        staged = part.staging_path
        file.error = None
        await db.commit()
        # Re-read before deciding: the API may have flipped RECEIVING -> QUEUED
        # while this part was in flight.
        await db.refresh(file, attribute_names=["status", "parts"])
        done = all(p.message_id is not None for p in file.parts)
        if done and file.status != FileStatus.RECEIVING:
            file.status = FileStatus.READY
            file.ready_at = datetime.utcnow()
        await db.commit()
        progress.clear(file.id)
        try:
            os.remove(staged)
        except FileNotFoundError:
            pass
        from app import sync as _sync2
        _sync2.bump()
        if done and file.status == FileStatus.READY:
            logger.info("File %s (%s) is in the channel: %d part(s)", file.id, file.name, total)

    async def _maybe_encrypt(self, db, file: File, part: FilePart, raw):
        """Wrap the plaintext reader when the file is encrypted. The salt is
        fixed per part (kept on retries so the caption stays truthful)."""
        if not file.encrypted:
            return raw, part.size
        key = await crypto.get_key(db)
        if key is None or crypto.key_id_of(key) != file.key_id:
            raise RuntimeError("content key for this file is not available (key id %s)" % file.key_id)
        salt = bytes.fromhex(part.enc_salt) if part.enc_salt else None
        enc = crypto.EncryptingReader(raw, part.size, key, salt)
        if part.enc_salt is None:
            part.enc_salt = enc.salt.hex()
            part.enc_size = enc.size
            await db.commit()
        return enc, enc.size

    async def _handle_failure(self, db, file: File, e: Exception, max_retries: int) -> None:
        wait = getattr(e, "seconds", None)
        if wait is not None:  # FloodWaitError: not our fault, don't burn a retry
            logger.warning("Flood wait %ss on file %s", wait, file.id)
            file.error = f"Telegram asked us to wait {wait}s"
            if file.status == FileStatus.UPLOADING:
                file.status = FileStatus.QUEUED
            await db.commit()
            await asyncio.sleep(min(int(wait) + 1, 3600))
            return
        file.retries += 1
        file.error = str(e)[:1000]
        if file.retries >= max_retries:
            file.status = FileStatus.FAILED
        elif file.status == FileStatus.UPLOADING:
            file.status = FileStatus.QUEUED
        logger.exception("transfer failed for %s (retry %s)", file.id, file.retries)
        await db.commit()
        if file.status != FileStatus.FAILED:
            await asyncio.sleep(min(30, 2 ** file.retries))

    async def _process_whole_file(self) -> bool:
        """Archives built server-side are staged as one file and split here."""
        async with _db.async_session() as db:
            file = (await db.execute(
                select(File).options(selectinload(File.parts))
                .where(File.status == FileStatus.QUEUED, File.staging_path.is_not(None))
                .order_by(File.created_at).limit(1)
            )).scalar_one_or_none()
            if file is None:
                return False
            file_id = file.id
            max_retries = await settings_store.get_int(db, "transfer.max_retries", config.MAX_UPLOAD_RETRIES)
            try:
                await self._process(db, file)
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                file = await db.get(File, file_id, options=[selectinload(File.parts)])
                file.status = FileStatus.QUEUED
                await self._handle_failure(db, file, e, max_retries)
                progress.clear(file_id)
            return True

    async def _process(self, db, file: File) -> None:
        if not file.staging_path or not os.path.exists(file.staging_path):
            raise FileNotFoundError("Staged file is missing; upload it again")

        # 1) Plan + hash parts (once).
        if not file.parts:
            file.status = FileStatus.HASHING
            await db.commit()
            plan = plan_parts(file.size, file.part_size)
            whole, per_part = await asyncio.to_thread(hash_ranges, file.staging_path, plan)
            file.sha256 = whole
            for (index, offset, length), digest in zip(plan, per_part):
                db.add(FilePart(file_id=file.id, index=index, offset=offset, size=length, sha256=digest))
            await db.commit()
            await db.refresh(file, attribute_names=["parts"])

        # 2) Upload the parts that are not in the channel yet.
        file.status = FileStatus.UPLOADING
        file.error = None
        await db.commit()
        total = len(file.parts)
        path = await folder_path(db, file.folder_id)
        by = await uploader_label(db, file)
        if file.uploaded_by is None:
            file.uploaded_by = by
        connections = await settings_store.get_int(db, "transfer.upload_connections", 4)
        for part in file.parts:
            if part.message_id is not None:
                continue
            name = part_name(file.name, part.index, total)

            def _cb(sent: int, size: int, _idx=part.index) -> None:
                progress.set(file.id, _idx, sent, size)

            progress.set(file.id, part.index, 0, part.size)
            with RangeReader(file.staging_path, part.offset, part.size, name=name) as raw:
                reader, size = await self._maybe_encrypt(db, file, part, raw)
                message_id = await self.manager.upload_part(
                    reader, size, name, caption_for(file, part, total, path, by), progress=_cb,
                    connections=max(1, min(16, connections)))
            part.message_id = message_id
            part.uploaded_at = datetime.utcnow()
            await db.commit()

        # 3) Done: drop the local copy.
        file.status = FileStatus.READY
        file.ready_at = datetime.utcnow()
        staging = file.staging_path
        keep = file.keep_source
        file.staging_path = None
        await db.commit()
        progress.clear(file.id)
        if staging and not keep:
            try:
                os.remove(staging)
            except FileNotFoundError:
                pass
        logger.info("File %s (%s) is in the channel: %d part(s)", file.id, file.name, total)


worker: TransferWorker | None = None


def kick() -> None:
    if worker is not None:
        worker.kick()
