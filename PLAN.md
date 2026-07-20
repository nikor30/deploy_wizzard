# PLAN.md — PnP Bridge Implementation Plan

Living plan per CLAUDE.md §7. Tick items off as they land; add a short demo note at the
end of each phase.

---

## P0 — Scaffold ✅ (this phase)

**Goal:** a runnable skeleton: FastAPI backend with `/api/health`, Vite React frontend
served by the backend, container image on port 8060, Makefile, CI.

**Affected files:** `pyproject.toml`, `app/` (main, config, api/health, logging_setup,
db stub), `frontend/` (Vite + React 18 + TS + Tailwind), `Containerfile`, `compose.yaml`,
`Makefile`, `.github/workflows/ci.yml`, `tests/unit/test_health.py`.

**Checklist:**
- [x] Repo layout per CLAUDE.md §3 (empty dirs stubbed with `__init__.py` / `.gitkeep`)
- [x] FastAPI app factory (`app.main:create_app`, module-level `app` for uvicorn)
- [x] `GET /api/health` → `{"status": "ok", "version": ...}`
- [x] Settings via pydantic-settings, `PNPB_` env prefix (`PNPB_SECRET_KEY`,
      `PNPB_DB_PATH`, `PNPB_LOG_LEVEL`, `PNPB_PORT`)
- [x] Structured JSON logging to stdout (`app/logging_setup.py`; DB sink comes in P6)
- [x] Vite React 18 + TypeScript + Tailwind CSS 4 app; placeholder page; build output
      served by FastAPI (`/` → SPA, `/api/*` → REST)
- [x] Backend tests: pytest (`tests/unit/test_health.py`), lint: ruff + mypy --strict
- [x] Frontend tests: vitest + React Testing Library smoke test; eslint + prettier
- [x] Containerfile: multi-stage (node build → python 3.12 runtime), EXPOSE 8060
- [x] compose.yaml (app; mock servers get added in P1/P3)
- [x] Makefile: `dev`, `lint`, `test`, `e2e`, `build`, `image`, `run`
- [x] CI: GitHub Actions — backend lint+test, frontend lint+test+build, image build

**Notes / deviations:**
- Local sandbox has Python 3.11 only (proxy blocks the 3.12 standalone download), so
  `requires-python = ">=3.11"` for now; the Containerfile and CI pin **3.12** per
  CLAUDE.md. Revisit when a 3.12 interpreter is available locally.
- `PNPB_SECRET_KEY` is optional: when unset, a Fernet key is generated at first start
  and persisted as `secret.key` next to the DB (zero-config container start; decided
  after P3 — the env var takes precedence when set).
- Alembic is wired in P1 together with the first real models (no empty migration churn).

**Demo:** `make image && make run` → `curl localhost:8060/api/health` returns
`{"status":"ok",...}`; `/` serves the SPA placeholder.

---

## P1 — Settings & clients ✅

**Goal:** credentials can be stored encrypted, tested against live CCC/NetBox, and both
API clients exist with auth, retry, and pagination — plus the first real UI (app shell +
Settings → Credentials page).

**Affected files:**
- `app/crypto.py` — Fernet encrypt/decrypt + `mask_secret` (`****abcd`)
- `app/errors.py` — typed error hierarchy (`PnPBridgeError`, `CatalystAuthError`,
  `CatalystApiError`, `NetBoxAuthError`, `NetBoxNotFound`, `NetBoxApiError`,
  `TaskTimeout` for P4)
- `app/db/` — SQLAlchemy base/session, `ServiceSettings` model (one row per service:
  `catalyst` / `netbox` / `webhook`; secrets stored Fernet-encrypted), Alembic env +
  initial migration; `alembic upgrade head` runs at app startup
- `app/clients/base.py` — shared httpx wrapper: 30 s timeout, 3× backoff retries on
  idempotent GETs
- `app/clients/catalyst.py` — Basic-auth token fetch, `X-Auth-Token` header,
  401-refresh-once + proactive refresh at 55 min behind an async lock, global
  5-connection semaphore, paginated `get_sites` / `get_pnp_devices`
- `app/clients/netbox.py` — `Authorization: Token`, `get_status`, paginated devices /
  VLANs, `patch_device_status`
- `app/api/settings.py` — `GET/PUT /api/settings/credentials` (secrets write-only,
  masked on read), `POST /api/settings/credentials/{service}/test`
- `app/logging_setup.py` — redaction of secret-like keys in structured log context
- `app/config.py` — `PNPB_SECRET_KEY` now **required** (fail fast at startup)
- Frontend: react-router app shell (sidebar nav with placeholder routes), Settings →
  Credentials page (3 blocks, test-connection buttons, masked values)

