"""Admin: app settings and user management."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app import config, maintenance, recovery, security, settings_store, status as status_mod
from app.transfers import importer
from app.telegram.manager import manager
from app.db import get_db
from app.models import Session, User, UserRole
from app.routers.common import user_out
from app.schemas import ImportRequest, SettingsUpdate, UserCreate

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    return await settings_store.public_settings(db)


@router.put("/settings")
async def update_settings(data: SettingsUpdate, db: AsyncSession = Depends(get_db),
                          user: User = Depends(security.current_admin)):
    if data.part_size_mb is not None:
        if data.part_size_mb > config.MAX_PART_SIZE_MB:
            raise HTTPException(400, f"Part size cannot exceed {config.MAX_PART_SIZE_MB} MB (Telegram limit)")
        await settings_store.set(db, "transfer.part_size_mb", str(data.part_size_mb))
    if data.max_retries is not None:
        await settings_store.set(db, "transfer.max_retries", str(data.max_retries))
    if data.compress_archives is not None:
        await settings_store.set(db, "transfer.compress_archives", "true" if data.compress_archives else "false")
    if data.upload_connections is not None:
        await settings_store.set(db, "transfer.upload_connections", str(data.upload_connections))
    if data.stale_upload_hours is not None:
        await settings_store.set(db, "transfer.stale_upload_hours", str(data.stale_upload_hours))
    await db.commit()
    return await settings_store.public_settings(db)


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    rows = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return [user_out(u) for u in rows]


@router.post("/users")
async def create_user(data: UserCreate, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    if data.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be admin or user")
    security.validate_password_strength(data.password)
    if await db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(409, "Username already taken")
    u = User(username=data.username, password_hash=security.hash_password(data.password), role=UserRole(data.role))
    db.add(u)
    await db.commit()
    return user_out(u)


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    if user_id == user.id:
        raise HTTPException(400, "You cannot delete your own account")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.role == UserRole.ADMIN:
        admins = await db.scalar(select(func.count(User.id)).where(User.role == UserRole.ADMIN))
        if (admins or 0) <= 1:
            raise HTTPException(400, "Cannot delete the last admin")
    # Files stay indexed under the deleted owner? No: cascade removes rows but
    # not channel messages; force the caller to delete files first.
    from app.models import File
    n = await db.scalar(select(func.count(File.id)).where(File.owner_id == target.id))
    if n:
        raise HTTPException(409, f"User still owns {n} file(s); delete them first")
    await db.execute(delete(Session).where(Session.user_id == target.id))
    await db.delete(target)
    await db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/toggle")
async def toggle_user(user_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    if user_id == user.id:
        raise HTTPException(400, "You cannot disable your own account")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    target.is_active = not target.is_active
    if not target.is_active:
        await db.execute(delete(Session).where(Session.user_id == target.id))
    await db.commit()
    return user_out(target)


@router.get("/rebuild")
async def rebuild_status(user: User = Depends(security.current_admin)):
    return recovery.snapshot()


@router.post("/rebuild")
async def rebuild_start(db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    """Scan the channel and import every file not present in the index.
    Runs in the background; poll GET /api/admin/rebuild for progress."""
    if not manager.ready():
        raise HTTPException(503, "Telegram is not connected or no channel is selected")
    if recovery.state.running:
        return recovery.snapshot()
    part_size = await settings_store.part_size_bytes(db)
    asyncio.create_task(recovery.rebuild(manager, user.id, part_size))
    await asyncio.sleep(0)
    return recovery.snapshot()


@router.get("/import/browse")
async def import_browse(path: str = "", user: User = Depends(security.current_admin)):
    """List a directory inside the server-side import mount."""
    if not importer.enabled():
        return {"enabled": False, "root": str(config.IMPORT_DIR), "path": "", "entries": []}
    return {"enabled": True, **importer.browse(path)}


@router.post("/import")
async def import_start(data: ImportRequest, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    """Import a file or directory from the import mount into the channel.
    Sources are read in place and never deleted."""
    from app.models import Folder
    from app.routers.common import file_out
    from app.transfers import worker as transfer_worker
    if data.folder_id is not None:
        folder = await db.get(Folder, data.folder_id)
        if folder is None or folder.owner_id != user.id:
            raise HTTPException(404, "Folder not found")
    src = importer.resolve(data.path)
    part_size = await settings_store.part_size_bytes(db)
    if src.is_file():
        if data.mode not in ("file", None):
            raise HTTPException(400, "A file can only be imported as a file")
        files = [await importer.import_file(db, user.id, data.folder_id, src, part_size)]
    elif data.mode == "tree":
        files = await importer.import_tree(db, user.id, data.folder_id, src, part_size)
    elif data.mode == "zip":
        name = (data.name or src.name).strip() or src.name
        if not name.lower().endswith(".zip"):
            name += ".zip"
        files = [await importer.start_zip(db, user.id, data.folder_id, src, name[:255], part_size)]
    else:
        raise HTTPException(400, "mode must be 'zip' or 'tree' for a directory")
    await db.commit()
    for f in files:
        await db.refresh(f, attribute_names=["parts"])
        importer.launch(f.id)
    transfer_worker.kick()
    return {"files": [file_out(f) for f in files], "count": len(files)}


@router.get("/status")
async def system_status(user: User = Depends(security.current_admin)):
    from app.transfers import worker as transfer_worker
    return await status_mod.snapshot(manager, transfer_worker.worker)


@router.post("/maintenance/cleanup")
async def run_cleanup(user: User = Depends(security.current_admin)):
    """Remove abandoned uploads and orphaned staging files now."""
    return await maintenance.cleanup(manager)
