"""Server-side import from a mounted directory.

Three modes:
- file: the File's staging path *is* the source (keep_source=True); the worker
  reads it in place and never deletes it.
- tree: every file under a directory imported as above, folders recreated.
- zip:  a directory streamed into one store-only archive through the same
        part pipeline the browser uses, driven by a background task with the
        same three-part staging cap.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from app import config, crypto, db as _db
from app.models import File, FilePart, FileStatus, Folder
from app.transfers import staging, zipstream
from app.transfers.io import plan_parts

logger = logging.getLogger(__name__)

BLOCK = 4 * 1024 * 1024
COMMIT_EVERY = 16 * 1024 * 1024
active: dict[str, dict] = {}  # file id -> {"path": ..., "started_at": ...}
_pending: dict[str, tuple] = {}  # file id -> (layout, sources) until the caller commits and launches


def enabled() -> bool:
    return config.IMPORT_DIR.is_dir()


def resolve(rel: str) -> Path:
    """Map a user-supplied relative path onto the import dir, refusing anything
    that escapes it (including through symlinks)."""
    root = config.IMPORT_DIR
    if not enabled():
        raise HTTPException(404, f"Import directory {root} does not exist. Mount a directory there to enable imports.")
    target = (root / (rel or "")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "Path escapes the import directory")
    if not target.exists():
        raise HTTPException(404, "No such file or directory")
    return target


def browse(rel: str) -> dict:
    target = resolve(rel)
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        try:
            st = entry.stat()
        except OSError:
            continue
        entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": st.st_size if entry.is_file() else None,
                        "mtime": datetime.utcfromtimestamp(st.st_mtime).isoformat()})
    return {"root": str(config.IMPORT_DIR), "path": str(target.relative_to(config.IMPORT_DIR)) if target != config.IMPORT_DIR else "",
            "entries": entries}


def _walk(dir_path: Path) -> list[tuple[str, Path]]:
    """[(relative path inside dir, absolute path)] for every regular file, sorted."""
    out = []
    for root, dirs, files in os.walk(dir_path):
        dirs.sort()
        for name in sorted(files):
            full = Path(root) / name
            if full.is_file():
                out.append((str(full.relative_to(dir_path)).replace(os.sep, "/"), full))
    return out


async def _ensure_path(db, owner_id: int, parent_id: int | None, rel_dir: str) -> int | None:
    for seg in [s for s in rel_dir.split("/") if s and s not in (".", "..")]:
        existing = await db.scalar(select(Folder).where(
            Folder.owner_id == owner_id, Folder.parent_id == parent_id, Folder.name == seg[:255]))
        if existing is None:
            existing = Folder(owner_id=owner_id, parent_id=parent_id, name=seg[:255])
            db.add(existing)
            await db.flush()
        parent_id = existing.id
    return parent_id


async def _enc_fields(db) -> dict:
    if not await crypto.encrypt_new_files(db):
        return {"encrypted": False, "key_id": None}
    key = await crypto.ensure_key(db)
    return {"encrypted": True, "key_id": crypto.key_id_of(key)}


async def import_file(db, owner_id: int, folder_id: int | None, src: Path, part_size: int) -> File:
    size = src.stat().st_size
    import mimetypes
    f = File(owner_id=owner_id, folder_id=folder_id, name=src.name[:255], size=size,
             mime_type=mimetypes.guess_type(src.name)[0], part_size=part_size,
             status=FileStatus.QUEUED, staging_path=str(src), keep_source=True, **(await _enc_fields(db)))
    db.add(f)
    await db.flush()
    return f


async def import_tree(db, owner_id: int, folder_id: int | None, src_dir: Path, part_size: int) -> list[File]:
    root_id = await _ensure_path(db, owner_id, folder_id, src_dir.name)
    files = []
    for rel, full in _walk(src_dir):
        target = await _ensure_path(db, owner_id, root_id, os.path.dirname(rel))
        files.append(await import_file(db, owner_id, target, full, part_size))
    return files


async def start_zip(db, owner_id: int, folder_id: int | None, src_dir: Path, name: str, part_size: int) -> File:
    members = [(f"{src_dir.name}/{rel}", full) for rel, full in _walk(src_dir)]
    if not members:
        raise HTTPException(400, "Directory is empty")
    manifest = [{"path": rel, "size": full.stat().st_size} for rel, full in members]
    created = datetime.utcnow()
    layout = zipstream.plan(manifest, created)
    f = File(owner_id=owner_id, folder_id=folder_id, name=name, size=layout.total, mime_type="application/zip",
             is_archive=True, part_size=part_size, status=FileStatus.RECEIVING, created_at=created,
             **(await _enc_fields(db)))
    db.add(f)
    await db.flush()
    for index, offset, length in plan_parts(layout.total, part_size):
        db.add(FilePart(file_id=f.id, index=index, offset=offset, size=length))
    await db.flush()
    staging.begin(f)
    _pending[f.id] = (layout, [full for _rel, full in members], str(src_dir))
    return f


def launch(file_id: str) -> None:
    """Start the streaming task for a zip import. Call AFTER the session that
    created the File has committed, so the task can see the rows."""
    job = _pending.pop(file_id, None)
    if job is None:
        return
    layout, sources, src_dir = job
    active[file_id] = {"path": src_dir, "started_at": datetime.utcnow().isoformat()}
    asyncio.create_task(_run_zip(file_id, layout, sources))


async def _wait_for_room(db, file_id: str) -> None:
    while True:
        rows = (await db.execute(select(FilePart.received, FilePart.size, FilePart.message_id, FilePart.staging_path)
                                 .where(FilePart.file_id == file_id))).all()
        waiting = sum(1 for r in rows if r.staging_path is not None and r.received >= r.size and r.message_id is None)
        if waiting < staging.MAX_STAGED_PARTS:
            return
        await asyncio.sleep(1)


async def _run_zip(file_id: str, layout: zipstream.Layout, sources: list[Path]) -> None:
    from app.transfers import worker as transfer_worker
    try:
        async with _db.async_session() as db:
            f = await db.get(File, file_id)
            parts = sorted((await db.execute(select(FilePart).where(FilePart.file_id == file_id))).scalars().all(),
                           key=lambda p: p.index)
            pos = 0
            since_commit = 0
            crcs: list[int] = []

            async def emit(data: bytes) -> None:
                nonlocal pos, since_commit
                done = await asyncio.to_thread(staging.write_range, f, parts, pos, data)
                pos += len(data)
                since_commit += len(data)
                if done or since_commit >= COMMIT_EVERY:
                    await db.commit()
                    since_commit = 0
                    if done:
                        transfer_worker.kick()
                        await _wait_for_room(db, file_id)

            for entry, src in zip(layout.entries, sources):
                await emit(zipstream.local_header(layout, entry))
                crc = 0
                with open(src, "rb") as fh:
                    remaining = entry.size
                    while remaining > 0:
                        block = await asyncio.to_thread(fh.read, min(BLOCK, remaining))
                        if not block:
                            raise IOError(f"{src} shrank while importing")
                        crc = zipstream.crc_update(crc, block)
                        remaining -= len(block)
                        await emit(block)
                crcs.append(crc)
                await emit(zipstream.data_descriptor(entry, crc))
            await emit(zipstream.central_directory(layout, crcs))
            f.sha256 = staging.whole_digest(f.id)
            staging.forget(f.id)
            await db.refresh(f, attribute_names=["parts"])
            if all(p.message_id is not None for p in f.parts):
                f.status = FileStatus.READY
                f.ready_at = datetime.utcnow()
            else:
                f.status = FileStatus.QUEUED
            await db.commit()
            transfer_worker.kick()
            logger.info("Import zip %s streamed (%d bytes)", f.name, layout.total)
    except Exception as e:  # noqa: BLE001
        logger.exception("zip import failed for %s", file_id)
        async with _db.async_session() as db:
            f = await db.get(File, file_id)
            if f is not None:
                f.status = FileStatus.FAILED
                f.error = f"Import failed: {e}"[:1000]
                await db.commit()
        staging.forget(file_id)
    finally:
        active.pop(file_id, None)
