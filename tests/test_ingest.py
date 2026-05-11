"""POST /upload_json — the trust boundary. Cover every contract clause."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _multipart(
    body: bytes, filename: str = "study_HRFs_2026-05-11_10-00-00_a1b2c3d4.json"
) -> dict[str, Any]:
    return {"jsonFile": (filename, body, "application/json")}


async def test_happy_path(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
    pool: asyncpg.Pool,
) -> None:
    label, secret = seeded_key
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret, "CF-Connecting-IP": "203.0.113.5"},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["stored_filename"].endswith(".json")

    row = await pool.fetchrow("SELECT * FROM hrf_submissions WHERE id = $1", body["id"])
    assert row is not None
    assert row["submitter_email"] == "researcher@example.edu"
    assert row["study"] == "flanker-pilot"
    assert row["doi"] == "10.1000/abcd.1234"
    assert row["api_key_id"] == label
    assert str(row["client_ip"]) == "203.0.113.5"
    assert row["size_bytes"] == len(sample_payload_bytes)
    assert len(row["content_sha256"]) == 64
    # The asyncpg JSONB codec decodes content for us — it's already a dict.
    assert row["content"]["_hrf_submission"]["study"] == "flanker-pilot"


async def test_missing_api_key_returns_401(
    client_primary: AsyncClient, sample_payload_bytes: bytes
) -> None:
    r = await client_primary.post("/upload_json", files=_multipart(sample_payload_bytes))
    assert r.status_code == 401
    assert "x-api-key" in r.text.lower()


async def test_invalid_api_key_returns_401(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": "not-a-real-secret"},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 401
    assert "invalid" in r.text.lower()


async def test_replica_role_returns_503(
    client_replica: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    _, secret = seeded_key
    r = await client_replica.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 503
    assert "primary" in r.text.lower()


async def test_missing_cf_access_headers_returns_401_when_required(
    client_cf_required: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    _, secret = seeded_key
    r = await client_cf_required.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 401
    assert "cloudflare" in r.text.lower() or "access" in r.text.lower()


async def test_cf_access_headers_present_passes_defense_in_depth(
    client_cf_required: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    _, secret = seeded_key
    r = await client_cf_required.post(
        "/upload_json",
        headers={
            "x-api-key": secret,
            "Cf-Access-Authenticated-User-Email": "frontend@example.com",
            "Cf-Access-Jwt-Assertion": "fake.jwt.here",
        },
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 200, r.text


async def test_oversize_content_length_returns_413(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    _, secret = seeded_key
    # Spoof an oversized Content-Length header; the framework's own multipart
    # parsing will count the real bytes too, but the early Content-Length check
    # should fire first and short-circuit.
    huge = 10 * 1024 * 1024
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret, "Content-Length": str(huge)},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 413


async def test_oversize_body_returns_413(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
) -> None:
    _, secret = seeded_key
    # Synthesize a payload larger than the 5 MiB default limit. Use a dict with
    # a single huge string value so it's valid JSON.
    big = json.dumps({"_hrf_submission": {}, "blob": "x" * (6 * 1024 * 1024)}).encode()
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(big),
    )
    assert r.status_code == 413


async def test_invalid_json_returns_400(
    client_primary: AsyncClient, seeded_key: tuple[str, str]
) -> None:
    _, secret = seeded_key
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(b"{this is not json"),
    )
    assert r.status_code == 400
    assert "json" in r.text.lower()


async def test_scalar_json_root_returns_400(
    client_primary: AsyncClient, seeded_key: tuple[str, str]
) -> None:
    _, secret = seeded_key
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(b'"a bare string is not an HRF"'),
    )
    assert r.status_code == 400
    assert "object" in r.text.lower() or "array" in r.text.lower()


async def test_array_root_accepted(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    pool: asyncpg.Pool,
) -> None:
    _, secret = seeded_key
    body = json.dumps([{"some": "hrf-like content"}]).encode()
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(body, filename="array_root.json"),
    )
    assert r.status_code == 200
    row = await pool.fetchrow(
        "SELECT submitter_email, study, doi FROM hrf_submissions WHERE id = $1",
        r.json()["id"],
    )
    assert row is not None
    # Array root: no _hrf_submission envelope is possible, so hot fields stay NULL.
    assert row["submitter_email"] is None
    assert row["study"] is None


async def test_idempotent_retry_returns_same_id(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
) -> None:
    _, secret = seeded_key

    def post() -> Any:
        return client_primary.post(
            "/upload_json",
            headers={"x-api-key": secret},
            files=_multipart(sample_payload_bytes),
        )

    first = await post()
    second = await post()
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["stored_filename"] == second.json()["stored_filename"]


async def test_stored_filename_falls_back_to_multipart_filename(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    pool: asyncpg.Pool,
) -> None:
    """If _hrf_submission lacks stored_filename, fall back to the upload's filename."""
    _, secret = seeded_key
    payload = {"_hrf_submission": {"email": "x@y"}, "data": [1, 2, 3]}
    body = json.dumps(payload).encode()
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(body, filename="fallback-name.json"),
    )
    assert r.status_code == 200
    assert r.json()["stored_filename"] == "fallback-name.json"


