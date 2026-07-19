# CapSolve

FastAPI service for solving Cloudflare Turnstile with a real Chrome browser and forwarding the generated token to a configured upstream endpoint. It accepts an NRIC/MyKad value, submits it with the Turnstile token, and returns the upstream result.

## Requirements

- Python 3.12+
- uv
- Google Chrome or Chromium
- Linux server: Xvfb

Install Xvfb on Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y xvfb
```

## Installation

```bash
git clone https://github.com/fanfanfw/CapSolve.git
cd CapSolve
uv sync
```

Dependencies are managed through `pyproject.toml`.

## Setup Postgres

Create the local database and run the SQL migration:

```bash
createdb capsolve
uv run capsolve-migrate-sql
```

## Configuration

Copy `.env.example` to `.env` for development. `ENVIRONMENT` accepts only `development` or `production`; `.env.example` contains the complete development baseline and integer defaults.

```bash
cp .env.example .env
```

Production API startup requires a nonempty `API_KEY` or `API_KEYS`, explicit `API_IP_ALLOWLIST` networks, explicit `ALLOWED_HOSTS`, explicit non-wildcard `FORWARDED_ALLOW_IPS`, disabled docs, and an absolute `UVICORN_UDS` (default `/run/capsolve/uvicorn/api.sock`). Production rejects `API_HOST`; development accepts only loopback IP binds. Generate each key through the deployment secret channel:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Record only the provisioning method, operator/change reference, and time in deployment evidence; never record or log the generated value. Production keys must be 43–128 URL-safe characters. A nonempty `API_KEYS` comma-separated list fully replaces `API_KEY`, enabling explicit rotation without implicitly retaining the single key. `API_IP_ALLOWLIST` accepts comma-separated IPv4, IPv6, or CIDR values; `*` is development-only.

The API validates inbound keys, allowlist, hosts, and all consumed integer/boolean settings. Worker and purge validation do not require inbound API security settings. Accepted boolean tokens are `1`, `true`, `yes`, `on`, `0`, `false`, `no`, and `off` (case-insensitive). Invalid or explicitly empty integer values fail validation. `JOB_RETENTION_HOURS` has the approved development default `24`, range `1..8760`, and is explicitly required for every production component.

| Variable | Description |
| --- | --- |
| `ENVIRONMENT` | `development` or `production` |
| `API_KEY` / `API_KEYS` | Inbound API key or explicit comma-separated rotation set |
| `API_IP_ALLOWLIST` | Comma-separated IPv4/IPv6 networks; development may use `*` |
| `ALLOWED_HOSTS` | Comma-separated hosts enforced by `TrustedHostMiddleware`; production forbids wildcards |
| `API_DOCS_ENABLED` | Development toggle; production must be false, leaving docs/ReDoc/OpenAPI unregistered |
| `FORWARDED_ALLOW_IPS` | Native Uvicorn proxy trust; production permits only explicit exact loopback peers (for the nginx-to-UDS peer identity), never `*` or broad CIDRs |
| `API_HOST`, `UVICORN_UDS`, `UVICORN_SOCKET_MODE`, `UVICORN_SOCKET_PARENT_GID`, `UVICORN_SOCKET_GID` | Development loopback TCP; production pre-bound Unix socket `0660`, parent creator GID (defaults to process primary GID) and required explicit nginx target GID |
| `SOLVER_TIMEOUT`, `LOCAL_POST_TIMEOUT` | Positive solve and upstream timeouts |
| `JOB_QUEUE_CAPACITY`, `JOB_QUEUE_RETRY_AFTER_SECONDS` | Positive outstanding-job limit and retry delay for rejected async submits |
| `BUDI95_SUBMIT_RATE_LIMIT_PER_MINUTE`, `BUDI95_READ_RATE_LIMIT_PER_MINUTE` | Per-resolved-client-IP fixed-window limits for submit/solve and BUDI95 read/poll routes; defaults `30`/`120`, `0` disables |
| `JOB_BATCH_LIMIT`, `JOB_MAX_ATTEMPTS` | Positive worker batch and new-job attempt limits |
| `JOB_RESET_STALE_MINUTES`, `SYNC_QUEUE_MAX_WAITING` | Nonnegative worker stale and bounded synchronous waiting settings |
| `JOB_RETENTION_HOURS`, `PURGE_BATCH_LIMIT` | Terminal-job retention: development default/approved value `24`, production explicitly required, range `1..8760`; deterministic purge batch default `1000`, range `1..10000` |
| `MAX_WORKERS`, `GLOBAL_CHROME_SLOTS`, `DB_CONNECT_TIMEOUT` | Positive local API concurrency, host-wide Chrome slots, and database connection limits |
| `TS_PROFILE_DIR` | Dedicated real directory for unique per-solve profiles; must be owned by the runtime uid, mode `0700`, and not a symlink |
| `DISPLAY`, `ENABLE_XVFB_VIRTUAL_DISPLAY` | External display (production `:99`) and development-only API Xvfb launch toggle |

### Configuration reload semantics

`.env` is not hot reload. API settings apply after an API restart; this includes API key and IP allowlist changes. Worker and purge settings apply on their next invocation. A changed `JOB_MAX_ATTEMPTS` applies only to jobs submitted afterward. Lowering `JOB_QUEUE_CAPACITY` never deletes existing jobs; new submissions remain blocked until outstanding jobs fall below the limit.

Async admission is serialized by a dedicated PostgreSQL transaction advisory lock. Capacity counts only `pending` plus `processing`; `success` and `failed` rows do not occupy slots. A full queue returns HTTP `429` with `Retry-After` and `{"detail":"Job queue is full"}`. An unavailable database/admission path returns HTTP `503` with the same header and `{"detail":"Job queue is unavailable"}`.

Application rate limits are fixed one-minute windows per resolved client IP after Host, IP allowlist, and API-key validation. `BUDI95_SUBMIT_RATE_LIMIT_PER_MINUTE` applies to `POST /api/budi95`, its trailing-slash alias, and legacy `POST /api/solve/`. `BUDI95_READ_RATE_LIMIT_PER_MINUTE` applies to config, result polling, and queue status. Exceeding either returns HTTP `429`, `Retry-After`, and `{"detail":"Rate limit exceeded"}`. Limits are per API process and reset on restart; use Cloudflare/nginx limits as an additional distributed edge layer when scaling beyond one process.

## Dynamic BUDI95 Config

CapSolve can resolve BUDI95 endpoint and Turnstile sitekey from the official website at runtime.

Default behavior:
- worker checks cached config first
- if cache expired, it fetches official website config
- if website fetch fails, it falls back to `.env`
- if upstream DNS/connection fails, worker force-refreshes config and retries once

The cache defaults to 30 minutes (`BUDI95_CONFIG_CACHE_SECONDS=1800`). `.env` is only a fallback/manual override source and is not auto-edited by the resolver.

Env vars:
- `BUDI95_AUTO_RESOLVE=true` enables runtime discovery.
- `BUDI95_CONFIG_URL=https://www.budirakyat.gov.my/eligibility-check` is the official config page.
- `BUDI95_CONFIG_CACHE_SECONDS=1800` controls cache lifetime.
- `BUDI95_CONFIG_FETCH_TIMEOUT=10` controls website fetch timeout.
- `BUDI95_FORCE_ENV_CONFIG=false` uses dynamic config unless set to force `.env` values.
- `BUDI95_CONFIG_CACHE_FILE=/tmp/capsolve_budi95_config.json` stores the resolved config cache.
- `LOCAL_POST_URL`, `TURNSTILE_SITEKEY`, and `TURNSTILE_SITEURL` remain the `.env` fallback/manual override values.

