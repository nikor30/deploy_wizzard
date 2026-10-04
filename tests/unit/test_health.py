from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_health_reports_the_released_version(client: TestClient) -> None:
    """The version identifies a running instance (e.g. which fixes a live system has).
    It came from a hard-coded `__version__ = "1.1.0"` that stopped being updated
    long ago - every release since reported 1.1.0."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    released = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert client.get("/api/health").json()["version"] == released
    assert client.get("/openapi.json").json()["info"]["version"] == released
