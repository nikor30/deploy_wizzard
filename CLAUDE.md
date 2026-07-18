# CLAUDE.md — PnP Bridge: NetBox ↔ Cisco Catalyst Center Onboarding Tool

This file is the single source of truth for how Claude Code works in this repository.
Read it fully before writing any code. When requirements and code diverge, this file wins —
or update this file first, then the code.

---

## 1. Project Overview

**PnP Bridge** is a self-hosted web tool that orchestrates the full lifecycle of network
device onboarding by synchronizing **NetBox** (source of truth) with **Cisco Catalyst
Center** (deployment engine):

1. **Claim (Day-0):** Show unclaimed PnP devices from Catalyst Center, match them against
   NetBox devices (status = `planned`) by **serial number**, enrich with NetBox data
   (site, name, mgmt IP, VLAN), and claim them to the mapped Catalyst Center site.
2. **Notify:** After successful Day-0, fire a configurable **webhook** to an external tool
   (ISE helper) so it can set the correct values in Cisco ISE.
3. **Provision (Day-N):** Deploy Day-N templates with variables auto-filled from NetBox;
   anything that cannot be mapped is presented for **manual entry** in the wizard.
4. **Close the loop:** On successful provisioning, set the device status in NetBox to
   `active`.

Supporting features: encrypted credential settings, CCC↔NetBox site mapping, Day-N
variable mapping, statistics dashboard, and a searchable log/troubleshooting page.

**Deployment target:** a single OCI container (Podman-first, Docker-compatible) listening
on **port 8060**.

---

## 2. Tech Stack (fixed — do not substitute without asking)

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python 3.12, **FastAPI**, `httpx` (async) | All external API calls async with timeouts + retries |
| Task handling | FastAPI `BackgroundTasks` + APScheduler | Task polling of CCC claim/deploy tasks; no Celery/Redis |
| DB | **SQLite** via SQLAlchemy 2.x + Alembic | Stores settings, mappings, job history, logs |
| Secrets | `cryptography` (Fernet) | API tokens encrypted at rest; key from `PNPB_SECRET_KEY` env var, or auto-generated at first start and persisted as `secret.key` next to the DB (zero-config start; all credentials entered via the web UI) |
| Frontend | **React 18 + Vite + TypeScript + Tailwind CSS** | Built statically, served by FastAPI (`/` → SPA, `/api/*` → REST) |
| UI components | Headless UI / Radix primitives | Responsive, dark-mode capable, no heavy UI framework |
| Container | Multi-stage build (node build → python runtime) | Final image runs `uvicorn app.main:app --host 0.0.0.0 --port 8060` |
| Tests | `pytest`, `pytest-asyncio`, `respx`; frontend: `vitest` + React Testing Library; e2e: Playwright | See §8 |
| Lint/format | `ruff` (lint+format), `mypy --strict` on `app/`, `eslint` + `prettier` | CI-enforced |

---

## 3. Repository Layout

```
.
├── CLAUDE.md                  # this file
├── PLAN.md                    # living implementation plan (see §7) — keep updated
├── Containerfile              # multi-stage build, EXPOSE 8060
├── compose.yaml               # dev convenience (app + mock servers)
├── pyproject.toml
├── app/
│   ├── main.py                # FastAPI app factory, static SPA mount
│   ├── config.py              # env settings (pydantic-settings)
│   ├── db/                    # models, session, alembic migrations
│   ├── clients/
│   │   ├── catalyst.py        # CatalystCenterClient (auth, PnP, sites, templates, tasks)
│   │   ├── netbox.py          # NetBoxClient (devices, sites, VLANs, IPs, status patch)
│   │   └── webhook.py         # outbound webhook sender (retry, HMAC signature)
│   ├── services/
│   │   ├── matching.py        # serial-based matching, site-mapping resolution
│   │   ├── day0.py            # claim orchestration + task polling + webhook trigger
│   │   ├── dayn.py            # template variable resolution + deploy + NetBox activate
│   │   └── stats.py           # aggregation for dashboard
│   ├── api/                   # routers: /api/settings, /api/mappings, /api/wizard, /api/logs, /api/stats
│   └── logging_setup.py       # structured JSON logs → stdout + DB sink (per-job)
├── frontend/
│   └── src/
│       ├── pages/             # Wizard, SettingsCredentials, SettingsMapping, SettingsDayN, Stats, Logs
│       └── components/
├── tests/
│   ├── unit/                  # pure logic, mocked HTTP (respx)
│   ├── integration/           # against mock CCC/NetBox FastAPI servers in tests/mocks/
│   ├── mocks/                 # recorded/representative CCC + NetBox JSON fixtures
│   └── e2e/                   # Playwright specs + serve scripts (app + mock stack)
└── docs/                      # API notes, screenshots, runbook
```

