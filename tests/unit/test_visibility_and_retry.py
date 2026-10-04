"""Code review, package 6: nothing may fail silently, and a webhook retry sends once.

- an exception escaping a per-device task was dropped by gather(return_exceptions)
  without a log line, leaving the device mid-run
- a webhook that cannot even be prepared (secret no longer decrypts) left no
  delivery record, so it could not be retried from the Logs page
- the Logs page showed an unhandled error as a bare path, without type or traceback
- a webhook retry re-sent delivered events, ignored "disabled", and ran twice on a
  double click
"""

import app.api.wizard as wizard_module
import app.clients.webhook as webhook_module
import app.services.day0 as day0_module
import app.services.dayn as dayn_module
import pytest
import respx
from app.db.models import ServiceSettings, WebhookDelivery
from app.db.session import open_session
from app.logging_setup import flush_db_sink
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.unit.test_day0_service import HOOK, _mock_ccc, _pnp_state, _setup
from tests.unit.test_dayn_service import _manual_for_all, _prepare, _run_day0, _store_dayn_mapping
from tests.unit.test_logs_and_stats import _day0_with_failed_webhook

FAST = {"poll_interval": 0, "timeout": 5}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def _errors(client: TestClient, text: str) -> list[dict[str, object]]:
    flush_db_sink()
    page = client.get("/api/logs", params={"level": "error", "q": text}).json()
    return list(page["entries"])


# --- per-device exceptions ------------------------------------------------------


def test_unexpected_day0_task_error_is_logged_and_never_strands_the_device(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _setup(client)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(day0_module, "_claim_one", boom)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})

    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "day0_failed"
    assert all(d["state"] == "failed" for d in job["devices"])
    assert all("worker exploded" in (d["error"] or "") for d in job["devices"])
    assert _errors(client, "Unexpected Day-0 error")


def test_unexpected_dayn_task_error_is_logged_and_never_strands_the_device(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _run_day0(client)
    _store_dayn_mapping(client)
    _prepare(client, job_id)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("deploy worker exploded")

    monkeypatch.setattr(dayn_module, "_deploy_one", boom)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        client.post(
            f"/api/wizard/jobs/{job_id}/dayn/deploy",
            json={
                "template_id": "tmpl-N",
                "manual": _manual_for_all(client, job_id),
                "poll_interval": 0,
                "task_timeout": 5,
            },
        )
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert job["status"] == "dayn_failed"
    assert all(d["state"] == "dayn_failed" for d in job["devices"])
    assert _errors(client, "Unexpected Day-N error")


def test_webhook_that_cannot_be_prepared_is_recorded_for_retry(client: TestClient) -> None:
    job_id = _setup(client)
    with open_session() as db:  # e.g. the key changed since the secret was saved
        row = db.scalar(select(ServiceSettings).where(ServiceSettings.service == "webhook"))
        assert row is not None
        row.secret_encrypted = "not-a-fernet-token"
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        hook = respx_mock.post(HOOK).respond(200)
        client.post(f"/api/wizard/jobs/{job_id}/claim", json={"config_id": "t", **FAST})

    assert not hook.called
    job = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert all(d["state"] == "success" for d in job["devices"])  # the claim stands
    deliveries = client.get("/api/logs/webhook-deliveries", params={"job_id": job_id}).json()
    assert len(deliveries) == 2
    assert all(d["status"] == "failed" for d in deliveries)
    assert all("decrypt" in (d["last_error"] or "") for d in deliveries)


# --- Logs page detail ------------------------------------------------------------


def test_unhandled_error_reaches_the_logs_page_with_type_and_traceback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise KeyError("deviceInfo")

    monkeypatch.setattr(wizard_module, "get_catalyst_client", broken)
    tolerant = TestClient(client.app, raise_server_exceptions=False)
    assert tolerant.get("/api/wizard/pnp-devices").status_code == 500

    (entry, *_) = _errors(client, "/api/wizard/pnp-devices")
    assert "KeyError" in str(entry["message"])
    context = entry["context"]
    assert isinstance(context, dict)
    assert "Traceback" in str(context.get("exc_info"))


# --- webhook retry -----------------------------------------------------------------


def test_retry_of_a_delivered_event_is_refused(client: TestClient) -> None:
    job_id = _day0_with_failed_webhook(client)
    target = client.get("/api/logs/webhook-deliveries", params={"job_id": job_id}).json()[0]
    with open_session() as db:
        row = db.get(WebhookDelivery, target["id"])
        assert row is not None
        row.status = "delivered"
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        hook = respx_mock.post(HOOK).respond(200)
        response = client.post(f"/api/logs/webhook-deliveries/{target['id']}/retry")
    assert response.status_code == 409
    assert not hook.called


def test_retry_in_progress_is_not_sent_twice(client: TestClient) -> None:
    job_id = _day0_with_failed_webhook(client)
    target = client.get("/api/logs/webhook-deliveries", params={"job_id": job_id}).json()[0]
    with open_session() as db:  # another request is sending it right now
        row = db.get(WebhookDelivery, target["id"])
        assert row is not None
        row.status = "retrying"
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        hook = respx_mock.post(HOOK).respond(200)
        response = client.post(f"/api/logs/webhook-deliveries/{target['id']}/retry")
    assert response.status_code == 409
    assert not hook.called


def test_retry_while_the_webhook_is_disabled_is_refused(client: TestClient) -> None:
    job_id = _day0_with_failed_webhook(client)
    target = client.get("/api/logs/webhook-deliveries", params={"job_id": job_id}).json()[0]
    with open_session() as db:
        row = db.scalar(select(ServiceSettings).where(ServiceSettings.service == "webhook"))
        assert row is not None
        row.enabled = False
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        hook = respx_mock.post(HOOK).respond(200)
        response = client.post(f"/api/logs/webhook-deliveries/{target['id']}/retry")
    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
    assert not hook.called