**Endpoints touched (external):** CCC `POST /dna/system/api/v1/auth/token`,
`GET /dna/intent/api/v1/site`, `GET /dna/intent/api/v1/onboarding/pnp-device`;
NetBox `GET /api/status/`, `GET /api/dcim/devices/`, `GET /api/ipam/vlans/`,
`PATCH /api/dcim/devices/{id}/`.

**Test plan:** unit tests for crypto + masking + redaction; respx client tests (token
fetch, 401-refresh-once then loud failure, proactive expiry refresh, GET retry/backoff,
pagination, error mapping); settings API round-trip (PUT then GET returns masked, secret
never in response/logs); connection-test endpoints against respx mocks; frontend vitest
for the settings form (masked display, save, test button states).

**Checklist:**
- [x] Backend implementation + tests green (34 pytest, incl. respx client suites)
- [x] Frontend shell + credentials page + tests green (5 vitest)
- [x] Migration included (`0001_service_settings`); migrations run in app lifespan;
      `PNPB_SECRET_KEY` required (fail fast at startup)
- [x] Demo note

**Demo:** with the SPA built and mock CCC/NetBox running, the full flow was driven with
a headless browser against `:8060`: fill credentials → "Test connection" hits the real
clients (CCC token + site count, NetBox status) → save → reload shows `****1234` masked
placeholders and the plaintext secret appears nowhere in the page. SPA routes survive
reload via the FastAPI fallback route.

## P2 — Site mapping ✅

**Goal:** persistable NetBox↔CCC site mapping editable in a two-column UI, exportable/
importable as JSON — the prerequisite for wizard Step 2 site resolution.

**Affected files:**
- `app/db/models.py` + migration `0002_site_mappings` — `SiteMapping`
  (`netbox_site_id` unique, `netbox_site_name`, `ccc_site_id`, `ccc_site_name`)
- `app/services/connections.py` — build configured clients from the stored
  (decrypted) credentials; `ConfigurationError` when a service isn't configured
- `app/api/mappings.py` — `GET/PUT /api/mappings/sites` (PUT replaces the full list —
  used by both the editor and JSON import; export is the GET payload),
  `GET /api/mappings/sources/netbox` and `/sources/ccc` (live site lists)
- `app/main.py` — `ConfigurationError` → HTTP 400 with actionable message
- Frontend `pages/SettingsMapping.tsx` — two searchable columns (NetBox sites left,
  CCC hierarchy right), click-to-pair, mapped list with remove, unmapped NetBox sites
  highlighted, Save / Export JSON / Import JSON

**Test plan:** mappings API round-trip + full-replace semantics + duplicate rejection;
sources endpoints via respx (TestClient traffic passed through); vitest: render sources,
pair a mapping, save payload shape, unmapped highlight.

**Checklist:**
- [x] Backend + tests green (40 pytest)
- [x] Frontend + tests green (9 vitest)
- [x] Demo note

**Demo:** headless-browser run against `:8060` with mock CCC/NetBox: mapping page loads
both site lists live, two pairs mapped by clicking left→right, saved, page reload shows
the persisted mappings ("Mappings (2)"), CCC column search filters correctly. Version
bumped to **1.0.0** for the first tagged release (release notes in
`docs/releases/v1.0.0.md`, release created by `.github/workflows/release.yml` on tag
push).

## P3 — Wizard steps 1–2 ✅

**Goal:** wizard steps 1 (select unclaimed PnP devices) and 2 (NetBox match review)
working end-to-end with server-side resumable job state.

**Affected files:**
- `app/db/models.py` + migration `0003_jobs` — `Job` (status, current_step, timestamps)
  and `JobDevice` (serial/pid/ccc_device_id + match result columns: netbox ids/names,
  ccc site, mgmt IP, mgmt VLAN, `match_status` ∈ matched | unmatched | unmapped_site)
- `app/services/matching.py` — `normalize_serial` (`strip().upper()`), match selected
  CCC devices against NetBox `planned` devices by serial, resolve CCC site via the
  mapping table, mgmt IP from `primary_ip4` with fallback to the device's
  `mgmt*`/`Vlan*` interface IPs, VLAN options from the device's site
- `app/api/wizard.py` — `GET /api/wizard/pnp-devices` (live unclaimed list),
  `POST /api/wizard/jobs` (create with selected devices), `GET /api/wizard/jobs`,
  `GET /api/wizard/jobs/{id}`, `POST /api/wizard/jobs/{id}/match` (run + persist),
  `PUT /api/wizard/jobs/{id}/devices/{device_id}` (pick mgmt VLAN)
