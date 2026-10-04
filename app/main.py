"""FastAPI app factory and static SPA mount."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import app as app_pkg
from app.api.health import router as health_router
from app.api.logs import router as logs_router
from app.api.mappings import router as mappings_router
from app.api.settings import router as settings_router
from app.api.stats import router as stats_router
from app.api.wizard import router as wizard_router
from app.config import get_settings
from app.errors import ConfigurationError, PnPBridgeError
from app.logging_setup import open_db_sink, set_http_trace, setup_logging

REPO_ROOT = Path(__file__).parent.parent
# Populated by the container build (frontend/dist copied to app/static).
STATIC_DIR = Path(__file__).parent / "static"


def run_migrations() -> None:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    command.upgrade(config, "head")


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    # Zero-config start: generate + persist an encryption key when none is set,
    # so credentials can be added later via the web UI.
    settings.ensure_secret_key()
    run_migrations()
    # Only now does `log_entries` exist: persist what was queued during startup.
    open_db_sink()
    # Nothing can be running yet: a job still marked `*_running` lost its
    # background task to a restart and would otherwise be stuck for good.
    from app.services.recovery import recover_interrupted_jobs

    recover_interrupted_jobs()
    # Restore the stored HTTP-trace setting: a capture in progress must survive
    # a container restart, or the operator loses the very call being chased.
    from app.db.session import open_session
    from app.services import settings_store

    with open_session() as db:
        set_http_trace(settings_store.http_trace(db))
    # Nightly log retention (default 90 days, configurable).
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.services.stats import cleanup_old_logs

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        cleanup_old_logs, "cron", hour=3, minute=0, args=[settings.log_retention_days]
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    application = FastAPI(title="PnP Bridge", version=app_pkg.__version__, lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(settings_router)
    application.include_router(mappings_router)
    application.include_router(wizard_router)
    application.include_router(logs_router)
    application.include_router(stats_router)

    @application.exception_handler(PnPBridgeError)
    async def pnpb_error_handler(request: Request, exc: PnPBridgeError) -> JSONResponse:
        status = 400 if isinstance(exc, ConfigurationError) else 500
        logging.getLogger("app.api").warning(
            "%s %s failed: %s", request.method, request.url.path, exc.message
        )
        return JSONResponse(status_code=status, content={"detail": exc.message})

    @application.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Every unexpected error must be visible on the Logs page (app.* sink).
        # type + text in the message itself: the Logs page list shows the message,
        # the traceback sits in the entry's context (exc_info)
        logging.getLogger("app.api").exception(
            "Unhandled error on %s %s: %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"Unexpected error: {type(exc).__name__}: {exc}. "
                "See the Logs page for details."
            },
        )

    if STATIC_DIR.is_dir():
        application.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @application.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            # Serve real files (favicon etc.); anything else falls back to the
            # SPA entry point so client-side routes survive a page reload.
            candidate = (STATIC_DIR / full_path).resolve()
            if full_path and candidate.is_relative_to(STATIC_DIR.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")

    return application


app = create_app()
