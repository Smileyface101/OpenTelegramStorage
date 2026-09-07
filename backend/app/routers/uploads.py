"""Browser -> staging resumable uploads, and bundles (server-side zip)."""
import asyncio
import logging
from datetime import datetime
import mimetypes
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import config, security, settings_store
from app.db import get_db
from app.models import Bundle, File, FilePart, FileStatus, Folder, Upload, UploadStatus, User
from app.routers.common import file_out
from app.schemas import BundleCreate, UploadInit
from app.transfers import staging, worker as transfer_worker
from app.transfers.io import build_archive, plan_parts

router = APIRouter(prefix="/api", tags=["uploads"])
logger = logging.getLogger(__name__)


def _safe_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise HTTPException(400, "Invalid file name")
    return name[:255]


def _safe_rel_path(path: str | None, name: str) -> str | None:
    """Normalise a client-supplied relative path: forward slashes, no empty,
    '.' or '..' components, file name last. Returns None when it adds nothing."""
    if not path:
        return None
    parts = [p.strip() for p in path.replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..")]
    if not parts:
        return None
    parts[-1] = name
    rel = "/".join(parts)
    return rel if rel != name else None


def _check_space(size: int) -> None:
    free = shutil.disk_usage(config.STAGING_DIR).free
    if free < size + config.STAGING_FREE_SPACE_MARGIN:
        raise HTTPException(507, f"Not enough staging space: need {size} bytes, {free} free")


async def _own_file(db: AsyncSession, user: User, file_id: str) -> File:
    f = await db.get(File, file_id, options=[selectinload(File.parts)])
    if f is None or f.owner_id != user.id:
        raise HTTPException(404, "File not found")
    return f


async def _own_upload(db: AsyncSession, user: User, upload_id: str) -> Upload:
    up = await db.get(Upload, upload_id)
    if up is None or up.owner_id != user.id:
        raise HTTPException(404, "Upload not found")
    return up


async def _check_folder(db: AsyncSession, user: User, folder_id: int | None) -> None:
    if folder_id is None:
        return
    folder = await db.get(Folder, folder_id)
    if folder is None or folder.owner_id != user.id:
        raise HTTPException(404, "Folder not found")


def _upload_out(up: Upload) -> dict:
    return {"id": up.id, "name": up.name, "size": up.size, "received": up.received,
            "status": up.status.value, "chunk_size": config.UPLOAD_CHUNK_SIZE, "bundle_id": up.bundle_id,
            "file_id": up.file_id}


# ---------------------------------------------------------------- uploads
@router.post("/uploads")
async def init_upload(data: UploadInit, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    name = _safe_name(data.name)
    await _check_folder(db, user, data.folder_id)
    if data.bundle_id:
        bundle = await db.get(Bundle, data.bundle_id)
        if bundle is None or bundle.owner_id != user.id:
            raise HTTPException(404, "Bundle not found")
    config.ensure_dirs()
    up = Upload(owner_id=user.id, folder_id=data.folder_id, bundle_id=data.bundle_id, name=name,
                rel_path=_safe_rel_path(data.path, name) if data.bundle_id else None,
                size=data.size, mime_type=data.mime_type or mimetypes.guess_type(name)[0], path="")
    db.add(up)
    await db.flush()
    if data.bundle_id:
        # Bundle members are zipped server-side later: whole file in staging.
        _check_space(data.size)
        up.path = str(config.STAGING_DIR / f"{up.id}.part")
        open(up.path, "wb").close()
    else:
        # Streaming upload: the File and its parts exist from the first byte;
        # completed parts go to Telegram while the rest is still arriving, so
        # staging only ever holds a few parts.
        part_size = await settings_store.part_size_bytes(db)
        _check_space(min(data.size, (staging.MAX_STAGED_PARTS + 1) * part_size))
        f = File(owner_id=user.id, folder_id=data.folder_id, name=name, size=data.size, mime_type=up.mime_type,
                 part_size=part_size, status=FileStatus.RECEIVING)
        db.add(f)
        await db.flush()
        for index, offset, length in plan_parts(data.size, part_size):
            db.add(FilePart(file_id=f.id, index=index, offset=offset, size=length))
        up.file_id = f.id
        up.path = ""
        staging.begin(f)
    await db.commit()
    return _upload_out(up)


@router.get("/uploads/{upload_id}")
async def upload_status(upload_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    return _upload_out(await _own_upload(db, user, upload_id))


@router.put("/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, db: AsyncSession = Depends(get_db),
                       user: User = Depends(security.current_user)):
    """Append a chunk. Header X-Chunk-Offset must equal bytes received so far,
    which makes retries idempotent and lets the client resume after a reload."""
    up = await _own_upload(db, user, upload_id)
    if up.status != UploadStatus.ACTIVE:
        raise HTTPException(409, "Upload already completed")
    try:
        offset = int(request.headers.get("x-chunk-offset", "-1"))
    except ValueError:
        offset = -1
    if offset != up.received:
        raise HTTPException(409, {"code": "offset_mismatch", "received": up.received})
    if up.file_id is None:
        written = 0
        with open(up.path, "r+b") as fh:
            fh.seek(up.received)
            async for block in request.stream():
                if up.received + written + len(block) > up.size:
                    raise HTTPException(400, "Chunk exceeds declared file size")
                fh.write(block)
                written += len(block)
        up.received += written
        await db.commit()
        return _upload_out(up)

    f = await _own_file(db, user, up.file_id)
    parts = sorted(f.parts, key=lambda p: p.index)
    # Backpressure: don't let staging grow beyond a few parts waiting for Telegram.
    starts_new_part = f.part_size and offset % f.part_size == 0 and offset < f.size
    if starts_new_part and staging.staged_waiting(parts) >= staging.MAX_STAGED_PARTS:
        raise HTTPException(429, {"code": "backpressure", "retry_after": 2, "received": up.received},
                            headers={"Retry-After": "2"})
    pos = offset
    completed: list = []
    async for block in request.stream():
        if pos + len(block) > up.size:
            raise HTTPException(400, "Chunk exceeds declared file size")
        completed += await asyncio.to_thread(staging.write_range, f, parts, pos, block)
        pos += len(block)
    up.received = pos
    await db.commit()
    if completed:
        transfer_worker.kick()
    return _upload_out(up)


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    up = await _own_upload(db, user, upload_id)
    if up.status != UploadStatus.ACTIVE:
        raise HTTPException(409, "Upload already completed")
    if up.received != up.size:
        raise HTTPException(400, {"code": "incomplete", "received": up.received, "size": up.size})
    up.status = UploadStatus.COMPLETE
    if up.bundle_id:
        await db.commit()
        return {"upload": _upload_out(up), "file": None}
    f = await _own_file(db, user, up.file_id)
    f.sha256 = staging.whole_digest(f.id)
    staging.forget(f.id)
    if all(p.message_id is not None for p in f.parts):
        f.status = FileStatus.READY
        f.ready_at = datetime.utcnow()
    else:
        f.status = FileStatus.QUEUED
    await db.delete(up)
    await db.commit()
    transfer_worker.kick()
    return {"upload": None, "file": file_out(f)}


@router.delete("/uploads/{upload_id}")
async def cancel_upload(upload_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    up = await _own_upload(db, user, upload_id)
    if up.path:
        try:
            os.remove(up.path)
        except FileNotFoundError:
            pass
    if up.file_id:
        from app.routers.files import _takedown
        f = await _own_file(db, user, up.file_id)
        staging.forget(f.id)
        await _takedown(f)
        await db.delete(f)
    await db.delete(up)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- bundles
@router.post("/bundles")
async def create_bundle(data: BundleCreate, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    await _check_folder(db, user, data.folder_id)
    name = _safe_name(data.name)
    if not name.lower().endswith(".zip"):
        name += ".zip"
    compress = data.compress if data.compress is not None else await settings_store.get_bool(db, "transfer.compress_archives")
    b = Bundle(owner_id=user.id, folder_id=data.folder_id, name=name, compress=compress)
    db.add(b)
    await db.commit()
    return {"id": b.id, "name": b.name, "compress": b.compress}


@router.post("/bundles/{bundle_id}/complete")
async def complete_bundle(bundle_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    b = await db.get(Bundle, bundle_id)
    if b is None or b.owner_id != user.id:
        raise HTTPException(404, "Bundle not found")
    ups = (await db.execute(select(Upload).where(Upload.bundle_id == b.id))).scalars().all()
    if not ups:
        raise HTTPException(400, "Bundle has no files")
    if any(u.status != UploadStatus.COMPLETE for u in ups):
        raise HTTPException(400, "All files must finish uploading first")
    total = sum(u.size for u in ups)
    _check_space(total)
    out_path = str(config.STAGING_DIR / f"{b.id}.zip")
    members = [(u.rel_path or u.name, u.path) for u in ups]
    try:
        size = await asyncio.to_thread(build_archive, out_path, members, b.compress)
    except Exception as e:  # noqa: BLE001
        logger.exception("archive build failed")
        raise HTTPException(500, f"Could not build archive: {e}")
    for u in ups:
        try:
            os.remove(u.path)
        except FileNotFoundError:
            pass
        await db.delete(u)
    part_size = await settings_store.part_size_bytes(db)
    f = File(owner_id=user.id, folder_id=b.folder_id, name=b.name, size=size, mime_type="application/zip",
             is_archive=True, part_size=part_size, status=FileStatus.QUEUED, staging_path=out_path)
    db.add(f)
    await db.delete(b)
    await db.commit()
    await db.refresh(f, attribute_names=["parts"])
    transfer_worker.kick()
    return {"file": file_out(f)}


@router.delete("/bundles/{bundle_id}")
async def cancel_bundle(bundle_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    b = await db.get(Bundle, bundle_id)
    if b is None or b.owner_id != user.id:
        raise HTTPException(404, "Bundle not found")
    for u in (await db.execute(select(Upload).where(Upload.bundle_id == b.id))).scalars():
        try:
            os.remove(u.path)
        except FileNotFoundError:
            pass
    await db.delete(b)
    await db.commit()
    return {"ok": True}
