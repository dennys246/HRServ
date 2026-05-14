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


async def test_healthz_supports_head_for_monitoring_probes(
    client_primary: AsyncClient,
) -> None:
    """Monitoring tools (UptimeRobot, BetterStack, Pingdom) default to HEAD.
    Without the explicit HEAD route, the GET-only endpoint returns 405 and the
    monitor flaps even though the service is healthy. Verify HEAD works.
    """
    r = await client_primary.head("/healthz")
    assert r.status_code == 200
    # Per RFC 7231 §4.3.2, HEAD MUST NOT send a body. Starlette enforces this
    # automatically for routes that declare both GET and HEAD methods.
    assert r.content == b""


async def test_healthz_head_returns_503_when_db_unreachable(
    client_primary: AsyncClient, pool: asyncpg.Pool
) -> None:
    """HEAD should mirror GET's status semantics, including the 503 path."""
    await pool.close()
    r = await client_primary.head("/healthz")
    assert r.status_code == 503
    assert r.content == b""
