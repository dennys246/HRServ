# Running and evaluating the shadow validation window

Phase 2b is live: every upload through hrfunc.org dual-writes to both the
legacy `flask.jib-jab.org` backend (still authoritative) AND HRServ. The
shadow window is the period during which we watch for divergence between
the two before flipping HRServ to authoritative.

This doc covers what to monitor, how to triage divergence, and how to
decide when it's safe to cut over.

## What you're trying to learn

Cutover is safe when:

1. Every recent upload that succeeded against legacy ALSO succeeded
   against HRServ (`shadow_write ... status_match=true`).
2. No `shadow_divergence` lines have appeared for at least N consecutive
   days where N >= 7.
3. HRServ has tolerable latency (within ~2× legacy on the p95).
4. HRServ stack hasn't crashed or restarted during the window.
5. (Optional but worth it) A handful of edge-case payloads have been
   uploaded through hrfunc.org — unusual filenames, large valid HRFs,
   weird-but-valid envelope fields — and all of them shadow-matched.

## Where the signal lives

| Signal | Where to find it | Tool |
|---|---|---|
| Per-upload shadow status | Render logs, `app.logger.info` lines | Render dashboard → Logs → search `shadow_write` |
| Shadow divergence (status mismatch) | Render logs, `app.logger.warning` | Render → Logs → search `shadow_divergence` |
| Total shadow attempts | Render logs over time window | grep + count |
| Actual rows landed in HRServ | `hrf_submissions` on hrserv-1 | psql query (below) |
| HRServ uptime | hrserv-1 host metrics + container `dc ps` | host-side |
| HRServ request latency | Not currently logged | (gap — see FOLLOWUPS) |

## Manual evaluation, weekly

Once a week during the shadow window:

### 1. Count successes vs divergences in Render logs

In Render dashboard → hrfunc-web → Logs, filter to the last 7 days:

```
shadow_write status_match=true
```

That's the success count. Compare to:

```
shadow_divergence
```

That's the failure count. Healthy ratio is everything in the first
bucket, nothing in the second.

### 2. Count rows in `hrf_submissions`

On hrserv-1:

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT count(*),
            min(uploaded_at) AS first_upload,
            max(uploaded_at) AS latest_upload
     FROM hrf_submissions
     WHERE uploaded_at > now() - interval '7 days';"
```

That count should match (or closely approximate) the
`shadow_write status_match=true` count above. Allow for:
- Duplicate retries (same `stored_filename` returns same `id` → no extra
  row, but each retry generates a `shadow_write` log line).
- Render log retention dropping older lines (free tier ~7 days).

### 3. Spot-check a few rows

```bash
dc exec postgres psql -U hrserv -d hrserv -c \
    "SELECT id, stored_filename, study, doi, submitter_email,
            size_bytes, length(content_sha256) AS sha_len
     FROM hrf_submissions
     ORDER BY uploaded_at DESC LIMIT 10;"
