# Phase 2a — `hrserv-1` setup runbook

Stand up HRServ on the legacy "HRF receive/send" PC while the old Flask
service (`flask.jib-jab.org/upload_json`) keeps running. The two services
share the box but listen on different internal ports and are fronted by
different Cloudflare Tunnels.

This runbook assumes you (Denny) are running the commands at a shell on
`hrserv-1` and pasting back output / errors when something doesn't match
what's described.

## 0. Pre-flight (before touching anything)

On the host:

```bash
# OS + arch:
uname -a
cat /etc/os-release

# What's currently listening — confirm 5432 (Postgres) and 8000 (HRServ
# container's internal port) are FREE on the host, and note where the old
# Flask service listens so we don't clobber it:
sudo ss -tlnp | grep -E ':(5432|6379|8000|80|443|5000|8080)\b' || \
    echo "(none of these ports in use)"

# Does the box already have Docker or Tailscale?
docker --version 2>/dev/null || echo "no docker yet"
tailscale --version 2>/dev/null || echo "no tailscale yet"

# Is there already a cloudflared service on the host (the old Flask might
# run its own tunnel)? Note it but DO NOT disable — the new tunnel will
# run inside a docker compose service, separate from any host-level one.
systemctl status cloudflared 2>/dev/null | head -5 || echo "(no host-level cloudflared service)"

# Disk room:
df -h /
```

Note down for later:
- The internal port the old Flask service binds to (we leave it alone).
- The hostname of any existing Cloudflare Tunnel pointed at this box.
- Whether 5432 is occupied. If it is (an old Postgres for the Flask app):
  STOP — pick a non-default host port for HRServ's Postgres or migrate the
  legacy Postgres off the default port. The compose file binds
  `${TAILSCALE_IP}:5432` so two services on the same port will collide.

## 1. Install Docker (skip if `docker --version` worked in Step 0)

Ubuntu/Debian — use Docker's official repo, NOT the distro's `docker.io`
package. **Do not run `apt-get remove docker*` if Docker is already present
on this box** — that could stop containers the old Flask service runs in.

```bash
# Prerequisites:
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Docker's GPG key + repo:
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine + compose plugin:
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

# Add your user to the docker group:
sudo usermod -aG docker $USER
# Log out + back in (or run `newgrp docker`) for the group change to take.

# Verify:
docker version
docker compose version
```

## 2. Install Tailscale (skip if `tailscale --version` worked in Step 0)

```bash
# Tailscale's install.sh is a third-party shell script run as root — review
# before piping if that worries you (https://tailscale.com/install.sh).
curl -fsSL https://tailscale.com/install.sh | sh

# Bring up Tailscale (opens a browser to auth):
sudo tailscale up

# Confirm tailnet membership + note this node's tailnet IP:
tailscale ip -4
tailscale status
```

**Record the tailnet IP** — we'll bind Postgres to it and reference it
from the test replica's `primary_conninfo` later.

## 3. Clone the repo

```bash
sudo mkdir -p /opt/hrserv
sudo chown $USER /opt/hrserv
cd /opt/hrserv
git clone https://github.com/dennys246/HRServ.git .
git checkout main
```

## 4. Cloudflare Tunnel for HRServ

In the Cloudflare dashboard (cloudflare.com → Zero Trust → Networks → Tunnels):

1. Create a new tunnel named `hrserv-1`.
2. Choose "Docker" as the connector type — copy the `TUNNEL_TOKEN` string
   that's displayed (you'll only see it once; save it to the password manager).
3. Add a Public Hostname:
   - Hostname: `api.hrfunc.org`
   - Service type: `HTTP`
   - URL: `hrserv:8000` (the container name + port within the compose network)

DNS for `api.hrfunc.org` should automatically point at the new tunnel — verify
under `hrfunc.org` → DNS that there's a `CNAME api → <tunnel-id>.cfargotunnel.com`.
Note: the existing `flask.jib-jab.org` tunnel for the old service is untouched.

## 5. Cloudflare Access policies (TWO apps)

Same dashboard → Zero Trust → Access → Applications. Create BOTH of these:

### App 1: `/upload_json` (service-token protected)

- Type: Self-hosted
- Hostname: `api.hrfunc.org`
- Path: `/upload_json`
- Policy:
  - Name: `frontend-service-token`
  - Action: Service Auth
  - Include: Service Token → create new → `flask-frontend`
  - Save both the **Client ID** and **Client Secret** to the password manager
    — they're only shown once.

### App 2: `/healthz` (public)

- Type: Self-hosted
- Hostname: `api.hrfunc.org`
- Path: `/healthz`
- Policy:
  - Name: `public-healthz`
  - Action: Bypass (no auth required)
  - Include: Everyone

The frontend polls `/healthz` to auto-toggle its maintenance banner, and your
own monitoring will hit it from anywhere — both need to bypass Access.
Without this app, `/healthz` inherits the zone-wide default policy, which is
indeterminate.

## 6. Configure the `.env` file

The `.env` file MUST live next to the compose file (compose auto-loads
`.env` from the compose-file directory, not the cwd):

```bash
cd /opt/hrserv
cp .env.example docker/.env
$EDITOR docker/.env
```

Fill in:
- `POSTGRES_PASSWORD` — generate a fresh random password (`openssl rand -base64 32`)
- `HRSERV_DB_PASSWORD` — generate a fresh random password (different from above)
- `REPLICATOR_PASSWORD` — generate a fresh random password (different from above)
- `TAILSCALE_IP` — what `tailscale ip -4` returned in Step 2
- `REPLICA_TAILSCALE_IP` — leave the placeholder for now; set in Phase 2c
  when the test replica joins the tailnet
