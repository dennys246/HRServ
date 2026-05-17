"""CORS behavior for /healthz polling from the frontend.

The frontend on hrfunc.org polls /healthz to render a status pill (green when
up, red when degraded). For that to work in a browser, HRServ must return
`Access-Control-Allow-Origin` for permitted origins on safe-method requests,
and must NOT widen CORS to the POST /upload_json surface.
"""

from __future__ import annotations

import asyncpg
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_healthz_get_includes_acao_for_allowed_origin(
    client_primary: AsyncClient,
) -> None:
    r = await client_primary.get("/healthz", headers={"Origin": "https://hrfunc.org"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://hrfunc.org"


async def test_healthz_get_includes_acao_for_www_origin(
    client_primary: AsyncClient,
) -> None:
    r = await client_primary.get("/healthz", headers={"Origin": "https://www.hrfunc.org"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://www.hrfunc.org"


async def test_healthz_get_omits_acao_for_disallowed_origin(
    client_primary: AsyncClient,
) -> None:
    """A disallowed origin still gets a 200 body (CORS is a browser-side gate),
    but without the ACAO header the browser will refuse to expose it to JS.
    """
    r = await client_primary.get("/healthz", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


async def test_healthz_503_still_includes_acao(
    client_primary: AsyncClient, pool: asyncpg.Pool
) -> None:
    """The status pill renders "degraded" off a 503 body, so the browser must
    still be allowed to read the response when the DB is down. Without this,
    the pill would go invisible (CORS error in the browser console) instead
    of red on a real outage — the exact failure mode it's meant to surface.
    """
    await pool.close()
    r = await client_primary.get("/healthz", headers={"Origin": "https://hrfunc.org"})
    assert r.status_code == 503
    assert r.headers.get("access-control-allow-origin") == "https://hrfunc.org"


async def test_healthz_head_includes_acao_for_allowed_origin(
    client_primary: AsyncClient,
) -> None:
    r = await client_primary.head("/healthz", headers={"Origin": "https://hrfunc.org"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://hrfunc.org"


async def test_upload_json_preflight_for_post_is_refused(
    client_primary: AsyncClient,
) -> None:
    """A preflight for POST /upload_json must not be granted — CORS is scoped to
    GET/HEAD so the browser can never be coerced into submitting an upload on a
    user's behalf from an attacker-controlled origin.

    Starlette's CORSMiddleware signals refusal on a per-failure basis: it returns
    400 with a "Disallowed CORS method" body and DOES still echo ACAO when the
    origin itself is allowed. What actually blocks the browser from proceeding
    is the absence of POST from Access-Control-Allow-Methods.
    """
    r = await client_primary.options(
        "/upload_json",
        headers={
            "Origin": "https://hrfunc.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key, content-type",
        },
    )
    assert r.status_code == 400
    allowed_methods = r.headers.get("access-control-allow-methods", "")
    assert "POST" not in allowed_methods
    assert "GET" in allowed_methods


async def test_healthz_preflight_for_get_is_granted(
    client_primary: AsyncClient,
) -> None:
    """A preflight for GET /healthz from an allowed origin must succeed so the
    browser proceeds with the actual fetch. (A no-custom-header GET is technically
    a 'simple request' that skips preflight, but a defensive client may still
    preflight — make sure it works.)
    """
    r = await client_primary.options(
        "/healthz",
        headers={
            "Origin": "https://hrfunc.org",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://hrfunc.org"
    allowed_methods = r.headers.get("access-control-allow-methods", "")
    assert "GET" in allowed_methods
    assert "POST" not in allowed_methods
