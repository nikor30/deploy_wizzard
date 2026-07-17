import respx
from fastapi.testclient import TestClient

CCC = "https://ccc.example.com"
NETBOX = "https://netbox.example.com"


def _store_credentials(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {"base_url": CCC, "username": "admin", "secret": "pw"},
            "netbox": {"base_url": NETBOX, "secret": "tok"},
        },
    )


def _store_mapping(client: TestClient) -> None:
    client.put(
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


def _mock_netbox(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{NETBOX}/api/dcim/devices/").respond(
        200,
        json={
            "results": [
                {
                    "id": 1,
                    "name": "sw-ffm-01",
                    "serial": " fcw1234abcd ",
                    "site": {"id": 10, "name": "FFM-DC1"},
                    "primary_ip4": {"address": "172.20.10.5/24"},
                    "status": {"value": "planned"},
                }
            ],
            "next": None,
        },
    )
    respx_mock.get(f"{NETBOX}/api/ipam/vlans/").respond(
        200,
        json={"results": [{"id": 5, "vid": 110, "name": "MGMT"}], "next": None},
    )


def test_pnp_devices_lists_unclaimed(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "tok"})
        pnp = respx_mock.get(f"{CCC}/dna/intent/api/v1/onboarding/pnp-device").respond(
            200,
            json={
                "response": [
                    {
                        "id": "pnp-1",
                        "deviceInfo": {
                            "serialNumber": "FCW1234ABCD",
                            "pid": "C9300-48P",
                            "state": "Unclaimed",
                            "lastContact": 1752675000000,
                        },
                    }
                ]
            },
        )
        _store_credentials(client)
        response = client.get("/api/wizard/pnp-devices")
    assert response.status_code == 200
    (device,) = response.json()
    assert device["serial"] == "FCW1234ABCD"
    assert device["pid"] == "C9300-48P"
    assert pnp.calls[0].request.url.params["state"] == "Unclaimed"


def test_job_lifecycle_create_get_resume(client: TestClient) -> None:
    created = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "FCW1234ABCD", "pid": "C9300-48P", "ccc_device_id": "pnp-1"}]},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert created.json()["device_count"] == 1

    fetched = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert fetched["devices"][0]["serial"] == "FCW1234ABCD"
    assert fetched["devices"][0]["match_status"] is None

    jobs = client.get("/api/wizard/jobs").json()
    assert [j["id"] for j in jobs] == [job_id]


def test_empty_job_rejected(client: TestClient) -> None:
    assert client.post("/api/wizard/jobs", json={"devices": []}).status_code == 422


def test_match_persists_results_and_survives_reload(client: TestClient) -> None:
    _store_credentials(client)
    _store_mapping(client)
    job_id = client.post(
        "/api/wizard/jobs",
        json={
            "devices": [
                {"serial": "FCW1234ABCD", "pid": "C9300-48P", "ccc_device_id": "pnp-1"},
                {"serial": "UNKNOWN999", "pid": "C9200-24T", "ccc_device_id": "pnp-2"},
            ]
        },
    ).json()["id"]

    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_netbox(respx_mock)
        matched = client.post(f"/api/wizard/jobs/{job_id}/match")
    assert matched.status_code == 200
    first, second = matched.json()["devices"]
    assert first["match_status"] == "matched"
    assert first["netbox_name"] == "sw-ffm-01"
    assert first["ccc_site_name"] == "Global/Germany/Frankfurt/DC1"
    assert first["mgmt_ip"] == "172.20.10.5/24"
    assert first["vlan_options"] == [{"id": 5, "vid": 110, "name": "MGMT"}]
    assert second["match_status"] == "unmatched"

    # Resume: results are persisted, no NetBox call needed
    reloaded = client.get(f"/api/wizard/jobs/{job_id}").json()
    assert reloaded["devices"][0]["match_status"] == "matched"
    assert reloaded["current_step"] == 2


