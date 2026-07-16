from fastapi.testclient import TestClient

PAYLOAD = {
    "catalyst": {
        "base_url": "https://ccc.example.com",
        "username": "admin",
        "secret": "catalyst-password-9999",
        "tls_verify": False,
    },
    "netbox": {
        "base_url": "https://netbox.example.com",
        "secret": "netbox-token-8888",
    },
    "webhook": {
        "base_url": "https://ise-helper.example.com/hook",
        "secret": "hmac-shared-7777",
        "enabled": True,
    },
}


def test_put_then_get_returns_masked_secrets(client: TestClient) -> None:
    put = client.put("/api/settings/credentials", json=PAYLOAD)
    assert put.status_code == 200

    body = client.get("/api/settings/credentials").json()
    assert body["catalyst"]["secret_masked"] == "****9999"
    assert body["netbox"]["secret_masked"] == "****8888"
    assert body["webhook"]["secret_masked"] == "****7777"
    assert body["catalyst"]["tls_verify"] is False
    assert body["catalyst"]["configured"] is True


def test_secret_never_appears_in_responses(client: TestClient) -> None:
    put_text = client.put("/api/settings/credentials", json=PAYLOAD).text
    get_text = client.get("/api/settings/credentials").text
    for secret in ("catalyst-password-9999", "netbox-token-8888", "hmac-shared-7777"):
        assert secret not in put_text
        assert secret not in get_text


def test_omitted_secret_keeps_stored_value(client: TestClient) -> None:
    client.put("/api/settings/credentials", json=PAYLOAD)
    update = {"catalyst": {"base_url": "https://ccc2.example.com", "username": "admin2"}}
    client.put("/api/settings/credentials", json=update)

    body = client.get("/api/settings/credentials").json()
    assert body["catalyst"]["base_url"] == "https://ccc2.example.com"
    assert body["catalyst"]["secret_masked"] == "****9999"


def test_empty_secret_clears_stored_value(client: TestClient) -> None:
    client.put("/api/settings/credentials", json=PAYLOAD)
    client.put(
        "/api/settings/credentials", json={"netbox": {"base_url": "https://n", "secret": ""}}
    )

    body = client.get("/api/settings/credentials").json()
    assert body["netbox"]["secret_masked"] is None


def test_unconfigured_services_report_configured_false(client: TestClient) -> None:
    body = client.get("/api/settings/credentials").json()
    for service in ("catalyst", "netbox", "webhook"):
        assert body[service]["configured"] is False
        assert body[service]["secret_masked"] is None


def test_connection_test_without_base_url_returns_422(client: TestClient) -> None:
    response = client.post("/api/settings/credentials/netbox/test", json={})
    assert response.status_code == 422
