# HRServ — deferred follow-ups

Issues surfaced during the bootstrap pre-push review that we intentionally
shipped instead of fixing immediately. Each one has an explicit "do this
when…" resolution moment so they don't slip.

The CLAUDE.md rule is no band-aids without a planned fix moment — this file
*is* the planned-fix-moment record. Anything not listed here should be
fixed-at-root, not deferred.

## Auth: timing side-channel on key verification

**Where:** `hrserv/auth.py:_verify_against_all`

**Symptom:** The loop iterates every active key (constant iteration count),
but argon2 verify takes ~100ms on a match vs ~microseconds on a mismatch. A
patient attacker measuring response latency could detect that *some* key
matched, and roughly estimate position in the iteration order.

**Why deferred:** With one active API key in the MVP, there's no position to
leak. The fix (run argon2 against a dummy hash on every non-match) doubles
the auth latency for legitimate requests and adds complexity that isn't
warranted yet.

**Resolve when:** the third active API key is minted, OR when read endpoints
ship and authenticate from a wider client population. Either trigger means
the "tiny candidate set" argument no longer applies.

**Fix sketch:** keep a precomputed dummy argon2 hash; run verify against it
on every non-match so total wall time is `N × full_verify` regardless of which
slot (if any) matched.

## cloudflared.yml drift policy

**Where:** `docker/cloudflared.yml`

**Symptom:** The file is informational — the real config lives in the
Cloudflare dashboard. A future operator editing the file and not the
dashboard sees no effect; the reverse drift is also silent.

**Why deferred:** No automated way to diff against the dashboard without an
API token in CI, which is out of scope for the bootstrap.

**Resolve when:** Phase 3 cutover, or earlier if a real drift incident
happens. The fix is either (a) write a script that pulls the current
dashboard config and diffs it against the committed file, or (b) accept the
file as documentation-only and rename it `cloudflared.example.yml`.

## Compose-file role coupling on failover

**Where:** `scripts/promote_replica.sh`, `docker/docker-compose.replica.yml`,
`deploy/hrserv.service` (hardcodes the primary file), and — as of 2026-07-15 —
`deploy/launchd/bin/hrserv-up.sh` (`COMPOSE_ROLE_FILE`, defaults to replica).
A node's role is now encoded in four places; the eventual fix should
converge them on one source of truth (e.g. a `docker/.role` file or `.env`
var read by both boot chains and the promote script). Note the macOS flip
mechanism is "edit a git-tracked script in the working tree" — a later
`git pull`/checkout can silently revert it and demote the node on next boot.

**Symptom:** After `promote_replica.sh --confirm`, the newly-promoted node is
running services from `docker-compose.replica.yml` with `NODE_ROLE=primary`.
Operationally this works (env override is honored), but it's confusing during
a real incident — the compose file name says "replica" while the role is
primary.

**Why deferred:** Swapping the compose file at promotion time requires
templating an `.env` and a clean teardown/up cycle, which is more incident
risk than a confusing-but-functional state. The runbook in
`docs/FAILOVER.md` step 5 calls out the eventual transition.

**Resolve when:** writing the post-failover restoration playbook in earnest,
OR when actual failover automation lands (post-MVP per BOOTSTRAP.md).

**Fix sketch:** extend `promote_replica.sh` to additionally `docker compose
-f docker-compose.replica.yml down`, then `up` with `docker-compose.primary.yml`
after the promotion. Requires keeping the postgres data volume in place
across the down/up.

## Content-Length early short-circuit isn't exercised by tests

**Where:** `hrserv/routes/ingest.py` Step 3 (early check) and the post-read
check just below

**Symptom:** `test_oversize_content_length_returns_413` passes via the
post-read length check, not the early Content-Length header check, because
httpx overwrites Content-Length with the real body size before sending. So
the early-short-circuit code path is unit-untested.

