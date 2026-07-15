#!/bin/bash
# hrserv-up.sh — wait for dockerd (inside the Colima VM), then bring the
# HRServ stack up clean. macOS equivalent of deploy/hrserv.service.
#
# Same boot semantics as the Linux unit: every boot is a manual
# `dc down && dc up -d`, so compose's depends_on/service_healthy ordering is
# honored (it is IGNORED by `restart: unless-stopped` recovery — the exact
# race documented in deploy/hrserv.service).
#
# launchd has no cross-daemon ordering, so this script self-orders by
# polling `docker info` until the Colima VM has booted dockerd. Colima VM
# boot is 30–90s on a Mac Mini; the 300s bound mirrors the Linux unit's
# TimeoutStartSec=300 — bounded, visible failure over infinite hang.
#
# ROLE SELECTION: COMPOSE_ROLE_FILE below is this host's role, mirroring how
# the Linux unit hardcodes docker-compose.primary.yml. After a failover
# promotion, update it here (see docs/FAILOVER.md step 5 and FOLLOWUPS.md
# "Compose-file role coupling on failover"). The plist invokes this script
# from the repo working tree, so the edit takes effect on the next boot —
# no reinstall needed.
#
# Invoked by com.hrfunc.hrserv.plist; not intended for interactive use.
set -euo pipefail

DOCKER_BIN="${DOCKER_BIN:-/opt/homebrew/bin/docker}"
HRSERV_DIR="${HRSERV_DIR:-/opt/hrserv}"
DOCKER_WAIT_SECS="${DOCKER_WAIT_SECS:-300}"
COMPOSE_ROLE_FILE="${COMPOSE_ROLE_FILE:-docker-compose.replica.yml}"

# Colima's docker socket lives under the operator's HOME (set by the plist).
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/default/docker.sock}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') hrserv-up: $*"; }

compose() {
    "$DOCKER_BIN" compose \
        -f "$HRSERV_DIR/docker/$COMPOSE_ROLE_FILE" \
        -f "$HRSERV_DIR/docker/docker-compose.macos.yml" \
        "$@"
}

log "waiting up to ${DOCKER_WAIT_SECS}s for dockerd at $DOCKER_HOST (role file: $COMPOSE_ROLE_FILE)"
deadline=$((SECONDS + DOCKER_WAIT_SECS))
until "$DOCKER_BIN" info >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        log "ERROR: dockerd not reachable after ${DOCKER_WAIT_SECS}s — is com.hrfunc.colima healthy? (launchctl print system/com.hrfunc.colima)"
        exit 1
    fi
    sleep 5
done

# Clean down BEFORE up — belt-and-suspenders against `restart:
# unless-stopped` having raced containers up over a half-ready VM. No -v:
# the named Postgres volume must survive.
log "dockerd ready; compose down (clean slate)"
compose down --remove-orphans
log "compose up -d"
compose up -d
log "stack is up:"
compose ps
