"""API key authentication: argon2 verify, scope check, revocation."""

from __future__ import annotations

import asyncpg
import pytest

from hrserv.auth import (
    AuthenticatedKey,
    authenticate,
    generate_secret,
    hash_secret,
)
from hrserv.db import insert_api_key

pytestmark = pytest.mark.integration


async def test_valid_key_authenticates(pool: asyncpg.Pool) -> None:
    secret = generate_secret()
    await insert_api_key(pool, label="kA", key_hash=hash_secret(secret), scopes=["ingest"])

    matched = await authenticate(pool, secret, required_scope="ingest")
    assert matched == AuthenticatedKey(id="kA", scopes=("ingest",))


async def test_wrong_secret_returns_none(pool: asyncpg.Pool) -> None:
    await insert_api_key(pool, label="kA", key_hash=hash_secret(generate_secret()))
    matched = await authenticate(pool, "totally-wrong-secret", required_scope="ingest")
    assert matched is None


async def test_empty_secret_returns_none(pool: asyncpg.Pool) -> None:
    await insert_api_key(pool, label="kA", key_hash=hash_secret(generate_secret()))
    assert await authenticate(pool, "", required_scope="ingest") is None


async def test_revoked_key_not_returned(pool: asyncpg.Pool) -> None:
    secret = generate_secret()
    await insert_api_key(pool, label="kA", key_hash=hash_secret(secret))
    await pool.execute("UPDATE api_keys SET revoked_at = now() WHERE id = 'kA'")

    matched = await authenticate(pool, secret, required_scope="ingest")
    assert matched is None


async def test_missing_scope_rejected(pool: asyncpg.Pool) -> None:
    secret = generate_secret()
    await insert_api_key(pool, label="kA", key_hash=hash_secret(secret), scopes=["readonly"])
    matched = await authenticate(pool, secret, required_scope="ingest")
    assert matched is None


async def test_correct_key_picked_from_multiple_active(pool: asyncpg.Pool) -> None:
    """With several active keys, authenticate returns the one whose hash matches."""
    s1 = generate_secret()
    s2 = generate_secret()
    s3 = generate_secret()
    await insert_api_key(pool, label="k1", key_hash=hash_secret(s1))
    await insert_api_key(pool, label="k2", key_hash=hash_secret(s2))
    await insert_api_key(pool, label="k3", key_hash=hash_secret(s3))

    matched = await authenticate(pool, s2, required_scope="ingest")
    assert matched is not None
    assert matched.id == "k2"


async def test_hash_secret_produces_argon2_format() -> None:
    h = hash_secret("hello")
    assert h.startswith("$argon2")