- Frontend `pages/Wizard.tsx` — job start/resume, Step 1 table (multi-select, search
  serial/PID, refresh + 60 s auto-refresh), Step 2 side-by-side match review with VLAN
  dropdown, unmatched/unmapped flags linking to Settings → Mapping; Day-0 button
  present but disabled until P4

**Rule honored:** unmatched or unmapped devices can never proceed to claiming.

**Test plan:** unit tests for matching (normalization incl. messy serials, unmatched,
unmapped site, mgmt-IP fallback, VLAN options); wizard API tests via respx (pnp list,
job create/resume, match persistence, VLAN update validation); vitest for step
transitions, selection gating, match-row rendering, VLAN pick.

**Checklist:**
- [x] Backend + tests green (54 pytest; matching suite covers messy serials, unmatched,
      unmapped site, mgmt-IP fallback, VLAN options; wizard API suite covers job
      lifecycle, match persistence/resume, VLAN validation)
- [x] Frontend + tests green (13 vitest)
- [x] Demo note

**Demo:** headless-browser run against `:8060` with mock CCC/NetBox: new job → step 1
table shows 2 unclaimed PnP devices → select both → step 2 shows one matched
(sw-ffm-01, FFM-DC1, mgmt IP prefilled, VLAN dropdown from site VLANs) and one
"no NetBox match" (excluded from claiming) → picked VLAN 110 → closed the wizard →
resumed the job → match result and VLAN selection persisted; Day-0 button correctly
gated until P4. PnP `deviceInfo` field names (`serialNumber`, `pid`, `state`,
`ipAddress`, `lastContact`) follow CLAUDE.md §6 — verify against a live CCC fixture
before P4 claiming.

## P4 — Day-0 ✅

**Goal:** wizard step 3 works end-to-end: pick a Day-0 template (+ optional image),
claim each device to its mapped CCC site, watch live per-device progress, fire the ISE
webhook on success.

**Affected files:**
- `app/clients/catalyst.py` — generic authed `_request` (401-refresh also on POST),
  `claim_device` (site-claim), `get_pnp_device` (state polling), `get_templates`
- `app/clients/webhook.py` — HMAC-SHA256 signing (`X-PnPB-Signature`), 3× backoff,
  delivery result reported to caller
- `app/services/day0.py` — pure `build_claim_payload` (hostname/mgmt IP/mask/VLAN from
  the job device; CIDR split), `run_day0` orchestration: per-device isolated
  (`asyncio.gather`, one failure never aborts siblings), states
  queued → claiming → provisioning → success/failed, PnP-state polling (default 5 s,
  30 min timeout), webhook fired per successful device (failure logged, never rolls
  back the claim)
- `app/db/models.py` + migration `0004` — Job day0 selection (`day0_config_id`,
  `day0_image_id`) + device timing/error columns; `WebhookDelivery` table (payload,
  status, attempts, error) so P6 can list + retry deliveries
- `app/api/wizard.py` — `GET /day0/templates`, `POST /jobs/{id}/claim` (BackgroundTask),
  `GET /jobs/{id}/events` (SSE snapshots; UI falls back to polling where EventSource
  is unavailable)
- Frontend `Wizard.tsx` step 3 — template/image pick, per-device variable preview,
  live progress badges, Day-N gated until P5

**Payload caution (§4):** `imageInfo`/`configParameters`/template-list field names
follow the §6 baseline + common CCC 2.3.7 shapes; must be verified against live
fixtures before production use — capture real responses into `tests/mocks/`.

**Test plan:** unit — payload builder (CIDR split, missing IP/VLAN), HMAC signature
(known vector), webhook retry/backoff + failure result; service — respx-driven day0 run
(success path incl. webhook, device failure isolation, poll timeout, error state on
claim rejection); API — claim endpoint state transitions, SSE snapshot; vitest — step 3
render, start claim, progress badges via polling fallback.

**Checklist:**
- [x] Backend + tests green (76 pytest: payload builder, HMAC + retry webhook, day0
      orchestration incl. per-device isolation / timeout / webhook-never-rolls-back,
      claim + templates + SSE endpoints)
- [x] Frontend + tests green (15 vitest)
- [x] Demo note

