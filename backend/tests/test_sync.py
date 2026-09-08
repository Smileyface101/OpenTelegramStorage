"""Shared workspace: what this server does with posts made by another server
on the same channel. The 'other server' is simulated by writing captions and
events straight into the fake channel and driving the sync entry points."""
import hashlib
import json
import os

from app import sync
from app.transfers import worker as transfer_worker
from tests.conftest import stored_plaintext
from tests.test_transfers import _drain, _upload


def _other_server_uploads(fm, name, data, part_size, by="laptop/alice", path=None, fid=None):
    """Emulate another server posting a file's parts (unencrypted) with captions."""
    import uuid
    fid = fid or uuid.uuid4().hex
    parts = [data[i:i + part_size] for i in range(0, len(data), part_size)] or [b""]
    ids = []
    for i, blob in enumerate(parts):
        cap = {"ots": 1, "id": fid, "name": name, "size": len(data), "part": i + 1, "of": len(parts),
               "psize": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "archive": False, "path": path, "by": by}
        mid = fm._next; fm._next += 1
        fm.messages[mid] = (name if len(parts) == 1 else f"{name}.{i+1:03d}", blob, cap)
        ids.append((mid, cap, len(blob)))
    return fid, ids


async def test_private_mode_ignores_other_servers(api, admin, fake_manager):
    fid, ids = _other_server_uploads(fake_manager, "foreign.bin", b"z" * 100, 50)
    for mid, cap, size in ids:
        await sync.handle_post(mid, json.dumps(cap), size)
    assert (await api.get("/api/files")).json()["files"] == []


async def test_live_posts_index_files_from_other_servers(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    data = os.urandom(250_000)
    fid, ids = _other_server_uploads(fake_manager, "movie.mkv", data, 100_000, path="Videos/2026")
    # First part arrives: file shows as syncing.
    mid, cap, size = ids[0]
    await sync.handle_post(mid, json.dumps(cap), size)
    f = (await api.get(f"/api/files/{fid}")).json()
    assert f["status"] == "syncing" and f["uploaded_by"] == "laptop/alice" and f["parts_total"] == 3
    for mid, cap, size in ids[1:]:
        await sync.handle_post(mid, json.dumps(cap), size)
    f = (await api.get(f"/api/files/{fid}")).json()
    assert f["status"] == "ready" and f["parts_uploaded"] == 3
    root = (await api.get("/api/files")).json()
    assert [d["name"] for d in root["folders"]] == ["Videos"]
    # It downloads, byte for byte, through this server.
    r = await api.get(f"/api/files/{fid}/download")
    assert r.status_code == 200 and r.content == data
    # Duplicate delivery is harmless.
    await sync.handle_post(ids[0][0], json.dumps(ids[0][1]), ids[0][2])
    assert (await api.get(f"/api/files/{fid}")).json()["parts_uploaded"] == 3


async def test_own_uploads_are_not_duplicated_and_carry_labels(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    f = (await _upload(api, "mine.bin", b"m" * 5000))["file"]
    await _drain(transfer_worker.worker)
    mid = max(fake_manager.messages)
    name, blob, cap = fake_manager.messages[mid]
    assert cap["by"] == "home/admin"
    # The channel echoes our own post back to us.
    await sync.handle_post(mid, json.dumps(cap), len(blob))
    files = (await api.get("/api/files")).json()["files"]
    assert len(files) == 1 and files[0]["uploaded_by"] == "home/admin" and files[0]["status"] == "ready"


async def test_delete_event_and_reconcile(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    data = b"q" * 3000
    fid, ids = _other_server_uploads(fake_manager, "gone.bin", data, 1000)
    for mid, cap, size in ids:
        await sync.handle_post(mid, json.dumps(cap), size)
    assert (await api.get(f"/api/files/{fid}")).status_code == 200
    # Other server deleted it and posted an event.
    ev_id = await fake_manager.send_text(sync.make_event("delete", id=fid, by="laptop/alice"))
    await sync.handle_post(ev_id, fake_manager.messages[ev_id][2], None)
    assert (await api.get(f"/api/files/{fid}")).status_code == 404
    # Reconcile: messages vanished without an event (deleted by hand in Telegram).
    fid2, ids2 = _other_server_uploads(fake_manager, "vanish.bin", b"v" * 2000, 1000)
    for mid, cap, size in ids2:
        await sync.handle_post(mid, json.dumps(cap), size)
    for mid, _c, _s in ids2:
        fake_manager.messages.pop(mid)
    res = await sync.reconcile(fake_manager)
    assert res["removed"] == 1
    assert (await api.get(f"/api/files/{fid2}")).status_code == 404
    # Our own delete announces an event for the others.
    mine = (await _upload(api, "mine.bin", b"m" * 100))["file"]
    await _drain(transfer_worker.worker)
    before = set(fake_manager.messages)
    assert (await api.delete(f"/api/files/{mine['id']}")).status_code == 200
    new = [fake_manager.messages[m][2] for m in set(fake_manager.messages) - before]
    assert any(isinstance(t, str) and json.loads(t)["t"] == "delete" and json.loads(t)["id"] == mine["id"] for t in new)


async def test_catch_up_after_downtime(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    # Posts happened while we were offline: two files and a delete event for one of them.
    fid_a, _ = _other_server_uploads(fake_manager, "a.bin", b"a" * 1500, 1000)
    fid_b, _ = _other_server_uploads(fake_manager, "b.bin", b"b" * 500, 1000)
    await fake_manager.send_text(sync.make_event("delete", id=fid_a, by="laptop/alice"))
    res = await sync.catch_up(fake_manager)
    assert res["parts"] == 3 and res["events"] == 1
    names = sorted(x["name"] for x in (await api.get("/api/files")).json()["files"])
    assert names == ["b.bin"]
    # Second run scans nothing new.
    again = await sync.catch_up(fake_manager)
    assert again["parts"] == 0 and again["events"] == 0 and again["scanned"] <= 1  # only the probe marker id
    r = await api.post("/api/admin/sync/catch-up")
    assert r.status_code == 200


async def test_shared_mode_makes_files_visible_to_all_local_users(api, admin, fake_manager):
    f = (await _upload(api, "team.bin", b"t" * 100))["file"]
    await _drain(transfer_worker.worker)
    await api.post("/api/admin/users", json={"username": "bob", "password": "bobs long password"})
    await api.post("/api/auth/logout")
    await api.post("/api/auth/login", json={"username": "bob", "password": "bobs long password"})
    assert (await api.get(f"/api/files/{f['id']}")).status_code == 404  # private mode: not bob's
    await api.post("/api/auth/logout")
    await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
    await api.put("/api/admin/settings", json={"workspace_mode": "shared"})
    await api.post("/api/auth/logout")
    await api.post("/api/auth/login", json={"username": "bob", "password": "bobs long password"})
    assert (await api.get(f"/api/files/{f['id']}")).status_code == 200
    assert [x["name"] for x in (await api.get("/api/files")).json()["files"]] == ["team.bin"]