**Why deferred:** The behavior is provable by reading the code; testing it
properly requires either an HTTP client that lets us spoof headers
post-encoding (none of httpx's documented APIs do) or a raw-socket integration
test (overkill for MVP).

**Resolve when:** any future bug suggests the size guards aren't firing in
the order documented. Until then, the post-read guard is the actually-load-
bearing check and is well-tested.

## Postgres bridge IP range broadened beyond docker default

**Where:** `docker/postgres/pg_hba.conf`

**Symptom:** The `hrserv` app-user rule allows `172.16.0.0/12`. Docker's
default bridge gateway picks an address in that range, but some custom
networks could land outside it.

**Why deferred:** Narrower rules require knowing the exact compose network
CIDR, which Docker assigns dynamically. Tightening would require pinning the
network CIDR in compose, which constrains hosting flexibility.

**Resolve when:** the compose stack picks an explicit CIDR for production, or
if a real "hrserv app can't reach postgres" incident reveals a different
range. Both are easy to fix in pg_hba.conf after the fact.

---

# Added 2026-05-12 — surfaced by Phase 2b parallel reviews

## External monitoring / alerting on /healthz

**Symptom:** No external poller is watching `https://api.hrfunc.org/healthz`.
If HRServ dies overnight, no one knows until a user tries to upload.

**Why deferred:** Not blocking Phase 2b shadow window since legacy backend
is still authoritative. But CRITICAL before cutover.

**Resolve when:** before flipping `HRFUNC_UPLOAD_URL` on Render to point at
HRServ (per `docs/SHADOW_WINDOW.md` cutover criteria). Setup is ~5 minutes
of UptimeRobot or BetterStack — see `docs/MONITORING.md`.

## Backups not yet wired (DR exposure)

**Where:** Full Phase 2a deployment.

**Symptom:** Single disk on jib-jab. No B2, no cross-ship to a peer
(no peer exists yet), no nightly cron. Disk failure = total loss of
post-shadow-cutover data.

**Why deferred:** Phase 2a was the receiver-only milestone. Backups are
explicitly part of Phase 2c per BOOTSTRAP.md.

**Resolve when:** Phase 2c — `docs/BACKUP_RESTORE.md` describes the target
state. Restore drill is mandatory before cutover.

## Tailscale key expiry on hrserv-1 (deadline 2026-11-07)

**Where:** Tailscale admin console for `jib-jab` machine.

**Symptom:** Default 180-day key expiry will drop the box from the tailnet
~2026-11-07. Postgres becomes unreachable from any future replica;
cross-node backup ship breaks silently.

**Why deferred:** Easy to fix (single click in tailnet admin); easy to
forget. Tracking here so we don't.

**Resolve when:** before 2026-08-01 (well ahead of expiry). Admin console
→ machines → jib-jab → "Disable key expiry". Repeat for hrserv-2 when it
joins.

## Render dashboard config drift

**Where:** Cloudflare Access apps + tunnel + DNS records, plus Render
env vars. All live outside the repo.

**Symptom:** Phase 2b debug revealed two specific cases: (a) Access apps
shipped with 0 policies attached → 302-redirect-to-login for all traffic,
(b) `HRFUNC_API_KEY_HRSERV` env var contained the wrong value (the CF
Access Client Secret pasted into the wrong slot) → 401 on every shadow
forward. Both invisible until someone uploaded.

**Why deferred:** No code path can detect dashboard drift directly. The
startup warning in hrfunc-web catches the missing-key case; the rest
requires manual audit.

**Resolve when:** quarterly. Add to operator calendar: open Cloudflare
dashboard, verify both Access apps show "Policies assigned: 1"; verify
tunnel UUID matches; verify Render env vars by name (don't need values).

## Connection-pool exhaustion under burst

**Where:** `hrserv/config.py:db_pool_max_size=8` + `pool.acquire()` blocks
indefinitely.

**Symptom:** A burst of >8 concurrent uploads queues on the pool. With
authenticate() holding a connection during argon2 verify (~100ms × N keys)
+ insert_submission holding another, request latency spikes and the
frontend's 10s timeout fires before HRServ responds. Shadow forwards land
with `primary_status=None`. Divergence guaranteed.

**Why deferred:** Current traffic is researcher uploads (sparse, not bursty).
Real risk only if HRServ becomes the primary AND a bulk-upload tool ever
emerges.

**Resolve when:** before adding any feature that could generate bursty
traffic (e.g., a "submit batch of HRFs" UI). Fixes: bump pool max, add
`pool.acquire(timeout=5)` so requests fail fast instead of piling up,
release the connection between auth and insert.

## JSON-parse recursion DoS

**Where:** `hrserv/routes/ingest.py:104` (`json.loads`) and
`hrfunc-web/app.py:json.loads`.

**Symptom:** Python's stdlib `json` is recursive and crashes with
`RecursionError` at ~1000 levels of nesting. A 5MB payload of `[[[[...`
exceeds that. A worker crash kills in-flight requests on that worker.

**Why deferred:** Requires intentional crafted upload; not seen in real
traffic; 5MB cap limits memory damage. Real risk only if HRServ becomes
publicly addressable without Cloudflare Access (it isn't).

**Resolve when:** before opening `/upload_json` to unauthenticated traffic
(never planned). Fix: switch to `simplejson` or pre-scan the payload
text for bracket-depth before calling `json.loads`.

## hrfunc-web 5MB limit checks wrong byte sequence

**Where:** `hrfunc-web/app.py` upload_json route, after augmentation step.

**Symptom:** The 5MB check at `app.py:407` compares `len(original_bytes)`,
but the bytes actually forwarded are `augmented_bytes` (which includes the
`_hrf_submission` envelope and form fields). User uploads a 4.9MB file +
fills out the form → augmented payload exceeds 5MB → HRServ returns 413
→ user sees "Upload failed: Payload too large" after filling out a long
form. Data is not corrupted but UX is bad.

**Why deferred:** Edge case — requires near-5MB file. Not user-impacting
at typical HRF sizes (~kilobytes per channel × ~50 channels = sub-MB).

**Resolve when:** the first time a real user hits this. Fix: check
`len(augmented_bytes)` instead, and reject before forwarding.

## Postgres role over-privileged

**Where:** `docker/postgres/initdb/01-create-roles.sh:19-20`.

**Symptom:** The `hrserv` app role has `ALL PRIVILEGES ON DATABASE` +
`CREATE ON SCHEMA public`, meaning the app could DROP its own tables if
the code ever issued such a statement.

**Why deferred:** No SQL-injection surface today (everything is parameterized
via asyncpg). Principle-of-least-privilege violation but not currently
reachable.

**Resolve when:** before public read endpoints expose any user-controlled
query parameter. Fix: split into `hrserv_owner` (for migrations) and
`hrserv_app` (only `SELECT, INSERT, UPDATE` on specific tables).

## No CSRF token on /upload_json (frontend)

**Where:** `hrfunc-web/templates/hrf_upload.html` + `app.py /upload_json`.

**Symptom:** Any cross-origin page can autosubmit a multipart form to
hrfunc.org's upload endpoint via a victim's browser. Impact is bounded —
the action requires a valid `x-api-key` (server-side env var, not user-
provided) but does NOT require user credentials. So an attacker can
*trigger* an upload with arbitrary content (including spam to
`send_confirmation_email`).

**Why deferred:** Low real impact (upload action is "submit research
data", not "delete account"). 5s session rate-limit mitigates spam.

**Resolve when:** any real abuse is observed, or before the frontend gains
authenticated user accounts. Fix: Flask-WTF CSRFProtect.

## Mac local DNS resolver caches NXDOMAIN aggressively

**Where:** Operator's Mac, not server-side.

**Symptom:** Home router DNS caches the pre-Cloudflare NXDOMAIN for
api.hrfunc.org for ~15-60 minutes. Even after Cloudflare DNS is correct,
`dig api.hrfunc.org` from the Mac returns no answer. Workaround used
during Phase 2a:
`curl --resolve api.hrfunc.org:443:172.67.X.Y https://api.hrfunc.org/...`

**Why deferred:** Operator-side workstation issue, not server. Production
frontend traffic from Render uses public DNS, not affected.

**Resolve when:** Whenever the operator gets annoyed enough to point Mac's
network preferences at 1.1.1.1 instead of the home router. Documented in
`docs/PHASE_2A_HRSERV1_SETUP.md` Step 9 for future operators.

## API key rotation needs `expires_at` for clean overlap

**Where:** `migrations/0001_init.sql` (`api_keys` table), `mint_key.py`.

**Symptom:** Current rotation requires either mint-with-new-label (so
both old and new exist temporarily) or delete-and-mint (with a downtime
window). No clean `expires_at` column to express "this key is valid until
X, then disable automatically."

**Why deferred:** Single key in production today; manual rotation per
`docs/KEY_ROTATION.md` works. Real need emerges when multiple frontends
(e.g., a future API gateway) each have their own key.

**Resolve when:** second `api_keys` row gets minted. Fix: add
`expires_at TIMESTAMPTZ NULL` to schema (migration 0002), update
`list_active_api_keys` to filter `WHERE expires_at IS NULL OR expires_at
> now()`, update mint_key to accept `--expires-at`.

## WAL retention via `wal_keep_size` is not a replication slot

**Where:** `docker/postgres/primary.conf:21` (`wal_keep_size = 1GB`).

**Symptom:** When the test replica or Mac Mini eventually joins after a
long offline gap, total WAL generated > 1GB → pg_basebackup fails with
"requested WAL segment has already been removed."

**Why deferred:** No replica exists yet to retain WAL for.

**Resolve when:** Phase 2c step 5 (per `docs/NEW_NODE_SETUP.md`) — create
a named replication slot via `pg_create_physical_replication_slot()` so
WAL is retained indefinitely. Monitor `pg_replication_slots.active` to
catch the inverse risk (disappeared replica + unbounded WAL growth).

## Shadow latency not logged

**Where:** `hrfunc-web/app.py:_shadow_forward`.

**Symptom:** Each shadow log line records statuses but not elapsed
milliseconds for either backend. Without latency comparison, cutover
safety can't include "HRServ p95 is within X of legacy."

**Why deferred:** Not blocking shadow validation — status comparison is
enough to determine correctness. But adds confidence at cutover time.

**Resolve when:** before cutover, OR during any shadow-window analysis
where divergence patterns suggest a timing issue. Fix: 4 lines in
`_shadow_forward` to wrap each request in `time.monotonic()` deltas and
include `primary_ms=` / `shadow_ms=` in the log line.

## Legacy host Postgres 17 data dir still present

**Where:** `/var/lib/postgresql/17/` on jib-jab.

**Symptom:** Pre-HRServ Postgres 17 install was stopped + disabled during
Phase 2a Step 0a; data dir left on disk (~tens of MB). Safety dump at
`/var/backups/legacy-hrfuncdb/`. Not blocking but consumes disk.

**Why deferred:** Cheap to leave; protects against an "oh wait we did
need that" moment. Cost is ~30 MB.

**Resolve when:** After Phase 2c is stable and the legacy backend is
genuinely retired. Delete with `sudo apt-get purge postgresql-17` + clear
data dir. Verify safety dump still exists at
`/var/backups/legacy-hrfuncdb/` first.

## Dead `rmdig` nginx upstream block on jib-jab

**Where:** nginx config on jib-jab (currently outside this repo).

**Symptom:** A `proxy_pass http://unix:/run/rmdig/rmdig.sock/` upstream
block exists for a defunct project. Doesn't affect HRServ — purely
janitorial.

**Why deferred:** Cleaning up the nginx config touches the old Flask
service too; safer to wait until cutover when we'd retire the whole
Flask block anyway.

**Resolve when:** Post-cutover when the old Flask service is retired and
nginx config gets cleaned up in one sweep.

---

# Added 2026-05-15 — surfaced by the reboot-resilience root-cause review

## Re-enable multi-worker uvicorn when user count justifies it

**Where:** `docker/Dockerfile` CMD — currently `--workers 1`.

**Symptom (today, none):** We're running a single uvicorn worker per the
reboot-resilience fix. uvicorn's two-worker master had known partial-
startup edge cases (one worker's lifespan succeeds while another's fails
with no clear logs), and HRF upload volume is tiny — sparse academic
submissions, not bursty traffic. Single worker is more deterministic and
easier to debug.

**Why deferred:** Performance is a non-issue at today's load. The point
of `--workers 1` is *startup determinism*, not throughput. We pay the
performance cost only if traffic actually grows beyond one worker's
capacity.

**Resolve when:** ANY of the following triggers fire:

1. **Sustained request rate exceeds ~5/sec** for more than a minute.
   Check via `dc logs hrserv | grep -c "POST /upload_json" && date` over
   a window. At our latency (~200ms per request including argon2 verify
   + Postgres roundtrip), 5 req/sec saturates a single worker.
2. **A bulk-upload feature is introduced** (e.g., a "submit batch of HRFs"
   UI in hrfunc-web, or scripted ingestion from a research collaborator
   processing many sessions). Any feature that could plausibly generate
   parallel uploads is a trigger.
3. **HRServ becomes the entry point for read endpoints** (per
   BOOTSTRAP.md "future read endpoints"). Read traffic patterns are
   typically more concurrent than write; multiple workers become useful.
4. **You observe `slow_request` patterns** in logs where a long-running
   request blocks subsequent ones — that's worker exhaustion, and the
   fix is more workers (or async-only request handling end-to-end).

**Fix sketch when the trigger fires:**

- Update `docker/Dockerfile`: switch `--workers 1` → `--workers 2` (or
  more, but probably no more than `2 * CPU_cores + 1` per gunicorn
  conventions; on jib-jab that's likely 4-8).
- Either pin uvicorn to a version with clean multi-worker lifespan
  semantics, OR switch to gunicorn + uvicorn workers (industry standard
  for prod Python ASGI). Gunicorn handles worker lifecycle more
  predictably.
  - If switching to gunicorn: add `gunicorn` to `pyproject.toml`
    dependencies, change CMD to
    `gunicorn hrserv.main:create_app --factory --bind 0.0.0.0:8000
    --worker-class uvicorn.workers.UvicornWorker --workers 4
    --timeout 60 --graceful-timeout 30`.
- Test under sustained load before merge (locally with `hey` or `wrk`
  against a docker-compose.test setup).
- Make sure the connection pool's `max_size` is bumped proportionally:
  `db_pool_max_size` should be >= `workers * 4` so each worker has
  multiple connections available without contention.
- Re-test the boot-resilience scenario — the multi-worker lifespan race
  could come back. Verify with an intentional Postgres-not-ready start.

**Note on `--reload` and dev:** None of this affects local development.
`uv run uvicorn hrserv.main:create_app --factory --reload` is single-
worker anyway. The change is production-only.

## Boot-resilience: monitor for "all retries exhausted" log line

**Where:** `hrserv/db.py` `create_pool_with_retry`.

**Symptom (today, none):** The retry loop logs WARNING per failed
attempt and ERROR if it exhausts all retries before raising. If the
ERROR ever fires in production, it means create_pool failed for ~2
minutes straight — which is a real outage worth alerting on.

**Why deferred:** No external alerting / log-monitoring beyond
UptimeRobot today. UptimeRobot will catch the symptom (`/healthz` down
for ~2 min) but won't tell us why.

**Resolve when:** Setting up structured log shipping / alerting (Phase
2c or later). Add a specific alert rule for the ERROR log line:
`grep -E "create_pool failed after.*attempts"`.

## Mac Mini (hrserv-2) launchd boot orchestration

**Where:** The Mac Mini (`big-mac-mini`, arrived July 2026, Phase 2c+).

**Status 2026-07-15 (evening):** Live in production — big-mac-mini runs the
boot chain as the PRIMARY after the same-day cutover. Reboot drills passed
in replica role and (post self-heal fix) in primary role; nine distinct
bring-up failures were caught and fixed at the root (buildx, port
co-tenancy, VM mounts, YAML-null tilde, pg_hba placeholder, initdb env +
wiring, stale Lima state, LIMA_HOME). Wanted before closing: one more
consecutive hands-free pass on the final (primary) configuration.

Original draft scope: `deploy/launchd/` (README, two
LaunchDaemon plists, boot scripts, installer) plus
`docker/docker-compose.macos.yml` and `docs/NEW_NODE_SETUP.md` Step 9.5-mac.
Design differs from the original sketch: Colima instead of Docker Desktop
(no GUI login session needed), a poll-for-specific-tailnet-IP loop inside
the colima daemon's wrapper script instead of a separate
wait-for-tailscale plist (launchd has no unit ordering, so a second plist
couldn't gate the first anyway), and a loopback Postgres bind because the
tailnet IP doesn't exist inside Colima's VM.

**Why deferred (original):** The current boot chain is Debian/Linux-specific —
`hrserv.service` (systemd unit), `wait-for-tailscale.conf` (systemd
drop-in), and `chmod -x /etc/wpa_supplicant/ifupdown.sh` (Debian
ifupdown). macOS uses launchd and doesn't have any of those.

**Resolve when:** the reboot drill in `deploy/launchd/README.md` §Verify
passes twice consecutively on the Mac Mini. Until then the draft is
UNVALIDATED on real hardware. Two more things are explicitly deferred to
promotion time (both listed in `docs/FAILOVER.md` §"macOS/Colima notes"):
the `tailscale serve` + pg_hba trade-off documented in
`deploy/launchd/README.md` §"Postgres over the tailnet on macOS", and the
backup.sh macOS port (separate entry below).

## cloudflared boot race audit

**Where:** `docker/docker-compose.primary.yml` cloudflared container.

**Symptom (today, none observed):** Cloudflared dials out over the
host's default route (not Tailscale), so it doesn't share the
tailscale-IP-not-yet-assigned race. But it `depends_on: hrserv` in
compose, which is up-time-only ordering — if hrserv is slow to come up
and cloudflared starts in parallel, cloudflared retries the upstream
connection until hrserv is healthy. Worth verifying this retry loop is
real and doesn't have an inverse race that would manifest only at boot.

**Why deferred:** No observed failure mode. The wait-for-tailscale fix
on docker.service applies to the daemon, so cloudflared also benefits
from waiting for tailscale before starting — but the question is
specifically about hrserv-becoming-healthy timing for cloudflared.

**Resolve when:** Investigate after the next clean reboot — run
`docker compose logs cloudflared --since 2m` and look for any
"connection refused" / "no such host" lines during boot.

## Documentation discoverability: index in README

**Where:** `README.md` (project root).

**Why deferred:** The repo has accumulated ~10 docs in `docs/` and they
aren't indexed from the README. A future operator landing on the repo
fresh wouldn't know `docs/NETWORK_TROUBLESHOOTING.md` exists.

**What to add:** A short "Documentation" section in README.md linking
to each doc with a one-line description. Cross-link from CLAUDE.md too.

**Resolve when:** Any tidy-up PR. Low-priority since current operators
(just Denny) know where things are.

**Resolved 2026-07-15:** README's Quick reference table refreshed to cover
every doc (added the launchd README and NETWORK_TROUBLESHOOTING, removed
stale status annotations) as part of the multi-OS setup docs pass.

## Tailscale key expiry calendar reminder

**Where:** jib-jab's Tailscale node key, deadline ~2026-11-07.

**Symptom if expired:** `tailscale wait` (now installed via the
docker.service drop-in) blocks until `TimeoutStartSec=120` fires,
docker.service fails, hrserv.service fails, no HRServ until manual
intervention. The 120s bound makes this a clean failure (not infinite
hang) but it's still an outage.

**Why deferred:** Calendar item, not code. Already in
`project_jib_jab_2026_05_15_outage.md` memory but worth tracking here
too.

**Resolve when:** Calendar reminder for 2026-10-15 to either:
1. Disable key expiry in Tailscale admin (preferred for servers), or
2. Manually re-auth jib-jab via `sudo tailscale up`.

---

# Added 2026-07-15 — surfaced by the macOS boot-chain parallel review

## promote_replica.sh cannot actually promote

**Where:** `scripts/promote_replica.sh:82` and `:88`, plus the literal
`NODE_ROLE:` values in both role compose files.

**Symptom:** Two independent breaks, both platform-agnostic and verified
against the code: (a) `docker compose exec -T postgres pg_ctl promote`
executes as root inside the postgres image (no `USER` directive; the
entrypoint drops privileges only for the server process), and `pg_ctl`
refuses to run as root — `set -euo pipefail` aborts the script before
promotion. (b) `NODE_ROLE=primary docker compose ... up -d --no-deps hrserv`
is a no-op: `docker-compose.replica.yml` hardcodes `NODE_ROLE: replica` as a
literal, shell env only affects `${...}` interpolation, so compose sees an
unchanged config and doesn't recreate the container. Net effect of running
the runbook: Postgres never promotes; even if (a) is fixed by hand, hrserv
keeps returning 503 on `/upload_json` while `/healthz` stays green — an
invisible write outage exactly when a failover is in progress.

**Why deferred:** Found 2026-07-15 during the macOS boot-chain review;
fixing it properly is its own branch (compose files move to
`NODE_ROLE: ${NODE_ROLE:-replica}`-style interpolation or the script swaps
role files; `exec -u postgres`; plus tests — the promotion contract
currently has none, violating the tests-for-promised-behavior rule).
`docs/FAILOVER.md` carries a KNOWN ISSUES banner pointing here so nobody
trusts the runbook in the meantime.

**Resolve when:** BEFORE the Mac Mini is promoted to primary (planned Phase
D of the July 2026 migration). Hard blocker for any real failover.

## backup.sh is Linux-only

**Where:** `scripts/backup.sh` (`shred -u`; `/var/backups/hrserv` and
`/var/log` paths; cron scheduling assumption).

**Symptom:** If run on a macOS primary as-is: `shred` doesn't exist on
macOS, so `set -euo pipefail` aborts mid-script — after the plaintext dump
is written but before the rsync (peer) and restic (B2) legs. From cron
that's zero off-site copies, silently, plus an un-shredded plaintext dump
left on disk every night.

**Why deferred:** The Mini is a replica for now; backups continue to run on
hrserv-1 (Linux), where the script works. Receiving cross-shipped dumps on
the Mini needs no porting (Remote Login + a writable `PEER_DIR` — see
NEW_NODE_SETUP Step 12).

**Resolve when:** before the Mini becomes primary (same trigger as the
promote_replica.sh entry). Port sketch: replace `shred -u` with `rm -P` (or
brew `gshred`), move paths to operator-writable locations, schedule via
launchd `StartCalendarInterval` instead of cron, then re-run the
`BACKUP_RESTORE.md` restore drill from the Mac.

## replica.conf's primary_conninfo env-expansion comment is false

**Where:** `docker/postgres/replica.conf:5-6` and the `primary_conninfo`
line below it.

**Symptom:** The comment claims "Postgres 16 supports environment
expansion" — PostgreSQL does not expand env vars in .conf files, on any
version. The `${PRIMARY_TAILSCALE_IP}`/`${REPLICATOR_PASSWORD}` in that
`primary_conninfo` are literal text. Nothing breaks today: the value is
only consulted in standby mode, and the Phase C bootstrap
(`pg_basebackup -R`, NEW_NODE_SETUP Step 6) writes the real conninfo into
`postgresql.auto.conf`, which is read after postgresql.conf and wins. But
if anyone ever removes auto.conf "to clean up," the standby silently tries
to dial a host literally named `${PRIMARY_TAILSCALE_IP}`.

**Why deferred:** Found 2026-07-15 while diagnosing the Mini's pg_hba
crash-loop; fixing it means deciding the intended mechanism (delete the
misleading line and rely on `-R`'s auto.conf, or template the file at
deploy time), which deserves its own moment — not a drive-by during
boot-chain work.

**Resolve when:** Phase C, when the Mini is seeded via `pg_basebackup -R` —
verify where `primary_conninfo` actually ends up, then delete the false
comment and the dead line (or replace with a pointer to auto.conf).
