"""FastAPI app factory and static SPA mount."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.config import get_settings
from app.logging_setup import setup_logging

# Populated by the container build (frontend/dist copied to app/static).
STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    application = FastAPI(title="PnP Bridge", version="0.1.0")
    application.include_router(health_router)

    if STATIC_DIR.is_dir():
        application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")

    return application


app = create_app()
