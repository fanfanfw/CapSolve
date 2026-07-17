# Phase 0 Baseline and Recovery Evidence

Status: **PENDING OPERATIONAL VERIFICATION** until an operator runs `capture`, completes a restore into a disposable database, and runs `validate --require-restore`. These commands do not restart services, migrate production, or generate traffic.

## Safety contract

- Run from the exact tracked checkout currently deployed. `deployment_commit` is read from `HEAD`; capture rejects all tracked changes and untracked files. Pass the reviewed previous commit as `--rollback-commit`.
- Keep evidence outside the Git checkout on encrypted, access-controlled storage. The tool rejects evidence paths inside this repository and creates files with owner-only permissions.
- Source and restore PostgreSQL access must use separate named libpq services in an operator-managed `pg_service.conf` plus protected password file. No DSN or password is accepted in environment variables, command arguments, logs, or evidence, and the tool never reads `.env`.
- The environment input is parsed only into sorted variable names. Values are neither stored nor printed.
- Configuration copies and the database dump may contain secrets or PII. Never commit them. Retain and dispose of them under the approved operational policy.
- Review source paths before capture. Capture is read-only. Restore is destructive only to the explicitly confirmed disposable restore database.

## Prerequisites

- `python3`, `git`, `psql`, `pg_dump`, and `pg_restore`.
- A source libpq service with read/backup access and permission to call `pg_control_system()`; capture fails closed when stable cluster/database identity cannot be read.
- A distinct named libpq service for a newly created, empty disposable restore database. The tool rejects the source database even through another socket/TCP alias and rejects targets containing non-system objects.
- Read access to the active systemd unit, crontab export, nginx vhost, Cloudflare ingress file, and service environment file.

Export crontab to a protected temporary file without placing it in the repository:

```bash
umask 077
crontab -l > /secure/operator-staging/capsolve.crontab
```

## Capture

Run from the deployed checkout. Provide any available 24-hour metric flags; omitted values remain pending rather than being invented.

```bash
umask 077
python3 deployment/baseline.py capture /secure/capsolve-phase0/2026-07-15 \
  --environment-file /secure/capsolve.env \
  --systemd /etc/systemd/system/capsolve.service \
  --crontab /secure/operator-staging/capsolve.crontab \
  --nginx /etc/nginx/sites-enabled/capsolve \
  --cloudflare /etc/cloudflared/config.yml \
  --source-pgservice capsolve_backup \
  --rollback-commit <reviewed-rollback-commit>
```

Add the available 24-hour metrics. Numeric evidence is limited to finite non-negative values up to `9223372036854775807`:

```text
--submit-count N --success-count N --failed-retry-count N
--median-process-seconds N --peak-memory-bytes N --log-size-bytes N
--oldest-pending-seconds N
```

The evidence directory contains:

- `baseline.json`: deployment/rollback commits, environment variable names, checksums, row count, and optional baseline metrics;
- `configuration/`: restorable copies of systemd, crontab, nginx, and Cloudflare ingress configuration; the manifest records each original/resolved path, every symlink in every pathname component, file owner/group/mode, and each symlink's owner/group/mode;
- `budi95_jobs.schema.sql`: table schema evidence;
- `database.dump`: PostgreSQL custom-format backup.

`baseline.json` intentionally contains no database host/name/user, password, DSN, environment value, NRIC, or other credential. Required configuration paths and numeric ownership metadata are present for rollback; stable database identity is stored only as a SHA-256 digest.

## Restore test

The confirmation flag is mandatory. Source and target identities use a hash of PostgreSQL's stable cluster system identifier plus database OID, so connection-route aliases compare equal. Identity lookup must succeed before any restore. The target must contain no user-created database objects, including schema objects, collations, publications/subscriptions, foreign-data objects, event triggers, extensions, large objects, or default ACLs. Restore does not use `--clean`; it records evidence only when `budi95_jobs` row counts match.

```bash
python3 deployment/baseline.py restore-test /secure/capsolve-phase0/2026-07-15 \
  --restore-pgservice capsolve_phase0_empty \
  --confirm-disposable-database

python3 deployment/baseline.py validate /secure/capsolve-phase0/2026-07-15 \
  --require-restore
```

A passing validation proves checksums are intact, required configuration copies exist, commit fields are valid, environment inventory contains names only, backup restore evidence refers to the captured dump, and source/restored row counts match.