---

## 4. How Claude Code Must Work Here (process rules)

1. **Plan before code.** For any non-trivial task: write/update the relevant section of
   `PLAN.md` (goal, affected files, API endpoints touched, test plan), get it right, then
   implement. Never start a feature by editing five files at once.
2. **TDD where practical.** Write the failing test first for services and clients
   (matching logic, payload builders, state transitions). UI can be test-after, but every
   wizard state transition needs a test.
3. **Small, verifiable steps.** After each change: `make lint && make test`. Do not stack
   unverified changes.
4. **Never invent Cisco/NetBox API fields.** Endpoint paths and payloads in §6 are the
   baseline; if unsure, check the fixture files in `tests/mocks/` or ask the user (who has
   live access to CCC 2.3.7.x and NetBox) to capture a real response. Do not guess field
   names — a wrong PnP claim payload bricks onboarding.
5. **No secrets in code, logs, or fixtures.** Tokens are write-only in the API
   (`PUT /api/settings/credentials` accepts them, `GET` returns masked values `****abcd`).
   Log redaction is mandatory and tested.
6. **Every external call is fallible.** Timeouts (default 30 s), 3 retries with backoff on
   idempotent GETs, explicit error types (`CatalystAuthError`, `NetBoxNotFound`,
   `TaskTimeout`, …) surfaced to the UI with actionable messages and logged with context.
7. **Migrations, not schema drift.** Any model change ⇒ Alembic migration in the same commit.
8. **Update this file** when architecture decisions change. Stale CLAUDE.md is a bug.

---

## 5. Functional Specification

### 5.1 Wizard (core UX)

A guided, resumable, step-by-step flow. State lives server-side as a **Job** record
(one job = one batch of devices) so the browser can be closed and resumed.

**Step 1 — Select unclaimed devices**
- Table of all Catalyst Center PnP devices in state `Unclaimed` (serial, PID, source IP,
  onboarding state, last contact).
- Multi-select via checkboxes; filter/search by serial and PID.
- Refresh button (re-poll CCC); auto-refresh every 60 s while page is open.

**Step 2 — Match with NetBox**
- For each selected device, look up NetBox device with the **same serial number** and
  status **`planned`**. Show side-by-side match result.
- From NetBox pull: device **name**, **site**, **mgmt IP** (primary IP or mgmt-interface
  IP), and the **VLAN list of the device's site** as a dropdown to pick the **mgmt VLAN**.
- Resolve the CCC target site via the **site mapping table** (§5.3). Unmapped site ⇒
  row is blocked with a link to Settings → Mapping.
- Unmatched devices (no serial hit or wrong status) are clearly flagged and excluded
  from claiming (never claim without a NetBox match).

**Step 3 — Day-0 claim**
- Per device: select Day-0 onboarding template + image (defaults configurable in settings),
  variables prefilled from NetBox (hostname, mgmt IP/mask/gateway, mgmt VLAN).
- Execute claim per device; poll CCC task/PnP state until `Provisioned`/error; live
  progress per device (queued → claiming → provisioning → success/failed) via SSE.
- **On success:** send the ISE webhook (§5.4). Webhook failure does NOT roll back the
  claim; it is logged and retryable from the Logs page.

**Step 4 — Day-N provisioning**
- Select Day-N template(s); resolve every template variable through the **Day-N variable
  mapping** (§5.3). Variables that resolve show read-only prefilled values; unresolvable
  variables render as **manual input fields** (required before proceeding).
- Deploy, poll task status, show per-device progress.

**Step 5 — Finalize**
- On Day-N success: `PATCH` the NetBox device to status **`active`**.
- Summary screen: per-device outcome, durations, links to logs; job marked complete.

### 5.2 Settings — Credentials (`/settings/credentials`)
- Catalyst Center: base URL, username, password, TLS-verify toggle.
- NetBox: base URL, API token, TLS-verify toggle.
- ISE webhook: target URL, optional shared secret (HMAC-SHA256 header
  `X-PnPB-Signature`), enable/disable.
- Each block has a **Test connection** button (CCC: fetch token + `GET /dna/intent/api/v1/site`
  count; NetBox: `GET /api/status/`). Store only after a successful test or explicit override.

