# PnP Bridge

A self-hosted deployment wizard that onboards network devices using **NetBox** as the
source of truth and **Cisco Catalyst Center** as the deployment engine: match unclaimed
PnP devices by serial, claim them to the right site (Day-0), provision Day-N templates
with NetBox-filled variables, notify Cisco ISE via webhook, and set the device `active`
in NetBox when done.

## Quick start

```bash
podman build -t pnp-bridge:dev -f Containerfile .   # or: make image
podman run --rm -p 8060:8060 -v pnpb-data:/data pnp-bridge:dev
```

Open **http://localhost:8060** — no configuration needed to start. Add your Catalyst
Center, NetBox and webhook credentials under **Settings → Credentials** (each block has
a *Test connection* button), then map your sites under **Settings → Site Mapping**.

Secrets are encrypted at rest with a Fernet key. By default the key is generated on
first start and stored as `secret.key` on the `/data` volume — keep that volume (or set
the key explicitly) or stored credentials cannot be decrypted:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
podman run --rm -p 8060:8060 -e PNPB_SECRET_KEY=<key> -v pnpb-data:/data pnp-bridge:dev
```

Runtime environment variables: `PNPB_SECRET_KEY` (optional, see above), `PNPB_DB_PATH`
(default `/data/pnpb.sqlite`), `PNPB_LOG_LEVEL` (default `INFO`), `PNPB_PORT`
(default `8060`).

## Development

```bash
make dev    # backend on :8060 with reload + vite dev server
make lint   # ruff + mypy + eslint + prettier
make test   # pytest + vitest
make image  # build the container image
```

See `CLAUDE.md` for the full specification and `PLAN.md` for the implementation
roadmap and current status.
