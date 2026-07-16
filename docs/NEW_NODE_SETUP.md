# Adding a new HRServ node

How to bring up an additional HRServ node — either as a **test replica**
(Phase 2c, low-stakes validation of streaming replication) or as a
**production replica** (Phase 2c→2d, real DR). The procedure is similar
but the stakes and verification steps differ.

For the FIRST node (the box currently serving as `hrserv-1`), see
`docs/PHASE_2A_HRSERV1_SETUP.md` instead.

## Decide upfront: test or production?

| | Test replica | Production replica |
|---|---|---|
| Purpose | Validate replication mechanics before production stakes | Real DR for hrserv-1 |
| Data | Non-canonical; can be wiped + rebuilt freely | Canonical from the moment it catches up |
| Location | Same building as hrserv-1 OK | MUST be different building/site |
| Backup leg | Doesn't count toward triple-copy | Counts toward triple-copy |
| When deleted | When prod replica arrives | Never (or when retired) |

The May–Aug 2026 plan: home-built PC as test replica → Mac Mini in August
as production replica.

## Prerequisites

Before starting, on the new box:

- **Linux** (x86_64 or arm64) — full procedure including boot-chain orchestration
  (Step 9.5) is supported. Debian 13 (Trixie) is the reference distribution.
- **macOS** — supported via Colima (NOT Docker Desktop — Desktop needs a
  logged-in GUI session, which defeats headless reboot resilience) plus the
  launchd boot chain in `deploy/launchd/` (Step 9.5-mac). One structural
  caveat: Postgres cannot bind the tailnet IP under Colima, so every compose
  command on macOS adds `-f docker/docker-compose.macos.yml` (the Step 3
  alias bakes this in). FileVault must be off.
- Network access to Cloudflare (outbound HTTPS + QUIC on 443) and the
  Tailscale coordination server.
- ~10 GB free disk for Docker + Postgres data + JSON content. More if
  growth is anticipated.
- An SSH path you (the operator) can reach.

On `hrserv-1`, you need:

- Working `dc` alias and the stack running (`dc ps` shows healthy)
- Postgres `REPLICATOR_PASSWORD` from `docker/.env` saved in your password
  manager (will need to type it on the new node)
- The new node's tailnet IP, once Tailscale joins (see Step 2)

## Step 0 — Pre-flight on the new node

```bash
# OS / arch:
uname -a

# Free disk:
df -h /

# Anything already on the ports we'll need? (5432, 8000, 80, 443)
sudo ss -tlnp | grep -E ':(5432|8000|80|443)\b' || echo "(clear)"

# Docker + Tailscale present?
docker --version 2>/dev/null || echo "no docker yet"
tailscale --version 2>/dev/null || echo "no tailscale yet"
```

macOS notes: `ss` doesn't exist — use
`sudo lsof -iTCP -sTCP:LISTEN -n -P | grep -E ':(5432|8000|80|443)\b'`.
And `df -h /` measures the Mac's disk; the actual capacity bound for
Postgres is the Colima VM disk you'll size in Step 1 (`--disk 20`).

If 5432 is occupied by a host-level Postgres, follow `PHASE_2A` Step 0a
(`sudo systemctl stop postgresql && sudo systemctl disable postgresql`)
before continuing.

## Step 1 — Install Docker (skip if present)

Linux one-liner:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker version
```

macOS (Colima):
```bash
brew install colima docker docker-compose
# Wire the compose plugin per `brew info docker-compose` (cliPluginsExtraDirs
# in ~/.docker/config.json), then:
colima start --cpu 2 --memory 4 --disk 20
docker compose version
```

## Step 2 — Install Tailscale and join

Linux:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

macOS — use Homebrew tailscaled as a system daemon, **NOT the GUI app**: the
GUI app only starts after a user logs in, so with it the Step 9.5-mac boot
chain waits forever on every headless reboot. (Everything works
interactively either way, which is exactly why this bites at drill time.)
```bash
brew install tailscale
sudo brew services start tailscale       # root LaunchDaemon (needed for utun)
sudo tailscale up --operator="$USER"     # lets you run the CLI without sudo
tailscale ip -4
```

Record the new node's tailnet IP (e.g., `100.64.x.x`). You'll plug it into
two places: hrserv-1's `pg_hba.conf` (replication peer) and this node's
`.env` (`PRIMARY_TAILSCALE_IP` and `TAILSCALE_IP`).

**Disable key expiry NOW** in https://login.tailscale.com/admin/machines —
otherwise the box drops off the tailnet every 180 days and replication
breaks silently.

## Step 3 — Clone the repo

```bash
sudo mkdir -p /opt/hrserv
sudo chown $USER /opt/hrserv
cd /opt/hrserv
git clone https://github.com/dennys246/HRServ.git .
git checkout main

