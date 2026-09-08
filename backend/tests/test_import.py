import io
import os
import zipfile

import pytest

from app import config
from app.transfers import worker as transfer_worker
from tests.conftest import stored_plaintext
from tests.test_transfers import _drain


@pytest.fixture
def import_dir(tmp_path, monkeypatch):
    root = tmp_path / "import"
    (root / "videos" / "2024").mkdir(parents=True)
    (root / "videos" / "a.mkv").write_bytes(os.urandom(1_300_000))
    (root / "videos" / "2024" / "b.mkv").write_bytes(b"b" * 5000)
    (root / "notes.txt").write_bytes(b"hello")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    os.symlink(outside, root / "link.txt")
    monkeypatch.setattr(config, "IMPORT_DIR", root)
    return root


async def _wait_not_receiving(api, fid):
    for _ in range(300):
        f = (await api.get(f"/api/files/{fid}")).json()
        if f["status"] != "receiving":
            return f
        await transfer_worker.worker.process_one()
    raise AssertionError("import did not finish")


async def test_browse_and_path_safety(api, admin, import_dir):
    r = await api.get("/api/admin/import/browse")
    assert r.status_code == 200 and r.json()["enabled"]
    names = [e["name"] for e in r.json()["entries"]]
    assert names == ["videos", "link.txt", "notes.txt"]
    r = await api.get("/api/admin/import/browse", params={"path": "../"})
    assert r.status_code == 400
    r = await api.post("/api/admin/import", json={"path": "link.txt"})
    assert r.status_code == 400  # symlink resolves outside the mount


async def test_import_file_reads_in_place(api, admin, fake_manager, import_dir):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    r = await api.post("/api/admin/import", json={"path": "videos/a.mkv"})
    assert r.status_code == 200, r.text
    f = r.json()["files"][0]
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{f['id']}")).json()
    assert f["status"] == "ready" and f["parts_total"] == 2
    assert (import_dir / "videos" / "a.mkv").exists(), "source must never be deleted"
    assert await stored_plaintext(fake_manager) == (import_dir / "videos" / "a.mkv").read_bytes()
    await api.delete(f"/api/files/{f['id']}")
    assert (import_dir / "videos" / "a.mkv").exists()


async def test_import_tree(api, admin, fake_manager, import_dir):
    r = await api.post("/api/admin/import", json={"path": "videos", "mode": "tree"})
    assert r.status_code == 200 and r.json()["count"] == 2
    await _drain(transfer_worker.worker)
    root = (await api.get("/api/files")).json()
    assert [d["name"] for d in root["folders"]] == ["videos"]
    vid = (await api.get("/api/files", params={"folder_id": root["folders"][0]["id"]})).json()
    assert [d["name"] for d in vid["folders"]] == ["2024"] and [x["name"] for x in vid["files"]] == ["a.mkv"]
    assert all(x["status"] == "ready" for x in vid["files"])


async def test_import_zip_streams(api, admin, fake_manager, import_dir):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    r = await api.post("/api/admin/import", json={"path": "videos", "mode": "zip"})
    assert r.status_code == 200, r.text
    f = r.json()["files"][0]
    assert f["is_archive"] and f["name"] == "videos.zip"
    f = await _wait_not_receiving(api, f["id"])
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{f['id']}")).json()
    assert f["status"] == "ready", f
    r = await api.get(f"/api/files/{f['id']}/download")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["videos/2024/b.mkv", "videos/a.mkv"]
        assert zf.read("videos/a.mkv") == (import_dir / "videos" / "a.mkv").read_bytes()
    assert not list(config.STAGING_DIR.glob(f"{f['id']}.p*"))
