"""Every server error must produce a JSON detail AND land in the Logs page."""

from pathlib import Path

import app.api.wizard as wizard_module
import pytest
from app.config import get_settings
from app.db.session import reset_engine
from app.logging_setup import flush_db_sink
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def tolerant_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PNPB_DB_PATH", str(tmp_path / "test.sqlite"))
    get_settings.cache_clear()
    reset_engine()
    yield TestClient(create_app(), raise_server_exceptions=False)
    flush_db_sink()
    reset_engine()
    get_settings.cache_clear()


def test_unhandled_error_returns_json_detail_and_is_logged(
    tolerant_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(wizard_module, "get_catalyst_client", boom)
    with tolerant_client as client:
        response = client.get("/api/wizard/pnp-devices")
        assert response.status_code == 500
        body = response.json()
        assert "AttributeError" in body["detail"]

        flush_db_sink()
        logs = client.get("/api/logs", params={"level": "error"}).json()
        assert logs["total"] >= 1
        assert any("/api/wizard/pnp-devices" in e["message"] for e in logs["entries"])


def test_configuration_error_is_logged_too(tolerant_client: TestClient) -> None:
    with tolerant_client as client:
        response = client.get("/api/wizard/pnp-devices")  # no credentials stored
        assert response.status_code == 400
        assert "not configured" in response.json()["detail"]

        flush_db_sink()
        logs = client.get("/api/logs", params={"component": "app.api"}).json()
        assert any("not configured" in e["message"] for e in logs["entries"])