## Rollback use

Rollback remains a reviewed operator action; Phase 0 does not change the server.

1. Stop or divert new submit traffic and stop the worker through the approved maintenance procedure.
2. Verify `baseline.json`, `restore-test.json`, and all checksums with `validate --require-restore`.
3. Check out the exact `rollback_commit` recorded in `baseline.json` using the deployment mechanism.
4. For systemd, nginx, and Cloudflare records, inspect `original_path`, `resolved_path`, and every ordered `symlink_chain` entry. Restore copied content to `resolved_path`, apply numeric `file_uid`, `file_gid`, and `file_mode`, then recreate each symlink with its exact target and recorded numeric metadata (noting that symlink mode is platform-dependent). Install the crontab copy with the native `crontab` command for its recorded numeric owner rather than writing a spool file. Inspect every path and diff before replacement; do not automate this privileged step from unreviewed evidence.
5. Reload/restart only through the approved change procedure, then verify API/result and queue state.
6. Do not delete pending/processing rows. Restore `database.dump` only for an approved database-recovery event, never as routine application rollback.

Until the real capture and disposable restore commands pass, deployment commit, configuration rollback, backup, and restore/row-count acceptance remain **PENDING OPERATIONAL VERIFICATION**. Secret-safe repository artifacts are statically verified; operators must still validate the external evidence before approval.

# Phase 7 PostgreSQL security, backup, and retention

Status: **PENDING OPERATIONAL VERIFICATION**. Repository tests use only a disposable loopback PostgreSQL instance. Do not run these steps against production without inventory, review, approval, and a rollback window.

## Inventory before change and network boundary

1. Inventory every local and remote PostgreSQL consumer, its source address, database, role, owner, and business owner before changing `listen_addresses`, `pg_hba.conf`, or firewall rules.
2. If every consumer is local, use the reviewed `postgresql.conf.example` and require `listen_addresses = 'localhost'`. Otherwise bind only approved RFC1918/ULA private addresses. Never use `*`, `0.0.0.0`, `::`, a public address, or expose TCP/5432 to the internet.
3. Restrict `pg_hba.conf` to exact local/private sources, database, dedicated role, and `scram-sha-256`; reject broad/public CIDRs. Verify TLS when traffic leaves the host.
4. Apply host and upstream network firewall rules denying public 5432, then verify externally from an approved test point. Repository static checks cannot prove the live firewall or listener.
5. Inventory current grants before applying the reviewed `postgres_least_privilege.sql` template. It requires an exact expected database and explicit role variables, creates missing no-password LOGIN roles idempotently, and grants API create/read, worker read/update, and purge read/delete only. Provision passwords separately through the secret channel. Verify `rolsuper=false` with the query output:

```bash
psql "service=capsolve_admin" -X -v ON_ERROR_STOP=1 \
  -v expected_db=capsolve -v api_role=capsolve_api \
  -v worker_role=capsolve_worker -v purge_role=capsolve_purge \
  -f deployment/postgres_least_privilege.sql
```

Run the same reviewed command again to prove idempotency. A database-name mismatch fails before role or grant changes.

## Backup and restore evidence

Use named libpq services (`service=capsolve_backup` and a distinct disposable restore service) with protected `0600` `pg_service.conf` and pgpass owned by `capsolve-backup`. Never put a password or DSN in argv, units, logs, or evidence. The daily `capsolve-backup.timer` invokes `deployment/baseline.py scheduled-backup` as `capsolve-backup`; it creates a custom-format dump, verifies SHA-256 and archive row count, atomically updates nonsecret evidence only after success, then removes only expired older dumps. Its protected directory and positive retention (maximum 24 hours) come from `backup.env`. Restore verification still uses `deployment/baseline.py restore-test <backup-directory> --restore-pgservice <disposable-service> --confirm-disposable-database` and never targets production. Production off-host copy/storage remains **PENDING OPERATIONAL VERIFICATION**.

Approved Phase 7 targets are RPO exactly 24 hours and RTO at most 60 minutes, with measured restore duration recorded. Backup retention must be positive and no longer than `JOB_RETENTION_HOURS` (approved value 24 hours). After each successful backup/checksum and periodic restore, write only the nonsecret fields in `recovery-evidence.example.json`, including artifact ID/basename/SHA-256 and coherent timestamps; atomically install it root-owned mode `0600`, maximum 64 KiB, then run runtime production preflight. Static mode validates artifacts only and never establishes timer/ownership operational readiness. Purge interval must be strictly less than retention. Keep backup artifacts themselves encrypted/access-controlled and delete them at policy expiry.

