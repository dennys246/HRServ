# macOS boot chain (launchd) — Colima + Tailscale + HRServ

The macOS equivalent of the Linux boot orchestration
(`deploy/hrserv.service` + `deploy/docker.service.d/wait-for-tailscale.conf`).
Written for the Mac Mini (`big-mac-mini`) joining as hrserv-2; resolves
`docs/FOLLOWUPS.md` "Mac Mini (hrserv-2) launchd boot orchestration".

Design goal, same as Linux: **a cold boot with nobody logged in ends with the
stack healthy**, or fails *visibly and boundedly* — never an infinite silent
hang.

## How the layers map

| Concern | Linux (jib-jab) | macOS (this dir) |
|---|---|---|
| Container runtime at boot | `docker.service` (systemd) | `com.hrfunc.colima.plist` → `bin/colima-up.sh` → `colima start --foreground` |
| Wait for tailnet IP first | `ExecStartPre=tailscale wait` drop-in, `TimeoutStartSec=120` | poll loop in `colima-up.sh` for the *specific* `TAILSCALE_IP` from `docker/.env`, 120s bound, launchd retries every 30s |
| Clean stack up on boot | `hrserv.service` (oneshot: `dc down && dc up -d`) | `com.hrfunc.hrserv.plist` → `bin/hrserv-up.sh` (waits for dockerd, then `compose down && up -d`) |
| Runtime crash recovery | compose `restart: unless-stopped` | same (unchanged jurisdiction split — see comments in `hrserv.service`) |
| Ordering between units | `After=`/`Requires=` | launchd has no dependency graph — ordering is enforced by the wait loops in the scripts |

Both daemons are **LaunchDaemons** (start at boot, before login — no
auto-login needed) that run **as the operator user** via the `UserName` key,
because Colima's VM state and the docker socket live in the operator's home.

## Key macOS differences you can't skip

0. **Colima must mount `/opt/hrserv` into its VM.** Bind-mount sources
   resolve inside the VM, and Colima only shares `$HOME` and `/tmp/colima`
   by default. For any path outside those, Docker silently fabricates an
   empty directory — Postgres then crash-loops with
   `input in flex scanner failed at file "/etc/postgresql/postgresql.conf"`
   (it's reading a directory). In `~/.colima/default/colima.yaml`, listing
   `mounts:` REPLACES the defaults, so include both:
   ```yaml
   mounts:
     - location: ~
       writable: true
     - location: /opt/hrserv
       writable: true
   ```
   then `colima stop && colima start` (restarts every container in the VM,
   co-tenant projects included). `install.sh` verifies this when the VM is
   running: `colima ssh -- test -f /opt/hrserv/docker/docker-compose.replica.yml`.
1. **Postgres can't bind the tailnet IP under Colima.** dockerd runs inside a
   Lima VM where `${TAILSCALE_IP}` doesn't exist on any interface, so the role
   compose files' `${TAILSCALE_IP}:5432:5432` fails with "cannot assign
   requested address". `docker/docker-compose.macos.yml` overrides the bind to
   `127.0.0.1:15432` — always pass it as a second `-f`. (`TAILSCALE_IP` stays
   required in `docker/.env`; the boot script uses it as the wait target.
   15432 rather than 5432 because the Colima VM is shared with other
   projects' stacks that auto-start at boot — a dedicated port removes the
   reboot race for 5432. Local psql: `psql -h 127.0.0.1 -p 15432`.)
2. **FileVault must be OFF** (`fdesetup status`). With FileVault on, boot
   stops at the disk-unlock screen and nothing below ever runs.
3. **Tailscale via Homebrew as a system daemon**, not the GUI app (which only
   starts after login):
   ```bash
   brew install tailscale
   sudo brew services start tailscale       # root LaunchDaemon; needed for utun
   sudo tailscale up --operator="$USER"     # --operator lets you run the CLI without sudo
   tailscale ip -4
   ```
   Then disable key expiry for this machine in the Tailscale admin console
   (same as NEW_NODE_SETUP Step 2).

## Host settings (one-time)

```bash
# Never sleep; auto-restart after power failure:
sudo pmset -a sleep 0 displaysleep 10 autorestart 1
pmset -g | grep -E 'sleep|autorestart'

# FileVault off (see above):
fdesetup status

# System Settings → General → Software Update → turn OFF automatic
# "Install macOS updates" — unattended update reboots are fine only AFTER
# the reboot drill below has passed at least once.
```

## Install

Prereqs: Colima working for the operator (`colima status`), repo at
`/opt/hrserv`, `docker/.env` populated (NEW_NODE_SETUP Step 7).

```bash
sudo /opt/hrserv/deploy/launchd/install.sh
```

The installer renders the operator username into the plists, copies them to
`/Library/LaunchDaemons`, and lints them. It deliberately does **not** start
anything — `com.hrfunc.hrserv` runs `compose down && up -d` the moment it
loads, which would kill a manually-started stack. The next reboot activates
the chain, and that reboot is the verification drill.

If Colima is currently managed by `brew services start colima`, stop that
first (`brew services stop colima`) — two owners of the same VM means
double-start races.

## Verify with a deliberate reboot

```bash
sudo reboot
# wait ~2-3 min, ssh back in (over Tailscale — that working at all is
# already half the test), then:

launchctl print system/com.hrfunc.colima | grep -E 'state|last exit code'
# want: state = running

launchctl print system/com.hrfunc.hrserv | grep -E 'state|last exit code'
# want: last exit code = 0 (oneshot, already exited)

tail -40 /opt/hrserv/logs/launchd-colima.log /opt/hrserv/logs/launchd-hrserv.log
# want in colima log:  "tailnet IP 100.x.y.z assigned; starting colima"
# want in hrserv log:  "compose up -d" then a healthy `compose ps` table

docker compose -f /opt/hrserv/docker/docker-compose.replica.yml \
               -f /opt/hrserv/docker/docker-compose.macos.yml ps
# want: postgres healthy, hrserv + cloudflared up
```

Run the drill at least twice in a row — the 2026-05-16 class of bug
(races) doesn't always fire on the first boot.

## Troubleshooting

- **Re-run the stack bringup NOW** (disruptive, kills in-flight requests —
  the `systemctl restart hrserv` analogue):
  `sudo launchctl kickstart -k system/com.hrfunc.hrserv`
- **colima daemon flapping**: `tail -f /opt/hrserv/logs/launchd-colima.log`.
  A repeating "waiting for tailnet IP" → exit 1 → 30s retry loop means
  tailscaled isn't assigning the IP: `sudo brew services info tailscale`,
  `tailscale status`. Key expired? (admin console)
- **hrserv oneshot failed** (`last exit code` ≠ 0): the log says whether it
  timed out waiting for dockerd (Colima problem — look one layer down) or
  compose itself failed (look at `docker compose logs`).
- **Everything up but peers can't reach Postgres**: expected as a replica
  (5432 is loopback-only on macOS). See next section before exposing it.

## Postgres over the tailnet on macOS

A **replica** only dials *out* to the primary — it needs no inbound 5432, so
the loopback bind is complete as-is.

When this node is **promoted to primary** and a peer must replicate *from*
it, expose 5432 tailnet-only via Tailscale's TCP proxy (persists across
reboots in tailscaled state; one-time):

