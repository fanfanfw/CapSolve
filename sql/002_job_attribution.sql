ALTER TABLE budi95_jobs ADD COLUMN IF NOT EXISTS api_client_id VARCHAR(32);
ALTER TABLE budi95_jobs ADD COLUMN IF NOT EXISTS api_credential_id VARCHAR(32);
UPDATE budi95_jobs SET api_client_id = 'legacy' WHERE api_client_id IS NULL;
UPDATE budi95_jobs SET api_credential_id = 'legacy' WHERE api_credential_id IS NULL;
ALTER TABLE budi95_jobs ALTER COLUMN api_client_id SET DEFAULT 'legacy';
ALTER TABLE budi95_jobs ALTER COLUMN api_credential_id SET DEFAULT 'legacy';
ALTER TABLE budi95_jobs ALTER COLUMN api_client_id SET NOT NULL;
ALTER TABLE budi95_jobs ALTER COLUMN api_credential_id SET NOT NULL;
DO $$ BEGIN
  ALTER TABLE budi95_jobs ADD CONSTRAINT budi95_jobs_api_client_id_check CHECK (api_client_id ~ '^[a-z0-9][a-z0-9_-]{0,31}$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE budi95_jobs ADD CONSTRAINT budi95_jobs_api_credential_id_check CHECK (api_credential_id ~ '^[a-z0-9][a-z0-9_-]{0,31}$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_budi95_jobs_client_status ON budi95_jobs (api_client_id, status);
