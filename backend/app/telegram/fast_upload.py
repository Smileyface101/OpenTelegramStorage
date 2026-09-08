"""Multi-connection upload.

Telethon's upload_file pushes 512 KB parts one after another over the main
connection, which tops out around 3-5 MB/s. Telegram allows the same file's
parts to be saved from several connections concurrently, so we open N extra
senders to the session's data center and feed them parts from a shared
reader. On any setup problem the caller falls back to the classic path."""
import asyncio
import logging
from typing import Callable

from telethon import TelegramClient, helpers
from telethon.tl import functions, types

logger = logging.getLogger(__name__)

PART_SIZE = 512 * 1024
BIG_FILE_THRESHOLD = 10 * 1024 * 1024
PART_RETRIES = 3


class _Reader:
    """Hands out (index, bytes) in order from a stream shared by the workers."""

    def __init__(self, stream, size: int) -> None:
        self.stream = stream
        self.size = size
        self.index = 0
        self.read_bytes = 0
        self.lock = asyncio.Lock()

    async def next(self) -> tuple[int, bytes] | None:
        async with self.lock:
            if self.read_bytes >= self.size:
                return None
            n = min(PART_SIZE, self.size - self.read_bytes)
            data = await asyncio.to_thread(self.stream.read, n)
            if len(data) != n:
                raise IOError("stream ended early")
            idx = self.index
            self.index += 1
            self.read_bytes += n
            return idx, data


async def _make_sender(client: TelegramClient, dc_id: int):
    """Extra MTProto connection to the session's own data center.

    Telegram refuses ExportAuthorization for the DC you are connected to, so
    (as FastTelethon does) a same-DC sender simply reuses the session's auth
    key on a fresh connection. Private Telethon API, pinned by requirements."""
    from telethon.network import MTProtoSender
    dc = await client._get_dc(dc_id)  # noqa: SLF001
    sender = MTProtoSender(client.session.auth_key, loggers=client._log)  # noqa: SLF001
    await sender.connect(client._connection(  # noqa: SLF001
        dc.ip_address, dc.port, dc.id, loggers=client._log, proxy=client._proxy))  # noqa: SLF001
    return sender


async def upload_parallel(client: TelegramClient, stream, size: int, file_name: str, *,
                          connections: int = 4,
                          progress: Callable[[int, int], None] | None = None) -> types.InputFileBig:
    """Upload `size` bytes from `stream` using `connections` senders. Only for
    files above BIG_FILE_THRESHOLD (Telegram's "big file" API has no md5)."""
    if size <= BIG_FILE_THRESHOLD:
        raise ValueError("use client.upload_file for small files")
    file_id = helpers.generate_random_long()
    part_count = (size + PART_SIZE - 1) // PART_SIZE
    dc_id = client.session.dc_id
    reader = _Reader(stream, size)
    sent = 0
    sent_lock = asyncio.Lock()
    senders = []
    try:
        # Sequential so a failure leaves nothing half-connected behind.
        for _ in range(connections):
            senders.append(await _make_sender(client, dc_id))

        async def worker(sender) -> None:
            nonlocal sent
            while True:
                item = await reader.next()
                if item is None:
                    return
                idx, data = item
                req = functions.upload.SaveBigFilePartRequest(file_id, idx, part_count, data)
                for attempt in range(PART_RETRIES):
                    try:
                        ok = await sender.send(req)
                        if not ok:
                            raise RuntimeError(f"SaveBigFilePart returned False for part {idx}")
                        break
                    except Exception:  # noqa: BLE001
                        if attempt == PART_RETRIES - 1:
                            raise
                        await asyncio.sleep(1 + attempt)
                async with sent_lock:
                    sent += len(data)
                    if progress:
                        progress(sent, size)

        await asyncio.gather(*[worker(s) for s in senders])
    finally:
        for s in senders:
            try:
                await s.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return types.InputFileBig(id=file_id, parts=part_count, name=file_name)
