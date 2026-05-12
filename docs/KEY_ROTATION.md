# API key rotation

Procedure for rotating an HRServ API key without breaking uploads. The
key in question is the `flask-frontend` argon2 hash row in `api_keys` on
HRServ, and the corresponding plaintext in Render's
`HRFUNC_API_KEY_HRSERV` env var.

This is the single trickiest cross-repo coordination point in the system,
because the key lives in BOTH HRServ's Postgres AND Render's env, and
they must agree byte-for-byte. The whole Phase 2b rollout debug was
because Render's value didn't match what was in HRServ (it was the
Cloudflare Access Client Secret pasted into the wrong slot).

## When to rotate

- **Suspected compromise** — secret in logs, screen-share leak, lost
  device, etc. Rotate immediately.
- **Quarterly hygiene** — no specific signal, just good practice.
- **Operator handoff** — when a new operator joins or an old one leaves.

## Rotation strategies — overlap vs cutover

There are two ways to do this. Pick based on traffic volume + risk tolerance:

### Overlap rotation (safer; recommended)

Both old and new keys are active for ~minutes. Frontend can use either.
No upload window where requests fail.

1. **Mint a new key** with a distinct label:
   ```bash
   # On hrserv-1:
   cd /opt/hrserv
   dc exec hrserv hrserv-mint-key --label flask-frontend-2026-05
   # Captures plaintext exactly ONCE. Save to password manager immediately
   # under a label like HRSERV_API_KEY_2026_05.
   ```

2. **Update Render env var.** On hrfunc-web's Render dashboard →
   Environment → `HRFUNC_API_KEY_HRSERV` → paste the new plaintext (NOT
   the old one). Save → wait for auto-redeploy → "Deploy live".

3. **Verify the new key works**, from your Mac:
   ```bash
   # Source smoke.env after updating HRSERV_API_KEY to the new plaintext:
   source ~/.hrserv/smoke.env
   curl -sSi --resolve api.hrfunc.org:443:172.67.188.154 \
       -X POST https://api.hrfunc.org/upload_json \
       -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
       -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
       -H "x-api-key: $HRSERV_API_KEY" \
       -F "jsonFile=@/Users/dennyschaedig/Scripts/HRServ/tests/fixtures/sample_hrf.json"
   ```
   Expect 200 with `"ok":true`.

4. **Confirm a real frontend upload uses the new key** — upload through
   hrfunc.org and check Render's logs for `shadow_write status_match=true`.

5. **Revoke the old key** on hrserv-1, ~10 minutes after the new key was
   confirmed working (in case any in-flight retries use the old key):
   ```bash
   dc exec postgres psql -U hrserv -d hrserv -c \
       "UPDATE api_keys SET revoked_at = now() WHERE id = 'flask-frontend';"
   ```

6. **Verify the old key now 401s** with the same curl as Step 3 but with
   the old plaintext. Should return `401 Invalid API key`.

7. **Delete the old plaintext from your password manager** to avoid
   future paste mistakes. Move the new one to occupy the canonical
   `HRSERV_API_KEY` slot.

### Cutover rotation (avoid — listed for completeness)

There is no clean "same-label, replace-in-place" rotation path with the
current schema. Two reasons it fails:

1. **`api_keys.id TEXT PRIMARY KEY` + UNIQUE on `key_hash`** means
   `mint_key.py` rejects a re-mint with the same label.
2. **The FK `hrf_submissions.api_key_id REFERENCES api_keys(id)`** has
   no `ON DELETE` clause. As soon as the first submission lands tagged
   with `flask-frontend`, you CANNOT DELETE that row — Postgres will
   refuse the DELETE with a foreign-key violation.

So even if you stop the frontend, mint a new key with the same label
fails. Use the overlap rotation above. If you absolutely must keep the
exact label string `flask-frontend` after rotation, the only path is:

1. ALTER TABLE drop the FK, rename `flask-frontend` to
   `flask-frontend-archived-2026-05`, re-add the FK, mint a fresh
   `flask-frontend`, update Render. This is risky and not recommended.
2. OR add an `expires_at` column (per FOLLOWUPS.md "API key rotation
   needs `expires_at`") and rotate with overlapping validity windows —
   but that's a schema change that needs its own design + migration.

In practice, **use the overlap rotation** with timestamp-suffixed
labels (`flask-frontend-2026-05`, `flask-frontend-2026-11`, …). The
historical label is preserved for audit; only Render's env var changes.

## Verifying values match byte-for-byte (when in doubt)

The Phase 2b debug specifically needed this. Use the Render Shell tab to
inspect what the running worker actually sees:

```bash
# In Render Shell on hrfunc-web:
echo "len=${#HRFUNC_API_KEY_HRSERV} hash=$(printf '%s' "$HRFUNC_API_KEY_HRSERV" | sha256sum | head -c 16)"
```

```bash
# On your Mac with the password manager value loaded:
source ~/.hrserv/smoke.env
echo "len=${#HRSERV_API_KEY} hash=$(printf '%s' "$HRSERV_API_KEY" | shasum -a 256 | head -c 16)"
```

If `len` and the 16-char hash prefix match, the bytes are identical.
Mismatch = re-paste from your password manager via shell to avoid
trailing-whitespace from GUI paste:
```bash
printf '%s' "$HRSERV_API_KEY" | pbcopy   # exact bytes, no trailing newline
```
Then paste into Render's env-var field.

## What can go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Render Shell shows `len=64` | You pasted CF Access Client Secret (typically 64 hex chars) | Paste the actual API key (43 URL-safe chars from token_urlsafe(32)) |
| `len=43` matches but hash differs | Different secret (typo, wrong line copied, old value) | Re-paste from password manager via shell |
| `len=44` instead of 43 | Trailing newline from paste | Use `printf '%s' | pbcopy` to copy without newline |
| Render Shell var is unset (empty) | Wrong env-var name (e.g., `HRFUNC_HRSERV_API_KEY`) | Fix the name in Render Environment tab — must be `HRFUNC_API_KEY_HRSERV` exactly |
| `hrserv-mint-key` fails with UNIQUE violation | Label already exists | Use a different label (e.g., add a date suffix) |
| Frontend uploads but HRServ shows nothing in `hrf_submissions` | Either shadow isn't firing or it's 401'ing — check Render logs for `shadow_write` / `shadow_divergence` | Most often: env-var value mismatch (this doc) |

## Future improvement

The current design has a single key (`flask-frontend`) and the rotation
ergonomics rely on changing the label on every rotation. Better long-term:
support multiple active keys with overlapping validity windows via
`expires_at` column (not currently in schema). Tracked in `FOLLOWUPS.md`.