**Demo:** headless-browser run of the full flow against mock CCC/NetBox/webhook:
steps 1–2 as in P3, then step 3 — picked template `Day0-Onboarding`, started the claim,
watched live SSE progress (queued → provisioning → success; mock CCC needs 3 polls to
reach `Provisioned`), summary "1 succeeded, 0 failed". The mock CCC received the exact
site-claim payload (`HOSTNAME=sw-ffm-01`, `MGMT_IP=172.20.10.5`,
`MGMT_MASK=255.255.255.0`, `MGMT_VLAN=110`, siteId from the mapping) and the mock ISE
endpoint received one signed `day0_success` webhook with the §5.4 payload. SQLite now
runs in WAL mode so the SSE reader and background claim writers coexist.

## P5 — Day-N ✅

**Goal:** wizard steps 4–5 work end-to-end: pick a Day-N template, variables auto-filled
from NetBox via the dot-path mapping, manual entry for the rest, deploy + task polling,
NetBox devices set `active` only on verified success, job summary.

**Affected files:**
- `app/db/models.py` + migration `0005` — `DayNMapping` (variable → dot-path),
  `Job.dayn_template_id`, `JobDevice.dayn_variables` (JSON incl. manual flags)
- `app/services/dayn.py` — `resolve_path` (dot-path over the NetBox device object:
  fields, custom_fields, config_context), `resolve_variables` (unresolvable ⇒ manual),
  `run_dayn`: per-device isolated deploy → task poll (5 s / 30 min, `isError` +
  `failureReason`, task-tree drill when the reason is empty per §11) → NetBox
  `PATCH status=active` **only** on verified success; PATCH failure ⇒ device
  `activate_failed` and job `partial_success`
- `app/clients/catalyst.py` — `get_template` (variable definitions),
  `deploy_template` (deploy/v2), `get_task`, `get_task_tree`
- `app/clients/netbox.py` — `get_device` (full object for resolution)
- `app/api/settings.py` — `GET/PUT /api/settings/dayn` (replace-all mapping list)
- `app/api/wizard.py` — `POST /jobs/{id}/dayn/prepare` (introspect + resolve +
  persist variables), `POST /jobs/{id}/dayn/deploy` (validates required manual values,
  BackgroundTask); the SSE endpoint already covers `dayn_running`
- Frontend — `SettingsDayN.tsx` (variable↔dot-path editor), wizard step 4 (template
  pick → variable review with read-only resolved values + required manual inputs →
  deploy with live progress) and step 5 summary (per-device outcome, completed /
  partial_success banner)

**Payload caution (§4):** template-list/`templateParams`/deploy-v2/task shapes follow
the §6 baseline + common CCC 2.3.7 payloads; verify against live fixtures before
production use.

**Test plan:** resolver unit tests (nested paths, custom_fields, config_context,
missing ⇒ manual); Day-N service via respx (deploy success + activate, task `isError`
with task-tree drill, NetBox PATCH failure ⇒ `partial_success`,
never-activate-on-failure, per-device isolation); settings-dayn API round-trip;
prepare/deploy API validation; vitest for the mapping editor and wizard steps 4–5.

**Checklist:**
- [x] Backend + tests green (86 pytest; new: resolver, Day-N settings round-trip,
      prepare/deploy validation, full deploy + activate, task-tree drill,
      PATCH-failure ⇒ partial_success, never-activate-on-failure)
- [x] Frontend + tests green (19 vitest)
- [x] Demo note

**Demo:** headless-browser run of the complete wizard (steps 1→5) against mock
CCC/NetBox/ISE: after Day-0, picked the Day-N template → "Resolve variables" filled
`SNMP_LOCATION` from `device.custom_fields.snmp_location` (read-only) and flagged
`CONTACT` as manual (deploy gated until filled) → deploy → task polled to success
(3 polls) → step-5 summary "1 device(s) active in NetBox" and the mock NetBox received
exactly one `PATCH {"status": "active"}`. Job statuses `completed` / `partial_success`
/ `dayn_failed` verified in unit tests.

## P6 — Stats & logs ✅

**Goal:** searchable log page fed by a redacted DB log sink (with webhook retry) and a
statistics dashboard; nightly retention cleanup.

**Affected files:**
- `app/db/models.py` + migration `0006` — `LogEntry` (timestamp, level, component,
  message, job_id, device_serial, redacted context JSON); `JobDevice.dayn_started_at`
  / `dayn_finished_at` for Day-N durations
- `app/logging_setup.py` — DB sink handler for `app.*` loggers (reuses the existing
  redaction; recursion-safe: SQLAlchemy/uvicorn loggers excluded)
- `app/services/stats.py` — aggregation: totals (claimed/provisioned/failed),
  success rate, avg Day-0/Day-N duration, failures by error category, jobs over time;
  `PNPB_LOG_RETENTION_DAYS` (default 90) cleanup used by a nightly APScheduler job
  started in the app lifespan
