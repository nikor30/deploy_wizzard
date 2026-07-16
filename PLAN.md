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
- `PNPB_SECRET_KEY` is optional until P1 (credential store) actually needs it; P1 must
  make it required and fail fast at startup.
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

## P2 — Site mapping ☐

Mapping model + migration, `/api/mappings` CRUD, JSON import/export, two-column mapping
UI (NetBox sites ↔ CCC hierarchy) with search, unmapped highlighting.

## P3 — Wizard steps 1–2 ☐

Job model + migration, PnP unclaimed device listing (paginated, auto-refresh),
`services/matching.py` (serial `strip().upper()` normalization, `planned` filter,
site-mapping resolution, mgmt-IP fallback lookup), match review UI, resumable job state.

## P4 — Day-0 ☐

Claim payload builder, site-claim execution, task polling (5 s / 30 min, task-tree drill
for buried errors), SSE progress, ISE webhook sender (HMAC-SHA256, 3× backoff, delivery
status stored + retryable). Per-device isolation: one failure never aborts siblings.

## P5 — Day-N ☐

Template variable introspection, dot-path variable resolver (unresolvable ⇒ manual
entry), Day-N mapping settings UI, deploy + polling, NetBox `PATCH status=active` only
on verified success; `partial_success` job state.

## P6 — Stats & logs ☐

DB log sink with redaction, `/logs` UI (filters, expandable context, webhook retry),
stats aggregation + charts, retention job (default 90 days, nightly).

## P7 — Hardening ☐

Playwright e2e suite, polling load tests, failure injection (401/429/5xx, timeouts,
half-failed batches), a11y + mobile pass, docs/runbook, image slimming, SECURITY.md.
