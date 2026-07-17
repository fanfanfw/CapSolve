from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest import mock
from urllib.parse import urlencode


BASELINE_COMMIT = "500f2e5"
API_KEY = "phase2-baseline-key"
CANARY = "S1234567A SQL db.internal dbuser dbpassword"


def request(app, method: str, path: str, *, query=None, body=None, api_key: str | None = API_KEY) -> dict:
    payload = json.dumps(body).encode() if body is not None else b""
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return {
        "status": start["status"],
        "headers": {"content-type": headers["content-type"]},
        "body": json.loads(content),
    }


def capture(worktree: Path) -> dict:
    commit = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if commit != BASELINE_COMMIT:
        raise RuntimeError("worktree is not the required baseline commit")
    solver = ModuleType("solver")
    solver.load_dotenv = lambda: None
    solver.solve = lambda *args, **kwargs: None
    solver.post_local_result = lambda *args, **kwargs: None
    sys.modules["solver"] = solver
    sys.path.insert(0, str(worktree))
    os.environ.clear()
    os.environ.update({"API_KEY": API_KEY, "MAX_WORKERS": "1"})
    import service
    from config_resolver import Budi95Config

    fixtures = {}
    success = {"status": 200, "body": {"results": {"success": True, "reason": None}}}
    with mock.patch.object(service, "_solve_and_post", return_value=success):
        fixtures["solve_success"] = request(service.app, "POST", "/api/solve/", query={"nric": "S1234567A"})
    fixtures["solve_missing_nric"] = request(service.app, "POST", "/api/solve/")
    fixtures["solve_missing_key"] = request(
        service.app, "POST", "/api/solve/", query={"nric": "S1234567A"}, api_key=None
    )
    fixtures["solve_invalid_key"] = request(
        service.app, "POST", "/api/solve/", query={"nric": "S1234567A"}, api_key="invalid"
    )
    with mock.patch.object(service, "_solve_and_post", side_effect=RuntimeError(CANARY)), contextlib.redirect_stdout(io.StringIO()):
        fixtures["solve_raw_failure"] = request(
            service.app, "POST", "/api/solve/", query={"nric": "S1234567A"}
        )

    configs = {
        False: Budi95Config("https://api.example.invalid/result", "https://site.example.invalid", "0x1234567890abcdef", "cache"),
        True: Budi95Config("https://api.example.invalid/result", "https://site.example.invalid", "0xabcdef1234567890", "website"),
    }
    with mock.patch.object(service, "resolve_budi95_config", side_effect=lambda force_refresh=False: configs[force_refresh]):
        fixtures["config_normal"] = request(service.app, "GET", "/api/budi95/config")
        fixtures["config_force"] = request(service.app, "GET", "/api/budi95/config", query={"force_refresh": "true"})

    job = {"nric": "S1234567A", "ulid": "a" * 32}
    with mock.patch.object(service.job_repository, "create_job", return_value=job):
        fixtures["submit"] = request(service.app, "POST", "/api/budi95", body={"nric": "S1234567A"})
        fixtures["submit_slash"] = request(service.app, "POST", "/api/budi95/", body={"nric": "S1234567A"})
    fixtures["submit_malformed_type"] = request(
        service.app, "POST", "/api/budi95", body={"nric": {"value": CANARY}}
    )
    fixtures["solve_malformed_timeout"] = request(
        service.app, "POST", "/api/solve/", query={"nric": "safe", "timeout": CANARY}
    )

    rows = {
        "pending": {"status": "pending"},
        "processing": {"status": "processing"},
        "success": {"status": "success", "response_body": {"results": {"success": True}}},
        "failed": {"status": "failed", "error": CANARY},
    }
    for state, row in rows.items():
        with mock.patch.object(service.job_repository, "get_job_by_ulid", return_value=row):
            fixtures[f"result_{state}"] = request(service.app, "GET", "/api/budi95/result/" + "b" * 32)
    with mock.patch.object(service.job_repository, "get_job_by_ulid", return_value=None):
        fixtures["result_not_found"] = request(service.app, "GET", "/api/budi95/result/missing")
    fixtures["health"] = request(service.app, "GET", "/api/health")
    return {
        "baseline_commit": BASELINE_COMMIT,
        "procedure": "git worktree add --detach <temporary-path> 500f2e5; .venv/bin/python tools/capture_api_contract.py <temporary-path> tests/fixtures/api_contract_500f2e5.json; git worktree remove <temporary-path>",
        "environment": {"API_KEY": "phase2-baseline-key", "MAX_WORKERS": "1"},
        "external_boundaries": "solver, database repository calls, and dynamic config resolver outputs mocked; FastAPI routing and serialization executed through ASGI",
        "fixtures": fixtures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(capture(args.worktree), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
