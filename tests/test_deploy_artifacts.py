"""Static validation of the macOS (launchd) boot-chain deploy artifacts.

These artifacts can't be exercised in CI — they need a macOS host with
Colima and a reboot. What CI *can* promise, and what these tests pin down:
the plists parse and reference boot scripts that exist in the repo, the
scripts are syntactically valid bash with the invariants the runbook
documents (bounded waits, correct compose files), and the macOS compose
override actually overrides the tailnet-IP port bind that cannot work under
Colima. The deliberate-reboot drill in deploy/launchd/README.md is the real
integration test.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO_ROOT / "deploy" / "launchd"
MACOS_OVERRIDE = REPO_ROOT / "docker" / "docker-compose.macos.yml"

PLIST_NAMES = ["com.hrfunc.colima.plist", "com.hrfunc.hrserv.plist"]
# Each daemon must run ITS script — a crossed wiring would pass every other
# structural check while booting the wrong thing.
PLIST_SCRIPT = {
    "com.hrfunc.colima.plist": "colima-up.sh",
    "com.hrfunc.hrserv.plist": "hrserv-up.sh",
}
SCRIPT_PATHS = [
    LAUNCHD_DIR / "bin" / "colima-up.sh",
    LAUNCHD_DIR / "bin" / "hrserv-up.sh",
    LAUNCHD_DIR / "install.sh",
]


def _load_plist(name: str) -> dict[str, Any]:
    with (LAUNCHD_DIR / name).open("rb") as f:
        data: dict[str, Any] = plistlib.load(f)
    return data


def test_expected_artifacts_exist() -> None:
    for name in PLIST_NAMES:
        assert (LAUNCHD_DIR / name).is_file(), f"missing {name}"
    for script in SCRIPT_PATHS:
        assert script.is_file(), f"missing {script}"
    assert MACOS_OVERRIDE.is_file()


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_parses_with_daemon_invariants(name: str) -> None:
    plist = _load_plist(name)

    # Label must match the filename — launchctl addresses daemons by label.
    assert plist["Label"] == name.removesuffix(".plist")

    # Boot-time activation is the whole point of the chain.
    assert plist["RunAtLoad"] is True

    # Repo copies keep the placeholder; install.sh renders the real user.
    assert plist["UserName"] == "REPLACE_WITH_OPERATOR_USER"
    env = plist["EnvironmentVariables"]
    assert env["HOME"] == "/Users/REPLACE_WITH_OPERATOR_USER"
    # Colima + docker CLI are Homebrew-installed; PATH must reach them.
    assert "/opt/homebrew/bin" in env["PATH"]

    # Logs must land somewhere an operator will find them (runbook greps
    # these paths).
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].startswith("/opt/hrserv/logs/"), f"{key} outside /opt/hrserv/logs"


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_program_is_a_repo_script(name: str) -> None:
    plist = _load_plist(name)
    program = Path(plist["ProgramArguments"][0])

    # Daemons run scripts from the /opt/hrserv working tree (same convention
    # as the systemd units). Map that onto this repo checkout and require
    # the script to exist and be executable.
    assert program.is_absolute()
    assert str(program).startswith("/opt/hrserv/"), "daemon must run from the /opt/hrserv tree"
    repo_relative = REPO_ROOT / program.relative_to("/opt/hrserv")
    assert repo_relative.is_file(), f"{program} has no counterpart in the repo"
    assert repo_relative.stat().st_mode & 0o111, f"{repo_relative} is not executable"
    assert program.name == PLIST_SCRIPT[name], "plist wired to the wrong boot script"


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_daemons_retry_until_first_success(name: str) -> None:
    plist = _load_plist(name)
    # SuccessfulExit=false on both: relaunch on failure (colima: crashed VM
    # or tailnet-wait timeout; hrserv: dockerd-wait timeout), but leave a
    # clean exit alone — after hrserv's first successful down/up, runtime
    # crash recovery belongs to compose `restart: unless-stopped`, and an
    # operator's deliberate `colima stop` must stay stopped.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    # A failing wait loop must back off, not hot-loop.
    assert plist["ThrottleInterval"] >= 10


def test_colima_daemon_gets_shutdown_grace() -> None:
    plist = _load_plist("com.hrfunc.colima.plist")
    # launchd's default ExitTimeOut is 20s SIGTERM->SIGKILL, which hard-kills
    # the Lima VM (and Postgres inside it) on every reboot. Mirror the Linux
    # unit's TimeoutStopSec=120.
    assert plist["ExitTimeOut"] >= 60


@pytest.mark.parametrize("script", SCRIPT_PATHS, ids=lambda p: p.name)
def test_script_is_valid_bash(script: Path) -> None:
    assert script.read_text().startswith("#!/bin/bash\n")
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


@pytest.mark.parametrize("script", SCRIPT_PATHS, ids=lambda p: p.name)
def test_script_waits_are_bounded(script: Path) -> None:
    """Every wait loop must be bounded — visible failure over infinite hang.

    install.sh has no wait loops, but all three scripts use `set -euo
    pipefail` so a failed step can't be silently skipped.
    """
    text = script.read_text()
    assert "set -euo pipefail" in text
    if script.name != "install.sh":
        # The deadline must actually be COMPARED, not just computed — a
        # deleted comparison with the variable left behind must fail here.
        assert re.search(r"SECONDS\s*>=\s*deadline", text), "wait loop must enforce its deadline"
        assert re.search(r"exit 1", text), "timeout must exit nonzero so launchd sees failure"


def test_install_sed_patterns_match_plist_placeholders() -> None:
    """install.sh's sed must target the exact placeholders the plists carry.

    Renaming a placeholder on either side alone would install unrendered
    plists whose daemons run as a nonexistent user — and every other test
    would stay green.
    """
    text = (LAUNCHD_DIR / "install.sh").read_text()
    assert "s|/Users/REPLACE_WITH_OPERATOR_USER|" in text, "HOME sed pattern drifted"
    assert "s/REPLACE_WITH_OPERATOR_USER/" in text, "username sed pattern drifted"


def test_install_verifies_repo_mounted_in_colima_vm() -> None:
    """Bind mounts resolve inside Colima's VM; a missing source silently
    becomes an empty directory and Postgres crash-loops. install.sh must
    probe visibility through the VM, not just on the Mac."""
    text = (LAUNCHD_DIR / "install.sh").read_text()
    assert "colima ssh" in text, "installer no longer probes the VM for the repo mount"


def test_hrserv_script_uses_role_file_plus_macos_override() -> None:
    text = (LAUNCHD_DIR / "bin" / "hrserv-up.sh").read_text()
    # Same clean-boot semantics as deploy/hrserv.service: down before up,
    # never with -v (the Postgres volume must survive).
    assert "down --remove-orphans" in text
    assert "up -d" in text
    assert "down -v" not in text
    # Both compose files must be passed; the override is what makes the
    # port bind work under Colima at all.
    assert "docker-compose.macos.yml" in text
    assert "COMPOSE_ROLE_FILE:-docker-compose.replica.yml" in text


def test_both_role_files_feed_initdb_all_role_passwords() -> None:
    """01-create-roles.sh hard-requires HRSERV_DB_PASSWORD and
    REPLICATOR_PASSWORD. A role compose file that omits one from the
    postgres environment aborts first boot mid-init on a fresh volume,
    and the restart policy then boots a half-initialized cluster (healthy
    postgres, no app role/schema)."""
    for role_file in ("docker-compose.primary.yml", "docker-compose.replica.yml"):
        text = (REPO_ROOT / "docker" / role_file).read_text()
        postgres_env = text.split("environment:")[1]
        for var in ("HRSERV_DB_PASSWORD", "REPLICATOR_PASSWORD"):
            assert f"{var}: ${{{var}" in postgres_env, f"{role_file} postgres env lacks {var}"
        # The whole init pipeline, not just its env: without POSTGRES_DB and
        # these mounts the entrypoint logs "ignoring
        # /docker-entrypoint-initdb.d/*" and boots a roleless, schemaless
        # cluster.
        assert "POSTGRES_DB: hrserv" in postgres_env, f"{role_file} lacks POSTGRES_DB"
        assert "./postgres/initdb:/docker-entrypoint-initdb.d:ro" in text, (
            f"{role_file} lacks the initdb mount"
        )
        assert "../migrations:/migrations:ro" in text, f"{role_file} lacks the migrations mount"


def test_macos_override_replaces_tailnet_port_bind() -> None:
    text = MACOS_OVERRIDE.read_text()
    # `!override` is load-bearing: without it compose MERGES the ports lists
    # and the broken ${TAILSCALE_IP} bind comes back.
    assert "ports: !override" in text
    # Host port 15432: the Colima VM is shared with other projects' stacks
    # that also want loopback 5432 at boot (see the override's header).
    assert '"127.0.0.1:15432:5432"' in text
    # The tailnet IP must not appear in effective YAML (comments explaining
    # why it can't be used are fine).
    effective = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "${TAILSCALE_IP}" not in effective
