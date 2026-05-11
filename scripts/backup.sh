#!/usr/bin/env bash
# HRServ nightly backup.
#
# Produces an encrypted pg_dump and ships three copies:
#   1. Local on this node          ->  $LOCAL_DIR/hrserv-<ts>.sql.age
#   2. Peer node over Tailscale     ->  rsync to $PEER_HOST:$PEER_DIR
#   3. Backblaze B2 (off-site)      ->  via restic, repository at $RESTIC_REPOSITORY
#
# Required env:
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE   — psql/pg_dump connection
#   AGE_RECIPIENTS                                   — comma-separated age public keys
#   PEER_HOST                                        — e.g. hrserv-2.tailnet.ts.net
#   PEER_DIR                                         — remote backup dir (absolute path)
#   RESTIC_REPOSITORY, RESTIC_PASSWORD               — for B2 push
#   B2_ACCOUNT_ID, B2_ACCOUNT_KEY                    — restic's B2 credentials
#
# Schedule via cron on each node:
#   15 3 * * * /opt/hrserv/scripts/backup.sh >>/var/log/hrserv-backup.log 2>&1
#
# After the first successful run, do the RESTORE drill described in
# docs/BACKUP_RESTORE.md. Untested backups are not backups.

set -euo pipefail

LOCAL_DIR="${LOCAL_DIR:-/var/backups/hrserv}"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
DUMP_PATH="${LOCAL_DIR}/hrserv-${TS}.sql"
ENCRYPTED_PATH="${DUMP_PATH}.age"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

require() {
    if [ -z "${!1:-}" ]; then
        echo "FATAL: required env var $1 is not set" >&2
        exit 2
    fi
}

# Hard-fail early on missing config — easier to debug than a partial backup.
for var in PGHOST PGUSER PGPASSWORD PGDATABASE \
           AGE_RECIPIENTS PEER_HOST PEER_DIR \
           RESTIC_REPOSITORY RESTIC_PASSWORD \
           B2_ACCOUNT_ID B2_ACCOUNT_KEY; do
    require "$var"
done

mkdir -p "$LOCAL_DIR"

log "Step 1/4: pg_dump -> ${DUMP_PATH}"
pg_dump --format=custom --no-owner --no-privileges \
    --file "$DUMP_PATH" \
    "$PGDATABASE"

log "Step 2/4: encrypt with age -> ${ENCRYPTED_PATH}"
# shellcheck disable=SC2086  # AGE_RECIPIENTS is intentionally word-split
age --encrypt $(printf -- '-r %s ' ${AGE_RECIPIENTS//,/ }) \
    --output "$ENCRYPTED_PATH" \
    "$DUMP_PATH"
shred -u "$DUMP_PATH"

log "Step 3/4: rsync to peer ${PEER_HOST}:${PEER_DIR}"
rsync --archive --compress --partial \
    "$ENCRYPTED_PATH" \
    "${PEER_HOST}:${PEER_DIR}/"

log "Step 4/4: restic backup -> ${RESTIC_REPOSITORY}"
restic backup "$ENCRYPTED_PATH" \
    --tag hrserv \
    --tag "node:$(hostname)"

# Prune local copies older than 30 days; B2/restic retention is managed via
# `restic forget --prune` in a separate, less-frequent cron job.
find "$LOCAL_DIR" -type f -name 'hrserv-*.sql.age' -mtime +30 -delete

log "Backup complete: ${ENCRYPTED_PATH}"