### 5.3 Settings — Mappings
- **Site mapping page (`/settings/mapping`):** two-column mapper, NetBox sites (left,
  from API) ↔ CCC site hierarchy (right, from API), with search on both sides. Persisted;
  unmapped sites highlighted. Export/import as JSON.
- **Day-N variable mapping (`/settings/dayn`):** map CCC template variables to NetBox
  data sources (device fields, custom fields, interface/IP/VLAN attributes, config
  contexts) using a small dot-path expression (e.g. `device.custom_fields.snmp_location`).
  Anything left unmapped ⇒ manual entry at wizard Step 4.

### 5.4 ISE Webhook (outbound, after Day-0 success)
```json
POST <configured URL>
{
  "event": "day0_success",
  "timestamp": "2026-07-16T12:34:56Z",
  "job_id": 42,
  "device": {
    "serial": "FCW1234ABCD",
    "hostname": "sw-ffm-01",
    "pid": "C9300-48P",
    "mgmt_ip": "172.20.10.5",
    "mgmt_vlan": 110,
    "netbox_site": "FFM-DC1",
    "ccc_site": "Global/Germany/Frankfurt/DC1",
    "netbox_device_id": 1234
  }
}
```
Retries: 3× exponential backoff; delivery status stored per event and retryable via UI.

### 5.5 Statistics & Logging
- **Stats (`/stats`):** totals (claimed, provisioned, failed) with time filters, success
  rate, average Day-0/Day-N duration, failures by error category, jobs over time (charts).
- **Logs (`/logs`):** searchable, filterable (job, device serial, level, component,
  time range) view of the structured log DB sink; expandable entries show the full
  request/response context (redacted); "retry webhook" action where applicable.
  Log retention configurable (default 90 days, nightly cleanup job).

---

## 6. External API Reference (baseline — verify against live fixtures)

### 6.1 Catalyst Center (target version 2.3.7.x, Intent API)
- **Auth:** `POST /dna/system/api/v1/auth/token` (HTTP Basic) → `Token`. Send as
  `X-Auth-Token` header. Token lifetime ~60 min ⇒ client must auto-refresh on 401 (once)
  and proactively at 55 min. Serialize refresh with an async lock.
- **PnP devices:** `GET /dna/intent/api/v1/onboarding/pnp-device?state=Unclaimed&limit=...&offset=...`
  (paginate; also used to poll per-device `deviceInfo.state`).
- **Sites:** `GET /dna/intent/api/v1/site` (paginate) for the mapping page.
- **Claim to site:** `POST /dna/intent/api/v1/onboarding/pnp-device/site-claim` with
  `{deviceId, siteId, type: "Default", imageInfo: {...}, configInfo: {configId, configParameters: [...]}}`.
- **Templates:** `GET /dna/intent/api/v1/template-programmer/template` (+ `/template/{id}`
  for variable definitions), deploy via `POST .../template/deploy/v2`; both return a task.
- **Tasks:** `GET /dna/intent/api/v1/task/{taskId}` — poll every 5 s, overall timeout
  30 min per device, treat `isError: true` + `failureReason` as terminal failure.
- Rate limiting: max 5 concurrent requests to CCC; global semaphore in the client.

### 6.2 NetBox (v4.x, REST + token auth `Authorization: Token <key>`)
- **Match candidates:** `GET /api/dcim/devices/?status=planned&limit=0` (or per-serial
  `?serial=<sn>`; serials in NetBox may have inconsistent case/whitespace ⇒ normalize
  with `strip().upper()` on both sides before comparing).
- **Device detail:** name, `site`, `primary_ip4`; if no primary IP, look up the
  management interface IP (`/api/ipam/ip-addresses/?device_id=<id>`, interface named
  `mgmt*`/`Vlan*` — make the lookup strategy configurable).
- **VLANs for site:** `GET /api/ipam/vlans/?site_id=<id>&limit=0`.
- **Config contexts / custom fields:** available on the device object; usable as Day-N
  variable sources.
- **Activate:** `PATCH /api/dcim/devices/<id>/` with `{"status": "active"}`.
- Always paginate (`next` links) unless `limit=0` is used; respect `brief=true` where
  full objects aren't needed.

---

## 7. Implementation Plan (phases — keep PLAN.md in sync, tick off as done)

- **P0 — Scaffold:** repo layout, FastAPI app factory, Vite React app, Containerfile
  (port 8060), Makefile (`dev`, `lint`, `test`, `build`, `image`), CI (GitHub Actions:
  lint + tests + image build), health endpoint `/api/health`.
