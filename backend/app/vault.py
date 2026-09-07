"""Secrets at rest.

Telegram credentials and the MTProto session are encrypted with AES-256-GCM
under a master key. The key comes from the OTS_MASTER_KEY environment variable
(64 hex chars) or, failing that, from a 0600 file generated in the data
directory on first run. Losing the key means re-entering the Telegram
credentials; nothing else is lost."""
import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import config

_PREFIX = "v1:"
_key: bytes | None = None


def _load_key() -> bytes:
    raw = os.getenv(config.MASTER_KEY_ENV, "").strip()
    if raw:
        key = bytes.fromhex(raw)
        if len(key) != 32:
            raise RuntimeError(f"{config.MASTER_KEY_ENV} must be 64 hex characters")
        return key
    path = config.MASTER_KEY_FILE
    if path.exists():
        key = bytes.fromhex(path.read_text().strip())
        if len(key) != 32:
            raise RuntimeError(f"{path} is corrupt")
        return key
    config.ensure_dirs()
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return key


def get_key() -> bytes:
    global _key
    if _key is None:
        _key = _load_key()
    return _key


def reset_for_tests() -> None:
    global _key
    _key = None


def encrypt(plaintext: str) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(get_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    if not token.startswith(_PREFIX):
        raise ValueError("unknown vault format")
    blob = base64.urlsafe_b64decode(token[len(_PREFIX):])
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(get_key()).decrypt(nonce, ct, None).decode("utf-8")
