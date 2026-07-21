from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import re
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import psycopg2

import database
import job_repository
import process_jobs
from tests.test_queue_postgres import TEST_DATABASE_URL, safe_test_connection_kwargs


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set; disposable PostgreSQL worker tests skipped")
class Phase3PostgresWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection_kwargs = safe_test_connection_kwargs(TEST_DATABASE_URL)
        cls.schema = "capsolve_phase3_" + uuid.uuid4().hex
        if not re.fullmatch(r"[a-z0-9_]+", cls.schema):
            raise RuntimeError("invalid owned test schema")
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{cls.schema}"')
                cursor.execute(
                    f"""
                    CREATE TABLE "{cls.schema}".budi95_jobs (
                      id BIGSERIAL PRIMARY KEY,
                      ulid VARCHAR(32) NOT NULL UNIQUE,
                      nric VARCHAR(32) NOT NULL,
                      status VARCHAR(20) NOT NULL DEFAULT 'pending',
                      response_status_code INTEGER,
                      response_body JSONB,
                      error TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 3,
                      api_client_id VARCHAR(32) NOT NULL DEFAULT 'legacy',
                      api_credential_id VARCHAR(32) NOT NULL DEFAULT 'legacy',
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      started_at TIMESTAMPTZ,
                      processed_at TIMESTAMPTZ,
                      CONSTRAINT budi95_jobs_status_check CHECK (status IN ('pending', 'processing', 'success', 'failed'))
                    )
                    """
                )
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        finally:
            conn.close()

    def setUp(self) -> None:
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("TRUNCATE budi95_jobs RESTART IDENTITY")
        finally:
            conn.close()
        self.connections = []

    def connection(self, *args, **kwargs):
        del args, kwargs
        return psycopg2.connect(**self.connection_kwargs, options=f"-c search_path={self.schema}")

    def tracked_connection(self, *args, **kwargs):
        conn = self.connection(*args, **kwargs)
        self.connections.append(conn)
        return conn

    def insert(self, count: int, max_attempts: int = 3) -> None:
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                for index in range(count):
                    cursor.execute(
                        "INSERT INTO budi95_jobs (ulid, nric, max_attempts) VALUES (%s, %s, %s)",
                        (uuid.uuid4().hex, f"test-{index}", max_attempts),
                    )
        finally:
            conn.close()

    def row(self, ulid: str) -> dict:
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status, attempts, error, response_body, processed_at FROM budi95_jobs WHERE ulid = %s",
                    (ulid,),
                )
                status, attempts, error, response_body, processed_at = cursor.fetchone()
                return {
                    "status": status,
                    "attempts": attempts,
                    "error": error,
                    "response_body": response_body,
                    "processed_at": processed_at,
                }
        finally:
            conn.close()

    def assert_connections_closed(self) -> None:
        self.assertTrue(self.connections)
        self.assertTrue(all(conn.closed for conn in self.connections))

    def test_concurrent_claims_are_distinct_and_only_claimed_rows_process(self) -> None:
        self.insert(3)
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(lambda _: job_repository.claim_pending_job(), range(2)))

        self.assertTrue(all(claims))
        self.assertEqual(len({job["id"] for job in claims}), 2)
        self.assertEqual([job["attempts"] for job in claims], [1, 1])
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("SELECT status, COUNT(*) FROM budi95_jobs GROUP BY status")
                self.assertEqual(dict(cursor.fetchall()), {"pending": 1, "processing": 2})
        finally:
            conn.close()
        self.assert_connections_closed()

    def test_just_in_time_single_claim_leaves_only_active_job_processing(self) -> None:
        self.insert(4)
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection):
            claim = job_repository.claim_pending_job()
        self.assertIsNotNone(claim)
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("SELECT status, COUNT(*) FROM budi95_jobs GROUP BY status")
                self.assertEqual(dict(cursor.fetchall()), {"pending": 3, "processing": 1})
        finally:
            conn.close()
        self.assert_connections_closed()

    def test_stale_reset_fences_old_attempt_and_new_attempt_can_complete(self) -> None:
        self.insert(1)
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection):
            old = job_repository.claim_pending_job()
            conn = self.connection()
            try:
                with conn, conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE budi95_jobs SET started_at = NOW() - INTERVAL '2 minutes' WHERE ulid = %s",
                        (old["ulid"],),
                    )
            finally:
                conn.close()
            self.assertEqual(job_repository.reset_stale_processing_jobs(1), 1)
            new = job_repository.claim_pending_job()
            self.assertEqual(new["attempts"], old["attempts"] + 1)
            self.assertFalse(job_repository.mark_job_success(old["ulid"], old["attempts"], 200, {"old": True}))
            self.assertFalse(job_repository.mark_job_failed(old["ulid"], old["attempts"], "internal_error"))
            self.assertEqual(self.row(old["ulid"])["status"], "processing")
            self.assertTrue(job_repository.mark_job_success(new["ulid"], new["attempts"], 200, {"new": True}))

        row = self.row(old["ulid"])
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["response_body"], {"new": True})
        self.assert_connections_closed()

    def test_failed_retry_is_excluded_in_same_invocation_and_last_attempt_is_failed(self) -> None:
        self.insert(1, max_attempts=2)
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection):
            first = job_repository.claim_pending_job()
            self.assertTrue(job_repository.mark_job_failed(first["ulid"], first["attempts"], "internal_error"))
            self.assertIsNone(job_repository.claim_pending_job({first["id"]}))
            second = job_repository.claim_pending_job()
            self.assertEqual(second["attempts"], 2)
            self.assertTrue(job_repository.mark_job_failed(second["ulid"], second["attempts"], "internal_error"))
        row = self.row(first["ulid"])
        self.assertEqual((row["status"], row["attempts"], row["error"]), ("failed", 2, "internal_error"))
        self.assertIsNotNone(row["processed_at"])
        self.assert_connections_closed()

    def test_cli_boundary_finalizes_config_failure_and_keeps_unfinalized_rows_processing(self) -> None:
        canary = "NRIC_" + "MARK token=turnstile credential=https://user:pass@host/body"
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=0, global_chrome_slots=1)
        config = {
            "sitekey": "test",
            "siteurl": "test",
            "post_url": "test",
            "solver_timeout": 1,
            "post_timeout": 1,
            "config_source": "test",
        }
        cases = ("config_retry", "config_final", "success_finalization", "failure_finalization", "outer_processing")
        for mode in cases:
            with self.subTest(mode=mode):
                conn = self.connection()
                try:
                    with conn, conn.cursor() as cursor:
                        cursor.execute("TRUNCATE budi95_jobs RESTART IDENTITY")
                finally:
                    conn.close()
                self.insert(1, max_attempts=1 if mode == "config_final" else 3)
                output = io.StringIO()
                patches = [
                    mock.patch.object(process_jobs, "load_dotenv"),
                    mock.patch.object(process_jobs, "load_settings", return_value=settings),
                    mock.patch.object(process_jobs.chrome_slots, "try_acquire", return_value=mock.Mock(release=lambda: None)),
                    mock.patch("sys.argv", ["capsolve-worker"]),
                    mock.patch.object(database, "get_connection", side_effect=self.tracked_connection),
                ]
                if mode.startswith("config_"):
                    patches.append(mock.patch.object(process_jobs, "_worker_config", side_effect=RuntimeError(canary)))
                elif mode == "outer_processing":
                    patches.extend(
                        [
                            mock.patch.object(process_jobs, "_worker_config", return_value=config),
                            mock.patch.object(process_jobs, "_process_job", side_effect=RuntimeError(canary)),
                        ]
                    )
                else:
                    patches.extend(
                        [
                            mock.patch.object(process_jobs, "_worker_config", return_value=config),
                            mock.patch.object(
                                process_jobs,
                                "solve",
                                return_value="token" if mode == "success_finalization" else mock.DEFAULT,
                                side_effect=None if mode == "success_finalization" else RuntimeError(canary),
                            ),
                            mock.patch.object(
                                process_jobs,
                                "post_local_result",
                                return_value={"status": 200, "body": {}},
                            ),
                            mock.patch.object(
                                job_repository,
                                "mark_job_success" if mode == "success_finalization" else "mark_job_failed",
                                side_effect=RuntimeError(canary),
                            ),
                        ]
                    )
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        result = process_jobs.main()

                self.assertNotEqual(result, 0)
                rendered = output.getvalue()
                self.assertNotIn(canary, rendered)
                self.assertNotIn("Traceback", rendered)
                records = [json.loads(line) for line in rendered.splitlines()]
                self.assertEqual(len(records), 1)
                summary = records[0]
                self.assertEqual(summary["event"], "worker_error")
                self.assertEqual(summary["error_code"], "internal_error")
                self.assertEqual(summary["claimed"], 1)
                conn = self.connection()
                try:
                    with conn, conn.cursor() as cursor:
                        cursor.execute("SELECT status, attempts FROM budi95_jobs")
                        expected_status = {
                            "config_retry": "pending",
                            "config_final": "failed",
                        }.get(mode, "processing")
                        self.assertEqual(cursor.fetchone(), (expected_status, 1))
                finally:
                    conn.close()
        self.assert_connections_closed()

    def test_exception_canary_is_absent_from_db_worker_output_summary_and_public_result(self) -> None:
        self.insert(1, max_attempts=1)
        canary = "NRIC_" + "MARK token=turnstile credential=https://user:pass@host/body"
        summary = {
            "success": 0,
            "failed": 0,
            "retried": 0,
            "lost_claim": 0,
            "exceptions": 0,
            "non_2xx": 0,
            "config_refreshed": 0,
            "config_source": "test",
        }
        config = {
            "sitekey": "test",
            "siteurl": "test",
            "post_url": "test",
            "solver_timeout": 1,
            "post_timeout": 1,
        }
        output = io.StringIO()
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=0, global_chrome_slots=1)
        config["config_source"] = "test"
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection), mock.patch.object(
            process_jobs, "load_dotenv"
        ), mock.patch.object(process_jobs, "load_settings", return_value=settings), mock.patch.object(
            process_jobs.chrome_slots, "try_acquire", return_value=mock.Mock(release=lambda: None)
        ), mock.patch.object(process_jobs, "_worker_config", return_value=config
        ), mock.patch.object(process_jobs, "solve", side_effect=RuntimeError(canary)), mock.patch(
            "sys.argv", ["capsolve-worker"]
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(process_jobs.main(), 0)
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("SELECT ulid FROM budi95_jobs")
                ulid = cursor.fetchone()[0]
        finally:
            conn.close()
        with mock.patch.object(database, "get_connection", side_effect=self.tracked_connection):
            stored = job_repository.get_job_by_ulid(ulid)

        public = job_repository.public_result_response(stored)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        summary = records[0]
        self.assertEqual((summary["event"], summary["error_code"], summary["outcome"]), ("worker_failure", "internal_error", "failed"))
        rendered = json.dumps({"log": output.getvalue(), "summary": summary, "db_error": stored["error"], "public": public})
        self.assertNotIn(canary, rendered)
        self.assertEqual(stored["error"], "internal_error")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(public["data"]["error_code"], "job_failed")
        self.assert_connections_closed()


if __name__ == "__main__":
    unittest.main()
