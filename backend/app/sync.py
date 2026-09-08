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
EVENT_MAX_CHARS = 3500          # Telegram messages cap at 4096 characters
HELLO_MIN_INTERVAL = 600.0      # seconds between layout publications triggered by "hello"
_last_hello_reply = 0.0


_hello_pending: list = []   # set by apply_event("hello"); drained by the worker loop

# Anything that changes the index bumps this; the UI polls it to refresh.
revision = 0
status = {"last_live_at": None, "last_live_kind": None, "last_catchup_at": None, "last_catchup": None,
          "last_reconcile_at": None, "events_applied": 0, "parts_indexed": 0, "last_error": None, "server_id": None,
          "top_seen": 0,
          # What sync is doing right now, for the UI: idle | catching_up | handshake
          "phase": "idle", "progress": None}


def note_message_id(message_id: int) -> None:
    """Highest channel message id we know of (live posts, our own sends)."""
    if message_id and message_id > status["top_seen"]:
        status["top_seen"] = message_id


def bump() -> None:
    global revision
    revision += 1


async def server_id(db) -> str:
    """Stable random id for this server; carried in events so a server can
    recognise its own echoes regardless of how it is named."""
    sid = await settings_store.get(db, "workspace.server_id")
    if not sid:
        import secrets
        sid = secrets.token_hex(4)
        await settings_store.set(db, "workspace.server_id", sid)
        await db.commit()
    status["server_id"] = sid
    return sid


def take_hello_request() -> bool:
    if _hello_pending:
        _hello_pending.clear()
        return True
    return False


def make_event(kind: str, **fields) -> str:
    ev = {EVENT_KEY: 1, "t": kind, "ts": datetime.utcnow().isoformat(), **fields}
    if status.get("server_id") and "sid" not in ev:
        ev["sid"] = status["server_id"]
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


def _ts(ev: dict) -> datetime:
    try:
        return datetime.fromisoformat(ev["ts"])
    except (KeyError, ValueError):
        return datetime.utcnow()


def _newer(ev_ts: datetime, current: datetime | None) -> bool:
    """Last-writer-wins: apply only if the event is newer than what we have.
    Equal timestamps are our own echo and are skipped."""
    return current is None or ev_ts > current


async def folder_path_of(db, folder_id: int | None) -> str | None:
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


async def resolve_path(db, path: str | None) -> Folder | None:
    """Folder for "A/B/C" in the (shared) tree, or None for the root / unknown."""
    if not path:
        return None
    parent_id = None
    folder = None
    for seg in [s for s in path.split("/") if s]:
        folder = await db.scalar(select(Folder).where(Folder.parent_id == parent_id, Folder.name == seg).order_by(Folder.id))
        if folder is None:
            return None
        parent_id = folder.id
    return folder


async def is_own(db, ev: dict) -> bool:
    """Our own events come back to us through the channel. Matched by the
    random server id when present (0.1.2+), else by server name."""
    sid = status.get("server_id") or await server_id(db)
    if ev.get("sid"):
        return ev["sid"] == sid
    name = (await settings_store.get(db, "workspace.name")) or ""
    by = str(ev.get("by") or "")
    return bool(name) and by.startswith(name + "/")


