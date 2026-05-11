"""FastAPI application factory and lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse

from hrserv import __version__
from hrserv.config import Settings, load_settings
from hrserv.db import close_pool, create_pool
from hrserv.routes import health, ingest

logger = logging.getLogger("hrserv.main")


async def _validation_error_to_plaintext(_request: Request, exc: Exception) -> PlainTextResponse:
    """Turn FastAPI's default 422 JSON validation error into a plain-text 400.

    The Flask frontend surfaces failures via `flash(f"Upload failed: {resp.text}")`,
    so the body needs to be human-readable text, not Pydantic's structured JSON.
    400 is more semantically accurate than 422 for our use case anyway — the
    contract here is "send a multipart with a JSON file", not "POST JSON".
    """
    if isinstance(exc, RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid request"}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"Malformed request: {first.get('msg', 'invalid request')}"
        if loc:
            message += f" (at {loc})"
    else:
        message = "Malformed request"
    return PlainTextResponse(content=message, status_code=400)


async def _unhandled_exception_to_plaintext(_request: Request, exc: Exception) -> PlainTextResponse:
    """Catch-all for any error that escapes the route handlers.

    Without this, FastAPI's default returns `{"detail": "Internal Server Error"}`
    as JSON, which would surface as raw JSON in the frontend's flash() banner.
    The full traceback is logged server-side; the user-facing body stays opaque.
    """
    logger.exception("Unhandled exception during request", exc_info=exc)
    return PlainTextResponse(content="Internal server error", status_code=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app. Settings can be injected for tests."""
    if settings is None:
        settings = load_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await create_pool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
        app.state.pool = pool
        app.state.settings = settings
        logger.info(
            "HRServ %s started; node_role=%s db_pool=%d-%d",
            __version__,
            settings.node_role,
            settings.db_pool_min_size,
            settings.db_pool_max_size,
        )
        try:
            yield
        finally:
            await close_pool(pool)
            logger.info("HRServ stopped")

    app = FastAPI(
        title="HRServ",
        version=__version__,
        description="Receiver service for HRF JSON uploads.",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(ingest.router)

    app.add_exception_handler(RequestValidationError, _validation_error_to_plaintext)
    app.add_exception_handler(Exception, _unhandled_exception_to_plaintext)

    return app


# Uvicorn loads this via `--factory`: `uvicorn hrserv.main:create_app --factory`.
# We do NOT instantiate a module-level `app` here, because doing so would call
# `load_settings()` at import time and crash on missing env in tooling that
# merely needs to read this module (tests, mypy, IDE introspection).