- `app/api/logs.py` — `GET /api/logs` (filters: job, serial, level, component, text,
  time range; paginated), `GET /api/logs/webhook-deliveries`,
  `POST /api/logs/webhook-deliveries/{id}/retry` (re-send stored payload with current
  webhook settings, delivery row updated)
- `app/api/stats.py` — `GET /api/stats?days=N`
- Frontend — `Logs.tsx` (filter bar, expandable context rows, webhook deliveries with
  Retry), `Stats.tsx` (summary tiles + charts)

**Test plan:** DB sink writes redacted context and skips non-app loggers; retention
cleanup deletes only old rows; logs API filter matrix; webhook retry success + failure
paths; stats aggregation against seeded jobs (durations, categories, success rate);
vitest for logs filters/expansion/retry and stats rendering.

**Checklist:**
- [x] Backend + tests green (99 pytest)
- [x] Frontend + tests green (24 vitest)
- [x] Demo note

**Notes:** the DB sink is queue-based (worker thread) — a synchronous sink deadlocked
against the request's own open SQLite write transaction (busy-timeout stalls; caught by
the test suite crawling from 19 s to 214 s). Chart palette (blue/orange) validated for
light + dark and CVD with the dataviz checker.

**Demo:** headless-browser run against `:8060` with mock CCC/NetBox: Day-0 job with a
dead webhook target → Logs page shows "Failed webhook deliveries (1)" → Retry button
re-sent the stored payload to the (now reachable) mock ISE (verified received) →
level filter + expandable redacted context work; Stats page shows tiles (success rate
100%, avg Day-0 1s), the devices-per-day chart and the failure-category chart.
Retention cleanup and the log-sink redaction are unit-tested.

## Wizard UX follow-ups (live-testing feedback) ✅

User feedback after live wizard runs: no way to step back or re-pull data, the
serial/`planned` match requirements were undocumented in the UI, and stale jobs
could not be removed.

- [x] `DELETE /api/wizard/jobs/{id}` — cascades to devices, 409 while a job is
  `*_running`, 404 unknown (3 new pytest)
- [x] Start view: per-job Delete button (disabled + tooltip while running), job
  status shown in the list
- [x] Match view: info banner explaining a match needs the **same serial number**
  and NetBox status **planned**, plus "Re-run matching" and "← Back to jobs"
- [x] Day-0 view: "← Back to matching"
- [x] 4 new vitest cases (re-run refetches `/match`, back navigation, delete,
  delete disabled while running)

## P7 — Hardening ✅

**Goal:** the tool is provably robust end-to-end: real clients/services run against
mock CCC/NetBox/ISE servers (integration + e2e), every documented failure mode is
injected and asserted, the container is hardened, and operations are documented.

**Affected files**

- `tests/mocks/` — `ccc.py`, `netbox.py`, `ise.py`, `stack.py`: small FastAPI apps
  mimicking live CCC 2.3.7 (token auth, bare-array 0-based PnP list, site-claim,
  templates, deploy/v2, task + task-tree) and NetBox v4 (devices, sites, VLANs,
  status PATCH), plus an ISE webhook sink. One combined app (`stack.py`) mounts them
  under `/ccc`, `/netbox`, `/ise` with `/__mock__/` control endpoints for seeding,
  failure injection (auth 401, flaky 5xx, PnP onboarding error, task `isError` with
  empty `failureReason` → task tree, webhook 500, NetBox PATCH 500) and a
  concurrency high-water-mark counter (asserts the 5-request CCC semaphore).
  Runnable standalone: `python -m tests.mocks.stack --port 9100`.
- `tests/integration/` — real app (TestClient) + real clients against the mock stack
  over real HTTP: full happy path Step 1→5, half-failed batch ⇒ `day0_partial` with
  sibling isolation, Day-N task error drilled from the task tree, webhook 500 does
  not roll back the claim, NetBox PATCH failure after Day-N success ⇒
  `partial_success`, CCC 5xx retry through the stack, and a 25-device polling load
  test (all succeed, CCC concurrency never exceeds 5).
- `tests/e2e/*.spec.ts` + `frontend/playwright.config.ts` — Playwright against the
  built SPA served by uvicorn + mock stack (auto-started via `webServer`): settings
  round-trip with masked secrets, complete wizard run with resume-after-reload and
  Day-N manual entry, mobile-viewport smoke.
- `Containerfile` + `.containerignore` — non-root user, HEALTHCHECK on
  `/api/health`, slimmer context/image.
- `docs/runbook.md`, `SECURITY.md`, `Makefile` (`e2e` target), `compose.yaml`
  (mock stack service for the e2e/demo profile).

