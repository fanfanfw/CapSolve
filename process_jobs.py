from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import statistics
import sys
import time
import urllib.error
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import RealDictCursor

import chrome_slots
from config_resolver import resolve_budi95_config
import database
import job_repository
from settings import Settings, load_settings
from solver import load_dotenv, post_local_result, solve


Job = dict[str, Any]


class WorkerFinalizationError(RuntimeError):
    pass


class WorkerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid worker arguments") from None


@contextlib.contextmanager
def _silence_subordinate_output():
    # File-descriptor redirection is process-global; this CLI processes jobs serially.
    for stream in (sys.stdout, sys.stderr):
        stream.flush()
    saved_stdout = os.dup(1)
    try:
        saved_stderr = os.dup(2)
    except Exception:
        os.close(saved_stdout)
        raise
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except Exception:
        os.close(saved_stdout)
        os.close(saved_stderr)
        raise
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                yield
            finally:
                sink.flush()
    finally:
        try:
            os.dup2(saved_stdout, 1)
        finally:
            try:
                os.dup2(saved_stderr, 2)
            finally:
                os.close(saved_stdout)
                os.close(saved_stderr)
                os.close(null_fd)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _preview_pending_jobs(limit: int) -> list[Job]:
    with contextlib.closing(database.get_connection()) as conn, conn:
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


