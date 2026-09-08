"""Housekeeping: abandoned uploads and orphaned staging files.

An upload the browser never finished keeps a row, per-part staging files and
possibly parts already in the channel. After `transfer.stale_upload_hours`
without activity (default 72 h, 0 disables) it is removed completely: channel
parts deleted, staging freed, rows gone. Staging files that no row references
are deleted once they are an hour old (guards against a crash between write
and commit)."""
import logging
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import config, db as _db, settings_store
from app.models import Bundle, File, FilePart, FileStatus, Share, Upload, UploadStatus
from app.transfers import staging

logger = logging.getLogger(__name__)

ORPHAN_MIN_AGE_SECONDS = 3600
last_run: dict | None = None


async def _takedown(manager, f: File) -> None:
    ids = [p.message_id for p in f.parts if p.message_id is not None]
    if ids:
        try:
            await manager.delete_messages(ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup: could not delete channel messages %s for %s: %s", ids, f.id, e)
    if f.staging_path and not f.keep_source:
        try:
            os.remove(f.staging_path)
        except FileNotFoundError:
            pass
    staging.remove_part_files(f.parts)
    staging.forget(f.id)


async def cleanup(manager) -> dict:
    global last_run
    summary = {"at": datetime.utcnow().isoformat(), "stale_uploads": 0, "stale_bundles": 0,
               "orphan_files_removed": 0, "orphan_bytes_freed": 0, "stale_receiving_files": 0, "errors": []}
    try:
        async with _db.async_session() as db:
            hours = await settings_store.get_int(db, "transfer.stale_upload_hours", 72)
            if hours > 0:
                cutoff = datetime.utcnow() - timedelta(hours=hours)
                # Plain uploads (streaming into their own File).
                ups = (await db.execute(select(Upload).where(
                    Upload.status == UploadStatus.ACTIVE, Upload.bundle_id.is_(None), Upload.updated_at < cutoff))).scalars().all()
                for up in ups:
                    if up.file_id:
                        f = await db.get(File, up.file_id, options=[selectinload(File.parts)])
                        if f is not None:
                            await _takedown(manager, f)
                            await db.delete(f)
                    elif up.path:
                        try:
                            os.remove(up.path)
                        except FileNotFoundError:
                            pass
                    await db.delete(up)
                    summary["stale_uploads"] += 1
                # Bundles: stale when no member moved since the cutoff (or never started).
                bundles = (await db.execute(select(Bundle).where(Bundle.created_at < cutoff))).scalars().all()
                for b in bundles:
                    members = (await db.execute(select(Upload).where(Upload.bundle_id == b.id))).scalars().all()
                    latest = max([m.updated_at for m in members] + [b.created_at])
                    if latest >= cutoff:
                        continue
                    if b.file_id:
                        f = await db.get(File, b.file_id, options=[selectinload(File.parts)])
                        if f is not None:
                            await _takedown(manager, f)
                            await db.delete(f)
                    for m in members:
                        await db.delete(m)
                    await db.delete(b)
                    summary["stale_bundles"] += 1
                # RECEIVING files nobody is feeding any more (session rows gone).
                receiving = (await db.execute(select(File).options(selectinload(File.parts)).where(
                    File.status == FileStatus.RECEIVING, File.created_at < cutoff))).scalars().all()
                for f in receiving:
                    has_session = await db.scalar(select(Upload.id).where(Upload.file_id == f.id).limit(1))
                    has_bundle = await db.scalar(select(Bundle.id).where(Bundle.file_id == f.id).limit(1))
                    if has_session or has_bundle:
                        continue
                    await _takedown(manager, f)
                    await db.delete(f)
                    summary["stale_receiving_files"] += 1
                await db.commit()

            # Share links expired for more than 30 days.
            old = datetime.utcnow() - timedelta(days=30)
            for sh in (await db.execute(select(Share).where(Share.expires_at.is_not(None), Share.expires_at < old))).scalars().all():
                await db.delete(sh)
                summary["expired_shares"] = summary.get("expired_shares", 0) + 1
            await db.commit()

            # Orphaned staging files.
            referenced = set((await db.execute(select(FilePart.staging_path).where(FilePart.staging_path.is_not(None)))).scalars())
            referenced |= set((await db.execute(select(File.staging_path).where(File.staging_path.is_not(None)))).scalars())
            referenced |= set((await db.execute(select(Upload.path).where(Upload.path != ""))).scalars())
        now = time.time()
        for entry in config.STAGING_DIR.iterdir():
            if not entry.is_file() or str(entry) in referenced:
                continue
            try:
                st = entry.stat()
                if now - st.st_mtime < ORPHAN_MIN_AGE_SECONDS:
                    continue
                entry.unlink()
                summary["orphan_files_removed"] += 1
                summary["orphan_bytes_freed"] += st.st_size
            except OSError as e:
                summary["errors"].append(f"{entry.name}: {e}")
    except Exception as e:  # noqa: BLE001
        logger.exception("cleanup failed")
        summary["errors"].append(str(e))
    last_run = summary
    if any(summary[k] for k in ("stale_uploads", "stale_bundles", "orphan_files_removed", "stale_receiving_files")):
        logger.info("cleanup: %s", summary)
    return summary


def staging_usage() -> dict:
    used = 0
    count = 0
    try:
        for entry in config.STAGING_DIR.iterdir():
            if entry.is_file():
                used += entry.stat().st_size
                count += 1
    except FileNotFoundError:
        pass
    import shutil
    du = shutil.disk_usage(config.STAGING_DIR if config.STAGING_DIR.exists() else config.DATA_DIR)
    return {"dir": str(config.STAGING_DIR), "used_bytes": used, "file_count": count,
            "free_bytes": du.free, "total_bytes": du.total}
