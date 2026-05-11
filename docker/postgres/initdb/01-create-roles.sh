#!/usr/bin/env bash
# Initialise app roles and database. Runs only on first boot, before the
# data dir is populated — Postgres's docker-entrypoint executes anything
# in /docker-entrypoint-initdb.d once.
#
# Variables expected to be present in the env (set by docker-compose):
#   HRSERV_DB_PASSWORD   — password for the application role
#   REPLICATOR_PASSWORD  — password for the replication role
#
# The migrations under /migrations are applied after these roles are set up.

set -euo pipefail

: "${HRSERV_DB_PASSWORD:?HRSERV_DB_PASSWORD must be set}"
: "${REPLICATOR_PASSWORD:?REPLICATOR_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE hrserv LOGIN PASSWORD '${HRSERV_DB_PASSWORD}';
    GRANT ALL PRIVILEGES ON DATABASE hrserv TO hrserv;
    GRANT USAGE, CREATE ON SCHEMA public TO hrserv;

    CREATE ROLE replicator LOGIN REPLICATION PASSWORD '${REPLICATOR_PASSWORD}';
EOSQL

# Apply baseline migrations as the app owner so it owns the tables/sequences.
PGPASSWORD="$HRSERV_DB_PASSWORD" psql \
    -v ON_ERROR_STOP=1 \
    --username hrserv \
    --dbname "$POSTGRES_DB" \
    -f /migrations/0001_init.sql
