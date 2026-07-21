from __future__ import annotations

import contextlib
import os
import re
import uuid
from typing import Any

from psycopg2.extras import Json, RealDictCursor

import database


Job = dict[str, Any]
# Reserved for queue admission; future Chrome slot locks must use a different key range.
QUEUE_ADMISSION_LOCK_KEY = 1_128_352_337
FAILURE_CODES = {"solver_timeout", "browser_error", "upstream_unavailable", "internal_error"}


class QueueFullError(Exception):
    pass


def new_ulid() -> str:
    return uuid.uuid4().hex


def create_job(nric: str, max_attempts: int | None = None, capacity: int | None = None, client_id: str = "legacy", credential_id: str = "legacy") -> Job:
    ulid = new_ulid()
    if max_attempts is None:
        max_attempts = int(os.environ.get("JOB_MAX_ATTEMPTS") or 3)
    if capacity is None:
        capacity = int(os.environ.get("JOB_QUEUE_CAPACITY") or 100)
    conn = database.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (QUEUE_ADMISSION_LOCK_KEY,))
                cursor.execute("SELECT COUNT(*) FROM budi95_jobs WHERE status IN ('pending', 'processing')")
                if cursor.fetchone()["count"] >= capacity:
                    raise QueueFullError
                cursor.execute(
                    """
                    INSERT INTO budi95_jobs (ulid, nric, max_attempts, api_client_id, api_credential_id)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (ulid, nric, max_attempts, client_id, credential_id),
                )
                return dict(cursor.fetchone())
    finally:
        conn.close()


def _connection():
    return contextlib.closing(database.get_connection())


def get_job_by_ulid(ulid: str) -> Job | None:
    with _connection() as conn, conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM budi95_jobs WHERE ulid = %s",
                (ulid,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def claim_pending_job(exclude_ids: set[int] | None = None) -> Job | None:
    excluded = list(exclude_ids or ())
    with _connection() as conn, conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH picked AS (
                  SELECT id
                  FROM budi95_jobs
                  WHERE status = 'pending'
                    AND attempts < max_attempts
                    AND NOT (id = ANY(%s))
                  ORDER BY created_at ASC
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE budi95_jobs j
                SET status = 'processing',
                    attempts = attempts + 1,
                    started_at = NOW(),
                    processed_at = NULL,
                    updated_at = NOW()
                FROM picked
                WHERE j.id = picked.id
                RETURNING j.*
                """,
                (excluded,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def mark_job_success(ulid: str, expected_attempt: int, status_code: int, body: dict) -> bool:
    with _connection() as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE budi95_jobs
                SET status = 'success',
                    response_status_code = %s,
                    response_body = %s,
                    error = NULL,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE ulid = %s
                  AND status = 'processing'
                  AND attempts = %s
                """,
                (status_code, Json(body), ulid, expected_attempt),
            )
            return cursor.rowcount == 1


def mark_job_failed(ulid: str, expected_attempt: int, error_code: str) -> bool:
    if error_code not in FAILURE_CODES:
        raise ValueError("unsupported failure code")
    with _connection() as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE budi95_jobs
                SET status = CASE
                        WHEN attempts < max_attempts THEN 'pending'
                        ELSE 'failed'
                    END,
                    error = %s,
                    processed_at = CASE
                        WHEN attempts >= max_attempts THEN NOW()
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE ulid = %s
                  AND status = 'processing'
                  AND attempts = %s
                """,
                (error_code, ulid, expected_attempt),
            )
            return cursor.rowcount == 1


def reset_stale_processing_jobs(older_than_minutes: int) -> int:
    with _connection() as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE budi95_jobs
                SET status = CASE
                        WHEN attempts < max_attempts THEN 'pending'
                        ELSE 'failed'
                    END,
                    error = CASE
                        WHEN attempts >= max_attempts THEN 'solver_timeout'
                        ELSE error
                    END,
                    processed_at = CASE
                        WHEN attempts >= max_attempts THEN NOW()
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE status = 'processing'
                  AND started_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (older_than_minutes,),
            )
            return cursor.rowcount


def queue_metrics(stale_minutes: int) -> dict[str, int | float | None]:
    with contextlib.closing(database.get_connection(statement_timeout=database.db_connect_timeout())) as conn, conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status IN ('pending', 'processing')) AS queue_depth,
                  COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
                  COUNT(*) FILTER (WHERE status = 'processing') AS processing_count,
                  EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (WHERE status = 'pending'))) AS oldest_pending_age_seconds,
                   COUNT(*) FILTER (
                     WHERE status = 'processing'
                       AND %s > 0
                       AND started_at < NOW() - (%s * INTERVAL '1 minute')
                   ) AS stale_processing_count,
                   COALESCE((
                     SELECT jsonb_object_agg(api_client_id, jsonb_build_object(
                       'pending', pending,
                       'processing', processing,
                       'depth', pending + processing
                     ))
                     FROM (
                       SELECT api_client_id,
                              COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                              COUNT(*) FILTER (WHERE status = 'processing') AS processing
                       FROM budi95_jobs
                       WHERE status IN ('pending', 'processing')
                       GROUP BY api_client_id
                     ) grouped_clients
                   ), '{}'::jsonb) AS clients
                 FROM budi95_jobs
                 """,
                 (stale_minutes, stale_minutes),
             )
            row = dict(cursor.fetchone())
            raw_clients = row.pop("clients", {})
            clients = {
                client_id: {
                    "pending": int(values["pending"]),
                    "processing": int(values["processing"]),
                    "depth": int(values["depth"]),
                }
                for client_id, values in raw_clients.items()
                if isinstance(client_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", client_id) and isinstance(values, dict)
            }
            age = row["oldest_pending_age_seconds"]
            return {
                "queue_depth": int(row["queue_depth"]),
                "pending_count": int(row["pending_count"]),
                "processing_count": int(row["processing_count"]),
                "oldest_pending_age_seconds": max(0.0, float(age)) if age is not None else None,
                "stale_processing_count": int(row["stale_processing_count"]),
                "clients": clients,
            }


def public_submit_response(job: Job) -> dict:
    return {"status": True, "id_no": job["nric"], "ulid": job["ulid"], "message": "OK"}


def public_result_response(job: Job | None) -> dict:
    if not job:
        return {"status": False, "job_status": None, "message": "Unable to process subsidy", "data": None}
    job_status = job.get("status")
    if job_status in {"pending", "processing"}:
        return {"status": True, "job_status": job_status, "message": "OK", "data": None}
    if job_status == "success":
        return {"status": True, "job_status": "completed", "message": "OK", "data": job.get("response_body")}
    if job_status == "failed":
        return {
            "status": False,
            "job_status": "failed",
            "message": "Unable to process subsidy",
            "data": {"error_code": "job_failed", "message": "Unable to process subsidy"},
        }
    return {"status": False, "job_status": job_status, "message": "Unable to process subsidy", "data": None}
