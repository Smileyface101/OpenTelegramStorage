"""Deterministic streaming ZIP (store-only).

Given the member list up front (names + sizes) every byte offset of the
archive is known before any data arrives, so the archive can be written
straight into the part pipeline and split at the planned part boundaries.
CRCs are not known in advance: local headers use the data-descriptor flag
(bit 3) and the CRC is written after each member's data, then again in the
central directory at the end. ZIP64 structures are used per entry when a size
or offset needs them, and always for the end record when any entry does."""
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime

ZIP64_LIMIT = 0xFFFFFFFF
LFH_SIG, DD_SIG, CD_SIG, EOCD_SIG = 0x04034B50, 0x08074B50, 0x02014B50, 0x06054B50
Z64_EOCD_SIG, Z64_LOC_SIG = 0x06064B50, 0x07064B50
FLAGS = 0x0808  # data descriptor + UTF-8 names


def dos_datetime(dt: datetime) -> tuple[int, int]:
    dt = max(dt, datetime(1980, 1, 1))
    return ((dt.hour << 11) | (dt.minute << 5) | (dt.second // 2),
            ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day)


@dataclass
class Entry:
    index: int
    path: str
    size: int
    lfh_offset: int
    data_offset: int
    dd_offset: int
    end: int
    zip64: bool

    @property
    def name(self) -> bytes:
        return self.path.encode("utf-8")


@dataclass
class Layout:
    entries: list[Entry]
    cd_offset: int
    cd_size: int
    total: int
    zip64: bool
    dos_time: int
    dos_date: int


def _unique(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for p in paths:
        base = p
        stem, dot, ext = base.rpartition(".")
        if not dot or "/" in ext:
            stem, ext = base, ""
        cand, n = base, 1
        while cand in seen:
            n += 1
            cand = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        seen.add(cand)
        out.append(cand)
    return out


def plan(members: list[dict], when: datetime, force_zip64: bool = False) -> Layout:
    """members: [{"path": str, "size": int}] in archive order."""
    paths = _unique([m["path"] for m in members])
    entries: list[Entry] = []
    pos = 0
    any64 = force_zip64
    for i, (m, path) in enumerate(zip(members, paths)):
        size = int(m["size"])
        name_len = len(path.encode("utf-8"))
        z64 = force_zip64 or size >= ZIP64_LIMIT or pos >= ZIP64_LIMIT
        any64 = any64 or z64
        lfh_len = 30 + name_len + (20 if z64 else 0)
        dd_len = 24 if z64 else 16
        e = Entry(i, path, size, pos, pos + lfh_len, pos + lfh_len + size, pos + lfh_len + size + dd_len, z64)
        entries.append(e)
        pos = e.end
    cd_offset = pos
    cd_size = sum(46 + len(e.name) + (28 if e.zip64 else 0) for e in entries)
    end_len = 22 + ((56 + 20) if (any64 or cd_offset >= ZIP64_LIMIT or len(entries) >= 0xFFFF) else 0)
    t, d = dos_datetime(when)
    return Layout(entries, cd_offset, cd_size, cd_offset + cd_size + end_len,
                  any64 or cd_offset >= ZIP64_LIMIT or len(entries) >= 0xFFFF, t, d)


def local_header(layout: Layout, e: Entry) -> bytes:
    version = 45 if e.zip64 else 20
    hdr = struct.pack("<IHHHHHIIIHH", LFH_SIG, version, FLAGS, 0, layout.dos_time, layout.dos_date,
                      0, 0, 0, len(e.name), 20 if e.zip64 else 0)
    extra = struct.pack("<HHQQ", 0x0001, 16, 0, 0) if e.zip64 else b""
    return hdr + e.name + extra


def data_descriptor(e: Entry, crc: int) -> bytes:
    if e.zip64:
        return struct.pack("<IIQQ", DD_SIG, crc & 0xFFFFFFFF, e.size, e.size)
    return struct.pack("<IIII", DD_SIG, crc & 0xFFFFFFFF, e.size, e.size)


def central_directory(layout: Layout, crcs: list[int]) -> bytes:
    out = bytearray()
    for e, crc in zip(layout.entries, crcs):
        version = 45 if e.zip64 else 20
        size_field = ZIP64_LIMIT if e.zip64 else e.size
        off_field = ZIP64_LIMIT if e.zip64 else e.lfh_offset
        extra = struct.pack("<HHQQQ", 0x0001, 24, e.size, e.size, e.lfh_offset) if e.zip64 else b""
        out += struct.pack("<IHHHHHHIIIHHHHHII", CD_SIG, (3 << 8) | version, version, FLAGS, 0,
                           layout.dos_time, layout.dos_date, crc & 0xFFFFFFFF, size_field, size_field,
                           len(e.name), len(extra), 0, 0, 0, 0o100644 << 16, off_field)
        out += e.name + extra
    n = len(layout.entries)
    if layout.zip64:
        z64_eocd_offset = layout.cd_offset + layout.cd_size
        out += struct.pack("<IQHHIIQQQQ", Z64_EOCD_SIG, 44, (3 << 8) | 45, 45, 0, 0, n, n,
                           layout.cd_size, layout.cd_offset)
        out += struct.pack("<IIQI", Z64_LOC_SIG, 0, z64_eocd_offset, 1)
        out += struct.pack("<IHHHHIIH", EOCD_SIG, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF),
                           min(layout.cd_size, ZIP64_LIMIT), ZIP64_LIMIT, 0)
    else:
        out += struct.pack("<IHHHHIIH", EOCD_SIG, 0, 0, n, n, layout.cd_size, layout.cd_offset, 0)
    assert len(out) == layout.total - layout.cd_offset, (len(out), layout.total - layout.cd_offset)
    return bytes(out)


def crc_update(crc: int, data: bytes) -> int:
    return zlib.crc32(data, crc) & 0xFFFFFFFF
