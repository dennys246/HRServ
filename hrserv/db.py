"""Database access layer: connection pool lifecycle and query helpers.

Every query against Postgres goes through this module so that pool management,
SQL, and type adapters stay in one place. Route handlers never touch asyncpg
directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger("hrserv.db")

# Backoff schedule for create_pool_with_retry. Total wait if every attempt
# fails is ~2 minutes — long enough to cover the typical Docker-bridge /
# network-online race at boot, short enough that genuine misconfigs (wrong
# password, wrong host) still fail fast on operator timescales.
_RETRY_DELAYS_SECONDS: tuple[int, ...] = (1, 2, 3, 5, 8, 13, 21, 30, 30, 30)


@dataclass(frozen=True, slots=True)
class APIKeyRecord:
    """An API key row as returned from the database for auth verification."""

    id: str
    key_hash: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmissionInsert:
    """Inputs required to persist an HRF submission."""

    stored_filename: str
    original_filename: str | None
    submitter_email: str | None
    study: str | None
    doi: str | None
    api_key_id: str
    client_ip: str | None
    size_bytes: int
    content_sha256: str
    content: dict[str, Any] | list[Any]


async def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    command_timeout: float = 30.0,
    max_inactive_connection_lifetime: float = 30.0,
) -> asyncpg.Pool:
    """Open an asyncpg pool. JSONB columns are encoded/decoded as Python objects.

    Per-request work is bounded by `command_timeout` so a hung query can't sit
    on a worker indefinitely (defaults to asyncpg's no-timeout if unset).
    `max_inactive_connection_lifetime` aggressively recycles idle connections
    so brief network blips don't leave stale TCP sockets in the pool past the
    next request (asyncpg's default of 300s is too tolerant for our pattern,
    where bridge-flap-then-recover is the realistic failure mode).
    """

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        max_inactive_connection_lifetime=max_inactive_connection_lifetime,
        init=_init,
    )
    if pool is None:  # pragma: no cover — create_pool returns None only on connect-time failure
        raise RuntimeError("Failed to create Postgres connection pool")
    return pool


async def create_pool_with_retry(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    command_timeout: float = 30.0,
    max_inactive_connection_lifetime: float = 30.0,
    retry_delays: tuple[int, ...] = _RETRY_DELAYS_SECONDS,
) -> asyncpg.Pool:
    """Open a pool, retrying on transient failures with exponential-ish backoff.

    Designed for the boot-time case where the Docker bridge or Postgres may
    not be ready when HRServ's lifespan first runs. Without retry, a single
    `OSError` from asyncpg during the bridge-flapping window would crash the
    FastAPI lifespan and leave the container unhealthy until a manual
    `dc down && dc up -d` (see docs/OPERATIONS.md "Symptom: Cloudflare 502
    after a host reboot").

    Retries on any exception; auth/config errors will still keep failing
    every attempt and surface after ~2 minutes total. That's the correct
    behavior — a wrong password should fail loudly, not silently retry forever.
    """
    last_exc: Exception | None = None
    for attempt, delay in enumerate(retry_delays, start=1):
        try:
            return await create_pool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                command_timeout=command_timeout,
                max_inactive_connection_lifetime=max_inactive_connection_lifetime,
            )
        except Exception as exc:
            # `Exception` only — NOT `BaseException`. That lets
            # `KeyboardInterrupt`, `SystemExit`, and `asyncio.CancelledError`
            # propagate during a shutdown so we don't keep retrying for ~2
            # minutes when uvicorn is trying to cancel the lifespan task.
            last_exc = exc
            if attempt >= len(retry_delays):
                break
            logger.warning(
                "create_pool attempt %d/%d failed (%s); retrying in %ds",
                attempt,
                len(retry_delays),
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
    logger.error(
        "create_pool failed after %d attempts; surfacing last exception",
        len(retry_delays),
        exc_info=last_exc,
    )
    if last_exc is None:  # pragma: no cover — unreachable: every iteration raises or returns
        raise RuntimeError(
            "create_pool_with_retry exhausted retries without recording an exception"
        )
    raise last_exc


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()


async def ping(pool: asyncpg.Pool) -> bool:
    """Return True if the pool can execute a trivial query."""
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
        return bool(value == 1)
    except Exception:
        logger.exception("Database ping failed")
        return False


async def list_active_api_keys(pool: asyncpg.Pool) -> list[APIKeyRecord]:
    """Return all API keys that have not been revoked.

    Auth iterates these and runs argon2 verify against each — see auth.py. The
    candidate set is intentionally small in normal operation (one per client app),
    so the O(n) verify loop is acceptable.
    """
    rows = await pool.fetch(
        """
        SELECT id, key_hash, scopes
        FROM api_keys
        WHERE revoked_at IS NULL
        """
    )
    return [
        APIKeyRecord(
            id=row["id"],
            key_hash=row["key_hash"],
            scopes=tuple(row["scopes"]),
        )
        for row in rows
    ]


async def insert_api_key(
    pool: asyncpg.Pool,
    *,
    label: str,
    key_hash: str,
    scopes: Sequence[str] = ("ingest",),
) -> None:
    """Insert a new API key row. Caller is responsible for hashing the plaintext."""
    await pool.execute(
        """
        INSERT INTO api_keys (id, key_hash, scopes)
        VALUES ($1, $2, $3)
        """,
        label,
        key_hash,
        list(scopes),
    )


async def insert_submission(pool: asyncpg.Pool, sub: SubmissionInsert) -> int:
    """Insert a submission row; return the id.

    Idempotent on stored_filename: if a row already exists, return its id without
    overwriting content. This makes client retries safe.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO hrf_submissions (
                stored_filename,
                original_filename,
                submitter_email,
                study,
                doi,
                api_key_id,
                client_ip,
                size_bytes,
                content_sha256,
                content
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (stored_filename) DO NOTHING
            RETURNING id
            """,
            sub.stored_filename,
            sub.original_filename,
            sub.submitter_email,
            sub.study,
            sub.doi,
            sub.api_key_id,
            sub.client_ip,
            sub.size_bytes,
            sub.content_sha256,
            sub.content,
        )
        if row is not None:
            return int(row["id"])

        existing = await conn.fetchrow(
            "SELECT id FROM hrf_submissions WHERE stored_filename = $1",
            sub.stored_filename,
        )
        if existing is None:  # pragma: no cover — possible only under racey deletes
            raise RuntimeError(
                f"Submission insert returned no row and no existing row for "
                f"stored_filename={sub.stored_filename!r}"
            )
        return int(existing["id"])


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Convenience context manager: yield a connection inside a transaction."""
    async with pool.acquire() as conn, conn.transaction():
        yield conn
