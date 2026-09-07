"""Admin: app settings and user management."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, security, settings_store
from app.db import get_db
from app.models import Session, User, UserRole
from app.routers.common import user_out
from app.schemas import SettingsUpdate, UserCreate

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
