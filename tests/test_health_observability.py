from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import psycopg2

import database
import job_repository
import process_jobs
import service
import worker_freshness
from tests.test_api_contract import BASELINE, request
from tests.test_queue_postgres import safe_test_connection_kwargs


CANARY = "phase6-canary db.internal user password postgresql://user:password@db.internal/name"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class Cursor:
    def __init__(self, row=(1,)):
        self.row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, cursor=None):
        self._cursor = cursor or Cursor()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def close(self):
        self.closed = True


class Phase6ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(db_connect_timeout=1)

    def test_health_exact_golden_never_checks_database(self):
        with mock.patch.object(service, "_settings", self.settings), mock.patch.object(
            service.database, "is_ready", side_effect=AssertionError("database called")
        ) as ready:
            status_code, headers, body = request("GET", "/api/health", api_key=None)
        self.assertEqual(
            {"status": status_code, "headers": {"content-type": headers["content-type"]}, "body": body},
            BASELINE["health"],
        )
        ready.assert_not_called()

    def test_ready_is_public_but_host_gated_and_queue_full_is_irrelevant(self):
        with mock.patch.object(service, "_settings", self.settings), mock.patch.object(
            service.database, "is_ready", return_value=True
        ) as ready, mock.patch.object(
            job_repository, "create_job", side_effect=job_repository.QueueFullError
        ) as admission:
            response = request("GET", "/api/ready", api_key=None)
        self.assertEqual(response[0], 200)
        self.assertEqual(response[2], {"status": "ready"})
        ready.assert_called_once_with(1)
        admission.assert_not_called()

        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/ready",
            "raw_path": b"/api/ready",
            "query_string": b"",
            "headers": [(b"host", b"wrong.example.invalid")],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        }
        asyncio.run(service.app(scope, receive, send))
        self.assertEqual(next(message["status"] for message in messages if message["type"] == "http.response.start"), 400)

    def test_ready_db_failure_is_fast_generic_and_silent(self):
        output = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(service, "_settings", self.settings), mock.patch.object(
            service.database, "is_ready", side_effect=RuntimeError(CANARY)
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = request("GET", "/api/ready", api_key=None)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response[0], 503)
        self.assertEqual(response[2], {"status": "unavailable"})
        self.assertNotIn(CANARY, json.dumps(response) + output.getvalue())

    def test_real_unused_local_port_failure_is_fast_generic_and_secret_free(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        canaries = {"DB_HOST": "127.0.0.1", "DB_PORT": str(port), "DB_NAME": "readiness_canary", "DB_USER": "readiness_user", "DB_PASSWORD": "readiness_password"}
        output = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(service, "_settings", self.settings), mock.patch.dict(os.environ, canaries, clear=False), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = request("GET", "/api/ready", api_key=None)
        rendered = json.dumps(response) + output.getvalue()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response[0:3:2], (503, {"status": "unavailable"}))
        for value in (*canaries.values(), "connection refused", "psycopg2"):
            self.assertNotIn(value, rendered.lower())

    def test_database_probe_bounds_connect_and_query_and_closes_on_all_paths(self):
        for failure in (None, RuntimeError(CANARY), KeyboardInterrupt(CANARY)):
            cursor = Cursor()
            if failure is not None:
                cursor.execute = mock.Mock(side_effect=failure)
            connection = Connection(cursor)
            with self.subTest(failure=type(failure).__name__ if failure else "success"), mock.patch.object(
                database, "get_connection", return_value=connection
            ) as connect:
                if failure is None:
                    self.assertTrue(database.is_ready(2))
                else:
                    with self.assertRaises(type(failure)):
                        database.is_ready(2)
            connect.assert_called_once_with(connect_timeout=2, statement_timeout=2)
            self.assertTrue(connection.closed)


