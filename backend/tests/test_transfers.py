import hashlib
import io
import os
import zipfile

from app import config
from app.transfers import worker as transfer_worker


CHUNK = config.UPLOAD_CHUNK_SIZE


async def _upload(api, name, data: bytes, chunk=None, folder_id=None, bundle_id=None, drain=True):
    """Chunked upload like the browser does it. On backpressure (staging full)
    the worker is run, mirroring what happens in production concurrently."""
    chunk = CHUNK
    r = await api.post("/api/uploads", json={"name": name, "size": len(data), "folder_id": folder_id,
                                             "bundle_id": bundle_id})
    assert r.status_code == 200, r.text
    up = r.json()
    off = 0
    while off < len(data):
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[off:off + chunk],
                          headers={"X-Chunk-Offset": str(off), "Content-Type": "application/octet-stream"})
        if r.status_code == 429 and drain:
            assert r.json()["detail"]["code"] == "backpressure"
            assert await transfer_worker.worker.process_one()
            continue
        assert r.status_code == 200, r.text
        off += chunk
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
    res = await _upload(api, "big.bin", data)
    f = res["file"]
    assert f["status"] in ("queued", "ready") and f["size"] == len(data)
    assert f["sha256"] == hashlib.sha256(data).hexdigest()

    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{f['id']}")).json()
    assert f["status"] == "ready", f
    assert f["parts_total"] == 3 and f["parts_uploaded"] == 3
    assert not list(config.STAGING_DIR.glob("*.p0*")), "part staging must be cleaned up"
    assert fake_manager.stored_bytes() == data
    names = [v[0] for _k, v in sorted(fake_manager.messages.items())]
    assert names == ["big.bin.001", "big.bin.002", "big.bin.003"]
    cap = list(fake_manager.messages.values())[0][2]
    assert cap["ots"] == 1 and cap["of"] == 3 and cap["name"] == "big.bin"
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


async def test_parallel_chunks_any_order_idempotent(api, admin, fake_manager):
    """Chunks may arrive in any order and be repeated; hashing follows the
    contiguous prefix so the digests are still exact."""
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(3 * CHUNK + 123)  # 4 chunks
    up = (await api.post("/api/uploads", json={"name": "p.bin", "size": len(data)})).json()
    assert up["parallel"] and up["total_chunks"] == 4 and up["missing_chunks"] == [0, 1, 2, 3]
    send = lambda i: api.put(f"/api/uploads/{up['id']}/chunk", content=data[i * CHUNK:(i + 1) * CHUNK], headers={"X-Chunk-Offset": str(i * CHUNK)})
    r = await send(2); assert r.status_code == 200 and r.json()["missing_chunks"] == [0, 1, 3]
    r = await send(3); assert r.status_code == 200
    r = await api.post(f"/api/uploads/{up['id']}/complete"); assert r.status_code == 400
    r = await send(0); assert r.status_code == 200 and r.json()["received"] == 3 * CHUNK
    r = await send(0); assert r.status_code == 200  # duplicate is fine
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"x", headers={"X-Chunk-Offset": "7"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_offset"
    r = await api.put(f"/api/uploads/{up['id']}/chunk", content=b"x", headers={"X-Chunk-Offset": str(CHUNK)})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "short_chunk"
    r = await send(1); assert r.status_code == 200 and r.json()["missing_chunks"] == [] and r.json()["received"] == len(data)
    # The resume listing shows it until completed.
    assert [u["id"] for u in (await api.get("/api/uploads")).json()] == [up["id"]]
    r = await api.post(f"/api/uploads/{up['id']}/complete", json={"sha256": hashlib.sha256(data).hexdigest()})
    assert r.status_code == 200, r.text
    assert (await api.get("/api/uploads")).json() == []
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{up['file_id']}")).json()
    assert f["status"] == "ready" and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert fake_manager.stored_bytes() == data


async def test_parallel_chunks_concurrently(api, admin, fake_manager):
    """Real concurrency: all chunks in flight at once, like the browser does."""
    import asyncio
    data = os.urandom(9 * CHUNK + 1)
    up = (await api.post("/api/uploads", json={"name": "c.bin", "size": len(data)})).json()
    rs = await asyncio.gather(*[api.put(f"/api/uploads/{up['id']}/chunk", content=data[i * CHUNK:(i + 1) * CHUNK],
                                        headers={"X-Chunk-Offset": str(i * CHUNK)}) for i in range(10)])
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200, r.text
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{up['file_id']}")).json()
    assert f["status"] == "ready" and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert fake_manager.stored_bytes() == data


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


async def _upload_member(api, bundle, index, path, data, chunk=5000):
    r = await api.post("/api/uploads", json={"name": path.split("/")[-1], "size": len(data), "bundle_id": bundle["id"],
                                             "path": path, "member_index": index})
    assert r.status_code == 200, r.text
    up = r.json()
    off = 0
    while off < len(data):
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[off:off + chunk], headers={"X-Chunk-Offset": str(off)})
        if r.status_code == 429:
            assert await transfer_worker.worker.process_one()
            continue
        assert r.status_code == 200, r.text
        off = r.json()["received"]
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200, r.text
    return up


