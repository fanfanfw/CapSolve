from __future__ import annotations

import concurrent.futures
import contextlib
import os
from pathlib import Path
import re
import unittest
import uuid
from urllib.parse import urlsplit
from unittest import mock

import psycopg2

import database
import job_repository


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def safe_test_connection_kwargs(url: str) -> dict:
    """Build psycopg2 kwargs for disposable local Postgres only.

    Auth: role from PGUSER (default postgres). Password from PGPASSWORD when set
    (libpq also reads PGPASSWORD; we pass it explicitly so mocks and TCP auth match).
    URL must not embed userinfo — keeps secrets out of argv/logs.
    """
    parsed = urlsplit(url)
    database_name = parsed.path.removeprefix("/")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("TEST_DATABASE_URL must be a PostgreSQL URL")
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise RuntimeError("TEST_DATABASE_URL must not contain userinfo, query options, or fragments")
    if (
        parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or not re.fullmatch(r"[A-Za-z0-9_-]*test[A-Za-z0-9_-]*", database_name, re.IGNORECASE)
        or "/" in database_name
    ):
        raise RuntimeError("TEST_DATABASE_URL must target an explicitly named local test database")
    try:
        port = parsed.port or 5432
    except ValueError:
        raise RuntimeError("TEST_DATABASE_URL contains an invalid port") from None
    user = os.environ.get("PGUSER", "postgres").strip() or "postgres"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", user):
        raise RuntimeError("PGUSER must be a simple PostgreSQL role name")
    kwargs: dict = {"host": parsed.hostname, "port": port, "dbname": database_name, "user": user}
    password = os.environ.get("PGPASSWORD", "")
    if password:
        kwargs["password"] = password
    return kwargs


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set; disposable PostgreSQL race test skipped")
class Phase2PostgresRaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection_kwargs = safe_test_connection_kwargs(TEST_DATABASE_URL)
        cls.schema = "capsolve_phase2_" + uuid.uuid4().hex
        if not re.fullmatch(r"[a-z0-9_]+", cls.schema):
            raise RuntimeError("invalid owned test schema")
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{cls.schema}"')
                # Pre-attribution shape: prove 002 upgrades existing installs.
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
                      processed_at TIMESTAMPTZ,
                      CONSTRAINT budi95_jobs_status_check CHECK (status IN ('pending', 'processing', 'success', 'failed'))
                    )
                    """
                )
                cursor.execute(f'SET LOCAL search_path TO "{cls.schema}"')
                cursor.execute("INSERT INTO budi95_jobs (ulid, nric) VALUES (%s, %s)", ("f" * 32, "legacy-row"))
                cursor.execute((Path(__file__).parents[1] / "sql" / "002_job_attribution.sql").read_text(encoding="utf-8"))
        finally:
            conn.close()
        cls.backend_pids: set[int] = set()

    @classmethod
    def tearDownClass(cls) -> None:
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        finally:
            conn.close()

    def connection(self, *args, **kwargs):
        del args, kwargs
        conn = psycopg2.connect(**self.connection_kwargs, options=f"-c search_path={self.schema}")
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            self.backend_pids.add(cursor.fetchone()[0])
        return conn

    def test_migration_backfills_legacy_and_new_submit_stores_attribution(self) -> None:
        # Independent of capacity race: rebuild seed, re-run 002 for idempotency.
        with contextlib.closing(self.connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute("TRUNCATE budi95_jobs RESTART IDENTITY")
            cursor.execute("INSERT INTO budi95_jobs (ulid, nric) VALUES (%s, %s)", ("f" * 32, "legacy-row"))
            cursor.execute((Path(__file__).parents[1] / "sql" / "002_job_attribution.sql").read_text(encoding="utf-8"))
            cursor.execute((Path(__file__).parents[1] / "sql" / "002_job_attribution.sql").read_text(encoding="utf-8"))
            cursor.execute("SELECT api_client_id, api_credential_id FROM budi95_jobs WHERE ulid = %s", ("f" * 32,))
            self.assertEqual(cursor.fetchone(), ("legacy", "legacy"))
        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            created = job_repository.create_job("attributed", max_attempts=3, capacity=10, client_id="staging", credential_id="stg-app-a")
        with contextlib.closing(self.connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute("SELECT api_client_id, api_credential_id FROM budi95_jobs WHERE ulid = %s", (created["ulid"],))
            self.assertEqual(cursor.fetchone(), ("staging", "stg-app-a"))
        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            metrics = job_repository.queue_metrics(30)
        self.assertEqual(metrics["clients"]["staging"], {"pending": 1, "processing": 0, "depth": 1})

    def test_atomic_capacity_race_terminal_slots_and_capacity_reduction(self) -> None:
        # Drop setUpClass seed so capacity race starts from zero outstanding jobs.
        with contextlib.closing(self.connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute("TRUNCATE budi95_jobs RESTART IDENTITY")
        self.backend_pids.clear()

        def submit(index: int) -> bool:
            try:
                job_repository.create_job(f"test-{index}", max_attempts=3, capacity=3)
                return True
            except job_repository.QueueFullError:
                return False

        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                accepted = list(executor.map(submit, range(10)))
            self.assertEqual(sum(accepted), 3)
            self.assertGreaterEqual(len(self.backend_pids), 2)

            with contextlib.closing(self.connection()) as conn, conn, conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM budi95_jobs")
                self.assertEqual(cursor.fetchone()[0], 3)
                cursor.execute("UPDATE budi95_jobs SET status = 'success' WHERE id = (SELECT MIN(id) FROM budi95_jobs)")
                cursor.execute("UPDATE budi95_jobs SET status = 'failed' WHERE id = (SELECT MAX(id) FROM budi95_jobs)")

            self.assertTrue(submit(10))
            self.assertTrue(submit(11))
            with self.assertRaises(job_repository.QueueFullError):
                job_repository.create_job("capacity-lowered", max_attempts=3, capacity=1)

        with contextlib.closing(self.connection()) as conn, conn, conn.cursor() as cursor:
            cursor.execute("SELECT status, COUNT(*) FROM budi95_jobs GROUP BY status")
            self.assertEqual(dict(cursor.fetchall()), {"pending": 3, "success": 1, "failed": 1})
            cursor.execute("SELECT COUNT(*) FROM budi95_jobs")
            self.assertEqual(cursor.fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()
