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

from app import config, crypto, security, settings_store
from app.db import get_db
from app.models import Bundle, File, FilePart, FileStatus, Folder, Upload, UploadStatus, User
from app.routers.common import file_out
import json

from app.schemas import BundleCreate, UploadComplete, UploadInit
from app.transfers import staging, worker as transfer_worker, zipstream
from app.transfers.io import plan_parts

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


async def _enc_fields(db: AsyncSession) -> dict:
    """encrypted/key_id for a new File, per the 'encrypt new uploads' setting."""
    if not await crypto.encrypt_new_files(db):
        return {"encrypted": False, "key_id": None}
    key = await crypto.ensure_key(db)
    return {"encrypted": True, "key_id": crypto.key_id_of(key)}


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


_locks: dict[str, asyncio.Lock] = {}


def _lock(upload_id: str) -> asyncio.Lock:
    return _locks.setdefault(upload_id, asyncio.Lock())


def _total_chunks(size: int) -> int:
    return max(1, (size + config.UPLOAD_CHUNK_SIZE - 1) // config.UPLOAD_CHUNK_SIZE) if size else 0


def _upload_out(up: Upload, part_size: int | None = None) -> dict:
    out = {"id": up.id, "name": up.name, "size": up.size, "received": up.received,
           "status": up.status.value, "chunk_size": config.UPLOAD_CHUNK_SIZE, "bundle_id": up.bundle_id,
           "file_id": up.file_id, "folder_id": up.folder_id, "part_size": part_size or getattr(up, "_part_size", None),
           "created_at": up.created_at}
    if up.file_id and up.bundle_id is None:
        cm = staging.ChunkMap(up.chunk_map, _total_chunks(up.size))
        out["total_chunks"] = cm.total
        out["missing_chunks"] = cm.missing()
        out["parallel"] = True
    return out


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
        bundle = await db.get(Bundle, data.bundle_id)
        layout = _layout(bundle)
        idx = data.member_index
        if idx is None or idx >= len(layout.entries):
            raise HTTPException(400, "member_index is required for bundle uploads")
        entry = layout.entries[idx]
        rel = _safe_rel_path(data.path, name) or name
        if entry.size != data.size or entry.path.rsplit(" (", 1)[0] not in (rel, rel.rsplit(".", 1)[0]) and entry.path != rel:
            raise HTTPException(409, {"code": "manifest_mismatch", "expected": {"path": entry.path, "size": entry.size}})
        if idx != bundle.completed_members:
            # Members stream into one archive, so they must arrive in order.
            existing = await db.scalar(select(Upload).where(Upload.bundle_id == bundle.id, Upload.member_index == idx))
            if existing is not None:
                await db.rollback()
                return _upload_out(existing)  # resume
            raise HTTPException(409, {"code": "out_of_order", "expected_index": bundle.completed_members})
        existing = await db.scalar(select(Upload).where(Upload.bundle_id == bundle.id, Upload.member_index == idx))
        if existing is not None:
            await db.rollback()
            return _upload_out(existing)
        up.rel_path = entry.path
        up.member_index = idx
        up.path = ""
        up.file_id = bundle.file_id
    else:
        # Streaming upload: the File and its parts exist from the first byte;
        # completed parts go to Telegram while the rest is still arriving, so
        # staging only ever holds a few parts.
        part_size = await settings_store.part_size_bytes(db)
        _check_space(min(data.size, (staging.MAX_STAGED_PARTS + 1) * part_size))
        f = File(owner_id=user.id, folder_id=data.folder_id, name=name, size=data.size, mime_type=up.mime_type,
                 part_size=part_size, status=FileStatus.RECEIVING, **(await _enc_fields(db)))
        db.add(f)
        await db.flush()
        for index, offset, length in plan_parts(data.size, part_size):
            db.add(FilePart(file_id=f.id, index=index, offset=offset, size=length))
        up.file_id = f.id
        up.path = ""
        staging.begin(f)
        up._part_size = part_size
    await db.commit()
    from app import sync
    sync.bump()  # the new row shows up in every open Files page right away
    return _upload_out(up)


@router.get("/uploads")
async def list_uploads(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    """The user's unfinished plain uploads (for the resume banner)."""
    rows = (await db.execute(select(Upload).where(
        Upload.owner_id == user.id, Upload.status == UploadStatus.ACTIVE, Upload.bundle_id.is_(None))
        .order_by(Upload.created_at))).scalars().all()
    out = []
    for up in rows:
        f = await db.get(File, up.file_id) if up.file_id else None
        d = _upload_out(up, f.part_size if f else None)
        d["file_status"] = f.status.value if f else None
        out.append(d)
    return out


@router.get("/uploads/{upload_id}")
async def upload_status(upload_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    up = await _own_upload(db, user, upload_id)
    f = await db.get(File, up.file_id) if up.file_id else None
    return _upload_out(up, f.part_size if f else None)


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
    if up.bundle_id is None and up.file_id is not None:
        return await _parallel_chunk(request, db, user, up, offset)
    if offset != up.received:
        raise HTTPException(409, {"code": "offset_mismatch", "received": up.received})
    if up.bundle_id is not None:
        return await _member_chunk(request, db, user, up)
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

    raise HTTPException(500, "unreachable")


async def _parallel_chunk(request: Request, db: AsyncSession, user: User, up: Upload, offset: int) -> dict:
    """Accept one fixed-size chunk at any offset. Idempotent: a chunk already
    present is acknowledged without rewriting. Data lands on disk right away;
    hashing follows the contiguous prefix under a per-upload lock."""
    cs = config.UPLOAD_CHUNK_SIZE
    total = _total_chunks(up.size)
    if offset < 0 or offset % cs != 0 or offset >= max(up.size, 1):
        raise HTTPException(400, {"code": "bad_offset", "chunk_size": cs, "size": up.size})
    idx = offset // cs
    expected = min(cs, up.size - offset)
    cm = staging.ChunkMap(up.chunk_map, total)
    if cm.has(idx):
        async for _ in request.stream():  # drain
            pass
        return _upload_out(up)
    f = await _own_file(db, user, up.file_id)
    parts = sorted(f.parts, key=lambda p: p.index)
    # Backpressure: chunks may run ahead of the hashed prefix by at most the
    # staging cap, so disk never holds more than a few parts.
    first_pending = next((p for p in parts if p.message_id is None), None)
    if first_pending is not None and f.part_size and (offset // f.part_size) - first_pending.index >= staging.MAX_STAGED_PARTS:
        raise HTTPException(429, {"code": "backpressure", "retry_after": 2, "received": up.received},
                            headers={"Retry-After": "2"})
    buf = bytearray()
    async for block in request.stream():
        buf += block
        if len(buf) > expected:
            raise HTTPException(400, "Chunk larger than expected")
    if len(buf) != expected:
        raise HTTPException(400, {"code": "short_chunk", "expected": expected, "got": len(buf)})
    await asyncio.to_thread(staging.write_at, f, parts, offset, bytes(buf))
    completed: list = []
    async with _lock(up.id):
        await db.refresh(up)
        cm = staging.ChunkMap(up.chunk_map, total)
        if not cm.has(idx):
            cm.set(idx)
            up.chunk_map = cm.raw()
            up.received = min(up.size, cm.count() * cs) if cm.count() < total else up.size
            new_prefix = cm.prefix_from(up.prefix_chunks)
            if new_prefix > up.prefix_chunks:
                hashed_to = min(up.prefix_chunks * cs, up.size)
                contiguous_to = min(new_prefix * cs, up.size)
                for p in parts:
                    await db.refresh(p, attribute_names=["message_id"])
                _, completed = await asyncio.to_thread(staging.advance_hash, f, parts, hashed_to, contiguous_to)
                up.prefix_chunks = new_prefix
            await db.commit()
    if completed:
        transfer_worker.kick()
    return _upload_out(up)


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, data: UploadComplete | None = None, db: AsyncSession = Depends(get_db),
                          user: User = Depends(security.current_user)):
    up = await _own_upload(db, user, upload_id)
    if up.status != UploadStatus.ACTIVE:
        raise HTTPException(409, "Upload already completed")
    if up.received != up.size:
        raise HTTPException(400, {"code": "incomplete", "received": up.received, "size": up.size})
    if up.bundle_id is None and up.file_id is not None:
        cm = staging.ChunkMap(up.chunk_map, _total_chunks(up.size))
        if cm.prefix_from(0) < cm.total:
            raise HTTPException(400, {"code": "incomplete", "missing_chunks": cm.missing(50)})
    _locks.pop(up.id, None)
    up.status = UploadStatus.COMPLETE
    if up.bundle_id:
        bundle = await db.get(Bundle, up.bundle_id)
        f = await _own_file(db, user, bundle.file_id)
        parts = sorted(f.parts, key=lambda p: p.index)
        layout = _layout(bundle)
        entry = layout.entries[up.member_index]
        if up.size == 0 and bundle.written == entry.lfh_offset:
            # Empty member: no chunk ever arrived, so emit its header now.
            await asyncio.to_thread(staging.write_range, f, parts, entry.lfh_offset, zipstream.local_header(layout, entry))
            bundle.written = entry.data_offset
        if bundle.written != entry.dd_offset:
            raise HTTPException(409, "Archive stream position out of sync; cancel the bundle and retry")
        completed = await asyncio.to_thread(staging.write_range, f, parts, entry.dd_offset,
                                            zipstream.data_descriptor(entry, up.crc32))
        bundle.written = entry.end
        bundle.completed_members += 1
        await db.commit()
        if completed:
            transfer_worker.kick()
        return {"upload": _upload_out(up), "file": None}
    f = await _own_file(db, user, up.file_id)
    server_digest = staging.whole_digest(f.id)
    staging.forget(f.id)
    parts = sorted(f.parts, key=lambda p: p.index)
    # Client-side digests (browser hashed the file while reading it). A
    # mismatch means bytes were corrupted between browser and server: the
    # file is failed and whatever already reached the channel is removed.
    if data and (data.sha256 or data.part_sha256):
        bad = None
        if data.part_sha256 is not None:
            if len(data.part_sha256) != len(parts):
                bad = f"client sent {len(data.part_sha256)} part digests, file has {len(parts)} parts"
            else:
                for p, d in zip(parts, data.part_sha256):
                    if p.sha256 and d != p.sha256:
                        bad = f"part {p.index + 1} was corrupted in transit"
                        break
        if bad is None and data.sha256 and server_digest and data.sha256 != server_digest:
            bad = "whole-file checksum mismatch between browser and server"
        if bad:
            from app.routers.files import _takedown
            await _takedown(f)
            for p in parts:
                p.message_id = None
                p.received = 0
                p.sha256 = None
                p.staging_path = None
            f.status = FileStatus.FAILED
            f.error = f"Upload rejected: {bad}. Please upload the file again."
            await db.delete(up)
            await db.commit()
            raise HTTPException(422, {"code": "checksum_mismatch", "detail": bad})
    # Whole digest: prefer what the server computed; after a restart mid-upload
    # only the browser still knows it.
    f.sha256 = server_digest or (data.sha256 if data else None)
    await db.refresh(f, attribute_names=["parts"])
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
def _layout(bundle: Bundle) -> zipstream.Layout:
    if not bundle.manifest:
        raise HTTPException(409, "This bundle was created by an older version; cancel it and start again")
    return zipstream.plan(json.loads(bundle.manifest), bundle.created_at)


async def _own_bundle(db: AsyncSession, user: User, bundle_id: str) -> Bundle:
    b = await db.get(Bundle, bundle_id)
    if b is None or b.owner_id != user.id:
        raise HTTPException(404, "Bundle not found")
    return b


async def _member_chunk(request: Request, db: AsyncSession, user: User, up: Upload) -> dict:
    """Route a member's bytes through the archive stream into the part files."""
    bundle = await _own_bundle(db, user, up.bundle_id)
    f = await _own_file(db, user, bundle.file_id)
    parts = sorted(f.parts, key=lambda p: p.index)
    layout = _layout(bundle)
    entry = layout.entries[up.member_index]
    if up.member_index != bundle.completed_members:
        raise HTTPException(409, {"code": "out_of_order", "expected_index": bundle.completed_members})
    if staging.staged_waiting(parts) >= staging.MAX_STAGED_PARTS:
        raise HTTPException(429, {"code": "backpressure", "retry_after": 2, "received": up.received},
                            headers={"Retry-After": "2"})
    completed: list = []
    if up.received == 0 and bundle.written == entry.lfh_offset:
        completed += await asyncio.to_thread(staging.write_range, f, parts, entry.lfh_offset,
                                             zipstream.local_header(layout, entry))
        bundle.written = entry.data_offset
    pos = entry.data_offset + up.received
    if bundle.written != pos:
        raise HTTPException(409, "Archive stream position out of sync; cancel the bundle and retry")
    crc = up.crc32
    async for block in request.stream():
        if pos + len(block) > entry.dd_offset:
            raise HTTPException(400, "Chunk exceeds declared file size")
        completed += await asyncio.to_thread(staging.write_range, f, parts, pos, block)
        crc = zipstream.crc_update(crc, block)
        pos += len(block)
    up.received = pos - entry.data_offset
    up.crc32 = crc
    bundle.written = pos
    await db.commit()
    if completed:
        transfer_worker.kick()
    return _upload_out(up)


@router.post("/bundles")
async def create_bundle(data: BundleCreate, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    """Start a streamed archive. The member list fixes the archive layout, so
    the File and its parts are created now and data flows to Telegram as it
    arrives, exactly like a plain upload."""
    await _check_folder(db, user, data.folder_id)
    name = _safe_name(data.name)
    if not name.lower().endswith(".zip"):
        name += ".zip"
    members = []
    for m in data.members:
        rel = _safe_rel_path(m.path, os.path.basename(m.path.replace("\\", "/")) or "file") or _safe_name(m.path)
        members.append({"path": rel, "size": int(m.size)})
    config.ensure_dirs()
    b = Bundle(owner_id=user.id, folder_id=data.folder_id, name=name, compress=False,
               manifest=json.dumps(members, separators=(",", ":")))
    db.add(b)
    await db.flush()
    layout = zipstream.plan(members, b.created_at)
    part_size = await settings_store.part_size_bytes(db)
    _check_space(min(layout.total, (staging.MAX_STAGED_PARTS + 1) * part_size))
    f = File(owner_id=user.id, folder_id=data.folder_id, name=name, size=layout.total, mime_type="application/zip",
             is_archive=True, part_size=part_size, status=FileStatus.RECEIVING, **(await _enc_fields(db)))
    db.add(f)
    await db.flush()
    for index, offset, length in plan_parts(layout.total, part_size):
        db.add(FilePart(file_id=f.id, index=index, offset=offset, size=length))
    b.file_id = f.id
    staging.begin(f)
    await db.commit()
    from app import sync
    sync.bump()
    return {"id": b.id, "name": b.name, "file_id": f.id, "size": layout.total, "compress": False,
            "members": [{"index": e.index, "path": e.path, "size": e.size} for e in layout.entries]}


@router.post("/bundles/{bundle_id}/complete")
async def complete_bundle(bundle_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    b = await _own_bundle(db, user, bundle_id)
    layout = _layout(b)
    if b.completed_members != len(layout.entries):
        raise HTTPException(400, {"code": "incomplete", "completed": b.completed_members, "total": len(layout.entries)})
    if b.written != layout.cd_offset:
        raise HTTPException(409, "Archive stream position out of sync; cancel the bundle and retry")
    f = await _own_file(db, user, b.file_id)
    parts = sorted(f.parts, key=lambda p: p.index)
    ups = (await db.execute(select(Upload).where(Upload.bundle_id == b.id).order_by(Upload.member_index))).scalars().all()
    crcs = [u.crc32 for u in ups]
    await asyncio.to_thread(staging.write_range, f, parts, layout.cd_offset, zipstream.central_directory(layout, crcs))
    f.sha256 = staging.whole_digest(f.id)
    staging.forget(f.id)
    await db.refresh(f, attribute_names=["parts"])
    if all(p.message_id is not None for p in f.parts):
        f.status = FileStatus.READY
        f.ready_at = datetime.utcnow()
    else:
        f.status = FileStatus.QUEUED
    for u in ups:
        await db.delete(u)
    await db.delete(b)
    await db.commit()
    transfer_worker.kick()
    return {"file": file_out(f)}


@router.delete("/bundles/{bundle_id}")
async def cancel_bundle(bundle_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    b = await _own_bundle(db, user, bundle_id)
    for u in (await db.execute(select(Upload).where(Upload.bundle_id == b.id))).scalars():
        if u.path:
            try:
                os.remove(u.path)
            except FileNotFoundError:
                pass
        await db.delete(u)
    if b.file_id:
        from app.routers.files import _takedown
        f = await _own_file(db, user, b.file_id)
        staging.forget(f.id)
        await _takedown(f)
        await db.delete(f)
    await db.delete(b)
    await db.commit()
    return {"ok": True}
