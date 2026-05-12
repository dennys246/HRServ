# HRServ backup and restore runbook

> **🚨 STATUS (as of 2026-05): NOT YET WIRED UP.** Phase 2a deployed
> HRServ on a single node with no backups configured. The `scripts/backup.sh`
> exists but no cron is scheduled, no Backblaze B2 account is set up, and
> no peer node exists to cross-ship to.
>
> **Real exposure today:** if the disk on `jib-jab.org` dies, every
> `hrf_submissions` row accumulated since shadow went live is lost. The
> legacy `flask.jib-jab.org` backend remains the authoritative source for
> all uploads during the shadow window, so its database is still the
> backup-of-record — but the moment cutover happens (per
> `docs/SHADOW_WINDOW.md`), this gap becomes critical.
>
> **Do NOT cut over to HRServ as authoritative until backups are wired.**
> This is the single highest-priority follow-up before Phase 2c finishes.
>
> The rest of this document describes the TARGET state for Phase 2c.

Backups exist in three places (local node, peer node, Backblaze B2). A backup
you haven't restored is not a backup — the drill described here is mandatory
before Phase 3 of the rollout, and should be re-run quarterly.

## Backup configuration

`scripts/backup.sh` runs nightly on each node via cron. It:

1. Runs `pg_dump --format=custom` against the local Postgres.
2. Encrypts the dump with `age` (recipients listed in `AGE_RECIPIENTS`).
3. Copies the encrypted file to the peer node via `rsync` over Tailscale.
4. Pushes the encrypted file to Backblaze B2 via `restic backup`.
5. Prunes local copies older than 30 days.

Cron entry (each node):
```
15 3 * * * /opt/hrserv/scripts/backup.sh >>/var/log/hrserv-backup.log 2>&1
```

Required env, set in `/etc/default/hrserv-backup`:
```
PGHOST=postgres
PGPORT=5432
PGUSER=hrserv
PGPASSWORD=<from password manager>
PGDATABASE=hrserv

AGE_RECIPIENTS=age1...,age1...   # public keys for both operators' offline-stored private keys

PEER_HOST=hrserv-2.tailnet.ts.net   # peer hostname; flip when running this script on the peer
PEER_DIR=/var/backups/hrserv/from-hrserv-1

RESTIC_REPOSITORY=b2:hrserv-backups:/hrserv-1   # one path per machine identity
RESTIC_PASSWORD=<repo passphrase>
B2_ACCOUNT_ID=<keyId>
B2_ACCOUNT_KEY=<applicationKey>
```

## Restore drill — DO THIS BEFORE GOING LIVE

Goal: prove the entire chain — encrypted dump → decrypt → restore → row count check.
You will run this against a **scratch container** so you can't damage production data.

### Step 1 — bring up a scratch Postgres

```bash
docker run --rm -d --name pg-restore-drill \
    -e POSTGRES_PASSWORD=drill \
    -e POSTGRES_DB=hrserv_restore_drill \
    -p 55433:5432 \
    postgres:16
```

### Step 2 — fetch the latest dump

From B2 (most realistic — proves the off-site copy works):
```bash
export RESTIC_REPOSITORY=b2:hrserv-backups:/hrserv-1
export RESTIC_PASSWORD=...
export B2_ACCOUNT_ID=...
export B2_ACCOUNT_KEY=...

mkdir -p /tmp/restore-drill && cd /tmp/restore-drill
restic restore latest --target . --include '*.sql.age'
```

### Step 3 — decrypt

```bash
age --decrypt --identity ~/.age/operator.key \
    --output hrserv.sql \
    /tmp/restore-drill/var/backups/hrserv/hrserv-*.sql.age
```

### Step 4 — restore into the scratch DB

```bash
pg_restore --no-owner --no-privileges \
    --host 127.0.0.1 --port 55433 \
    --username postgres --dbname hrserv_restore_drill \
    /tmp/restore-drill/hrserv.sql
```

### Step 5 — verify

Compare counts against production. Run on production primary:
```sql
SELECT count(*), min(uploaded_at), max(uploaded_at) FROM hrf_submissions;
```

Run on the scratch DB:
```bash
psql -h 127.0.0.1 -p 55433 -U postgres -d hrserv_restore_drill \
    -c "SELECT count(*), min(uploaded_at), max(uploaded_at) FROM hrf_submissions;"
```

The scratch counts should be **less than or equal to** production (the dump was taken
before the most recent inserts). Time deltas should match.

### Step 6 — spot-check a payload

```bash
psql -h 127.0.0.1 -p 55433 -U postgres -d hrserv_restore_drill \
    -c "SELECT content->'_hrf_submission' FROM hrf_submissions ORDER BY uploaded_at DESC LIMIT 1;"
```

The `_hrf_submission` envelope should be present and parseable.

### Step 7 — clean up

```bash
docker rm -f pg-restore-drill
rm -rf /tmp/restore-drill
```

Record the drill date and any deltas in `docs/drill-log.md` (create the file the
first time; append after each drill).

## Restore for real (disaster scenario)

The procedure is the same as the drill, but:
- Restore into the production Postgres on the surviving node (whichever of hrserv-1/hrserv-2 is still up during failover).
- Run after `promote_replica.sh` and DNS cutover, so writes can resume in parallel with
  the restore catching up older data.
- If both nodes are gone: stand up a fresh Postgres on whatever box is available, restore
  there, point `api.hrfunc.org` at a brand-new tunnel terminating on that box.

In all cases, after restore: run `scripts/mint_key.py` to issue a new API key and tell
the Flask frontend operator to rotate `HRFUNC_API_KEY_HRSERV` on Render.
