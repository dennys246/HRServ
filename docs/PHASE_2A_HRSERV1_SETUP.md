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

`/opt/hrserv` is the conventional location (FHS-compliant for add-on service
software) and matches what every node in the cluster will use:

```bash
sudo mkdir -p /opt/hrserv
sudo chown $USER /opt/hrserv
cd /opt/hrserv
git clone https://github.com/dennys246/HRServ.git .
git checkout main
```

**Quality-of-life tip** — add a compose alias to your shell rc, since
you'll type the full `docker compose -f docker/...` prefix dozens of times
during Phase 2:

```bash
echo "alias dc='docker compose -f docker/docker-compose.primary.yml'" >> ~/.bashrc
source ~/.bashrc
```

Then `dc logs -f hrserv`, `dc ps`, `dc exec hrserv hrserv-mint-key --label …`
all work without the long prefix. The runbook below still spells out the full
command for clarity, but use `dc` if you've set up the alias.

## 4. Cloudflare Tunnel for HRServ

> **Heads up — UI rewrite (2026).** Cloudflare significantly restructured the
> tunnel dashboard in early 2026. What used to be called "Public Hostnames" is
> now "**Published applications**", the "Configure" tab is gone, and Networks
> → Tunnels lives in both the Zero Trust dashboard AND the core Cloudflare
> dashboard. The flow below reflects the current UI; if your dashboard looks
> different, the menus may have moved again.

Open https://one.dash.cloudflare.com → **Networks → Tunnels**.
(If your dashboard has consolidated tunnel management into the core dashboard
at `dash.cloudflare.com → Networking → Tunnels` by the time you read this,
that path works too — Cloudflare maintains both.)

### Create the tunnel

1. Click **Create a tunnel** → choose **Cloudflared** as the connector
   (NOT WARP).
2. Name it `hrserv-1` → Save.
3. On the **Install and run a connector** screen, switch tabs to **Docker**
   (NOT Debian / Linux / Windows; those install a host-level systemd service
   that would compete with our compose `cloudflared` container).
4. The token is the long base64 string after `--token` in the example command.
   Copy ONLY that token. Save it to your password manager — suggested label
   `HRSERV_TUNNEL_TOKEN` (it lands in `docker/.env` as the variable named
   `TUNNEL_TOKEN`; the env var name is fixed by cloudflared's convention,
   but the password-manager label is yours to pick — prefixing with `HRSERV_`
   keeps it grouped with the other secrets).
   - Don't paste the token into a shell on hrserv-1 (it'd land in `.bash_history`).
   - Don't run the example `docker run` command from the dashboard — our
     compose stack will run cloudflared in Step 8 using the same token.

### Add the published-application route

5. After the connector step, navigate to the tunnel's **Published application**
   tab. Click **Add a published application** (or **Add route**).
6. Fill in:

| Field | Value |
|---|---|
| Subdomain | `api` |
| Domain | `hrfunc.org` |
| Path | (leave empty — catches all paths under the hostname) |
| Service URL | `http://hrserv:8000` |

Note the `http://` prefix is **required** — the new UI combined the Service
Type dropdown and URL into a single field that validates the protocol. Plain
`hrserv:8000` will be rejected with "Invalid service URL format".

7. Save. Cloudflare automatically creates the DNS CNAME for `api.hrfunc.org`
   pointing at `<tunnel-uuid>.cfargotunnel.com` (Proxied / orange cloud).

> **Gotcha:** if a DNS record for `api` already exists in your `hrfunc.org`
> zone (e.g., from an earlier manual experiment), the save will fail with
> "A record with that host already exists." Delete the manual record under
> **DNS → Records** and re-save the published application; Cloudflare will
> recreate an identical CNAME with the proper "managed by tunnel" tagging.

The existing `flask.jib-jab.org` tunnel for the old Flask service is on a
different zone and is untouched.

## 5. Cloudflare Access policies (TWO apps)

> **🚨 CRITICAL gotcha:** the "Create an application" flow in the 2026 UI does
> NOT force you to attach a policy. If you save the application without
> attaching one, the app shows "Policies assigned: 0" and Cloudflare's
> default action is **deny** — every request gets 302-redirected to the
> Access login page (even `/healthz`, even with valid service tokens). Verify
> `Policies assigned: 1` on each app's details view before considering Step 5
> done. If the dashboard prompts you mid-flow to "add a policy" — do it,
> don't skip past the prompt.

