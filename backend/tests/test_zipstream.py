import io
import zipfile
from datetime import datetime

import pytest

from app.transfers import zipstream as zs


def build(members: dict[str, bytes], force_zip64=False) -> bytes:
    layout = zs.plan([{"path": p, "size": len(b)} for p, b in members.items()], datetime(2024, 6, 1, 12, 30, 10), force_zip64)
    out = bytearray()
    crcs = []
    for e, data in zip(layout.entries, members.values()):
        assert len(out) == e.lfh_offset
        out += zs.local_header(layout, e)
        assert len(out) == e.data_offset
        out += data
        crc = zs.crc_update(0, data)
        crcs.append(crc)
        assert len(out) == e.dd_offset
        out += zs.data_descriptor(e, crc)
        assert len(out) == e.end
    assert len(out) == layout.cd_offset
    out += zs.central_directory(layout, crcs)
    assert len(out) == layout.total
    return bytes(out)


@pytest.mark.parametrize("force_zip64", [False, True])
def test_archive_is_valid_and_sizes_are_exact(force_zip64):
    members = {"docs/readme.txt": b"hello world\n", "img/a.bin": bytes(range(256)) * 300, "empty.txt": b"",
               "dup.txt": b"1", "dup.txt ": b"2"}
    blob = build(members, force_zip64)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == list(members.keys())
        for name, data in members.items():
            assert zf.read(name) == data
        info = zf.getinfo("docs/readme.txt")
        assert info.date_time == (2024, 6, 1, 12, 30, 10)


def test_duplicate_names_get_suffixes():
    layout = zs.plan([{"path": "a.txt", "size": 1}, {"path": "a.txt", "size": 1}, {"path": "b", "size": 0}, {"path": "b", "size": 0}], datetime(2024, 1, 1))
    assert [e.path for e in layout.entries] == ["a.txt", "a (2).txt", "b", "b (2)"]


def test_zip64_layout_math_for_huge_entries():
    big = 5 * 1024 ** 3
    layout = zs.plan([{"path": "x.iso", "size": big}, {"path": "y.bin", "size": 10}], datetime(2024, 1, 1))
    x, y = layout.entries
    assert x.zip64 and y.zip64  # y's offset is past 4 GiB
    assert x.data_offset == 30 + 5 + 20 and x.dd_offset == x.data_offset + big and x.end == x.dd_offset + 24
    assert layout.total == layout.cd_offset + layout.cd_size + 22 + 56 + 20
