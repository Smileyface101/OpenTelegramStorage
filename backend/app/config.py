"""Runtime configuration. Everything lives under one data directory so a single
volume mount is the entire persistent state of an installation."""
import os
from pathlib import Path


def _env_bool(name: str, default: bool | None) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


DATA_DIR = Path(os.getenv("OTS_DATA_DIR", "./data")).resolve()
STAGING_DIR = DATA_DIR / "staging"
DB_PATH = DATA_DIR / "ots.db"
MASTER_KEY_FILE = DATA_DIR / "master.key"
MASTER_KEY_ENV = "OTS_MASTER_KEY"

# Cookie hardening. None = decide per request from the scheme (https -> Secure).
COOKIE_SECURE: bool | None = _env_bool("OTS_COOKIE_SECURE", None)
SESSION_TTL_DAYS = _env_int("OTS_SESSION_TTL_DAYS", 14)

# Reverse proxies in front of the app, counted from the right of
# X-Forwarded-For. 0 = trust the socket peer only.
TRUSTED_PROXY_COUNT = _env_int("OTS_TRUSTED_PROXY_COUNT", 0)

# Login brute-force protection.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15
LOGIN_RATE_PER_MINUTE = 20

# Transfers. Telegram caps a single bot upload at 2000 MiB; we keep a margin.
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MAX_PART_SIZE_MB = 1990
DEFAULT_PART_SIZE_MB = _env_int("OTS_DEFAULT_PART_SIZE_MB", 512)
MAX_UPLOAD_RETRIES = 3
STAGING_FREE_SPACE_MARGIN = 256 * 1024 * 1024

# Server-side import: a directory (mount it into the container) whose files
# can be sent to the channel directly, without a browser or a staging copy.
IMPORT_DIR = Path(os.getenv("OTS_IMPORT_DIR", str(DATA_DIR / "import"))).resolve()

# Where the built SPA lives (Docker copies it here).
FRONTEND_DIST = Path(os.getenv("OTS_FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend" / "dist")))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_db_name()


def _migrate_legacy_db_name() -> None:
    """Installs created under the project's former name used otg.db."""
    old = DATA_DIR / "otg.db"
    if old.exists() and not DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            src = DATA_DIR / f"otg.db{suffix}"
            if src.exists():
                src.rename(DATA_DIR / f"ots.db{suffix}")