# Same QoL alias as hrserv-1, but pointing at the REPLICA compose file:
echo "alias dc='docker compose -f docker/docker-compose.replica.yml'" >> ~/.bashrc
source ~/.bashrc
```

On macOS, the alias must also include the Colima port-binding override (and
lands in `~/.zshrc`):

```bash
echo "alias dc='docker compose -f /opt/hrserv/docker/docker-compose.replica.yml -f /opt/hrserv/docker/docker-compose.macos.yml'" >> ~/.zshrc
source ~/.zshrc
```

## Step 4 — On hrserv-1: open replication to this peer

This is the one cross-node coordination step. On `hrserv-1`, with the new
node's tailnet IP (from Step 2):

```bash
cd /opt/hrserv

# Heads-up: pg_hba.conf on hrserv-1's working tree probably has an UNCOMMITTED
# local edit from Phase 2a (the 127.0.0.1 placeholder substituted before any
# real replication peer existed). Check first:
git diff docker/postgres/pg_hba.conf

# If the diff only shows the 127.0.0.1 substitution (expected), the script
# below will rewrite that line again. The committed file at HEAD still has
# REPLACE_WITH_PEER_TAILSCALE_IP, and the substitution-script's regex
# handles both placeholder and previously-substituted IPs.

./scripts/configure_pg_hba.sh 100.64.X.Y   # ← new node's tailnet IP
grep replication docker/postgres/pg_hba.conf
# Expected: host    replication     replicator     100.64.X.Y/32  scram-sha-256

# Restart Postgres on hrserv-1 to pick up the new pg_hba:
docker compose -f docker/docker-compose.primary.yml restart postgres
```

**About committing this change:** The peer IP is a tailnet IP (CGNAT
100.64.0.0/10), not a public IP, so committing it to the repo doesn't
expose anything that isn't already in the Tailscale account. Whether to
commit is a style call:

- **Commit it**: pg_hba.conf becomes a faithful record of the live
  config. Pro: anyone cloning the repo to set up a recovery primary
  gets the right starting state. Con: the file ships out-of-the-box
  with one operator's tailnet IPs in it.
- **Leave uncommitted**: keep the repo's pg_hba.conf as the
  placeholder-only template; operators re-run `configure_pg_hba.sh`
  every time they git pull. Pro: cleaner repo. Con: easy to forget,
  and the file will keep showing as dirty in git.

For now, do not commit until Phase 2c is stable. Track the substituted
value in your password manager or local notes alongside the tailnet IPs.

## Step 5 — On hrserv-1: ensure a replication slot exists

Without an explicit slot, `wal_keep_size=1GB` (in
`docker/postgres/primary.conf`) limits how far behind a replica can fall
before WAL is removed. Creating a slot guarantees retention indefinitely
(at the cost of unbounded WAL growth if the replica disappears — monitor
it).

```bash
# On hrserv-1:
docker compose -f docker/docker-compose.primary.yml exec postgres \
    psql -U postgres -d hrserv -c \
    "SELECT pg_create_physical_replication_slot('hrserv_2');"

# Verify:
docker compose -f docker/docker-compose.primary.yml exec postgres \
    psql -U postgres -d hrserv -c \
    "SELECT slot_name, slot_type, active FROM pg_replication_slots;"
```

Replace `hrserv_2` with whatever this peer will be — slot names use
underscores, not hyphens.

## Step 6 — On the new node: bootstrap the data dir via pg_basebackup

This is the one-time copy that seeds the replica from the primary. Run on
the new node:

```bash
cd /opt/hrserv

# Create the docker volume in advance so basebackup can write to it.
docker volume create hrserv_pg_data_replica

# Capture the primary tailnet IP:
PRIMARY_TS_IP=100.91.182.4   # hrserv-1's tailnet IP (verify with `tailscale status` from new node)
read -s -p "REPLICATOR_PASSWORD: " REPLICATOR_PW; echo

