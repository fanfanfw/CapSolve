CREATE TABLE IF NOT EXISTS budi95_jobs (
  id BIGSERIAL PRIMARY KEY,
  ulid VARCHAR(32) NOT NULL UNIQUE,
  nric VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  response_status_code INTEGER,
  response_body JSONB,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  processed_at TIMESTAMPTZ,
  CONSTRAINT budi95_jobs_status_check CHECK (status IN ('pending', 'processing', 'success', 'failed')),
  CONSTRAINT budi95_jobs_api_client_id_check CHECK (api_client_id ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
  CONSTRAINT budi95_jobs_api_credential_id_check CHECK (api_credential_id ~ '^[a-z0-9][a-z0-9_-]{0,31}$')
);

CREATE INDEX IF NOT EXISTS idx_budi95_jobs_status_created_at
ON budi95_jobs (status, created_at);

CREATE INDEX IF NOT EXISTS idx_budi95_jobs_ulid
ON budi95_jobs (ulid);

CREATE INDEX IF NOT EXISTS idx_budi95_jobs_nric_created_at
ON budi95_jobs (nric, created_at DESC);

