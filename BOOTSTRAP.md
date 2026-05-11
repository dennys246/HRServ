# HRServ — Bootstrap

A self-contained brief for starting the `hrserv` repo.

## What HRServ is

The receiver service that replaces `https://flask.jib-jab.org/upload_json`. It accepts HRF (Hemodynamic Response Function) JSON uploads from the `hrfunc-web` frontend, validates them, and persists them in Postgres. Exposed to the internet only through a Cloudflare Tunnel.

**Topology: two nodes from day one.**

- **`hrserv-1`** (initially primary) — owns writes. Serves `POST /upload_json` at `api.hrfunc.org` behind a Cloudflare Tunnel.
- **`hrserv-2`** (initially replica) — Postgres streaming replica of `hrserv-1` via Tailscale. Hot standby for failover; serves future read endpoints (`/api/hrfs/*`) once distribution lands. Has its own tunnel, inactive in DNS until needed.

**Node-name convention: role-agnostic, numeric.** `hrserv-1`/`hrserv-2` are machine identities, not role labels. After a failover, `hrserv-2` may be the primary and `hrserv-1` a replica — the names stay put. Keep numbers OUT of role-specific filenames (`docker-compose.primary.yml` / `.replica.yml`), because those files describe the role the host is currently running.

**MVP scope: receiver only.** No read/query/distribution endpoints yet. The schema and replica are built so they can be added later when the GitHub-hosted HRF database outgrows the repo.

## Stack (decided)

- **FastAPI** + uvicorn (the application)
- **Postgres 16** for both file bytes (`jsonb`) and metadata; streaming replication primary → replica
- **Tailscale** as the inter-node private network for Postgres replication traffic — Postgres is never exposed to the public internet
- **cloudflared** in connector mode on each node, no host ports published
- **Cloudflare Access** (service tokens) protects `POST /upload_json` at the path level on top of the app-level `x-api-key`
- **docker-compose** runs the stack on each node

## Contract to implement (must match the Flask frontend exactly)

