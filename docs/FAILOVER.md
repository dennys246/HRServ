# HRServ failover runbook

> **🚨 STATUS (as of 2026-05): NOT YET POSSIBLE.** This runbook assumes a
> production replica exists. As of writing, only `hrserv-1` is deployed.
> Phase 2c (test replica on a home-built PC) and the August Mac Mini
> arrival will land the replica that makes this runbook executable.
>
> **Current realistic recovery if hrserv-1 dies right now:** stand up a
> fresh hrserv-1 on whatever hardware you can find, re-run the Phase 2a
> runbook (`docs/PHASE_2A_HRSERV1_SETUP.md`), and accept the data loss for
> everything submitted since shadow went live. **The legacy `flask.jib-
> jab.org` backend is still authoritative** during the shadow window, so
> its database is the only source of truth that survives.
>
> Until Phase 2c lands, also see `docs/BACKUP_RESTORE.md` — backups are
> similarly aspirational.

When the current primary is unrecoverable, promote the current replica. This
is a manual process — the wrong promotion at the wrong time produces
split-brain (two nodes both accepting writes, diverging schemas/rows). Read
all the way through before doing anything.

**Node naming**: at bootstrap, `hrserv-1` is the primary and `hrserv-2` is the
replica, but after a failover those roles can swap. This runbook uses "current
primary" / "current replica" rather than the machine names so it stays correct
across role flips.

## Pre-flight: is failover actually necessary?

Try these first, in order:

1. **Is the host alive?** `tailscale ping <primary-host>` from the replica.
2. **Is the Postgres container running?** `docker compose -f docker/docker-compose.primary.yml ps`
3. **Is HRServ running?** `curl -sS https://api.hrfunc.org/healthz`
4. **Restart the stack** if the host is alive but the services are wedged:
   `docker compose -f docker/docker-compose.primary.yml restart`

Failover is only the answer if the current primary is truly gone or so degraded that
restarting it isn't an option.

## Step 1 — fence the old primary

Before promoting, ensure no client can keep writing to the old primary:

1. In the Cloudflare dashboard, **disable the api.hrfunc.org ingress rule** on the old
   primary's tunnel (or delete the DNS record temporarily — DNS change propagates
   faster than tunnel reconfig).
2. If the old primary is reachable: `docker compose -f docker/docker-compose.primary.yml down`
3. Double-check no writes are landing: tail the application logs and watch
   `hrf_submissions` row count for ~30 seconds.

## Step 2 — promote the current replica

On the current replica:

```bash
cd /opt/hrserv
./scripts/promote_replica.sh           # dry run, prints what it would do
./scripts/promote_replica.sh --confirm # actually promote
```

The script:
- verifies `pg_is_in_recovery()` returns `t` (refuses to promote a primary on top of itself)
- runs `pg_ctl promote` inside the postgres container
- restarts the `hrserv` container with `NODE_ROLE=primary`

## Step 3 — switch DNS

In the Cloudflare dashboard:
1. Repoint `api.hrfunc.org` to the new primary's tunnel.
2. Confirm `curl https://api.hrfunc.org/healthz` returns 200 from outside the LAN.
3. Re-enable the Access policy for `/upload_json` if it was disabled during fencing.

## Step 4 — smoke test

Submit a known-good past upload via curl using the service tokens + an active app key:

```bash
curl -sS -X POST https://api.hrfunc.org/upload_json \
    -H "CF-Access-Client-Id: $CF_ID" \
    -H "CF-Access-Client-Secret: $CF_SECRET" \
    -H "x-api-key: $HRSERV_API_KEY" \
    -F "jsonFile=@samples/known-good.json"
```

Expect HTTP 200 and a JSON body with an integer `id`. If a row with the same `stored_filename`
already exists, the response still returns 200 with the existing id — that's the intended
idempotency.

## Step 5 — re-establish replication

The new primary is now running `docker-compose.primary.yml`; commit that role swap in
infra notes. Then, once the old primary returns:

1. Wipe the old primary's data dir.
2. `pg_basebackup -h <new-primary-tailnet-ip> -U replicator -D /var/lib/postgresql/data -X stream -P`
3. Write `standby.signal` into the data dir.
4. Update `primary_conninfo` in `replica.conf` to point at the new primary's tailnet IP.
5. Bring it up as the new replica (i.e. switch to `docker-compose.replica.yml`).
6. Verify replication: insert a test row on the new primary, confirm it appears on the new replica.
7. **Boot-chain install on the resurrected node.** Run `docs/NEW_NODE_SETUP.md` Step 9.5 on the
   newly-demoted node so it survives reboots cleanly. The replica's compose file also binds
   `${TAILSCALE_IP}:5432`, so the same wait-for-tailscale drop-in is required — without it,
   any reboot will reproduce the 2026-05-16 outage on the demoted node.

## Step 6 — post-mortem

Within 24 hours, write up:
- What failed on the old primary
- How long was the read window down (HRServ /healthz outage)
- How long was the write window down (the gap between fencing the old primary and the DNS switch)
- What's the root-cause fix to prevent a repeat
- Whether the manual-failover threshold should change

Append the post-mortem to this file's history section (below) so the next operator has the
context for next time.

## History

(none yet — keep this section append-only.)