async def apply_event(db, ev: dict) -> bool:
    if await is_own(db, ev):
        return False
    kind = ev.get("t")
    ts = _ts(ev)
    if kind == "delete":
        f = await db.get(File, str(ev.get("id")), options=[selectinload(File.parts)])
        if f is None:
            return False
        from app.transfers.staging import remove_part_files
        remove_part_files(f.parts)
        await db.delete(f)
        logger.info("sync: file %s deleted by %s", ev.get("id"), ev.get("by"))
        return True
    if kind in ("rename", "move"):
        f = await db.get(File, str(ev.get("id")))
        if f is None or not _newer(ts, f.meta_updated_at):
            return False
        if kind == "rename":
            name = str(ev.get("name") or "").strip()
            if not name:
                return False
            f.name = name[:255]
        else:
            owner = await _owner_id(db)
            f.folder_id = await _ensure_path(db, owner, ev.get("path")) if ev.get("path") else None
        f.meta_updated_at = ts
        return True
    if kind == "folder_create":
        owner = await _owner_id(db)
        if owner is None:
            return False
        await _ensure_path(db, owner, ev.get("path"))
        return True
    if kind == "tree":
        # Full folder list from another server (published layout). It also
        # tells us how far the channel goes, so our next catch-up reads all of it.
        if isinstance(ev.get("top"), int):
            note_message_id(ev["top"])
        owner = await _owner_id(db)
        if owner is None:
            return False
        for path in ev.get("folders") or []:
            await _ensure_path(db, owner, str(path))
        return True
    if kind == "place":
        # {id: [path, name]} for a batch of files: current location and name.
        owner = await _owner_id(db)
        changed = False
        for fid, spec in (ev.get("files") or {}).items():
            f = await db.get(File, str(fid))
            if f is None or not isinstance(spec, list) or len(spec) != 2 or not _newer(ts, f.meta_updated_at):
                continue
            path, name = spec
            f.folder_id = await _ensure_path(db, owner, path) if path else None
            if name:
                f.name = str(name)[:255]
            f.meta_updated_at = ts
            changed = True
        return changed
    if kind == "hello":
        # A server joined or rebuilt: publish our layout so it can catch up.
        global _last_hello_reply
        import time as _time
        if _time.monotonic() - _last_hello_reply > HELLO_MIN_INTERVAL:
            _last_hello_reply = _time.monotonic()
            _hello_pending.append(True)
        return False
    if kind in ("folder_rename", "folder_move", "folder_delete"):
        folder = await resolve_path(db, ev.get("path"))
        if folder is None:
            return False
        if kind == "folder_delete":
            has_files = await db.scalar(select(File.id).where(File.folder_id == folder.id).limit(1))
            if has_files:
                return False  # deletes of its files arrive as their own events; keep until empty
            await db.delete(folder)
            return True
        if not _newer(ts, folder.meta_updated_at):
            return False
        if kind == "folder_rename":
            name = str(ev.get("name") or "").strip()
            if not name or "/" in name:
                return False
            folder.name = name[:255]
        else:
            target = await resolve_path(db, ev.get("to")) if ev.get("to") else None
            if ev.get("to") and target is None:
                owner = await _owner_id(db)
                tid = await _ensure_path(db, owner, ev.get("to"))
                target = await db.get(Folder, tid)
            if target is not None and (target.id == folder.id):
                return False
            folder.parent_id = target.id if target else None
        folder.meta_updated_at = ts
        return True
    return False


async def handle_post(message_id: int, text: str, doc_size: int | None) -> None:
    """Live channel post (called from the Telegram manager)."""
    note_message_id(message_id)
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return
        cap = parse_caption(text) if doc_size is not None else None
        ev = parse_event(text) if cap is None else None
        try:
            changed = False
            if cap is not None:
                changed = await index_part(db, message_id, cap, doc_size)
                status["parts_indexed"] += 1 if changed else 0
                status["last_live_kind"] = "part"
            elif ev is not None:
                changed = await apply_event(db, ev)
                status["events_applied"] += 1 if changed else 0
                status["last_live_kind"] = ev.get("t")
            status["last_live_at"] = datetime.utcnow().isoformat()
            # Note: live posts do NOT advance workspace.last_message_id. That
            # marker means "scanned contiguously up to here" and only catch-up
            # moves it, so a message missed live is never skipped.
            await db.commit()
            if changed:
                bump()
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            status["last_error"] = f"live message {message_id}: {e}"[:300]
            logger.exception("sync: failed to apply message %s", message_id)


