import io
import os
import zipfile

from app import config
from app.transfers import worker as transfer_worker


async def _upload(api, name, data: bytes, chunk=5000, folder_id=None, bundle_id=None):
    r = await api.post("/api/uploads", json={"name": name, "size": len(data), "folder_id": folder_id,
                                             "bundle_id": bundle_id})
    assert r.status_code == 200, r.text
    up = r.json()
    for off in range(0, len(data), chunk) or [0]:
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[off:off + chunk],
                          headers={"X-Chunk-Offset": str(off), "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200, r.text
    return r.json()


async def _drain(worker, max_iter=20):
    for _ in range(max_iter):
        if not await worker.process_one():
            break


async def test_upload_split_download_delete(api, admin, fake_manager):
    r = await api.put("/api/admin/settings", json={"part_size_mb": 1})
    assert r.status_code == 200 and r.json()["part_size_mb"] == 1
    data = os.urandom(2 * 1024 * 1024 + 12345)  # 3 parts at 1 MiB
    res = await _upload(api, "big.bin", data, chunk=700_000)
    f = res["file"]
    assert f["status"] == "queued" and f["size"] == len(data)

    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{f['id']}")).json()
    assert f["status"] == "ready", f
    assert f["parts_total"] == 3 and f["parts_uploaded"] == 3
    assert fake_manager.stored_bytes() == data
    names = [v[0] for _k, v in sorted(fake_manager.messages.items())]
    assert names == ["big.bin.001", "big.bin.002", "big.bin.003"]
    cap = list(fake_manager.messages.values())[0][2]
    assert cap["otg"] == 1 and cap["of"] == 3 and cap["name"] == "big.bin"
    assert not list(config.STAGING_DIR.glob("*.part")), "staging must be cleaned up"

    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-length"] == str(len(data))

    # Byte range spanning the boundary between part 1 and part 2.
    r = await api.get(f"/api/files/{f['id']}/download", headers={"Range": "bytes=1048000-1049000"})
    assert r.status_code == 206
    assert r.content == data[1048000:1049001]
    assert r.headers["content-range"] == f"bytes 1048000-1049000/{len(data)}"
    r = await api.get(f"/api/files/{f['id']}/download", headers={"Range": "bytes=-10"})
    assert r.content == data[-10:]

    r = await api.delete(f"/api/files/{f['id']}")
    assert r.status_code == 200
    assert sorted(fake_manager.deleted) == [100, 101, 102]
    assert (await api.get(f"/api/files/{f['id']}")).status_code == 404


async def test_chunk_offset_mismatch_and_resume(api, admin):
    r = await api.post("/api/uploads", json={"name": "x.bin", "size": 10})
    up = r.json()
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"12345", headers={"X-Chunk-Offset": "0"})
    assert r.json()["received"] == 5
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"12345", headers={"X-Chunk-Offset": "0"})
    assert r.status_code == 409 and r.json()["detail"]["received"] == 5
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 400
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"678901", headers={"X-Chunk-Offset": "5"})
    assert r.status_code == 400  # exceeds declared size
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"67890", headers={"X-Chunk-Offset": "5"})
    assert r.status_code == 200
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200 and r.json()["file"]["status"] == "queued"


async def test_retry_after_failures(api, admin, fake_manager):
    fake_manager.fail_next = 5
    await api.put("/api/admin/settings", json={"max_retries": 2})
    res = await _upload(api, "flaky.bin", b"abc" * 1000)
    fid = res["file"]["id"]
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{fid}")).json()
    assert f["status"] == "failed" and "simulated" in f["error"]
    fake_manager.fail_next = 0
    r = await api.post(f"/api/files/{fid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    await _drain(transfer_worker.worker)
    assert (await api.get(f"/api/files/{fid}")).json()["status"] == "ready"


async def test_bundle_zip(api, admin, fake_manager):
    r = await api.post("/api/bundles", json={"name": "photos"})
    b = r.json()
    assert b["name"] == "photos.zip"
    await _upload(api, "1.txt", b"one", bundle_id=b["id"])
    await _upload(api, "2.txt", b"two" * 10, bundle_id=b["id"])
    r = await api.post(f"/api/bundles/{b['id']}/complete")
    assert r.status_code == 200, r.text
    f = r.json()["file"]
    assert f["is_archive"] and f["name"] == "photos.zip"
    await _drain(transfer_worker.worker)
    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["1.txt", "2.txt"]
        assert zf.read("2.txt") == b"two" * 10


async def test_folders_and_isolation(api, admin, fake_manager):
    r = await api.post("/api/folders", json={"name": "docs"})
    folder = r.json()
    res = await _upload(api, "note.txt", b"hi", folder_id=folder["id"])
    listing = (await api.get("/api/files", params={"folder_id": folder["id"]})).json()
    assert [f["name"] for f in listing["files"]] == ["note.txt"]
    assert listing["breadcrumbs"][0]["name"] == "docs"
    root = (await api.get("/api/files")).json()
    assert root["files"] == [] and root["folders"][0]["name"] == "docs"

    # Another user cannot see or touch it.
    await api.post("/api/admin/users", json={"username": "eve", "password": "eves long password"})
    await api.post("/api/auth/logout")
    await api.post("/api/auth/login", json={"username": "eve", "password": "eves long password"})
    assert (await api.get(f"/api/files/{res['file']['id']}")).status_code == 404
    assert (await api.delete(f"/api/folders/{folder['id']}")).status_code == 404
    assert (await api.get("/api/files")).json()["folders"] == []


async def test_bundle_keeps_folder_structure(api, admin, fake_manager):
    b = (await api.post("/api/bundles", json={"name": "trip"})).json()
    r = await api.post("/api/uploads", json={"name": "a.jpg", "size": 3, "bundle_id": b["id"], "path": "trip/2024/a.jpg"})
    up1 = r.json()
    await api.put(f"/api/uploads/{up1['id']}/chunk", content=b"abc", headers={"X-Chunk-Offset": "0"})
    await api.post(f"/api/uploads/{up1['id']}/complete")
    # Path traversal attempts are neutralised, and the file name always wins.
    r = await api.post("/api/uploads", json={"name": "b.txt", "size": 2, "bundle_id": b["id"], "path": "../../etc/x.txt"})
    up2 = r.json()
    await api.put(f"/api/uploads/{up2['id']}/chunk", content=b"hi", headers={"X-Chunk-Offset": "0"})
    await api.post(f"/api/uploads/{up2['id']}/complete")
    f = (await api.post(f"/api/bundles/{b['id']}/complete")).json()["file"]
    await _drain(transfer_worker.worker)
    r = await api.get(f"/api/files/{f['id']}/download")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["etc/b.txt", "trip/2024/a.jpg"]


async def test_folder_ensure_is_idempotent_and_scoped(api, admin):
    leaf = (await api.post("/api/folders/ensure", json={"path": "photos/2024/june"})).json()
    again = (await api.post("/api/folders/ensure", json={"path": "photos/2024/june"})).json()
    assert leaf["id"] == again["id"]
    root = (await api.get("/api/files")).json()
    assert [d["name"] for d in root["folders"]] == ["photos"]
    sub = (await api.post("/api/folders/ensure", json={"path": "raw", "parent_id": leaf["id"]})).json()
    assert sub["parent_id"] == leaf["id"]
    r = await api.post("/api/folders/ensure", json={"path": "../../x"})
    assert r.status_code == 200 and r.json()["name"] == "x"