Troubleshooting:

```bash
curl -H "Host: localhost" -H "x-api-key: ..." http://127.0.0.1:8191/api/budi95/config
curl -H "Host: localhost" -H "x-api-key: ..." "http://127.0.0.1:8191/api/budi95/config?force_refresh=true"
```

Use `force_refresh=true` to bypass the cache and fetch the official website config again.

## Run

Development:

```bash
uv run capsolve-api
```

Production uses the `capsolve-api` entry point and a permission-bound Unix socket; do not invoke a TCP Uvicorn command:

```bash
ENVIRONMENT=production UVICORN_UDS=/run/capsolve/uvicorn/api.sock uv run capsolve-api
```

`run()` opens every UDS ancestor with `openat`/`O_NOFOLLOW`, requires the final parent to be immutable to untrusted users (`0750`/`0770`, matching configured group), binds through the held parent fd, applies `0660` to an inode-bound fd, and passes the inherited listener to Uvicorn so path replacement and Uvicorn's `0666` behavior cannot win. It also enables native Uvicorn proxy headers with the exact validated `FORWARDED_ALLOW_IPS` value. On the production UDS, the permission-authenticated peer is represented as loopback solely so Uvicorn's native middleware can resolve nginx's single XFF; business code still sees only the resolved `request.client.host`. Business routes enforce Host, resolved client allowlist, API key, then queue/capacity. They never parse `Forwarded`, XFF, X-Real-IP, or Cloudflare headers. Health remains the existing public shallow liveness endpoint.

