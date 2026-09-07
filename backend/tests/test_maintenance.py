import os
from datetime import datetime, timedelta

from sqlalchemy import select

from app import config, db as _db, maintenance
from app.models import Bundle, File, Upload
from app.transfers import worker as transfer_worker
from tests.test_transfers import CHUNK


async def _age(model, ident, hours):
    async with _db.async_session() as db:
        row = await db.get(model, ident)
        ts = datetime.utcnow() - timedelta(hours=hours)
        if hasattr(row, "updated_at"):
            row.updated_at = ts
        row.created_at = ts
        await db.commit()


async def test_cleanup_removes_stale_uploads_and_orphans(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1, "stale_upload_hours": 24})
    data = os.urandom(1024 * 1024 + 10)
    # Stale plain upload with one part already in the channel.
    stale = (await api.post("/api/uploads", json={"name": "stale.bin", "size": len(data)})).json()
    for i in range(1024 * 1024 // CHUNK):
        await api.put(f"/api/uploads/{stale['id']}/chunk", content=data[i * CHUNK:(i + 1) * CHUNK], headers={"X-Chunk-Offset": str(i * CHUNK)})
    assert await transfer_worker.worker.process_one()
    assert len(fake_manager.messages) == 1
    await _age(Upload, stale["id"], 48)
    # Fresh upload must survive.
    fresh = (await api.post("/api/uploads", json={"name": "fresh.bin", "size": 10})).json()
    # Stale bundle with no members.
    b = (await api.post("/api/bundles", json={"name": "old", "members": [{"path": "a", "size": 5}]})).json()
    await _age(Bundle, b["id"], 48)
    # Orphaned staging files: one old, one new.
    old = config.STAGING_DIR / "orphan-old.p000"; old.write_bytes(b"x" * 100); os.utime(old, (1, 1))
    new = config.STAGING_DIR / "orphan-new.p000"; new.write_bytes(b"y" * 100)

    r = await api.post("/api/admin/maintenance/cleanup")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["stale_uploads"] == 1 and s["stale_bundles"] == 1 and s["orphan_files_removed"] == 1 and s["orphan_bytes_freed"] == 100
    assert fake_manager.deleted == [100]
    assert (await api.get(f"/api/files/{stale['file_id']}")).status_code == 404
    assert (await api.get(f"/api/files/{b['file_id']}")).status_code == 404
    assert (await api.get(f"/api/uploads/{fresh['id']}")).status_code == 200
    assert not old.exists() and new.exists()
    assert not list(config.STAGING_DIR.glob(f"{stale['file_id']}.p*"))


async def test_cleanup_disabled_when_zero(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"stale_upload_hours": 0})
    up = (await api.post("/api/uploads", json={"name": "keep.bin", "size": 10})).json()
    await _age(Upload, up["id"], 24 * 30)
    s = (await api.post("/api/admin/maintenance/cleanup")).json()
    assert s["stale_uploads"] == 0
    assert (await api.get(f"/api/uploads/{up['id']}")).status_code == 200


async def test_status_snapshot(api, admin, fake_manager):
    r = await api.get("/api/admin/status")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["version"] and "telegram" in s and "worker" in s and "staging" in s
    assert s["staging"]["free_bytes"] > 0 and s["uploads"]["stale_after_hours"] == 72
    assert isinstance(s["recent_errors"], list) and s["worker"]["files_by_status"]["ready"] == 0
