import hashlib
import io
import zipfile

import pytest

from app.transfers.io import RangeReader, build_archive, hash_ranges, part_name, plan_parts


def test_plan_parts():
    assert plan_parts(0, 10) == [(0, 0, 0)]
    assert plan_parts(10, 10) == [(0, 0, 10)]
    assert plan_parts(25, 10) == [(0, 0, 10), (1, 10, 10), (2, 20, 5)]
    with pytest.raises(ValueError):
        plan_parts(5, 0)


def test_part_name():
    assert part_name("a.bin", 0, 1) == "a.bin"
    assert part_name("a.bin", 0, 3) == "a.bin.001"
    assert part_name("a.bin", 11, 12) == "a.bin.012"


def test_range_reader_and_hashes(tmp_path):
    data = bytes(range(256)) * 40  # 10240 bytes
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    plan = plan_parts(len(data), 4000)
    whole, per = hash_ranges(p, plan)
    assert whole == hashlib.sha256(data).hexdigest()
    assert per == [hashlib.sha256(data[o:o + n]).hexdigest() for _i, o, n in plan]
    with RangeReader(p, 4000, 4000, name="f.bin.002") as r:
        assert r.tell() == 0
        assert r.seek(0, 2) == 4000
        r.seek(0)
        chunk = r.read(1000)
        assert chunk == data[4000:5000]
        assert r.read() == data[5000:8000]
        assert r.read() == b""


def test_build_archive(tmp_path):
    a = tmp_path / "a.txt"; a.write_bytes(b"hello")
    b = tmp_path / "b.txt"; b.write_bytes(b"world" * 100)
    out = tmp_path / "out.zip"
    size = build_archive(out, [("a.txt", str(a)), ("a.txt", str(b))], compress=False)
    assert size == out.stat().st_size
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as zf:
        assert zf.namelist() == ["a.txt", "a (2).txt"]
        assert zf.read("a (2).txt") == b"world" * 100