The Flask frontend forwards uploads to `UPLOAD_URL` using this shape (see `forward_to_backend` in the frontend's `app.py`):

```
POST /upload_json
Headers:
  CF-Access-Client-Id:     <service token id>      (Phase 1.5 of frontend will send this)
  CF-Access-Client-Secret: <service token secret>  (Phase 1.5 of frontend will send this)
  x-api-key:               <plaintext app key>
Body: multipart/form-data
  jsonFile: <filename>, <augmented JSON bytes>
```

- The two `CF-Access-Client-*` headers are validated by Cloudflare Access **before** the request reaches HRServ. HRServ itself does not need to verify them — but it should reject requests missing the `Cf-Access-Authenticated-User-Email` / `Cf-Access-Jwt-Assertion` headers that Access injects on success, as defense-in-depth in case the policy is ever misconfigured.
- The `x-api-key` is the **app-level** secret HRServ validates against the `api_keys` table.
- The JSON body is **pre-augmented** by the frontend — it contains a top-level `_hrf_submission` key with all submitter metadata (email, study, DOI, dataset ownership, experimental context, `uploaded_at`, `original_filename`, `stored_filename`).
- Max body size: 5 MB.
- Filename format: `{root}_{YYYY-MM-DD_HH-MM-SS}_{8-hex}.json`.
- Response on success: `200 {"ok": true, "id": <int>, "stored_filename": "..."}`. Frontend only checks `status_code == 200`.
- Response on failure: any non-200 with safe-to-flash text body (frontend does `flash(f"Upload failed: {resp.text}")`).

Add a `GET /healthz` that returns `200 {"status": "ok"}` after pinging the DB. Frontend can poll it to auto-toggle the maintenance banner. `/healthz` should be on a public Access policy (no service token required) so monitoring can hit it.

## Repo layout

```
hrserv/
  hrserv/
    __init__.py
    main.py            # FastAPI app + lifespan (DB pool)
    config.py          # pydantic-settings: DATABASE_URL, MAX_UPLOAD_BYTES, NODE_ROLE
    db.py              # asyncpg pool, query helpers
    auth.py            # x-api-key header → hashed lookup in api_keys
    models.py          # pydantic schemas
    routes/
      __init__.py
      ingest.py        # POST /upload_json (primary only)
      health.py        # GET /healthz
  migrations/
    0001_init.sql      # schema below
  docker/
    Dockerfile
    docker-compose.primary.yml   # role: primary postgres + hrserv + cloudflared
    docker-compose.replica.yml   # role: replica postgres + hrserv (read-only) + cloudflared
    cloudflared.yml              # tunnel ingress config (per-node)
    postgres/
      primary.conf               # wal_level=replica, max_wal_senders, etc.
      replica.conf               # hot_standby=on, primary_conninfo via env
      pg_hba.conf                # allow replication from Tailscale IPs only
  scripts/
    mint_key.py        # one-shot: hash + insert an api_keys row
    backup.sh          # pg_dump → ship to peer over Tailscale + push to B2
    promote_replica.sh # failover runbook: promote the current replica to primary
  docs/
    FAILOVER.md        # manual failover runbook
    BACKUP_RESTORE.md  # backup + restore drill instructions
  tests/
    test_ingest.py
    test_auth.py
  pyproject.toml
  README.md
  BOOTSTRAP.md         # this file
```

## Postgres schema (migrations/0001_init.sql)

```sql
CREATE TABLE api_keys (
  id            TEXT PRIMARY KEY,         -- short label, e.g. "flask-frontend"
  key_hash      TEXT NOT NULL UNIQUE,     -- argon2 hash of the plaintext secret
  scopes        TEXT[] NOT NULL DEFAULT '{ingest}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at    TIMESTAMPTZ
);

CREATE TABLE hrf_submissions (
  id                BIGSERIAL PRIMARY KEY,
  stored_filename   TEXT NOT NULL UNIQUE,
  original_filename TEXT,
  uploaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitter_email   TEXT,
  study             TEXT,
  doi               TEXT,
  api_key_id        TEXT REFERENCES api_keys(id),
  client_ip         INET,
  size_bytes        INTEGER NOT NULL,
  content_sha256    TEXT NOT NULL,
  content           JSONB NOT NULL,
  CHECK (jsonb_typeof(content) IN ('object','array'))
);

CREATE INDEX ON hrf_submissions (uploaded_at DESC);
CREATE INDEX ON hrf_submissions (study);
CREATE INDEX ON hrf_submissions (doi) WHERE doi IS NOT NULL;
CREATE INDEX ON hrf_submissions (submitter_email);
CREATE INDEX ON hrf_submissions USING GIN (content jsonb_path_ops);
```

The GIN index is the bet on future distribution — lets eventual `GET /api/hrfs?...` endpoints filter on arbitrary `content @> '{...}'` patterns efficiently.

Hot-field extraction at ingest: read `content['_hrf_submission']` and populate `submitter_email`, `study`, `doi`. The rest stays in `content` only.

## Ingest endpoint behavior (primary only)

1. Reject 503 if `NODE_ROLE != "primary"`.
2. Verify `x-api-key` header → argon2 hash compare against `api_keys` rows (`revoked_at IS NULL`, scope contains `ingest`). 401 on miss. (Cloudflare Access has already authenticated the service token before this point — HRServ trusts the Access JWT was present.)
3. Reject 413 if `Content-Length > MAX_UPLOAD_BYTES` (5 MB).
4. Read `jsonFile` part, decode UTF-8, `json.loads`. Reject 400 on parse error.
5. Require result to be `dict | list`. Reject 400 otherwise.
6. If dict, extract `_hrf_submission` (warn-log if missing). Pull `email`, `study`, `doi`, `original_filename`, `stored_filename` from it.
7. Compute `sha256(payload_bytes)`, capture `CF-Connecting-IP` for `client_ip`.
8. INSERT row. `ON CONFLICT (stored_filename) DO NOTHING RETURNING id` — idempotent retries.
9. Return `200 {"ok": true, "id": <int>, "stored_filename": "..."}`.

## Auth layers (defense in depth)

Three independent checks must all pass to write data:

1. **Cloudflare Access (service token)** — Cloudflare validates `CF-Access-Client-Id` + `CF-Access-Client-Secret` at the edge. Requests without valid tokens never reach the origin. Tokens are scoped to the `/upload_json` path on `api.hrfunc.org`.
2. **App-level `x-api-key`** — HRServ checks against argon2-hashed `api_keys` table. Per-client labels enable per-key revocation.
3. **Body validation** — JSON shape, size, `_hrf_submission` presence.

Losing any one layer doesn't compromise the others.

### Minting keys

`scripts/mint_key.py` is a one-shot CLI:
```
python scripts/mint_key.py --label flask-frontend
# prints a fresh secret to stdout ONCE; stores argon2 hash in api_keys
```
Use [argon2-cffi](https://pypi.org/project/argon2-cffi/). Plaintext secret never persists.

## Deployment

### Per-node services (docker-compose)

**Whichever node is currently primary (`docker-compose.primary.yml`)**:
- **postgres**: `postgres:16`, bind-mount volume, `postgresql.conf` mounted from `docker/postgres/primary.conf`. Listens on Tailscale interface only (not `0.0.0.0`). `pg_hba.conf` allows replication from the peer's Tailscale IP only.
- **hrserv**: `NODE_ROLE=primary`. Mounts the app.
- **cloudflared**: ingress `api.hrfunc.org` → `hrserv:8000`.

**Whichever node is currently replica (`docker-compose.replica.yml`)**:
- **postgres**: `postgres:16` in standby mode. `primary_conninfo` points to the primary's Tailscale IP. `standby.signal` file present.
- **hrserv**: `NODE_ROLE=replica`. Returns 503 on `/upload_json` (so misrouted writes fail loudly). `/healthz` still works.
- **cloudflared**: ingress configured but DNS not pointed here. Activated during failover.

At bootstrap, `hrserv-1` runs `docker-compose.primary.yml` and `hrserv-2` runs `docker-compose.replica.yml`. After a failover the roles can swap without renaming hosts.

### Cloudflare Access policy

In the Cloudflare dashboard, create an Access Application:
- **Hostname**: `api.hrfunc.org`
- **Path**: `/upload_json` (path-scoped, so `/healthz` and future read paths can have different policies)
- **Policy**: Service Auth → require service token `flask-frontend`
- **Mint the service token** in the Access settings; capture `Client ID` and `Client Secret` (one-time visibility).

### Backups

Three copies of every nightly dump:

1. **Local on the originating node** (e.g., `/var/backups/hrserv/`)
2. **Cross-shipped to the peer node** via Tailscale + `rsync` or `restic`
3. **Pushed to Backblaze B2** (free tier 10 GB; trivially fits years of HRF dumps)

`scripts/backup.sh` runs on both nodes nightly via cron. Encrypts dumps with `age` or `restic` before leaving the box.

**Backup drill is mandatory**: at least once before Phase 3, restore a dump on a scratch directory and verify row counts + a couple of JSON payloads. Document the procedure in `docs/BACKUP_RESTORE.md`.

## Phase 2 — host-side setup checklist

Do **`hrserv-1` first, end to end, before `hrserv-2`**. Replication setup is easier when one side already works.

### hrserv-1 (initial primary)

1. [ ] Install Docker + docker-compose + Tailscale on the host.
2. [ ] Join Tailscale, note the node's tailnet IP (e.g., `100.64.x.x`).
3. [ ] In Cloudflare dashboard: create tunnel for `hrserv-1`, copy `TUNNEL_TOKEN`.
4. [ ] In Cloudflare dashboard: point `api.hrfunc.org` DNS at the tunnel.
5. [ ] In Cloudflare dashboard: create Access Application for `api.hrfunc.org/upload_json`, mint service token `flask-frontend`. Save `CF-Access-Client-Id` + `CF-Access-Client-Secret` to password manager.
6. [ ] Bring up `docker-compose.primary.yml` with **just** `cloudflared` + a FastAPI `/healthz` returning `{"ok": true}`. `curl https://api.hrfunc.org/healthz` from outside the LAN must return 200.
7. [ ] Add Postgres to the compose. Configure `primary.conf` for replication (`wal_level=replica`, `max_wal_senders=3`, etc.). Update `/healthz` to also ping DB. Re-curl.
8. [ ] Run `migrations/0001_init.sql` via psql.
9. [ ] Mint a key labeled `flask-frontend` via `mint_key.py`. Save plaintext to password manager.

### hrserv-2 (initial replica)

10. [ ] Install Docker + docker-compose + Tailscale.
11. [ ] Join Tailscale, verify ping to `hrserv-1`'s tailnet IP.
12. [ ] In Cloudflare dashboard: create separate tunnel for `hrserv-2`. **Do not point any DNS at it yet.**
13. [ ] On `hrserv-1`: run `./scripts/configure_pg_hba.sh <hrserv-2 tailnet IP>` to substitute the replica's /32 into `docker/postgres/pg_hba.conf`. The shipped template has a deliberately-invalid placeholder so a forgotten substitution fails loudly. Commit the resulting file. Restart Postgres so the new rules take effect.
14. [ ] On `hrserv-2`: `pg_basebackup` from `hrserv-1` over Tailscale. Bring up `docker-compose.replica.yml` with `standby.signal` and `primary_conninfo` set.
15. [ ] Verify `SELECT pg_is_in_recovery();` returns `t` on `hrserv-2` and `f` on `hrserv-1`.
16. [ ] Verify writes on `hrserv-1` appear on `hrserv-2` within a few seconds (insert a test `api_keys` row).

### Both nodes

17. [ ] Write `scripts/backup.sh`. Wire up B2 (free tier account) + cross-ship to peer via Tailscale.
18. [ ] **Restore drill**: take a dump from B2, restore on a scratch dir, verify. Document in `docs/BACKUP_RESTORE.md`.
19. [ ] Write `docs/FAILOVER.md` — exact steps to promote the current replica if the current primary dies.
20. [ ] Add cron entries for nightly backups on both nodes.

Only after every box is ticked, move to Phase 3 (ingest endpoint code).

## Phase 3 — connecting the frontend

Once `POST /upload_json` works against the tunnel with curl + a real past submission:

1. In `hrfunc-web`, set on Render:
   - `HRFUNC_SHADOW_URL=https://api.hrfunc.org/upload_json`
   - `HRFUNC_ACCESS_CLIENT_ID=<service token id>`
   - `HRFUNC_ACCESS_CLIENT_SECRET=<service token secret>`
   - `HRFUNC_API_KEY_HRSERV=<minted app key>` (separate from existing `HRFUNC_API_KEY`)
2. Land the dual-write change in the Flask app (Phase 3 frontend work — not yet done).
3. Don't change `HRFUNC_UPLOAD_URL` yet — old `flask.jib-jab.org` backend stays authoritative.
4. Watch logs daily for divergence.
5. After N weeks of clean shadow: flip `HRFUNC_UPLOAD_URL=https://api.hrfunc.org/upload_json`, drop or invert the shadow.

## Resolved decisions

- ✅ **Hostname**: `api.hrfunc.org` — single namespace for ingest + future reads, path-level Access policies handle auth differences.
- ✅ **Cloudflare Access**: Service tokens from day one, scoped to `/upload_json` path.
- ✅ **Topology**: Two nodes from day one — primary + replica via Postgres streaming replication over Tailscale.
- ✅ **Node naming**: `hrserv-1`, `hrserv-2`. Role-agnostic, scales cleanly, names stay put during failover. Numbers stay OUT of role-specific filenames.
- ✅ **Backups**: Local + cross-shipped to peer + Backblaze B2 free tier. Triple-copy strategy.
- ✅ **Files in Postgres** as `jsonb` (not on disk). Reconsider only if files balloon past 5 MB or backups become painful.

## Resolved hardware plan

- ✅ **hrserv-1 (production primary, now):** the legacy "HRF receive/send" Linux x86-64 PC that has historically run `flask.jib-jab.org/upload_json`. The old Flask service keeps running on this box during the shadow phase; HRServ comes up on its own internal port + Cloudflare Tunnel for `api.hrfunc.org/upload_json`.
- ✅ **hrserv-2 (production replica, ~August 2026):** Mac Mini purchased for this purpose. macOS host, arm64. All our images have arm64 variants. Cutover from the test PC happens when the Mini arrives.
- ✅ **Test replica (parallel, May 2026 → August 2026):** a separate home-built Linux x86-64 PC, used to validate streaming replication and failover scripts ahead of the Mini's arrival. Data on this node is **non-canonical** — it can be wiped and rebuilt freely. Backups stay local + B2 on hrserv-1 during this period; the cross-shipped backup leg through the test PC does NOT count as a real off-site copy until the Mini is in a different building. **Decommission when the Mini arrives:** wipe the data directory, remove the node from the tailnet, re-run `scripts/configure_pg_hba.sh` on hrserv-1 with the Mini's tailnet IP. Do NOT leave the test PC running as a third node.

## Still to decide during Phase 2

- [ ] Physical locations of hrserv-1 and the eventual Mac Mini (must be in different buildings/sites to make the triple-copy backup story fully off-site)
- [ ] Argon2 parameters (library defaults unless reason otherwise)
- [ ] Failover trigger threshold (manual only for MVP; automation is post-MVP)

## Things to NOT build in MVP

- No read / list / search endpoints. Schema and replica are ready; endpoints deferred until distribution is actually needed.
- No web UI. HRServ is API-only. The Flask frontend is the UI.
- No automated failover. Manual promotion via `promote_replica.sh` per the runbook.
- No multi-tenant logic. One Cloudflare account, two tunnels (one per node), one logical DB.
- No email sending — stays in the Flask frontend, fires only on HRServ 200.
- No retries / queues. Frontend's `try/except` flashes errors and user retries. Revisit if shadow phase reveals flakiness.

## Reference: frontend wiring

After **Phase 1** of `hrfunc-web` (landed, commit 276eaf7): frontend reads `HRFUNC_UPLOAD_URL` from env, calls `forward_to_backend(UPLOAD_URL, filename, augmented_bytes)`.

After **Phase 1.5** (landing next): `forward_to_backend` will also send `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers when those env vars are set. No-op for the current `flask.jib-jab.org` backend (which ignores them); required for `api.hrfunc.org`.

After **Phase 3** (frontend dual-write, not yet landed): a second `HRFUNC_SHADOW_URL` env var triggers parallel forwarding for the shadow validation period. Old backend stays authoritative until cutover.
