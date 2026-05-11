"""Database access layer: connection pool lifecycle and query helpers.

Every query against Postgres goes through this module so that pool management,
SQL, and type adapters stay in one place. Route handlers never touch asyncpg
directly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger("hrserv.db")


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
) -> asyncpg.Pool:
    """Open an asyncpg pool. JSONB columns are encoded/decoded as Python objects."""

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
        init=_init,
    )
    if pool is None:  # pragma: no cover — create_pool returns None only on connect-time failure
        raise RuntimeError("Failed to create Postgres connection pool")
    return pool


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
