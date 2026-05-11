"""POST /upload_json — accept an HRF JSON submission from the frontend.

Trust boundary for the entire HRFunc ecosystem. Three independent auth layers
have to pass:

1. Cloudflare Access service token  (validated at the edge — we only verify
   that the JWT/email headers it injects are present, as defense in depth).
2. App-level `x-api-key` argon2 match against the api_keys table.
3. Body validation: size, JSON shape, `_hrf_submission` envelope.

See BOOTSTRAP.md "Ingest endpoint behavior" for the canonical step list.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Header, Request, UploadFile
from fastapi.responses import PlainTextResponse

from hrserv.auth import authenticate
from hrserv.config import NodeRole
from hrserv.db import SubmissionInsert, insert_submission
from hrserv.models import HRFSubmissionMeta, IngestResponse

logger = logging.getLogger("hrserv.ingest")

router = APIRouter(tags=["ingest"])

INGEST_SCOPE = "ingest"

# Header aliases follow the wire-protocol casing exactly. FastAPI is case-
# insensitive on header matching, but the alias makes the contract visible.
JsonFileForm = Annotated[UploadFile, File(description="Augmented HRF JSON upload")]
ApiKeyHeader = Annotated[str | None, Header(alias="x-api-key")]
CFAccessEmailHeader = Annotated[str | None, Header(alias="Cf-Access-Authenticated-User-Email")]
CFAccessJwtHeader = Annotated[str | None, Header(alias="Cf-Access-Jwt-Assertion")]
CFConnectingIPHeader = Annotated[str | None, Header(alias="CF-Connecting-IP")]
ContentLengthHeader = Annotated[int | None, Header(alias="Content-Length")]


def _client_error(status_code: int, message: str) -> PlainTextResponse:
    """Return a plain-text error safe to surface via the frontend's flash()."""
    return PlainTextResponse(content=message, status_code=status_code)


