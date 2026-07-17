import os
from collections.abc import Iterator
from pathlib import Path

from cryptography.fernet import Fernet

# Must be set before app modules are imported anywhere.
os.environ.setdefault("PNPB_SECRET_KEY", Fernet.generate_key().decode())

import pytest
from app.config import get_settings
from app.db.session import reset_engine
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App with a fresh, migrated SQLite DB per test."""
    monkeypatch.setenv("PNPB_DB_PATH", str(tmp_path / "test.sqlite"))
    get_settings.cache_clear()
    reset_engine()
    try:
        with TestClient(create_app()) as test_client:
            yield test_client
    finally:
        # Drain queued DB-sink records before the engine (and tmp DB) go away.
        from app.logging_setup import flush_db_sink

        flush_db_sink()
        reset_engine()
        get_settings.cache_clear()
