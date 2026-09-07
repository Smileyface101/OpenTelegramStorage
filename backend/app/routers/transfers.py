from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import security
from app.db import get_db
from app.models import File, FileStatus, User
from app.routers.common import file_out

router = APIRouter(prefix="/api/transfers", tags=["transfers"])


@router.get("")
async def list_transfers(limit: int = 50, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_user)):
    active = (await db.execute(
        select(File).options(selectinload(File.parts))
        .where(File.owner_id == user.id, File.status != FileStatus.READY)
        .order_by(File.created_at))).scalars().all()
    recent = (await db.execute(
        select(File).options(selectinload(File.parts))
        .where(File.owner_id == user.id, File.status == FileStatus.READY)
        .order_by(File.ready_at.desc()).limit(min(limit, 200)))).scalars().all()
    return {"active": [file_out(f) for f in active], "recent": [file_out(f) for f in recent]}