async def test_malformed_multipart_returns_plain_text_400(
    client_primary: AsyncClient, seeded_key: tuple[str, str]
) -> None:
    """A multipart upload without a real file is rejected by FastAPI validation;
    the validation-error exception handler reshapes that to a plain-text 400 so
    the frontend's flash() shows something human-readable instead of JSON.
    """
    _, secret = seeded_key
    body = json.dumps({"just": "an object"}).encode()
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files={"jsonFile": ("", body, "application/json")},
    )
    assert r.status_code == 400
    # Plain text, not JSON — flash()-friendly.
    assert r.headers["content-type"].startswith("text/plain")
    assert "{" not in r.text  # not a JSON body


async def test_malformed_cf_connecting_ip_is_dropped_not_error(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload_bytes: bytes,
    pool: asyncpg.Pool,
) -> None:
    """A garbled CF-Connecting-IP gets dropped, not propagated to Postgres.

    Without the boundary validation in ingest._validate_client_ip, the INET cast
    would raise asyncpg.InvalidTextRepresentationError mid-insert and return a
    500 with the raw error leaking — violating the safe-to-flash contract.
    """
    _, secret = seeded_key
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret, "CF-Connecting-IP": "not-an-ip-address!!!"},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 200, r.text
    row = await pool.fetchrow("SELECT client_ip FROM hrf_submissions WHERE id = $1", r.json()["id"])
    assert row is not None
    assert row["client_ip"] is None


async def test_api_key_without_ingest_scope_returns_401(
    client_primary: AsyncClient,
    pool: asyncpg.Pool,
    sample_payload_bytes: bytes,
) -> None:
    """An otherwise-valid key whose scopes don't include 'ingest' must be rejected."""
    from hrserv.auth import generate_secret, hash_secret
    from hrserv.db import insert_api_key

    secret = generate_secret()
    await insert_api_key(
        pool, label="readonly-key", key_hash=hash_secret(secret), scopes=["readonly"]
    )

    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/plain")


async def test_nan_or_infinity_in_payload_returns_400(
    client_primary: AsyncClient, seeded_key: tuple[str, str]
) -> None:
    """JSON's NaN/Infinity extensions aren't valid JSON and Postgres JSONB rejects them.

    Reject at the parse step (via parse_constant) rather than letting the DB
    raise a 500 mid-insert.
    """
    _, secret = seeded_key
    body = b'{"_hrf_submission": {"stored_filename": "x.json"}, "value": NaN}'
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(body),
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")
    assert "nan" in r.text.lower()


async def test_infinity_in_payload_returns_400(
    client_primary: AsyncClient, seeded_key: tuple[str, str]
) -> None:
    _, secret = seeded_key
    body = b'{"_hrf_submission": {"stored_filename": "x.json"}, "v": Infinity}'
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(body),
    )
    assert r.status_code == 400
    assert "infinity" in r.text.lower()


async def test_content_persisted_verbatim_with_envelope(
    client_primary: AsyncClient,
    seeded_key: tuple[str, str],
    sample_payload: dict[str, Any],
    sample_payload_bytes: bytes,
    pool: asyncpg.Pool,
) -> None:
    """The stored `content` column round-trips the original JSON intact."""
    _, secret = seeded_key
    r = await client_primary.post(
        "/upload_json",
        headers={"x-api-key": secret},
        files=_multipart(sample_payload_bytes),
    )
    assert r.status_code == 200
    row = await pool.fetchrow("SELECT content FROM hrf_submissions WHERE id = $1", r.json()["id"])
    assert row is not None
    assert row["content"] == sample_payload