**Test plan:** `make lint test` green (new integration suite included), `make e2e`
green locally, container still builds.

- [x] Mock CCC/NetBox/ISE stack (`tests/mocks/`) with failure injection +
  concurrency stats; runnable standalone for demos
- [x] Integration suite (7 tests): happy path 1→5, half-failed batch isolation,
  task-tree drilling, webhook 500, NetBox PATCH failure ⇒ `partial_success`,
  5xx retry, 25-device load test (semaphore ≤ 5, shared token)
- [x] Playwright e2e (4 tests, `make e2e`, own CI job): full wizard run with
  resume-after-reload + Day-N manual entry, half-failed batch, settings
  round-trip with masked secrets, mobile smoke
- [x] Mobile layout fix: sidebar becomes a top nav bar below `md`
- [x] Container hardening: non-root user (uid 10001), HEALTHCHECK,
  `.dockerignore`
- [x] `docs/runbook.md` + `SECURITY.md`
- [x] Backend 116 pytest green, frontend 28 vitest green, lint clean

**Notes:** e2e switched from the originally planned compose stack to Playwright
`webServer` (app :8061 + mock stack :9100 as local processes) — simpler, works
identically in CI, CLAUDE.md updated. The e2e harness lives in a root
`package.json` because specs in `tests/e2e/` cannot resolve modules from
`frontend/node_modules`.

**Demo:** `make e2e` boots the full stack and drives the browser through the
entire wizard: 2 devices claimed against the mock CCC (HMAC-signed ISE webhooks
verified), Day-N deployed with one manual variable, both NetBox devices
`active`, plus the half-failed-batch and masked-secrets flows — 4/4 green in
~33 s. The same mock stack powers the integration suite, which also proves the
5-request CCC rate limit under a 25-device batch.

## Auto-suggest mappings (first-use effort reduction) ✅

**Goal:** pre-match as much as possible so first-time setup is "review and
correct" instead of "build from scratch": suggest NetBox↔CCC site pairs and
Day-N variable→NetBox dot-path mappings with confidence scores.

**Approach:** lightweight learned-heuristic matching (token normalization,
Jaccard token overlap + sequence similarity + leaf weighting, synonym
dictionary for network-engineering vocabulary, greedy unique assignment).
No heavy ML dependency — deterministic, explainable scores, fully unit-tested.

**Affected files**

- `app/services/suggest.py` — `suggest_site_mappings()` (NetBox name/slug vs
  CCC hierarchy leaf + full path, unique best assignment, confidence 0–1) and
  `suggest_variable_mappings()` (template variables vs candidate dot-paths
  discovered from a sample NetBox device: device fields, custom_fields.*,
  config_context.*; synonym expansion hostname→name, ip→primary_ip4 …).
- `app/api/mappings.py` — `GET /api/mappings/sites/suggest` (unmapped NetBox
  sites only).
- `app/api/settings.py` — `POST /api/settings/dayn/suggest` `{template_id}`.
- `frontend/src/pages/SettingsMapping.tsx` — "Suggest mappings" button,
  suggested rows flagged with confidence, user corrects then saves.
- `frontend/src/pages/SettingsDayN.tsx` — template picker + "Suggest from
  template", rows prefilled with suggested paths + confidence badges.

**Test plan:** unit tests for normalization/scoring/assignment and variable
suggestion (incl. synonyms, no-match threshold); API tests with respx; vitest
for both settings pages; `make lint test` green.

- [x] `app/services/suggest.py` + 7 unit tests (leaf match, abbreviations,
  unique assignment, threshold, path discovery, synonyms, F1 tie-breaks)
- [x] `GET /api/mappings/sites/suggest` + `POST /api/settings/dayn/suggest`
  (3 API tests)
- [x] Both settings pages: suggest buttons, confidence badges, review-first
  flow (3 new vitest)
- [x] Backend 126 pytest / frontend 31 vitest green, lint + mypy clean,
  e2e suite 4/4 green

**Demo:** on the mapping page, "Suggest mappings" pre-pairs the unmapped
NetBox sites against the CCC hierarchy (e.g. `FFM-DC1 →
Global/Germany/Frankfurt/DC1`, badge "suggested · 82%"); on the Day-N page,
picking a template and hitting "Suggest mappings" fills `HOSTNAME →
device.name`, `SNMP_LOCATION → device.custom_fields.snmp_location`, … and
adds unmatched variables as empty rows for manual mapping. Nothing is saved
until the user reviews and clicks Save.

## Template secrets (encrypted variable sources) ✅