# Run pg_basebackup inside a throwaway postgres container that writes into the volume.
docker run --rm \
    -v hrserv_pg_data_replica:/var/lib/postgresql/data \
    -e PGPASSWORD="$REPLICATOR_PW" \
    postgres:16 \
    pg_basebackup \
        -h "$PRIMARY_TS_IP" \
        -U replicator \
        -D /var/lib/postgresql/data \
        -X stream \
        -S hrserv_2 \
        -R \
        -P -v

unset REPLICATOR_PW
```

What each flag does:
- `-X stream` — stream WAL during backup (no risk of WAL gap)
- `-S hrserv_2` — use the slot we created in Step 5
- `-R` — write `standby.signal` and a partial `postgresql.auto.conf` with
  `primary_conninfo`. Marks the dir as a standby.
- `-P -v` — progress + verbose; useful for the initial sync

This can take seconds to many minutes depending on DB size.

## Step 7 — Configure `.env` on the new node

```bash
cp .env.example docker/.env
chmod 600 docker/.env
$EDITOR docker/.env
```

Fill in:
```
POSTGRES_PASSWORD=<same as hrserv-1's POSTGRES_PASSWORD>
HRSERV_DB_PASSWORD=<same as hrserv-1's HRSERV_DB_PASSWORD>
REPLICATOR_PASSWORD=<same as hrserv-1's REPLICATOR_PASSWORD>

TAILSCALE_IP=<this node's tailnet IP from Step 2>
PRIMARY_TAILSCALE_IP=<hrserv-1's tailnet IP, e.g. 100.91.182.4>
REPLICA_TAILSCALE_IP=<this node's own IP — unused on replica but kept for symmetry>

TUNNEL_TOKEN=<new tunnel token from Step 8 below>
```

If this node will serve `/healthz` to a frontend on a hostname other than
`hrfunc.org` / `www.hrfunc.org` (e.g. a staging frontend), also set:
```
CORS_ORIGINS=https://hrfunc.org,https://www.hrfunc.org,https://staging.hrfunc.org
```
Otherwise the default allowlist covers production and you can leave it unset.

Note on password symmetry: the Postgres role passwords (`POSTGRES_PASSWORD`,
`HRSERV_DB_PASSWORD`, `REPLICATOR_PASSWORD`) effectively come from the
primary via streaming replication — the replica's data dir is a copy of
the primary's, so role definitions match by construction. The replica's
`docker-entrypoint.sh` will SKIP its initdb step entirely because the data
dir is already populated by `pg_basebackup`, which means these env values
are not actually used for first-boot role creation on the replica.

In practice you should still set them to match hrserv-1's values, because:
- if you ever wipe the data dir and re-bootstrap, initdb WILL run on the
  fresh dir using these values
- the values appear in error messages and logs, so having them match
  reduces operator confusion
- the `hrserv` app on the replica reads `HRSERV_DB_PASSWORD` to connect to
  Postgres — so this one DOES matter for HRServ-to-Postgres auth even
  during normal operation

## Step 8 — Cloudflare Tunnel for the new node

The new node has its own tunnel + connector. DNS for `api.hrfunc.org`
stays pointed at hrserv-1's tunnel until manual failover (per
`docs/FAILOVER.md`). The replica's tunnel just sits idle in DNS but is
ready when needed.

1. https://one.dash.cloudflare.com → Networks → Tunnels → Create a tunnel
2. Cloudflared connector → name it `hrserv-2` (or whatever this node is)
3. Docker tab → copy the long token after `--token`
4. Save to password manager as `HRSERV_2_TUNNEL_TOKEN`. Paste into
   `docker/.env` as `TUNNEL_TOKEN`.
5. **Do NOT add a public hostname for this tunnel yet.** During normal
   operation the replica's tunnel is connected but unused. Failover adds
   the public-hostname route to it.

## Step 9 — Bring up the stack

On macOS, add `-f docker/docker-compose.macos.yml` to every command below
(or just use the `dc` alias from Step 3, which includes it) — the replica
file alone tries the tailnet-IP bind that cannot work under Colima.

```bash
cd /opt/hrserv
docker compose -f docker/docker-compose.replica.yml up -d
sleep 5
docker compose -f docker/docker-compose.replica.yml ps
docker compose -f docker/docker-compose.replica.yml logs --tail 30 postgres
docker compose -f docker/docker-compose.replica.yml logs --tail 30 hrserv
```

Expected log signals:
- `postgres`: lines about `started streaming WAL from primary at ...`
- `hrserv`: `HRServ 0.1.0 started; node_role=replica db_pool=1-8`
- `cloudflared`: `Registered tunnel connection` (4 entries)

## Step 9.5 — Wire the boot chain for clean reboots (Linux only)

The replica's `docker-compose.replica.yml` also binds Postgres to
`${TAILSCALE_IP}:5432:5432`, so it inherits the exact same boot race that
hit jib-jab on 2026-05-16: Docker starts before tailscaled finishes
assigning the tailnet IP → port allocation fails → containerd kills the
container → manual `systemctl restart` needed to recover.

This step installs the same three-layer boot orchestration that
`PHASE_2A_HRSERV1_SETUP.md` §9a uses on hrserv-1. On macOS, skip to
Step 9.5-mac instead.

### 9.5.i — WiFi auto-connect at boot (Debian)

If this box is on WiFi (not wired), install NetworkManager so the WiFi
profile comes up automatically at boot:

```bash
sudo apt install -y network-manager
sudo nmcli device wifi connect "<SSID>" password "<PWD>"
sudo nmcli connection modify "<SSID>" 802-11-wireless.cloned-mac-address permanent
sudo nmcli connection modify "<SSID>" 802-11-wireless.powersave 2
sudo systemctl mask networking
sudo chmod -x /etc/wpa_supplicant/ifupdown.sh
```

Why all those steps: see `docs/NETWORK_TROUBLESHOOTING.md` — bad WiFi boot
state can mimic dozens of unrelated symptoms.

### 9.5.ii — Make Docker wait for Tailscale

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo cp /opt/hrserv/deploy/docker.service.d/wait-for-tailscale.conf \
    /etc/systemd/system/docker.service.d/
sudo systemctl daemon-reload
```

