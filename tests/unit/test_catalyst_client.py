from typing import Any

import app.clients.base as base
import httpx
import pytest
import respx
from app.clients.catalyst import PAGE_SIZE, PNP_ACTIONABLE_STATES, CatalystCenterClient
from app.errors import CatalystAuthError, CatalystError

BASE = "https://ccc.example.com"
TOKEN_URL = f"{BASE}/dna/system/api/v1/auth/token"
SITE_URL = f"{BASE}/dna/intent/api/v1/site"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "BACKOFF_BASE_SECONDS", 0)


def sites(count: int) -> dict[str, Any]:
    return {"response": [{"id": f"site-{i}", "siteName": f"Site {i}"} for i in range(count)]}


@respx.mock
async def test_token_fetch_and_header() -> None:
    route = respx.post(TOKEN_URL).respond(200, json={"Token": "tok-1"})
    site_route = respx.get(SITE_URL).respond(200, json=sites(1))
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_sites()
    assert route.called
    assert site_route.calls[0].request.headers["X-Auth-Token"] == "tok-1"


@respx.mock
async def test_bad_credentials_raise_auth_error() -> None:
    respx.post(TOKEN_URL).respond(401)
    async with CatalystCenterClient(BASE, "admin", "wrong") as client:
        with pytest.raises(CatalystAuthError, match="credentials"):
            await client.get_sites()


@respx.mock
async def test_401_refreshes_token_exactly_once_then_fails_loudly() -> None:
    token_route = respx.post(TOKEN_URL)
    token_route.side_effect = [
        httpx.Response(200, json={"Token": "tok-old"}),
        httpx.Response(200, json={"Token": "tok-new"}),
    ]
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [httpx.Response(401), httpx.Response(200, json=sites(1))]

    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        result = await client.get_sites()
    assert len(result) == 1
    assert token_route.call_count == 2
    assert site_route.calls[1].request.headers["X-Auth-Token"] == "tok-new"


@respx.mock
async def test_persistent_401_after_refresh_raises_auth_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(401)
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystAuthError, match="after a token refresh"):
            await client.get_sites()


@respx.mock
async def test_expired_token_is_refreshed_proactively() -> None:
    token_route = respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(200, json=sites(1))
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_sites()
        client._token_fetched_at -= 56 * 60  # age the token past the 55-min window
        await client.get_sites()
    assert token_route.call_count == 2


@respx.mock
async def test_get_retries_on_5xx_then_succeeds() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json=sites(2)),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert len(await client.get_sites()) == 2
    assert site_route.call_count == 3


@respx.mock
async def test_persistent_5xx_raises_catalyst_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(503)
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="503"):
            await client.get_sites()