```bash
tailscale serve --bg --tcp 5432 tcp://127.0.0.1:15432
tailscale serve status
```

⚠️ **pg_hba implication (decide deliberately at promotion time):** traffic
proxied via `tailscale serve` + the Lima port-forward reaches Postgres with a
Docker-bridge source address, NOT the peer's tailnet IP — so the committed
`pg_hba.conf` replication rule (`host replication replicator <peer-ip>/32`)
will not match, and would need to become a Docker-bridge-range rule
(`172.16.0.0/12`) on this host. The compensating controls are that the host
bind is loopback-only and `tailscale serve` accepts tailnet traffic only, but
this IS a weaker in-database restriction than the Linux setup. Alternatives
(Lima port-forward config, subnet-routing the VM) exist if this trade-off is
unacceptable — evaluate BEFORE a promotion window, never in a hurry during
failover. `docs/FAILOVER.md` §"macOS/Colima notes" carries the checklist
(this pg_hba decision, the `COMPOSE_ROLE_FILE` flip, the always-pair-the-
override rule, and the backup.sh port).

## Files

- `com.hrfunc.colima.plist` / `com.hrfunc.hrserv.plist` — daemon definitions
  (repo copies keep the `REPLACE_WITH_OPERATOR_USER` placeholder; the
  installer renders it)
- `bin/colima-up.sh` — tailnet wait + `colima start --foreground`
- `bin/hrserv-up.sh` — dockerd wait + clean `compose down && up -d`; role
  compose file is selected by `COMPOSE_ROLE_FILE` at the top of the script.
  The macOS override pairs with EITHER role file — after a promotion, set
  `COMPOSE_ROLE_FILE="docker-compose.primary.yml"` and never run a role file
  on macOS without the override (see `docs/FAILOVER.md` §"macOS/Colima notes")
- `install.sh` — precondition checks (incl. a merged compose-config dry run,
  which also pins compose ≥ 2.24 for `!override`) + render + lint + install

Logs under `/opt/hrserv/logs/` grow unbounded by default; if the box runs
for months, add a rotation entry, e.g. `/etc/newsyslog.d/hrserv.conf`:
`/opt/hrserv/logs/*.log <operator>:staff 644 5 1024 * J`

CI validates these statically (`tests/test_deploy_artifacts.py`): plists
parse and reference scripts that exist, scripts pass `bash -n`, the compose
override actually overrides the tailnet bind. The reboot drill is the real
integration test.
