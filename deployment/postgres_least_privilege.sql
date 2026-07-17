\set ON_ERROR_STOP on
\if :{?expected_db}
\else
  \echo 'expected_db is required'
  SELECT 1 / 0;
\endif
\if :{?api_role}
\else
  \echo 'api_role is required'
  SELECT 1 / 0;
\endif
\if :{?worker_role}
\else
  \echo 'worker_role is required'
  SELECT 1 / 0;
\endif
\if :{?purge_role}
\else
  \echo 'purge_role is required'
  SELECT 1 / 0;
\endif

SELECT current_database() = :'expected_db' AS database_matches \gset
\if :database_matches
\else
  \echo 'connected database does not match expected_db'
  SELECT 1 / 0;
\endif

SELECT format($roles$
DO $do$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %1$L) THEN
    EXECUTE format('CREATE ROLE %%I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', %1$L);
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %2$L) THEN
    EXECUTE format('CREATE ROLE %%I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', %2$L);
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %3$L) THEN
    EXECUTE format('CREATE ROLE %%I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', %3$L);
  END IF;
END
$do$;
$roles$, :'api_role', :'worker_role', :'purge_role') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I, %I', :'expected_db', :'api_role', :'worker_role', :'purge_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I, %I, %I', :'api_role', :'worker_role', :'purge_role') \gexec
SELECT format('GRANT SELECT, INSERT ON TABLE public.budi95_jobs TO %I', :'api_role') \gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE public.budi95_jobs_id_seq TO %I', :'api_role') \gexec
SELECT format('GRANT SELECT, UPDATE ON TABLE public.budi95_jobs TO %I', :'worker_role') \gexec
SELECT format('GRANT SELECT, DELETE ON TABLE public.budi95_jobs TO %I', :'purge_role') \gexec

SELECT rolname, rolsuper
FROM pg_roles
WHERE rolname IN (:'api_role', :'worker_role', :'purge_role')
ORDER BY rolname;
