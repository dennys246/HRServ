#!/bin/bash
# colima-up.sh — wait for the tailnet IP, then run Colima in the foreground.
#
# macOS equivalent of the `ExecStartPre=tailscale wait` layer in
# deploy/docker.service.d/wait-for-tailscale.conf. Instead of `tailscale wait`
# we poll for the SPECIFIC tailnet IP this node expects (TAILSCALE_IP from
# docker/.env, when present) — a stronger guarantee than "some IP is
# assigned", and it doesn't depend on the `wait` subcommand existing in the
# Homebrew tailscale build.
#
# Why wait at all, given docker-compose.macos.yml binds Postgres to
# 127.0.0.1 (not the tailnet IP)? Because the stack should come up with the
# tailnet already routable: the replica's Postgres dials the primary over
# Tailscale the moment it starts, and `tailscale serve` (when configured for
# 5432) needs tailscaled up. Unlike the Linux port-bind failure this isn't
# fatal — but waiting keeps every boot deterministic instead of "usually
# fine, occasionally 2 minutes of retry noise".
#
# Exit nonzero on timeout so launchd (KeepAlive SuccessfulExit=false,
# ThrottleInterval 30) retries — the launchd analogue of the bounded
# TimeoutStartSec=120 on Linux: a visible failure loop in the log, never an
# invisible infinite hang.
#
# Invoked by com.hrfunc.colima.plist; not intended for interactive use.
set -euo pipefail

TAILSCALE_BIN="${TAILSCALE_BIN:-/opt/homebrew/bin/tailscale}"
COLIMA_BIN="${COLIMA_BIN:-/opt/homebrew/bin/colima}"
HRSERV_DIR="${HRSERV_DIR:-/opt/hrserv}"
TAILSCALE_WAIT_SECS="${TAILSCALE_WAIT_SECS:-120}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') colima-up: $*"; }

expected_ip=""
if [[ -f "$HRSERV_DIR/docker/.env" ]]; then
    expected_ip="$(sed -n 's/^TAILSCALE_IP=//p' "$HRSERV_DIR/docker/.env" | tail -1)"
fi
log "waiting up to ${TAILSCALE_WAIT_SECS}s for tailnet IP ${expected_ip:-(any)}"

deadline=$((SECONDS + TAILSCALE_WAIT_SECS))
while true; do
    current_ip="$("$TAILSCALE_BIN" ip -4 2>/dev/null | head -1 || true)"
    if [[ -n "$current_ip" && ( -z "$expected_ip" || "$current_ip" == "$expected_ip" ) ]]; then
        break
    fi
    if (( SECONDS >= deadline )); then
        log "ERROR: tailnet IP not assigned after ${TAILSCALE_WAIT_SECS}s (currently: '${current_ip:-none}'); exiting so launchd retries"
        exit 1
    fi
    sleep 2
done
log "tailnet IP $current_ip assigned; starting colima"

# Foreground so launchd owns the VM lifecycle (same invocation brew services
# uses). If the VM is already running, colima exits 0 and, per
# SuccessfulExit=false, launchd leaves it alone.
exec "$COLIMA_BIN" start --foreground