Same dashboard → **Access → Applications**. Create BOTH of these.

### App 1: `hrserv-upload` (service-token protected)

- Type: Self-hosted
- Application name: `hrserv-upload`
- Application Domain: Subdomain `api`, Domain `hrfunc.org`, Path `upload_json`
  (no leading slash — Cloudflare adds it)
- Policy:
  - Name: `frontend-service-token`
  - Action: **Service Auth**
  - Include rule: Selector `Service Token` → `flask-frontend`
    (create the token mid-flow if it doesn't yet exist; capture **Client ID**
    AND **Client Secret** to the password manager BEFORE clicking off the
    dialog — the secret is shown exactly once)

### App 2: `hrserv-healthz` (public)

- Type: Self-hosted
- Application name: `hrserv-healthz`
- Application Domain: Subdomain `api`, Domain `hrfunc.org`, Path `healthz`
- Policy:
  - Name: `public-healthz`
  - Action: **Bypass** (NOT "Allow" — see distinction below)
  - Include rule: Selector `Everyone`

> **Bypass vs Allow — important distinction.** Both sound permissive, but:
>
> - **Bypass** → skip Cloudflare Access entirely for matching requests. No
>   session cookie, no JWT, no challenge. Right pick for an unauthed public
>   endpoint like `/healthz`.
> - **Allow** → still evaluates Access (creates a session, sets cookies);
>   only meaningful when paired with identity providers. Wrong pick for an
>   unauthed endpoint — without an authenticated identity, "Allow" still
>   302-redirects.
>
> Picking Allow here was a real Phase 2a mis-step on the first try.

The frontend polls `/healthz` to auto-toggle its maintenance banner, and your
own monitoring will hit it from anywhere — both need **Bypass**, not Allow.
Without this app, `/healthz` inherits the zone-wide default policy, which is
indeterminate.

### Verify both apps before continuing

Open each app's details page. Both should show **Policies assigned: 1** (NOT 0).
If either reads 0, Edit → Policies → Add → save → re-check. This is the single
most common reason Step 9's smoke tests come back as 302 redirects instead of
hitting HRServ.

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

