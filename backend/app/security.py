"""Passwords, cookie sessions, CSRF, login throttling, client IP."""
import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.db import get_db
from app.models import Session, User, UserRole

SESSION_COOKIE = "ots_session"
CSRF_COOKIE = "ots_csrf"
CSRF_HEADER = "x-csrf-token"

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    if len(password) > 256:
        raise HTTPException(400, "Password is too long")


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if config.TRUSTED_PROXY_COUNT <= 0:
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if len(hops) >= config.TRUSTED_PROXY_COUNT:
        return hops[-config.TRUSTED_PROXY_COUNT]
    return peer


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _cookie_secure(request: Request) -> bool:
    if config.COOKIE_SECURE is not None:
        return config.COOKIE_SECURE
    proto = request.headers.get("x-forwarded-proto", "") if config.TRUSTED_PROXY_COUNT > 0 else ""
    return (proto or request.url.scheme) == "https"


async def create_session(db: AsyncSession, user: User, request: Request, response: Response) -> Session:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    sess = Session(
        token_hash=_token_hash(token),
        user_id=user.id,
        csrf_token=csrf,
        created_at=now,
        expires_at=now + timedelta(days=config.SESSION_TTL_DAYS),
        last_seen_at=now,
        ip=client_ip(request)[:64],
        user_agent=(request.headers.get("user-agent") or "")[:255],
    )
    db.add(sess)
    await db.flush()
    secure = _cookie_secure(request)
    max_age = config.SESSION_TTL_DAYS * 86400
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=secure, samesite="lax", path="/")
    return sess


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


async def load_session(db: AsyncSession, request: Request) -> tuple[Session, User] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = await db.get(Session, _token_hash(token))
    if sess is None:
        return None
    now = datetime.utcnow()
    if sess.expires_at <= now:
        await db.delete(sess)
        await db.commit()
        return None
    user = await db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if (now - sess.last_seen_at) > timedelta(minutes=5):
        sess.last_seen_at = now
        await db.commit()
    return sess, user


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    loaded = await load_session(db, request)
    if loaded is None:
        raise HTTPException(401, "Not signed in")
    sess, user = loaded
    request.state.session = sess
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    return user


def csrf_ok(request: Request) -> bool:
    """Double-submit check: header must match the CSRF cookie. Combined with
    SameSite=Lax cookies this blocks cross-site state changes."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    return bool(cookie and header and secrets.compare_digest(cookie, header))


class LoginThrottle:
    """Per-IP sliding-window limit plus per-account lockout (in the DB)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def check_ip(self, ip: str) -> None:
        now = time.monotonic()
        q = self._hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.LOGIN_RATE_PER_MINUTE:
            raise HTTPException(429, "Too many login attempts, slow down")
        q.append(now)

    def reset(self) -> None:
        self._hits.clear()


login_throttle = LoginThrottle()


def account_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.utcnow())


def record_failure(user: User) -> None:
    user.failed_logins += 1
    if user.failed_logins >= config.LOGIN_MAX_FAILURES:
        user.locked_until = datetime.utcnow() + timedelta(minutes=config.LOGIN_LOCKOUT_MINUTES)
        user.failed_logins = 0


def record_success(user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
