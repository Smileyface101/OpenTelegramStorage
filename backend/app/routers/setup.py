from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__, security, settings_store
from app.db import get_db
from app.models import User, UserRole
from app.routers.common import user_out
from app.schemas import Credentials, SetupStatus
from app.telegram.manager import manager

router = APIRouter(prefix="/api/setup", tags=["setup"])


async def user_count(db: AsyncSession) -> int:
    return (await db.scalar(select(func.count(User.id)))) or 0


@router.get("/status", response_model=SetupStatus)
async def status(db: AsyncSession = Depends(get_db)):
    st = manager.status
    return SetupStatus(
        needs_admin=(await user_count(db)) == 0,
        telegram_configured=st.configured,
        telegram_connected=st.connected,
        channel_configured=st.channel_id is not None,
        workspace_mode=await settings_store.get(db, "workspace.mode"),
        version=__version__,
    )


@router.post("/admin")
async def create_admin(data: Credentials, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """First-run only: create the admin account and sign in."""
    if await user_count(db) > 0:
        raise HTTPException(409, "Setup already completed")
    security.validate_password_strength(data.password)
    user = User(username=data.username, password_hash=security.hash_password(data.password), role=UserRole.ADMIN)
    db.add(user)
    await db.flush()
    await security.create_session(db, user, request, response)
    await db.commit()
    return user_out(user)
