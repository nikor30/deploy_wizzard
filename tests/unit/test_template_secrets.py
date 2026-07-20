"""Template secrets: encrypted store, masked API, secret.<NAME> variables.

The plaintext must exist in exactly one place outside the DB: the deploy
payload sent to CCC. UI, job records, logs, and API responses only ever see
the mask.
"""

import respx
from app.services.dayn import resolve_variables
from app.services.suggest import suggest_variable_mappings
from fastapi.testclient import TestClient

CCC = "https://ccc.example.com"
NETBOX = "https://netbox.example.com"


# --- resolver ----------------------------------------------------------------


def test_resolver_maps_secret_paths_without_exposing_values() -> None:
    result = resolve_variables(
        ["RADIUS_KEY", "HOSTNAME"],
        {"RADIUS_KEY": "secret.radius_key", "HOSTNAME": "device.name"},
        {"device": {"name": "sw-1"}},
        secret_names=["radius_key"],
    )
    assert result["RADIUS_KEY"] == {"value": "****", "source": "secret", "secret": "radius_key"}
    assert result["HOSTNAME"] == {"value": "sw-1", "source": "mapped"}


def test_resolver_auto_applies_secret_by_matching_name() -> None:
    """A secret whose name matches a template variable auto-fills it — a global
    variable set once, no explicit secret.<name> mapping needed."""
    result = resolve_variables(
        ["RADIUS_KEY", "OTHER"],
        {},  # no explicit mappings
        {"device": {}},
        secret_names=["radius_key"],
    )
    assert result["RADIUS_KEY"] == {"value": "****", "source": "secret", "secret": "radius_key"}
    assert result["OTHER"]["source"] == "manual"


def test_resolver_unknown_secret_falls_back_to_manual() -> None:
    result = resolve_variables(
        ["RADIUS_KEY"],
        {"RADIUS_KEY": "secret.missing"},
        {"device": {}},
        secret_names=["radius_key"],
    )
    assert result["RADIUS_KEY"]["source"] == "manual"
    assert result["RADIUS_KEY"]["value"] is None


# --- suggestions -------------------------------------------------------------


def test_suggestions_include_secret_names() -> None:
    device = {"id": 1, "name": "sw-1", "serial": "SN1"}
    result = suggest_variable_mappings(
        ["RADIUS_KEY", "SNMP_COMMUNITY"],
        device,
        secret_names=["radius_key", "snmp_community"],
    )
    assert result["RADIUS_KEY"]["source_path"] == "secret.radius_key"
    assert result["SNMP_COMMUNITY"]["source_path"] == "secret.snmp_community"


# --- API ---------------------------------------------------------------------


def test_secrets_api_roundtrip_masked_and_deletable(client: TestClient) -> None:
    assert client.get("/api/settings/secrets").json() == []

    put = client.put("/api/settings/secrets/RADIUS_KEY", json={"secret": "super-radius-123"})
    assert put.status_code == 200
    assert "super-radius-123" not in put.text

    (listed,) = client.get("/api/settings/secrets").json()
    assert listed["name"] == "RADIUS_KEY"
    assert listed["secret_masked"] == "****-123"

    # upsert replaces the value
    client.put("/api/settings/secrets/RADIUS_KEY", json={"secret": "rotated-9999"})
    (listed,) = client.get("/api/settings/secrets").json()
    assert listed["secret_masked"] == "****9999"

    assert client.delete("/api/settings/secrets/RADIUS_KEY").status_code == 204
    assert client.get("/api/settings/secrets").json() == []
    assert client.delete("/api/settings/secrets/RADIUS_KEY").status_code == 404


def test_secrets_api_rejects_empty_values(client: TestClient) -> None:
    assert client.put("/api/settings/secrets/X", json={"secret": ""}).status_code == 422


# --- deploy ------------------------------------------------------------------


def _store_credentials(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
        },
    )


def _make_dayn_ready_device(client: TestClient) -> tuple[int, int]:
    """Job with one device that finished Day-0 and has resolved variables."""
    from app.db.models import JobDevice
    from app.db.session import open_session

    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "SN1", "pid": None, "ccc_device_id": "pnp-1"}]},
    ).json()["id"]
    device_id = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]["id"]
    with open_session() as db:
        device = db.get(JobDevice, device_id)
        assert device is not None
        device.state = "success"
        device.mgmt_ip = "172.20.10.5/24"
        device.dayn_variables = {
            "RADIUS_KEY": {"value": "****", "source": "secret", "secret": "radius_key"},
            "HOSTNAME": {"value": "sw-1", "source": "mapped"},
        }
    return job_id, device_id


def test_deploy_sends_plaintext_to_ccc_but_keeps_job_masked(client: TestClient) -> None:
    _store_credentials(client)
    client.put("/api/settings/secrets/radius_key", json={"secret": "radius-plaintext-77"})
    job_id, _device_id = _make_dayn_ready_device(client)

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        deploy_route = respx_mock.post(
            f"{CCC}/dna/intent/api/v1/template-programmer/template/deploy/v2"
        ).respond(200, json={"response": {"taskId": "task-1"}})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/task/task-1").respond(
            200, json={"response": {"taskId": "task-1", "isError": False, "endTime": 1}}
        )
        respx_mock.patch(f"{NETBOX}/api/dcim/devices/", params=None).respond(200, json={})
        response = client.post(
            f"/api/wizard/jobs/{job_id}/dayn/deploy",
            json={"template_id": "tpl-1", "poll_interval": 0.01, "task_timeout": 5},
        )
    assert response.status_code == 200, response.text

    import json

    deploy_body = json.loads(deploy_route.calls[0].request.content)
    params = deploy_body["targetInfo"][0]["params"]
    assert params["RADIUS_KEY"] == "radius-plaintext-77"
    assert params["HOSTNAME"] == "sw-1"

    # job record + API keep the mask, never the plaintext
    job = client.get(f"/api/wizard/jobs/{job_id}")
    assert "radius-plaintext-77" not in job.text
    assert job.json()["devices"][0]["dayn_variables"]["RADIUS_KEY"]["value"] == "****"


def test_deploy_fails_actionably_when_secret_was_deleted(client: TestClient) -> None:
    _store_credentials(client)
    job_id, _device_id = _make_dayn_ready_device(client)  # secret never stored
    response = client.post(
        f"/api/wizard/jobs/{job_id}/dayn/deploy",
        json={"template_id": "tpl-1", "poll_interval": 0.01, "task_timeout": 5},
    )
    assert response.status_code == 422
    assert "radius_key" in response.json()["detail"]
