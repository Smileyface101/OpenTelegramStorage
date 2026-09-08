"""Rebuild the file index from the channel.

Every part the app posts carries a JSON caption: {"ots":1,"id":...,"name":...,
"size":...,"part":n,"of":m,"psize":...,"sha256":...,"archive":bool,"path":"a/b"}.
Scanning the channel by message id and grouping captions by file id is enough
to recreate `files` and `file_parts` on a fresh install or after losing the
database. Files already indexed are left alone; files with missing parts are
imported as FAILED with an explanatory error so the user can see them."""
import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import db as _db
from app.models import File, FilePart, FileStatus, Folder

logger = logging.getLogger(__name__)

BATCH = 100
CAPTION_KEYS = ("ots", "otg")  # current + legacy marker


@dataclass
class RebuildState:
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    last_message_id: int = 0
    scanned: int = 0
    parts_found: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    files_incomplete: int = 0
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.log.append(msg)
        if len(self.log) > 50:
            self.log.pop(0)


state = RebuildState()
_lock = asyncio.Lock()


def parse_caption(text: str) -> dict | None:
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not any(data.get(k) == 1 for k in CAPTION_KEYS):
        return None
    required = ("id", "name", "size", "part", "of")
    if any(k not in data for k in required):
        return None
    return data


async def _ensure_path(db, owner_id: int, path: str | None) -> int | None:
    if not path:
        return None
    parent_id = None
    for seg in [p for p in path.split("/") if p and p not in (".", "..")]:
        seg = seg[:255]
        existing = await db.scalar(select(Folder).where(
            Folder.owner_id == owner_id, Folder.parent_id == parent_id, Folder.name == seg))
        if existing is None:
            existing = Folder(owner_id=owner_id, parent_id=parent_id, name=seg)
            db.add(existing)
            await db.flush()
        parent_id = existing.id
    return parent_id


async def rebuild(manager, owner_id: int, part_size_default: int) -> RebuildState:
    """Scan the whole channel and import anything not yet indexed."""
    global state
    if _lock.locked():
        return state
    async with _lock:
        state = RebuildState(running=True, started_at=datetime.utcnow().isoformat())
        try:
            await _rebuild(manager, owner_id, part_size_default)
        except Exception as e:  # noqa: BLE001
            logger.exception("index rebuild failed")
            state.error = str(e)
        finally:
            state.running = False
            state.finished_at = datetime.utcnow().isoformat()
        return state


async def _rebuild(manager, owner_id: int, part_size_default: int) -> None:
    last = await manager.probe_last_message_id()
    state.last_message_id = last
    state.note(f"Channel has message ids up to {last}")

    # 1) Collect every part caption in the channel.
    found: dict[str, dict] = {}  # file id -> {"meta": caption, "parts": {index: {...}}}
    for start in range(1, last + 1, BATCH):
        ids = list(range(start, min(start + BATCH, last + 1)))
        msgs = await manager.fetch_messages(ids)
        state.scanned += len(ids)
        for m in msgs:
            cap = parse_caption(m["caption"])
            if cap is None:
                continue
            entry = found.setdefault(str(cap["id"]), {"meta": cap, "parts": {}})
            enc = cap.get("enc") if isinstance(cap.get("enc"), dict) else None
            entry["parts"][int(cap["part"]) - 1] = {
                "message_id": m["id"], "size": int(cap.get("psize", m["size"])), "sha256": cap.get("sha256"),
                "enc_salt": enc.get("salt") if enc else None, "enc_size": enc.get("ct") if enc else None,
                "kid": enc.get("kid") if enc else None,
            }
            state.parts_found += 1
        await asyncio.sleep(0)  # let the API answer status polls
    state.note(f"Found {state.parts_found} part(s) belonging to {len(found)} file(s)")

    # 2) Import what the index does not have.
    async with _db.async_session() as db:
        existing_ids = set((await db.execute(select(File.id))).scalars())
        for fid, entry in found.items():
            meta, parts = entry["meta"], entry["parts"]
            if fid in existing_ids:
                state.files_skipped += 1
                continue
            total = int(meta["of"])
            complete = all(i in parts for i in range(total))
            folder_id = await _ensure_path(db, owner_id, meta.get("path"))
            sizes = [parts[i]["size"] for i in range(total) if i in parts]
            part_size = max(sizes) if sizes else part_size_default
            kids = {p.get("kid") for p in parts.values() if p.get("kid")}
            f = File(
                id=fid, owner_id=owner_id, folder_id=folder_id, name=str(meta["name"])[:255],
                size=int(meta["size"]), is_archive=bool(meta.get("archive")), part_size=part_size,
                encrypted=bool(kids), key_id=(next(iter(kids)) if kids else None),
                status=FileStatus.READY if complete else FileStatus.FAILED,
                error=None if complete else f"Recovered from channel but {total - len(parts)} of {total} part(s) are missing",
                ready_at=datetime.utcnow() if complete else None,
            )
            db.add(f)
            offset = 0
            for i in range(total):
                p = parts.get(i)
                size = p["size"] if p else 0
                db.add(FilePart(file_id=fid, index=i, offset=offset, size=size,
                                sha256=p["sha256"] if p else None,
                                message_id=p["message_id"] if p else None,
                                enc_salt=p["enc_salt"] if p else None, enc_size=p["enc_size"] if p else None,
                                uploaded_at=datetime.utcnow() if p else None))
                offset += size
            if complete:
                state.files_imported += 1
            else:
                state.files_incomplete += 1
            state.note(f"{'Imported' if complete else 'Incomplete'}: {meta['name']} ({len(parts)}/{total} parts)")
        await db.commit()


def snapshot() -> dict:
    return asdict(state)
