import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.core.cache import build_cache
from app.core.rate_limiter import TokenBucketLimiter
from app.db import Base, SessionLocal, engine
from app.deps import require_docs_admin
from app.realtime import hub
from app.routers import admin, auth, bookings, events
from app.services import booking_service, event_service

settings = get_settings()
STATIC = Path(__file__).parent / "static"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        base.update(getattr(record, "extra_fields", {}))
        return json.dumps(base)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
log = logging.getLogger("ticketdhaka")


async def expiry_sweeper(app: FastAPI) -> None:
    """Frees seats whose 5-minute hold ran out. On a multi-instance deploy this
    would be a separate worker process; SKIP LOCKED keeps it safe either way."""
    while True:
        await asyncio.sleep(settings.expiry_sweep_seconds)
        try:
            def sweep() -> set[int]:
                with SessionLocal() as db:
                    return booking_service.release_expired_holds(db)

            changed = await asyncio.to_thread(sweep)
            for event_id in changed:
                event_service.invalidate_event(app.state.cache, event_id)
                hub.publish(event_id, {"type": "refresh"})
            if changed:
                log.info("released expired holds", extra={"extra_fields": {"events": sorted(changed)}})
        except Exception:
            log.exception("expiry sweep failed")


def migrate_schema() -> None:
    """Small hand-written migrations for databases created by older versions.
    `create_all` makes missing tables but never changes existing ones, so new
    columns and enum values are added here. Every statement is idempotent (IF NOT EXISTS),
    so running it on every start is safe. (If more of these pile up, switch to Alembic.)"""
    if engine.dialect.name != "postgresql":
        return
    # ALTER TYPE ... ADD VALUE can't run inside a transaction, hence AUTOCOMMIT
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if conn.execute(text("SELECT 1 FROM pg_type WHERE typname = 'role'")).first():
            conn.execute(text("ALTER TYPE role ADD VALUE IF NOT EXISTS 'admin'"))
        if conn.execute(text("SELECT to_regclass('events')")).scalar():
            conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS area VARCHAR(60) NOT NULL DEFAULT 'Dhaka'"))
            conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS category VARCHAR(30) NOT NULL DEFAULT 'other'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_category_starts ON events (category, starts_at)"))
            # Backfill the 3 events from the first release, so they get proper labels
            for title, area, cat in [("Dhaka Tech Fest 2026", "Purbachal Road", "tech"),
                                     ("Indie Night Live", "Segunbagicha", "music"),
                                     ("AI Builders Meetup", "Palashi", "tech")]:
                conn.execute(text("UPDATE events SET area = :a, category = :c WHERE title = :t AND category = 'other'"),
                             {"a": area, "c": cat, "t": title})


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate_schema()
    Base.metadata.create_all(engine)
    from app.seed import ensure_admin, seed

    with SessionLocal() as db:
        if settings.seed_demo_data:
            seed(db)
        ensure_admin(db, settings.admin_email, settings.admin_password)
    app.state.cache = build_cache(
        settings.redis_url, settings.cache_capacity, settings.cache_ttl_seconds, settings.cache_enabled
    )
    hub.loop = asyncio.get_running_loop()
    task = asyncio.create_task(expiry_sweeper(app))
    yield
    task.cancel()


app = FastAPI(
    title="TicketDhaka",
    description="Concurrency-safe event ticket booking API. No double-booking, even under load.",
    version="1.0.0",
    lifespan=lifespan,
    # FastAPI's built-in docs are public. Turn them off and serve our own, admin-only versions below.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
limiter = TokenBucketLimiter(settings.rate_limit_capacity, settings.rate_limit_refill_per_sec)
# /docs and /openapi.json are NOT exempt: they take a password now, so they're rate limited against guessing
EXEMPT = ("/health", "/static", "/ws")


@app.middleware("http")
async def rate_limit_and_log(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    path = request.url.path
    if settings.rate_limit_enabled and path != "/" and not path.startswith(EXEMPT):
        client = request.headers.get("X-Forwarded-For", request.client.host if request.client else "?").split(",")[0]
        ok, retry = limiter.allow(client)
        if not ok:
            return JSONResponse(
                {"detail": "Too many requests"}, status_code=429,
                headers={"Retry-After": f"{retry:.1f}", "X-Request-ID": request_id},
            )
    start = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    if not path.startswith(("/static", "/health")):
        log.info("request", extra={"extra_fields": {
            "id": request_id, "method": request.method, "path": path,
            "status": response.status_code, "ms": round(ms, 1),
        }})
    return response


app.include_router(auth.router)
app.include_router(events.router)
app.include_router(bookings.router)
app.include_router(admin.router)


@app.get("/health", tags=["ops"])
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "db": db_ok, "cache": app.state.cache.stats()},
        status_code=200 if db_ok else 503,
    )


@app.websocket("/ws/events/{event_id}")
async def seat_feed(ws: WebSocket, event_id: int):
    await hub.join(event_id, ws)
    try:
        while True:
            await ws.receive_text()  # keepalive pings from the client
    except WebSocketDisconnect:
        hub.leave(event_id, ws)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_docs_admin)])
def openapi_schema():
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_docs_admin)])
def api_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="TicketDhaka API docs (admin)")