@router.post("/upload_json", response_model=None)
async def upload_json(
    request: Request,
    jsonFile: JsonFileForm,
    x_api_key: ApiKeyHeader = None,
    cf_access_email: CFAccessEmailHeader = None,
    cf_access_jwt: CFAccessJwtHeader = None,
    cf_connecting_ip: CFConnectingIPHeader = None,
    content_length: ContentLengthHeader = None,
) -> PlainTextResponse | IngestResponse:
    settings = request.app.state.settings
    pool = request.app.state.pool

    # Step 1: replicas refuse writes loudly so misrouted traffic surfaces fast.
    if settings.node_role != NodeRole.PRIMARY:
        return _client_error(503, "Node is not the write primary")

    # Defense in depth: Cloudflare Access should have injected its identity
    # headers. If they're missing, either the policy is misconfigured or the
    # request bypassed Access entirely. Allow disabling for local dev only.
    if settings.require_cf_access_headers and not (cf_access_email or cf_access_jwt):
        logger.warning(
            "Rejecting upload: missing Cloudflare Access headers (CF-Connecting-IP=%s)",
            cf_connecting_ip,
        )
        return _client_error(401, "Cloudflare Access headers missing")

    # Step 2: app-level API key.
    if not x_api_key:
        return _client_error(401, "Missing x-api-key header")
    matched = await authenticate(pool, x_api_key, required_scope=INGEST_SCOPE)
    if matched is None:
        return _client_error(401, "Invalid API key")

    # Step 3: pre-read size guard. The framework also enforces this on the
    # actual read, but rejecting based on Content-Length avoids buffering huge
    # bodies up to the limit before saying no.
    max_bytes = settings.max_upload_bytes
    if content_length is not None and content_length > max_bytes:
        return _client_error(413, f"Payload too large (limit {max_bytes} bytes)")

    raw = await jsonFile.read()
    if len(raw) > max_bytes:
        return _client_error(413, f"Payload too large (limit {max_bytes} bytes)")

    # Step 4: JSON parse.
    try:
        payload_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _client_error(400, "Body is not valid UTF-8")

    try:
        content: Any = json.loads(payload_text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as e:
        return _client_error(400, f"JSON parse error: {e.msg}")
    except ValueError as e:
        # Raised by _reject_json_constant on NaN/Infinity/-Infinity. Postgres's
        # JSONB type rejects these tokens too, but rejecting at the parse step
        # gives the frontend a clean 400 instead of an opaque 500.
        return _client_error(400, f"JSON parse error: {e}")

    # Step 5: structural shape.
    if not isinstance(content, (dict, list)):
        return _client_error(400, "JSON root must be an object or array")

    # Step 6: extract _hrf_submission envelope from top-level if dict.
    meta = _extract_meta(content)

    # We need a stored_filename for the unique constraint. Prefer the one the
    # frontend supplied in `_hrf_submission`; fall back to the multipart filename.
    stored_filename = meta.stored_filename or jsonFile.filename
    if not stored_filename:
        return _client_error(400, "No stored_filename in _hrf_submission or multipart")

    # Step 7: hash + client IP.
    content_sha256 = hashlib.sha256(raw).hexdigest()
    client_ip = _validate_client_ip(
        cf_connecting_ip or (request.client.host if request.client else None)
    )

    # Step 8: insert (idempotent on stored_filename).
    submission = SubmissionInsert(
        stored_filename=stored_filename,
        original_filename=meta.original_filename or jsonFile.filename,
        submitter_email=meta.email,
        study=meta.study,
        doi=meta.doi,
        api_key_id=matched.id,
        client_ip=client_ip,
        size_bytes=len(raw),
        content_sha256=content_sha256,
        content=content,
    )
    submission_id = await insert_submission(pool, submission)

    logger.info(
        "Ingested submission id=%d stored_filename=%s api_key=%s size=%d",
        submission_id,
        stored_filename,
        matched.id,
        len(raw),
    )

    # Step 9: response.
    return IngestResponse(id=submission_id, stored_filename=stored_filename)


def _reject_json_constant(token: str) -> Any:
    """Used as `parse_constant` for json.loads — rejects NaN/Infinity literals.

    The bare `json.loads` accepts the non-standard tokens `NaN`, `Infinity`,
    `-Infinity` by default. Postgres JSONB does not. Rejecting at parse-time
    keeps the failure mode visible at the application boundary (clean 400)
    rather than letting it surface as a DB-layer 500 mid-insert.
    """
    raise ValueError(f"non-standard JSON token {token!r} is not allowed")


def _validate_client_ip(value: str | None) -> str | None:
    """Return value only if it parses as an IPv4/IPv6 address; else None.

    Postgres's INET type rejects anything non-parseable with a server-side error.
    If we passed a garbled CF-Connecting-IP straight through, the insert would
    raise asyncpg.InvalidTextRepresentationError mid-request and FastAPI's
    default 500 JSON would leak through to the frontend's flash(). Dropping the
    value silently is the safest behavior — `client_ip` is best-effort audit
    data, not a security control.
    """
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        logger.warning("Dropping malformed client IP %r", value)
        return None
    return value


def _extract_meta(content: Any) -> HRFSubmissionMeta:
    """Pull the `_hrf_submission` envelope from a top-level dict, if present.

    Missing envelope is warn-logged but not an error — the frontend is the only
    expected caller and the rest of the payload is still preserved in `content`.
    Returns an empty meta on missing or non-dict roots so downstream code can
    treat the result uniformly.
    """
    if not isinstance(content, dict):
        return HRFSubmissionMeta()

    envelope = content.get("_hrf_submission")
    if envelope is None:
        logger.warning("Submission missing _hrf_submission envelope")
        return HRFSubmissionMeta()

    if not isinstance(envelope, dict):
        logger.warning(
            "Submission _hrf_submission has unexpected type %s; ignoring", type(envelope).__name__
        )
        return HRFSubmissionMeta()

    return HRFSubmissionMeta.model_validate(envelope)
