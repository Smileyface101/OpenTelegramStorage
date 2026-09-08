import hashlib
import os

from app import config
from app.transfers import worker as transfer_worker
from tests.test_transfers import _drain, _upload


async def _verify(api, fid):
    r = await api.post(f"/api/files/{fid}/verify")
    assert r.status_code == 200, r.text
    for _ in range(200):
        f = (await api.get(f"/api/files/{fid}")).json()
        if not f["verifying"]:
            return f
    raise AssertionError("verification did not finish")


async def test_verify_ok_then_detects_corruption(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(2 * 1024 * 1024 + 99)
    f = (await _upload(api, "v.bin", data))["file"]
    await _drain(transfer_worker.worker)
    f = await _verify(api, f["id"])
    assert f["verified_at"] and f["integrity_error"] is None

    # Corrupt part 2 in the "channel".
    mid = sorted(fake_manager.messages)[1]
    name, blob, cap = fake_manager.messages[mid]
    # Flip a byte well past the 20-byte encryption header so the data itself is damaged.
    fake_manager.messages[mid] = (name, blob[:100] + bytes([blob[100] ^ 0xFF]) + blob[101:], cap)
    f = await _verify(api, f["id"])
    assert "part 2" in f["integrity_error"]  # checksum mismatch (plain) or decryption failed (encrypted)

    # A full download aborts instead of handing over corrupt bytes.
    import httpx
    try:
        r = await api.get(f"/api/files/{f['id']}/download")
        assert len(r.content) < len(data)
    except (httpx.RemoteProtocolError, RuntimeError):
        pass
    # A ranged download inside a part is not verifiable and still works.
    r = await api.get(f"/api/files/{f['id']}/download", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == data[:100]
    assert r.headers["x-checksum-sha256"] == hashlib.sha256(data).hexdigest()


async def test_verify_detects_missing_message(api, admin, fake_manager):
    f = (await _upload(api, "m.bin", b"abc" * 1000))["file"]
    await _drain(transfer_worker.worker)
    fake_manager.messages.clear()
    f = await _verify(api, f["id"])
    assert "missing from the channel" in f["integrity_error"]


async def _chunked(api, up, data, chunk=None):
    chunk = config.UPLOAD_CHUNK_SIZE
    off = 0
    while off < len(data):
        r = await api.put(f"/api/uploads/{up['id']}/chunk", content=data[off:off + chunk], headers={"X-Chunk-Offset": str(off)})
        if r.status_code == 429:
            assert await transfer_worker.worker.process_one()
            continue
        assert r.status_code == 200, r.text
        off += chunk


async def test_client_digests_accepted_and_used_after_restart(api, admin, fake_manager):
    from app.transfers import staging
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(1024 * 1024 + 5)
    up = (await api.post("/api/uploads", json={"name": "h.bin", "size": len(data)})).json()
    assert up["part_size"] == 1024 * 1024
    await _chunked(api, up, data)
    parts = [hashlib.sha256(data[:1024 * 1024]).hexdigest(), hashlib.sha256(data[1024 * 1024:]).hexdigest()]
    whole = hashlib.sha256(data).hexdigest()
    staging.forget(up["file_id"])  # simulate a server restart: in-memory digest gone
    r = await api.post(f"/api/uploads/{up['id']}/complete", json={"sha256": whole, "part_sha256": parts})
    assert r.status_code == 200, r.text
    assert r.json()["file"]["sha256"] == whole  # browser's digest filled the gap
    await _drain(transfer_worker.worker)
    assert (await api.get(f"/api/files/{up['file_id']}")).json()["status"] == "ready"


async def test_client_digest_mismatch_rejects_upload(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    data = os.urandom(1024 * 1024 + 5)
    up = (await api.post("/api/uploads", json={"name": "bad.bin", "size": len(data)})).json()
    await _chunked(api, up, data)
    assert await transfer_worker.worker.process_one()  # part 1 already in the channel
    wrong = ["0" * 64, hashlib.sha256(data[1024 * 1024:]).hexdigest()]
    r = await api.post(f"/api/uploads/{up['id']}/complete", json={"sha256": hashlib.sha256(data).hexdigest(), "part_sha256": wrong})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "checksum_mismatch"
    f = (await api.get(f"/api/files/{up['file_id']}")).json()
    assert f["status"] == "failed" and "part 1 was corrupted" in f["error"]
    assert fake_manager.deleted == [100]  # the bad part was pulled from the channel
    assert not list(config.STAGING_DIR.glob(f"{up['file_id']}.p*"))