def _worker_config(settings: Settings, force_refresh: bool = False) -> dict[str, Any]:
    budi95_config = resolve_budi95_config(force_refresh=force_refresh)
    return {
        "sitekey": budi95_config.turnstile_sitekey,
        "siteurl": budi95_config.turnstile_siteurl,
        "post_url": budi95_config.local_post_url,
        "config_source": budi95_config.source,
        "solver_timeout": settings.solver_timeout,
        "post_timeout": settings.local_post_timeout,
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


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "solver_timeout"
    if isinstance(exc, (urllib.error.URLError, socket.gaierror, ConnectionError)):
        return "upstream_unavailable"
    if any(word in type(exc).__name__.lower() for word in ("browser", "chrome", "nodriver")):
        return "browser_error"
    return "internal_error"


def _mark_failed(job: Job, error_code: str, summary: dict[str, Any]) -> str:
    try:
        finalized = job_repository.mark_job_failed(job["ulid"], job["attempts"], error_code)
    except Exception:
        raise WorkerFinalizationError("controlled finalization failure") from None
    if not finalized:
        summary["lost_claim"] += 1
        return "lost_claim"
    if _will_retry(job):
        summary["retried"] += 1
        return "retried"
    summary["failed"] += 1
    return "failed"


def _process_job(job: Job, config: dict[str, Any], summary: dict[str, Any], settings: Settings) -> None:
    started = time.monotonic()
    try:
        for refreshed in (False, True):
            try:
                with _silence_subordinate_output():
                    token = solve(config["sitekey"], config["siteurl"], timeout=config["solver_timeout"])
                    result = post_local_result(config["post_url"], job["nric"], token, timeout=config["post_timeout"])
                break
            except Exception as exc:
                if refreshed or not _is_config_error(exc):
                    raise
                with _silence_subordinate_output():
                    config.update(_worker_config(settings, force_refresh=True))
                summary["config_refreshed"] += 1
                summary["config_source"] = config["config_source"]
        status = int(result.get("status", 0))
        if 200 <= status < 300:
            try:
                finalized = job_repository.mark_job_success(
                    job["ulid"], job["attempts"], status, result.get("body", {})
                )
            except Exception:
                raise WorkerFinalizationError("controlled finalization failure") from None
            if finalized:
                summary["success"] += 1
            else:
                summary["lost_claim"] += 1
                summary["event"] = "worker_failure"
                summary["error_code"] = "lost_claim"
                summary["outcome"] = "lost_claim"
            return
        summary["non_2xx"] += 1
        summary["outcome"] = _mark_failed(job, "upstream_unavailable", summary)
        summary["event"] = "worker_failure"
        summary["error_code"] = "upstream_unavailable"
    except WorkerFinalizationError:
        raise
    except Exception as exc:
        summary["exceptions"] += 1
        error_code = _failure_code(exc)
        summary["outcome"] = _mark_failed(job, error_code, summary)
        summary["event"] = "worker_failure"
        summary["error_code"] = error_code
    finally:
        summary.setdefault("solve_duration_seconds", []).append(round(time.monotonic() - started, 3))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit_summary(summary: dict[str, Any], exit_status: int, stale_minutes: int | None) -> None:
    durations = sorted(summary.pop("solve_duration_seconds", []))
    summary["solve_duration_median_seconds"] = statistics.median(durations) if durations else None
    summary["solve_duration_p95_seconds"] = durations[max(0, (len(durations) * 95 + 99) // 100 - 1)] if durations else None
    if stale_minutes is not None:
        try:
            summary.update(job_repository.queue_metrics(stale_minutes))
            summary["queue_metrics_available"] = True
        except BaseException:
            pass
    summary.setdefault("event", "worker_complete" if exit_status == 0 else "worker_error")
    summary["exit_status"] = exit_status
    summary["completed_at"] = _utc_now()
    print(json.dumps(summary))


def main() -> int:
    summary = {
        "invoked_at": _utc_now(),
        "claimed": 0,
        "success": 0,
        "failed": 0,
        "retried": 0,
        "lost_claim": 0,
        "exceptions": 0,
        "non_2xx": 0,
        "reset_stale": 0,
        "config_source": None,
        "config_refreshed": 0,
        "queue_depth": None,
        "pending_count": None,
        "processing_count": None,
        "oldest_pending_age_seconds": None,
        "stale_processing_count": None,
        "queue_metrics_available": False,
        "solve_duration_seconds": [],
    }
    stale_minutes = None
    try:
        load_dotenv()
        parser = WorkerArgumentParser(description="Process pending CapSolve jobs.", add_help=False)
        parser.add_argument("--help", action="store_true")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--reset-stale-minutes", type=int)
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args()
        if args.help:
            summary["event"] = "worker_help"
            summary["help"] = "Options: --limit N, --reset-stale-minutes N, --dry-run"
            _emit_summary(summary, 0, None)
            return 0

        settings = load_settings("worker")
        args.limit = args.limit if args.limit is not None else settings.job_batch_limit
        args.reset_stale_minutes = (
            args.reset_stale_minutes
            if args.reset_stale_minutes is not None
            else settings.job_reset_stale_minutes
        )
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        if args.reset_stale_minutes < 0:
            parser.error("--reset-stale-minutes must be at least 0")
        stale_minutes = args.reset_stale_minutes

        if args.dry_run:
            jobs = _preview_pending_jobs(args.limit)
            summary.update({
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
            })
            _emit_summary(summary, 0, stale_minutes)
            return 0

        if args.reset_stale_minutes > 0:
            summary["reset_stale"] = job_repository.reset_stale_processing_jobs(args.reset_stale_minutes)

        seen_ids: set[int] = set()
        config: dict[str, Any] | None = None
        while len(seen_ids) < args.limit:
            slot = chrome_slots.try_acquire(settings.global_chrome_slots)
            if slot is None:
                summary["event"] = "worker_busy"
                summary["error_code"] = "chrome_slot_unavailable"
                break
            try:
                job = job_repository.claim_pending_job(seen_ids)
                if not job:
                    break
                seen_ids.add(job["id"])
                summary["claimed"] += 1
                if config is None:
                    try:
                        with _silence_subordinate_output():
                            config = _worker_config(settings)
                    except BaseException:
                        _mark_failed(job, "internal_error", summary)
                        raise
                    summary["config_source"] = config["config_source"]
                _process_job(job, config, summary, settings)
            finally:
                slot.release()
    except BaseException:
        summary.pop("outcome", None)
        summary["event"] = "worker_error"
        summary["error_code"] = "internal_error"
        _emit_summary(summary, 1, stale_minutes)
        return 1

    _emit_summary(summary, 0, stale_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
