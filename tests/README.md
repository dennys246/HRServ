# HRServ tests

Tests run against a real Postgres — the schema and JSONB behavior are the
contract HRServ promises, and mocks would diverge over time. The DB connection
is read from `DATABASE_URL`.

## Local

```bash
docker compose -f docker/docker-compose.test.yml up -d
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/hrserv_test
uv run pytest
```

Each test runs inside its own transaction that rolls back at teardown
(`conftest.py:db_conn` fixture), so tests don't see each other's writes.

## CI

The GitHub Actions workflow (`.github/workflows/ci.yml`) spins up a Postgres
service container, applies `migrations/0001_init.sql`, then runs the same
`pytest` invocation as local.
