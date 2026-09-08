"""Folders, file index, delete (with channel takedown) and streamed download."""
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import crypto, security
from app.db import get_db
from app.models import File, FileStatus, Folder, User
from app.routers.common import file_out, folder_out
from app.schemas import BulkMove, FolderCreate, FolderEnsure, Move, Rename
from app.telegram.manager import TelegramNotConfigured, manager
from app.transfers import verify as integrity, worker as transfer_worker

router = APIRouter(prefix="/api", tags=["files"])
logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


async def _shared(db: AsyncSession) -> bool:
    from app import settings_store
    return await settings_store.shared_mode(db)


async def _own_folder(db: AsyncSession, user: User, folder_id: int | None) -> Folder | None:
    """In a shared workspace every user of this server sees every folder."""
    if folder_id is None:
        return None
    folder = await db.get(Folder, folder_id)
    if folder is None or (folder.owner_id != user.id and not await _shared(db)):
        raise HTTPException(404, "Folder not found")
    return folder


async def _own_file(db: AsyncSession, user: User, file_id: str) -> File:
    f = await db.get(File, file_id, options=[selectinload(File.parts)])
    if f is None or (f.owner_id != user.id and not await _shared(db)):
        raise HTTPException(404, "File not found")
    return f


def _bump() -> None:
    from app import sync
    sync.bump()


def _scope(query, model, user: User, shared: bool):
    return query if shared else query.where(model.owner_id == user.id)


async def _emit(db: AsyncSession, user: User, kind: str, **fields):
    """In a shared workspace, tell the other servers. Returns the event
    timestamp (stamp it on the row so our own echo is ignored) or None."""
    if not await _shared(db) or not manager.ready():
        return None
    from app import sync
    return await sync.announce(manager, kind, by=await sync.label(db, user.username), **fields)


async def _path(db: AsyncSession, folder_id: int | None) -> str | None:
    from app import sync
    return await sync.folder_path_of(db, folder_id)


# ----------------------------------------------------------------- listing
@router.get("/files/revision")
async def files_revision(user: User = Depends(security.current_user)):
    """Cheap change counter; the UI polls it and reloads the listing when it moves."""
    from app import sync
    return {"revision": sync.revision}


