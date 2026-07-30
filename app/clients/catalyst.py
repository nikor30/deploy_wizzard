"""Catalyst Center Intent API client (target 2.3.7.x).

Auth: POST /dna/system/api/v1/auth/token with HTTP Basic -> Token, sent as
X-Auth-Token. Tokens live ~60 min; we refresh proactively at 55 min and on a
401 exactly once, serialized behind an async lock. All requests share a global
5-connection semaphore (CCC rate limit).
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from types import TracebackType
from typing import Any

import httpx

from app.clients.base import DEFAULT_TIMEOUT, get_with_retries, trace_http
from app.errors import CatalystAuthError, CatalystError

logger = logging.getLogger(__name__)

TOKEN_LIFETIME_SECONDS = 55 * 60
MAX_CONCURRENT_REQUESTS = 5
PAGE_SIZE = 50

# Template deploy: v2 is the 2.3.x endpoint; the older path is the fallback
# for builds that do not expose v2 (they take the same body).
DEPLOY_V2_PATH = "/dna/intent/api/v1/template-programmer/template/deploy/v2"
DEPLOY_V1_PATH = "/dna/intent/api/v1/template-programmer/template/deploy"

# PnP workflow states that are still actionable for onboarding. A device that
# failed a claim (or was reset after a failed attempt) stays in the CCC PnP
# inventory as Error/Planned/Onboarding — CCC keeps the old record — so the
# wizard must list those, not just Unclaimed, or the device is invisible here
# while still showing in CCC's own GUI. "Provisioned"/"Deleted" are terminal
# and left out to keep the list focused on devices that still need work.
PNP_ACTIONABLE_STATES: tuple[str, ...] = ("Unclaimed", "Planned", "Onboarding", "Error")

# Every PnP state an operator may choose to list in wizard step 1 (Settings →
# Credentials → "PnP device states"). Superset of the actionable default: the
# terminal states are selectable for troubleshooting, e.g. to confirm a device
# really did reach Provisioned, without becoming visible noise by default.
PNP_SELECTABLE_STATES: tuple[str, ...] = (
    "Unclaimed",
    "Planned",
    "Onboarding",
    "Error",
    "Provisioned",
    "Deleted",
)


def _template_params(template: dict[str, Any]) -> list[str]:
    """Variable names from a template payload, de-duplicated in order.

    `templateParams` is the CCC 2.3.7 shape; older/other payloads spell it
    `params`, so both are accepted rather than silently returning nothing.
    """
    names: list[str] = []
    seen: set[str] = set()
    for key in ("templateParams", "params"):
        for param in template.get(key) or []:
            name = param.get("parameterName") or param.get("paramName")
            if name and str(name) not in seen:
                seen.add(str(name))
                names.append(str(name))
    return names


class CatalystCenterClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        tls_verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), verify=tls_verify, timeout=timeout
        )
        self._token: str | None = None
        self._token_fetched_at = 0.0
        self._token_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def __aenter__(self) -> "CatalystCenterClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_token(self) -> str:
        try:
            response = await self._client.post(
                "/dna/system/api/v1/auth/token",
                auth=(self._username, self._password),
            )
        except httpx.TransportError as exc:
            raise CatalystError(f"Cannot reach Catalyst Center: {exc}") from exc
        if response.status_code in (401, 403):
            raise CatalystAuthError(
                "Catalyst Center rejected the credentials (HTTP "
                f"{response.status_code}). Check username/password in Settings."
            )
        if response.status_code != 200:
            raise CatalystError(
                f"Catalyst Center token request failed with HTTP {response.status_code}."
            )
        token = response.json().get("Token")
        if not token or not isinstance(token, str):
            raise CatalystError("Catalyst Center token response did not contain a Token.")
        return token

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            expired = time.monotonic() - self._token_fetched_at > TOKEN_LIFETIME_SECONDS
            if self._token is None or expired or force_refresh:
                self._token = await self._fetch_token()
                self._token_fetched_at = time.monotonic()
            return self._token

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> httpx.Response:
        headers = {"X-Auth-Token": token}
        async with self._semaphore:
            if method == "GET":
                # only GETs are idempotent — retry with backoff
                return await get_with_retries(self._client, path, headers=headers, params=params)
            try:
                return await self._client.request(
                    method, path, headers=headers, params=params, json=json
                )
            except httpx.TransportError as exc:
                raise CatalystError(f"Cannot reach Catalyst Center: {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> httpx.Response:
        """Authenticated request with 401-refresh-retry exactly once."""
        token = await self._get_token()
        response = await self._send(method, path, token, params, json)
        if response.status_code == 401:
            token = await self._get_token(force_refresh=True)
            response = await self._send(method, path, token, params, json)
            if response.status_code == 401:
                raise CatalystAuthError(
                    "Catalyst Center returned 401 even after a token refresh. "
                    "Check the credentials in Settings."
                )
        trace_http(method, path, json, response, service="catalyst")
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = f" — {body.get('message') or body.get('response') or ''}".rstrip(" —")
            except ValueError:
                pass
            raise CatalystError(
                f"Catalyst Center {method} {path} failed with HTTP {response.status_code}{detail}."
            )
        return response

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _get_paginated(
        self, path: str, params: dict[str, Any] | None = None, *, first_offset: int = 1
    ) -> list[dict[str, Any]]:
        """Collect all pages of an offset/limit-paginated list endpoint.

        Offset bases differ per API family: /site is 1-based, the PnP
        onboarding list is 0-based (a 1-based offset silently skips the
        first device — seen on live CCC 2.3.7)."""
        items: list[dict[str, Any]] = []
        offset = first_offset
        while True:
            page_params: dict[str, Any] = dict(params or {})
            page_params.update({"limit": PAGE_SIZE, "offset": offset})
            response = await self._get(path, params=page_params)
            payload = response.json()
            # Some CCC list endpoints wrap the page in {"response": [...]},
            # others (e.g. the PnP device list on live 2.3.7) return a bare array.
            if isinstance(payload, list):
                page = payload
            elif isinstance(payload, dict) and isinstance(payload.get("response"), list):
                page = payload["response"]
            else:
                raise CatalystError(f"Unexpected response shape from Catalyst Center {path}.")
            items.extend(page)
            if len(page) < PAGE_SIZE:
                return items
            offset += PAGE_SIZE

    async def test_connection(self) -> int:
        """Fetch a token and count sites; returns the site count."""
        await self._get_token(force_refresh=True)
        return len(await self.get_sites())

    async def get_sites(self) -> list[dict[str, Any]]:
        return await self._get_paginated("/dna/intent/api/v1/site")

    async def get_pnp_devices(
        self, states: Sequence[str] = PNP_ACTIONABLE_STATES
    ) -> list[dict[str, Any]]:
        """PnP devices across the given workflow states (default: everything
        not yet successfully provisioned), merged and de-duplicated by id.

        One query per state — the single-state filter is the shape proven on
        live CCC 2.3.7; this avoids betting on multi-value or omitted-state
        query support. Order follows `states`, so Unclaimed devices sort first.
        """
        merged: dict[str, dict[str, Any]] = {}
        for state in states:
            page = await self._get_paginated(
                "/dna/intent/api/v1/onboarding/pnp-device",
                params={"state": state},
                first_offset=0,
            )
            for device in page:
                merged.setdefault(str(device.get("id")), device)
        return list(merged.values())

    async def get_pnp_device(self, device_id: str) -> dict[str, Any]:
        """Single PnP device — used to poll deviceInfo.state during claiming."""
        response = await self._get(f"/dna/intent/api/v1/onboarding/pnp-device/{device_id}")
        return dict(response.json())

    async def claim_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST site-claim (§6.1). Not retried — claiming is not idempotent."""
        response = await self._request(
            "POST", "/dna/intent/api/v1/onboarding/pnp-device/site-claim", json=payload
        )
        return dict(response.json())

    async def get_templates(self) -> list[dict[str, Any]]:
        """Onboarding/CLI templates from the template programmer."""
        response = await self._get("/dna/intent/api/v1/template-programmer/template")
        body = response.json()
        return list(body) if isinstance(body, list) else list(body.get("response", []))

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """Single template incl. variable definitions (templateParams)."""
        response = await self._get(f"/dna/intent/api/v1/template-programmer/template/{template_id}")
        return dict(response.json())

    async def get_template_variables(self, template_id: str) -> list[str]:
        """Variable names a template needs, including a composite's members.

        A CCC *composite* template holds no `templateParams` of its own — its
        variables live in the member templates listed under `containingTemplates`
        (the member entries are stubs, so each is fetched by id). Resolving only
        the top level therefore yields an empty set and every member variable
        silently goes unfilled. Members are fetched once each and their
        variables unioned in declaration order (first occurrence wins), so a
        variable shared by two members is asked for once.
        """
        template = await self.get_template(template_id)
        variables = _template_params(template)
        seen = set(variables)
        for member in template.get("containingTemplates") or []:
            member_id = member.get("id") or member.get("templateId")
            # a member stub may already carry its params; only fetch when it doesn't
            member_vars = _template_params(member)
            if not member_vars and member_id:
                member_vars = _template_params(await self.get_template(str(member_id)))
            for name in member_vars:
                if name not in seen:
                    seen.add(name)
                    variables.append(name)
        return variables

    async def get_deployable_templates(self, template_id: str) -> list[tuple[str, list[str]]]:
        """The template(s) to actually deploy, as ordered `(id, variables)`.

        A **composite** template is a container, not something CCC can render on
        its own: deploying it pushes its `containingTemplates` JSON to the device
        as CLI text (`% Invalid input detected`). Its members must be deployed
        individually, in declaration order. A regular template returns itself.

        Each member comes back with its own variable list so the deploy sends
        only the parameters that member declares. A plain template returns an
        empty list instead — meaning "send everything", which keeps the proven
        single-template behaviour rather than risking dropping a needed value
        because introspection missed a parameter.
        """
        template = await self.get_template(template_id)
        members = template.get("containingTemplates") or []
        if not members:
            return [(template_id, [])]
        deployable: list[tuple[str, list[str]]] = []
        for member in members:
            member_id = member.get("id") or member.get("templateId")
            if not member_id:
                continue
            variables = _template_params(member)
            if not variables:
                variables = _template_params(await self.get_template(str(member_id)))
            deployable.append((str(member_id), variables))
        if not deployable:
            raise CatalystError(
                f"Composite template {template_id} lists no usable member templates."
            )
        return deployable

    async def deploy_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST deploy/v2 (§6.1); returns a task. Not retried (not idempotent).

        Some Catalyst Center builds do not expose `deploy/v2` and answer 404;
        the older `template/deploy` takes the same body, so fall back to it once
        rather than failing the whole Day-N run. Only a 404 triggers the
        fallback — a 4xx about the payload must still surface as itself.
        """
        try:
            response = await self._request("POST", DEPLOY_V2_PATH, json=payload)
        except CatalystError as exc:
            if "HTTP 404" not in str(exc):
                raise
            logger.warning("deploy/v2 not available (404) — retrying on %s", DEPLOY_V1_PATH)
            response = await self._request("POST", DEPLOY_V1_PATH, json=payload)
        return dict(response.json())

    async def get_deployment_status(self, deployment_id: str) -> dict[str, Any]:
        """Status of a template deployment (deploy/v2 returns a deployment, not
        a task): `{status, devices: [{status, detailedStatusMessage}], ...}`."""
        response = await self._get(
            f"/dna/intent/api/v1/template-programmer/template/deploy/status/{deployment_id}"
        )
        body = response.json()
        return dict(body.get("response", body))

    # --- inventory metadata: device role + tags ---------------------------
    #
    # NOTE: these four endpoints are the documented 2.3.7 Intent API shapes but
    # have not yet been confirmed against the user's live controller. Every
    # caller treats a failure here as non-fatal — inventory metadata is
    # cosmetic, and a 4xx must never fail an otherwise good onboarding.

    async def get_network_device_by_ip(self, ip: str) -> dict[str, Any]:
        """Inventory device (post-claim) by management IP — gives the UUID the
        role and tag endpoints need."""
        response = await self._get(f"/dna/intent/api/v1/network-device/ip-address/{ip}")
        body = response.json()
        return dict(body.get("response", body))

    async def set_device_role(self, device_uuid: str, role: str) -> None:
        """Set the inventory role (ACCESS/CORE/DISTRIBUTION/BORDER ROUTER).

        `roleSource: MANUAL` stops CCC's discovery from overwriting it on the
        next inventory sync.
        """
        await self._request(
            "PUT",
            "/dna/intent/api/v1/network-device/brief",
            json={"id": device_uuid, "role": role, "roleSource": "MANUAL"},
        )

    async def ensure_tag(self, name: str) -> str:
        """Tag id for `name`, creating the tag if it does not exist yet."""
        response = await self._get("/dna/intent/api/v1/tag", params={"name": name})
        existing = response.json().get("response") or []
        for tag in existing:
            if str(tag.get("name", "")).casefold() == name.casefold() and tag.get("id"):
                return str(tag["id"])
        created = await self._request("POST", "/dna/intent/api/v1/tag", json={"name": name})
        body = created.json().get("response") or {}
        tag_id = body.get("id")
        if not tag_id:
            # CCC answers tag creation with a task; re-read to get the id.
            response = await self._get("/dna/intent/api/v1/tag", params={"name": name})
            for tag in response.json().get("response") or []:
                if tag.get("id"):
                    return str(tag["id"])
            raise CatalystError(f"Catalyst Center did not return an id for tag {name!r}.")
        return str(tag_id)

    async def tag_device(self, tag_id: str, device_uuid: str) -> None:
        await self._request(
            "POST",
            f"/dna/intent/api/v1/tag/{tag_id}/member",
            json={"networkdevice": [device_uuid]},
        )

    async def get_provisioned_device(self, device_uuid: str) -> dict[str, Any] | None:
        """The device's existing provisioning record, or None if it has none.

        PnP site-claim already registers the device as provisioned, so a *new*
        provision (POST) is rejected by intent validation (NCSP11001) — the
        correct call for an already-provisioned device is a re-provision (PUT).
        """
        response = await self._get(
            "/dna/intent/api/v1/sda/provisionDevices",
            params={"networkDeviceId": device_uuid},
        )
        entries = response.json().get("response") or []
        return dict(entries[0]) if entries else None

    async def provision_devices(self, site_id: str, device_uuid: str) -> dict[str, Any]:
        """Provision a device to its site so the site's network settings — AAA,
        RADIUS/TACACS, DNS, DHCP, NTP, syslog, SNMP — are pushed to it.

        PnP site-claim assigns the device to a site and runs the Day-0 template;
        it does **not** apply network settings. Template deploy does not either.
        Only provisioning does, which is why an otherwise green onboarding left
        the switch without any AAA config.

        `/dna/intent/api/v1/sda/provisionDevices` takes a list of
        `{siteId, networkDeviceId}` and answers with a task. Despite the `sda`
        path segment this is the general provision API in 2.3.7, not fabric-only
        (see the Catalyst Center 2.3.7 "Provision devices" API reference).

        **POST creates a provisioning, PUT updates one.** A device that already
        has a provisioning record is re-provisioned with PUT, carrying the
        record's `id`. Not retried — provisioning is not idempotent.

        The site is used exactly as mapped: a switch can be provisioned to a
        floor, so no substitution is made. (An earlier version silently used the
        floor's building parent, based on a doc line about *assignment*; that
        was wrong and changed the operator's intent.)
        """
        existing = await self.get_provisioned_device(device_uuid)
        entry: dict[str, Any] = {"siteId": site_id, "networkDeviceId": device_uuid}
        method = "POST"
        if existing is not None:
            method = "PUT"
            if existing.get("id"):
                entry["id"] = str(existing["id"])
            logger.info(
                "Device %s is already provisioned — re-provisioning (PUT) instead of POST",
                device_uuid,
            )
        response = await self._request(
            method, "/dna/intent/api/v1/sda/provisionDevices", json=[entry]
        )
        return dict(response.json())

    async def get_task(self, task_id: str) -> dict[str, Any]:
        response = await self._get(f"/dna/intent/api/v1/task/{task_id}")
        body = response.json()
        return dict(body.get("response", body))

    async def get_task_tree(self, task_id: str) -> list[dict[str, Any]]:
        """Child tasks — §11: claim/deploy errors are often buried here."""
        response = await self._get(f"/dna/intent/api/v1/task/{task_id}/tree")
        body = response.json()
        return list(body.get("response", body if isinstance(body, list) else []))