async def test_bundle_zip(api, admin, fake_manager):
    members = {"1.txt": b"one", "2.txt": b"two" * 10, "empty.bin": b""}
    r = await api.post("/api/bundles", json={"name": "photos", "members": [{"path": p, "size": len(d)} for p, d in members.items()]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == "photos.zip" and b["size"] > sum(len(d) for d in members.values())
    assert (await api.get(f"/api/files/{b['file_id']}")).json()["status"] == "receiving"
    for i, (p, d) in enumerate(members.items()):
        await _upload_member(api, b, i, p, d)
    r = await api.post(f"/api/bundles/{b['id']}/complete")
    assert r.status_code == 200, r.text
    f = r.json()["file"]
    assert f["is_archive"] and f["name"] == "photos.zip" and f["size"] == b["size"]
    await _drain(transfer_worker.worker)
    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 200 and len(r.content) == b["size"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == list(members)
        assert zf.read("2.txt") == b"two" * 10
    assert not list(config.STAGING_DIR.glob(f"{f['id']}.p*"))


async def test_bundle_streams_into_parts_with_backpressure(api, admin, fake_manager):
    """A multi-part archive is sent to Telegram while members are still arriving."""
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    members = {f"vid/{i}.bin": os.urandom(900_000) for i in range(6)}  # ~5.4 MB -> 6 parts
    b = (await api.post("/api/bundles", json={"name": "trip", "members": [{"path": p, "size": len(d)} for p, d in members.items()]})).json()
    for i, (p, d) in enumerate(members.items()):
        await _upload_member(api, b, i, p, d, chunk=300_000)
    mid = (await api.get(f"/api/files/{b['file_id']}")).json()
    assert mid["status"] == "receiving" and mid["parts_uploaded"] >= 2
    # Out-of-order / duplicate member is refused, resume of the same member is allowed.
    r = await api.post("/api/uploads", json={"name": "x", "size": 1, "bundle_id": b["id"], "path": "x", "member_index": 0})
    assert r.status_code == 409
    f = (await api.post(f"/api/bundles/{b['id']}/complete")).json()["file"]
    await _drain(transfer_worker.worker)
    r = await api.get(f"/api/files/{f['id']}/download")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        for p, d in members.items():
            assert zf.read(p) == d
    assert f["sha256"] == hashlib.sha256(r.content).hexdigest()


async def test_cancel_bundle_removes_everything(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    b = (await api.post("/api/bundles", json={"name": "c", "members": [{"path": "a.bin", "size": 1_500_000}]})).json()
    r = await api.post("/api/uploads", json={"name": "a.bin", "size": 1_500_000, "bundle_id": b["id"], "path": "a.bin", "member_index": 0})
    up = r.json()
    await api.put(f"/api/uploads/{up['id']}/chunk", content=os.urandom(1_200_000), headers={"X-Chunk-Offset": "0"})
    assert await transfer_worker.worker.process_one()
    assert len(fake_manager.messages) == 1
    assert (await api.delete(f"/api/bundles/{b['id']}")).status_code == 200
    assert fake_manager.deleted == [100]
    assert (await api.get(f"/api/files/{b['file_id']}")).status_code == 404
    assert not list(config.STAGING_DIR.glob(f"{b['file_id']}.p*"))


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
    members = [{"path": "trip/2024/a.jpg", "size": 3}, {"path": "../../etc/x.txt", "size": 2}]
    b = (await api.post("/api/bundles", json={"name": "trip", "members": members})).json()
    # Path traversal attempts are neutralised at planning time.
    assert [m["path"] for m in b["members"]] == ["trip/2024/a.jpg", "etc/x.txt"]
    await _upload_member(api, b, 0, "trip/2024/a.jpg", b"abc")
    await _upload_member(api, b, 1, "etc/x.txt", b"hi")
    f = (await api.post(f"/api/bundles/{b['id']}/complete")).json()["file"]
    await _drain(transfer_worker.worker)
    r = await api.get(f"/api/files/{f['id']}/download")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["etc/x.txt", "trip/2024/a.jpg"]


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


async def test_move_files_and_folders(api, admin, fake_manager):
    a = (await api.post("/api/folders", json={"name": "A"})).json()
    b = (await api.post("/api/folders", json={"name": "B"})).json()
    a1 = (await api.post("/api/folders", json={"name": "A1", "parent_id": a["id"]})).json()
    f = (await _upload(api, "x.txt", b"x"))["file"]
    # bulk: file + folder B into A
    r = await api.post("/api/move", json={"file_ids": [f["id"]], "folder_ids": [b["id"]], "target_folder_id": a["id"]})
    assert r.status_code == 200 and r.json()["moved"] == 2
    inside = (await api.get("/api/files", params={"folder_id": a["id"]})).json()
    assert sorted(d["name"] for d in inside["folders"]) == ["A1", "B"]
    assert [x["name"] for x in inside["files"]] == ["x.txt"]
    # cycle: A into its own child A1
    r = await api.post(f"/api/folders/{a['id']}/move", json={"folder_id": a1["id"]})
    assert r.status_code == 400
    # name clash: another "B" at top level, then move A/B up
    await api.post("/api/folders", json={"name": "B"})
    r = await api.post(f"/api/folders/{b['id']}/move", json={"folder_id": None})
    assert r.status_code == 409
    # move file back to top level; tree lists depth
    r = await api.post("/api/move", json={"file_ids": [f["id"]], "target_folder_id": None})
    assert r.status_code == 200
    tree = (await api.get("/api/folders/tree")).json()
    assert [(t["name"], t["depth"]) for t in tree] == [("A", 0), ("A1", 1), ("B", 1), ("B", 0)]


async def test_streaming_overlap_and_backpressure(api, admin, fake_manager):
    """Parts go to Telegram while the browser is still sending, staging never
    holds more than MAX_STAGED_PARTS waiting parts, and per-part hashes are
    computed incrementally."""
    from app.transfers.staging import MAX_STAGED_PARTS
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(6 * 1024 * 1024 + 5)  # 7 parts
    r = await api.post("/api/uploads", json={"name": "stream.bin", "size": len(data)})
    up = r.json()
    fid = up["file_id"]
    assert (await api.get(f"/api/files/{fid}")).json()["status"] == "receiving"
    off, chunk, hit_backpressure = 0, CHUNK, False
    while off < len(data):
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[off:off + chunk],
                          headers={"X-Chunk-Offset": str(off)})
        if r.status_code == 429:
            hit_backpressure = True
            waiting = len([p for p in list(config.STAGING_DIR.glob(f"{fid}.p*"))])
            assert waiting <= MAX_STAGED_PARTS + 1
            assert await transfer_worker.worker.process_one()  # a part leaves for Telegram
            continue
        assert r.status_code == 200, r.text
        off += chunk
    assert hit_backpressure
    # Some parts are already in the channel before the browser finished.
    assert len(fake_manager.messages) >= MAX_STAGED_PARTS
    mid = (await api.get(f"/api/files/{fid}")).json()
    assert mid["status"] == "receiving" and mid["parts_uploaded"] >= MAX_STAGED_PARTS
    r = await api.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{fid}")).json()
    assert f["status"] == "ready" and f["parts_uploaded"] == 7
    assert f["sha256"] == hashlib.sha256(data).hexdigest()
    assert fake_manager.stored_bytes() == data
    caps = [v[2] for _k, v in sorted(fake_manager.messages.items())]
    assert [c["sha256"] for c in caps] == [hashlib.sha256(data[i * 1048576:(i + 1) * 1048576]).hexdigest() for i in range(7)]
    assert not list(config.STAGING_DIR.glob(f"{fid}.p*"))
    r = await api.get(f"/api/files/{fid}/download")
    assert r.content == data


async def test_cancel_receiving_upload_removes_channel_parts(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(2 * 1024 * 1024)
    up = (await api.post("/api/uploads", json={"name": "c.bin", "size": len(data)})).json()
    for i in range(1024 * 1024 // CHUNK):
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[i * CHUNK:(i + 1) * CHUNK], headers={"X-Chunk-Offset": str(i * CHUNK)})
        assert r.status_code == 200
    assert await transfer_worker.worker.process_one()
    assert len(fake_manager.messages) == 1
    r = await api.delete(f"/api/uploads/{up['id']}")
    assert r.status_code == 200
    assert fake_manager.deleted == [100]
    assert (await api.get(f"/api/files/{up['file_id']}")).status_code == 404
    assert not list(config.STAGING_DIR.glob(f"{up['file_id']}.p*"))