async def catch_up(manager) -> dict:
    """Scan message ids newer than the last one we processed. No marker
    messages: we read forward in batches and stop at the first batch past the
    highest id we know of that contains nothing (ids are assigned in order,
    so an empty stretch beyond the top means the channel ends there)."""
    summary = {"scanned": 0, "parts": 0, "events": 0}
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return summary
        last = await settings_store.get_int(db, "workspace.last_message_id", 0)
    start = last + 1
    highest_found = last
    status["phase"] = "catching_up"
    status["progress"] = summary
    # How many empty batches past the highest known id end the scan. When we
    # know the top from live traffic or a peer, one is enough. A fresh server
    # knows nothing yet, and a channel that lost its early messages (deletes,
    # auto-delete) starts with a long empty stretch, so look much further.
    empty_limit = 1 if status["top_seen"] > 0 else 20
    empty_run = 0
    while True:
        ids = list(range(start, start + BATCH))
        msgs = await manager.fetch_messages(ids, include_text=True)
        summary["scanned"] += len(ids)
        if msgs:
            empty_run = 0
            async with _db.async_session() as db:
                for m in msgs:
                    highest_found = max(highest_found, m["id"])
                    cap = parse_caption(m["caption"]) if m.get("size") is not None else None
                    ev = parse_event(m["caption"]) if cap is None else None
                    try:
                        if cap is not None:
                            if await index_part(db, m["id"], cap, m["size"]):
                                summary["parts"] += 1
                        elif ev is not None:
                            if await apply_event(db, ev):
                                summary["events"] += 1
                    except Exception as e:  # noqa: BLE001
                        status["last_error"] = f"catch-up message {m['id']}: {e}"[:300]
                        logger.exception("sync: catch-up failed on message %s", m["id"])
                await settings_store.set(db, "workspace.last_message_id", str(highest_found))
                await db.commit()
            note_message_id(highest_found)
            if summary["parts"] or summary["events"]:
                bump()
        elif start > status["top_seen"]:
            empty_run += 1
            if empty_run >= empty_limit:
                break  # nothing here and nothing known beyond: end of channel
        start += BATCH
        if summary["scanned"] > 50000:
            break  # safety valve
    status["last_catchup_at"] = datetime.utcnow().isoformat()
    status["last_catchup"] = summary
    status["phase"] = "idle"
    status["progress"] = None
    if summary["parts"] or summary["events"]:
        logger.info("sync catch-up: %s", summary)
        bump()
    return summary


_missing_seen: dict[str, datetime] = {}   # file id -> first time its messages were not found
RECONCILE_CONFIRM_AFTER = 3600.0          # seconds a file must stay missing before removal
RECONCILE_MAX_MISSING_FRACTION = 0.5      # more than this in one pass = fetch problem, not deletions


async def reconcile(manager) -> dict:
    """Drop files whose channel messages are gone (deleted on another server
    while we were offline, or deleted by hand in Telegram).

    Guarded so a bad read can never empty the index: a file is removed only
    if it is missing on two passes at least an hour apart, and a pass in which
    more than half of everything is missing is treated as a fetch failure."""
    summary = {"checked": 0, "missing": 0, "removed": 0, "skipped": None}
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
        status["last_reconcile_at"] = datetime.utcnow().isoformat()
        all_files = {fid for fid in by_msg.values()}
        summary["missing"] = len(gone)
        if ids and len(gone) > RECONCILE_MAX_MISSING_FRACTION * len(all_files) and len(gone) > 1:
            summary["skipped"] = "too many missing at once; treating as a fetch problem"
            status["last_error"] = f"reconcile: {len(gone)} of {len(all_files)} files not found; not removing anything"
            logger.warning("sync reconcile: %s", status["last_error"])
            return summary
        now = datetime.utcnow()
        for fid in list(_missing_seen):
            if fid not in gone:
                _missing_seen.pop(fid, None)   # it is back (or was already removed)
        for fid in gone:
            f = await db.get(File, fid, options=[selectinload(File.parts)])
            if f is None or f.status in (FileStatus.RECEIVING, FileStatus.QUEUED, FileStatus.UPLOADING, FileStatus.HASHING, FileStatus.SYNCING):
                continue  # in flight: parts legitimately absent
            first = _missing_seen.setdefault(fid, now)
            if (now - first).total_seconds() < RECONCILE_CONFIRM_AFTER:
                continue  # wait for a second confirmation
            from app.transfers.staging import remove_part_files
            remove_part_files(f.parts)
            await db.delete(f)
            _missing_seen.pop(fid, None)
            summary["removed"] += 1
            logger.info("sync reconcile: %s (%s) missing from the channel for over an hour; removed", fid, f.name)
        await db.commit()
        if summary["removed"]:
            bump()
    return summary