The terminal purge index is intentionally deferred: current bounded batches avoid a blocking production index build. Inspect production volume and `EXPLAIN` first; if needed, use a separately reviewed `CREATE INDEX CONCURRENTLY` rollout and `DROP INDEX CONCURRENTLY` rollback outside the transactional migration runner.

A real production network probe, role query, scheduled backup, checksum, restore timing, matching row counts, timer enabled/active state, and protected current evidence remain **PENDING OPERATIONAL VERIFICATION**; no production action is performed by repository implementation.

# Phase 8–12 deployment and operations runbook

Repository status is **ARTIFACT READY; PENDING OPERATIONAL VERIFICATION**. Nothing in this section has been installed or run on production. Every command prefixed `MANUAL OPERATOR COMMAND` is mutating or contacts the live service and requires change approval, reviewed values, a maintenance window where applicable, and operator execution. Never paste secrets, NRIC, DSNs, or tokens into chat, logs, command arguments, or repository files.

## Static quality gate

The local gate compiles every solution Python source outside `.git`, `.venv`, `dist`, and caches; validates systemd, nginx, cloudflared, component environment examples, journald retention, and the benchmark self-check; runs the existing self-check; then runs full unittest discovery once. Every module containing PostgreSQL tests, including Phase 6 and 7, receives `TEST_DATABASE_URL` only when it names a loopback database containing `test`, `temp`, or `disposable` and `PGPASSWORD` is available; otherwise the gate removes both and reports pending/rejected rather than PostgreSQL PASS. It never installs artifacts or sends BUDI95 requests.

```bash
uv run python deployment/ops.py quality
uv run python deployment/ops.py benchmark
uv run python deployment/ops.py alerts --input /protected/read-only-metrics.json
```

The benchmark command defaults to local dry-run and sends zero requests. It accepts only an existing summary CSV with `round,mode,component,outcome,submitted_at,completed_at,queue_seconds,solve_seconds,retries,cpu_percent,memory_bytes,memory_max_bytes,swap_bytes,chrome_tasks`; sensitive identity/credential columns are rejected. A valid measurement requires at least three rounds, each with at least 30 jobs or 15 minutes and both API/worker plus sync/async rows. The validator reports separate throughput, queue/solve/end-to-end median and p95, retry/failure cost, CPU, memory headroom, swap, Chrome tasks, and drain time; it requires memory headroom >=30%, no steady swap, and sustained CPU <70%. Missing approved failure-ratio or latency SLA thresholds is `PENDING`, never PASS.

## Artifact layout and permission chain

Install reviewed copies under `/opt/capsolve`, unit files under `/etc/systemd/system`, component environment files under `/etc/capsolve`, and the journal namespace file as `/etc/systemd/journald@capsolve.conf`. API, worker, purge, backup, and Xvfb use distinct non-root identities. Create separate owner-only `/var/lib/capsolve-api/profiles` and `/var/lib/capsolve-worker/profiles` state trees; they share only external Xvfb and PostgreSQL advisory-lock state. Environment files are separate, root-owned mode `0600`. The API runs `capsolve-api:capsolve-nginx`; nginx alone receives `capsolve-nginx` membership, while `capsolve-worker` must not. No file logging is configured, so logrotate is not applicable.

The permission-controlled chain is cloudflared -> `/run/capsolve/ingress/cloudflared.sock` (`root:cloudflared 0660`) -> nginx -> `/run/capsolve/uvicorn/api.sock` (`capsolve-api:capsolve-nginx 0660`). The API unit creates `/run/capsolve/uvicorn` as `capsolve-api:capsolve-nginx 0770`; nginx needs supplementary `capsolve-nginx`, and worker gets no traversal/connect permission. `secure_nginx_ingress.py` verifies and opens the ingress directory only after nginx creates the root-owned socket. Production has no Uvicorn/nginx TCP listener. Replace every `<REQUIRED_...>` placeholder through a protected deployment channel before validation.

