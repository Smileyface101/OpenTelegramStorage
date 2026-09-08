import os
from datetime import datetime, timedelta

from httpx import ASGITransport, AsyncClient

from app import db as _db
from app.models import Share
from app.transfers import worker as transfer_worker
from tests.test_transfers import _drain, _upload


async def _ready_file(api, name="shared.bin", size=200_000):
    data = os.urandom(size)
    f = (await _upload(api, name, data))["file"]
    await _drain(transfer_worker.worker)
    return f, data


async def test_public_link_flow(api, admin, fake_manager):
    from main import app
    f, data = await _ready_file(api)
    r = await api.post(f"/api/files/{f['id']}/shares", json={"label": "for bob", "max_downloads": 2})
    assert r.status_code == 200, r.text
    sh = r.json()
    assert sh["url"].endswith(f"/s/{sh['id']}") and sh["active"] and not sh["has_password"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        info = (await anon.get(f"/api/share/{sh['id']}")).json()
        assert info["name"] == "shared.bin" and info["size"] == len(data) and info["downloads_left"] == 2
        r = await anon.get(f"/api/share/{sh['id']}/download")
        assert r.status_code == 200 and r.content == data and r.headers["x-checksum-sha256"] == f["sha256"]
        # Range continuation is not a second download.
        r = await anon.get(f"/api/share/{sh['id']}/download", headers={"Range": "bytes=1000-1999"})
        assert r.status_code == 206 and r.content == data[1000:2000]
        assert (await anon.get(f"/api/share/{sh['id']}")).json()["downloads_left"] == 1
        r = await anon.get(f"/api/share/{sh['id']}/download")
        assert r.status_code == 200
        # Cap reached: link is gone for the public.
        assert (await anon.get(f"/api/share/{sh['id']}")).status_code == 404
        assert (await anon.get(f"/api/share/{sh['id']}/download")).status_code == 404
        # Unknown token.
        assert (await anon.get("/api/share/nope")).status_code == 404
    mine = (await api.get("/api/shares")).json()
    assert len(mine) == 1 and mine[0]["download_count"] == 2 and mine[0]["active"] is False


async def test_password_and_expiry_and_revoke(api, admin, fake_manager):
    from main import app
    f, data = await _ready_file(api)
    sh = (await api.post(f"/api/files/{f['id']}/shares", json={"password": "open sesame", "expires_in_hours": 1})).json()
    assert sh["has_password"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        assert (await anon.get(f"/api/share/{sh['id']}")).json()["requires_password"] is True
        assert (await anon.get(f"/api/share/{sh['id']}/download")).status_code == 401
        r = await anon.post(f"/api/share/{sh['id']}/unlock", json={"password": "wrong"})
        assert r.status_code == 401
        grant = (await anon.post(f"/api/share/{sh['id']}/unlock", json={"password": "open sesame"})).json()["grant"]
        r = await anon.get(f"/api/share/{sh['id']}/download", params={"grant": grant})
        assert r.status_code == 200 and r.content == data
        assert (await anon.get(f"/api/share/{sh['id']}/download", params={"grant": "bogus"})).status_code == 401
        # Disable, re-enable, expire, delete.
        assert (await api.post(f"/api/shares/{sh['id']}/toggle")).json()["disabled"] is True
        assert (await anon.get(f"/api/share/{sh['id']}")).status_code == 404
        assert (await api.post(f"/api/shares/{sh['id']}/toggle")).json()["disabled"] is False
        assert (await anon.get(f"/api/share/{sh['id']}")).status_code == 200
        async with _db.async_session() as db:
            row = await db.get(Share, sh["id"]); row.expires_at = datetime.utcnow() - timedelta(minutes=1); await db.commit()
        assert (await anon.get(f"/api/share/{sh['id']}")).status_code == 404
        assert (await api.delete(f"/api/shares/{sh['id']}")).status_code == 200
        assert (await api.get(f"/api/files/{f['id']}/shares")).json() == []


async def test_share_requires_ready_file_and_ownership(api, admin, fake_manager):
    r = await api.post("/api/uploads", json={"name": "x.bin", "size": 10})
    fid = r.json()["file_id"]
    assert (await api.post(f"/api/files/{fid}/shares", json={})).status_code == 409
    f, _ = await _ready_file(api)
    sh = (await api.post(f"/api/files/{f['id']}/shares", json={})).json()
    await api.post("/api/admin/users", json={"username": "eve", "password": "eves long password"})
    await api.post("/api/auth/logout")
    await api.post("/api/auth/login", json={"username": "eve", "password": "eves long password"})
    assert (await api.delete(f"/api/shares/{sh['id']}")).status_code == 404
    assert (await api.get(f"/api/files/{f['id']}/shares")).status_code == 404


async def test_password_attempts_throttled(api, admin, fake_manager):
    from main import app
    f, _ = await _ready_file(api)
    sh = (await api.post(f"/api/files/{f['id']}/shares", json={"password": "secret-pw"})).json()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as anon:
        codes = [(await anon.post(f"/api/share/{sh['id']}/unlock", json={"password": "nope"})).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
