from __future__ import annotations

import os
import uuid
from typing import Any

from psycopg2.extras import Json, RealDictCursor

import database


Job = dict[str, Any]


def new_ulid() -> str:
    return uuid.uuid4().hex


def create_job(nric: str, max_attempts: int | None = None) -> Job:
    ulid = new_ulid()
    if max_attempts is None:
        max_attempts = int(os.environ.get("JOB_MAX_ATTEMPTS") or 3)
    with database.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO budi95_jobs (ulid, nric, max_attempts)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (ulid, nric, max_attempts),
            )
            return dict(cursor.fetchone())


def get_job_by_ulid(ulid: str) -> Job | None:
    with database.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM budi95_jobs WHERE ulid = %s",
                (ulid,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def claim_pending_jobs(limit: int) -> list[Job]:
    with database.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH picked AS (
                  SELECT id
                  FROM budi95_jobs
                  WHERE status = 'pending'
                    AND attempts < max_attempts
                  ORDER BY created_at ASC
                  LIMIT %s
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE budi95_jobs j
                SET status = 'processing',
                    attempts = attempts + 1,
                    started_at = NOW(),
                    updated_at = NOW()
                FROM picked
                WHERE j.id = picked.id
                RETURNING j.*
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


def mark_job_success(ulid: str, status_code: int, body: dict) -> None:
    with database.get_connection() as conn:
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
                """,
                (status_code, Json(body), ulid),
            )


def mark_job_failed(ulid: str, error: str) -> None:
    with database.get_connection() as conn:
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
                        ELSE processed_at
                    END,
                    updated_at = NOW()
                WHERE ulid = %s
                """,
                (error, ulid),
            )


def reset_stale_processing_jobs(older_than_minutes: int) -> int:
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE budi95_jobs
                SET status = CASE
                        WHEN attempts < max_attempts THEN 'pending'
                        ELSE 'failed'
                    END,
                    error = CASE
                        WHEN attempts >= max_attempts THEN COALESCE(error, 'processing timed out')
                        ELSE error
                    END,
                    processed_at = CASE
                        WHEN attempts >= max_attempts THEN NOW()
                        ELSE processed_at
                    END,
                    updated_at = NOW()
                WHERE status = 'processing'
                  AND started_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (older_than_minutes,),
            )
            return cursor.rowcount


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
        return {"status": False, "job_status": "failed", "message": "Unable to process subsidy", "data": {"error": job.get("error")}}
    return {"status": False, "job_status": job_status, "message": "Unable to process subsidy", "data": None}
