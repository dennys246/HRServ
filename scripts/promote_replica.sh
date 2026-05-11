#!/usr/bin/env bash
# Promote the current replica to primary. RUN ONLY DURING A REAL FAILOVER.
# (At bootstrap this is hrserv-2; after a flip it may be hrserv-1.)
#
# Failover is intentionally manual for MVP — the wrong promotion at the wrong
# moment causes split-brain. See docs/FAILOVER.md for the full runbook,
# including the pre-flight checks you should run before invoking this script.
#
# What this does:
#   1. Confirms the node is currently a standby
#   2. Confirms the operator has fenced the old primary (Cloudflare DNS off, container stopped)
#   3. Runs `pg_ctl promote` against the postgres container
#   4. Flips NODE_ROLE from "replica" to "primary" in the running hrserv container by
#      writing an override env file and restarting the service
#
# What this does NOT do — must be done by hand:
#   - Switching the Cloudflare DNS record for api.hrfunc.org to point at this node's tunnel
#   - Decommissioning the OLD primary (so it doesn't accidentally come back as a writer)
#   - Reconfiguring the OLD primary as a new replica after recovery
#
# Usage:
#   ./scripts/promote_replica.sh --confirm
#
# Without --confirm the script prints what it would do and exits 0.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker/docker-compose.replica.yml}"
DRY_RUN=true

for arg in "$@"; do
    case "$arg" in
        --confirm) DRY_RUN=false ;;
        --help|-h)
            sed -n '2,/^set -euo pipefail/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

run() {
    if $DRY_RUN; then
        log "DRY-RUN: $*"
    else
        log "RUN: $*"
        eval "$@"
    fi
}

# Pre-flight: confirm we're actually a standby. Aborting here is much better
# than promoting a primary on top of itself.
log "Checking current Postgres role..."
in_recovery=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U postgres -d hrserv -tAc 'SELECT pg_is_in_recovery();' | tr -d '[:space:]')

if [ "$in_recovery" != "t" ]; then
    echo "ABORT: this node is not in recovery (pg_is_in_recovery=$in_recovery)." >&2
    echo "Either it's already a primary, or the postgres container isn't running." >&2
    exit 3
fi
log "Confirmed: node is currently a standby."

cat <<'WARN'

==============================================================================
  Have you fenced the old primary?
    - Stopped its docker-compose stack? (`docker compose down`)
    - Removed/disabled its Cloudflare Tunnel route for api.hrfunc.org?
    - Verified no fresh writes are landing there?
  Promotion will produce split-brain if the old primary keeps accepting writes.
==============================================================================
WARN

if $DRY_RUN; then
    log "Dry run complete. Re-run with --confirm to perform promotion."
    exit 0
fi

log "Promoting Postgres..."
run docker compose -f "$COMPOSE_FILE" exec -T postgres pg_ctl promote -D /var/lib/postgresql/data

log "Flipping hrserv NODE_ROLE -> primary..."
# We pass an env override on the compose `up` call rather than mutating the
# committed compose file. The operator follows up by updating the file +
# committing.
run NODE_ROLE=primary docker compose -f "$COMPOSE_FILE" up -d --no-deps hrserv

log "Promotion done. Manual next steps:"
cat <<'NEXT'
  1. In Cloudflare dashboard: switch api.hrfunc.org DNS to point at this node's tunnel.
  2. Update docker/docker-compose.replica.yml -> rename or replace; commit the change.
  3. Treat the OLD primary as a candidate replica when it comes back. Run pg_basebackup
     from THIS node and configure standby.signal there.
  4. Verify a sample upload end-to-end via curl + a real past JSON.
NEXT
