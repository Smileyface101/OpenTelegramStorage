"""Admin status snapshot + in-memory ring buffer of recent warnings/errors."""
import logging
import time
from collections import deque
from datetime import datetime

from sqlalchemy import func, select

from app import __version__, config, db as _db, maintenance, recovery, settings_store
from app.models import File, FileStatus, Upload, UploadStatus
from app.transfers import importer
from app.transfers.worker import progress

STARTED_AT = datetime.utcnow()
_t0 = time.monotonic()
recent_errors: deque = deque(maxlen=50)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            recent_errors.append({
                "at": datetime.utcfromtimestamp(record.created).isoformat(),
                "level": record.levelname, "logger": record.name,
                "message": self.format(record)[:500],
            })
        except Exception:  # noqa: BLE001
            pass


def install_log_capture() -> None:
    h = RingHandler(level=logging.WARNING)
    h.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(h)


async def snapshot(manager, worker) -> dict:
    async with _db.async_session() as db:
        counts = {s.value: 0 for s in FileStatus}
        for status, n in (await db.execute(select(File.status, func.count(File.id)).group_by(File.status))).all():
            counts[status.value] = n
        stored = await db.scalar(select(func.coalesce(func.sum(File.size), 0)).where(File.status == FileStatus.READY))
        sessions = await db.scalar(select(func.count(Upload.id)).where(Upload.status == UploadStatus.ACTIVE))
        stale_hours = await settings_store.get_int(db, "transfer.stale_upload_hours", 72)
    st = manager.status
    task = getattr(worker, "_task", None)
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return {
        "version": __version__,
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int(time.monotonic() - _t0),
        "telegram": {"configured": st.configured, "connected": st.connected, "bot_username": st.bot_username,
                     "channel": ({"id": st.channel_id, "title": st.channel_title} if st.channel_id else None),
                     "error": st.error},
        "worker": {"alive": bool(task and not task.done()), "in_flight": progress.active, "files_by_status": counts,
                   "bytes_in_channel": int(stored or 0)},
        "uploads": {"active_sessions": sessions or 0, "stale_after_hours": stale_hours},
        "staging": maintenance.staging_usage(),
        "database": {"path": str(config.DB_PATH), "size_bytes": db_size},
        "import": {"enabled": importer.enabled(), "dir": str(config.IMPORT_DIR), "active": importer.active},
        "recovery": recovery.snapshot(),
        "last_cleanup": maintenance.last_run,
        "recent_errors": list(recent_errors)[-20:][::-1],
    }
