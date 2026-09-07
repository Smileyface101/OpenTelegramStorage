from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.db import get_db
from app.models import Session, User
from app.routers.common import user_out
from app.schemas import Credentials, PasswordChange

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(data: Credentials, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    security.login_throttle.check_ip(security.client_ip(request))
    user = await db.scalar(select(User).where(User.username == data.username))
    generic = HTTPException(401, "Invalid username or password")
    if user is None or not user.is_active:
        # Burn similar time to a real verify so usernames can't be probed by timing.
        security.verify_password(data.password, security.hash_password("x" * 12))
        raise generic
    if security.account_locked(user):
        raise HTTPException(423, "Account temporarily locked after repeated failures")
    if not security.verify_password(data.password, user.password_hash):
        security.record_failure(user)
        await db.commit()
        raise generic
    security.record_success(user)
    await security.create_session(db, user, request, response)
    await db.commit()
    return user_out(user)


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db),
                 user: User = Depends(security.current_user)):
    sess: Session = request.state.session
    await db.delete(sess)
    await db.commit()
    security.clear_session_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(security.current_user)):
    return user_out(user)


@router.post("/password")
async def change_password(data: PasswordChange, request: Request, db: AsyncSession = Depends(get_db),
                          user: User = Depends(security.current_user)):
    if not security.verify_password(data.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    security.validate_password_strength(data.new_password)
    user.password_hash = security.hash_password(data.new_password)
    # Revoke every other session for this account.
    current: Session = request.state.session
    await db.execute(delete(Session).where(Session.user_id == user.id, Session.token_hash != current.token_hash))
    await db.commit()
    return {"ok": True}
