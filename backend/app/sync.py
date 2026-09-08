"""Shared-workspace sync: several servers, one bot, one channel.

The channel is the source of truth. Every part carries a caption; every
metadata change is an event message. This module keeps the local index in
step with what other servers post:

- handle_post: a channel post arrived (live). Index parts we do not have.
- handle_event: a JSON event message arrived (delete for now).
- catch_up: scan message ids we have not seen (startup / periodic).
- reconcile: verify indexed parts still exist; drop files deleted elsewhere.

Only active when workspace.mode == "shared"."""
import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import db as _db, settings_store
from app.models import File, FilePart, FileStatus, Folder, User, UserRole
from app.recovery import parse_caption

logger = logging.getLogger(__name__)

EVENT_KEY = "ots-ev"
BATCH = 100


def make_event(kind: str, **fields) -> str:
    ev = {EVENT_KEY: 1, "t": kind, "ts": datetime.utcnow().isoformat(), **fields}
    return json.dumps(ev, separators=(",", ":"))


def parse_event(text: str) -> dict | None:
    if not text or not text.startswith("{"):
        return None
    try:
        ev = json.loads(text)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) and ev.get(EVENT_KEY) == 1 and "t" in ev else None


async def _owner_id(db) -> int | None:
    """Files synced from other servers belong to this server's first admin."""
    return await db.scalar(select(User.id).where(User.role == UserRole.ADMIN).order_by(User.id).limit(1))


async def _ensure_path(db, owner_id: int, path: str | None) -> int | None:
    if not path:
        return None
    parent_id = None
    for seg in [s for s in path.split("/") if s and s not in (".", "..")]:
        seg = seg[:255]
        existing = await db.scalar(select(Folder).where(
            Folder.owner_id == owner_id, Folder.parent_id == parent_id, Folder.name == seg))
        if existing is None:
            existing = Folder(owner_id=owner_id, parent_id=parent_id, name=seg)
            db.add(existing)
            await db.flush()
        parent_id = existing.id
    return parent_id


async def _bump_last_seen(db, message_id: int) -> None:
    last = await settings_store.get_int(db, "workspace.last_message_id", 0)
    if message_id > last:
        await settings_store.set(db, "workspace.last_message_id", str(message_id))


async def index_part(db, message_id: int, caption: dict, doc_size: int) -> bool:
    """Add one part from a caption. Returns True if the index changed."""
    fid = str(caption["id"])
    f = await db.get(File, fid, options=[selectinload(File.parts)])
    total = int(caption["of"])
    idx = int(caption["part"]) - 1
    enc = caption.get("enc") if isinstance(caption.get("enc"), dict) else None
    psize = int(caption.get("psize", doc_size))
    if f is None:
        owner = await _owner_id(db)
        if owner is None:
            return False
        f = File(id=fid, owner_id=owner, folder_id=await _ensure_path(db, owner, caption.get("path")),
                 name=str(caption["name"])[:255], size=int(caption["size"]), is_archive=bool(caption.get("archive")),
                 part_size=max(psize, 1), status=FileStatus.SYNCING,
                 encrypted=bool(enc), key_id=enc.get("kid") if enc else None,
                 uploaded_by=(caption.get("by") or None))
        db.add(f)
        await db.flush()
        # Plan every part; the ones we have not seen yet stay without message ids.
        offset = 0
        for i in range(total):
            n = min(f.part_size, max(f.size - offset, 0)) if i < total - 1 else f.size - offset
            db.add(FilePart(file_id=fid, index=i, offset=offset, size=max(n, 0)))
            offset += max(n, 0)
        await db.flush()
        await db.refresh(f, attribute_names=["parts"])
        logger.info("sync: new file %s (%s) from %s", fid, f.name, caption.get("by"))
    part = next((p for p in f.parts if p.index == idx), None)
    if part is None:
        return False
    changed = False
    if part.message_id is None:
        part.message_id = message_id
        part.size = psize
        part.sha256 = caption.get("sha256")
        part.enc_salt = enc.get("salt") if enc else None
        part.enc_size = enc.get("ct") if enc else None
        part.received = psize
        part.uploaded_at = datetime.utcnow()
        changed = True
    if f.status == FileStatus.SYNCING and all(p.message_id is not None for p in f.parts):
        # Recompute offsets from the real part sizes now that all are known.
        off = 0
        for p in sorted(f.parts, key=lambda p: p.index):
            p.offset = off
            off += p.size
        f.status = FileStatus.READY
        f.ready_at = datetime.utcnow()
        changed = True
        logger.info("sync: %s complete (%d parts)", f.name, total)
    return changed


