"""In-memory stand-in for TelegramManager used by the worker/download tests."""
from typing import AsyncIterator


from app.telegram.manager import TelegramStatus


class FakeManager:
    def __init__(self) -> None:
        self.status = TelegramStatus(configured=True, connected=True, bot_username="fake_bot", channel_id=-1001, channel_title="Fake")
        self.messages: dict[int, tuple[str, bytes, dict]] = {}
        self.deleted: list[int] = []
        self._next = 100
        self.fail_next = 0
        self.is_ready = True

    def ready(self) -> bool:
        return self.is_ready

    async def upload_part(self, stream, size, file_name, caption, progress=None, connections=1):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated telegram failure")
        data = stream.read()
        assert len(data) == size, (len(data), size)
        if progress:
            progress(size, size)
        mid = self._next
        self._next += 1
        self.messages[mid] = (file_name, data, caption)
        return mid

    async def probe_last_message_id(self):
        mid = self._next
        self._next += 1
        return mid

    async def fetch_messages(self, ids, include_text=False):
        import json
        out = []
        for i in ids:
            if i in self.messages:
                name, data, cap = self.messages[i]
                if data is None:  # text-only message (event)
                    if include_text:
                        out.append({"id": i, "caption": cap, "size": None, "file_name": None})
                    continue
                out.append({"id": i, "caption": json.dumps(cap), "size": len(data), "file_name": name})
        return out

    async def send_text(self, text):
        mid = self._next
        self._next += 1
        self.messages[mid] = (None, None, text)
        return mid

    def stored_bytes(self) -> bytes:
        return b"".join(v[1] for _k, v in sorted(self.messages.items()) if v[1] is not None)

    async def get_document(self, message_id):
        if message_id not in self.messages:
            raise FileNotFoundError(message_id)
        return message_id

    async def iter_download(self, document, offset: int, length: int, chunk: int = 7) -> AsyncIterator[bytes]:
        data = self.messages[document][1][offset:offset + length]
        for i in range(0, len(data), chunk):
            yield data[i:i + chunk]

    async def delete_messages(self, ids):
        for i in ids:
            self.deleted.append(i)
            self.messages.pop(i, None)

    def plaintext_bytes(self, key: bytes | None) -> bytes:
        """What the channel holds, decrypted where captions say so."""
        from app import crypto
        out = b""
        for _k, (_name, blob, cap) in sorted(self.messages.items()):
            if blob is None:
                continue
            enc = cap.get("enc")
            if enc and key is not None:
                salt = crypto.parse_header(blob[:crypto.HEADER])
                dec = crypto.BlockDecryptor(key, salt, 0, int(cap["psize"]))
                out += dec.feed(blob[crypto.HEADER:]) + dec.finish()
            else:
                out += blob
        return out
