import asyncio
import hashlib
import io
import os

import pytest

from app import crypto
from app.transfers import worker as transfer_worker
from tests.conftest import content_key, stored_plaintext
from tests.test_transfers import _drain, _upload


class _Src(io.BytesIO):
    pass


@pytest.mark.parametrize("size", [0, 1, crypto.BLOCK - 1, crypto.BLOCK, crypto.BLOCK + 1, 3 * crypto.BLOCK + 12345])
def test_roundtrip_and_sizes(size):
    key = os.urandom(32)
    data = os.urandom(size)
    enc = crypto.EncryptingReader(_Src(data), size, key)
    ct = b""
    while True:
        piece = enc.read(70000)
        if not piece:
            break
        ct += piece
    assert len(ct) == crypto.ct_size(size) == enc.size
    assert ct[:4] == crypto.MAGIC and (size == 0 or ct[crypto.HEADER:] != data[:len(ct) - crypto.HEADER])
    salt = crypto.parse_header(ct)
    dec = crypto.BlockDecryptor(key, salt, 0, size)
    assert dec.feed(ct[crypto.HEADER:]) + dec.finish() == data
    # Rewind support (Telethon fallback path re-reads from 0).
    assert enc.seek(0) == 0 and enc.read() == ct


def test_range_math_and_partial_decrypt():
    key = os.urandom(32)
    size = 2 * crypto.BLOCK + 500
    data = os.urandom(size)
    enc = crypto.EncryptingReader(_Src(data), size, key)
    ct = enc.read()
    salt = crypto.parse_header(ct)
    for pt_from, pt_len in [(0, 10), (crypto.BLOCK - 5, 10), (crypto.BLOCK, crypto.BLOCK), (size - 7, 7), (5, size - 5)]:
        off, length, first = crypto.ct_range_for(pt_from, pt_len, size)
        dec = crypto.BlockDecryptor(key, salt, first, size)
        pt = dec.feed(ct[off:off + length]) + dec.finish()
        skip = pt_from - first * crypto.BLOCK
        assert pt[skip:skip + pt_len] == data[pt_from:pt_from + pt_len]


def test_tamper_is_detected():
    key = os.urandom(32)
    data = os.urandom(crypto.BLOCK + 10)
    ct = bytearray(crypto.EncryptingReader(_Src(data), len(data), key).read())
    ct[crypto.HEADER + 100] ^= 1
    dec = crypto.BlockDecryptor(key, crypto.parse_header(bytes(ct)), 0, len(data))
    with pytest.raises(Exception):
        dec.feed(bytes(ct[crypto.HEADER:]))


