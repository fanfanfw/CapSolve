#!/usr/bin/env python3
"""Run a command against a disposable loopback Postgres (Docker).

Starts postgres:16 on 127.0.0.1:<port>, waits until ready, sets:
  TEST_DATABASE_URL=postgresql://127.0.0.1:<port>/capsolve_disposable_test
  PGPASSWORD=<ephemeral>
  PGUSER=postgres
then runs the given command and always removes the container.

Usage:
  uv run python tools/with_disposable_postgres.py -- uv run python deployment/ops.py quality
  uv run python tools/with_disposable_postgres.py -- python -m unittest discover -s tests -v
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid


CONTAINER_PREFIX = "capsolve-disposable-pg-"
IMAGE = "postgres:16-alpine"
DB_NAME = "capsolve_disposable_test"
DEFAULT_PORT = 55432


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("provide a command after --")

    if shutil.which("docker") is None:
        print("docker is required for disposable Postgres", file=sys.stderr)
        return 2

    password = secrets.token_urlsafe(24)
    name = CONTAINER_PREFIX + uuid.uuid4().hex[:12]
    port = args.port
    start = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={DB_NAME}",
            "-p",
            f"127.0.0.1:{port}:5432",
            args.image,
        ]
    )
    if start.returncode:
        print(start.stderr or start.stdout or "docker run failed", file=sys.stderr)
        return start.returncode

    env = os.environ.copy()
    env["TEST_DATABASE_URL"] = f"postgresql://127.0.0.1:{port}/{DB_NAME}"
    env["PGPASSWORD"] = password
    env["PGUSER"] = "postgres"

    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            probe = _run(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"PGPASSWORD={password}",
                    name,
                    "pg_isready",
                    "-U",
                    "postgres",
                    "-d",
                    DB_NAME,
                ]
            )
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            print("disposable Postgres did not become ready", file=sys.stderr)
            return 1

        # Prefer host-side connect via published port (what tests use).
        import psycopg2

        for _ in range(40):
            try:
                conn = psycopg2.connect(
                    host="127.0.0.1",
                    port=port,
                    dbname=DB_NAME,
                    user="postgres",
                    password=password,
                    connect_timeout=2,
                )
                conn.close()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("host cannot connect to disposable Postgres on published port", file=sys.stderr)
            return 1

        result = subprocess.run(command, env=env, check=False)
        return result.returncode
    finally:
        _run(["docker", "rm", "-f", name])


if __name__ == "__main__":
    raise SystemExit(main())
