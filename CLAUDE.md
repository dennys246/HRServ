# CLAUDE.md — HRServ

Project-specific guidance for Claude Code working in this repo.

## What this repo is

HRServ is the FastAPI receiver service that replaces `flask.jib-jab.org/upload_json`. It accepts
HRF JSON uploads from the `hrfunc-web` frontend (pre-augmented with a `_hrf_submission`
envelope) and persists them in Postgres. Read [BOOTSTRAP.md](BOOTSTRAP.md) for the original plan
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the current system overview.

**Current state (2026-05):** Phase 2b is live — every upload through hrfunc.org dual-writes to
both the legacy backend (authoritative) and HRServ (shadow). Shadow validation window is running.
Phase 2c (test replica + backups) is the next big block.

Sibling repos worth knowing about:
- `/Users/dennyschaedig/Scripts/HRFunc` — the Python library (`pip install hrfunc`) that produces
  the JSON HRServ ingests. May be updated to better suit HRServ; flag rather than work around.
- `/Users/dennyschaedig/Scripts/hrfunc-web` — Flask frontend on Render that augments JSON before
  forwarding here (and to the legacy backend, during the shadow window).

## Development standards (hard rules)

These were stated explicitly by the user during bootstrap. Treat them as binding.

### 1. Tests for every promised behavior
Every public behavior the service promises (every endpoint contract, every auth rule, every
schema invariant) needs tests. When you change code, find the related tests and re-run them;
when you add behavior, write new tests. Don't ship a "we'll add tests later" PR — if you must,
get explicit user approval and file a follow-up issue.

### 2. Work in branches; deep parallel review before pushing
Never commit directly to `main`. Use a branch per change. **Before pushing**, run a deep parallel
review with two concurrent agents:
- **Execution review** — code correctness, edge cases, test adequacy.
- **Architectural review** — system fit, coupling, cross-repo effects (HRFunc, hrfunc-web,
  schema/replication implications).

Use the Agent tool with `Explore` or `general-purpose` subagents, dispatched in a single message
so they run concurrently. Surface findings to the user before pushing.

### 3. Root cause every issue
No band-aid fixes unless there's an explicit, scheduled moment to resolve the underlying issue.
When tests fail, when CI fails, when something behaves oddly — dig until you find the real cause
and fix it there. Don't loosen assertions, don't suppress exceptions to make a symptom go away,
don't `--no-verify`. If a band-aid is truly necessary right now, state it plainly, file the TODO
with a planned resolution moment, and surface it to the user.

### 4. Stop and ask on uncertain majors
When a major decision (API contract, data schema, deployment topology, security posture,
cross-repo behavior) arises and you have <80% confidence in the right answer, stop and ask via
AskUserQuestion. Batch questions when possible. Small judgment calls (variable names, file
layout, fixture shape) — just make a reasonable choice.

## Stack and tooling

- **Language**: Python 3.12+
- **Web**: FastAPI + uvicorn
- **DB**: Postgres 16 (asyncpg), JSONB content + metadata columns
- **Auth**: argon2-cffi hashing, three-layer (Cloudflare Access service token → app `x-api-key`
  → body validation)
- **Config**: pydantic-settings (env-driven)
- **Tooling**: uv (deps + venv), ruff (lint + format), pytest + pytest-asyncio, mypy

Run commands:
```bash
uv sync                          # install deps from uv.lock
uv run ruff check .              # lint
uv run ruff format .             # format
uv run mypy hrserv               # type check
uv run pytest                    # tests (needs DATABASE_URL set to a Postgres instance)
uv run hrserv-mint-key --label X # mint a new API key
```

For tests locally, the easiest path is `docker compose -f docker/docker-compose.test.yml up -d`
to get a throwaway Postgres on a non-default port, then export `DATABASE_URL` and run pytest.
(See `tests/README.md`.)

## Code conventions

- **Async everywhere on the request path.** asyncpg is async; FastAPI route handlers are async;
  the argon2 verify is CPU-bound and runs in a thread executor for the auth path.
- **No globals beyond the FastAPI app and the DB pool.** Both live on `app.state` and are wired
  up in the lifespan handler. Avoid module-level state for testability.
- **Errors return safe-to-flash text.** The frontend does `flash(f"Upload failed: {resp.text}")`,
  so error bodies must never leak stack traces, table names, or internal paths. Use plain English
  reason strings: "Invalid API key", "Payload too large", "JSON parse error".
- **No print statements.** Use the configured logger (`logging.getLogger("hrserv.<module>")`).
- **Type hints on every function signature.** mypy in strict mode.

## Testing patterns

- Tests connect to a real Postgres (the schema's behavior is the contract — mocks would diverge).
- Each test gets a fresh transaction that rolls back at teardown (`conftest.py:db` fixture).
- Endpoint tests use `httpx.AsyncClient` against the FastAPI app — no live uvicorn needed.
- `tests/fixtures/sample_hrf.json` is a minimal-but-realistic upload payload. Don't make it too
  large; we're testing semantics, not throughput.

## Topology and replication notes

- `NODE_ROLE=primary` accepts writes; `NODE_ROLE=replica` returns 503 on `/upload_json`. Both
  serve `/healthz`.
- Postgres replication runs over Tailscale (`pg_hba.conf` restricts replication to tailnet IPs).
  Schema migrations apply to both nodes via WAL — never run DDL on the replica.
- Failover is manual via `scripts/promote_replica.sh` and the runbook in `docs/FAILOVER.md`.

## Things to NOT do (per bootstrap)

- No read/list/search endpoints in MVP. Schema is ready; endpoints land when distribution is needed.
- No automated failover. Manual only.
- No retries/queues. Frontend handles retries via user retry.
- No web UI. Flask app is the UI.
- No email. Frontend sends email after HRServ returns 200.
- No HRFunc library dependency. Validation stays format-agnostic.