Keep Uvicorn at one process before benchmarking. `MAX_WORKERS` bounds active synchronous solves in that API process; `SYNC_QUEUE_MAX_WAITING` bounds admitted synchronous waiters, and overflow returns HTTP `429` with the configured `JOB_QUEUE_RETRY_AFTER_SECONDS`. API and serial worker share PostgreSQL session advisory slots, so aggregate Chrome never exceeds `GLOBAL_CHROME_SLOTS`. Slot keys are the documented stable range `1128360000` through `1128360000 + GLOBAL_CHROME_SLOTS - 1`, separate from queue admission key `1128352337`. The `capsolve-api` entry point disables Uvicorn access logging because its native access log includes full query strings; every direct Uvicorn command must keep `--no-access-log`.

## API

### `POST /api/solve/`

Query parameters:

| Parameter | Required | Description |
| --- | --- | --- |
| `nric` | yes | NRIC/MyKad number to check |
| `timeout` | no | Override `SOLVER_TIMEOUT` |
| `post_timeout` | no | Override `LOCAL_POST_TIMEOUT` |

Headers:

| Header | Required | Description |
| --- | --- | --- |
| `x-api-key` | yes | Must match the active `API_KEYS` set, or `API_KEY` when `API_KEYS` is empty |

Example:

```bash
curl -X POST "http://localhost:8191/api/solve/?nric=911024146045" \
  -H "x-api-key: replace-with-a-strong-api-key"
```

Example response:

```json
{
  "status": 200,
  "body": {
    "results": {
      "success": true,
      "quotas": [
        {
          "type": "FUEL",
          "skucode": "RON95",
          "expiredtime": "2026-06-30T16:00:59+00:00",
          "quotaunits": [
            {
              "unit": "LITRE",
              "quotaentitled": "200.000",
              "quotaavailable": "155.158",
              "quotalocked": "0.000",
              "quotaused": "44.842"
            },
            {
              "unit": "MYR",
              "quotaentitled": "346.00",
              "quotaavailable": "268.42",
              "quotalocked": "0.00",
              "quotaused": "77.58"
            }
          ]
        }
      ],
      "reason": null
    }
  }
}
```

Missing or invalid API key returns `401`. Internal solve failures use the approved stable contract HTTP `500` with `{"error_code":"solve_failed","message":"Unable to process subsidy"}`; exception details and NRIC are not returned.

Rejected request input that FastAPI/Pydantic would otherwise echo uses the approved generic HTTP `422` response `{"detail":"Invalid request"}`. The response and logs omit the raw body, query, header value, NRIC, and validation input. Safe missing-field validation may retain its existing structured `422` response when no submitted input is present.

### `POST /api/budi95`

Submits an async BUDI95 job. Both `/api/budi95` and `/api/budi95/` preserve the same HTTP `200` success response. The client sends only `nric` and does not send `captchadata`; surrounding whitespace is trimmed, empty values are rejected, and the schema-compatible maximum is 32 characters. No 12-digit format is imposed. The worker later generates the Turnstile token itself using the existing solver and posts the token to the upstream service.

```bash
curl -X POST "http://localhost:8191/api/budi95" \
  -H "Content-Type: application/json" \
  -H "x-api-key: replace-with-a-strong-api-key" \
  -d '{"nric":"950724115057"}'
```

Response:

```json
{
  "status": true,
  "id_no": "950724115057",
  "ulid": "01JABCDEF1234567890XYZABCD",
  "message": "OK"
}
```

Run the worker to process pending async jobs:

```bash
uv run capsolve-worker --limit 50
```

The worker claims one job immediately before processing it. `--limit` counts distinct jobs per invocation; a failed attempt returned to `pending` is not retried until a later invocation. Each invocation emits one final JSON record. `worker_failure` describes a controlled `retried`, `failed`, or `lost_claim` outcome; `worker_error` is a fatal invocation error and returns nonzero. Summary `failed` means a fenced terminal failure, `retried` means a fenced return to `pending`, and `lost_claim` means finalization updated no row because a newer attempt owns it.

Every final record includes UTC `invoked_at` and `completed_at`, numeric `exit_status`, `event`, queue depth (`pending + processing`), separate pending/processing counts, oldest pending age in seconds, and stale processing count. Even `--help` emits exactly one JSON record (`worker_help`) with concise help text and unavailable/null metrics rather than additional usage lines. `JOB_RESET_STALE_MINUTES=0` disables stale detection and reports zero. The metrics query never selects NRIC or result data. If this optional final metrics query is unavailable, metric fields remain `null`, `queue_metrics_available` is false, and an otherwise successful invocation remains successful.