@respx.mock
async def test_pagination_collects_all_pages() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    site_route = respx.get(SITE_URL)
    site_route.side_effect = [
        httpx.Response(200, json=sites(PAGE_SIZE)),
        httpx.Response(200, json=sites(3)),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        result = await client.get_sites()
    assert len(result) == PAGE_SIZE + 3
    first, second = (call.request.url.params for call in site_route.calls)
    assert first["offset"] == "1"
    assert second["offset"] == str(1 + PAGE_SIZE)


@respx.mock
async def test_pnp_devices_query_all_actionable_states() -> None:
    """The wizard must see failed/reset devices (Error/Planned/Onboarding),
    not only Unclaimed — one query per actionable state, merged."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    pnp_route = respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device").respond(
        200, json={"response": []}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_pnp_devices()
    queried = {call.request.url.params["state"] for call in pnp_route.calls}
    assert queried == set(PNP_ACTIONABLE_STATES)
    assert "Error" in queried


@respx.mock
async def test_pnp_devices_lists_failed_device_and_dedups() -> None:
    """Regression: a switch that failed onboarding stays in CCC as Error and
    must appear in the list; a device returned under two states is not doubled."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})

    def by_state(request: httpx.Request) -> httpx.Response:
        state = request.url.params["state"]
        if state == "Unclaimed":
            return httpx.Response(
                200, json=[{"id": "pnp-new", "deviceInfo": {"serialNumber": "NEW", "state": state}}]
            )
        if state == "Error":
            return httpx.Response(
                200,
                json=[{"id": "pnp-old", "deviceInfo": {"serialNumber": "OLD", "state": state}}],
            )
        return httpx.Response(200, json=[])

    respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device").mock(side_effect=by_state)
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        devices = await client.get_pnp_devices()
    by_serial = {d["deviceInfo"]["serialNumber"]: d["deviceInfo"]["state"] for d in devices}
    assert by_serial == {"NEW": "Unclaimed", "OLD": "Error"}


@respx.mock
async def test_pnp_devices_accepts_bare_array_response() -> None:
    """Live CCC 2.3.7 returns the PnP list as a bare JSON array (no 'response'
    wrapper) — regression for the wizard 500 on real Catalyst Center."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    pnp_route = respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device")
    pnp_route.side_effect = [
        httpx.Response(
            200,
            json=[
                {"id": f"pnp-{i}", "deviceInfo": {"serialNumber": f"SN{i}"}}
                for i in range(PAGE_SIZE)
            ],
        ),
        httpx.Response(200, json=[{"id": "pnp-x", "deviceInfo": {"serialNumber": "SNX"}}]),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        devices = await client.get_pnp_devices(states=["Unclaimed"])
    assert len(devices) == PAGE_SIZE + 1
    assert pnp_route.call_count == 2


@respx.mock
async def test_unexpected_pagination_shape_raises_typed_error() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(SITE_URL).respond(200, json={"response": "not-a-list"})
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="Unexpected response shape"):
            await client.get_sites()


@respx.mock
async def test_pnp_list_uses_zero_based_offset() -> None:
    """The PnP onboarding list is 0-based; a 1-based offset silently dropped
    the first unclaimed device on live CCC (4 in the GUI, 3 in the wizard)."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    pnp_route = respx.get(f"{BASE}/dna/intent/api/v1/onboarding/pnp-device")
    pnp_route.side_effect = [
        httpx.Response(
            200,
            json=[
                {"id": f"pnp-{i}", "deviceInfo": {"serialNumber": f"SN{i}"}}
                for i in range(PAGE_SIZE)
            ],
        ),
        httpx.Response(200, json=[{"id": "pnp-last", "deviceInfo": {"serialNumber": "LAST"}}]),
    ]
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        devices = await client.get_pnp_devices(states=["Unclaimed"])
    assert len(devices) == PAGE_SIZE + 1
    first, second = (call.request.url.params for call in pnp_route.calls)
    assert first["offset"] == "0"
    assert second["offset"] == str(PAGE_SIZE)


@respx.mock
async def test_site_list_keeps_one_based_offset() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    site_route = respx.get(SITE_URL).respond(200, json=sites(1))
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        await client.get_sites()
    assert site_route.calls[0].request.url.params["offset"] == "1"


TEMPLATE_URL = f"{BASE}/dna/intent/api/v1/template-programmer/template"


@respx.mock
async def test_template_variables_plain_template() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/tpl-1").respond(
        200,
        json={
            "id": "tpl-1",
            "templateParams": [{"parameterName": "HOSTNAME"}, {"parameterName": "MGMT_IP"}],
        },
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert await client.get_template_variables("tpl-1") == ["HOSTNAME", "MGMT_IP"]


@respx.mock
async def test_template_variables_composite_unions_member_templates() -> None:
    """A composite template carries no templateParams of its own — its variables
    live in the member templates under containingTemplates."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/comp-1").respond(
        200,
        json={
            "id": "comp-1",
            "composite": True,
            "templateParams": [],
            "containingTemplates": [{"id": "m-1"}, {"id": "m-2"}],
        },
    )
    respx.get(f"{TEMPLATE_URL}/m-1").respond(
        200,
        json={"id": "m-1", "templateParams": [{"parameterName": "HOSTNAME"}]},
    )
    respx.get(f"{TEMPLATE_URL}/m-2").respond(
        200,
        json={
            "id": "m-2",
            "templateParams": [{"parameterName": "MGMT_IP"}, {"parameterName": "HOSTNAME"}],
        },
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        variables = await client.get_template_variables("comp-1")
    # union in declaration order, shared variable asked for once
    assert variables == ["HOSTNAME", "MGMT_IP"]


@respx.mock
async def test_template_variables_uses_inline_member_params_without_extra_fetch() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/comp-2").respond(
        200,
        json={
            "id": "comp-2",
            "composite": True,
            "containingTemplates": [
                {"id": "m-9", "templateParams": [{"parameterName": "VLAN_ID"}]}
            ],
        },
    )
    member = respx.get(f"{TEMPLATE_URL}/m-9").respond(200, json={"id": "m-9"})
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert await client.get_template_variables("comp-2") == ["VLAN_ID"]
    assert not member.called  # inline params are enough


@respx.mock
async def test_template_variables_accepts_legacy_params_key() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/tpl-legacy").respond(
        200, json={"id": "tpl-legacy", "params": [{"paramName": "SNMP_LOCATION"}]}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert await client.get_template_variables("tpl-legacy") == ["SNMP_LOCATION"]


DEPLOY_V2_URL = f"{BASE}/dna/intent/api/v1/template-programmer/template/deploy/v2"
DEPLOY_V1_URL = f"{BASE}/dna/intent/api/v1/template-programmer/template/deploy"


@respx.mock
async def test_deploy_falls_back_to_v1_when_v2_is_404() -> None:
    """Some CCC builds do not expose deploy/v2; the older path takes the same
    body, so a 404 must not fail the whole Day-N run."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    v2 = respx.post(DEPLOY_V2_URL).respond(404, json={"message": "Not Found"})
    v1 = respx.post(DEPLOY_V1_URL).respond(200, json={"response": {"taskId": "task-9"}})
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        result = await client.deploy_template({"templateId": "t-1", "targetInfo": []})
    assert v2.called and v1.called
    assert result["response"]["taskId"] == "task-9"


@respx.mock
async def test_deploy_does_not_fall_back_on_a_payload_error() -> None:
    """A 400 is about the request, not the endpoint — surface it as itself."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.post(DEPLOY_V2_URL).respond(400, json={"message": "bad templateId"})
    v1 = respx.post(DEPLOY_V1_URL).respond(200, json={"response": {"taskId": "task-9"}})
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="400"):
            await client.deploy_template({"templateId": "t-1", "targetInfo": []})
    assert not v1.called


@respx.mock
async def test_deployable_templates_plain_template_sends_all_params() -> None:
    """A plain template deploys as itself with no param filtering."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/tpl-1").respond(
        200, json={"id": "tpl-1", "templateParams": [{"parameterName": "HOSTNAME"}]}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert await client.get_deployable_templates("tpl-1") == [("tpl-1", [])]


@respx.mock
async def test_deployable_templates_composite_expands_to_members() -> None:
    """Deploying the composite itself pushes its member JSON to the device as
    CLI text, so it must expand to its members in declaration order."""
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/comp-1").respond(
        200,
        json={
            "id": "comp-1",
            "composite": True,
            "containingTemplates": [
                {"id": "banner", "templateParams": [{"parameterName": "BANNER"}]},
                {"id": "ports"},
            ],
        },
    )
    respx.get(f"{TEMPLATE_URL}/ports").respond(
        200, json={"id": "ports", "templateParams": [{"parameterName": "PO_ID"}]}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        assert await client.get_deployable_templates("comp-1") == [
            ("banner", ["BANNER"]),
            ("ports", ["PO_ID"]),
        ]


@respx.mock
async def test_composite_without_usable_members_fails_loudly() -> None:
    respx.post(TOKEN_URL).respond(200, json={"Token": "tok"})
    respx.get(f"{TEMPLATE_URL}/comp-2").respond(
        200, json={"id": "comp-2", "containingTemplates": [{"name": "no-id"}]}
    )
    async with CatalystCenterClient(BASE, "admin", "pw") as client:
        with pytest.raises(CatalystError, match="no usable member templates"):
            await client.get_deployable_templates("comp-2")
