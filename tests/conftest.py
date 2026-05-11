"""Shared pytest fixtures.

Tests run against a real Postgres reached via `DATABASE_URL`. Tables are
truncated between tests rather than relying on transaction-level rollback —
asyncpg pools acquire fresh connections for each request, so transaction-per-test
would need a custom pool wrapper. TRUNCATE is simpler and fast enough at this
scale.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from hrserv.auth import generate_secret, hash_secret
from hrserv.config import NodeRole, Settings
from hrserv.db import close_pool, create_pool, insert_api_key
from hrserv.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _require_database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip(
            "DATABASE_URL not set. Run "
            "`docker compose -f docker/docker-compose.test.yml up -d` "
            "and export DATABASE_URL.",
            allow_module_level=False,
        )
    return dsn


@pytest.fixture(scope="session")
def database_url() -> str:
    return _require_database_url()


@pytest.fixture(scope="session")
def settings_primary(database_url: str) -> Settings:
    """Settings for tests of the primary node — accepts writes."""
    return Settings(
        database_url=database_url,
        node_role=NodeRole.PRIMARY,
        require_cf_access_headers=False,
        max_upload_bytes=5 * 1024 * 1024,
    )


@pytest.fixture(scope="session")
def settings_replica(database_url: str) -> Settings:
    """Settings for tests of the replica node — refuses writes with 503."""
    return Settings(
        database_url=database_url,
        node_role=NodeRole.REPLICA,
        require_cf_access_headers=False,
    )


@pytest.fixture(scope="session")
def settings_with_cf_required(database_url: str) -> Settings:
    """Settings with the Cloudflare Access header check turned on."""
    return Settings(
        database_url=database_url,
        node_role=NodeRole.PRIMARY,
        require_cf_access_headers=True,
    )


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """Per-test asyncpg pool. Truncates app tables before yielding."""
    pool = await create_pool(database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE TABLE hrf_submissions RESTART IDENTITY CASCADE;"
                "TRUNCATE TABLE api_keys RESTART IDENTITY CASCADE;"
            )
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
async def seeded_key(pool: asyncpg.Pool) -> tuple[str, str]:
    """Insert a fresh API key, return (label, plaintext_secret)."""
    label = "test-frontend"
    secret = generate_secret()
    await insert_api_key(pool, label=label, key_hash=hash_secret(secret), scopes=["ingest"])
    return label, secret


@pytest.fixture
def sample_payload() -> dict[str, Any]:
    """Load the canonical sample HRF JSON for upload tests."""
    return json.loads((FIXTURES_DIR / "sample_hrf.json").read_text())


@pytest.fixture
def sample_payload_bytes(sample_payload: dict[str, Any]) -> bytes:
    return json.dumps(sample_payload).encode("utf-8")


async def _make_client(settings: Settings, pool: asyncpg.Pool) -> AsyncIterator[AsyncClient]:
    """Build a FastAPI app wired to the shared pool and a matching async client.

    Bypasses the lifespan handler so tests don't have to spin up/tear down a
    pool per app instance. The route handlers read pool + settings from
    `app.state`, which is what they'd see in production.
    """
    app = create_app(settings=settings)
    app.state.pool = pool
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def client_primary(
    settings_primary: Settings, pool: asyncpg.Pool
) -> AsyncIterator[AsyncClient]:
    async for c in _make_client(settings_primary, pool):
        yield c


@pytest.fixture
async def client_replica(
    settings_replica: Settings, pool: asyncpg.Pool
) -> AsyncIterator[AsyncClient]:
    async for c in _make_client(settings_replica, pool):
        yield c


@pytest.fixture
async def client_cf_required(
    settings_with_cf_required: Settings, pool: asyncpg.Pool
) -> AsyncIterator[AsyncClient]:
    async for c in _make_client(settings_with_cf_required, pool):
        yield c
