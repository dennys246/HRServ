-- HRServ initial schema.
--
-- Applied to the primary; replicated to standbys via streaming replication.
-- Never run DDL against the replica directly — WAL carries it from the primary.

BEGIN;

CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,
    scopes        TEXT[] NOT NULL DEFAULT '{ingest}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS hrf_submissions (
    id                BIGSERIAL PRIMARY KEY,
    stored_filename   TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    uploaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitter_email   TEXT,
    study             TEXT,
    doi               TEXT,
    api_key_id        TEXT REFERENCES api_keys(id),
    client_ip         INET,
    size_bytes        INTEGER NOT NULL,
    content_sha256    TEXT NOT NULL,
    content           JSONB NOT NULL,
    CHECK (jsonb_typeof(content) IN ('object', 'array'))
);

CREATE INDEX IF NOT EXISTS hrf_submissions_uploaded_at_idx
    ON hrf_submissions (uploaded_at DESC);

CREATE INDEX IF NOT EXISTS hrf_submissions_study_idx
    ON hrf_submissions (study);

CREATE INDEX IF NOT EXISTS hrf_submissions_doi_idx
    ON hrf_submissions (doi)
    WHERE doi IS NOT NULL;

CREATE INDEX IF NOT EXISTS hrf_submissions_submitter_email_idx
    ON hrf_submissions (submitter_email);

-- GIN index on the JSONB content lets future read endpoints filter on arbitrary
-- `content @> '{...}'` patterns efficiently. Pay the write-time cost now so we
-- never have to add it to a large table later.
CREATE INDEX IF NOT EXISTS hrf_submissions_content_gin_idx
    ON hrf_submissions USING GIN (content jsonb_path_ops);

COMMIT;
