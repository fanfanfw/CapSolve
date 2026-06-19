# EzSolver BUDI95 API

FastAPI service for checking BUDI95 RON95 quota by NRIC. The service opens a real Chrome browser with `nodriver`, solves the Cloudflare Turnstile challenge, posts the generated token to a configured upstream endpoint, and returns the upstream result.

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
cd EzSolver
uv sync
```

Dependencies are managed through `pyproject.toml`.

## Configuration

Copy the example environment file and fill in your own values:

```bash
cp .env.example .env
```

Example configuration is stored in `.env.example`:

```env
LOCAL_POST_URL=https://example.com/api/endpoint
SOLVER_TIMEOUT=45
LOCAL_POST_TIMEOUT=30
TURNSTILE_SITEKEY=your-turnstile-sitekey
TURNSTILE_SITEURL=https://example.com/page-with-turnstile

API_HOST=0.0.0.0
API_PORT=8191
API_KEY=replace-with-a-strong-api-key
# API_KEYS=key-one,key-two

ENABLE_XVFB_VIRTUAL_DISPLAY=true
XVFB_DISPLAY=:99
TS_PROFILE_DIR=/tmp/ts_profile_xvfb
MAX_WORKERS=4

# Optional
# CHROME_PATH=/usr/bin/google-chrome
# CHROME_ARGS=--disable-gpu
```

| Variable | Description |
| --- | --- |
| `LOCAL_POST_URL` | Upstream API endpoint that receives `nric` and `captchadata` |
| `TURNSTILE_SITEKEY` | Cloudflare Turnstile sitekey |
| `TURNSTILE_SITEURL` | Page URL where the Turnstile widget is loaded |
| `API_HOST` | FastAPI bind host |
| `API_PORT` | FastAPI bind port |
| `API_KEY` | API key required in the `x-api-key` header |
| `API_KEYS` | Optional comma-separated list of allowed API keys |
| `SOLVER_TIMEOUT` | Max seconds to wait for Turnstile token |
| `LOCAL_POST_TIMEOUT` | Max seconds to wait for upstream response |
| `MAX_WORKERS` | Max concurrent Chrome solve jobs in one API process |
| `ENABLE_XVFB_VIRTUAL_DISPLAY` | Force Chrome to run inside hidden Xvfb on Linux |
| `XVFB_DISPLAY` | Xvfb display number, for example `:99` |
| `TS_PROFILE_DIR` | Chrome profile directory used by the solver |
| `CHROME_PATH` | Optional explicit Chrome executable path |
| `CHROME_ARGS` | Optional extra Chrome launch arguments |

## Run

Development:

```bash
uv run ezsolver-api
```

Production-style single process:

```bash
uv run uvicorn service:app --host 0.0.0.0 --port 8191 --workers 1
```

Do not increase Uvicorn `--workers` unless each process has its own Xvfb display and Chrome profile directory. Use `MAX_WORKERS` to control concurrent Chrome jobs inside one process.

## API

### `POST /solve/`

Query parameters:

| Parameter | Required | Description |
| --- | --- | --- |
| `nric` | yes | NRIC/MyKad number to check |
| `timeout` | no | Override `SOLVER_TIMEOUT` |
| `post_timeout` | no | Override `LOCAL_POST_TIMEOUT` |

Headers:

| Header | Required | Description |
| --- | --- | --- |
| `x-api-key` | yes | Must match `API_KEY` or one value in `API_KEYS` |

Example:

```bash
curl -X POST "http://localhost:8191/solve/?nric=911024146045" \
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

Missing or invalid API key returns `401`.

### `GET /health`

```bash
curl http://localhost:8191/health
```

Response:

```json
{
  "status": "ok",
  "workers": 4,
  "active": 0,
  "queued": 0
}
```

### API docs

```text
http://localhost:8191/docs
```

## CLI usage

You can still run the solver directly:

```bash
uv run python solver.py --nric 911024146045
```

This prints both the generated Turnstile token and the upstream result.

## Xvfb behavior

On Linux, if `ENABLE_XVFB_VIRTUAL_DISPLAY=true`, the solver forces Chrome to run inside the configured Xvfb display even if the current desktop has `DISPLAY` set. This keeps Chrome hidden when running from a GUI terminal or tmux session.

If the configured display is already used, set another display and profile directory:

```env
XVFB_DISPLAY=:100
TS_PROFILE_DIR=/tmp/ts_profile_xvfb_100
```

## Deployment notes

Recommended command:

```bash
uv run uvicorn service:app --host 0.0.0.0 --port 8191 --workers 1
```

For background testing:

```bash
nohup uv run uvicorn service:app --host 0.0.0.0 --port 8191 --workers 1 > service.log 2>&1 &
```

Check logs:

```bash
tail -f service.log
```

Check process:

```bash
ps aux | grep uvicorn
```

## Project structure

```text
EzSolver/
├── pyproject.toml  # uv project dependencies and console script
├── service.py      # FastAPI API service
├── solver.py       # Browser automation and upstream posting logic
├── clientsend.py   # Legacy client/helper
├── .env.example    # Safe example configuration
└── .env            # Local runtime configuration, ignored by git
```

## Troubleshooting

### Chrome still appears on desktop

Stop old Chrome/solver processes and clear the old profile:

```bash
pkill -f "solver.py|uvicorn|/opt/google/chrome/chrome"
rm -rf /tmp/ts_profile /tmp/ts_profile_xvfb
```

Then start the API again.

### Xvfb display already active

Use another display:

```env
XVFB_DISPLAY=:100
TS_PROFILE_DIR=/tmp/ts_profile_xvfb_100
```

### Chrome not found

Set `CHROME_PATH`:

```env
CHROME_PATH=/usr/bin/google-chrome
```

### API key errors

Make sure `.env` has `API_KEY` and request header has matching `x-api-key`.
