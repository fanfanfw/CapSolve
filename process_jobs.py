from __future__ import annotations

import argparse
import json
import os
import socket
import urllib.error
from typing import Any

from psycopg2.extras import RealDictCursor

from config_resolver import resolve_budi95_config
import database
import job_repository
from solver import load_dotenv, post_local_result, solve


Job = dict[str, Any]


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _preview_pending_jobs(limit: int) -> list[Job]:
    with database.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT ulid, attempts, max_attempts, created_at
                FROM budi95_jobs
                WHERE status = 'pending'
                  AND attempts < max_attempts
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


def _worker_config(force_refresh: bool = False) -> dict[str, Any]:
    budi95_config = resolve_budi95_config(force_refresh=force_refresh)
    return {
        "sitekey": budi95_config.turnstile_sitekey,
        "siteurl": budi95_config.turnstile_siteurl,
        "post_url": budi95_config.local_post_url,
        "config_source": budi95_config.source,
        "solver_timeout": _env_int("SOLVER_TIMEOUT", 45),
        "post_timeout": _env_int("LOCAL_POST_TIMEOUT", 30),
    }


def _is_config_error(exc: Exception) -> bool:
    if isinstance(exc, (urllib.error.URLError, socket.gaierror)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.gaierror, TimeoutError)):
        return True
    text = str(exc).lower()
    return "connection refused" in text or "timed out" in text


def _will_retry(job: Job) -> bool:
    return int(job.get("attempts", 0)) < int(job.get("max_attempts", 0))


def _mark_failed(job: Job, error: str, summary: dict[str, Any]) -> None:
    job_repository.mark_job_failed(job["ulid"], error)
    if _will_retry(job):
        summary["retried"] += 1
    else:
        summary["failed"] += 1


def _process_job(job: Job, config: dict[str, Any], summary: dict[str, Any]) -> None:
    try:
        for refreshed in (False, True):
            try:
                token = solve(config["sitekey"], config["siteurl"], timeout=config["solver_timeout"])
                result = post_local_result(config["post_url"], job["nric"], token, timeout=config["post_timeout"])
                break
            except Exception as exc:
                if refreshed or not _is_config_error(exc):
                    raise
                config.update(_worker_config(force_refresh=True))
                summary["config_refreshed"] += 1
                summary["config_source"] = config["config_source"]
        status = int(result.get("status", 0))
        if 200 <= status < 300:
            job_repository.mark_job_success(job["ulid"], status, result.get("body", {}))
            summary["success"] += 1
            return
        summary["non_2xx"] += 1
        _mark_failed(job, f"upstream returned HTTP {status}", summary)
    except Exception as exc:
        summary["exceptions"] += 1
        _mark_failed(job, f"{type(exc).__name__}: {str(exc)[:200]}", summary)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Process pending CapSolve jobs.")
    parser.add_argument("--limit", type=int, default=_env_int("JOB_BATCH_LIMIT", 50))
    parser.add_argument(
        "--reset-stale-minutes",
        type=int,
        default=_env_int("JOB_RESET_STALE_MINUTES", 30),
        help="Reset stale processing jobs older than this many minutes; 0 disables.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview pending jobs without claiming or solving.")
    args = parser.parse_args()

    if args.dry_run:
        jobs = _preview_pending_jobs(args.limit)
        print(json.dumps({
            "dry_run": True,
            "pending": len(jobs),
            "jobs": [
                {
                    "ulid": _mask(job["ulid"]),
                    "attempts": job["attempts"],
                    "max_attempts": job["max_attempts"],
                }
                for job in jobs
            ],
        }))
        return 0

    config = _worker_config()
    summary = {
        "claimed": 0,
        "success": 0,
        "failed": 0,
        "retried": 0,
        "exceptions": 0,
        "non_2xx": 0,
        "reset_stale": 0,
        "config_source": config["config_source"],
        "config_refreshed": 0,
    }

    if args.reset_stale_minutes > 0:
        summary["reset_stale"] = job_repository.reset_stale_processing_jobs(args.reset_stale_minutes)

    jobs = job_repository.claim_pending_jobs(args.limit)
    summary["claimed"] = len(jobs)
    for job in jobs:
        _process_job(job, config, summary)

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