**Goal:** store named secrets (RADIUS/TACACS keys, SNMP communities, local
passwords, AES keys …) encrypted in Settings and use them as Day-N template
variables via `secret.<NAME>` source paths — without the value ever appearing
in the UI, job records, logs, or API responses. Only the deploy call to CCC
receives the plaintext.

**Affected files**

- `app/db/models.py` + migration `0007_template_secrets` — `TemplateSecret`
  (unique name, Fernet-encrypted value).
- `app/api/settings.py` — `GET /api/settings/secrets` (masked),
  `PUT /api/settings/secrets/{name}` (write-only upsert),
  `DELETE /api/settings/secrets/{name}`.
- `app/services/dayn.py` — resolver understands `secret.<NAME>` paths:
  resolves to `{"value": "****", "source": "secret", "secret": NAME}` (never
  the plaintext); unknown secret name ⇒ manual entry.
- `app/api/wizard.py` — prepare passes stored secret names; deploy decrypts
  secret-sourced params just-in-time for the CCC payload.
- `app/services/suggest.py` + suggest endpoint — secret names join the
  candidate pool (`RADIUS_KEY → secret.radius_key`).
- `frontend/src/pages/SettingsDayN.tsx` — "Template secrets" card: masked
  list, add (name + value), delete; wizard step 4 shows `****` read-only.

**Test plan:** resolver unit tests (secret path, unknown name, masking),
secrets API round-trip (masked list, upsert, delete, value never echoed),
deploy test asserting plaintext reaches the CCC payload while job/API keep
`****`, suggestion test, vitest for the secrets card. `make lint test` green.

- [x] Model + migration 0007, masked write-only API (PUT/GET/DELETE)
- [x] Resolver `secret.<NAME>` support (masked placeholder; unknown ⇒ manual)
- [x] Deploy-time just-in-time decryption; deleted secret ⇒ actionable 422
- [x] Suggestion engine includes secret names (`RADIUS_KEY → secret.radius_key`)
- [x] Settings → Day-N "Template secrets" card (masked list, add, delete)
- [x] 7 new pytest + 1 vitest; 133 pytest / 32 vitest / 4 e2e green

**Demo:** store `radius_key` on the Day-N page (value shows as `****-123`),
map `RADIUS_KEY → secret.radius_key` (also auto-suggested), deploy: the CCC
deploy/v2 payload carries the plaintext, while the wizard, job record, API
responses, and logs only ever contain `****` — verified by test asserting the
plaintext appears in exactly one place (the captured CCC request).

## NetBox as full source of truth: location hierarchy + uplink/network context ✅

**Feedback:** mapping only covers top-level NetBox sites, but NetBox has a
sub-hierarchy (locations: buildings, floors) mirroring the CCC hierarchy; and
Day-N provisioning needs the switch's uplink/port details, IP/network, and
VLAN pulled from NetBox — everything to deploy a switch comes from NetBox.

- **Location-aware site mapping:** `SiteMapping` gains optional
  `netbox_location_id/name` (migration 0008). Mapping sources list sites AND
  their location tree as paths ("FFM-DC1 / Building A / Floor 2"). Matching
  resolves a device's CCC target by walking device.location → parent
  locations → site, most specific mapped level wins. Suggestions cover
  locations too.
- **Richer Day-N context:** `NetBoxClient.get_locations()` and
  `get_interfaces(device_id)`; `build_device_context()` enriches the device
  with `device.uplinks.0.{name,type,peer_device,peer_interface}` (cabled,
  non-mgmt interfaces), `device.interfaces`, and `device.mgmt.{address,ip,
  netmask,prefix_length,network,cidr}` computed from primary/mgmt IP.
  Suggestion candidates include the new paths.
- AES keys/passwords: already covered by template secrets (`secret.<name>`).

**Test plan:** unit (location-walk resolution, context builder, candidate
paths), respx client tests, mappings/suggest API tests with locations, mock
stack gains locations + interfaces, frontend mapping page composite keys,
full suites green.

- [x] Migration 0008, location-aware `SiteMapping`, parent-walk resolution
- [x] Sources list sites + location tree paths; mapping UI on composite keys
- [x] `get_locations()`/`get_interfaces()`; `build_device_context()` with
  `device.uplinks.*` and `device.mgmt.*`; suggestion candidates extended
- [x] Mock stack: locations + interfaces; 141 pytest / 33 vitest / 4 e2e green

**Demo:** map "FFM-DC1 / Building A" to a CCC building node — a device located
on "Floor 1" (child of Building A) resolves to that node via the parent walk,
while devices without a location keep using the site-level mapping. Day-N
variables can now use e.g. `device.uplinks.0.name`, `device.uplinks.0.peer_device`,
`device.mgmt.netmask`, `device.mgmt.network` — all suggested automatically.

