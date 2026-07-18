# PnP Bridge — Operations Runbook

## Deploy

```bash
make image                       # podman build -t pnp-bridge:dev -f Containerfile .
podman run -d --name pnpb \
  -p 8060:8060 \
  -v pnpb-data:/data \
  pnp-bridge:dev
```

- The container starts with **zero configuration**: no env vars required.
  A Fernet key is generated on first start and stored as `/data/secret.key`.
- To supply your own key instead: `-e PNPB_SECRET_KEY=<fernet key>`
  (generate one with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
- The container runs as uid 10001 (non-root). With a **bind mount** instead of
  a named volume, make the host directory writable for that uid
  (`chown 10001 <dir>` or podman's `--userns=keep-id`); SELinux hosts need `:Z`.
- Optional env vars: `PNPB_DB_PATH` (default `/data/pnpb.sqlite`),
  `PNPB_LOG_LEVEL` (default `INFO`), `PNPB_PORT` (default `8060`).

First-time setup in the UI:

1. **Settings → Credentials**: enter Catalyst Center, NetBox, and (optionally)
   the ISE webhook. Use *Test connection* before saving.
2. **Settings → Site Mapping**: map every NetBox site to its CCC site.
3. **Settings → Day-N Variables**: map template variables to NetBox dot-paths
   (e.g. `device.custom_fields.snmp_location`). Unmapped variables become
   manual-entry fields in the wizard.

## Upgrade

```bash
git pull && make image
podman stop pnpb && podman rm pnpb
podman run -d --name pnpb -p 8060:8060 -v pnpb-data:/data pnp-bridge:dev
```

Database migrations run automatically at startup (Alembic). Keep the `/data`
volume — it holds the DB **and** the encryption key.

## Backup / restore

Back up the `pnpb-data` volume (two files: `pnpb.sqlite`, `secret.key`).
Both are required — the DB without the key means all stored credentials are
unrecoverable and must be re-entered.

```bash
podman volume export pnpb-data > pnpb-backup.tar     # backup
podman volume import pnpb-data pnpb-backup.tar       # restore (container stopped)
```

## Health & monitoring

- `GET /api/health` → `{"status": "ok"}` — also used by the container
  HEALTHCHECK (`podman ps` shows `healthy`).
- **Stats** page: success rate, durations, failures by category.
- **Logs** page: filter by job, serial, level, component, time range; failed
  ISE webhook deliveries can be retried there.
- Log retention default is 90 days (nightly cleanup job).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "CCC rejected the credentials" on test | wrong user/password or missing Intent API role | verify the account can call `/dna/intent/api/v1/site` |
| Wizard shows fewer unclaimed devices than CCC | fixed in v1.1+ (0-based PnP paging) | upgrade |
| Device row "site not mapped" | NetBox site has no CCC mapping | Settings → Site Mapping |
| Device "no NetBox match" | serial missing in NetBox or device not in `planned` status | fix the device in NetBox, then *Re-run matching* — serials are compared case/whitespace-insensitively |
| Claim fails with no reason | CCC buries errors in the task tree | the app drills child tasks automatically; see the Logs page entry for the full context |
| Day-0 ok but ISE not updated | webhook delivery failed | Logs page → retry the delivery; claims are never rolled back for webhook failures |
| Device stuck `provisioning`, then timeout | device never reached `Provisioned` in CCC | check the device console/PnP state in CCC; re-run the job for that device |
| Job ends `partial_success` | Day-N succeeded but the NetBox PATCH failed | set the device `active` in NetBox manually; the config on the device is fine |
| Container unhealthy right after start | migrations still running on a big DB | wait for `start-period` (15 s); check `podman logs pnpb` |
| "no space left" / DB locked errors | volume full or two instances sharing one DB | free space; never run two app instances against the same SQLite file |

## Local development

```bash
make dev      # backend :8060 + vite dev server
make lint     # ruff + mypy + eslint/prettier
make test     # pytest (unit + integration vs mock CCC/NetBox/ISE) + vitest
make e2e      # Playwright against the built SPA + mock stack
```

The mock Catalyst Center/NetBox/ISE stack can also be run standalone for
demos: `uv run python -m tests.mocks.stack --port 9100` — then point
Settings → Credentials at `http://127.0.0.1:9100/ccc`, `.../netbox`, and
`.../ise/hook`. Failure injection: `POST /__mock__/config` (see
`tests/mocks/stack.py`).
