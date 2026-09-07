"""Streaming helpers: bounded range reader (so a part is never copied to a
temporary file), hashing, and archive building."""
import hashlib
import os
import zipfile
from pathlib import Path

HASH_BLOCK = 4 * 1024 * 1024


class RangeReader:
    """File-like view over [start, start+length) of a file on disk. Telethon's
    upload_file reads from it sequentially; seek/tell are supported because
    Telethon probes them to size the stream."""

    def __init__(self, path: str | os.PathLike, start: int, length: int, name: str | None = None):
        self._fh = open(path, "rb")
        self._start = start
        self._length = length
        self._pos = 0
        self.name = name or os.path.basename(str(path))
        self._fh.seek(start)

    def read(self, n: int = -1) -> bytes:
        remaining = self._length - self._pos
        if n is None or n < 0 or n > remaining:
            n = remaining
        if n <= 0:
            return b""
        data = self._fh.read(n)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._pos + offset
        elif whence == os.SEEK_END:
            new = self._length + offset
        else:
            raise ValueError("bad whence")
        new = max(0, min(self._length, new))
        self._fh.seek(self._start + new)
        self._pos = new
        return new

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def plan_parts(size: int, part_size: int) -> list[tuple[int, int, int]]:
    """Return [(index, offset, length)] covering `size` bytes. A zero-byte
    file still gets one empty part so it has a message in the channel."""
    if part_size <= 0:
        raise ValueError("part_size must be positive")
    if size == 0:
        return [(0, 0, 0)]
    parts = []
    offset = 0
    index = 0
    while offset < size:
        length = min(part_size, size - offset)
        parts.append((index, offset, length))
        offset += length
        index += 1
    return parts


def part_name(file_name: str, index: int, total: int) -> str:
    if total <= 1:
        return file_name
    return f"{file_name}.{index + 1:03d}"


def hash_ranges(path: str | os.PathLike, ranges: list[tuple[int, int, int]]) -> tuple[str, list[str]]:
    """Blocking. Returns (whole-file sha256, [per-range sha256]) in one pass."""
    whole = hashlib.sha256()
    per: list[str] = []
    with open(path, "rb") as fh:
        for _index, offset, length in ranges:
            fh.seek(offset)
            h = hashlib.sha256()
            remaining = length
            while remaining > 0:
                block = fh.read(min(HASH_BLOCK, remaining))
                if not block:
                    raise IOError("file shorter than expected")
                h.update(block)
                whole.update(block)
                remaining -= len(block)
            per.append(h.hexdigest())
    return whole.hexdigest(), per


def build_archive(out_path: str | os.PathLike, members: list[tuple[str, str]], compress: bool) -> int:
    """Blocking. Zip `members` = [(arcname, source_path)] into out_path.
    Store-only by default: most large files are already compressed and the
    transfer is bound by Telegram, not disk. Returns the archive size."""
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    seen: set[str] = set()
    with zipfile.ZipFile(out_path, "w", compression=method, allowZip64=True) as zf:
        for arcname, src in members:
            arcname = _unique_arcname(arcname, seen)
            zf.write(src, arcname=arcname)
    return Path(out_path).stat().st_size


def _unique_arcname(name: str, seen: set[str]) -> str:
    base = name
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    candidate = base
    n = 1
    while candidate in seen:
        n += 1
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
    seen.add(candidate)
    return candidate