This installs `tailscale wait` as an ExecStartPre on docker.service with
`TimeoutStartSec=120`. Docker won't start until `tailscale0` is bindable,
and if Tailscale stays unhealthy for >2 minutes the unit fails cleanly
rather than hanging boot.

**Precondition before install**: verify `which tailscale` returns
`/usr/bin/tailscale` (apt-installed). If Tailscale is installed via static
binary or another path, edit the drop-in's `ExecStartPre=` line to match
before copying.

### 9.5.iii — Install the HRServ stack systemd unit

```bash
cd /opt/hrserv
sudo cp deploy/hrserv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hrserv
# Don't `systemctl start hrserv` from the same terminal you used
# `dc up -d` from in Step 9 — the unit would `dc down` your running stack.
# The next host reboot picks it up naturally.
```

### Verify with a deliberate reboot

```bash
sudo reboot
# Wait ~60-90 seconds, then SSH back in and:
systemctl status hrserv          # active (exited) — stack came up clean
docker compose -f docker/docker-compose.replica.yml ps   # all three healthy
journalctl -u docker -b 0 | grep 'tailscale wait'        # drop-in fired at boot
```

If `hrserv.service` is `failed` after reboot, see
`docs/NETWORK_TROUBLESHOOTING.md` "SSH from Mac to jib-jab is hanging" — the
diagnostic ladder there applies to any reboot-clean failure on the boot chain.

## Step 9.5-mac — Wire the boot chain for clean reboots (macOS / Colima)

The launchd equivalent of Step 9.5, using two LaunchDaemons that run before
login (no auto-login needed): `com.hrfunc.colima` (waits for the tailnet IP,
then runs the Colima VM in the foreground) and `com.hrfunc.hrserv` (waits for
dockerd, then a clean `compose down && up -d`). Full design rationale,
host-hygiene checklist (FileVault OFF, `pmset autorestart`, Homebrew
tailscaled as a system daemon instead of the GUI app), and troubleshooting
live in `deploy/launchd/README.md` — read it once before installing.

```bash
# One-time host settings (details in deploy/launchd/README.md):
sudo pmset -a sleep 0 displaysleep 10 autorestart 1
fdesetup status        # must be Off

# If colima is currently under brew services, hand ownership to our daemon:
brew services stop colima 2>/dev/null || true

sudo /opt/hrserv/deploy/launchd/install.sh
```

The installer checks preconditions, renders your username into the plists,
and installs them WITHOUT starting anything — the next reboot activates the
chain.

### Verify with a deliberate reboot

