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


def _foreign(kind, **fields):
    """An event as another server would post it (different server id)."""
    ev = json.loads(sync.make_event(kind, **fields))
    ev["sid"] = "other0001"
    return json.dumps(ev)


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
    ev_id = await fake_manager.send_text(_foreign("delete", id=fid, by="laptop/alice"))
    await sync.handle_post(ev_id, fake_manager.messages[ev_id][2], None)
    assert (await api.get(f"/api/files/{fid}")).status_code == 404
    # Reconcile: messages vanished without an event (deleted by hand in Telegram).
    fid2, ids2 = _other_server_uploads(fake_manager, "vanish.bin", b"v" * 2000, 1000)
    for mid, cap, size in ids2:
        await sync.handle_post(mid, json.dumps(cap), size)
    for mid, _c, _s in ids2:
        fake_manager.messages.pop(mid)
    # One file of one is >50% missing -> treated as a fetch problem unless it is a single file.
    res = await sync.reconcile(fake_manager)
    assert res["missing"] == 1 and res["removed"] == 0  # first sighting: wait for confirmation
    assert (await api.get(f"/api/files/{fid2}")).status_code == 200
    from datetime import datetime, timedelta
    sync._missing_seen[fid2] = datetime.utcnow() - timedelta(hours=2)
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
    await fake_manager.send_text(_foreign("delete", id=fid_a, by="laptop/alice"))
    res = await sync.catch_up(fake_manager)
    assert res["parts"] == 3 and res["events"] >= 1  # the delete, plus our own layout/hello echoes
    names = sorted(x["name"] for x in (await api.get("/api/files")).json()["files"])
    assert names == ["b.bin"]
    # Second run scans nothing new.
    again = await sync.catch_up(fake_manager)
    assert again["parts"] == 0 and again["events"] == 0  # one empty batch confirms the end; no marker posted
    assert not any(v[1] is None and "marker" in str(v[2]) for v in fake_manager.messages.values())
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


def _events(fm, since: set):
    return [json.loads(fm.messages[m][2]) for m in sorted(set(fm.messages) - since) if fm.messages[m][1] is None]


