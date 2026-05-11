#!/usr/bin/env bash
# Substitute the peer's tailnet IP into docker/postgres/pg_hba.conf.
#
# pg_hba.conf does NOT support environment-variable expansion at Postgres
# startup, so we fill in the concrete IP at deploy-time and commit the result
# alongside the per-host .env. (The shipped template uses a deliberately
# invalid placeholder so a missed substitution fails loudly.)
#
# Usage (run from the repo root, on each node):
#     ./scripts/configure_pg_hba.sh 100.64.1.42
#
# Where 100.64.1.42 is the OTHER node's tailnet IP. After running, restart
# Postgres so the new rules take effect:
#     docker compose -f docker/docker-compose.<role>.yml restart postgres
#
# Re-run any time the peer's tailnet IP changes (after a Tailscale auth
# rotation, for example).

set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
Usage: scripts/configure_pg_hba.sh <PEER_TAILSCALE_IP>

Substitutes REPLACE_WITH_PEER_TAILSCALE_IP in docker/postgres/pg_hba.conf
with the given /32 address. The peer is the OTHER node in the primary/replica
pair (when running on hrserv-1, pass hrserv-2's tailnet IP, and vice-versa).
USAGE
    exit 2
}

[ "$#" -eq 1 ] || usage

PEER_IP="$1"

# Validate it looks like an IPv4 address. Tailscale CGNAT is always 100.64/10.
if ! [[ "$PEER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: '$PEER_IP' does not look like an IPv4 address." >&2
    exit 3
fi
if ! [[ "$PEER_IP" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]; then
    echo "WARNING: '$PEER_IP' is outside the Tailscale CGNAT range 100.64.0.0/10." >&2
    echo "         Continuing — confirm this is intentional before deploying." >&2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HBA_FILE="$REPO_ROOT/docker/postgres/pg_hba.conf"

if [ ! -f "$HBA_FILE" ]; then
    echo "ERROR: $HBA_FILE not found" >&2
    exit 4
fi

if ! grep -q 'REPLACE_WITH_PEER_TAILSCALE_IP' "$HBA_FILE"; then
    echo "INFO: $HBA_FILE has no remaining placeholders — nothing to do." >&2
    echo "      (If you need to update the peer IP, edit the file by hand or" >&2
    echo "       reset it from git first.)" >&2
    exit 0
fi

# Portable in-place sed (BSD vs GNU). Use a backup suffix and remove it.
sed -i.bak "s|REPLACE_WITH_PEER_TAILSCALE_IP|$PEER_IP|g" "$HBA_FILE"
rm -f "$HBA_FILE.bak"

echo "Substituted REPLACE_WITH_PEER_TAILSCALE_IP -> $PEER_IP/32 in $HBA_FILE"
echo "Restart Postgres for the new rules to take effect."
