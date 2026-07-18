"""Full-stack integration: real app + real clients against the mock CCC/
NetBox/ISE servers over HTTP. Covers the happy path Step 1→5 and the key
failure paths from CLAUDE.md §8."""

from typing import Any

import app.clients.base as base
import app.clients.webhook as webhook
import httpx
import pytest
from fastapi.testclient import TestClient

FAST = {"poll_interval": 0.02, "timeout": 10}
DAYN_FAST = {"poll_interval": 0.02, "task_timeout": 10}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(webhook, "BACKOFF_BASE_SECONDS", 0)


def _create_matched_job(client: TestClient) -> dict[str, Any]:
    devices = client.get("/api/wizard/pnp-devices").json()
    assert devices, "mock stack returned no unclaimed PnP devices"
    job_id = client.post(
        "/api/wizard/jobs",
        json={
            "devices": [
                {"serial": d["serial"], "pid": d["pid"], "ccc_device_id": d["ccc_device_id"]}
                for d in devices
            ]
        },
    ).json()["id"]
    matched = client.post(f"/api/wizard/jobs/{job_id}/match")
    assert matched.status_code == 200, matched.text
    return dict(matched.json())


def _deploy_dayn(
    client: TestClient, job: dict[str, Any], manual: dict[str, dict[str, str]]
) -> dict[str, Any]:
    deployed = client.post(
        f"/api/wizard/jobs/{job['id']}/dayn/deploy",
        json={"template_id": "tpl-dayn", "manual": manual, **DAYN_FAST},
    )
    assert deployed.status_code == 200, deployed.text
    return dict(client.get(f"/api/wizard/jobs/{job['id']}").json())


def test_full_happy_path_step1_to_step5(configured_client: TestClient, mock: httpx.Client) -> None:
    client = configured_client
    job = _create_matched_job(client)
    assert all(d["match_status"] == "matched" for d in job["devices"])

    # Step 3 — Day-0 claim (TestClient blocks until the background task ends)
    claimed = client.post(
        f"/api/wizard/jobs/{job['id']}/claim", json={"config_id": "tpl-day0", **FAST}
    )
    assert claimed.status_code == 200, claimed.text
    job = client.get(f"/api/wizard/jobs/{job['id']}").json()
    assert job["status"] == "day0_complete"
    assert all(d["state"] == "success" for d in job["devices"])

    snapshot = mock.get("/__mock__/state").json()
    assert len(snapshot["claims"]) == len(job["devices"])
    claim = snapshot["claims"][0]
    assert claim["siteId"] == "uuid-ffm"
    parameters = {p["key"]: p["value"] for p in claim["configInfo"]["configParameters"]}
    assert parameters["HOSTNAME"].startswith("sw-ffm-")
    assert parameters["MGMT_IP"].startswith("172.20.10.")

    # ISE webhook fired per device, HMAC-signed
    assert len(snapshot["deliveries"]) == len(job["devices"])
    delivery = snapshot["deliveries"][0]
    assert delivery["payload"]["event"] == "day0_success"
    assert delivery["signature"], "webhook must be HMAC-signed when a secret is set"

    # Step 4 — Day-N: SNMP_LOCATION + NTP_SERVER resolve, CONTACT stays manual
    client.put(
        "/api/settings/dayn",
        json={
            "mappings": [
                {"variable": "SNMP_LOCATION", "source_path": "device.custom_fields.snmp_location"},
                {"variable": "NTP_SERVER", "source_path": "device.config_context.ntp_server"},
            ]
        },
    )
    prepared = client.post(
        f"/api/wizard/jobs/{job['id']}/dayn/prepare", json={"template_id": "tpl-dayn"}
    )
    assert prepared.status_code == 200, prepared.text
    variables = prepared.json()["devices"][0]["dayn_variables"]
    assert variables["SNMP_LOCATION"] == {"value": "FFM DC1 / Rack 4", "source": "mapped"}
    assert variables["NTP_SERVER"] == {"value": "10.0.0.1", "source": "mapped"}
    assert variables["CONTACT"]["source"] == "manual"

    # Manual value required before deploying
    incomplete = client.post(
        f"/api/wizard/jobs/{job['id']}/dayn/deploy", json={"template_id": "tpl-dayn", **DAYN_FAST}
    )
    assert incomplete.status_code == 422

    manual = {str(d["id"]): {"CONTACT": "noc@example.com"} for d in prepared.json()["devices"]}
    job = _deploy_dayn(client, job, manual)

    # Step 5 — job complete, NetBox devices switched to active
    assert job["status"] == "completed"
    assert all(d["state"] == "completed" for d in job["devices"])
    statuses = mock.get("/__mock__/state").json()["netbox_statuses"]
    assert set(statuses.values()) == {"active"}


