from app import config, security


async def test_first_run_and_login_flow(api, client):
    st = (await api.get("/api/setup/status")).json()
    assert st["needs_admin"] is True and st["telegram_configured"] is False

    r = await api.post("/api/setup/admin", json={"username": "admin", "password": "short"})
    assert r.status_code == 400
    r = await api.post("/api/setup/admin", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert security.SESSION_COOKIE in client.cookies and security.CSRF_COOKIE in client.cookies

    r = await api.post("/api/setup/admin", json={"username": "second", "password": "correct horse battery"})
    assert r.status_code == 409

    assert (await api.get("/api/auth/me")).json()["username"] == "admin"

    # State-changing call without CSRF header is refused.
    r = await client.post("/api/folders", json={"name": "docs"})
    assert r.status_code == 403
    r = await api.post("/api/folders", json={"name": "docs"})
    assert r.status_code == 200

    r = await api.post("/api/auth/logout")
    assert r.status_code == 200
    assert (await api.get("/api/auth/me")).status_code == 401

    r = await api.post("/api/auth/login", json={"username": "admin", "password": "wrong password"})
    assert r.status_code == 401
    r = await api.post("/api/auth/login", json={"username": "nobody", "password": "wrong password"})
    assert r.status_code == 401
    r = await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200


async def test_lockout_after_repeated_failures(api, admin):
    await api.post("/api/auth/logout")
    for _ in range(config.LOGIN_MAX_FAILURES):
        r = await api.post("/api/auth/login", json={"username": "admin", "password": "nope nope nope"})
        assert r.status_code == 401
    r = await api.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 423


async def test_password_change_revokes_other_sessions(api, admin, client):
    from httpx import ASGITransport, AsyncClient
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as other:
        r = await other.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
        assert r.status_code == 200
        r = await api.post("/api/auth/password", json={"current_password": "correct horse battery",
                                                      "new_password": "new longer password"})
        assert r.status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await api.get("/api/auth/me")).status_code == 200


async def test_admin_only_routes(api, admin):
    r = await api.post("/api/admin/users", json={"username": "bob", "password": "bobs long password", "role": "user"})
    assert r.status_code == 200
    await api.post("/api/auth/logout")
    r = await api.post("/api/auth/login", json={"username": "bob", "password": "bobs long password"})
    assert r.status_code == 200
    assert (await api.get("/api/admin/settings")).status_code == 403
    assert (await api.get("/api/telegram/status")).status_code == 200
