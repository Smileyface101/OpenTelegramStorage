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


# ---------------------------------------------------------------------------
# Two-factor authentication (TOTP, RFC 6238) and recovery codes
# ---------------------------------------------------------------------------
import base64 as _b64
import json as _json
import os as _os

import pyotp

from app import vault

TOTP_ISSUER = "OpenTelegramStorage"
RECOVERY_CODE_COUNT = 10
MFA_PENDING_TTL_SECONDS = 300
MFA_MAX_ATTEMPTS = 5


def totp_new_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def totp_verify(user: User, code: str) -> bool:
    """Verify a 6-digit code with ±1 step tolerance, refusing reuse of the
    same time step (replay guard)."""
    if not user.totp_secret:
        return False
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    secret = vault.decrypt(user.totp_secret)
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (0, -1, 1):
        t = now + offset * totp.interval
        if totp.verify(code, for_time=t, valid_window=0):
            counter = t // totp.interval
            if counter <= user.totp_last_counter:
                return False
            user.totp_last_counter = counter
            return True
    return False


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def recovery_generate(user: User) -> list[str]:
    """Create fresh recovery codes; returns the plaintext list (shown once)."""
    codes = ["-".join(_b64.b32encode(_os.urandom(5)).decode("ascii").lower()[i:i + 4] for i in (0, 4)) for _ in range(RECOVERY_CODE_COUNT)]
    user.recovery_codes = _json.dumps([_code_hash(c) for c in codes])
    return codes


def recovery_use(user: User, code: str) -> bool:
    """Consume a recovery code. Returns True if it was valid."""
    if not user.recovery_codes:
        return False
    code = (code or "").strip().lower().replace(" ", "")
    if not code:
        return False
    hashes = _json.loads(user.recovery_codes)
    h = _code_hash(code)
    for stored in hashes:
        if secrets.compare_digest(stored, h):
            hashes.remove(stored)
            user.recovery_codes = _json.dumps(hashes)
            return True
    return False


def recovery_remaining(user: User) -> int:
    return len(_json.loads(user.recovery_codes)) if user.recovery_codes else 0


class PendingLogins:
    """Password accepted, second factor outstanding. Short-lived, in memory
    (the app runs as a single process)."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def create(self, user_id: int, ip: str) -> str:
        self._sweep()
        token = secrets.token_urlsafe(32)
        self._items[token] = {"user_id": user_id, "ip": ip, "expires": time.monotonic() + MFA_PENDING_TTL_SECONDS, "attempts": 0}
        return token

    def get(self, token: str) -> dict | None:
        self._sweep()
        return self._items.get(token or "")

    def fail(self, token: str) -> None:
        item = self._items.get(token)
        if item is None:
            return
        item["attempts"] += 1
        if item["attempts"] >= MFA_MAX_ATTEMPTS:
            self._items.pop(token, None)

    def consume(self, token: str) -> None:
        self._items.pop(token, None)

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if v["expires"] < now]:
            self._items.pop(k, None)

    def reset(self) -> None:
        self._items.clear()


pending_logins = PendingLogins()
