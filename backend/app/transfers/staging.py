"""Streaming upload state: maps incoming byte ranges onto per-part staging
files and keeps incremental SHA-256 hashers so a part is hashed by the time
its last byte arrives (no second pass over the disk).

Hasher state lives in memory. After a server restart a partially received
part is re-hashed from its staging file on the next chunk; the whole-file
digest cannot be recovered (earlier parts are already gone to Telegram) and is
left NULL in that case, which the UI reports honestly."""
import hashlib
import os
from dataclasses import dataclass, field

from app import config
from app.models import File, FilePart

MAX_STAGED_PARTS = 3  # parts waiting for Telegram before the browser is told to pause
HASH_BLOCK = 4 * 1024 * 1024


@dataclass
class _State:
    whole: "hashlib._Hash | None"
    parts: dict[int, "hashlib._Hash"] = field(default_factory=dict)


_states: dict[str, _State] = {}


def part_path(file_id: str, index: int) -> str:
    return str(config.STAGING_DIR / f"{file_id}.p{index:03d}")


def _state(file: File, fresh: bool) -> _State:
    st = _states.get(file.id)
    if st is None:
        st = _State(whole=hashlib.sha256() if fresh else None)
        _states[file.id] = st
    return st


def _part_hasher(st: _State, part: FilePart) -> "hashlib._Hash":
    h = st.parts.get(part.index)
    if h is None:
        h = hashlib.sha256()
        # Server restarted mid-part: rebuild from what is on disk.
        if part.received and part.staging_path and os.path.exists(part.staging_path):
            with open(part.staging_path, "rb") as fh:
                remaining = part.received
                while remaining > 0:
                    block = fh.read(min(HASH_BLOCK, remaining))
                    if not block:
                        break
                    h.update(block)
                    remaining -= len(block)
        st.parts[part.index] = h
    return h


def begin(file: File) -> None:
    """Call once when a streaming upload starts (before any chunk)."""
    _states[file.id] = _State(whole=hashlib.sha256())


def forget(file_id: str) -> None:
    _states.pop(file_id, None)


def write_range(file: File, parts: list[FilePart], offset: int, data: bytes) -> list[FilePart]:
    """Append `data` at file offset `offset` (must equal bytes received so far)
    into the right part file(s). Returns the parts that became complete."""
    st = _state(file, fresh=(offset == 0))
    completed: list[FilePart] = []
    pos = offset
    view = memoryview(data)
    while view:
        part = parts[pos // file.part_size] if file.part_size else parts[0]
        if part.staging_path is None:
            part.staging_path = part_path(file.id, part.index)
        local = pos - part.offset
        room = part.size - local
        chunk = view[:room]
        h = _part_hasher(st, part)
        with open(part.staging_path, "ab" if local else "wb") as fh:
            fh.write(chunk)
        h.update(chunk)
        if st.whole is not None:
            st.whole.update(chunk)
        part.received = local + len(chunk)
        pos += len(chunk)
        view = view[len(chunk):]
        if part.received >= part.size:
            part.sha256 = h.hexdigest()
            st.parts.pop(part.index, None)
            completed.append(part)
    return completed


def whole_digest(file_id: str) -> str | None:
    st = _states.get(file_id)
    return st.whole.hexdigest() if st and st.whole is not None else None


def staged_waiting(parts: list[FilePart]) -> int:
    return sum(1 for p in parts if p.staged and p.message_id is None)


def remove_part_files(parts: list[FilePart]) -> None:
    for p in parts:
        if p.staging_path:
            try:
                os.remove(p.staging_path)
            except FileNotFoundError:
                pass
