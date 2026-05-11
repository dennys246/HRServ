"""Settings parsing — env vars, defaults, validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hrserv.config import NodeRole, Settings

# Any env var the developer might have exported in their shell while iterating
# (e.g. README's local-dev snippet exports REQUIRE_CF_ACCESS_HEADERS=false).
# Tests that assert defaults must clear these so they don't read the dev shell.
_OPTIONAL_SETTINGS_ENV = (
    "MAX_UPLOAD_BYTES",
    "REQUIRE_CF_ACCESS_HEADERS",
    "LOG_LEVEL",
    "DB_POOL_MIN_SIZE",
    "DB_POOL_MAX_SIZE",
)


def _scrub_optional_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the optional Settings env vars so we test schema defaults, not shell state."""
    for name in _OPTIONAL_SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)


def test_optional_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_optional_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("NODE_ROLE", "primary")
    s = Settings()  # type: ignore[call-arg]
    assert s.node_role == NodeRole.PRIMARY
    assert s.max_upload_bytes == 5 * 1024 * 1024
    assert s.require_cf_access_headers is True
    assert s.db_pool_min_size == 1
    assert s.db_pool_max_size == 8


def test_node_role_replica_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("NODE_ROLE", "replica")
    s = Settings()  # type: ignore[call-arg]
    assert s.node_role == NodeRole.REPLICA


def test_node_role_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """NODE_ROLE has no default — a node must declare its role explicitly."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("NODE_ROLE", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_invalid_node_role_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("NODE_ROLE", "tertiary")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_database_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NODE_ROLE", "primary")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_max_upload_bytes_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("NODE_ROLE", "primary")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "100")  # below the 1024-byte floor
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
