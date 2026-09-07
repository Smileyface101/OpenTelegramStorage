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

from app import config, settings_store
from app import db as _db
from app.models import File, FilePart, FileStatus
from app.transfers.io import RangeReader, hash_ranges, part_name, plan_parts

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.0


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


def caption_for(file: File, part: FilePart, total_parts: int) -> dict:
    """Self-describing caption so the channel alone can rebuild the index."""
    return {
        "otg": 1,
        "id": file.id,
        "name": file.name,
        "size": file.size,
        "part": part.index + 1,
        "of": total_parts,
        "psize": part.size,
        "sha256": part.sha256,
        "archive": file.is_archive,
    }


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
        while not self._stop.is_set():
            worked = False
            try:
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
        """Process the oldest queued file. Returns True if something was done."""
        async with _db.async_session() as db:
            file = (await db.execute(
                select(File).options(selectinload(File.parts))
                .where(File.status == FileStatus.QUEUED)
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
                wait = getattr(e, "seconds", None)
                if wait is not None:  # FloodWaitError: not our fault, don't burn a retry
                    logger.warning("Flood wait %ss on file %s", wait, file_id)
                    file.status = FileStatus.QUEUED
                    file.error = f"Telegram asked us to wait {wait}s"
                    await db.commit()
                    await asyncio.sleep(min(int(wait) + 1, 3600))
                    return True
                file.retries += 1
                file.error = str(e)[:1000]
                file.status = FileStatus.FAILED if file.retries >= max_retries else FileStatus.QUEUED
                logger.exception("transfer failed for %s (retry %s)", file_id, file.retries)
                await db.commit()
                progress.clear(file_id)
                if file.status == FileStatus.QUEUED:
                    await asyncio.sleep(min(30, 2 ** file.retries))
                return True
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
        for part in file.parts:
            if part.message_id is not None:
                continue
            name = part_name(file.name, part.index, total)

            def _cb(sent: int, size: int, _idx=part.index) -> None:
                progress.set(file.id, _idx, sent, size)

            progress.set(file.id, part.index, 0, part.size)
            with RangeReader(file.staging_path, part.offset, part.size, name=name) as reader:
                message_id = await self.manager.upload_part(
                    reader, part.size, name, caption_for(file, part, total), progress=_cb)
            part.message_id = message_id
            part.uploaded_at = datetime.utcnow()
            await db.commit()

        # 3) Done: drop the local copy.
        file.status = FileStatus.READY
        file.ready_at = datetime.utcnow()
        staging = file.staging_path
        file.staging_path = None
        await db.commit()
        progress.clear(file.id)
        if staging:
            try:
                os.remove(staging)
            except FileNotFoundError:
                pass
        logger.info("File %s (%s) is in the channel: %d part(s)", file.id, file.name, total)


worker: TransferWorker | None = None


def kick() -> None:
    if worker is not None:
        worker.kick()
