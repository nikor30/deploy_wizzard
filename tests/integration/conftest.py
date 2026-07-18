"""Integration fixtures: the real app + real clients against the mock stack
served over real HTTP (uvicorn in a background thread)."""

import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from tests.mocks.stack import create_stack


@pytest.fixture(scope="session")
def mock_stack_url() -> Iterator[str]:
    config = uvicorn.Config(create_stack(), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Mock stack failed to start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def mock(mock_stack_url: str) -> Iterator[httpx.Client]:
    """Fresh mock state per test; the client talks to the control API."""
    with httpx.Client(base_url=mock_stack_url, timeout=10) as control:
        control.post("/__mock__/reset").raise_for_status()
        yield control


@pytest.fixture
def configured_client(client: TestClient, mock: httpx.Client, mock_stack_url: str) -> TestClient:
    """App with credentials, webhook, and site mapping pointing at the mocks."""
    response = client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": f"{mock_stack_url}/ccc", "username": "admin", "secret": "pw"},
            "netbox": {"base_url": f"{mock_stack_url}/netbox", "secret": "nb-token"},
            "webhook": {
                "base_url": f"{mock_stack_url}/ise/hook",
                "secret": "hmac-secret",
                "enabled": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    response = client.put(
        "/api/mappings/sites",
        json={
            "mappings": [
                {
                    "netbox_site_id": 10,
                    "netbox_site_name": "FFM-DC1",
                    "ccc_site_id": "uuid-ffm",
                    "ccc_site_name": "Global/Germany/Frankfurt/DC1",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    return client
