# HRServ architecture

High-level system overview for new operators. Read this first before touching
production. See `BOOTSTRAP.md` for the original design rationale; see
`docs/OPERATIONS.md` for day-to-day work.

## The 30-second picture

```
  ┌──────────────┐        ┌──────────────────┐        ┌──────────────────┐
  │  Researcher  │        │ hrfunc-web       │        │ Legacy backend   │
  │  with HRfunc │  POST  │ (Flask, Render)  │──────▶ │ flask.jib-jab.org│
  │  estimates   │ ─────▶ │ hrfunc.org       │        │ (authoritative)  │
  └──────────────┘        │                  │        └──────────────────┘
                          │  forward_to_     │
                          │   backend() ×2   │
                          │  (Phase 3 dual-  │
                          │   write)         │        ┌──────────────────┐
                          │                  │──────▶ │ HRServ (shadow)  │
                          └──────────────────┘        │ api.hrfunc.org   │
                                                      │ Cloudflare Tunnel│
                                                      │   ▼              │
                                                      │ hrserv-1         │
                                                      │ (jib-jab.org)    │
                                                      │   ▼              │
                                                      │ Postgres 16      │
                                                      │ hrf_submissions  │
                                                      └──────────────────┘
```

A researcher runs `montage.save("study_HRFs.json")` from the HRFunc library
on their machine. They upload that JSON through hrfunc.org's web UI. The
Flask app (`hrfunc-web`) augments the JSON with a `_hrf_submission` envelope
(submitter email, study, DOI, filenames, experimental context) and forwards
the augmented payload to BOTH the legacy backend (still authoritative) AND
HRServ (shadow validation). Once shadow proves stable for ~weeks, the
authoritative URL is flipped to HRServ.

## The three repos

| Repo | What it is | Where it runs |
|---|---|---|
| **HRFunc** (`pip install hrfunc`) | Python library for estimating hemodynamic response functions from fNIRS data. Produces `montage.save()` JSON files. | Researchers' own machines. |
| **hrfunc-web** | Flask web app — guides/docs site + upload form. Augments uploaded JSON with submitter metadata and forwards to one or two backends. | Render (`hrfunc.org`, `www.hrfunc.org`). |
| **HRServ** (this repo) | FastAPI receiver. Validates + persists augmented uploads in Postgres. Eventually serves read endpoints for the HRtree. | Homelab node(s); currently just `hrserv-1` (jib-jab.org). |

## Auth — three independent layers

Every write to `/upload_json` must clear all three:

1. **Cloudflare Access service token** at the edge. Validates
   `CF-Access-Client-Id` + `CF-Access-Client-Secret`. Misconfigured request
   never reaches the origin. Configured per-app in the Cloudflare dashboard
   (`hrserv-upload` Access app with `Service Auth` policy).
2. **App-level `x-api-key`** verified by HRServ via argon2 against the
   `api_keys` table. Plaintext is one-time-mint via `scripts/mint_key.py`;
   only argon2 hashes ever persist server-side. Scope check requires
   `ingest`.
3. **Body validation** — JSON parses, root is dict-or-list, NaN/Infinity
   rejected, `_hrf_submission` envelope warn-logged-if-missing, 5 MiB max,
   `stored_filename` extractable for idempotent inserts.

Losing any one layer doesn't compromise the others. This was a live
defense-in-depth proof during Phase 2a debugging: Access policies briefly
shipped with 0 attached policies → all `/healthz` requests 302-redirected
even though HRServ's own auth would have responded 200.

## Topology

**Today (post-Phase 2b):**
- `hrserv-1` is the only production node. Linux x86_64, on the legacy
  jib-jab.org machine that originally ran the Flask backend (legacy Flask
  still runs alongside HRServ; they share nginx + a host).
- Single point of failure. Single disk. No replica, no off-site backup yet.

**Future (Phase 2c, May–Aug 2026):**
- A separate home-built Linux PC joins as a **test replica** to validate
  streaming replication, failover scripts, and the backup chain before
  production stakes get involved. Its data is non-canonical; can be wiped.

**Future (Aug 2026+):**
- A Mac Mini (already purchased, arrives August) replaces the test PC as
  `hrserv-2` in a separate physical location. At that point the triple-copy
  backup story (local + cross-shipped + B2) becomes real.

**Node naming convention:** `hrserv-1`, `hrserv-2` are role-agnostic
machine identities. After a failover the names stay put even though roles
swap. Role-specific filenames (`docker-compose.primary.yml`,
`docker-compose.replica.yml`, `primary.conf`, `replica.conf`) keep numbers
OUT — they describe what role the file configures, not which physical box.

## Storage

