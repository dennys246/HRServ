# HRServ

FastAPI receiver service for HRF JSON uploads from the
[hrfunc](https://github.com/dennys246/hrfunc) ecosystem.

HRServ replaces `flask.jib-jab.org/upload_json`. The `hrfunc-web` frontend
augments each upload with a `_hrf_submission` envelope (submitter email, study,
DOI, filenames) and forwards it here. HRServ validates, hashes, and persists the
payload in Postgres.

The MVP is a **receiver only** — no read/query/distribution endpoints yet. The
schema and the streaming-replica setup are ready for those to land once the
GitHub-hosted HRF database needs to come online.

**Current status (2026-07-15):** HRServ is authoritative (cutover from the
legacy backend happened 2026-05-14). Production runs on **big-mac-mini**
(Mac Mini, macOS + Colima) behind `api.hrfunc.org` — a fresh-primary standup
performed while jib-jab was down, with the empty-dataset trade-off accepted
deliberately. jib-jab (Linux) is pending revival and re-seed as the
streaming replica, which will restore two-node redundancy. External
monitoring (UptimeRobot → Pushover) watches `/healthz` per-node and in
production.

## Quick reference

| | |
|---|---|
| Public endpoint | `POST https://api.hrfunc.org/upload_json` |
| Health | `GET https://api.hrfunc.org/healthz` |
| Auth | Cloudflare Access service token + app `x-api-key` (argon2-hashed) |
| Topology | primary = `big-mac-mini` (macOS/Colima); `jib-jab` (Linux) re-seeding as replica |
| Original plan | [BOOTSTRAP.md](BOOTSTRAP.md) |
| System overview | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Day-to-day ops | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| **Node setup (any OS)** | [docs/NEW_NODE_SETUP.md](docs/NEW_NODE_SETUP.md) — start here for new machines |
| macOS boot chain | [deploy/launchd/README.md](deploy/launchd/README.md) |
| First-node history (Linux) | [docs/PHASE_2A_HRSERV1_SETUP.md](docs/PHASE_2A_HRSERV1_SETUP.md) |
| Key rotation | [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md) |
| Monitoring (wired 2026-07) | [docs/MONITORING.md](docs/MONITORING.md) |
| Failover | [docs/FAILOVER.md](docs/FAILOVER.md) — read its KNOWN ISSUES banner first |
| Network debugging | [docs/NETWORK_TROUBLESHOOTING.md](docs/NETWORK_TROUBLESHOOTING.md) |
| Backup/restore (not yet wired) | [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
| Shadow validation (historical) | [docs/SHADOW_WINDOW.md](docs/SHADOW_WINDOW.md) |
| Deferred items | [docs/FOLLOWUPS.md](docs/FOLLOWUPS.md) |
| Development standards | [CLAUDE.md](CLAUDE.md) |

## Local development

```bash
# Install deps (uv handles the venv).
uv sync

# Bring up a throwaway Postgres on port 55432.
docker compose -f docker/docker-compose.test.yml up -d

# Point the app/tests at it.
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/hrserv_test
export NODE_ROLE=primary                 # required (no default) so misconfigured nodes fail to start
export REQUIRE_CF_ACCESS_HEADERS=false   # the Access edge isn't in front of us locally
# Optional: override allowed CORS origins for /healthz polling (CSV).
# Defaults to https://hrfunc.org,https://www.hrfunc.org,http://localhost:5000
# export CORS_ORIGINS="https://hrfunc.org,https://www.hrfunc.org"

# Run lint, types, tests.
uv run ruff check .
uv run mypy hrserv
uv run pytest

# Run the server (factory mode — see hrserv/main.py for the rationale).
uv run uvicorn hrserv.main:create_app --factory --reload
```

## Repo layout

```
hrserv/
  hrserv/                  Python package (FastAPI app + helpers)
    main.py                app factory, lifespan, pool wiring
    config.py              pydantic-settings; env -> Settings
    db.py                  asyncpg pool, query helpers
    auth.py                argon2 verify + scope check
    models.py              pydantic request/response models
    routes/
      ingest.py            POST /upload_json
      health.py            GET /healthz
    scripts/mint_key.py    `hrserv-mint-key` console script
  migrations/0001_init.sql baseline schema
  docker/                  Dockerfile, compose (primary/replica/test), postgres configs
  scripts/                 backup.sh, promote_replica.sh, mint_key.py (thin wrapper)
  docs/                    ARCHITECTURE, OPERATIONS, NEW_NODE_SETUP, KEY_ROTATION,
                           SHADOW_WINDOW, MONITORING, PHASE_2A_HRSERV1_SETUP,
                           FAILOVER, BACKUP_RESTORE, FOLLOWUPS
  tests/                   pytest suite (real Postgres, rolled-back transactions)
  pyproject.toml           uv + ruff + mypy + pytest config
```

## Contract HRServ honors (must not break)

```
POST /upload_json
Headers:
  CF-Access-Client-Id:     <service token id>
  CF-Access-Client-Secret: <service token secret>
  x-api-key:               <plaintext app key>
Body: multipart/form-data
  jsonFile: <filename>, <augmented JSON bytes (<=5MB)>

Success: 200  {"ok": true, "id": <int>, "stored_filename": "..."}
Failure: 4xx/5xx with a plain-text body safe to surface via flash()
```

See [BOOTSTRAP.md](BOOTSTRAP.md) §"Contract" and §"Ingest endpoint behavior"
for the canonical step list.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
