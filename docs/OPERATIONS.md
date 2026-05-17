# HRServ day-to-day operations

Quick reference for what to do regularly, what to check, and what to do
when something looks wrong. Pairs with `docs/ARCHITECTURE.md` (what the
system is) and `docs/FAILOVER.md` (when it's broken).

## Daily / weekly habits

### Watch the shadow window (active 2026-05 onward)

Phase 2b is live: every upload through hrfunc.org dual-writes to both the
legacy backend and HRServ. The shadow validation window runs until you flip
`HRFUNC_UPLOAD_URL` on Render. While that's open:

- On hrfunc-web's Render log stream, grep `shadow_write status_match`. You
  want a steady flow of `status_match=true` and zero `shadow_divergence`
  WARN entries. Any divergence is a signal to investigate before cutover.
- See `docs/SHADOW_WINDOW.md` for the structured evaluation procedure.

### Quick health check

```bash
# On hrserv-1:
cd /opt/hrserv
dc ps                                          # all three containers Up + (healthy)
curl -fsS http://127.0.0.1:8000/healthz 2>&1   # won't work; no host port
# Use the compose-internal call instead:
dc exec hrserv python -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
```

From outside the LAN:
```bash
curl -sS https://api.hrfunc.org/healthz
# Expect: {"status":"ok","db":true,"node_role":"primary"}
```

### Spot-check recent submissions

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT id, uploaded_at, study, submitter_email, size_bytes
     FROM hrf_submissions
     ORDER BY id DESC LIMIT 10;"
```

### Tail logs

```bash
dc logs -f hrserv                              # request handling
dc logs --tail 200 postgres                    # if something looks wrong with DB
dc logs --tail 200 cloudflared                 # tunnel health
```

## Useful shell aliases

These were added to dennys' `~/.bashrc` on hrserv-1 during Phase 2a:

```bash
alias dc='docker compose -f docker/docker-compose.primary.yml'
```

Recommended additions (not yet committed; consider adding):

```bash
alias dclogs='dc logs -f hrserv'
alias dcstatus='dc ps && echo --- && dc exec hrserv python -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8000/healthz\").read().decode())"'
alias dclatest='dc exec postgres psql -U hrserv -d hrserv -c "SELECT id, stored_filename, study, uploaded_at FROM hrf_submissions ORDER BY id DESC LIMIT 10;"'
alias dckeys='dc exec postgres psql -U hrserv -d hrserv -c "SELECT id, scopes, created_at, revoked_at FROM api_keys;"'
```

## Common operations

### Mint a new API key

See `docs/KEY_ROTATION.md` for the full procedure. Quick version:

```bash
dc exec hrserv hrserv-mint-key --label <descriptive-label>
# Captures plaintext to stdout EXACTLY ONCE. Save to password manager
# immediately. The argon2 hash lands in api_keys.
```

### List active API keys

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT id, scopes, created_at, revoked_at FROM api_keys ORDER BY created_at DESC;"
```

### Revoke an API key

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "UPDATE api_keys SET revoked_at = now() WHERE id = '<label>';"
```

Revocation is immediate — next request with that key will 401. Plaintext
is unrecoverable, so a revoked key is effectively dead.

### Restart a service after a config change

```bash
dc restart hrserv         # picks up env-var changes / image rebuild
dc restart cloudflared    # rare; if tunnel token rotated
dc restart postgres       # if pg_hba.conf changed (config file is reloaded on restart)
```

To pull new code from `main`:
```bash
cd /opt/hrserv
git pull --ff-only origin main
dc build hrserv           # only if hrserv/ Python code changed
dc up -d                  # compose detects changes and recreates only what's needed
```

### Inspect a specific submission

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT id, stored_filename, submitter_email, study, doi, size_bytes,
            length(content_sha256) AS sha_len, api_key_id, client_ip,
            uploaded_at
     FROM hrf_submissions WHERE id = <ID>;"

# To see the full augmented JSON content:
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT content FROM hrf_submissions WHERE id = <ID>;"
```

### Re-run a migration (if a new one lands)

```bash
cd /opt/hrserv
git pull --ff-only origin main
dc exec postgres psql -U hrserv -d hrserv -f /migrations/000N_whatever.sql
```

Future-Denny note: this assumes the migration is idempotent (uses
`IF NOT EXISTS`, etc.). When we add a real schema-migration framework
post-MVP, this section gets revised.

## Monitoring (what to set up — none of this exists yet)

The single highest-leverage operational gap as of 2026-05 is **no external
monitoring**. Concrete recommendations, in priority order:

1. **UptimeRobot or BetterStack monitor on `https://api.hrfunc.org/healthz`**
   with SMS/email to Denny if it returns non-200 for 2+ consecutive checks.
   ~5 minutes to set up; free tier covers it.
2. **A weekly "submissions landed" report** — query `hrf_submissions`
   `WHERE uploaded_at > now() - interval '7 days'` and email yourself the
   count. If shadow is working, this should match (or exceed if old data
   is also flowing) what the legacy backend received.
3. **A monthly "are backups working" check** — once backups are wired up
   in Phase 2c. Until then, this is "are backups configured yet" which is
   answered by `docs/BACKUP_RESTORE.md`.

## Things that go wrong

> **Can't even SSH to jib-jab?** See [docs/NETWORK_TROUBLESHOOTING.md](NETWORK_TROUBLESHOOTING.md) —
> covers the SSH-hangs / packets-vanish / fail2ban-banned-me class of issues that block you
> from running any of the diagnostics below.

### Symptom: hrserv-web upload returns "Internal server error"

Most likely cause: HRServ container crashed or Postgres is down.

```bash
dc ps                                # which container is down?
dc logs --tail 100 hrserv            # what was the last thing it said?
dc logs --tail 100 postgres
```

Common fixes:
- Postgres OOM kill → bump memory limits in compose; investigate the query
- hrserv worker crash → uvicorn restarts automatically; check why
- Disk full → `df -h`; clean up Docker layers or rotate logs

### Symptom: uploads succeed but `hrf_submissions` doesn't grow

The frontend is talking to the LEGACY backend (which is authoritative
pre-cutover), but shadow forwards to HRServ aren't landing. Check
hrfunc-web Render logs for `shadow_write` / `shadow_divergence` lines.

If you see `shadow_status=401 shadow_body=Invalid API key`:
- The HRFUNC_API_KEY_HRSERV value on Render doesn't match what's in
  `api_keys` on HRServ. See `docs/KEY_ROTATION.md` to verify or rotate.

If you see no `shadow_write` lines at all but there ARE uploads:
- Either `HRFUNC_SHADOW_URL` isn't set on Render (check Environment tab)
- Or `HRFUNC_API_KEY_HRSERV` isn't set (would trigger the
  `HRFUNC_SHADOW_URL is set but HRFUNC_API_KEY_HRSERV is not` WARN at
  startup)

