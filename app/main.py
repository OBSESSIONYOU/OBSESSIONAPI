"""OBSESSIONAPI — 自建 API 网关 + 管理后台。"""
import asyncio
import os
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database
from .admin_api import router as admin_router
from .gateway import (auto_recover_loop, external_router, is_channel_auto_disabled,
                      is_channel_cooling, load_breaker_state, router as gateway_router)
from .settings_store import clear_cache, get

app = FastAPI(title="OBSESSIONAPI", version="1.0.0",
              docs_url="/api/docs", redoc_url=None)

# 允许浏览器端 AI 应用（Web UI 等）跨域直连网关；管理接口本身仍有登录态保护
cors_origins = [origin.strip() for origin in os.getenv(
    "OBSESSIONAPI_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"
).split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(gateway_router)
app.include_router(external_router)
app.include_router(admin_router)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def refresh_console_assets(request, call_next):
    request_id = request.headers.get("x-request-id") or secrets.token_hex(8)
    request.state.request_id = request_id
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers.setdefault("X-Request-ID", request_id)
    return response


@app.get("/health")
async def health():
    from . import database

    try:
        database.query_one("SELECT 1")
        db_writable = os.access(database.DATA_DIR, os.W_OK) and (
            not database.DB_PATH.exists() or os.access(database.DB_PATH, os.W_OK)
        )
    except Exception:  # noqa: BLE001
        db_writable = False
    channels = database.query("SELECT id FROM channels WHERE status=1")
    cooling = [row["id"] for row in channels if is_channel_cooling(row["id"])]
    auto_disabled = [row["id"] for row in channels if is_channel_auto_disabled(row["id"])]
    errors = database.query_one(
        "SELECT COUNT(*) c FROM request_logs WHERE status='error' AND created_at >= ?",
        (database.iso(database.utcnow() - timedelta(hours=1)),),
    )["c"]
    total = database.query_one(
        "SELECT COUNT(*) c FROM request_logs WHERE created_at >= ?",
        (database.iso(database.utcnow() - timedelta(hours=1)),),
    )["c"]
    return {
        "status": "ok",
        "service": "obsessionapi",
        "version": app.version,
        "db_writable": db_writable,
        "enabled_channels": len(channels),
        "cooling_channels": len(cooling),
        "auto_disabled_channels": len(auto_disabled),
        "error_rate_1h": round(errors * 100 / total, 2) if total else None,
    }


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.on_event("startup")
async def startup():
    database.init_db()
    clear_cache()
    load_breaker_state()
    database.execute("DELETE FROM sessions WHERE expires_at <= ?", (database.iso(database.utcnow()),))
    retention = int(get("log_retention_days"))
    if retention:
        database.execute(
            "DELETE FROM request_logs WHERE created_at < ?",
            (database.iso(database.utcnow() - timedelta(days=retention)),),
        )
    app.state.recover_task = asyncio.create_task(auto_recover_loop())
    if get("probe_enabled"):
        from .health_probe import probe_loop

        app.state.probe_task = asyncio.create_task(probe_loop())


@app.on_event("shutdown")
async def shutdown():
    for name in ("recover_task", "probe_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@app.exception_handler(404)
async def not_found(request, exc):
    if request.url.path.startswith(("/v1", "/api")):
        return JSONResponse({"error": {"message": f"路径 {request.url.path} 不存在", "code": 404}}, status_code=404)
    return FileResponse(STATIC_DIR / "index.html")
