"""Folders, file index, delete (with channel takedown) and streamed download."""
import logging
import os
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import security
from app.db import get_db
from app.models import File, FileStatus, Folder, User
from app.routers.common import file_out, folder_out
from app.schemas import FolderCreate, Move, Rename
from app.telegram.manager import TelegramNotConfigured, manager
from app.transfers import worker as transfer_worker

router = APIRouter(prefix="/api", tags=["files"])
logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


async def _own_folder(db: AsyncSession, user: User, folder_id: int | None) -> Folder | None:
    if folder_id is None:
        return None
    folder = await db.get(Folder, folder_id)
    if folder is None or folder.owner_id != user.id:
        raise HTTPException(404, "Folder not found")
    return folder


async def _own_file(db: AsyncSession, user: User, file_id: str) -> File:
    f = await db.get(File, file_id, options=[selectinload(File.parts)])
    if f is None or f.owner_id != user.id:
        raise HTTPException(404, "File not found")
    return f


# ----------------------------------------------------------------- listing
@router.get("/files")
async def list_files(folder_id: int | None = None, q: str | None = None,
                     db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    fq = select(File).options(selectinload(File.parts)).where(File.owner_id == user.id)
    dq = select(Folder).where(Folder.owner_id == user.id)
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
    total, size = (await db.execute(
        select(func.count(File.id), func.coalesce(func.sum(File.size), 0))
        .where(File.owner_id == user.id, File.status == FileStatus.READY))).one()
    pending = await db.scalar(select(func.count(File.id)).where(
        File.owner_id == user.id, File.status != FileStatus.READY))
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
    await db.commit()
    return folder_out(folder)


@router.patch("/folders/{folder_id}")
async def rename_folder(folder_id: int, data: Rename, db: AsyncSession = Depends(get_db),
                        user: User = Depends(security.current_user)):
    folder = await _own_folder(db, user, folder_id)
    folder.name = FolderCreate(name=data.name).name
    await db.commit()
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
    files = await _collect_files(db, folder)
    for f in files:
        await _takedown(f)
    await db.delete(folder)  # cascades to subfolders and file rows
    await db.commit()
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
    await db.commit()
    return file_out(f)


@router.post("/files/{file_id}/move")
async def move_file(file_id: str, data: Move, db: AsyncSession = Depends(get_db),
                    user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    await _own_folder(db, user, data.folder_id)
    f.folder_id = data.folder_id
    await db.commit()
    return file_out(f)


@router.post("/files/{file_id}/retry")
async def retry_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.FAILED:
        raise HTTPException(400, "Only failed transfers can be retried")
    if not f.staging_path or not os.path.exists(f.staging_path):
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
    if f.staging_path:
        try:
            os.remove(f.staging_path)
        except FileNotFoundError:
            pass


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status in (FileStatus.HASHING, FileStatus.UPLOADING):
        raise HTTPException(409, "Wait for the transfer to finish or fail before deleting")
    await _takedown(f)
    await db.delete(f)
    await db.commit()
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


@router.get("/files/{file_id}/download")
async def download(file_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.READY:
        raise HTTPException(409, "File is not fully in the channel yet")
    if not manager.ready():
        raise HTTPException(503, "Telegram is not connected")
    parts = sorted(f.parts, key=lambda p: p.index)
    rng = _parse_range(request.headers.get("range"), f.size) if f.size else None
    start, end = rng if rng else (0, max(f.size - 1, 0))
    length = (end - start + 1) if f.size else 0

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
            doc = await manager.get_document(p.message_id)
            async for chunk in manager.iter_download(doc, from_off, take):
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(f.name)}",
        "Content-Length": str(length),
        "X-Content-Type-Options": "nosniff",
    }
    status = 200
    if rng:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{f.size}"
    return StreamingResponse(body(), status_code=status, headers=headers,
                             media_type=f.mime_type or "application/octet-stream")


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")
