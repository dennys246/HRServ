#!/usr/bin/env bash
# Substitute the peer's tailnet IP into docker/postgres/pg_hba.conf.
#
# pg_hba.conf does NOT support environment-variable expansion at Postgres
# startup, so we fill in the concrete IP at deploy-time and commit the result
# alongside the per-host .env. (The shipped template uses a deliberately
# invalid placeholder so a missed substitution fails loudly — Postgres will
# refuse to load the hba file and the container crashloops, which is what
# we want operators to notice.)
#
# Usage (run from the repo root, on each node):
#     ./scripts/configure_pg_hba.sh 100.64.1.42
#
# Where 100.64.1.42 is the OTHER node's tailnet IP. During Phase 2a (no
# replica exists yet) substitute 127.0.0.1 — replication from anywhere real
# will fail to match, which is what we want. Re-run with the real peer IP
# when the replica is being provisioned.
#
# This script handles BOTH first-time substitution (replacing the
# REPLACE_WITH_PEER_TAILSCALE_IP placeholder) AND later updates (replacing
# whatever IP is currently substituted). After running, restart Postgres
# so the new rules take effect:
#     docker compose -f docker/docker-compose.<role>.yml restart postgres

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

# Match the host-replication-replicator line and rewrite its address.
# This handles both first-time substitution (matches the literal placeholder)
# AND later re-substitution (matches a previously-installed IPv4/32). Anchor
# on the leading `host replication replicator` so we don't touch other lines.
#
# Sed Extended-regex is more readable across BSD/GNU when escaping is light;
# use a Perl-style group to capture the address slot.
REPLICATION_LINE_RE='^([[:space:]]*host[[:space:]]+replication[[:space:]]+replicator[[:space:]]+)([^[:space:]]+)([[:space:]]+.*)$'

if ! grep -qE "$REPLICATION_LINE_RE" "$HBA_FILE"; then
    echo "ERROR: no replication entry found in $HBA_FILE — was it edited by hand?" >&2
    echo "       Restore from git: git checkout -- $HBA_FILE" >&2
    exit 5
fi

# Portable in-place sed (BSD vs GNU). Use a backup suffix and remove it.
sed -i.bak -E \
    "s|$REPLICATION_LINE_RE|\1${PEER_IP}/32\3|" \
    "$HBA_FILE"
rm -f "$HBA_FILE.bak"

# Confirm the substitution took.
NEW_LINE=$(grep -E "^[[:space:]]*host[[:space:]]+replication[[:space:]]+replicator" "$HBA_FILE" || true)
if [ -z "$NEW_LINE" ] || ! echo "$NEW_LINE" | grep -q "$PEER_IP/32"; then
    echo "ERROR: substitution failed; pg_hba.conf may be in an inconsistent state." >&2
    echo "       Inspect: $HBA_FILE" >&2
    exit 6
fi

echo "Set replication peer to $PEER_IP/32 in $HBA_FILE"
echo "Current line:"
echo "  $NEW_LINE"
echo "Restart Postgres for the new rules to take effect."