```bash
sudo reboot
# Wait ~2-3 minutes, SSH back in (over Tailscale), and:
launchctl print system/com.hrfunc.colima | grep -E 'state|last exit code'   # state = running
launchctl print system/com.hrfunc.hrserv | grep -E 'state|last exit code'   # last exit code = 0
tail -40 /opt/hrserv/logs/launchd-hrserv.log                                # healthy `compose ps` table
dc ps                                                                        # all three services up, postgres healthy
```

Run the drill at least twice — boot races don't always fire on the first try.

## Step 10 — Verify replication is healthy

On hrserv-1 (the primary):
```bash
dc exec postgres psql -U postgres -d hrserv -c \
    "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn, sync_state
     FROM pg_stat_replication;"
```

Expected: one row with the new node's tailnet IP as `client_addr`, `state =
streaming`, all four LSN columns within a few bytes of each other, `sync_state
= async`.

On the new node (the replica):
```bash
dc exec postgres psql -U postgres -d hrserv -c "SELECT pg_is_in_recovery();"
# Expect: t

dc exec postgres psql -U postgres -d hrserv -c \
    "SELECT pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()) AS lag_bytes;"
# Expect: 0 or very small
```

Test write propagation: on hrserv-1, INSERT a sentinel row into a test
table; on the new node, SELECT it within 1–2 seconds. (Or just upload a
fresh HRF through hrfunc.org and check `hrf_submissions` on both.)

## Step 11 — Verify HRServ on the new node correctly refuses writes

The replica's HRServ runs with `NODE_ROLE=replica`. It should 503 on
`/upload_json`:

```bash
# From the new node, hit its local hrserv:
docker compose -f docker/docker-compose.replica.yml exec hrserv \
    python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1:8000/upload_json', data=b'', timeout=3)
except urllib.error.HTTPError as e:
    print('status', e.code, e.read().decode()[:200])
"
# Expect: status 503 Node is not the write primary
```

`/healthz` should still respond 200:
```bash
docker compose -f docker/docker-compose.replica.yml exec hrserv \
    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
# Expect: {"status":"ok","db":true,"node_role":"replica"}
```

## Step 12 — Backups (if this is a production replica)

For a test replica, skip — backups stay on hrserv-1 + B2 only.

For a production replica (the Mac Mini in August):
1. Set up Backblaze B2 (or your chosen off-site) per
   `docs/BACKUP_RESTORE.md` — create the bucket, mint app keys, populate
   `RESTIC_REPOSITORY` env on hrserv-1.
2. Configure `scripts/backup.sh` to cross-ship dumps from hrserv-1 to this
   node over Tailscale. Add a cron entry on hrserv-1 (`15 3 * * *`).
   If this node is a Mac: enable Remote Login (System Settings → Sharing)
   so hrserv-1 can rsync in, and create a `PEER_DIR` the operator owns.
   Note `backup.sh` itself is Linux-only today and must be ported before
   it ever RUNS ON a Mac node (see `docs/FOLLOWUPS.md` §"backup.sh is
   Linux-only") — receiving dumps is fine.
3. **Run the restore drill** on a scratch container per
   `BACKUP_RESTORE.md` §"Restore drill". Until this drill succeeds,
   backups don't count.

## Step 13 — Update memory + docs

- `docs/PHASE_2A_HRSERV1_SETUP.md`'s "Lessons learned" — append a dated
  entry for what surprised you setting up this node.
- Memory file `project_hardware_plan.md` — update the "currently a test
  replica" / "Mac Mini production replica" state.
- If this is the August Mac Mini cutover: also delete the test replica
  per `docs/PHASE_2C_TEST_REPLICA_DECOMMISSION.md` (write that doc when
  the time comes).

## Common pitfalls

| Symptom | Likely cause |
|---|---|
| `pg_basebackup: error: connection to server ... failed` | pg_hba on primary not updated (Step 4), or REPLICATOR_PASSWORD wrong, or tailnet route broken |
| `postgres` container on new node crashloops with `could not find primary` | `primary_conninfo` in `postgresql.auto.conf` (written by `pg_basebackup -R`) has wrong host/password |
| Replica's `pg_is_in_recovery()` returns `f` | `standby.signal` missing from data dir |
| Replica lag grows unboundedly | Slot wasn't created on primary, or the `replicator` role can't authenticate so streaming silently dies |
| `Updated to new configuration` for tunnel never logs on the new node | TUNNEL_TOKEN wrong, or the new tunnel was deleted in the dashboard |

When in doubt, check `pg_stat_replication` on the primary first — it tells
you whether the replica is even connected.