Resource limits are conservative templates, not measured capacity: API/worker `MemoryHigh=2G`, `MemoryMax=3G`, `TasksMax=512`, `CPUQuota=200%`; Xvfb `512M/768M`, 64 tasks, 100%; purge 256M, 32 tasks, 50%. Validate Chrome behavior and host headroom before accepting them. The API restart policy is `on-failure`; `KillMode=control-group` and stop timeouts prevent normal restarts from intentionally leaving Chrome children. Worker/purge are oneshot timer units, so systemd does not overlap a second activation of the same service.

## Observability and alert checks

Structured API lifecycle/request events and worker/purge summaries are JSON and contain controlled event/outcome/count/duration/config-source fields only; solver diagnostics may still be plain text. API records accepted/rejected submit, sync solve outcome/duration, readiness, and startup. Worker summary records success, terminal failure, retry, lost claim, solve median/p95, freshness timestamps/exit status, queue depth, pending/processing, oldest pending age, stale processing, and config source. It never selects/logs NRIC for metrics. Uvicorn/nginx access logs remain disabled because request URLs may contain NRIC.

Use approved SLA/baseline values for placeholders below. Initial hard limits are queue >=80% capacity, stale processing >0, readiness failure, three missed one-minute worker intervals (freshness >180 seconds), memory >=80% of `MemoryMax`, disk/inodes >=80%, and sustained CPU >=70% host. Failed-ratio and oldest-pending alerts remain `PENDING OPERATIONAL VERIFICATION` until owners approve `<FAILED_RATIO_THRESHOLD>` and `<OLDEST_PENDING_SLA_SECONDS>` from baseline/SLA. Repeated config fallback/failure is any non-`env` source outside the approved resolver policy or repeated resolver errors within three intervals.

Read-only operator checks:

```bash
journalctl --namespace=capsolve -u capsolve-api.service -u capsolve-worker.service -u capsolve-purge.service -o cat --since '15 minutes ago'
journalctl --namespace=capsolve -u capsolve-worker.service -o json --since '15 minutes ago' | /opt/capsolve/.venv/bin/python /opt/capsolve/worker_freshness.py --max-age-seconds 180
systemctl show capsolve-api.service capsolve-worker.service --property=NRestarts,MemoryCurrent,TasksCurrent,CPUUsageNSec,ActiveState
systemctl show capsolve-worker.timer capsolve-purge.timer --property=ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime
curl --fail --silent --show-error --unix-socket /run/capsolve/uvicorn/api.sock -H 'Host: <REQUIRED_CAPSOLVE_HOSTNAME>' http://localhost/api/ready
```

Read-only SQL through an operator-managed named libpq service; output contains counts/ages only:

```sql
SELECT COUNT(*) FILTER (WHERE status IN ('pending','processing')) AS queue_depth,
       COUNT(*) FILTER (WHERE status='pending') AS pending_count,
       COUNT(*) FILTER (WHERE status='processing') AS processing_count,
       EXTRACT(EPOCH FROM (NOW()-MIN(created_at) FILTER (WHERE status='pending'))) AS oldest_pending_age_seconds,
       COUNT(*) FILTER (WHERE status='processing' AND started_at < NOW()-INTERVAL '30 minutes') AS stale_processing_count
FROM budi95_jobs;
```

## Rollout and canary

