"""Pydantic models for request parsing and response shaping."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HRFSubmissionMeta(BaseModel):
    """The `_hrf_submission` envelope the frontend prepends to uploads.

    All fields are optional at the schema level — we warn-log on missing pieces
    rather than reject, because the trusted frontend is the only caller and
    schema drift there shouldn't lose data. Hot fields (`stored_filename`,
    `email`, `study`, `doi`) are extracted into dedicated columns; the full
    envelope is preserved inside `content`.
    """

    model_config = {"extra": "allow"}

    email: str | None = None
    study: str | None = None
    doi: str | None = None
    original_filename: str | None = None
    stored_filename: str | None = None


class IngestResponse(BaseModel):
    """Success response from POST /upload_json.

    The frontend only inspects the HTTP status code, but logs the body during
    shadow-mode validation. Keep this shape stable.
    """

    ok: bool = Field(default=True)
    id: int
    stored_filename: str


class HealthResponse(BaseModel):
    """Response shape for GET /healthz."""

    status: str
    db: bool
    node_role: str
