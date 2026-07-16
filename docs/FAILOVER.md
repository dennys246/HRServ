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
>
> **Update 2026-07-15 (supersedes the above):** `big-mac-mini` (macOS,
> Colima) is now the **PRIMARY**, via a fresh-primary standup while jib-jab
> was down — the empty-dataset trade-off was accepted deliberately (see
> "Path B" in `docs/NEW_NODE_SETUP.md`). No promotion was involved, so the
> known issues below remain unexercised and unfixed. `jib-jab` (Linux) is
> pending revival and re-seed as the replica; once it streams, this runbook
> becomes executable — after the KNOWN ISSUES are fixed, and noting that
> the "macOS/Colima notes" section now applies to the PRIMARY side
> (serving replication FROM a Mac is the item-3 pg_hba decision).

> **🐛 KNOWN ISSUES (verified 2026-07-15, unfixed — promotion will NOT work
> as written):** `scripts/promote_replica.sh` has two bugs, platform-independent:
> (a) line 82 runs `pg_ctl promote` via `docker compose exec`, which executes
> as **root** in the postgres image; `pg_ctl` refuses to run as root, so the
> script aborts before promoting (needs `exec -T -u postgres`). (b) line 88's
> `NODE_ROLE=primary docker compose up` is a **no-op**: the compose file
> hardcodes `NODE_ROLE: replica` as a literal, so the env prefix changes
> nothing and hrserv keeps 503ing writes after "successful" promotion.
> Fix + tests tracked in `docs/FOLLOWUPS.md` §"promote_replica.sh cannot
> actually promote" — must land before any real failover.

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

## macOS/Colima notes — read BEFORE promoting a Mac replica

On a Mac node the stack runs under Colima (dockerd inside a Lima VM) with the
boot chain in `deploy/launchd/`. Four deltas from a Linux promotion — decide
and prepare these BEFORE a failover window, not during one:

1. **Always pair role files with the macOS override.** Every compose command
   on a Mac adds `-f docker/docker-compose.macos.yml` after the role file.
   Running `docker-compose.primary.yml` alone recreates Postgres with the
   `${TAILSCALE_IP}:5432` bind, which deterministically fails under Colima —
   Postgres down mid-failover. The `dc` alias also hardcodes the replica
   file; update it at promotion.
2. **Flip the boot chain's role.** After promoting, set
   `COMPOSE_ROLE_FILE="docker-compose.primary.yml"` in
   `deploy/launchd/bin/hrserv-up.sh`. Otherwise the next reboot quietly
   brings the node back up as a replica: every upload 503s while `/healthz`
   stays green, so uptime monitoring never fires. (This file is git-tracked —
   a later `git pull`/checkout can revert the edit. See FOLLOWUPS
   §"Compose-file role coupling".)
3. **Serving replication to the peer requires `tailscale serve` + a pg_hba
   decision.** Postgres binds loopback on macOS; expose 5432 tailnet-only
   with `tailscale serve --bg --tcp 5432 tcp://127.0.0.1:15432` (the host
   bind is 15432 on macOS; peers still dial tailnet port 5432). Proxied
   connections reach Postgres with a Docker-bridge source address, NOT the
   peer's tailnet IP — the `<peer-ip>/32` replication rule never matches, and
   `scripts/configure_pg_hba.sh` cannot express the `172.16.0.0/12` rule
   you'd need (it validates bare IPv4s and appends `/32`). Trade-offs and
   alternatives: `deploy/launchd/README.md` §"Postgres over the tailnet on
   macOS". Afterwards, expect `pg_stat_replication.client_addr` to show the
   bridge address — that's the proxying, not a bug (adjusts Step 5.6's
   verification and NEW_NODE_SETUP Step 10's expected output).
4. **Backups.** `scripts/backup.sh` is Linux-only today (`shred`, `/var`
   paths, cron scheduling). It must be ported before a Mac node is primary —
   tracked in `docs/FOLLOWUPS.md` §"backup.sh is Linux-only".

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
7. **Boot-chain install on the resurrected node.** Run `docs/NEW_NODE_SETUP.md` Step 9.5
   (Linux) or Step 9.5-mac (macOS) on the newly-demoted node so it survives reboots cleanly.
   On Linux the replica's compose file binds `${TAILSCALE_IP}:5432`, so the wait-for-tailscale
   drop-in is required — without it, any reboot reproduces the 2026-05-16 outage. On macOS the
   bind is loopback and the launchd chain handles the ordering.

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