Reviewed `capsolve-worker.service` and `.timer` templates are provided under `deployment/` but are not installed by the repository. After installation, detect a missing worker by checking both scheduling and the last parseable summary:

```bash
systemctl status capsolve-worker.timer capsolve-worker.service
journalctl --namespace=capsolve -u capsolve-worker.service -o json --since "15 minutes ago" | uv run python worker_freshness.py --max-age-seconds 900
```

`worker_freshness.py` accepts journal JSON or direct worker-summary lines, skips malformed and unrelated records, requires valid UTC invocation/completion timestamps, and checks the latest valid worker event. `--max-age-seconds` accepts `1` through `315360000` (10 years). It prints only `{"worker_fresh":true}` or `false`, never journal content. Invalid/out-of-range arguments, a missing/stale record, or latest nonzero `exit_status` exit nonzero; an inactive/late timer remains independently visible through `systemctl status`.

Cron example:

```cron
* * * * * cd /path/to/CapSolve && uv run capsolve-worker --limit 50
```

### Terminal-job retention and purge

The approved development default is `JOB_RETENTION_HOURS=24`: it leaves enough time for result polling and brief incident review while limiting NRIC and result lifetime. Every production API startup, worker invocation, purge invocation, and preflight requires it explicitly. Valid retention is `1..8760` hours; purge limits are `1..10000`. Malformed, empty, zero, negative, or larger values fail validation generically.

Run `uv run capsolve-purge-jobs --limit 1000`; add `--dry-run` to inspect only the bounded eligible count, oldest timestamp, and strict cutoff (`processed_at < cutoff`). Purge selects only `success`/`failed`, orders by `processed_at,id`, and never logs identifiers, NRIC, or result bodies. Reviewed 30-minute non-overlapping oneshot artifacts are provided at `deployment/capsolve-purge.service` and `.timer`; they are part of the deployment bundle but are not installed by the repository.

Run `uv run capsolve-production-preflight --static --evidence /root-owned/phase7.json` only for non-operational artifact/evidence validation; its JSON always reports `operational_ready=false`. Runtime mode is required for operational readiness and reads only `systemctl show` state/timestamps; it never changes units. Production CLI evidence must be root-owned, nonsymlink, regular, at most 64 KiB, and mode `0600` or tighter. It binds a nonsecret artifact ID/basename to SHA-256, requires coherent backup/checksum/restore timestamps, matching strict integer row counts, RPO exactly 24 hours, measured restore duration, and RTO no more than 60 minutes.

### `GET /api/budi95/result/{ulid}`

Checks an async job result by `ulid`:

```bash
curl "http://localhost:8191/api/budi95/result/01JABCDEF1234567890XYZABCD" \
  -H "x-api-key: replace-with-a-strong-api-key"
```

Response while pending or processing:

```json
{
  "status": true,
  "job_status": "pending",
  "message": "OK",
  "data": null
}
```

Response when complete:

```json
{
  "status": true,
  "job_status": "completed",
  "message": "OK",
  "data": {}
}
```

Response when failed:

```json
{
  "status": false,
  "job_status": "failed",
  "message": "Unable to process subsidy",
  "data": {
    "error_code": "job_failed",
    "message": "Unable to process subsidy"
  }
}
```

### `GET /api/budi95/queue/status`

Returns PostgreSQL-backed async BUDI95 queue utilization. This endpoint requires the same client-IP allowlist and `x-api-key` as other BUDI95 routes.

```bash
curl -H "Host: localhost" -H "x-api-key: ..." http://127.0.0.1:8191/api/budi95/queue/status
```

`pending` is waiting work, `processing` approximates active scheduled-worker work, and `available` is remaining admission capacity. `worker.max_concurrent_solves` is the configured host-wide Chrome limit; the cron/systemd worker process may be absent between invocations. Database failure returns a generic HTTP `503`.

### `GET /api/health`

```bash
curl http://localhost:8191/api/health
```

Response:

```json
{
  "status": "ok",
  "workers": 1,
  "active": 0,
  "queued": 0
}
```

This is shallow process liveness and preserves its existing exact status, headers, and body. It never checks PostgreSQL, Chrome, BUDI95, or another upstream. `workers`, `active`, and `queued` are synchronous-solve counters for this single API process; they are not PostgreSQL queue metrics.

### `GET /api/ready`

```bash
curl -H "Host: localhost" http://127.0.0.1:8191/api/ready
```

