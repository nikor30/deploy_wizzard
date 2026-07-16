import respx
from fastapi.testclient import TestClient

MAPPINGS = {
    "mappings": [
        {
            "netbox_site_id": 1,
            "netbox_site_name": "FFM-DC1",
            "ccc_site_id": "uuid-1",
            "ccc_site_name": "Global/Germany/Frankfurt/DC1",
        },
        {
            "netbox_site_id": 2,
            "netbox_site_name": "BER-DC1",
            "ccc_site_id": "uuid-2",
            "ccc_site_name": "Global/Germany/Berlin/DC1",
        },
    ]
}


def test_put_then_get_roundtrip(client: TestClient) -> None:
    put = client.put("/api/mappings/sites", json=MAPPINGS)
    assert put.status_code == 200
    body = client.get("/api/mappings/sites").json()
    assert len(body["mappings"]) == 2
    assert body["mappings"][0]["netbox_site_name"] == "BER-DC1"  # sorted by name


def test_put_replaces_previous_mappings(client: TestClient) -> None:
    client.put("/api/mappings/sites", json=MAPPINGS)
    single = {"mappings": [MAPPINGS["mappings"][0]]}
    client.put("/api/mappings/sites", json=single)
    body = client.get("/api/mappings/sites").json()
    assert len(body["mappings"]) == 1
    assert body["mappings"][0]["netbox_site_id"] == 1


def test_duplicate_netbox_site_rejected(client: TestClient) -> None:
    payload = {"mappings": [MAPPINGS["mappings"][0], MAPPINGS["mappings"][0]]}
    response = client.put("/api/mappings/sites", json=payload)
    assert response.status_code == 422
    assert "Duplicate" in response.json()["detail"]


def test_sources_without_credentials_return_400(client: TestClient) -> None:
    for source in ("netbox", "ccc"):
        response = client.get(f"/api/mappings/sources/{source}")
        assert response.status_code == 400
        assert "not configured" in response.json()["detail"]


def _store_credentials(client: TestClient) -> None:
    client.put(
        "/api/settings/credentials",
        json={
            "catalyst": {
                "base_url": "https://ccc.example.com",
                "username": "admin",
                "secret": "pw",
            },
            "netbox": {"base_url": "https://netbox.example.com", "secret": "tok"},
        },
    )


@respx.mock(assert_all_called=False)
def test_netbox_sources_use_stored_credentials(
    respx_mock: respx.MockRouter, client: TestClient
) -> None:
    respx_mock.route(host="testserver").pass_through()
    route = respx_mock.get("https://netbox.example.com/api/dcim/sites/").respond(
        200,
        json={"results": [{"id": 1, "name": "FFM-DC1", "slug": "ffm-dc1"}], "next": None},
    )
    _store_credentials(client)
    response = client.get("/api/mappings/sources/netbox")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "FFM-DC1", "slug": "ffm-dc1"}]
    assert route.calls[0].request.headers["Authorization"] == "Token tok"


@respx.mock(assert_all_called=False)
def test_ccc_sources_use_stored_credentials(
    respx_mock: respx.MockRouter, client: TestClient
) -> None:
    respx_mock.route(host="testserver").pass_through()
    respx_mock.post("https://ccc.example.com/dna/system/api/v1/auth/token").respond(
        200, json={"Token": "tok"}
    )
    respx_mock.get("https://ccc.example.com/dna/intent/api/v1/site").respond(
        200,
        json={"response": [{"id": "uuid-1", "siteNameHierarchy": "Global/Germany/Frankfurt/DC1"}]},
    )
    _store_credentials(client)
    response = client.get("/api/mappings/sources/ccc")
    assert response.status_code == 200
    assert response.json() == [{"id": "uuid-1", "name_hierarchy": "Global/Germany/Frankfurt/DC1"}]
