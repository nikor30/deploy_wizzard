"""The container must start with zero configuration: no PNPB_SECRET_KEY set."""

from pathlib import Path

import pytest
from app.config import get_settings
from app.db.session import reset_engine
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def keyless_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PNPB_SECRET_KEY", raising=False)
    monkeypatch.setenv("PNPB_DB_PATH", str(tmp_path / "pnpb.sqlite"))
    get_settings.cache_clear()
    reset_engine()
    yield tmp_path
    reset_engine()
    get_settings.cache_clear()


def test_starts_without_key_and_generates_key_file(keyless_env: Path) -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
    key_file = keyless_env / "secret.key"
    assert key_file.is_file()
    assert (key_file.stat().st_mode & 0o777) == 0o600


def test_generated_key_persists_across_restarts(keyless_env: Path) -> None:
    with TestClient(create_app()) as client:
        client.put(
            "/api/settings/credentials",
            json={"netbox": {"base_url": "https://n.example.com", "secret": "tok-1234"}},
        )
    first_key = (keyless_env / "secret.key").read_text()

    # Simulate a container restart: fresh settings + engine, same volume.
    get_settings.cache_clear()
    reset_engine()
    with TestClient(create_app()) as client:
        body = client.get("/api/settings/credentials").json()
    assert body["netbox"]["secret_masked"] == "****1234"
    assert (keyless_env / "secret.key").read_text() == first_key


def test_env_var_takes_precedence_over_key_file(
    keyless_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    env_key = Fernet.generate_key().decode()
    monkeypatch.setenv("PNPB_SECRET_KEY", env_key)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
    assert get_settings().secret_key == env_key
    assert not (keyless_env / "secret.key").exists()