### Symptom: tunnel shows "Inactive" in Cloudflare dashboard

The `cloudflared` container is down or can't reach Cloudflare.

```bash
dc ps                                # is cloudflared up?
dc logs --tail 100 cloudflared       # what was the last connection state?
# Look for "Registered tunnel connection" — should see 4 of them.
# If you see "context deadline exceeded" or DNS errors, network is bad.
```

If the container is up but unregistered: rotate the tunnel token via the
Cloudflare dashboard and update `TUNNEL_TOKEN` in `docker/.env`, then
`dc up -d cloudflared`.

### Symptom: Cloudflare 502 or 503 after a host reboot / network change

Recurred on 2026-05-15 after jib-jab was physically relocated. Symptom:
`cloudflared` and `postgres` containers report healthy, but `hrserv` is
`unhealthy`. External requests get Cloudflare 502.

**Root cause** (identified after parallel review): docker's restart
manager brings containers up before the host network is fully ready AND
bypasses compose's `depends_on: service_healthy` gate. hrserv's lifespan
tries to dial postgres over a half-ready bridge, `create_pool` raises,
and the container goes unhealthy. `dc restart hrserv` doesn't fix it
because (a) restart doesn't re-trigger depends_on gating and (b) the
broken pool state may persist across the restart.

**Permanent fix shipped on `fix/reboot-resilience` branch (commit-ref
when this lands):**
1. `hrserv/main.py` lifespan uses `create_pool_with_retry` (10 attempts
   with 1→30s backoff, ~2 minutes total).
2. `hrserv/db.py` `create_pool` sets `command_timeout=30s` and
   `max_inactive_connection_lifetime=30s` so stale TCP sockets are
   recycled aggressively.
3. Dockerfile healthcheck `--start-period=60s` (was 10s) gives the retry
   loop room to succeed.
4. Dockerfile CMD `--workers 1` (was 2) eliminates uvicorn's
   multi-worker partial-startup edge cases.
5. New `deploy/hrserv.service` systemd unit replaces `restart:
   unless-stopped` as the boot-time controller. Runs `dc down` then
   `dc up -d` after `network-online.target`, so every boot is equivalent
   to the working manual recovery.

**Retroactive install on hrserv-1** (after this PR merges) — three layers,
ALL needed. Skip any one and the same outage recurs:

```bash
cd /opt/hrserv
git pull --ff-only origin main

# (1) WiFi must auto-connect at boot — install NetworkManager if not present.
# Skip if the node is on Ethernet or already has working network-online.target.
sudo apt install -y network-manager
sudo systemctl enable NetworkManager NetworkManager-wait-online
sudo nmcli device wifi connect "SSID" password "PASSWORD"   # save it as auto-connect

# (2) Make Docker wait for Tailscale to assign the tailnet IP before
# trying to bind it on Postgres' container.
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo cp deploy/docker.service.d/wait-for-tailscale.conf \
    /etc/systemd/system/docker.service.d/
sudo systemctl daemon-reload

# (3) HRServ stack systemd unit (runs `dc down && dc up -d` after the
# network is fully up, replacing reliance on compose's restart policy
# for boot ordering only):
sudo cp deploy/hrserv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hrserv
# Don't `systemctl start` yet — that would dc down + up while you're
# in this terminal. The next host reboot picks it up.

# Rebuild the hrserv image to pick up the Dockerfile changes:
dc build hrserv
dc up -d hrserv     # picks up retry + timeouts + workers=1
```