Readiness is public for infrastructure probes (no API key or client-IP allowlist dependency) but still passes the configured Host gate. Startup has already validated required API settings. The endpoint runs only `SELECT 1`, with both PostgreSQL connect and statement timeout derived from the small `DB_CONNECT_TIMEOUT`; it never checks queue capacity, Chrome, BUDI95, or another upstream. A usable database returns HTTP `200` with `{"status":"ready"}`. Any database failure returns HTTP `503` with `{"status":"unavailable"}` and no database exception or connection detail. A full job queue remains ready because result reads are still serviceable.

### API docs

Development may enable `/docs`, `/redoc`, and `/openapi.json`. Production rejects `API_DOCS_ENABLED=true`; all three routes are unregistered when disabled. Because route registration happens at app construction, set the toggle before process import/start and restart to change it.

## CLI usage

Direct `solver.py` execution is disabled so it cannot bypass PostgreSQL Chrome-slot admission or print Turnstile tokens. Use the synchronous API or submit an asynchronous job for `capsolve-worker`.

## Xvfb behavior

Production uses one externally managed Xvfb shared by API and worker:

```env
DISPLAY=:99
ENABLE_XVFB_VIRTUAL_DISPLAY=false
```

Neither process starts Xvfb in this configuration, production validation rejects `ENABLE_XVFB_VIRTUAL_DISPLAY=true`, and the worker never starts Xvfb. The API may start `XVFB_DISPLAY` only in development when `ENABLE_XVFB_VIRTUAL_DISPLAY=true` and `DISPLAY` is unset. A reviewed external Xvfb unit template is provided at `deployment/capsolve-xvfb.service` but is not installed by the repository. If combined-load testing shows one external display is unstable, assign separate externally managed displays. `TS_PROFILE_DIR` is only a base: every solve gets a unique PID/random-suffixed child that is removed after Chrome stops.

## Deployment notes

`deployment/capsolve-nginx.conf.example`, `deployment/cloudflared-config.yml.example`, and `deployment/secure_nginx_ingress.py` define the reviewed trust-boundary templates without installing or changing server configuration. The ingress directory starts `root:cloudflared 0700`; nginx's root master creates a root-owned listener, then the root launcher opens all ancestors with `openat`/`O_NOFOLLOW`, verifies the inode through an object fd, changes the socket to `root:cloudflared 0660`, and only then grants cloudflared traversal (`0710`). The Uvicorn directory is `root:capsolve 0770`; capsolve creates `capsolve:capsolve-nginx 0660`, and nginx has supplementary `capsolve-nginx` membership. cloudflared alone connects to a group-restricted nginx Unix socket; nginx alone connects to the Uvicorn Unix socket. There is no public or TCP listener. nginx clears all inbound forwarding headers, accepts a syntactically valid canonical `CF-Connecting-IP` only on the authenticated cloudflared socket, and synthesizes one XFF value. Every angle-bracket placeholder must be replaced before validation. Final users/groups, unit ownership setup, rollout, and live authenticated tunnel verification remain pending operational work. The local cloudflared binary validates syntax only; no authenticated tunnel has been started by repository tests.

Start production only through the validated entry point after those socket directories have been provisioned:

```bash
uv run capsolve-api
```

Check namespaced service logs:

```bash
journalctl --namespace=capsolve -u capsolve-api.service -f -o cat
```

Check process:

```bash
ps aux | grep uvicorn
```

## Project structure

```text
CapSolve/
├── pyproject.toml  # uv project dependencies and console script
├── service.py      # FastAPI API service
├── settings.py     # Startup/invocation environment validation
├── solver.py       # Browser automation and upstream posting logic
├── clientsend.py   # Legacy client/helper
├── .env.example    # Safe example configuration
└── .env            # Local runtime configuration, ignored by git
```

## Troubleshooting

### Chrome still appears on desktop

Production recovery must use `systemctl stop` or `systemctl restart` for the affected CapSolve API/worker unit; its `KillMode=control-group` contains Chrome children. Do not target shared nginx/cloudflared.

For development, stop the process you started through its normal terminal/process handle, confirm no Chrome child remains for that invocation, inspect the configured `TS_PROFILE_DIR`, and clean only its verified child profile entries. Current production profile roots are `/var/lib/capsolve-api/chrome` and `/var/lib/capsolve-worker/chrome`; never recursively delete an unverified path.

### Xvfb display already active

Use another display:

```env
XVFB_DISPLAY=:100
TS_PROFILE_DIR=/var/lib/capsolve-api/chrome
```

### Chrome not found

Set `CHROME_PATH`:

```env
CHROME_PATH=/usr/bin/google-chrome
```

### API key errors

Make sure `.env` has `API_KEY` and request header has matching `x-api-key`.
