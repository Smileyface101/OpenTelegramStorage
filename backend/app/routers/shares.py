"""Public share links.

Owner side (session): create/list/revoke links per file.
Public side (no session): metadata, password check, download. A password
check returns a short-lived grant token so the password never travels in a
URL; the download itself is a plain GET (Range-capable) with the grant."""
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import config, security, settings_store
from app.db import get_db
from app.models import File, FileStatus, Share, User
from app.routers.files import _own_file, stream_file
from app.schemas import ShareCreate, SharePassword
from app.telegram.manager import manager

router = APIRouter(prefix="/api", tags=["shares"])

GRANT_TTL = 600
_grants: dict[str, tuple[str, float]] = {}   # grant -> (share id, expires)
_attempts: dict[str, list[float]] = {}       # "share:ip" -> timestamps


def _base_url(request: Request, public_url: str) -> str:
    if public_url:
        return public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") if config.TRUSTED_PROXY_COUNT > 0 else None
    host = request.headers.get("x-forwarded-host") if config.TRUSTED_PROXY_COUNT > 0 else None
    proto = proto or request.url.scheme
    host = host or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _share_out(s: Share, base: str) -> dict:
    return {
        "id": s.id, "url": f"{base}/s/{s.id}", "label": s.label, "file_id": s.file_id,
        "file_name": s.file.name if s.file else None, "file_size": s.file.size if s.file else None,
        "has_password": bool(s.password_hash), "expires_at": s.expires_at, "max_downloads": s.max_downloads,
        "download_count": s.download_count, "disabled": s.disabled, "created_at": s.created_at,
        "last_access_at": s.last_access_at, "active": _is_active(s),
    }


def _is_active(s: Share) -> bool:
    if s.disabled:
        return False
    if s.expires_at and s.expires_at <= datetime.utcnow():
        return False
    if s.max_downloads is not None and s.download_count >= s.max_downloads:
        return False
    return True


# ------------------------------------------------------------------ owner
@router.post("/files/{file_id}/shares")
async def create_share(file_id: str, data: ShareCreate, request: Request, db: AsyncSession = Depends(get_db),
                       user: User = Depends(security.current_user)):
    f = await _own_file(db, user, file_id)
    if f.status != FileStatus.READY:
        raise HTTPException(409, "Only files that are fully in the channel can be shared")
    s = Share(file_id=f.id, owner_id=user.id, label=(data.label or None),
              password_hash=security.hash_password(data.password) if data.password else None,
              expires_at=(datetime.utcnow() + timedelta(hours=data.expires_in_hours)) if data.expires_in_hours else None,
              max_downloads=data.max_downloads)
    db.add(s)
    await db.commit()
    await db.refresh(s, attribute_names=["file"])
    return _share_out(s, _base_url(request, await settings_store.get(db, "app.public_url") or ""))


@router.get("/files/{file_id}/shares")
async def list_file_shares(file_id: str, request: Request, db: AsyncSession = Depends(get_db),
                           user: User = Depends(security.current_user)):
    await _own_file(db, user, file_id)
    rows = (await db.execute(select(Share).options(selectinload(Share.file))
                             .where(Share.file_id == file_id, Share.owner_id == user.id).order_by(Share.created_at.desc()))).scalars().all()
    base = _base_url(request, await settings_store.get(db, "app.public_url") or "")
    return [_share_out(s, base) for s in rows]


@router.get("/shares")
async def list_shares(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    rows = (await db.execute(select(Share).options(selectinload(Share.file))
                             .where(Share.owner_id == user.id).order_by(Share.created_at.desc()))).scalars().all()
    base = _base_url(request, await settings_store.get(db, "app.public_url") or "")
    return [_share_out(s, base) for s in rows]


@router.post("/shares/{share_id}/toggle")
async def toggle_share(share_id: str, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    s = await db.get(Share, share_id, options=[selectinload(Share.file)])
    if s is None or s.owner_id != user.id:
        raise HTTPException(404, "Share not found")
    s.disabled = not s.disabled
    await db.commit()
    return _share_out(s, _base_url(request, await settings_store.get(db, "app.public_url") or ""))


@router.delete("/shares/{share_id}")
async def delete_share(share_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    s = await db.get(Share, share_id)
    if s is None or s.owner_id != user.id:
        raise HTTPException(404, "Share not found")
    await db.delete(s)
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- public
async def _public_share(db: AsyncSession, token: str) -> Share:
    s = await db.get(Share, token, options=[selectinload(Share.file).selectinload(File.parts)])
    if s is None or not _is_active(s) or s.file is None or s.file.status != FileStatus.READY:
        raise HTTPException(404, "This link does not exist or is no longer available")
    return s


def _throttle(key: str, limit: int = 10, window: float = 60.0) -> None:
    now = time.monotonic()
    q = [t for t in _attempts.get(key, []) if now - t < window]
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts, try again in a minute")
    q.append(now)
    _attempts[key] = q


def _new_grant(share_id: str) -> str:
    now = time.monotonic()
    for g in [g for g, (_sid, exp) in _grants.items() if exp < now]:
        _grants.pop(g, None)
    g = secrets.token_urlsafe(24)
    _grants[g] = (share_id, now + GRANT_TTL)
    return g


def _grant_ok(share_id: str, grant: str | None) -> bool:
    if not grant:
        return False
    item = _grants.get(grant)
    return bool(item and item[0] == share_id and item[1] > time.monotonic())


@router.get("/share/{token}")
async def public_share_info(token: str, db: AsyncSession = Depends(get_db)):
    s = await _public_share(db, token)
    return {
        "name": s.file.name, "size": s.file.size, "mime_type": s.file.mime_type, "label": s.label,
        "requires_password": bool(s.password_hash), "expires_at": s.expires_at,
        "downloads_left": (s.max_downloads - s.download_count) if s.max_downloads is not None else None,
        "sha256": s.file.sha256,
    }


@router.post("/share/{token}/unlock")
async def public_share_unlock(token: str, data: SharePassword, request: Request, db: AsyncSession = Depends(get_db)):
    s = await _public_share(db, token)
    if not s.password_hash:
        return {"grant": _new_grant(s.id)}
    _throttle(f"{s.id}:{security.client_ip(request)}")
    if not security.verify_password(data.password, s.password_hash):
        raise HTTPException(401, "Wrong password")
    return {"grant": _new_grant(s.id)}


@router.get("/share/{token}/download")
async def public_share_download(token: str, request: Request, grant: str | None = None,
                                db: AsyncSession = Depends(get_db)):
    s = await _public_share(db, token)
    if s.password_hash and not _grant_ok(s.id, grant):
        raise HTTPException(401, "Password required")
    if not manager.ready():
        raise HTTPException(503, "Storage backend is not connected right now")
    # Count a download once: range requests that continue a download start
    # past byte 0 and are not counted again.
    rng = request.headers.get("range", "")
    if not rng or rng.strip().startswith("bytes=0-"):
        s.download_count += 1
    s.last_access_at = datetime.utcnow()
    await db.commit()
    return stream_file(s.file, request)