def test_half_failed_batch_is_isolated(configured_client: TestClient, mock: httpx.Client) -> None:
    """One device failing onboarding must not abort or roll back its sibling."""
    client = configured_client
    mock.post("/__mock__/config", json={"fail_onboarding_serials": ["SN000002"]})
    job = _create_matched_job(client)
    client.post(f"/api/wizard/jobs/{job['id']}/claim", json={"config_id": "tpl-day0", **FAST})
    job = client.get(f"/api/wizard/jobs/{job['id']}").json()
    assert job["status"] == "day0_partial"
    by_serial = {d["serial"]: d for d in job["devices"]}
    assert by_serial["SN000001"]["state"] == "success"
    assert by_serial["SN000002"]["state"] == "failed"
    assert "onboarding failed" in by_serial["SN000002"]["error"]
    # webhook fired only for the successful sibling
    deliveries = mock.get("/__mock__/state").json()["deliveries"]
    assert [d["payload"]["device"]["serial"] for d in deliveries] == ["SN000001"]


def test_webhook_failure_does_not_roll_back_claim(
    configured_client: TestClient, mock: httpx.Client
) -> None:
    client = configured_client
    mock.post("/__mock__/config", json={"ise_fail": True})
    job = _create_matched_job(client)
    client.post(f"/api/wizard/jobs/{job['id']}/claim", json={"config_id": "tpl-day0", **FAST})
    job = client.get(f"/api/wizard/jobs/{job['id']}").json()
    assert job["status"] == "day0_complete"
    assert all(d["state"] == "success" for d in job["devices"])
    # delivery recorded as failed and retryable from the Logs page
    failed = client.get("/api/logs/webhook-deliveries").json()
    assert failed and all(d["status"] == "failed" for d in failed)


def test_dayn_task_error_is_drilled_from_task_tree(
    configured_client: TestClient, mock: httpx.Client
) -> None:
    """CCC buries deploy errors in the task tree when failureReason is empty."""
    client = configured_client
    job = _create_matched_job(client)
    client.post(f"/api/wizard/jobs/{job['id']}/claim", json={"config_id": "tpl-day0", **FAST})
    mock.post("/__mock__/config", json={"dayn_task_fail": True})
    prepared = client.post(
        f"/api/wizard/jobs/{job['id']}/dayn/prepare", json={"template_id": "tpl-dayn"}
    )
    manual = {
        str(d["id"]): {
            v: "x" for v, info in d["dayn_variables"].items() if info["source"] == "manual"
        }
        for d in prepared.json()["devices"]
    }
    job = _deploy_dayn(client, client.get(f"/api/wizard/jobs/{job['id']}").json(), manual)
    assert job["status"] == "dayn_failed"
    assert all("config apply failed" in d["error"] for d in job["devices"])
    # NetBox must NOT have been touched (§11)
    statuses = mock.get("/__mock__/state").json()["netbox_statuses"]
    assert set(statuses.values()) == {"planned"}


def test_netbox_patch_failure_after_dayn_success_is_partial(
    configured_client: TestClient, mock: httpx.Client
) -> None:
    client = configured_client
    job = _create_matched_job(client)
    client.post(f"/api/wizard/jobs/{job['id']}/claim", json={"config_id": "tpl-day0", **FAST})
    mock.post("/__mock__/config", json={"netbox_patch_fail": True})
    prepared = client.post(
        f"/api/wizard/jobs/{job['id']}/dayn/prepare", json={"template_id": "tpl-dayn"}
    )
    manual = {
        str(d["id"]): {
            v: "x" for v, info in d["dayn_variables"].items() if info["source"] == "manual"
        }
        for d in prepared.json()["devices"]
    }
    job = _deploy_dayn(client, client.get(f"/api/wizard/jobs/{job['id']}").json(), manual)
    assert job["status"] == "partial_success"
    assert all(d["state"] == "activate_failed" for d in job["devices"])


def test_ccc_5xx_is_retried_through_the_real_stack(
    configured_client: TestClient, mock: httpx.Client
) -> None:
    mock.post("/__mock__/config", json={"fail_next_ccc_gets": 2})
    devices = configured_client.get("/api/wizard/pnp-devices")
    assert devices.status_code == 200
    assert len(devices.json()) == 2
