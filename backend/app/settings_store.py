"""Typed access to the settings table, with vault encryption for secrets."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config, vault
from app.models import Setting

SECRET_KEYS = {"telegram.api_hash", "telegram.bot_token", "telegram.session"}

DEFAULTS: dict[str, str] = {
    "transfer.part_size_mb": str(config.DEFAULT_PART_SIZE_MB),
    "transfer.max_retries": str(config.MAX_UPLOAD_RETRIES),
    "transfer.compress_archives": "false",
    "transfer.upload_connections": "4",
}


async def get(db: AsyncSession, key: str) -> str | None:
    row = await db.get(Setting, key)
    if row is None or row.value is None:
        return DEFAULTS.get(key)
    return vault.decrypt(row.value) if row.encrypted else row.value


async def set(db: AsyncSession, key: str, value: str | None) -> None:
    row = await db.get(Setting, key)
    encrypted = key in SECRET_KEYS and value is not None
    stored = vault.encrypt(value) if encrypted else value
    if row is None:
        db.add(Setting(key=key, value=stored, encrypted=encrypted))
    else:
        row.value, row.encrypted = stored, encrypted
    await db.flush()


async def delete(db: AsyncSession, key: str) -> None:
    row = await db.get(Setting, key)
    if row is not None:
        await db.delete(row)
        await db.flush()


async def get_int(db: AsyncSession, key: str, default: int) -> int:
    try:
        return int(await get(db, key) or default)
    except ValueError:
        return default


async def get_bool(db: AsyncSession, key: str, default: bool = False) -> bool:
    val = await get(db, key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


async def part_size_bytes(db: AsyncSession) -> int:
    mb = await get_int(db, "transfer.part_size_mb", config.DEFAULT_PART_SIZE_MB)
    mb = max(1, min(config.MAX_PART_SIZE_MB, mb))
    return mb * 1024 * 1024


async def public_settings(db: AsyncSession) -> dict:
    return {
        "part_size_mb": await get_int(db, "transfer.part_size_mb", config.DEFAULT_PART_SIZE_MB),
        "max_part_size_mb": config.MAX_PART_SIZE_MB,
        "max_retries": await get_int(db, "transfer.max_retries", config.MAX_UPLOAD_RETRIES),
        "compress_archives": await get_bool(db, "transfer.compress_archives", False),
        "upload_connections": max(1, min(16, await get_int(db, "transfer.upload_connections", 4))),
    }


async def all_rows(db: AsyncSession) -> list[Setting]:
    return list((await db.execute(select(Setting))).scalars())
