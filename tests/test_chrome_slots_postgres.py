from __future__ import annotations

import concurrent.futures
import os
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import psycopg2

import chrome_slots
import database
import job_repository
import process_jobs
import service
from tests.test_queue_postgres import TEST_DATABASE_URL, safe_test_connection_kwargs


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set; disposable PostgreSQL slot tests skipped")
class Phase4PostgresSlotsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection_kwargs = safe_test_connection_kwargs(TEST_DATABASE_URL)
        cls.schema = "capsolve_phase4_" + uuid.uuid4().hex
        if not re.fullmatch(r"[a-z0-9_]+", cls.schema):
            raise RuntimeError("invalid owned test schema")
        conn = psycopg2.connect(**cls.connection_kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{cls.schema}"')
                cursor.execute(
                    f"""CREATE TABLE "{cls.schema}".budi95_jobs (
                    id BIGSERIAL PRIMARY KEY, ulid VARCHAR(32) NOT NULL UNIQUE, nric VARCHAR(32) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending', response_status_code INTEGER,
                    response_body JSONB, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ, processed_at TIMESTAMPTZ,
                    CONSTRAINT status_check CHECK (status IN ('pending','processing','success','failed')))
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

    def connection(self):
        return psycopg2.connect(**self.connection_kwargs, options=f"-c search_path={self.schema}")

    def test_two_slots_allow_api_and_worker_and_third_opens_no_chrome(self) -> None:
        chrome_opened: list[str] = []

        def enter(label: str):
            slot = chrome_slots.try_acquire(2)
            if slot is not None:
                chrome_opened.append(label)
            return slot

        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                api_slot, worker_slot = list(executor.map(enter, ("api", "worker")))
            third = enter("third")
            self.assertTrue(api_slot and worker_slot)
            self.assertIsNone(third)
            self.assertEqual(set(chrome_opened), {"api", "worker"})
            api_slot.release()
            worker_slot.release()
            reacquired = [chrome_slots.try_acquire(2), chrome_slots.try_acquire(2)]
            self.assertTrue(all(reacquired))
            for slot in reacquired:
                slot.release()

    def test_api_and_worker_share_one_external_display_and_aggregate_slots(self) -> None:
        settings = SimpleNamespace(
            sync_queue_max_waiting=0,
            global_chrome_slots=2,
            job_queue_retry_after_seconds=1,
            solver_timeout=1,
            local_post_timeout=1,
        )
        entered: list[str] = []
        release = __import__("threading").Event()

        def api_contender():
            slot = service._acquire_sync_slot()
            try:
                entered.append("api")
                release.wait(1)
            finally:
                service._release_sync_slot(slot)

        def worker_contender():
            slot = chrome_slots.try_acquire(2)
            try:
                entered.append("worker")
                release.wait(1)
            finally:
                slot.release()

        with mock.patch.dict(os.environ, {"DISPLAY": ":99", "ENABLE_XVFB_VIRTUAL_DISPLAY": "false"}), mock.patch.object(
            database, "get_connection", side_effect=self.connection
        ), mock.patch.object(service, "_settings", settings), mock.patch.object(service, "MAX_WORKERS", 2), mock.patch.object(
            service, "_active_count", 0
        ), mock.patch.object(service, "_queued_count", 0), mock.patch.object(service.subprocess, "Popen") as popen:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(api_contender), executor.submit(worker_contender)]
                for _ in range(100):
                    if len(entered) == 2:
                        break
                    __import__("time").sleep(0.005)
                self.assertEqual(set(entered), {"api", "worker"})
                self.assertIsNone(chrome_slots.try_acquire(2))
                release.set()
                for future in futures:
                    future.result()
        popen.assert_not_called()

    def test_actual_api_pipeline_and_worker_main_claim_finalize_with_real_profiles(self) -> None:
        settings = SimpleNamespace(
            sync_queue_max_waiting=0, global_chrome_slots=2, job_queue_retry_after_seconds=1,
            solver_timeout=1, local_post_timeout=1, job_batch_limit=1, job_reset_stale_minutes=0,
        )
        entered = threading.Event()
        release = threading.Event()
        profiles: list[str] = []
        lock = threading.Lock()
        ulid = uuid.uuid4().hex
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO budi95_jobs (ulid,nric,max_attempts) VALUES (%s,%s,1)", (ulid, "worker"))

        class Browser:
            def __init__(self, profile):
                self.profile = profile
                self._process = None

            async def get(self, url):
                return __import__("tests.test_chrome_runtime", fromlist=["FakePage"]).FakePage()

            async def aclose(self):
                return None

        async def start(owner):
            with lock:
                profiles.append(owner.profile_dir)
                if len(profiles) == 2:
                    entered.set()
            release.wait(2)
            return Browser(owner.profile_dir)

        config = SimpleNamespace(
            turnstile_sitekey="key", turnstile_siteurl="url", local_post_url="post", source="test"
        )
        output = __import__("io").StringIO()
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            os.mkdir(base, 0o700)
            patches = (
                mock.patch.dict(os.environ, {"DISPLAY": ":99", "TS_PROFILE_DIR": base}),
                mock.patch.object(database, "get_connection", side_effect=self.connection),
                mock.patch.object(service, "_settings", settings), mock.patch.object(service, "MAX_WORKERS", 2),
                mock.patch.object(service, "_active_count", 0), mock.patch.object(service, "_queued_count", 0),
                mock.patch.object(service, "resolve_budi95_config", return_value=config),
                mock.patch.object(process_jobs, "resolve_budi95_config", return_value=config),
                mock.patch.object(service, "solve", side_effect=lambda *a, **k: __import__("solver").solve(*a, **k)),
                mock.patch.object(process_jobs, "solve", side_effect=lambda *a, **k: __import__("solver").solve(*a, **k)),
                mock.patch.object(__import__("solver"), "_find_chrome", return_value="chrome"),
                mock.patch.object(service, "post_local_result", return_value={"status": 200, "body": {"ok": "api"}}),
                mock.patch.object(process_jobs, "post_local_result", return_value={"status": 200, "body": {"ok": "worker"}}),
                mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(process_jobs, "load_settings", return_value=settings),
                mock.patch("sys.argv", ["capsolve-worker"]), mock.patch("sys.stdout", output),
                mock.patch.object(__import__("solver").asyncio, "sleep", new=mock.AsyncMock()),
            )
            with __import__("contextlib").ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                low_solve = stack.enter_context(mock.patch.object(__import__("solver"), "_start_browser", side_effect=start))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    api = executor.submit(service.solve_endpoint, "api", 1, 1, None)
                    worker = executor.submit(process_jobs.main)
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(service.HTTPException) as raised:
                        service.solve_endpoint("third", 1, 1, None)
                    self.assertEqual(raised.exception.status_code, 429)
                    self.assertEqual(low_solve.call_count, 2)
                    release.set()
                    self.assertEqual(api.result()["body"]["ok"], "api")
                    self.assertEqual(worker.result(), 0)
            self.assertEqual(list(os.scandir(base)), [])
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT status, attempts, response_body FROM budi95_jobs WHERE ulid=%s", (ulid,))
            self.assertEqual(cursor.fetchone(), ("success", 1, {"ok": "worker"}))
        self.assertEqual(len(set(profiles)), 2)
        self.assertEqual((service._active_count, service._queued_count), (0, 0))

    def test_keys_are_in_documented_range_and_distinct_from_queue_admission(self) -> None:
        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            slots = [chrome_slots.try_acquire(3) for _ in range(3)]
        try:
            keys = {slot.key for slot in slots if slot}
            self.assertEqual(len(keys), 3)
            self.assertTrue(all(chrome_slots.CHROME_SLOT_BASE_KEY <= key < chrome_slots.CHROME_SLOT_BASE_KEY + 3 for key in keys))
            self.assertNotIn(job_repository.QUEUE_ADMISSION_LOCK_KEY, keys)
        finally:
            for slot in slots:
                if slot:
                    slot.release()

    def test_acquisition_failure_is_closed_and_release_failure_still_closes(self) -> None:
        class BrokenCursor:
            def __enter__(self):
                raise RuntimeError("sensitive marker")

            def __exit__(self, *args):
                return False

        class BrokenConnection:
            def __init__(self):
                self.closed = False

            def cursor(self):
                return BrokenCursor()

            def close(self):
                self.closed = True

        connection = BrokenConnection()
        with mock.patch.object(database, "get_connection", return_value=connection), self.assertRaisesRegex(
            chrome_slots.ChromeSlotError, "acquisition failed"
        ):
            chrome_slots.try_acquire(1)
        self.assertTrue(connection.closed)

        connection = BrokenConnection()
        with self.assertRaisesRegex(chrome_slots.ChromeSlotError, "release failed"):
            chrome_slots.ChromeSlot(connection, 0, chrome_slots.CHROME_SLOT_BASE_KEY).release()
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