After install, the manual `dc down && dc up -d` recovery should become
rare. If you do still see this symptom, follow the legacy procedure:

```bash
cd /opt/hrserv
dc down          # NO -v flag. Named volumes persist.
sleep 3
dc up -d
sleep 30         # give the retry loop time
dc ps
```

### Symptom: a researcher reports "Upload failed: Invalid API key"

That message comes from the legacy backend (currently authoritative), not
HRServ. Check the legacy backend's API key on Render
(`HRFUNC_API_KEY`, NOT `HRFUNC_API_KEY_HRSERV`). After cutover, the same
message from HRServ would mean the frontend's HRServ key is wrong (see
KEY_ROTATION).

### Symptom: replication lag growing on the future hrserv-2

(Won't apply until Phase 2c.) Check `pg_stat_replication` on the primary
— if `state` is not `streaming`, the replica disconnected. If `state` is
`streaming` but LSN columns are diverging, the replica is slower than the
primary's write rate. Usually means CPU or I/O on the replica is
overloaded; not common at HRF ingest volume.

## Things that go wrong — bigger

For these, follow the dedicated runbook:

- HRServ stack is wedged and a restart doesn't fix it → start with
  `dc down && dc up -d` (preserves named volumes).
- jib-jab is unreachable / dead → `docs/FAILOVER.md` (caveat: requires
  hrserv-2 to exist, which it doesn't yet).
- Need to restore from backup → `docs/BACKUP_RESTORE.md` (caveat: no
  backups wired yet).
- Production data inconsistency between primary and shadow during the
  shadow window → `docs/SHADOW_WINDOW.md` §"Divergence triage".

## Render-side ops

The hrfunc-web service runs on Render. For shadow-mode operations you'll
spend more time in Render's dashboard than on jib-jab:

- **Logs** → search `shadow_write` or `shadow_divergence`
- **Environment** → six HRFUNC_* vars + SECRET_KEY + SMTP_*. The two most
  commonly-changed: `HRFUNC_SHADOW_URL` and `HRFUNC_API_KEY_HRSERV`.
- **Events** → most recent deploy commit hash and status
- **Shell** (paid plans only) → `printf '%s' "$HRFUNC_API_KEY_HRSERV" |
  sha256sum | head -c 16` to verify env var bytes match what your local
  shell sees
- **Manual Deploy → Deploy latest commit** when env-var changes don't
  auto-trigger a redeploy

## Things to NOT do casually

- **Don't `dc down -v`** — the `-v` flag wipes the named Postgres volume.
  Everything is gone. Use `dc down` (without `-v`) for restarts.
- **Don't delete the `flask-frontend` api_keys row** without coordinating
  a rotation with hrfunc-web's `HRFUNC_API_KEY_HRSERV` env var. See
  `docs/KEY_ROTATION.md`.
- **Don't directly edit `pg_hba.conf` for replication peer changes** —
  use `scripts/configure_pg_hba.sh <new-ip>` which handles
  re-substitution + commits cleanly.
- **Don't change passwords in `docker/.env` after Postgres first boot**
  without also running `ALTER ROLE` inside psql. Postgres bakes the
  passwords into role definitions during initdb.
- **Don't push the `flask-frontend` plaintext (or any secret) into any
  git repo.** Use the password manager + Render env vars + jib-jab
  `docker/.env` (which is gitignored).

## Where things live

| Thing | Path / location |
|---|---|
| HRServ install on hrserv-1 | `/opt/hrserv` |
| Postgres data | named volume `hrserv_pg_data_primary` |
| `.env` (gitignored) | `/opt/hrserv/docker/.env` |
| Backup safety dump from legacy | `/var/backups/legacy-hrfuncdb/` |
| Production backups (Phase 2c) | TBD: local `/var/backups/hrserv/` + peer node + B2 |
| Cloudflare tunnel UUID | `4af4fd9c-0510-447d-b1a1-ec9704969988` (hrserv-1) |
| Cloudflare team URL | `maxim-mini.cloudflareaccess.com` |
| Tailscale IP for hrserv-1 | `100.91.182.4` |
| Render service | hrfunc-web |
| Password manager labels | `HRSERV_*` prefix (TUNNEL_TOKEN, ACCESS_*, API_KEY, POSTGRES_PASSWORD, DB_PASSWORD, REPLICATOR_PASSWORD) |