## Bugfix: failed/reset switches invisible in the wizard (v1.3.2) ✅

Live feedback: a switch that failed onboarding and was factory-reset still
showed in Catalyst Center but not in the wizard. Cause: `/api/wizard/pnp-devices`
queried only `state=Unclaimed`, but a failed/reset device lingers in CCC as
`Error`/`Planned`/`Onboarding` (CCC keeps the old PnP record).

- [x] `CatalystCenterClient.get_pnp_devices()` now queries all actionable
  states (`Unclaimed`, `Planned`, `Onboarding`, `Error`), one query per
  state (the proven single-state filter), merged + de-duplicated by id
- [x] Wizard select view shows a colored state badge and a hint that
  non-Unclaimed rows are earlier attempts, re-claimable after a reset
- [x] Regression tests: client merges/dedups + surfaces Error devices; wizard
  API lists an Error-state device; frontend renders it with badge + hint
- [x] 143 pytest / 34 vitest / 4 e2e green; runbook troubleshooting row added

## Real IT-DayN variables + verify-by-serial (netbox_cc_dayn parity, v1.4.0) ✅

Grounded in the user's All_templates.csv (real CC Day-N export) and the
nikor30/netbox_cc_dayn mappings.yaml/resolvers, so our derivations match the
production tool, plus a way to verify against a real device before deploying.

- [x] `build_device_context` computes the flat CC values used by the real
  templates: `device.uplink_ports` (cabled iface names, `Te1/1/3,Te1/1/4`),
  `device.uplink_switch` (unique cabling far-end, ambiguous⇒unset),
  `device.site_vlans` (`(vid,name);…`), `device.support_contact`
  (site contact by role "Local IT" → device contact → tenant name)
- [x] `NetBoxClient.get_contact_assignments()`; `load_device_context()` shared
  helper fetches interfaces + site VLANs + contacts and builds the context
  (used by prepare, suggest, and preview)
- [x] Suggester learns the real variable names/paths (site_full_name,
  building_room→location, rack_id, device_role, asset_id, uplink_*, arrVLANs→
  site_vlans, support_contact) via new synonyms + computed candidate paths
- [x] `POST /api/settings/dayn/preview {serial, template_id?}` — resolves the
  current mappings against a real NetBox device (looked up by serial), read-only,
  secrets stay masked; Day-N settings page gets a "Verify against a real device"
  panel showing variable → source → resolved value
- [x] Mock stack: contacts endpoint + device tenant/rack/role/asset_tag;
  148 pytest / 35 vitest / 4 e2e green; CLAUDE.md §6.2 updated

**Demo:** map the IT-DayN variables to the computed paths, enter serial
`SN000001` on the Day-N page → the preview shows `uplink_ports=
TenGigabitEthernet1/1/1`, `uplink_switch=dist-ffm-01`,
`site_vlans=(110,MGMT);(120,USERS)`, `support_contact=Ladislav Fekete`, matching
the All_templates.csv columns.

## Day-0 template variable preview + gateway + debug switch (v1.5.0) ✅

Live feedback on the Day-0 claim step: preview the selected template's
variables (prefilled vs open), let the operator fill open ones (the gateway was
missing), and a global debug switch to inspect what's needed/prefilled/open.

- [x] `resolve_day0_variables` / `day0_builtins`: introspect the Day-0
  template's variables and resolve each — built-in onboarding values by name
  alias (HOSTNAME/MGMT_IP/MASK/PREFIX/VLAN), then a Day-N dot-path mapping,
  else open manual; `gateway` guessed as the mgmt subnet's first host and left
  editable
- [x] `POST /jobs/{id}/day0/prepare {config_id}` stores per-device
  `day0_variables`; claim accepts `manual` overrides and persists them;
  `build_claim_payload` sends the resolved+overridden set (empty omitted),
  legacy fallback preserved
- [x] Wizard Day-0 step: selecting a template previews the variables
  (prefilled read-only + editable manual incl. gateway), start gated on prepare
- [x] Global debug flag (`AppSetting` + `GET/PUT /api/settings/flags`), toggle
  on the Credentials page; when on, Day-0/Day-N show each variable's source
- [x] migration 0009; 157 pytest / 35 vitest / 4 e2e green

**Demo:** pick the Day-0 template → the wizard shows HOSTNAME/MGMT_IP/MASK/VLAN
prefilled from NetBox and GATEWAY as an editable field pre-filled with the
subnet's .1; enable debug on Settings → Credentials to see each variable's
source badge.
