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

**Where:** `scripts/promote_replica.sh`, `docker/docker-compose.replica.yml`

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