1. Review the exact commit/diff, quality output, locked dependencies, rollback commit/config, Phase 0 backup/restore evidence, approved retention, and Phase 11 measurements. Stop if any is unavailable.
2. `MANUAL OPERATOR COMMAND`: install reviewed code with `uv sync --frozen`; install protected environment/unit/proxy/journal artifacts; set identities, groups, directories, ownership, and modes. No migration is expected for Phase 8–12.
3. `MANUAL OPERATOR COMMAND`: before starting anything, run `systemd-analyze verify` on the five CapSolve services, three timers, and the ingress permission service/path; run `nginx -t`, local cloudflared config validation, static production preflight, and journal configuration validation. Stop on warnings, errors, or placeholders.
4. `MANUAL OPERATOR COMMAND`: install and enable `capsolve-ingress-permissions.path`, keeping `/run/capsolve/ingress` root-only until its helper succeeds; then enable/start Xvfb and API. Do not add drop-ins that stop or restart shared nginx/cloudflared.
5. `MANUAL OPERATOR COMMAND`: enable/start worker, purge, and backup timers. Trigger and verify one scheduled backup using the documented named-service procedure; the backup timer must have a successful last trigger.
6. `MANUAL OPERATOR COMMAND`: restore that current dump only into the confirmed disposable database, verify row counts/checksum/RTO, and install current protected aggregate restore evidence plus scheduled-backup evidence with approved ownership and mode.
7. `MANUAL OPERATOR COMMAND`: only now run runtime production preflight. It cannot pass before the backup timer has triggered and current restore evidence exists. Keep client traffic closed until it passes.
8. External public-origin and authenticated ingress probing remains `PENDING OPERATIONAL VERIFICATION`.
7. `MANUAL OPERATOR COMMAND`: validate the authenticated cloudflared/nginx path with approved hostname, source IP, and key; prove forged forwarding headers, unauthorized key, blocked IP, docs paths, direct sockets, and public origin are rejected.
8. `MANUAL OPERATOR COMMAND`: submit exactly one approved synthetic test identity supplied through the approved secret/input channel; poll result and verify worker, result contract, queue drain, retention, and privacy-safe logs. Never put the identity in command history or this runbook.
9. `MANUAL OPERATOR COMMAND`: canary at low volume with `MAX_WORKERS=1`, `GLOBAL_CHROME_SLOTS=1`, `SYNC_QUEUE_MAX_WAITING=0`, conservative measured capacity, and no concurrency increase. Observe at least the approved stabilization window; stop on threshold breach.
10. `MANUAL OPERATOR COMMAND`: increase client traffic only after operator sign-off. Production capacity/concurrency remains `PENDING OPERATIONAL VERIFICATION` until three approved benchmark rounds pass.

## Smoke check and go-live checklist

Before traffic: quality gate PASS; static/runtime preflight PASS; secrets random/protected; explicit allowlist/hosts/trusted proxy; docs disabled; no public origin/PostgreSQL; UDS chain modes proven; external Xvfb stable; queue 429 and DB 503 contracts proven; fencing/global slots disposable-DB tests PASS; retention approved; purge/backup/restore evidence current; worker freshness, queue query, readiness, resource/disk/inode alerts and journal retention active; rollback reviewed. Canary must prove authorized success, unauthorized key 401, blocked IP 403, docs unavailable, one approved end-to-end result, non-overlap, no PII/secret logs, no profile lock/orphan, and resource recovery. All live items remain `PENDING OPERATIONAL VERIFICATION` until signed evidence exists.

## Rollback and queue recovery

Trigger rollback on readiness failure, sustained threshold breach, queue growth beyond SLA, stale/lost-claim anomaly, upstream rejection increase, Chrome/Xvfb instability, privacy leak, or contract regression.

1. `MANUAL OPERATOR COMMAND`: stop/divert new submits at the approved ingress maintenance control; preserve result reads where possible.
2. `MANUAL OPERATOR COMMAND`: stop/disable the worker timer and wait for or terminate only through systemd's control group. Do not delete or rewrite pending/processing rows.
3. Capture read-only queue/service/journal evidence without PII. If a processing job was interrupted, allow the existing stale reset/fencing path on a later reviewed worker invocation; never manually decrement attempts or force terminal state.
4. `MANUAL OPERATOR COMMAND`: restore the recorded application commit and previous unit/nginx/cloudflared/journal configuration, verify diffs/modes, run frozen dependency sync and static validation, then daemon-reload/restart only approved services.
5. Verify health/readiness/result reads and queue counts before reopening submit. Database restore is only for an approved database-recovery incident, never routine application rollback.

## API key rotation

1. Generate the replacement with the documented `secrets.token_urlsafe(32)` mechanism directly into the protected secret channel; do not print/store it in logs or Git.
2. `MANUAL OPERATOR COMMAND`: atomically install root-owned `0600` API environment containing `API_KEYS=old,new`, restart API, and verify readiness plus client traffic using privacy-safe counts.
3. Move approved clients to the new key through the secret channel and observe the stabilization window.
4. `MANUAL OPERATOR COMMAND`: atomically install `API_KEYS=new`, restart API, verify the new key succeeds and the retired key receives 401, then revoke/dispose the old secret per policy.
5. Roll back to the protected previous two-key file if clients fail; never rotate worker/purge files because they contain no inbound API key.

No rollout, canary, smoke job, key rotation, server mutation, production database action, or real BUDI95 benchmark has been performed by this repository work.
