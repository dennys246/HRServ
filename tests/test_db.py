"""Direct exercise of db.py helpers without the HTTP layer."""

from __future__ import annotations

from unittest.mock import patch

import asyncpg
import pytest

from hrserv.db import (
    SubmissionInsert,
    create_pool_with_retry,
    insert_api_key,
    insert_submission,
    list_active_api_keys,
    ping,
)

pytestmark = pytest.mark.integration


async def test_ping_true_on_healthy_pool(pool: asyncpg.Pool) -> None:
    assert await ping(pool) is True


async def test_ping_false_on_closed_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    assert await ping(pool) is False


async def test_list_active_api_keys_excludes_revoked(pool: asyncpg.Pool) -> None:
    await insert_api_key(pool, label="active", key_hash="hash1")
    await insert_api_key(pool, label="dead", key_hash="hash2")
    await pool.execute("UPDATE api_keys SET revoked_at = now() WHERE id = 'dead'")

    keys = await list_active_api_keys(pool)
    ids = {k.id for k in keys}
    assert ids == {"active"}


async def test_insert_submission_idempotent_on_stored_filename(pool: asyncpg.Pool) -> None:
    await insert_api_key(pool, label="kA", key_hash="x")

    def make(content: dict[str, str]) -> SubmissionInsert:
        return SubmissionInsert(
            stored_filename="same.json",
            original_filename="orig.json",
            submitter_email="a@b.com",
            study=None,
            doi=None,
            api_key_id="kA",
            client_ip="10.0.0.1",
            size_bytes=10,
            content_sha256="0" * 64,
            content=content,
        )

    first_id = await insert_submission(pool, make({"v": "1"}))
    second_id = await insert_submission(pool, make({"v": "2"}))

    assert first_id == second_id
    # First write wins — second insert is a no-op against the unique constraint.
    row = await pool.fetchrow("SELECT content FROM hrf_submissions WHERE id = $1", first_id)
    assert row is not None
    # asyncpg's JSONB codec decodes the value into a Python dict for us.
    assert row["content"] == {"v": "1"}


async def test_insert_submission_persists_array_content(pool: asyncpg.Pool) -> None:
    await insert_api_key(pool, label="kA", key_hash="x")
    sub = SubmissionInsert(
        stored_filename="arr.json",
        original_filename=None,
        submitter_email=None,
        study=None,
        doi=None,
        api_key_id="kA",
        client_ip=None,
        size_bytes=2,
        content_sha256="0" * 64,
        content=[1, 2, 3],
    )
    sid = await insert_submission(pool, sub)
    row = await pool.fetchrow("SELECT content FROM hrf_submissions WHERE id = $1", sid)
    assert row is not None
    assert row["content"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# create_pool_with_retry — the lifespan resilience contract
# ---------------------------------------------------------------------------


async def test_create_pool_with_retry_succeeds_on_first_attempt(database_url: str) -> None:
    """Happy path: Postgres is reachable, no retries needed, pool works."""
    pool = await create_pool_with_retry(database_url, retry_delays=(0,))
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await pool.close()


async def test_create_pool_with_retry_retries_on_transient_failure(database_url: str) -> None:
    """If the first attempt fails with a transient error, we retry and succeed.

    This is the boot-time scenario: bridge briefly flapping, Postgres briefly
    unreachable. Without this retry behavior, the FastAPI lifespan would
    crash on the first OSError and the container would go unhealthy until
    manual recovery. Mocked via a side_effect that fails the first two calls
    then delegates to the real implementation.
    """
    real_create_pool = __import__("hrserv.db", fromlist=["create_pool"]).create_pool
    call_count = {"n": 0}

    async def flaky_create_pool(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise OSError("simulated bridge-not-ready")
        return await real_create_pool(*args, **kwargs)

    with patch("hrserv.db.create_pool", side_effect=flaky_create_pool):
        # Short delays so the test doesn't actually wait 6+ seconds.
        pool = await create_pool_with_retry(database_url, retry_delays=(0, 0, 0, 0))
        try:
            assert call_count["n"] == 3  # failed twice, succeeded on third attempt
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT 1") == 1
        finally:
            await pool.close()


async def test_create_pool_with_retry_raises_after_exhausting_attempts(
    database_url: str,
) -> None:
    """If every attempt fails, the last exception bubbles up.

    Wrong-password / wrong-host configurations should NOT silently retry
    forever — they should fail loudly so the operator sees the misconfig.
    """

    async def always_fails(*args, **kwargs):
        raise OSError("simulated persistent failure")

    with (
        patch("hrserv.db.create_pool", side_effect=always_fails),
        pytest.raises(OSError, match="simulated persistent failure"),
    ):
        await create_pool_with_retry(database_url, retry_delays=(0, 0, 0))
