"""Health check endpoint.

The frontend polls /healthz to auto-toggle its maintenance banner instead of
needing a code deploy, so this endpoint must stay cheap and dependency-free
beyond a single DB ping.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hrserv.db import ping
from hrserv.models import HealthResponse

router = APIRouter(tags=["health"])


# Both GET and HEAD are accepted per RFC 7231 §4.3.2 — HEAD should be valid
# wherever GET is. Monitoring tools (UptimeRobot, BetterStack, Pingdom) default
# to HEAD for efficiency; without this, those probes get 405 even though the
# endpoint is healthy. Starlette automatically strips the response body for
# HEAD requests, so the handler implementation stays the same.
@router.api_route(
    "/healthz",
    methods=["GET", "HEAD"],
    response_model=HealthResponse,
)
async def healthz(request: Request) -> JSONResponse:
    """Return 200 when DB reachable, 503 otherwise.

    Returns the response directly (not via FastAPI response_model coercion) so
    we can send 503 with the same body shape on failure.
    """
    pool = request.app.state.pool
    settings = request.app.state.settings

    db_ok = await ping(pool)
    body = HealthResponse(
        status="ok" if db_ok else "degraded",
        db=db_ok,
        node_role=str(settings.node_role),
    ).model_dump()
    return JSONResponse(status_code=200 if db_ok else 503, content=body)