class Phase6WorkerSummaryTest(unittest.TestCase):
    def test_queue_metrics_mapping_and_connection_closure(self):
        row = {
            "queue_depth": 5,
            "pending_count": 3,
            "processing_count": 2,
            "oldest_pending_age_seconds": 12.5,
            "stale_processing_count": 1,
            "clients": {},
        }
        connection = Connection(Cursor(row))
        with mock.patch.object(job_repository.database, "get_connection", return_value=connection):
            metrics = job_repository.queue_metrics(30)
        self.assertEqual(metrics, row)
        self.assertTrue(connection.closed)
        query, parameters = connection._cursor.executed[0]
        self.assertNotIn("nric", query.lower())
        self.assertEqual(parameters, (30, 30))

    def test_zero_stale_threshold_is_explicitly_disabled(self):
        row = {
            "queue_depth": 1,
            "pending_count": 0,
            "processing_count": 1,
            "oldest_pending_age_seconds": None,
            "stale_processing_count": 0,
            "clients": {},
        }
        connection = Connection(Cursor(row))
        with mock.patch.object(job_repository.database, "get_connection", return_value=connection):
            self.assertEqual(job_repository.queue_metrics(0)["stale_processing_count"], 0)
        self.assertIn("%s > 0", connection._cursor.executed[0][0])

    def test_parseable_summary_exposes_freshness_metrics_and_exit_status(self):
        summary = {"claimed": 1}
        metrics = {
            "queue_depth": 4,
            "pending_count": 3,
            "processing_count": 1,
            "oldest_pending_age_seconds": 75.0,
            "stale_processing_count": 0,
        }
        output = io.StringIO()
        with mock.patch.object(process_jobs, "_utc_now", return_value="2026-07-16T12:00:00Z"), mock.patch.object(
            process_jobs.job_repository, "queue_metrics", return_value=metrics
        ), contextlib.redirect_stdout(output):
            process_jobs._emit_summary(summary, 0, 30)
        record = json.loads(output.getvalue())
        self.assertEqual(record["completed_at"], "2026-07-16T12:00:00Z")
        self.assertEqual(record["exit_status"], 0)
        self.assertEqual(record["event"], "worker_complete")
        self.assertTrue(record["queue_metrics_available"])
        self.assertEqual({key: record[key] for key in metrics}, metrics)

    def test_invalid_arguments_still_emit_timestamp_and_exit_status(self):
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=30, global_chrome_slots=1)
        output = io.StringIO()
        with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
            process_jobs, "load_settings", return_value=settings
        ), mock.patch.object(
            process_jobs.job_repository, "queue_metrics", return_value={}
        ), mock.patch("sys.argv", ["capsolve-worker", "--limit", "invalid"]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(process_jobs.main(), 1)
        record = json.loads(output.getvalue())
        self.assertEqual((record["event"], record["exit_status"]), ("worker_error", 1))
        for field in ("invoked_at", "completed_at"):
            parsed = datetime.fromisoformat(record[field].replace("Z", "+00:00"))
            self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_help_is_exactly_one_json_record_with_unavailable_metrics(self):
        output = io.StringIO()
        with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
            process_jobs, "load_settings", side_effect=AssertionError("settings called")
        ), mock.patch("sys.argv", ["capsolve-worker", "--help"]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(process_jobs.main(), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual((record["event"], record["exit_status"]), ("worker_help", 0))
        self.assertFalse(record["queue_metrics_available"])
        for field in ("queue_depth", "pending_count", "processing_count", "oldest_pending_age_seconds", "stale_processing_count"):
            self.assertIsNone(record[field])
        for field in ("invoked_at", "completed_at"):
            self.assertEqual(datetime.fromisoformat(record[field].replace("Z", "+00:00")).tzinfo, timezone.utc)

    def test_optional_metrics_failure_does_not_change_success_or_leak(self):
        summary = {
            "queue_depth": None,
            "pending_count": None,
            "processing_count": None,
            "oldest_pending_age_seconds": None,
            "stale_processing_count": None,
            "queue_metrics_available": False,
        }
        output = io.StringIO()
        with mock.patch.object(
            process_jobs.job_repository, "queue_metrics", side_effect=RuntimeError(CANARY)
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            process_jobs._emit_summary(summary, 0, 30)
        record = json.loads(output.getvalue())
        self.assertEqual((record["event"], record["exit_status"]), ("worker_complete", 0))
        self.assertFalse(record["queue_metrics_available"])
        self.assertNotIn(CANARY, output.getvalue())


class Phase6FreshnessCheckTest(unittest.TestCase):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    def record(self, **overrides) -> str:
        value = {
            "event": "worker_complete",
            "invoked_at": "2026-07-16T11:58:00Z",
            "completed_at": "2026-07-16T11:59:00Z",
            "exit_status": 0,
        }
        value.update(overrides)
        return json.dumps(value)

    def test_skips_malformed_and_unrelated_json_then_accepts_latest_success(self):
        lines = [
            "not json",
            json.dumps({"MESSAGE": "not json " + CANARY}),
            json.dumps({"event": "unrelated", "completed_at": "2026-07-16T11:59:30Z", "exit_status": 0}),
            json.dumps({"MESSAGE": self.record()}),
        ]
        self.assertTrue(worker_freshness.check(lines, 120, self.now))

    def test_rejects_missing_stale_future_and_latest_failure(self):
        cases = (
            [],
            [self.record(completed_at="2026-07-16T11:57:59Z")],
            [self.record(invoked_at="2026-07-16T12:01:00Z", completed_at="2026-07-16T12:01:00Z")],
            [self.record(), self.record(completed_at="2026-07-16T11:59:30Z", exit_status=1, event="worker_error")],
        )
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertFalse(worker_freshness.check(lines, 120, self.now))

    def test_help_is_not_a_worker_invocation_and_does_not_replace_last_worker_event(self):
        help_record = self.record(
            event="worker_help",
            invoked_at="2026-07-16T11:59:30Z",
            completed_at="2026-07-16T11:59:45Z",
        )
        self.assertFalse(worker_freshness.check([help_record], 120, self.now))
        self.assertTrue(worker_freshness.check([self.record(), help_record], 120, self.now))

    def test_equal_completed_timestamp_uses_later_input_record(self):
        success = self.record()
        failure = self.record(event="worker_error", exit_status=1)
        self.assertFalse(worker_freshness.check([success, failure], 120, self.now))
        self.assertTrue(worker_freshness.check([failure, success], 120, self.now))

    def test_invalid_cli_arguments_are_generic_and_do_not_echo_canary(self):
        cases = (
            (["worker-freshness", "--max-age-seconds", CANARY], ""),
            (["worker-freshness", "--max-age-seconds"], ""),
            (["worker-freshness", "--unknown", CANARY], ""),
            (["worker-freshness", "--max-age-seconds", "0"], ""),
            (
                ["worker-freshness", "--max-age-seconds", str(worker_freshness.MAX_AGE_SECONDS + 1)],
                self.record() + "\n",
            ),
            (["worker-freshness", "--max-age-seconds", "9" * 1000], self.record() + "\n"),
        )
        for arguments, stdin in cases:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.subTest(arguments=arguments[1:]), mock.patch("sys.argv", arguments), mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(worker_freshness.main(), 1)
            self.assertEqual(stdout.getvalue(), '{"worker_fresh": false}\n')
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(CANARY, stdout.getvalue())

    def test_requires_explicit_utc_timestamps_and_valid_order(self):
        accepted = ("2026-07-16T11:59:00Z", "2026-07-16T11:59:00+00:00")
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(worker_freshness.check([self.record(completed_at=value)], 120, self.now))
        rejected = (
            "2026-07-16T11:59:00",
            "2026-07-16T13:59:00+02:00",
            "invalid",
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(worker_freshness.check([self.record(completed_at=value)], 120, self.now))
        self.assertFalse(worker_freshness.check([
            self.record(invoked_at="2026-07-16T11:59:30Z", completed_at="2026-07-16T11:59:00Z")
        ], 120, self.now))


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set; disposable PostgreSQL Phase 6 tests skipped")
class Phase6PostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection_kwargs = safe_test_connection_kwargs(TEST_DATABASE_URL)
        cls.schema = "capsolve_phase6_" + uuid.uuid4().hex
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
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      started_at TIMESTAMPTZ,
                      processed_at TIMESTAMPTZ
                    )
                    """
                )
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        finally:
            conn.close()

    def connection(self):
        return psycopg2.connect(**self.connection_kwargs, options=f"-c search_path={self.schema}")

    def test_real_readiness_and_privacy_safe_metrics(self):
        readiness = self.connection()
        with mock.patch.object(database, "get_connection", return_value=readiness):
            self.assertTrue(database.is_ready(1))
        self.assertTrue(readiness.closed)

        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO budi95_jobs (ulid, nric, status, created_at)
                    VALUES (%s, %s, 'pending', NOW() - INTERVAL '90 seconds')
                    """,
                    (uuid.uuid4().hex, "privacy-test"),
                )
                cursor.execute(
                    """
                    INSERT INTO budi95_jobs (ulid, nric, status, started_at)
                    VALUES (%s, %s, 'processing', NOW() - INTERVAL '31 minutes')
                    """,
                    (uuid.uuid4().hex, "privacy-test"),
                )
        finally:
            conn.close()

        metrics_connection = self.connection()
        with mock.patch.object(database, "get_connection", return_value=metrics_connection), mock.patch.object(
            database, "db_connect_timeout", return_value=1
        ):
            metrics = job_repository.queue_metrics(30)
        self.assertTrue(metrics_connection.closed)
        self.assertEqual(
            (metrics["queue_depth"], metrics["pending_count"], metrics["processing_count"], metrics["stale_processing_count"]),
            (2, 1, 1, 1),
        )
        self.assertGreaterEqual(metrics["oldest_pending_age_seconds"], 89)
        self.assertNotIn(CANARY, json.dumps(metrics))


if __name__ == "__main__":
    unittest.main()
