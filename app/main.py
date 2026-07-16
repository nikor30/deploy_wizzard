"""FastAPI app factory and static SPA mount."""

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
from app.api.mappings import router as mappings_router
from app.api.settings import router as settings_router
from app.config import get_settings
from app.errors import ConfigurationError, PnPBridgeError
from app.logging_setup import setup_logging

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
    # Fail fast on misconfiguration instead of failing on first credential save.
    settings.require_secret_key()
    run_migrations()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="PnP Bridge", version=app_pkg.__version__, lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(settings_router)
    application.include_router(mappings_router)

    @application.exception_handler(PnPBridgeError)
    async def pnpb_error_handler(_request: Request, exc: PnPBridgeError) -> JSONResponse:
        status = 400 if isinstance(exc, ConfigurationError) else 500
        return JSONResponse(status_code=status, content={"detail": exc.message})

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
