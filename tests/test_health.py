"""/healthz behavior."""

from __future__ import annotations

import asyncpg
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_healthz_returns_200_when_db_up(client_primary: AsyncClient) -> None:
    r = await client_primary.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "db": True, "node_role": "primary"}


async def test_healthz_replica_role_in_body(client_replica: AsyncClient) -> None:
    r = await client_replica.get("/healthz")
    assert r.status_code == 200
    assert r.json()["node_role"] == "replica"


async def test_healthz_returns_503_when_db_unreachable(
    client_primary: AsyncClient, pool: asyncpg.Pool
) -> None:
    # Close the pool to simulate DB unavailability; ping() catches and returns False.
    await pool.close()
    r = await client_primary.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] is False
