import time

import pyotp

from app import security


async def _enable(api):
    setup = (await api.post("/api/auth/totp/setup")).json()
    code = pyotp.TOTP(setup["secret"]).now()
    r = await api.post("/api/auth/totp/enable", json={"code": code})
    assert r.status_code == 200, r.text
    return setup["secret"], r.json()["recovery_codes"]


async def test_enrol_login_recovery_and_disable(api, admin, client):
    secret, codes = await _enable(api)
    assert len(codes) == 10 and (await api.get("/api/auth/totp")).json() == {"enabled": True, "recovery_codes_left": 10}

    await api.post("/api/auth/logout")
    r = await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200 and r.json()["mfa_required"] is True
    assert security.SESSION_COOKIE not in client.cookies, "no session before the second factor"
    token = r.json()["mfa_token"]

    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "000000"})
    assert r.status_code == 401
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": "bogus-token-value", "code": "000000"})
    assert r.status_code == 401
    # The code used to enrol cannot be reused (replay guard), so take the next step's code.
    good = pyotp.TOTP(secret).at(int(time.time()) + 30)
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": good})
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert (await api.get("/api/auth/me")).status_code == 200

    # The same code cannot be replayed within its time step.
    await api.post("/api/auth/logout")
    token = (await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})).json()["mfa_token"]
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": good})
    assert r.status_code == 401

    # Recovery code works exactly once.
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 200
    assert (await api.get("/api/auth/totp")).json()["recovery_codes_left"] == 9
    await api.post("/api/auth/logout")
    token = (await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})).json()["mfa_token"]
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 401
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": codes[1]})
    assert r.status_code == 200

    # Disable needs password + a valid factor.
    r = await api.post("/api/auth/totp/disable", json={"password": "wrong", "code": codes[2]})
    assert r.status_code == 400
    r = await api.post("/api/auth/totp/disable", json={"password": "correct horse battery", "code": codes[2]})
    assert r.status_code == 200 and r.json()["enabled"] is False
    await api.post("/api/auth/logout")
    r = await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200 and "mfa_required" not in r.json()


async def test_mfa_attempts_are_limited(api, admin):
    secret, _ = await _enable(api)
    await api.post("/api/auth/logout")
    token = (await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})).json()["mfa_token"]
    for _ in range(security.MFA_MAX_ATTEMPTS):
        assert (await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": "111111"})).status_code == 401
    # Token is burned even with the right code now.
    r = await api.post("/api/auth/login/mfa", json={"mfa_token": token, "code": pyotp.TOTP(secret).at(int(time.time()) + 30)})
    assert r.status_code == 401


async def test_sessions_list_and_revoke(api, admin):
    from httpx import ASGITransport, AsyncClient
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers={"User-Agent": "OtherBrowser/1.0"}) as other:
        assert (await other.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})).status_code == 200
        sessions = (await api.get("/api/auth/sessions")).json()
        assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
        other_id = next(s["id"] for s in sessions if not s["current"])
        assert next(s for s in sessions if not s["current"])["user_agent"] == "OtherBrowser/1.0"
        me_id = next(s["id"] for s in sessions if s["current"])
        assert (await api.delete(f"/api/auth/sessions/{me_id}")).status_code == 400
        assert (await api.delete(f"/api/auth/sessions/{other_id}")).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await other.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})).status_code == 200
        r = await api.post("/api/auth/sessions/revoke-others")
        assert r.json()["revoked"] == 1 and (await other.get("/api/auth/me")).status_code == 401


async def test_admin_reset_totp(api, admin):
    await api.post("/api/admin/users", json={"username": "bob", "password": "bobs long password"})
    from httpx import ASGITransport, AsyncClient
    from main import app
    from tests.conftest import Api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        bob = Api(c)
        await bob.post("/api/auth/login", json={"username": "bob", "password": "bobs long password"})
        await _enable(bob)
        users = (await api.get("/api/admin/users")).json()
        assert next(u for u in users if u["username"] == "bob")["totp_enabled"] is True
        bob_id = next(u["id"] for u in users if u["username"] == "bob")
        r = await api.post(f"/api/admin/users/{bob_id}/totp/reset")
        assert r.status_code == 200 and r.json()["totp_enabled"] is False
        assert (await bob.get("/api/auth/me")).status_code == 401  # signed out everywhere
        r = await bob.post("/api/auth/login", json={"username": "bob", "password": "bobs long password"})
        assert r.status_code == 200 and "mfa_required" not in r.json()
