"""DB log sink, logs API, webhook retry, retention, stats aggregation."""

import logging
from datetime import UTC, datetime, timedelta

import app.clients.webhook as webhook_module
import pytest
import respx
from app.db.models import Job, JobDevice, LogEntry
from app.db.session import open_session
from app.logging_setup import flush_db_sink
from app.services.stats import categorize_error, cleanup_old_logs
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.unit.test_day0_service import HOOK, _mock_ccc, _pnp_state, _setup


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "BACKOFF_BASE_SECONDS", 0)


def test_db_sink_stores_redacted_app_logs_only(client: TestClient) -> None:
    logging.getLogger("app.test").info(
        "storing credentials",
        extra={"job_id": 7, "serial": "FCW1", "password": "hunter2", "request": {"token": "x"}},
    )
    logging.getLogger("sqlalchemy.engine").info("SELECT should not be stored")
    flush_db_sink()

    response = client.get("/api/logs", params={"component": "app.test"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    (entry,) = body["entries"]
    assert entry["message"] == "storing credentials"
    assert entry["job_id"] == 7
    assert entry["device_serial"] == "FCW1"
    assert entry["context"]["password"] == "[REDACTED]"
    assert entry["context"]["request"]["token"] == "[REDACTED]"
    assert "hunter2" not in response.text

    engine_logs = client.get("/api/logs", params={"q": "SELECT should not"}).json()
    assert engine_logs["total"] == 0


def test_logs_api_filters(client: TestClient) -> None:
    log = logging.getLogger("app.day0")
    log.info("claim ok", extra={"job_id": 1, "serial": "AAA"})
    log.error("claim failed", extra={"job_id": 2, "serial": "BBB"})
    logging.getLogger("app.webhook").warning("delivery slow", extra={"job_id": 2})
    flush_db_sink()

    assert client.get("/api/logs", params={"job_id": 2}).json()["total"] == 2
    assert client.get("/api/logs", params={"level": "error"}).json()["total"] == 1
    assert client.get("/api/logs", params={"serial": "AAA"}).json()["total"] == 1
    assert client.get("/api/logs", params={"q": "delivery"}).json()["total"] == 1
    assert client.get("/api/logs", params={"component": "app.day0"}).json()["total"] == 2
    page = client.get("/api/logs", params={"limit": 1}).json()
    assert page["total"] == 3
    assert len(page["entries"]) == 1


def test_retention_deletes_only_old_entries(client: TestClient) -> None:
    logging.getLogger("app.test").info("fresh entry")
    flush_db_sink()
    with open_session() as db:
        db.add(
            LogEntry(
                timestamp=datetime.now(tz=UTC) - timedelta(days=120),
                level="INFO",
                component="app.test",
                message="ancient entry",
            )
        )
    assert cleanup_old_logs(90) == 1
    body = client.get("/api/logs", params={"component": "app.test"}).json()
    assert body["total"] == 1
    assert body["entries"][0]["message"] == "fresh entry"


def _day0_with_failed_webhook(client: TestClient) -> int:
    job_id = _setup(client)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_ccc(respx_mock)
        _pnp_state(respx_mock, "pnp-1", {"state": "Provisioned"})
        _pnp_state(respx_mock, "pnp-2", {"state": "Provisioned"})
        respx_mock.post(HOOK).respond(500)
        client.post(
            f"/api/wizard/jobs/{job_id}/claim",
            json={"config_id": "tmpl-0", "poll_interval": 0, "timeout": 5},
        )
    return job_id


def test_webhook_retry_flow(client: TestClient) -> None:
    job_id = _day0_with_failed_webhook(client)
    deliveries = client.get("/api/logs/webhook-deliveries", params={"job_id": job_id}).json()
    assert len(deliveries) == 2
    assert all(d["status"] == "failed" for d in deliveries)
    target = deliveries[0]

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(HOOK).respond(200)
        retried = client.post(f"/api/logs/webhook-deliveries/{target['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "delivered"
    assert retried.json()["attempts"] > target["attempts"]

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(HOOK).respond(500)
        failed_again = client.post(f"/api/logs/webhook-deliveries/{deliveries[1]['id']}/retry")
    assert failed_again.json()["status"] == "failed"
    assert "500" in failed_again.json()["last_error"]


def test_retry_unknown_delivery_404(client: TestClient) -> None:
    assert client.post("/api/logs/webhook-deliveries/999/retry").status_code == 404


@pytest.mark.parametrize(
    ("error", "category"),
    [
        ("Device did not reach 'Provisioned' within 1800s", "timeout"),
        ("Catalyst Center rejected the credentials (HTTP 401)", "authentication"),
        ("NetBox request failed: HTTP 500", "netbox"),
        ("PnP onboarding failed (state=Error)", "catalyst"),
        (None, "unknown"),
        ("something weird", "other"),
    ],
)
def test_categorize_error(error: str | None, category: str) -> None:
    assert categorize_error(error) == category


def test_stats_aggregation(client: TestClient) -> None:
    now = datetime.now(tz=UTC)
    with open_session() as db:
        job = Job(status="partial_success", current_step=5)
        job.devices.append(
            JobDevice(
                serial="OK1",
                ccc_device_id="p1",
                match_status="matched",
                state="completed",
                day0_started_at=now,
                day0_finished_at=now + timedelta(seconds=60),
                dayn_started_at=now,
                dayn_finished_at=now + timedelta(seconds=120),
            )
        )
        job.devices.append(
            JobDevice(
                serial="BAD1",
                ccc_device_id="p2",
                match_status="matched",
                state="failed",
                error="Device did not reach 'Provisioned' within 5s",
                day0_started_at=now,
                day0_finished_at=now + timedelta(seconds=30),
            )
        )
        db.add(job)

    stats = client.get("/api/stats", params={"days": 7}).json()
    assert stats["totals"]["jobs"] == 1
    assert stats["totals"]["devices"] == 2
    assert stats["totals"]["claimed"] == 1
    assert stats["totals"]["provisioned"] == 1
    assert stats["totals"]["failed"] == 1
    assert stats["success_rate"] == 0.5
    assert stats["avg_day0_seconds"] == 45.0
    assert stats["avg_dayn_seconds"] == 120.0
    assert stats["failures_by_category"] == {"timeout": 1}
    assert len(stats["jobs_over_time"]) == 1
    assert stats["jobs_over_time"][0]["succeeded"] == 1
    assert stats["jobs_over_time"][0]["failed"] == 1


def test_stats_empty_window(client: TestClient) -> None:
    stats = client.get("/api/stats", params={"days": 1}).json()
    assert stats["totals"]["jobs"] == 0
    assert stats["success_rate"] is None


def _unused() -> None:  # keep the select import used for future filters
    select(LogEntry)
