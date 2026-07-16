# Monitoring HRServ

What to monitor, recommended tools, alerting thresholds. As of 2026-05
none of this is wired up — this doc is the to-do list for the first
post-shadow operational hardening pass. (2026-07-15: UptimeRobot +
Pushover wiring in progress alongside the hrserv-2 bring-up; see the
per-node monitors section.)

## The single most important thing to do today

**Set up an external `/healthz` monitor before cutover.** Without it, if
HRServ goes down at 3am, no one notices until a researcher tries to
upload and gets an error. With cutover happening in weeks, this becomes
critical.

Recommended (any of these work; pick whichever you'll actually keep
running):

| Service | Free tier | Setup time |
|---|---|---|
| **UptimeRobot** | 50 monitors @ 5-min interval | 5 min |
| **BetterStack** | 10 monitors @ 3-min interval | 10 min |
| **Cron-poll on a different VPS** | depends on your VPS | 30 min |

Setup (UptimeRobot example):

1. Sign up at uptimerobot.com (free)
2. Add monitor:
   - Type: HTTP(s)
   - URL: `https://api.hrfunc.org/healthz`
   - Interval: 5 minutes
   - Match: HTTP status is `200` AND body contains `"status":"ok"`
3. Alert contacts: your email + SMS
4. Add a second monitor for `https://hrfunc.org/` (or `www.hrfunc.org/`)
   — covers the frontend's availability too

Result: 5+ minutes of downtime → email/SMS. Resolves itself when health
returns.

## Per-node monitors (hrserv-2 onward) — added 2026-07-15

The production monitor above watches `api.hrfunc.org`, i.e. **whichever
node currently holds production** — it follows the hostname through a
failover with no edits. To also watch a specific standby node (the Mac
Mini), give that node's tunnel a monitoring-only hostname:

1. Zero Trust dashboard → Networks → Tunnels → **`hrserv-2`** (⚠️ the
   replica's tunnel — NOT jib-jab's; adding hostnames to the production
   tunnel is a failover action, per `docs/FAILOVER.md`) → Public Hostname
   → Add: subdomain `hrserv-2`, domain `hrfunc.org`, **path `healthz`**,
   service `HTTP` → `hrserv:8000`. The path restriction 404s everything
   else at the edge; `api.hrfunc.org` routing is untouched.
2. Verify: `curl -sS https://hrserv-2.hrfunc.org/healthz` →
   `{"status":"ok","db":true,"node_role":"replica"}`.
3. UptimeRobot: Keyword monitor, keyword `"status":"ok"` (alert when
   missing), 5-min interval, 2 consecutive failures before alerting.

Keep the two nodes' alerts distinguishable — a replica blip must not read
like production down at 3am:

| Monitor name | URL | Alert contacts | Severity |
|---|---|---|---|
| HRServ PROD (api.hrfunc.org) | `https://api.hrfunc.org/healthz` | Pushover + email | SEV1 — wake up Denny |
| HRServ replica (big-mac-mini) | `https://hrserv-2.hrfunc.org/healthz` | email only | SEV2 — business hours |
| Frontend | `https://hrfunc.org/` | Pushover + email | SEV1 |

Pushover hookup: use UptimeRobot's native Pushover alert contact if the
plan offers it; otherwise Pushover's email gateway (a `@pomail.net`
alias created in Pushover's settings) as an email contact works on any
tier.

After a failover/role swap: the PROD monitor needs nothing. Add a
`hrserv-1.hrfunc.org` healthz hostname + replica monitor for the demoted
node, same pattern as above. Expect reboot drills on a node to trip its
per-node monitor briefly — that's the alert chain working, not an
incident.

## Beyond uptime — what else to track

### Submission rate

A daily counter of `hrf_submissions` rows added. Drops to zero unexpectedly
= something is wrong upstream (frontend, Cloudflare, DNS).

Implementation today: manual psql query. Future: a tiny scheduled job
that emails a daily summary.

```bash
# Daily count for the past 7 days:
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT date_trunc('day', uploaded_at) AS day, count(*)
     FROM hrf_submissions
     WHERE uploaded_at > now() - interval '7 days'
     GROUP BY day ORDER BY day DESC;"
```

### Shadow divergence (during the window)

Render logs filter for `shadow_divergence`. Should be empty. Each one is
a real anomaly. See `docs/SHADOW_WINDOW.md` for triage.