- **P1 — Settings & clients:** encrypted credential store, Catalyst/NetBox clients with
  auth + retry + pagination, connection tests, Settings UI.
- **P2 — Site mapping:** mapping model + API + two-column UI, JSON import/export.
- **P3 — Wizard steps 1–2:** unclaimed device list, NetBox matching service (serial
  normalization, planned-status filter), match review UI, job model.
- **P4 — Day-0:** claim payload builder, task polling, SSE progress, webhook sender
  with HMAC + retries.
- **P5 — Day-N:** template variable introspection, variable mapping settings + resolver,
  manual-entry UI, deploy + polling, NetBox `active` update.
- **P6 — Stats & logs:** DB log sink, logs UI with filters and webhook retry, stats
  aggregation + charts, retention job.
- **P7 — Hardening & fine-tuning:** e2e Playwright suite, load test the polling loops,
  failure-injection tests (CCC 401/429/5xx, NetBox timeouts, half-failed batches),
  accessibility + mobile layout pass, docs/runbook, image slimming, `SECURITY.md`.

Each phase ends with: all tests green, lint clean, a working `make image && podman run -p 8060:8060 ...`, and a short demo note in `PLAN.md`.

---

## 8. Testing Strategy (non-negotiable)

- **Unit (fast, no I/O):** matching logic (serial normalization, status filter, unmapped
  site behavior), claim/deploy payload builders, variable resolver (dot-paths, missing
  data ⇒ manual flag), webhook signing, log redaction. Target ≥ 90 % coverage on
  `app/services/` and `app/clients/`.
- **Client tests with `respx`:** token refresh on 401, pagination, retry/backoff, error
  mapping to typed exceptions.
- **Integration:** `tests/mocks/` contains small FastAPI apps mimicking CCC and NetBox
  using the recorded fixtures; run the real clients/services against them, including the
  full happy path Step 1 → 5 and key failure paths (claim task `isError`, webhook 500,
  NetBox PATCH failure after successful Day-N ⇒ job ends in `partial_success`).
- **Frontend:** vitest + RTL for wizard state machine, settings forms, mapping UI.
- **E2E:** Playwright (config at repo root; `webServer` auto-starts the app on :8061
  and the mock stack on :9100): complete wizard run, resume
  after reload, manual-entry flow, settings round-trip with masked secrets.
- **Rule:** every bug fix ships with a regression test. Every PR: `make lint test` green.

---

## 9. Commands

```bash
make dev        # uvicorn --reload on :8060 + vite dev server (proxied)
make lint       # ruff check + ruff format --check + mypy + eslint
make test       # pytest (unit+integration) + vitest
make e2e        # playwright suite (auto-starts app :8061 + mock CCC/NetBox/ISE :9100)
make image      # podman build -t pnp-bridge:dev -f Containerfile .
make run        # podman run --rm -p 8060:8060 -e PNPB_SECRET_KEY=... -v pnpb-data:/data pnp-bridge:dev
```

Runtime env vars: `PNPB_SECRET_KEY` (optional Fernet key; when unset one is generated at
first start and persisted as `secret.key` next to the DB — keep the `/data` volume or
set the env var explicitly), `PNPB_DB_PATH` (default `/data/pnpb.sqlite`),
`PNPB_LOG_LEVEL`, `PNPB_PORT` (default **8060**).

---

## 10. Definition of Done (per feature)

- [ ] PLAN.md updated before and after
- [ ] Tests written and passing (`make lint test`)
- [ ] No secret can appear in logs/UI/fixtures (redaction test covers new code paths)
- [ ] Errors surface in the UI with actionable text and appear in the Logs page
- [ ] Works in the container (`make image && make run`, verified on :8060)
- [ ] Migration included if the schema changed
- [ ] CLAUDE.md updated if an architectural decision changed

---

## 11. Known Pitfalls (learned the hard way — respect these)

- CCC tokens silently expire; always handle 401-refresh-retry exactly once, then fail loudly.
- PnP `site-claim` errors are often buried in the task tree (`getTaskTree`) — fetch child
  tasks when `failureReason` is empty.
- NetBox `planned` devices frequently lack `primary_ip4`; the mgmt-IP lookup fallback is
  essential, not optional.
- Serial mismatches are usually whitespace/case, not real mismatches — normalize first,
  and log both raw values when a match fails.
- Never mark a NetBox device `active` unless the Day-N task is verifiably successful;
  a half-updated source of truth is worse than a failed job.
- Batch operations must be per-device isolated: one failed device must never abort or
  roll back its siblings.
