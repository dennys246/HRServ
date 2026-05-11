#!/usr/bin/env bash
# Apply baseline migrations to the throwaway test database.

set -euo pipefail

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    -f /migrations/0001_init.sql
