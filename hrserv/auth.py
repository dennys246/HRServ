"""API key authentication via argon2-hashed lookup against the api_keys table.

The frontend sends `x-api-key: <plaintext>`. We iterate active keys and verify each
hash; on match we check the requested scope. The O(n) loop is acceptable because
the candidate set is tiny (one row per client app) and argon2's intentional
slowness is what makes brute force impractical anyway.

A note on timing side-channels: we iterate every candidate (not stopping at the
first match) to keep the *iteration count* independent of which slot matched.
We do NOT achieve fully constant-time verification — argon2 verify is much
slower on a match than on a mismatch (mismatch raises after rejecting the
hash structure; match runs the full key derivation). A patient attacker
measuring response time could in principle detect that *some* key matched and
roughly estimate its position. Mitigating that properly would require running
argon2 against a dummy hash for every non-match, which is post-MVP. The current
behavior is documented in docs/FOLLOWUPS.md.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

import asyncpg
from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions

from hrserv.db import APIKeyRecord, list_active_api_keys

logger = logging.getLogger("hrserv.auth")

_hasher = PasswordHasher()

# Each ingest does an argon2 verify per active key (~tens of ms each). The
# population is expected to be a handful; if it grows past this we log a
# warning so operators notice the implicit latency tax before it becomes
# user-visible.
_ACTIVE_KEY_COUNT_WARN_THRESHOLD = 20


def hash_secret(plaintext: str) -> str:
    """Argon2-hash a plaintext secret. Uses library defaults."""
    return _hasher.hash(plaintext)


def generate_secret(nbytes: int = 32) -> str:
    """Mint a fresh plaintext API key. Caller persists only the hash."""
    return secrets.token_urlsafe(nbytes)


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    """The api_keys row whose hash matched the presented plaintext."""

    id: str
    scopes: tuple[str, ...]


async def authenticate(
    pool: asyncpg.Pool,
    presented: str,
    *,
    required_scope: str,
) -> AuthenticatedKey | None:
    """Verify `presented` against all active keys; return the match or None.

    argon2 verify is CPU-bound (~tens to hundreds of ms per call), so it runs in a
    thread executor to keep the event loop responsive. We intentionally check every
    key — not just stopping at the first mismatch — so the wall time is constant for
    a given pool size and doesn't leak which slot matched (mild side-channel hygiene).
    """
    if not presented:
        return None

    candidates = await list_active_api_keys(pool)
    if not candidates:
        return None
    if len(candidates) > _ACTIVE_KEY_COUNT_WARN_THRESHOLD:
        logger.warning(
            "Active api_keys count (%d) exceeds threshold (%d). Each request runs an "
            "argon2 verify per key — consider revoking unused keys to keep auth latency "
            "low.",
            len(candidates),
            _ACTIVE_KEY_COUNT_WARN_THRESHOLD,
        )

    matched: AuthenticatedKey | None = await asyncio.to_thread(
        _verify_against_all, presented, candidates
    )

    if matched is None:
        return None

    if required_scope not in matched.scopes:
        logger.info(
            "API key %s authenticated but lacks required scope %r (has %r)",
            matched.id,
            required_scope,
            matched.scopes,
        )
        return None

    return matched


def _verify_against_all(presented: str, candidates: list[APIKeyRecord]) -> AuthenticatedKey | None:
    """Synchronous inner loop, intended to run in a thread executor."""
    matched: AuthenticatedKey | None = None
    for record in candidates:
        try:
            _hasher.verify(record.key_hash, presented)
        except argon2_exceptions.VerifyMismatchError:
            continue
        except argon2_exceptions.InvalidHashError:
            logger.warning("Skipping malformed argon2 hash for api_keys.id=%s", record.id)
            continue
        # Don't break — see docstring for why we keep iterating.
        if matched is None:
            matched = AuthenticatedKey(id=record.id, scopes=record.scopes)
    return matched
