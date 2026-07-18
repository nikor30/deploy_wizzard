# Security

## Reporting a vulnerability

Please open a **private security advisory** on GitHub (Security → Advisories →
"Report a vulnerability") rather than a public issue. Include steps to
reproduce and the affected version/commit. You should get a first response
within a few days.

## Security model

PnP Bridge is a **trusted-operator tool**: it is designed to run inside a
management network, operated by network engineers who already hold Catalyst
Center and NetBox credentials. It has **no built-in user authentication** —
anyone who can reach port 8060 can operate the wizard and read the logs.

**Deploy it accordingly:**

- Never expose port 8060 to an untrusted network or the internet.
- Put it behind a reverse proxy with authentication (or an equivalent network
  ACL/VPN) if more than a trusted team can reach it.

## How credentials are handled

- Catalyst Center passwords, NetBox tokens, and the webhook shared secret are
  encrypted at rest (Fernet/AES-128-CBC + HMAC) in the SQLite database.
- The encryption key comes from `PNPB_SECRET_KEY`, or is auto-generated on
  first start and stored as `secret.key` **next to the database** — protect
  the `/data` volume: whoever has both files can decrypt the secrets.
- The API is write-only for secrets: `GET /api/settings/credentials` returns
  masked values (`****abcd`) only; secrets are never echoed back to the UI.
- Log redaction is enforced and tested — tokens/passwords never reach the
  log sink, stdout, or fixtures. Report it as a vulnerability if you find one.
- The outbound ISE webhook can be signed with HMAC-SHA256
  (`X-PnPB-Signature`); verify it on the receiving side.

## TLS

TLS verification for Catalyst Center/NetBox is **on by default** and can be
disabled per service in Settings for lab gear with self-signed certificates.
Leave it on in production.

## Container hardening

The published Containerfile runs the app as a dedicated non-root user
(uid 10001), holds all state in the `/data` volume, and ships a container
HEALTHCHECK against `/api/health`.
