from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.db import get_db
from app.models import Session, User
from app.routers.common import user_out
from app import vault
from app.schemas import Credentials, MfaLogin, PasswordChange, TotpCode, TotpDisable

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
    if user.totp_enabled:
        # Password is right; hand out a short-lived token for the second step.
        await db.commit()
        token = security.pending_logins.create(user.id, security.client_ip(request))
        return {"mfa_required": True, "mfa_token": token, "recovery_codes_left": security.recovery_remaining(user)}
    await security.create_session(db, user, request, response)
    await db.commit()
    return user_out(user)


@router.post("/login/mfa")
async def login_mfa(data: MfaLogin, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Second step: a TOTP code or a recovery code."""
    security.login_throttle.check_ip(security.client_ip(request))
    pending = security.pending_logins.get(data.mfa_token)
    if pending is None:
        raise HTTPException(401, "Sign in again")
    user = await db.get(User, pending["user_id"])
    if user is None or not user.is_active or not user.totp_enabled:
        security.pending_logins.consume(data.mfa_token)
        raise HTTPException(401, "Sign in again")
    ok = security.totp_verify(user, data.code) or security.recovery_use(user, data.code)
    if not ok:
        security.pending_logins.fail(data.mfa_token)
        await db.rollback()
        raise HTTPException(401, "Invalid code")
    security.pending_logins.consume(data.mfa_token)
    await security.create_session(db, user, request, response)
    await db.commit()
    return user_out(user)


# ------------------------------------------------------------ two-factor
@router.get("/totp")
async def totp_status(user: User = Depends(security.current_user)):
    return {"enabled": bool(user.totp_enabled), "recovery_codes_left": security.recovery_remaining(user)}


@router.post("/totp/setup")
async def totp_setup(user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    """Generate a secret (not yet active) and return the otpauth URI to scan."""
    if user.totp_enabled:
        raise HTTPException(409, "Two-factor is already enabled; disable it first to re-enrol")
    secret = security.totp_new_secret()
    user.totp_secret = vault.encrypt(secret)
    user.totp_last_counter = 0
    await db.commit()
    return {"secret": secret, "uri": security.totp_uri(secret, user.username)}


@router.post("/totp/enable")
async def totp_enable(data: TotpCode, request: Request, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    """Confirm the authenticator works, switch 2FA on, return recovery codes (once)."""
    if user.totp_enabled:
        raise HTTPException(409, "Already enabled")
    if not user.totp_secret:
        raise HTTPException(400, "Run setup first")
    if not security.totp_verify(user, data.code):
        raise HTTPException(400, "That code is not valid; check the time on your phone and try again")
    user.totp_enabled = True
    codes = security.recovery_generate(user)
    # Every other session must re-authenticate with the new factor.
    current: Session = request.state.session
    await db.execute(delete(Session).where(Session.user_id == user.id, Session.token_hash != current.token_hash))
    await db.commit()
    return {"enabled": True, "recovery_codes": codes}


@router.post("/totp/recovery-codes")
async def totp_new_recovery_codes(data: TotpCode, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    if not user.totp_enabled:
        raise HTTPException(400, "Two-factor is not enabled")
    if not security.totp_verify(user, data.code):
        raise HTTPException(400, "Invalid code")
    codes = security.recovery_generate(user)
    await db.commit()
    return {"recovery_codes": codes}


@router.post("/totp/disable")
async def totp_disable(data: TotpDisable, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    if not user.totp_enabled:
        raise HTTPException(400, "Two-factor is not enabled")
    if not security.verify_password(data.password, user.password_hash):
        raise HTTPException(400, "Password is incorrect")
    if not (security.totp_verify(user, data.code) or security.recovery_use(user, data.code)):
        raise HTTPException(400, "Invalid code")
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = None
    await db.commit()
    return {"enabled": False}


# ------------------------------------------------------------ sessions
def _session_out(s: Session, current: Session) -> dict:
    return {"id": s.token_hash[:16], "created_at": s.created_at, "last_seen_at": s.last_seen_at,
            "expires_at": s.expires_at, "ip": s.ip, "user_agent": s.user_agent,
            "current": s.token_hash == current.token_hash}


@router.get("/sessions")
async def list_sessions(request: Request, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    current: Session = request.state.session
    rows = (await db.execute(select(Session).where(Session.user_id == user.id).order_by(Session.last_seen_at.desc()))).scalars().all()
    return [_session_out(s, current) for s in rows]


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    current: Session = request.state.session
    rows = (await db.execute(select(Session).where(Session.user_id == user.id))).scalars().all()
    target = next((s for s in rows if s.token_hash[:16] == session_id), None)
    if target is None:
        raise HTTPException(404, "Session not found")
    if target.token_hash == current.token_hash:
        raise HTTPException(400, "Use sign out for the current session")
    await db.delete(target)
    await db.commit()
    return {"ok": True}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, user: User = Depends(security.current_user), db: AsyncSession = Depends(get_db)):
    current: Session = request.state.session
    r = await db.execute(delete(Session).where(Session.user_id == user.id, Session.token_hash != current.token_hash))
    await db.commit()
    return {"ok": True, "revoked": r.rowcount}


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
