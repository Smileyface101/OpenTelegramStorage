"""OpenTelegramStorage — self-hosted file hosting on a Telegram channel."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, config, db as _db, security, status as status_mod
from app.routers import admin, auth, files, setup, shares, telegram, transfers, uploads
from app.telegram.manager import manager
from app.transfers import worker as transfer_worker

logging.basicConfig(level=os.getenv("OTS_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ots")
status_mod.install_log_capture()

CSRF_EXEMPT = ("/api/auth/login", "/api/auth/login/mfa", "/api/setup/admin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    if _db.async_session is None:
        _db.init_engine()
    await manager.load_from_settings()
    transfer_worker.worker = transfer_worker.TransferWorker(manager)
    transfer_worker.worker.start()
    logger.info("OpenTelegramStorage %s ready (data dir %s)", __version__, config.DATA_DIR)
    yield
    await transfer_worker.worker.stop()
    await manager.shutdown()


app = FastAPI(title="OpenTelegramStorage", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers_and_csrf(request: Request, call_next):
    path = request.url.path
    if (path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and path not in CSRF_EXEMPT and not path.startswith("/api/share/")
            and request.cookies.get(security.SESSION_COOKIE)):
        if not security.csrf_ok(request):
            return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store" if path.startswith("/api/") else "public, max-age=3600")
    return response


for r in (setup, auth, telegram, files, uploads, transfers, admin, shares):
    app.include_router(r.router)


@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__, "telegram": manager.status.connected}


# ---- SPA ----
dist = config.FRONTEND_DIST
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