async def test_encrypted_upload_download_verify_share_rebuild(api, admin, fake_manager):
    await api.put("/api/admin/settings", json={"part_size_mb": 3})
    st = (await api.get("/api/admin/encryption")).json()
    assert st["encrypt_new"] is True
    data = os.urandom(4 * 1024 * 1024 + 777)  # 2 parts; part 1 spans 3 blocks
    f = (await _upload(api, "secret.bin", data))["file"]
    assert f["encrypted"] and f["key_id"]
    await _drain(transfer_worker.worker)
    f = (await api.get(f"/api/files/{f['id']}")).json()
    assert f["status"] == "ready"
    # Channel holds ciphertext only, with the format header.
    blobs = [v[1] for _k, v in sorted(fake_manager.messages.items())]
    assert all(b[:4] == b"OTS1" for b in blobs) and data[:1000] not in b"".join(blobs)
    caps = [v[2] for _k, v in sorted(fake_manager.messages.items())]
    assert caps[0]["enc"]["kid"] == f["key_id"] and caps[0]["enc"]["ct"] == len(blobs[0]) == crypto.ct_size(3 * 1024 * 1024)
    assert await stored_plaintext(fake_manager) == data
    # Full and ranged downloads decrypt on the fly (ranges cross block and part boundaries).
    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 200 and r.content == data
    for a, b in [(0, 99), (1048570, 1048600), (3 * 1048576 - 10, 3 * 1048576 + 10), (len(data) - 50, len(data) - 1)]:
        r = await api.get(f"/api/files/{f['id']}/download", headers={"Range": f"bytes={a}-{b}"})
        assert r.status_code == 206 and r.content == data[a:b + 1], (a, b)
    # Verify passes, then catches tampering.
    await api.post(f"/api/files/{f['id']}/verify")
    for _ in range(200):
        await asyncio.sleep(0.01)
        v = (await api.get(f"/api/files/{f['id']}")).json()
        if not v["verifying"] and (v["verified_at"] or v["integrity_error"]):
            break
    assert v["integrity_error"] is None and v["verified_at"]
    mid = sorted(fake_manager.messages)[1]
    name, blob, cap = fake_manager.messages[mid]
    fake_manager.messages[mid] = (name, blob[:crypto.HEADER + 5] + bytes([blob[crypto.HEADER + 5] ^ 1]) + blob[crypto.HEADER + 6:], cap)
    await api.post(f"/api/files/{f['id']}/verify")
    for _ in range(200):
        await asyncio.sleep(0.01)
        v = (await api.get(f"/api/files/{f['id']}")).json()
        if not v["verifying"] and (v["verified_at"] or v["integrity_error"]):
            break
    assert "part 2" in v["integrity_error"] and "decryption failed" in v["integrity_error"]
    fake_manager.messages[mid] = (name, blob, cap)
    # Public share link serves plaintext too.
    from httpx import ASGITransport, AsyncClient
    from main import app
    sh = (await api.post(f"/api/files/{f['id']}/shares", json={})).json()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        r = await anon.get(f"/api/share/{sh['id']}/download")
        assert r.status_code == 200 and r.content == data
    # Rebuild from the channel restores the encryption metadata.
    from sqlalchemy import delete
    from app import db as _db
    from app.models import File, FilePart, Share
    async with _db.async_session() as db:
        await db.execute(delete(Share)); await db.execute(delete(FilePart)); await db.execute(delete(File)); await db.commit()
    await api.post("/api/admin/rebuild")
    for _ in range(100):
        st = (await api.get("/api/admin/rebuild")).json()
        if not st["running"]:
            break
    assert st["files_imported"] == 1
    g = (await api.get(f"/api/files/{f['id']}")).json()
    assert g["encrypted"] and g["key_id"] == f["key_id"]
    r = await api.get(f"/api/files/{f['id']}/download")
    assert r.content == data


async def test_key_export_import_and_toggle(api, admin, fake_manager):
    st = (await api.get("/api/admin/encryption")).json()
    assert st["has_key"] is True and st["encrypted_files"] == 0  # key exists as soon as encryption is on
    f = (await _upload(api, "a.bin", b"x" * 1000))["file"]
    await _drain(transfer_worker.worker)
    st2 = (await api.get("/api/admin/encryption")).json()
    assert st2["encrypted_files"] == 1 and st2["key_id"] == st["key_id"] == f["key_id"]
    assert (await api.post("/api/admin/encryption/export", json={"password": "wrong"})).status_code == 400
    exp = (await api.post("/api/admin/encryption/export", json={"password": "correct horse battery"})).json()
    assert len(exp["key"]) == 64 and exp["key_id"] == st["key_id"]
    # Cannot replace a key that files depend on.
    r = await api.post("/api/admin/encryption/import", json={"key": "00" * 32, "password": "correct horse battery"})
    assert r.status_code == 409
    # Turning encryption off: new files are plain, old stay encrypted and readable.
    await api.put("/api/admin/settings", json={"encrypt_new": False})
    g = (await _upload(api, "plain.bin", b"y" * 1000))["file"]
    await _drain(transfer_worker.worker)
    assert g["encrypted"] is False and fake_manager.messages[sorted(fake_manager.messages)[-1]][1] == b"y" * 1000
    assert (await api.get(f"/api/files/{f['id']}/download")).content == b"x" * 1000
    # Simulate a fresh install that imports the exported key.
    from app import db as _db, settings_store
    async with _db.async_session() as db:
        await settings_store.delete(db, "content.key"); await settings_store.delete(db, "content.key_id"); await db.commit()
    assert (await api.get(f"/api/files/{f['id']}/download")).status_code == 503
    r = await api.post("/api/admin/encryption/import", json={"key": exp["key"], "password": "correct horse battery"})
    assert r.status_code == 200 and r.json()["key_id"] == st["key_id"]
    assert (await api.get(f"/api/files/{f['id']}/download")).content == b"x" * 1000