@router.get("/files")
async def list_files(folder_id: int | None = None, q: str | None = None,
                     db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    shared = await _shared(db)
    fq = _scope(select(File).options(selectinload(File.parts)), File, user, shared)
    dq = _scope(select(Folder), Folder, user, shared)
    if q:
        fq = fq.where(File.name.ilike(f"%{q}%"))
        dq = dq.where(Folder.name.ilike(f"%{q}%"))
    else:
        fq = fq.where(File.folder_id == folder_id)
        dq = dq.where(Folder.parent_id == folder_id)
    files = (await db.execute(fq.order_by(File.created_at.desc()))).scalars().all()
    folders = (await db.execute(dq.order_by(Folder.name))).scalars().all()
    crumbs = []
    cur = folder
    while cur is not None:
        crumbs.append(folder_out(cur))
        cur = await db.get(Folder, cur.parent_id) if cur.parent_id else None
    crumbs.reverse()
    return {"folder": folder_out(folder) if folder else None, "breadcrumbs": crumbs,
            "folders": [folder_out(d) for d in folders], "files": [file_out(f) for f in files]}


@router.get("/files/stats")
async def stats(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    shared = await _shared(db)
    total, size = (await db.execute(
        _scope(select(func.count(File.id), func.coalesce(func.sum(File.size), 0)), File, user, shared)
        .where(File.status == FileStatus.READY))).one()
    pending = await db.scalar(_scope(select(func.count(File.id)), File, user, shared).where(File.status != FileStatus.READY))
    return {"files": total or 0, "bytes": int(size or 0), "pending": pending or 0}


# ----------------------------------------------------------------- folders
@router.post("/folders")
async def create_folder(data: FolderCreate, db: AsyncSession = Depends(get_db),
                        user: User = Depends(security.current_user)):
    await _own_folder(db, user, data.parent_id)
    exists = await db.scalar(select(Folder).where(
        Folder.owner_id == user.id, Folder.parent_id == data.parent_id, Folder.name == data.name))
    if exists:
        raise HTTPException(409, "A folder with that name already exists here")
    folder = Folder(owner_id=user.id, parent_id=data.parent_id, name=data.name)
    db.add(folder)
    await db.flush()
    await _emit(db, user, "folder_create", path=await _path(db, folder.id))
    await db.commit()
    _bump()
    return folder_out(folder)


@router.get("/folders/tree")
async def folder_tree(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    """Every folder of the user as a flat list with depth, in tree order, for
    the move picker."""
    rows = (await db.execute(_scope(select(Folder), Folder, user, await _shared(db)).order_by(Folder.name))).scalars().all()
    children: dict[int | None, list[Folder]] = {}
    for f in rows:
        children.setdefault(f.parent_id, []).append(f)
    out: list[dict] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for f in children.get(parent_id, []):
            out.append({**folder_out(f), "depth": depth})
            walk(f.id, depth + 1)

    walk(None, 0)
    return out


async def _descendant_ids(db: AsyncSession, folder_id: int) -> set[int]:
    ids: set[int] = set()
    stack = [folder_id]
    while stack:
        fid = stack.pop()
        for d in (await db.execute(select(Folder.id).where(Folder.parent_id == fid))).scalars():
            if d not in ids:
                ids.add(d)
                stack.append(d)
    return ids


async def _move_folder(db: AsyncSession, user: User, folder: Folder, target_id: int | None) -> None:
    if target_id == folder.id or (target_id is not None and target_id in await _descendant_ids(db, folder.id)):
        raise HTTPException(400, f'Cannot move "{folder.name}" into itself')
    if folder.parent_id == target_id:
        return
    clash = await db.scalar(select(Folder).where(
        Folder.owner_id == user.id, Folder.parent_id == target_id, Folder.name == folder.name, Folder.id != folder.id))
    if clash:
        raise HTTPException(409, f'A folder named "{folder.name}" already exists in the destination')
    folder.parent_id = target_id


@router.post("/move")
async def bulk_move(data: BulkMove, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    """Move files and/or folders into target_folder_id (null = top level).
    Only the index changes; nothing moves in the channel."""
    await _own_folder(db, user, data.target_folder_id)
    target_path = await _path(db, data.target_folder_id)
    moved = 0
    for fid in dict.fromkeys(data.folder_ids):
        folder = await _own_folder(db, user, fid)
        old_path = await _path(db, folder.id)
        await _move_folder(db, user, folder, data.target_folder_id)
        folder.meta_updated_at = (await _emit(db, user, "folder_move", path=old_path, to=target_path)) or datetime.utcnow()
        moved += 1
    for file_id in dict.fromkeys(data.file_ids):
        f = await _own_file(db, user, file_id)
        f.folder_id = data.target_folder_id
        f.meta_updated_at = (await _emit(db, user, "move", id=f.id, path=target_path)) or datetime.utcnow()
        moved += 1
    await db.commit()
    _bump()
    return {"ok": True, "moved": moved}


@router.post("/folders/{folder_id}/move")
async def move_folder(folder_id: int, data: Move, db: AsyncSession = Depends(get_db),
                      user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    await _own_folder(db, user, data.folder_id)
    old_path = await _path(db, folder.id)
    await _move_folder(db, user, folder, data.folder_id)
    folder.meta_updated_at = (await _emit(db, user, "folder_move", path=old_path, to=await _path(db, data.folder_id))) or datetime.utcnow()
    await db.commit()
    _bump()
    return folder_out(folder)


@router.post("/folders/ensure")
async def ensure_folder_path(data: FolderEnsure, db: AsyncSession = Depends(get_db),
                             user: User = Depends(security.current_user)):
    """Walk/create a nested folder path under parent_id; returns the leaf folder.
    Idempotent, so the folder uploader can call it per file without races
    mattering (a duplicate create is caught and re-read)."""
    parent = await _own_folder(db, user, data.parent_id)
    parent_id = parent.id if parent else None
    segments = [s.strip() for s in data.path.replace("\\", "/").split("/")]
    segments = [s for s in segments if s and s not in (".", "..")]
    if not segments:
        raise HTTPException(400, "Empty path")
    folder = parent
    for seg in segments:
        seg = seg[:255]
        existing = await db.scalar(select(Folder).where(
            Folder.owner_id == user.id, Folder.parent_id == parent_id, Folder.name == seg))
        if existing is None:
            existing = Folder(owner_id=user.id, parent_id=parent_id, name=seg)
            db.add(existing)
            try:
                await db.flush()
            except Exception:  # noqa: BLE001 - lost a race: re-read
                await db.rollback()
                existing = await db.scalar(select(Folder).where(
                    Folder.owner_id == user.id, Folder.parent_id == parent_id, Folder.name == seg))
                if existing is None:
                    raise
        folder = existing
        parent_id = folder.id
    await db.commit()
    _bump()
    return folder_out(folder)


@router.patch("/folders/{folder_id}")
async def rename_folder(folder_id: int, data: Rename, db: AsyncSession = Depends(get_db),
                        user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    old_path = await _path(db, folder.id)
    folder.name = FolderCreate(name=data.name).name
    folder.meta_updated_at = (await _emit(db, user, "folder_rename", path=old_path, name=folder.name)) or datetime.utcnow()
    await db.commit()
    _bump()
    return folder_out(folder)


async def _collect_files(db: AsyncSession, folder: Folder) -> list[File]:
    out: list[File] = []
    stack = [folder.id]
    while stack:
        fid = stack.pop()
        out.extend((await db.execute(select(File).options(selectinload(File.parts))
                                     .where(File.folder_id == fid))).scalars().all())
        stack.extend([d.id for d in (await db.execute(select(Folder).where(Folder.parent_id == fid))).scalars()])
    return out


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: int, db: AsyncSession = Depends(get_db),
                        user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    folder_path_before = await _path(db, folder.id)
    files = await _collect_files(db, folder)
    for f in files:
        await _takedown(f)
    ids = [f.id for f in files]
    shared = await _shared(db)
    await db.delete(folder)  # cascades to subfolders and file rows
    await db.commit()
    _bump()
    if shared and manager.ready():
        from app import sync
        by = await sync.label(db, user.username)
        for fid in ids:
            await sync.announce_delete(manager, fid, by)
        await sync.announce(manager, "folder_delete", path=folder_path_before, by=by)
    return {"ok": True, "files_deleted": len(files)}


# ------------------------------------------------------------------- files
@router.get("/files/{file_id}")
async def get_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    return file_out(await _own_file(db, user, file_id))


@router.patch("/files/{file_id}")
async def rename_file(file_id: str, data: Rename, db: AsyncSession = Depends(get_db),
                      user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    name = data.name.strip()
    if not name or "/" in name or "\\" in name:
        raise HTTPException(400, "Invalid file name")
    f.name = name
    f.meta_updated_at = (await _emit(db, user, "rename", id=f.id, name=name)) or datetime.utcnow()
    await db.commit()
    _bump()
    return file_out(f)


@router.post("/files/{file_id}/move")
async def move_file(file_id: str, data: Move, db: AsyncSession = Depends(get_db),
                    user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    await _own_folder(db, user, data.folder_id)
    f.folder_id = data.folder_id
    f.meta_updated_at = (await _emit(db, user, "move", id=f.id, path=await _path(db, data.folder_id))) or datetime.utcnow()
    await db.commit()
    _bump()
    return file_out(f)


@router.post("/files/{file_id}/retry")
async def retry_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.FAILED:
        raise HTTPException(400, "Only failed transfers can be retried")
    pending = [p for p in f.parts if p.message_id is None]
    have_whole = bool(f.staging_path and os.path.exists(f.staging_path))
    have_parts = pending and all(p.staging_path and os.path.exists(p.staging_path) for p in pending)
    if not (have_whole or have_parts):
        raise HTTPException(410, "The staged copy is gone; upload the file again")
    f.status = FileStatus.QUEUED
    f.retries = 0
    f.error = None
    await db.commit()
    transfer_worker.kick()
    return file_out(f)


async def _takedown(f: File) -> None:
    """Remove the channel messages (real deletion, not just the index row) and
    any staged copy. Channel errors are logged, not fatal: the row goes away
    so the user is never stuck, and the message ids are logged for cleanup."""
    ids = [p.message_id for p in f.parts if p.message_id is not None]
    if ids:
        try:
            await manager.delete_messages(ids)
        except TelegramNotConfigured:
            logger.warning("Telegram offline; could not delete messages %s for file %s", ids, f.id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed deleting channel messages %s for file %s", ids, f.id)
    if f.staging_path and not f.keep_source:
        try:
            os.remove(f.staging_path)
        except FileNotFoundError:
            pass
    from app.transfers.staging import remove_part_files
    remove_part_files(f.parts)


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status in (FileStatus.HASHING, FileStatus.UPLOADING):
        raise HTTPException(409, "Wait for the transfer to finish or fail before deleting")
    await _takedown(f)
    shared = await _shared(db)
    await db.delete(f)
    await db.commit()
    _bump()
    if shared and manager.ready():
        from app import sync
        await sync.announce_delete(manager, file_id, await sync.label(db, user.username))
    return {"ok": True}


# ---------------------------------------------------------------- download
def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        raise HTTPException(416, "Invalid Range")
    start_s, end_s = m.groups()
    if start_s == "" and end_s == "":
        raise HTTPException(416, "Invalid Range")
    if start_s == "":
        length = min(int(end_s), size)
        return size - length, size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    if start >= size or start > end:
        raise HTTPException(416, "Range not satisfiable")
    return start, min(end, size - 1)


@router.post("/files/{file_id}/verify")
async def verify_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    """Re-read every part from the channel and compare checksums (background)."""
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.READY:
        raise HTTPException(409, "Only files that are fully in the channel can be verified")
    if not manager.ready():
        raise HTTPException(503, "Telegram is not connected")
    if f.id not in integrity.verifying:
        asyncio.create_task(integrity.verify_file(manager, f.id))
        await asyncio.sleep(0)
    return {"ok": True, "verifying": True}


def stream_file(f: File, request: Request, key: bytes | None = None) -> StreamingResponse:
    """Build the (Range-aware, integrity-checked) streaming response for a
    READY file. Shared by the authenticated download and public share links.
    Encrypted files are decrypted block by block on the way out."""
    if f.encrypted and (key is None or crypto.key_id_of(key) != f.key_id):
        raise HTTPException(503, "This file is encrypted and its content key is not available on this server")
    parts = sorted(f.parts, key=lambda p: p.index)
    rng = _parse_range(request.headers.get("range"), f.size) if f.size else None
    start, end = rng if rng else (0, max(f.size - 1, 0))
    length = (end - start + 1) if f.size else 0
    file_id, file_name = f.id, f.name

    async def body():
        remaining = length
        for p in parts:
            if remaining <= 0:
                break
            p_start, p_end = p.offset, p.offset + p.size - 1
            if p.size == 0 or p_end < start or p_start > end:
                continue
            from_off = max(start, p_start) - p_start
            take = min(end, p_end) - max(start, p_start) + 1
            full = from_off == 0 and take == p.size and p.sha256
            h = hashlib.sha256() if full else None
            doc = await manager.get_document(p.message_id)
            source = crypto.decrypt_range(manager, doc, p, key, from_off, take) if f.encrypted else manager.iter_download(doc, from_off, take)
            try:
                async for chunk in source:
                    remaining -= len(chunk)
                    if h is not None:
                        h.update(chunk)
                    yield chunk
            except InvalidTag:
                # Authentication tag failed: ciphertext was altered in the channel.
                await integrity.record_mismatch(file_id, f"part {p.index + 1}: decryption failed (tampered or corrupt)")
                raise RuntimeError(f"Integrity failure: part {p.index + 1} of {file_name} could not be decrypted")
            if h is not None and h.hexdigest() != p.sha256:
                await integrity.record_mismatch(file_id, f"part {p.index + 1}: checksum mismatch on download")
                raise RuntimeError(f"Integrity failure: part {p.index + 1} of {file_name} does not match its checksum")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(f.name)}",
        "Content-Length": str(length),
        "X-Content-Type-Options": "nosniff",
    }
    if f.sha256:
        headers["X-Checksum-SHA256"] = f.sha256
    status = 200
    if rng:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{f.size}"
    return StreamingResponse(body(), status_code=status, headers=headers,
                             media_type=f.mime_type or "application/octet-stream")


@router.get("/files/{file_id}/download")
async def download(file_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.READY:
        raise HTTPException(409, "File is not fully in the channel yet")
    if not manager.ready():
        raise HTTPException(503, "Telegram is not connected")
    return stream_file(f, request, await crypto.get_key(db) if f.encrypted else None)


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")