async def apply_event(db, ev: dict) -> bool:
    kind = ev.get("t")
    if kind == "delete":
        f = await db.get(File, str(ev.get("id")), options=[selectinload(File.parts)])
        if f is None:
            return False
        from app.transfers.staging import remove_part_files
        remove_part_files(f.parts)
        await db.delete(f)
        logger.info("sync: file %s deleted by %s", ev.get("id"), ev.get("by"))
        return True
    return False


async def handle_post(message_id: int, text: str, doc_size: int | None) -> None:
    """Live channel post (called from the Telegram manager)."""
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return
        cap = parse_caption(text) if doc_size is not None else None
        ev = parse_event(text) if cap is None else None
        try:
            if cap is not None:
                await index_part(db, message_id, cap, doc_size)
            elif ev is not None:
                await apply_event(db, ev)
            await _bump_last_seen(db, message_id)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("sync: failed to apply message %s", message_id)


async def catch_up(manager) -> dict:
    """Scan message ids newer than the last one we processed."""
    summary = {"scanned": 0, "parts": 0, "events": 0}
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return summary
        last = await settings_store.get_int(db, "workspace.last_message_id", 0)
    top = await manager.probe_last_message_id()
    if top <= last + 1:
        return summary
    for start in range(last + 1, top, BATCH):
        ids = list(range(start, min(start + BATCH, top)))
        msgs = await manager.fetch_messages(ids, include_text=True)
        summary["scanned"] += len(ids)
        async with _db.async_session() as db:
            for m in msgs:
                cap = parse_caption(m["caption"]) if m.get("size") is not None else None
                ev = parse_event(m["caption"]) if cap is None else None
                if cap is not None:
                    await index_part(db, m["id"], cap, m["size"]); summary["parts"] += 1
                elif ev is not None:
                    await apply_event(db, ev); summary["events"] += 1
            await settings_store.set(db, "workspace.last_message_id", str(ids[-1]))
            await db.commit()
    if summary["parts"] or summary["events"]:
        logger.info("sync catch-up: %s", summary)
    return summary


async def reconcile(manager) -> dict:
    """Drop files whose channel messages are gone (deleted on another server
    while we were offline, or deleted by hand in Telegram)."""
    summary = {"checked": 0, "removed": 0}
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return summary
        rows = (await db.execute(select(FilePart.message_id, FilePart.file_id)
                                 .where(FilePart.message_id.is_not(None)))).all()
        by_msg = {mid: fid for mid, fid in rows}
        ids = sorted(by_msg)
        gone: set[str] = set()
        for i in range(0, len(ids), BATCH):
            batch = ids[i:i + BATCH]
            present = {m["id"] for m in await manager.fetch_messages(batch)}
            summary["checked"] += len(batch)
            for mid in batch:
                if mid not in present:
                    gone.add(by_msg[mid])
        for fid in gone:
            f = await db.get(File, fid, options=[selectinload(File.parts)])
            if f is None or f.status in (FileStatus.RECEIVING, FileStatus.QUEUED, FileStatus.UPLOADING, FileStatus.HASHING):
                continue  # our own in-flight upload: parts legitimately absent
            from app.transfers.staging import remove_part_files
            remove_part_files(f.parts)
            await db.delete(f)
            summary["removed"] += 1
            logger.info("sync reconcile: %s (%s) no longer in the channel; removed", fid, f.name)
        await db.commit()
    return summary


async def announce_delete(manager, file_id: str, by: str | None) -> None:
    """Tell other servers a file was deleted (best effort)."""
    try:
        await manager.send_text(make_event("delete", id=file_id, by=by))
    except Exception as e:  # noqa: BLE001
        logger.warning("sync: could not post delete event for %s: %s", file_id, e)


async def label(db, username: str) -> str:
    name = (await settings_store.get(db, "workspace.name")) or "server"
    return f"{name}/{username}"[:120]
