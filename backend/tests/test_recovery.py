import os

from sqlalchemy import delete

from app import db as _db, recovery
from app.models import File, FilePart, Folder
from app.transfers import worker as transfer_worker
from tests.test_transfers import _drain, _upload


async def test_rebuild_index_from_channel(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 1})
    folder = (await api.post("/api/folders/ensure", json={"path": "docs/2024"})).json()
    data = os.urandom(1024 * 1024 + 777)  # 2 parts
    f = (await _upload(api, "big.bin", data, folder_id=folder["id"]))["file"]
    small = (await _upload(api, "note.txt", b"hello"))["file"]
    await _drain(transfer_worker.worker)
    assert (await api.get(f"/api/files/{f['id']}")).json()["status"] == "ready"
    cap = list(fake_manager.messages.values())[0][2]
    assert cap["path"] == "docs/2024"

    # Simulate a lost database: wipe the index (channel keeps the messages).
    async with _db.async_session() as db:
        await db.execute(delete(FilePart)); await db.execute(delete(File)); await db.execute(delete(Folder))
        await db.commit()
    assert (await api.get("/api/files")).json() == {"folder": None, "breadcrumbs": [], "folders": [], "files": []}

    # Drop one message of a third file to exercise the incomplete path.
    fake_manager.messages[999] = ("lost.bin.001", b"x" * 10, {"ots": 1, "id": "deadbeef", "name": "lost.bin", "size": 20,
                                                              "part": 1, "of": 2, "psize": 10, "sha256": None})
    fake_manager._next = 1000

    r = await api.post("/api/admin/rebuild")
    assert r.status_code == 200
    for _ in range(50):
        st = (await api.get("/api/admin/rebuild")).json()
        if not st["running"]:
            break
    assert st["error"] is None, st
    assert st["files_imported"] == 2 and st["files_incomplete"] == 1 and st["files_skipped"] == 0

    root = (await api.get("/api/files")).json()
    assert [d["name"] for d in root["folders"]] == ["docs"]
    names = sorted(x["name"] for x in root["files"])
    assert names == ["lost.bin", "note.txt"]
    lost = next(x for x in root["files"] if x["name"] == "lost.bin")
    assert lost["status"] == "failed" and "missing" in lost["error"]

    # The recovered big file downloads byte-for-byte from the channel.
    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 200 and r.content == data
    # Running again imports nothing new.
    await api.post("/api/admin/rebuild")
    for _ in range(50):
        st = (await api.get("/api/admin/rebuild")).json()
        if not st["running"]:
            break
    assert st["files_skipped"] == 3 and st["files_imported"] == 0


def test_parse_caption_rejects_junk():
    assert recovery.parse_caption("hello") is None
    assert recovery.parse_caption('{"foo":1}') is None
    assert recovery.parse_caption('{"ots":1,"id":"a","name":"n","size":1,"part":1,"of":1}')["id"] == "a"
    assert recovery.parse_caption('{"otg":1,"id":"a","name":"n","size":1,"part":1,"of":1}') is not None
