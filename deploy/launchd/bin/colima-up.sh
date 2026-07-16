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
LIMACTL_BIN="${LIMACTL_BIN:-/opt/homebrew/bin/limactl}"
HRSERV_DIR="${HRSERV_DIR:-/opt/hrserv}"
TAILSCALE_WAIT_SECS="${TAILSCALE_WAIT_SECS:-120}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') colima-up: $*"; }

# Tolerate the ways operators actually write .env values: quotes, trailing
# whitespace, CRLF. docker compose strips quotes when IT reads .env, so a
# quoted TAILSCALE_IP works everywhere else in the system — this loop must
# not be the one place it breaks (Phase 2b's sharp-edge list is full of
# env-var paste mistakes).
expected_ip=""
if [[ -f "$HRSERV_DIR/docker/.env" ]]; then
    expected_ip="$(sed -n 's/^TAILSCALE_IP=//p' "$HRSERV_DIR/docker/.env" | tail -1 | tr -d "\r\"' \t")"
fi
if [[ -n "$expected_ip" && ! "$expected_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    log "WARNING: TAILSCALE_IP in docker/.env ('$expected_ip') is not a plain IPv4; waiting for any tailnet IP instead"
    expected_ip=""
fi
log "waiting up to ${TAILSCALE_WAIT_SECS}s for tailnet IP ${expected_ip:-(any)}"

deadline=$((SECONDS + TAILSCALE_WAIT_SECS))
while true; do
    current_ip="$("$TAILSCALE_BIN" ip -4 2>/dev/null | head -1 || true)"
    if [[ -n "$current_ip" ]]; then
        if [[ -z "$expected_ip" || "$current_ip" == "$expected_ip" ]]; then
            break
        fi
        if (( SECONDS >= deadline )); then
            # Unlike Linux, nothing on macOS binds the node's OWN tailnet IP
            # (the compose override binds loopback), so a stale expected IP
            # must not keep the stack down. Proceed loudly.
            log "WARNING: tailnet IP is $current_ip but docker/.env expects $expected_ip — stale TAILSCALE_IP after a key re-auth? Proceeding anyway; fix docker/.env."
            break
        fi
    elif (( SECONDS >= deadline )); then
        log "ERROR: no tailnet IP after ${TAILSCALE_WAIT_SECS}s; exiting so launchd retries (is tailscaled running? key expired?)"
        exit 1
    fi
    sleep 2
done
log "tailnet IP $current_ip assigned; starting colima"

# Self-heal stale Lima state from an UNCLEAN shutdown (hard reboot, power
# loss, SIGKILL past the ExitTimeOut grace): Lima's bookkeeping still says
# the instance is Running/Broken, but its hostagent is gone, and
# `colima start` then fails forever with "errors inspecting instance:
# ... ha.sock: connect: connection refused" — a retry loop that never
# converges without hands. Observed live on big-mac-mini 2026-07-15 after
# a reboot drill. When the recorded status and the hostagent socket
# disagree, force-stop to reset the bookkeeping; when state is clean this
# whole block is a no-op.
HA_SOCK="$HOME/.colima/_lima/colima/ha.sock"
lima_status="$("$LIMACTL_BIN" list --format '{{.Status}}' colima 2>/dev/null | head -1 | tr -d '[:space:]')" || lima_status=""
if [[ -n "$lima_status" && "$lima_status" != "Stopped" ]] \
    && ! curl -sf --max-time 3 --unix-socket "$HA_SOCK" http://lima-hostagent/v1/info >/dev/null 2>&1; then
    log "Lima reports status '$lima_status' but the hostagent socket is dead — resetting stale state (unclean shutdown)"
    "$LIMACTL_BIN" stop -f colima || true
fi

# Foreground so launchd owns the VM lifecycle (same invocation brew services
# uses). If the VM is already running, colima exits 0 and, per
# SuccessfulExit=false, launchd leaves it alone.
exec "$COLIMA_BIN" start --foreground
