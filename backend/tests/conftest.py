import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

_tmp = tempfile.mkdtemp(prefix="ots-test-")
os.environ["OTS_DATA_DIR"] = _tmp
os.environ["OTS_COOKIE_SECURE"] = "false"
os.environ["OTS_UPLOAD_CHUNK_SIZE"] = str(64 * 1024)  # small chunks keep the tests fast

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import config, db as _db, security, vault  # noqa: E402
from app.models import Base  # noqa: E402
from app.transfers import worker as transfer_worker  # noqa: E402
from tests.fake_telegram import FakeManager  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database():
    config.ensure_dirs()
    vault.reset_for_tests()
    _db.init_engine(f"sqlite+aiosqlite:///{Path(_tmp) / 'test.db'}")
    async with _db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await _db.engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Each test starts from empty tables."""
    async with _db.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    security.login_throttle.reset()
    transfer_worker.progress.active.clear()
    from app import sync as _sync
    _sync._hello_pending.clear(); _sync._last_hello_reply = 0.0
    for leftover in config.STAGING_DIR.glob("*"):
        leftover.unlink()
    yield


@pytest.fixture
def fake_manager(monkeypatch):
    fm = FakeManager()
    import app.routers.admin as admin_router
    import app.routers.files as files_router
    import app.routers.shares as shares_router
    monkeypatch.setattr(files_router, "manager", fm)
    monkeypatch.setattr(admin_router, "manager", fm)
    monkeypatch.setattr(shares_router, "manager", fm)
    transfer_worker.worker = transfer_worker.TransferWorker(fm, poll_interval=0.01)
    yield fm
    transfer_worker.worker = None


@pytest_asyncio.fixture
async def client():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


class Api:
    """Thin wrapper adding the CSRF header from the cookie jar."""

    def __init__(self, client: AsyncClient):
        self.c = client

    def _h(self, headers=None):
        h = dict(headers or {})
        csrf = self.c.cookies.get(security.CSRF_COOKIE)
        if csrf:
            h[security.CSRF_HEADER] = csrf
        return h

    async def get(self, url, **kw):
        return await self.c.get(url, **kw)

    async def post(self, url, headers=None, **kw):
        return await self.c.post(url, headers=self._h(headers), **kw)

    async def put(self, url, headers=None, **kw):
        return await self.c.put(url, headers=self._h(headers), **kw)

    async def patch(self, url, headers=None, **kw):
        return await self.c.patch(url, headers=self._h(headers), **kw)

    async def delete(self, url, headers=None, **kw):
        return await self.c.delete(url, headers=self._h(headers), **kw)


@pytest_asyncio.fixture
async def api(client):
    return Api(client)


@pytest_asyncio.fixture
async def admin(api):
    r = await api.post("/api/setup/admin", json={"username": "admin", "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return r.json()


async def content_key():
    from app import crypto
    async with _db.async_session() as db:
        return await crypto.get_key(db)


async def stored_plaintext(fm) -> bytes:
    return fm.plaintext_bytes(await content_key())