- `TUNNEL_TOKEN` — the token from Step 4

`.env` is gitignored. Save the passwords to your password manager.

**Important caveat about password changes:** the values for `HRSERV_DB_PASSWORD`
and `REPLICATOR_PASSWORD` are baked into the Postgres roles on the container's
**first boot only** (via `docker/postgres/initdb/01-create-roles.sh`). Once
the data directory has been initialized, editing `.env` will NOT change the
DB role passwords — the app will simply fail to connect until you also run
`ALTER ROLE hrserv WITH PASSWORD '...'` inside psql. So get these right
before Step 8.

## 7. Substitute the pg_hba.conf replication placeholder (REQUIRED)

`docker/postgres/pg_hba.conf` ships with a deliberately-invalid placeholder
for the replication peer IP. Postgres WILL refuse to start until you
substitute a syntactically-valid address into it. Since no real replica
exists yet, use `127.0.0.1` — replication from anywhere real will fail to
match the rule (which is what we want):

```bash
cd /opt/hrserv
./scripts/configure_pg_hba.sh 127.0.0.1
git diff docker/postgres/pg_hba.conf   # confirm only the replication line changed
```

The script handles re-substitution automatically: when Phase 2c brings the
test replica online, re-run with the replica's actual tailnet IP and
restart Postgres.

## 8. Bring up the stack

```bash
cd /opt/hrserv
docker compose -f docker/docker-compose.primary.yml up -d
docker compose -f docker/docker-compose.primary.yml ps
docker compose -f docker/docker-compose.primary.yml logs --tail 50 hrserv
docker compose -f docker/docker-compose.primary.yml logs --tail 50 cloudflared
```

Expect:
- `postgres` healthy after a few seconds
- `hrserv` started: `HRServ 0.1.0 started; node_role=primary db_pool=1-8`
- `cloudflared` registered: `Registered tunnel connection`

If `postgres` crashloops with a pg_hba complaint, you skipped Step 7.

## 9. Verify externally

From a device NOT on your LAN (phone with wifi off, friend's machine,
GitHub Actions, etc.):

```bash
curl -sS https://api.hrfunc.org/healthz
# Expect: {"status":"ok","db":true,"node_role":"primary"}

curl -sS -o /dev/null -w "%{http_code}\n" \
    -X POST https://api.hrfunc.org/upload_json
# Expect: 401 (Cloudflare Access blocks at the edge — no service token)

curl -sS -X POST https://api.hrfunc.org/upload_json \
    -H "CF-Access-Client-Id: <client id from step 5>" \
    -H "CF-Access-Client-Secret: <client secret from step 5>"
# Expect: 401 from HRServ ("Missing x-api-key header" plain text)
```

If `/healthz` doesn't return 200 from off-LAN: check cloudflared logs,
verify the Access "public-healthz" app exists, verify DNS resolution
of `api.hrfunc.org`.

## 10. Mint the frontend's API key

```bash
cd /opt/hrserv
docker compose -f docker/docker-compose.primary.yml exec hrserv \
    hrserv-mint-key --label flask-frontend
```

This prints the secret to stdout **once**. Save it to the password manager
immediately under a clear label (`HRSERV_API_KEY` or similar).

## 11. End-to-end smoke from outside the LAN

Use the repo's test fixture; it already has a complete `_hrf_submission`
envelope so no editing is needed:

```bash
# On the host (or any machine with the repo cloned):
cd /opt/hrserv
curl -sS -X POST https://api.hrfunc.org/upload_json \
    -H "CF-Access-Client-Id: $CF_ID" \
    -H "CF-Access-Client-Secret: $CF_SECRET" \
    -H "x-api-key: $HRSERV_KEY" \
    -F "jsonFile=@tests/fixtures/sample_hrf.json"
# Expect: 200 {"ok":true,"id":1,"stored_filename":"study_HRFs_2026-05-11_..."}
```

Then verify the row landed in Postgres:

```bash
docker compose -f docker/docker-compose.primary.yml exec postgres \
    psql -U hrserv -d hrserv -c \
    "SELECT id, stored_filename, study, doi, size_bytes FROM hrf_submissions;"
```

**Caveat — idempotent retries:** the fixture has a fixed `stored_filename`.
A second `curl` will return `200` with the **same `id`** (ON CONFLICT DO
NOTHING), not insert a new row. That's the contract working, not a no-op
disguised as success. To exercise a fresh insert, edit the fixture's
`stored_filename` first or use a different file.

## 12. Final notes

- The old Flask service should still be reachable at `flask.jib-jab.org`.
  Hit it from outside the LAN and confirm it returns its usual responses.
  We have not touched it.
- **Backups are NOT real yet.** Phase 2a has no peer node and no B2 account
  configured, so even if `scripts/backup.sh` runs nightly it would only
  write to `/var/backups/hrserv/` on the same disk as Postgres — a single
  disk failure wipes both. Treat Phase 2a as "the dataset is recoverable
  by re-running the shadow phase", not "we have backups." Wire up B2 in
  Phase 2c when the test replica goes in.
- File anything that surprised you during this runbook into the "Lessons
  learned" section below — append-only, with date.

## Lessons learned

(none yet — append after running this for real.)