async def test_rename_and_move_events_apply_with_lww(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    fid, ids = _other_server_uploads(fake_manager, "draft.txt", b"d" * 10, 10, path="Docs")
    for mid, cap, size in ids:
        await sync.handle_post(mid, json.dumps(cap), size)
    # Rename + move from the other server.
    for ev in [_foreign("rename", id=fid, name="final.txt", by="laptop/alice"),
               _foreign("folder_create", path="Archive/2026", by="laptop/alice"),
               _foreign("move", id=fid, path="Archive/2026", by="laptop/alice")]:
        mid = await fake_manager.send_text(ev)
        await sync.handle_post(mid, ev, None)
    f = (await api.get(f"/api/files/{fid}")).json()
    assert f["name"] == "final.txt"
    tree = (await api.get("/api/folders/tree")).json()
    assert [(t["name"], t["depth"]) for t in tree] == [("Archive", 0), ("2026", 1), ("Docs", 0)]
    inside = (await api.get("/api/files", params={"folder_id": tree[1]["id"]})).json()
    assert [x["name"] for x in inside["files"]] == ["final.txt"]
    # An OLDER event must not undo the newer rename (last writer wins).
    old = json.loads(_foreign("rename", id=fid, name="stale.txt")); old["ts"] = "2000-01-01T00:00:00"
    mid = await fake_manager.send_text(json.dumps(old)); await sync.handle_post(mid, json.dumps(old), None)
    assert (await api.get(f"/api/files/{fid}")).json()["name"] == "final.txt"
    # Folder rename/move from the other server.
    for ev in [_foreign("folder_rename", path="Archive/2026", name="Y2026", by="laptop/alice"),
               _foreign("folder_move", path="Archive/Y2026", to="Docs", by="laptop/alice")]:
        mid = await fake_manager.send_text(ev); await sync.handle_post(mid, ev, None)
    tree = (await api.get("/api/folders/tree")).json()
    assert [(t["name"], t["depth"]) for t in tree] == [("Archive", 0), ("Docs", 0), ("Y2026", 1)]
    # Deleting a folder that still has files is refused until its files are gone.
    ev = _foreign("folder_delete", path="Docs/Y2026"); mid = await fake_manager.send_text(ev); await sync.handle_post(mid, ev, None)
    assert any(t["name"] == "Y2026" for t in (await api.get("/api/folders/tree")).json())
    ev = _foreign("delete", id=fid); mid = await fake_manager.send_text(ev); await sync.handle_post(mid, ev, None)
    ev = _foreign("folder_delete", path="Docs/Y2026"); mid = await fake_manager.send_text(ev); await sync.handle_post(mid, ev, None)
    assert not any(t["name"] == "Y2026" for t in (await api.get("/api/folders/tree")).json())


async def test_local_changes_emit_events_and_echo_is_harmless(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    f = (await _upload(api, "notes.txt", b"n" * 100))["file"]
    await _drain(transfer_worker.worker)
    since = set(fake_manager.messages)
    folder = (await api.post("/api/folders", json={"name": "Inbox"})).json()
    assert (await api.patch(f"/api/files/{f['id']}", json={"name": "renamed.txt"})).status_code == 200
    assert (await api.post(f"/api/files/{f['id']}/move", json={"folder_id": folder["id"]})).status_code == 200
    assert (await api.patch(f"/api/folders/{folder['id']}", json={"name": "Outbox"})).status_code == 200
    evs = _events(fake_manager, since)
    assert [e["t"] for e in evs] == ["folder_create", "rename", "move", "folder_rename"]
    assert evs[1]["name"] == "renamed.txt" and evs[2]["path"] == "Inbox" and evs[3] == {**evs[3], "path": "Inbox", "name": "Outbox"}
    assert all(e["by"] == "home/admin" for e in evs)
    # The channel echoes our own events back: nothing changes, nothing reverts.
    for m in sorted(set(fake_manager.messages) - since):
        await sync.handle_post(m, fake_manager.messages[m][2], None)
    g = (await api.get(f"/api/files/{f['id']}")).json()
    assert g["name"] == "renamed.txt" and g["folder_id"] == folder["id"]
    assert [t["name"] for t in (await api.get("/api/folders/tree")).json()] == ["Outbox"]
    # Bulk move + folder delete emit too.
    since = set(fake_manager.messages)
    top = (await api.post("/api/folders", json={"name": "Top"})).json()
    await api.post("/api/move", json={"folder_ids": [folder["id"]], "target_folder_id": top["id"]})
    await api.delete(f"/api/folders/{top['id']}")
    kinds = [e["t"] for e in _events(fake_manager, since)]
    assert kinds == ["folder_create", "folder_move", "delete", "folder_delete"]


async def test_layout_publish_and_apply(api, admin, fake_manager):
    # Server A (us) has a layout made before sharing: empty folder, moved + renamed file.
    f = (await _upload(api, "orig.txt", b"o" * 50))["file"]
    await _drain(transfer_worker.worker)
    empty = (await api.post("/api/folders", json={"name": "Empty"})).json()
    dest = (await api.post("/api/folders/ensure", json={"path": "Docs/2026"})).json()
    await api.patch(f"/api/files/{f['id']}", json={"name": "final.txt"})
    await api.post(f"/api/files/{f['id']}/move", json={"folder_id": dest["id"]})
    before = set(fake_manager.messages)
    # Switching to shared publishes the layout and says hello.
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    evs = _events(fake_manager, before)
    kinds = [e["t"] for e in evs]
    assert kinds == ["tree", "place", "hello"], kinds
    assert sorted(evs[0]["folders"]) == ["Docs", "Docs/2026", "Empty"]
    assert evs[1]["files"][f["id"]] == ["Docs/2026", "final.txt"]
    # Server B: fresh index from captions only (file at root, original name), then applies the layout.
    from sqlalchemy import delete
    from app import db as _db
    from app.models import File, FilePart, Folder
    async with _db.async_session() as db:
        await db.execute(delete(FilePart)); await db.execute(delete(File)); await db.execute(delete(Folder)); await db.commit()
    await api.put("/api/admin/settings", json={"workspace_name": "other"})  # pretend to be the other server
    mid, (name, blob, cap) = next((m, v) for m, v in sorted(fake_manager.messages.items()) if v[1] is not None)
    await sync.handle_post(mid, json.dumps(cap), len(blob))
    g = (await api.get(f"/api/files/{f['id']}")).json()
    assert g["name"] == "orig.txt" and g["folder_id"] is None
    for e in evs[:2]:  # tree + place (the hello would make us publish in turn)
        e = {**e, "sid": "other0001"}  # as if posted by the other server
        m = await fake_manager.send_text(json.dumps(e)); await sync.handle_post(m, json.dumps(e), None)
    g = (await api.get(f"/api/files/{f['id']}")).json()
    tree = (await api.get("/api/folders/tree")).json()
    assert [(t["name"], t["depth"]) for t in tree] == [("Docs", 0), ("2026", 1), ("Empty", 0)]
    assert g["name"] == "final.txt" and g["folder_id"] == next(t["id"] for t in tree if t["name"] == "2026")
    # A hello from another server queues a layout publication for the worker.
    assert sync.take_hello_request() is False
    hello = _foreign("hello", by="laptop/system"); m = await fake_manager.send_text(hello); await sync.handle_post(m, hello, None)
    assert sync.take_hello_request() is True


async def test_server_id_makes_echo_detection_name_independent(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "Linux"})
    f = (await _upload(api, "x.txt", b"x" * 10))["file"]
    await _drain(transfer_worker.worker)
    since = set(fake_manager.messages)
    await api.patch(f"/api/files/{f['id']}", json={"name": "y.txt"})
    ev = _events(fake_manager, since)[0]
    assert ev["sid"] == sync.status["server_id"] and ev["by"] == "Linux/admin"
    # Same name on the other server no longer hides its events: a rename with a
    # different sid but the same name prefix is applied.
    foreign = json.loads(sync.make_event("rename", id=f["id"], name="from-other.txt", by="Linux/bob")); foreign["sid"] = "deadbeef"
    m = await fake_manager.send_text(json.dumps(foreign)); await sync.handle_post(m, json.dumps(foreign), None)
    assert (await api.get(f"/api/files/{f['id']}")).json()["name"] == "from-other.txt"
    # Our own echo (same sid) is ignored even after a newer local change.
    m = await fake_manager.send_text(json.dumps(ev)); await sync.handle_post(m, json.dumps(ev), None)
    assert (await api.get(f"/api/files/{f['id']}")).json()["name"] == "from-other.txt"


async def test_revision_and_startup_handshake(api, admin, fake_manager):
    r0 = (await api.get("/api/files/revision")).json()["revision"]
    await api.post("/api/folders", json={"name": "A"})
    assert (await api.get("/api/files/revision")).json()["revision"] > r0
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    before = set(fake_manager.messages)
    await sync.startup_handshake(fake_manager)
    kinds = [e["t"] for e in _events(fake_manager, before)]
    assert "hello" in kinds and "tree" in kinds
    st = (await api.get("/api/admin/status")).json()["sync"]
    assert st["mode"] == "shared" and st["server_id"] and st["last_catchup_at"]



async def test_reconcile_refuses_mass_removal(api, admin, fake_manager):
    """If a fetch returns nothing (transient failure), nothing may be deleted."""
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    fids = []
    for n in range(4):
        fid, ids = _other_server_uploads(fake_manager, f"f{n}.bin", b"x" * 100, 100)
        for mid, cap, size in ids:
            await sync.handle_post(mid, json.dumps(cap), size)
        fids.append(fid)
    saved = dict(fake_manager.messages); fake_manager.messages.clear()   # everything "missing"
    from datetime import datetime, timedelta
    for fid in fids:
        sync._missing_seen[fid] = datetime.utcnow() - timedelta(hours=2)
    res = await sync.reconcile(fake_manager)
    assert res["removed"] == 0 and res["skipped"]
    for fid in fids:
        assert (await api.get(f"/api/files/{fid}")).status_code == 200
    fake_manager.messages.update(saved)


async def test_fresh_server_catches_up_past_a_deleted_start(api, admin, fake_manager):
    """A new server knows nothing; the channel's first 300 ids were deleted.
    Catch-up must still find the files further on."""
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "new"})
    fake_manager._next = 350  # ids 1..349 never existed / were deleted
    fid, _ = _other_server_uploads(fake_manager, "old.bin", b"o" * 2000, 1000)
    res = await sync.catch_up(fake_manager)
    assert res["parts"] == 2
    assert (await api.get(f"/api/files/{fid}")).json()["status"] == "ready"
    # A peer's tree event carries its top id; we scan up to it even after a gap.
    fake_manager._next = 3000
    fid2, _ = _other_server_uploads(fake_manager, "far.bin", b"f" * 500, 1000)
    tree = _foreign("tree", folders=[], top=3001); m = await fake_manager.send_text(tree); await sync.handle_post(m, tree, None)
    res = await sync.catch_up(fake_manager)
    assert res["parts"] == 1 and (await api.get(f"/api/files/{fid2}")).status_code == 200


async def test_revision_endpoint_reports_sync_phase(api, admin, fake_manager):
    r = (await api.get("/api/files/revision")).json()
    assert r["sync"] is None  # private mode: nothing to report
    await api.put("/api/admin/settings", json={"workspace_mode": "shared", "workspace_name": "home"})
    r = (await api.get("/api/files/revision")).json()
    assert r["sync"]["shared"] is True and r["sync"]["phase"] == "idle"
    sync.status["phase"] = "catching_up"; sync.status["progress"] = {"scanned": 300, "parts": 2, "events": 0}
    r = (await api.get("/api/files/revision")).json()
    assert r["sync"]["phase"] == "catching_up" and r["sync"]["progress"]["scanned"] == 300
    sync.status["phase"] = "idle"; sync.status["progress"] = None
    await sync.catch_up(fake_manager)
    assert sync.status["phase"] == "idle" and (await api.get("/api/files/revision")).json()["sync"]["last_catchup_at"]
