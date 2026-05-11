"""Direct exercise of db.py helpers without the HTTP layer."""

from __future__ import annotations

import asyncpg
import pytest

from hrserv.db import (
    SubmissionInsert,
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