```

Each row should:
- Have a `study` extracted from `_hrf_submission`
- Have a `submitter_email` extracted
- `sha_len == 64`
- `size_bytes < 5_242_880` (5 MiB cap)

If hot-field columns are NULL on rows where you'd expect a value, the
frontend may be sending a malformed envelope — investigate.

## Divergence triage

If you see `shadow_divergence` lines, classify by `shadow_status`:

### `shadow_status=401`
- **`shadow_body=Invalid API key`** → `HRFUNC_API_KEY_HRSERV` on Render
  doesn't match the `api_keys` row on HRServ. Per `KEY_ROTATION.md`,
  verify byte-equal values via Render Shell, or rotate.
- **`shadow_body=Missing x-api-key header`** → the header isn't being
  sent. Code regression? Should never happen with current
  hrfunc-web/app.py.

### `shadow_status=302`
- Cloudflare Access challenged the request at the edge. The
  `Cf-Access-Client-Id` / `Secret` headers aren't being recognized.
- Most often: the CF Access app's policy got modified, or the service
  token was rotated/revoked.
- Check Cloudflare dashboard → Access → Applications → `hrserv-upload` →
  Policies. Should have 1 policy: Service Auth + Service Token =
  `flask-frontend`.

### `shadow_status=400`
- HRServ rejected the body. The `shadow_body` excerpt should contain a
  human-readable reason: "JSON parse error", "Payload too large", "JSON
  root must be an object or array", "non-standard JSON token", etc.
- Investigate the specific upload — does the legacy backend accept a
  payload HRServ rejects? That's interesting and possibly worth
  loosening HRServ's validation, or worth telling the user why they
  should reformat.

### `shadow_status=413`
- HRServ's 5 MiB cap rejected the augmented payload while legacy
  accepted it. This is the known augmented-size ordering bug — the
  envelope adds non-trivial bytes to a near-5MB file. Worth filing as a
  follow-up if it happens; for now, document for the user.

### `shadow_status=503`
- HRServ returned 503 — either it's running in replica mode
  (`NODE_ROLE=replica`, shouldn't happen on hrserv-1), OR the DB is
  down. Check `dc ps` and recent postgres logs on hrserv-1.

### `shadow_status=5xx` (other)
- Internal HRServ error. Check hrserv logs (`dc logs --tail 200 hrserv`)
  for the actual stack trace.

### `primary_status=None` shadow lines
- Means the legacy backend forward threw a transport exception (network
  blip, DNS issue, etc.) BEFORE returning any status. Shadow fired
  anyway. The user saw "Error contacting API" on the frontend.
- Useful for detecting legacy-backend flakiness.

## When to cut over

Suggested cutover criteria (adjust to your risk tolerance):

- [ ] Shadow window has run for at least **14 days** of real production
      uploads
- [ ] **Zero** `shadow_divergence` lines in the past 7 days
- [ ] Shadow success count for the past 7 days >= 10 real uploads
- [ ] `hrf_submissions` row count matches Render's
      `status_match=true` count for the same period
- [ ] HRServ stack hasn't restarted unexpectedly during the window
- [ ] All hot-field columns are populated on recent rows (envelope
      contract working)
- [ ] At least one edge case tested: a near-5MB upload, an upload with
      special characters in filename, an upload from a different
      researcher than yourself
- [ ] **Backups are now wired** (Phase 2c B2 + cross-ship, restore drill
      passed) — DO NOT cut over without this; pre-cutover the legacy
      backend is your only backup

## Cutover procedure

When the criteria are met:

1. **Lower the legacy backend's WAF / rate-limit** if any — to make sure
   there's no edge that'll surprise you after cutover.
2. **On Render**, change `HRFUNC_UPLOAD_URL` from
   `https://flask.jib-jab.org/upload_json` to
   `https://api.hrfunc.org/upload_json`. Save → wait for redeploy.
3. **Leave `HRFUNC_SHADOW_URL` empty** (or remove the env var entirely)
   — there's no need to shadow anymore.
4. **Update `HRFUNC_API_KEY`** to be the HRServ key (the one that was
   previously `HRFUNC_API_KEY_HRSERV`). Remove `HRFUNC_API_KEY_HRSERV`.
5. **Verify**: upload through hrfunc.org → check HRServ's
   `hrf_submissions` for the new row. The legacy backend should NOT see
   new uploads.
6. **Wait 24h** with shadow inverted (legacy as shadow, HRServ as
   primary). Add `HRFUNC_SHADOW_URL=https://flask.jib-jab.org/upload_json`
   and re-add the legacy key as `HRFUNC_API_KEY_HRSERV` (swapping roles).
   Anything weird? Investigate.
7. **Cut the legacy shadow** by removing `HRFUNC_SHADOW_URL` again. Now
   HRServ is the sole upload target.
8. **Decommission the legacy backend** — stop the gunicorn service, remove
   the nginx upstream block, delete the old `flask.jib-jab.org` Cloudflare
   Tunnel. See `docs/FOLLOWUPS.md` for the items to track.

## Things to file in FOLLOWUPS if the shadow window surfaces them

- Patterns in divergence (specific researchers, browsers, file shapes)
- Latency comparison gaps once they're tracked
- Edge cases not currently in the test suite
- Operational friction (e.g., "had to grep Render logs three times to
  find the divergence reason")

Make every shadow-window finding into either a fix or a tracked
follow-up. The whole point of running this window is to NOT regret
cutover.