Postgres 16 in a docker container, named volume `hrserv_pg_data_primary`.
Schema (per `migrations/0001_init.sql`):

- `api_keys` — `id TEXT PRIMARY KEY` (this column is what the `mint_key --label X` CLI calls the "label" — same thing, different layer), `key_hash` (argon2), `scopes TEXT[]`, `created_at`, `revoked_at`.
- `hrf_submissions` — id (BIGSERIAL), stored_filename (UNIQUE), uploaded_at,
  hot-extracted columns (`submitter_email`, `study`, `doi`),
  `api_key_id` FK, `client_ip` (INET), `size_bytes`, `content_sha256`
  (sha256 of the augmented JSON bytes), `content` (JSONB).
- Indexes: `uploaded_at DESC`, `study`, `doi WHERE NOT NULL`,
  `submitter_email`, **GIN on `content jsonb_path_ops`** (for future
  `content @> '{...}'` lookups when read endpoints land).

**JSONB roundtrip caveat:** Postgres reformats JSONB internally — strips
whitespace, sorts keys, deduplicates. So `content_sha256` (sha256 of
incoming bytes) will NOT match `sha256(jsonb_text(content))` after a SELECT.
The hash is "what was uploaded" provenance, not a recomputable checksum.

**GIN opclass caveat:** `jsonb_path_ops` supports `@>` only, not `?` /
`?|` / `?&` / `@@`. Future read endpoints must phrase filters as
`content @> '{"_hrf_submission":{"task":"flanker"}}'` not
`content -> '_hrf_submission' ->> 'task' = 'flanker'`. The latter works
but seq-scans.

## Network

- **Inbound public traffic** to HRServ goes ONLY through Cloudflare Tunnel.
  No host ports are published for the application. The tunnel connector
  (`cloudflared` container) opens an outbound QUIC connection to Cloudflare
  POPs (currently `den01`, `den03`, `dfw06`, `dfw08`); ingress traffic
  rides those connections back.
- **Tailscale** is the private network for inter-node Postgres replication.
  `hrserv-1`'s tailnet IP is `100.91.182.4`. The Postgres container binds
  `${TAILSCALE_IP}:5432` (NOT public). When hrserv-2 joins, replication
  will dial that IP over the tailnet.
- **`pg_hba.conf`** rule for replication is set to `127.0.0.1/32` until the
  test replica gets a real tailnet IP — substituted via
  `scripts/configure_pg_hba.sh`.

## What's running where

| Service | Container | Host port | Notes |
|---|---|---|---|
| `postgres` | postgres:16 | `${TAILSCALE_IP}:5432` | Bound to tailnet IP only |
| `hrserv` | hrserv:local (built locally) | 8000 internal only | uvicorn `--workers 2` |
| `cloudflared` | cloudflare/cloudflared:latest | none (outbound only) | Authenticates with `TUNNEL_TOKEN` |

Compose project name is `hrserv` (intentionally role-agnostic — survives
role flips).

## Key dependencies & versions

- Python 3.12 in the HRServ container (3.13 OK locally for tests)
- asyncpg ≥0.30
- argon2-cffi ≥23.1 (uses argon2id with library defaults — exceeds OWASP min)
- pydantic + pydantic-settings ≥2.x
- FastAPI ≥0.115
- Postgres 16 (replica must match the major version)

## Cross-cutting design choices

1. **Idempotent writes.** Same `stored_filename` → same `id` returned. Lets
   the frontend retry safely if a forward times out.
2. **Plain-text error bodies.** Every 4xx/5xx returns a one-line plain-text
   reason — the frontend does `flash(f"Upload failed: {resp.text}")`.
   `_validation_error_to_plaintext` and `_unhandled_exception_to_plaintext`
   in `hrserv/main.py` enforce this for every error path.
3. **Format-agnostic ingest.** HRServ doesn't depend on the HRFunc library;
   it only checks JSON shape + `_hrf_submission` presence. Library version
   bumps don't force server redeploys.
4. **NODE_ROLE required (no default).** Misconfigured node fails to start
   instead of silently picking the wrong role.

See also:
- `BOOTSTRAP.md` — the original design doc
- `docs/PHASE_2A_HRSERV1_SETUP.md` — first-node standup runbook
- `docs/NEW_NODE_SETUP.md` — adding additional nodes
- `docs/OPERATIONS.md` — day-to-day
- `docs/KEY_ROTATION.md` — API key lifecycle
- `docs/FAILOVER.md` — manual failover (not yet possible without hrserv-2)
- `docs/BACKUP_RESTORE.md` — backup story (not yet wired)
- `docs/SHADOW_WINDOW.md` — running and evaluating shadow validation
- `docs/FOLLOWUPS.md` — deferred items