From any machine that doesn't short-circuit DNS or routing to jib-jab — your
local dev machine (if it's on a different physical network) works, as does
your phone on cellular, a VPS, or GitHub Actions. Cloudflare Tunnel is
outbound-only from the connector, so requests always traverse a Cloudflare
POP regardless of physical location; "different physical network" is just
extra hygiene.

```bash
curl -sSi https://api.hrfunc.org/healthz
# Expect: HTTP/2 200 + {"status":"ok","db":true,"node_role":"primary"}

curl -sS -o /dev/null -w "%{http_code}\n" \
    -X POST https://api.hrfunc.org/upload_json
# Expect: 302 OR 401 (Cloudflare Access blocks at the edge — request never
#         reaches HRServ). Browsers and some curl builds get 302 (redirect
#         to login); other clients get 401. Both signal "edge denied".

curl -sS -X POST https://api.hrfunc.org/upload_json \
    -H "CF-Access-Client-Id: <client id from step 5>" \
    -H "CF-Access-Client-Secret: <client secret from step 5>"
# Expect: 401 + "Missing x-api-key header" plain text (request reached HRServ;
#         app-layer auth caught the missing key)
```

While running these, tail the hrserv logs in another terminal on jib-jab to
verify each request's progress:

```bash
docker compose -f docker/docker-compose.primary.yml logs -f hrserv
```

- The 302/401 case (no service token) should produce **NO** new log entry —
  Cloudflare blocked at the edge before forwarding to the tunnel.
- The 200 and HRServ-401 cases should each produce a log entry with a
  non-`127.0.0.1` source IP (typically something in `172.x.x.x` — that's
  cloudflared inside the compose internal network forwarding the request;
  the exact subnet varies depending on other docker networks on the host).

### If DNS is misbehaving on the test client

Some home routers / ISP resolvers cache `NXDOMAIN` aggressively. If you find
that `dig api.hrfunc.org` returns no answer locally but `dig @1.1.1.1
api.hrfunc.org` does, the local resolver is the culprit. Two options:

1. **Bypass DNS for the test:** use `curl --resolve` to tell curl which IP
   to dial without consulting DNS. Extract the IP via Cloudflare's resolver
   in one step so you don't paste a literal X/Y placeholder:
   ```bash
   IP=$(dig @1.1.1.1 api.hrfunc.org +short | head -1)
   echo "Using $IP"
   curl -sSi --resolve "api.hrfunc.org:443:$IP" https://api.hrfunc.org/healthz
   ```
2. **Point the test client at Cloudflare DNS** (more permanent — Network
   preferences on Mac / network manager on Linux → DNS → `1.1.1.1`).

The router's negative-cache TTL is usually 15–60 minutes; option 2 dodges it.
Production frontend traffic from Render uses public DNS, so this is a
test-client issue only — won't affect real users.

### If you see 302 redirects (especially from `/healthz`)

That almost certainly means an Access app has "Policies assigned: 0". Go
back to Step 5 and verify both apps have their policies attached. This was
the single most common Step 9 failure during the original Phase 2a run.

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

Append-only log of what surprised the operator during a real Phase 2a run.

### 2026-05-11 — hrserv-1 initial setup (Denny + Claude)

- **5432 was occupied** by a host-level Postgres 17 that had been set up
  speculatively for the old Flask app but never actually used. Stopped +
  disabled the systemctl unit (`postgresql.service`), kept the data dir at
  `/var/lib/postgresql/17/` on disk in case we want to refer back. Safety
  pg_dump archived at `/var/backups/legacy-hrfuncdb/` before stopping. Step 0
  pre-flight caught this cleanly.
- **Tailscale 1.96.4** installed fine via the official `install.sh`. Tailnet
  IP for `jib-jab` is `100.91.182.4`. Disable key expiry in the admin panel
  after install (180-day default expiry would silently kick the box off the
  tailnet).
- **Cloudflare Tunnel install path:** the dashboard offers connector-type
  tabs (Docker / Debian / etc.). Picking Debian installed cloudflared as a
  host systemd service which then competed with the compose `cloudflared`
  container. Cleanup is `sudo systemctl stop cloudflared && sudo cloudflared
  service uninstall && sudo rm -f /etc/systemd/system/cloudflared.service &&
  sudo systemctl daemon-reload`. Always pick the **Docker** tab.
- **Access apps shipped with 0 policies attached.** This was the single
  biggest time-sink — the apps existed, the tunnel was healthy, DNS resolved,
  but every request 302-redirected to `cloudflareaccess.com/cdn-cgi/access/login`.
  Diagnostic: `curl -sSi` to see headers, look for the `location: ...login`
  hint and `service_token_status:false` in the JWT meta. Fix: edit each app
  → Policies tab → Add a policy → save → verify "Policies assigned: 1".
- **Cloudflare auto-DNS conflict.** If you manually create the `api` CNAME
  before adding the Published Application route (for instance, while
  debugging DNS), the route save fails with "A record with that host
  already exists." Delete the manual record, save the route, Cloudflare
  recreates an equivalent CNAME with managed-by-tunnel metadata.
- **Service URL required `http://` prefix** in the new "Published
  application" form. Plain `hrserv:8000` is rejected by client-side
  validation; `http://hrserv:8000` works. The schema field disappeared in
  the 2026 UI redesign.
- **Local DNS issue on the Mac.** The Mac's home router (10.39.49.3)
  cached the pre-Cloudflare NXDOMAIN for `api.hrfunc.org` aggressively;
  even after Cloudflare DNS was correct and `dig @1.1.1.1` resolved, the
  Mac's default resolver did not. Workaround for smoke tests:
  `curl --resolve api.hrfunc.org:443:172.67.X.Y https://...`. Permanent
  fix: point Mac DNS at `1.1.1.1` or wait out the cache.
- **Docker group lag.** Even after `usermod -aG docker dennys`, some shells
  (especially `su -`'d shells) didn't pick up the docker group until
  `newgrp docker` or a full SSH re-login. Symptom: `permission denied
  while trying to connect to the docker API at unix:///var/run/docker.sock`.
- **Frontend repo renamed** from `hrfunc-flask-app` to `hrfunc-web` mid-Phase
  to better describe its scope. The minted API key label `flask-frontend`
  was kept as-is (historical) since rotating the key wasn't required just
  to relabel.
