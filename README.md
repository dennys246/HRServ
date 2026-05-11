# HRServ

FastAPI receiver service for HRF JSON uploads from the
[hrfunc](https://github.com/dennys246/hrfunc) ecosystem.

HRServ replaces `flask.jib-jab.org/upload_json`. The `hrfunc-flask-app` frontend
augments each upload with a `_hrf_submission` envelope (submitter email, study,
DOI, filenames) and forwards it here. HRServ validates, hashes, and persists the
payload in Postgres.

The MVP is a **receiver only** — no read/query/distribution endpoints yet. The
schema and the streaming-replica setup are ready for those to land once the
GitHub-hosted HRF database needs to come online.

## Quick reference

| | |
|---|---|
| Public endpoint | `POST https://api.hrfunc.org/upload_json` |
| Health | `GET https://api.hrfunc.org/healthz` |
| Auth | Cloudflare Access service token + app `x-api-key` (argon2-hashed) |
| Topology | Primary + streaming replica over Tailscale |
| Bootstrap | [BOOTSTRAP.md](BOOTSTRAP.md) |
| Failover | [docs/FAILOVER.md](docs/FAILOVER.md) |
| Backup/restore | [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
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
  docs/                    FAILOVER.md, BACKUP_RESTORE.md
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