### Disk space on jib-jab

The data dir grows with every submission. With JSONB ~10-50 KB per row
and (rough estimate) ~10 submissions per week, growth is slow — gigabytes
per year. But the docker layer cache also grows. Check monthly:

```bash
df -h /
du -sh /var/lib/docker/volumes/hrserv_pg_data_primary/ 2>/dev/null
```

Alert threshold: < 10 GB free on the data partition.

### Postgres replication lag (Phase 2c onward)

When hrserv-2 joins, check `pg_stat_replication` on hrserv-1:

```bash
dc exec postgres psql -U postgres -d hrserv -c \
    "SELECT client_addr, state,
            pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
     FROM pg_stat_replication;"
```

Alert: `lag_bytes > 100 MB` for more than 5 minutes, OR `state != 'streaming'`.

### Tailscale connectivity

Both nodes should appear as `online` in `tailscale status` on each other.
Lost tailnet connectivity → replication breaks silently.

```bash
tailscale status | grep -v offline
```

Alert: peer node not shown as online.

### Tailscale key expiry

**Hard deadline:** without intervention, jib-jab's Tailscale key expires
~2026-11-07 (180 days after the 2026-05-11 install). Disable expiry in
https://login.tailscale.com/admin/machines NOW; otherwise put a calendar
reminder for 2026-10-15 to re-auth.

### Cloudflare tunnel health

Render dashboard for hrfunc-web won't show this — it's on the Cloudflare
side. Quick check:

```bash
# From any external client:
curl -sS https://api.hrfunc.org/healthz
# 200 + JSON = tunnel + HRServ both healthy
# 530 / 502 = tunnel connector died on hrserv-1
# 522 = tunnel itself is up but origin (cloudflared) can't reach hrserv:8000
```

Status is also visible in the Cloudflare dashboard → Networks → Tunnels.
"Healthy" with 4 connections = good.

## Log retention (or lack thereof)

The biggest observability gap:

- **Render logs**: ~7 days on free tier (varies)
- **HRServ container logs**: only as long as the container runs
  (`dc logs` shows what's currently buffered, not historical)
- **Postgres logs**: inside the container, lost on `dc down`
- **No structured persistent log store** anywhere

Suggested mitigations, in priority order:

1. **Docker logging driver with rotation** — configure
   `docker-compose.primary.yml` with:
   ```yaml
   logging:
     driver: json-file
     options:
       max-size: 10m
       max-file: 5
   ```
   on each service. At least keeps recent logs accessible across container
   restarts.

2. **A nightly log archive** — `docker compose logs hrserv > /var/log/hrserv/hrserv-$(date).log`
   via cron, with `logrotate` keeping 30 days. Gives you offline-grepable
   logs.

3. **Render log download** — Render exposes a "Logs → Download" feature
   for time windows. Run this weekly during shadow validation; archive
   to your own machine.

4. (Future) **Loki + Grafana** or a similar SaaS — overkill for current
   scale.

## Render-side monitoring

Render has its own metrics page per service (CPU, memory, request rate).
Worth knowing about but not the primary signal — the application-level
signals (`shadow_write`, `hrf_submissions` rows) are more meaningful for
our use case.

## Alerting thresholds — first draft

Until you've run the system for a while and have a sense of normal:

| Alert | Threshold | Severity |
|---|---|---|
| `/healthz` returns non-200 | 2+ consecutive checks (~10 min) | SEV1 — wake up Denny |
| Tunnel "Inactive" in Cloudflare dashboard | 5 min | SEV2 |
| `hrf_submissions` no new rows | 7+ days during active period | SEV3 — investigate during normal hours |
| Disk on jib-jab < 5GB free | once | SEV2 |
| Replication lag > 1GB (Phase 2c+) | 5+ min | SEV2 |
| Daily `shadow_divergence` count > 0 (shadow window) | once | SEV3 — triage per SHADOW_WINDOW.md |

Adjust as you learn what "normal" looks like.

## What's NOT worth monitoring (over-alerting trap)

- argon2 verify timing variance — known + acceptable
- transient single-request 5xx — happens; investigate only if pattern emerges
- Cloudflare Access "challenged" hits from bots scanning `/upload_json` —
  expected background noise from internet scanners (see hrfunc-web Render
  logs flood for examples)
- Daemon thread count on hrfunc-web — currently unbounded, will be a
  problem under sustained burst; monitor only if a real burst happens