async def announce(manager, kind: str, **fields) -> datetime | None:
    """Post an event for the other servers (best effort). Returns the event
    timestamp so the caller can stamp its local row (echo is then skipped)."""
    text = make_event(kind, **fields)
    try:
        await manager.send_text(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("sync: could not post %s event: %s", kind, e)
    return datetime.fromisoformat(json.loads(text)["ts"])


async def announce_delete(manager, file_id: str, by: str | None) -> None:
    await announce(manager, "delete", id=file_id, by=by)


async def label(db, username: str) -> str:
    await server_id(db)  # make sure every event we emit carries our id
    name = (await settings_store.get(db, "workspace.name")) or "server"
    return f"{name}/{username}"[:120]


async def layout_events(db, by: str | None) -> list[str]:
    """Compact description of this server's current tree: every folder path,
    then every file's (path, name), split into messages under Telegram's size
    limit. Lets a joining server reproduce empty folders, moves and renames
    that captions cannot express."""
    folders = (await db.execute(select(Folder))).scalars().all()
    by_id = {f.id: f for f in folders}
    paths = []
    for f in folders:
        names, cur, seen = [], f, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id); names.append(cur.name); cur = by_id.get(cur.parent_id)
        paths.append("/".join(reversed(names)))
    paths.sort(key=lambda p: (p.count("/"), p))
    events: list[str] = []
    batch: list[str] = []
    top = max(status["top_seen"], await settings_store.get_int(db, "workspace.last_message_id", 0))
    for path in paths:
        batch.append(path)
        if len(json.dumps(batch)) > EVENT_MAX_CHARS - 200:
            events.append(make_event("tree", folders=batch[:-1], by=by, top=top)); batch = [path]
    events.append(make_event("tree", folders=batch, by=by, top=top))  # always at least one, even if empty
    files = (await db.execute(select(File.id, File.name, File.folder_id).where(File.status == FileStatus.READY))).all()
    folder_paths: dict = {}
    for f in folders:
        names, cur, seen = [], f, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id); names.append(cur.name); cur = by_id.get(cur.parent_id)
        folder_paths[f.id] = "/".join(reversed(names))
    chunk: dict = {}
    for fid, name, folder_id in files:
        chunk[fid] = [folder_paths.get(folder_id) if folder_id else None, name]
        if len(json.dumps(chunk)) > EVENT_MAX_CHARS - 200:
            last = chunk.popitem()
            events.append(make_event("place", files=chunk, by=by)); chunk = dict([last])
    if chunk:
        events.append(make_event("place", files=chunk, by=by))
    return events


async def publish_layout(manager, by: str | None) -> int:
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return 0
        events = await layout_events(db, by)
    for text in events:
        await manager.send_text(text)
    logger.info("sync: published layout in %d message(s)", len(events))
    return len(events)


async def say_hello(manager, by: str | None) -> None:
    """Ask the other servers for their layout (after joining or rebuilding)."""
    try:
        await manager.send_text(make_event("hello", by=by))
    except Exception as e:  # noqa: BLE001
        logger.warning("sync: hello failed: %s", e)


async def startup_handshake(manager) -> None:
    """Shared server coming online: catch up on what we missed, then ask the
    others for their layout and offer ours. Fully automatic; no buttons."""
    async with _db.async_session() as db:
        if not await settings_store.shared_mode(db):
            return
        await server_id(db)
        name = (await settings_store.get(db, "workspace.name")) or "server"
    try:
        await catch_up(manager)
        status["phase"] = "handshake"
        await say_hello(manager, f"{name}/system")
        await publish_layout(manager, f"{name}/system")
    except Exception as e:  # noqa: BLE001
        status["last_error"] = f"startup handshake: {e}"[:300]
        logger.warning("sync: startup handshake failed: %s", e)
    finally:
        status["phase"] = "idle"
        status["progress"] = None
