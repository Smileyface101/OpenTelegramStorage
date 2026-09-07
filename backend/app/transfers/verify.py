"""Integrity checks.

- `verify_file` re-reads every part from Telegram, hashes it, and compares with
  the digest recorded at upload time (and the whole-file digest when known).
  Nothing is written to disk. Runs as a background task per file.
- `HashingStream` wraps the download generator so a part that streams out in
  full is checked on the fly; a mismatch aborts the response and is recorded.
"""
import asyncio
import hashlib
import logging
from datetime import datetime

from sqlalchemy.orm import selectinload

from app import db as _db
from app.models import File

logger = logging.getLogger(__name__)

verifying: set[str] = set()


async def record_mismatch(file_id: str, message: str) -> None:
    async with _db.async_session() as db:
        f = await db.get(File, file_id)
        if f is not None and not f.integrity_error:
            f.integrity_error = message[:300]
            await db.commit()
    logger.error("Integrity failure on file %s: %s", file_id, message)


async def verify_file(manager, file_id: str) -> None:
    if file_id in verifying:
        return
    verifying.add(file_id)
    try:
        async with _db.async_session() as db:
            f = await db.get(File, file_id, options=[selectinload(File.parts)])
            if f is None:
                return
            parts = sorted(f.parts, key=lambda p: p.index)
            whole = hashlib.sha256()
            problems: list[str] = []
            for p in parts:
                if p.message_id is None:
                    problems.append(f"part {p.index + 1} is not in the channel")
                    continue
                h = hashlib.sha256()
                got = 0
                try:
                    doc = await manager.get_document(p.message_id)
                    async for chunk in manager.iter_download(doc, 0, p.size):
                        h.update(chunk)
                        whole.update(chunk)
                        got += len(chunk)
                except FileNotFoundError:
                    problems.append(f"part {p.index + 1}: message {p.message_id} is missing from the channel")
                    continue
                if got != p.size:
                    problems.append(f"part {p.index + 1}: got {got} bytes, expected {p.size}")
                elif p.sha256 and h.hexdigest() != p.sha256:
                    problems.append(f"part {p.index + 1}: checksum mismatch")
                await asyncio.sleep(0)
            if not problems and f.sha256 and whole.hexdigest() != f.sha256:
                problems.append("whole-file checksum mismatch")
            f.verified_at = datetime.utcnow()
            f.integrity_error = "; ".join(problems)[:300] if problems else None
            await db.commit()
            if problems:
                logger.error("Verification of %s failed: %s", f.name, f.integrity_error)
            else:
                logger.info("Verified %s (%d part(s)) OK", f.name, len(parts))
    except Exception as e:  # noqa: BLE001
        logger.exception("verify failed for %s", file_id)
        await record_mismatch(file_id, f"verification error: {e}")
    finally:
        verifying.discard(file_id)