def test_vlan_selection_validated_against_site_options(client: TestClient) -> None:
    _store_credentials(client)
    _store_mapping(client)
    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "FCW1234ABCD", "pid": None, "ccc_device_id": "pnp-1"}]},
    ).json()["id"]
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        _mock_netbox(respx_mock)
        client.post(f"/api/wizard/jobs/{job_id}/match")
    device_id = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]["id"]

    ok = client.put(f"/api/wizard/jobs/{job_id}/devices/{device_id}", json={"mgmt_vlan": 110})
    assert ok.status_code == 200
    assert ok.json()["mgmt_vlan"] == 110

    bad = client.put(f"/api/wizard/jobs/{job_id}/devices/{device_id}", json={"mgmt_vlan": 999})
    assert bad.status_code == 422


def test_vlan_on_unmatched_device_rejected(client: TestClient) -> None:
    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "X", "pid": None, "ccc_device_id": "pnp-9"}]},
    ).json()["id"]
    device_id = client.get(f"/api/wizard/jobs/{job_id}").json()["devices"][0]["id"]
    response = client.put(f"/api/wizard/jobs/{job_id}/devices/{device_id}", json={"mgmt_vlan": 110})
    assert response.status_code == 422


def test_pnp_source_ip_fallbacks(client: TestClient) -> None:
    """Live CCC puts the source IP in httpHeaders/ipInterfaces, not ipAddress."""
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.route(host="testserver").pass_through()
        respx_mock.post(f"{CCC}/dna/system/api/v1/auth/token").respond(200, json={"Token": "t"})
        respx_mock.get(f"{CCC}/dna/intent/api/v1/onboarding/pnp-device").respond(
            200,
            json=[
                {
                    "id": "pnp-1",
                    "deviceInfo": {
                        "serialNumber": "SN-HEADERS",
                        "httpHeaders": [
                            {"key": "user-agent", "value": "pnp"},
                            {"key": "clientAddress", "value": "172.20.99.11"},
                        ],
                    },
                },
                {
                    "id": "pnp-2",
                    "deviceInfo": {
                        "serialNumber": "SN-IFACES",
                        "ipInterfaces": [{"ipv4Address": "172.20.99.12"}],
                    },
                },
                {
                    "id": "pnp-3",
                    "deviceInfo": {"serialNumber": "SN-PLAIN", "ipAddress": "172.20.99.13"},
                },
                {"id": "pnp-4", "deviceInfo": {"serialNumber": "SN-NONE"}},
            ],
        )
        _store_credentials(client)
        devices = client.get("/api/wizard/pnp-devices").json()
    by_serial = {d["serial"]: d["ip_address"] for d in devices}
    assert by_serial["SN-HEADERS"] == "172.20.99.11"
    assert by_serial["SN-IFACES"] == "172.20.99.12"
    assert by_serial["SN-PLAIN"] == "172.20.99.13"
    assert by_serial["SN-NONE"] is None


def test_delete_job_removes_job_and_devices(client: TestClient) -> None:
    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "FCW1", "pid": None, "ccc_device_id": "pnp-1"}]},
    ).json()["id"]
    assert client.delete(f"/api/wizard/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/wizard/jobs/{job_id}").status_code == 404
    assert client.get("/api/wizard/jobs").json() == []


def test_delete_running_job_rejected(client: TestClient) -> None:
    from app.db.models import Job
    from app.db.session import open_session

    job_id = client.post(
        "/api/wizard/jobs",
        json={"devices": [{"serial": "FCW1", "pid": None, "ccc_device_id": "pnp-1"}]},
    ).json()["id"]
    with open_session() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "day0_running"
    response = client.delete(f"/api/wizard/jobs/{job_id}")
    assert response.status_code == 409
    assert "running" in response.json()["detail"]


def test_delete_unknown_job_404(client: TestClient) -> None:
    assert client.delete("/api/wizard/jobs/999").status_code == 404
