from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import chrome_slots
import clientsend
import job_repository
import process_jobs
import service
from settings import load_settings
import solver
from tests.test_api_contract import API_KEY, request


class FakePage:
    async def evaluate(self, script: str):
        if "if (window._tsToken)" in script:
            return "test-token"
        return None


class FakeProcess:
    def __init__(self, *, wait_error=None):
        self.returncode = None
        self.wait_error = wait_error
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        if self.wait_error:
            raise self.wait_error
        self.returncode = 0


class FakeBrowser:
    def __init__(self, *, get_error: Exception | None = None, stop_error: Exception | None = None):
        self.get_error = get_error
        self.stop_error = stop_error
        self.stopped = False
        self._process = FakeProcess()

    async def get(self, url: str):
        if self.get_error:
            raise self.get_error
        return FakePage()

    async def aclose(self) -> None:
        self.stopped = True
        if self.stop_error:
            raise self.stop_error


class FakeSlot:
    def __init__(self):
        self.released = False

    def release(self) -> None:
        self.released = True


class Phase4ProfileTest(unittest.TestCase):
    def run_solve(self, browser_or_error):
        profiles: list[str] = []

        async def start(profile_dir):
            profiles.append(profile_dir)
            if isinstance(browser_or_error, Exception):
                raise browser_or_error
            await asyncio.sleep(0)
            return browser_or_error

        async def no_sleep(_):
            return None

        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            os.mkdir(base, 0o700)
            with mock.patch.dict(
                os.environ, {"TS_PROFILE_DIR": base}, clear=False
            ), mock.patch.object(solver, "_start_browser", side_effect=start), mock.patch.object(
                solver.asyncio, "sleep", side_effect=no_sleep
            ):
                try:
                    result = asyncio.run(solver._solve("key", "url", 1))
                except BaseException as exc:
                    result = exc
                remaining = list(Path(base).iterdir())
                self.assertTrue(Path(base).exists())
        return result, profiles, remaining

    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "Linux /proc descriptor accounting is unavailable")
    def test_partial_profile_setup_removes_directory_and_closes_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            os.mkdir(base, 0o700)
            before = len(list(Path("/proc/self/fd").iterdir()))
            real_open = solver.os.open

            injected = False

            def fail_profile_open(path, flags, *args, **kwargs):
                nonlocal injected
                if not injected and str(path).startswith("capsolve-") and kwargs.get("dir_fd") is not None:
                    injected = True
                    raise OSError("injected")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False), mock.patch.object(
                solver.os, "open", side_effect=fail_profile_open
            ), self.assertRaisesRegex(RuntimeError, "creation failed"):
                asyncio.run(solver._solve("key", "url", 1))
            self.assertEqual(list(Path(base).iterdir()), [])
            self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)

    def test_failure_between_profile_creation_and_browser_start_cleans_profile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            os.mkdir(base, 0o700)
            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False), mock.patch.object(
                solver, "BrowserOwner", side_effect=RuntimeError("setup failed")
            ), self.assertRaisesRegex(RuntimeError, "setup failed"):
                asyncio.run(solver._solve("key", "url", 1))
            self.assertEqual(list(Path(base).iterdir()), [])

    def test_concurrent_solves_use_unique_profiles_and_remove_them(self) -> None:
        profiles: list[str] = []

        async def start(profile_dir):
            profiles.append(profile_dir)
            await asyncio.sleep(0)
            return FakeBrowser()

        async def no_sleep(_):
            return None

        async def both():
            return await asyncio.gather(solver._solve("key", "url", 1), solver._solve("key", "url", 1))

        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            os.mkdir(base, 0o700)
            with mock.patch.dict(
                os.environ, {"TS_PROFILE_DIR": base}, clear=False
            ), mock.patch.object(solver, "_start_browser", side_effect=start), mock.patch.object(
                solver.asyncio, "sleep", side_effect=no_sleep
            ):
                self.assertEqual(asyncio.run(both()), ["test-token", "test-token"])
                self.assertEqual(len(set(profiles)), 2)
                self.assertEqual(list(Path(base).iterdir()), [])

    def test_profile_cleanup_requires_confirmed_browser_stop(self) -> None:
        cases = (
            (RuntimeError("start failed"), 0),
            (FakeBrowser(get_error=RuntimeError("get failed")), 0),
            (FakeBrowser(stop_error=RuntimeError("stop failed")), 0),
        )
        for case, residue in cases:
            with self.subTest(case=type(case).__name__):
                result, profiles, remaining = self.run_solve(case)
                self.assertIsInstance(result, RuntimeError)
                self.assertEqual(len(profiles), 1)
                self.assertEqual(len(remaining), residue)


class Phase4BrowserLifecycleTest(unittest.TestCase):
    def test_partial_start_is_stopped_and_waited(self) -> None:
        browser = FakeBrowser()

        async def start():
            browser._process = FakeProcess()
            raise KeyboardInterrupt

        browser.start = start
        with mock.patch.object(solver.uc, "Config", return_value=object()), mock.patch.object(
            solver.uc, "Browser", return_value=browser
        ):
            owner = solver.BrowserOwner("profile")
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(solver._start_browser(owner))
            asyncio.run(solver._stop_browser(owner.browser))
        self.assertTrue(browser.stopped)
        self.assertTrue(browser._process.terminated)
        self.assertEqual(browser._process.returncode, 0)

    def test_live_process_after_close_terminate_kill_wait_failures_retains_profile(self) -> None:
        browser = FakeBrowser(stop_error=RuntimeError("close"))
        browser._process.wait = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        with self.assertRaisesRegex(solver.BrowserCleanupError, "cleanup failed"):
            asyncio.run(solver._stop_browser(browser))
        self.assertTrue(browser._process.terminated)
        self.assertTrue(browser._process.killed)
        self.assertIsNone(browser._process.returncode)

        result, _, remaining = Phase4ProfileTest().run_solve(browser)
        self.assertIsInstance(result, solver.BrowserCleanupError)
        self.assertEqual(len(remaining), 1)

    def test_aclose_failure_without_process_status_retains_profile(self) -> None:
        browser = FakeBrowser(stop_error=RuntimeError("close"))
        browser._process = None
        result, _, remaining = Phase4ProfileTest().run_solve(browser)
        self.assertIsInstance(result, solver.BrowserCleanupError)
        self.assertEqual(len(remaining), 1)

    def test_aclose_failure_with_confirmed_process_death_deletes_profile(self) -> None:
        browser = FakeBrowser(stop_error=RuntimeError("close"))
        result, _, remaining = Phase4ProfileTest().run_solve(browser)
        self.assertIsInstance(result, solver.BrowserCleanupError)
        self.assertEqual(remaining, [])
        self.assertEqual(browser._process.returncode, 0)

    def test_profile_delete_occurs_only_after_process_wait(self) -> None:
        order: list[str] = []
        browser = FakeBrowser()

        async def close():
            order.append("close")

        async def wait():
            order.append("wait")
            browser._process.returncode = 0

        browser.aclose = close
        browser._process.wait = wait
        with mock.patch.object(solver, "_remove_profile", side_effect=lambda *args: order.append("delete")):
            async def cleanup():
                await solver._stop_browser(browser)
                solver._remove_profile("base", [1], "profile", 2)

            asyncio.run(cleanup())
        self.assertEqual(order, ["close", "wait", "delete"])


class Phase4ProfileContainmentTest(unittest.TestCase):
    def test_base_must_be_owned_real_directory_with_mode_0700(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False):
                path, fds = solver._open_profile_base()
                solver._close_fds(fds)
                self.assertEqual(path, base)
                self.assertEqual(os.stat(base).st_mode & 0o777, 0o700)

                os.chmod(base, 0o755)
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    solver._open_profile_base()
                os.chmod(base, 0o700)
                with mock.patch.object(solver.os, "getuid", return_value=os.stat(base).st_uid + 1):
                    with self.assertRaisesRegex(RuntimeError, "unsafe"):
                        solver._open_profile_base()

            os.rmdir(base)
            target = os.path.join(root, "target")
            os.mkdir(target, 0o700)
            os.symlink(target, base)
            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False), self.assertRaisesRegex(
                RuntimeError, "unsafe"
            ):
                solver._open_profile_base()

    def test_ancestor_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            os.mkdir(target)
            ancestor = os.path.join(root, "ancestor")
            os.symlink(target, ancestor)
            with mock.patch.dict(
                os.environ, {"TS_PROFILE_DIR": os.path.join(ancestor, "profiles")}, clear=False
            ), self.assertRaisesRegex(RuntimeError, "unsafe"):
                solver._open_profile_base()

    def test_profile_swap_before_browser_launch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            outside = os.path.join(root, "outside")
            os.mkdir(base, 0o700)
            os.mkdir(outside, 0o700)
            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False):
                trusted, fds = solver._open_profile_base()
                profile, name, profile_fd = solver._create_profile(trusted, fds)
                os.rmdir(profile)
                os.symlink(outside, profile)
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    solver._validate_profile(trusted, fds, name, profile_fd)
                os.close(profile_fd)
                solver._close_fds(fds)

    def test_regular_directory_inode_swaps_rejected_at_launch_and_cleanup(self) -> None:
        for component in ("ancestor", "base", "profile"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as root:
                ancestor = os.path.join(root, "ancestor")
                base = os.path.join(ancestor, "profiles")
                os.mkdir(ancestor)
                os.mkdir(base, 0o700)
                with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False):
                    trusted, fds = solver._open_profile_base()
                    profile, name, profile_fd = solver._create_profile(trusted, fds)
                    target = {"ancestor": ancestor, "base": base, "profile": profile}[component]
                    os.rename(target, target + ".old")
                    os.mkdir(target, 0o700)
                    with self.assertRaisesRegex(RuntimeError, "unsafe"):
                        solver._validate_profile(trusted, fds, name, profile_fd)
                    with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                        solver._remove_profile(trusted, fds, name, profile_fd)
                    os.close(profile_fd)
                    solver._close_fds(fds)

    def test_base_and_profile_uid_mode_changes_rejected_at_launch_and_cleanup(self) -> None:
        for component in ("base", "profile"):
            for mutation in ("mode", "uid"):
                with self.subTest(component=component, mutation=mutation), tempfile.TemporaryDirectory() as root:
                    base = os.path.join(root, "profiles")
                    os.mkdir(base, 0o700)
                    with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False):
                        trusted, fds = solver._open_profile_base()
                        profile, name, profile_fd = solver._create_profile(trusted, fds)
                        target = base if component == "base" else profile
                        context = mock.patch.object(solver.os, "getuid", return_value=os.getuid() + 1) if mutation == "uid" else __import__("contextlib").nullcontext()
                        if mutation == "mode":
                            os.chmod(target, 0o755)
                        with context:
                            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                                solver._validate_profile(trusted, fds, name, profile_fd)
                            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                                solver._remove_profile(trusted, fds, name, profile_fd)
                        os.close(profile_fd)
                        solver._close_fds(fds)

    def test_base_or_profile_symlink_swap_never_deletes_target(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "profiles")
            outside = os.path.join(root, "outside")
            os.mkdir(base, 0o700)
            os.mkdir(outside, 0o700)
            marker = os.path.join(outside, "keep")
            Path(marker).write_text("keep")
            with mock.patch.dict(os.environ, {"TS_PROFILE_DIR": base}, clear=False):
                trusted, fds = solver._open_profile_base()
                _, name, profile_fd = solver._create_profile(trusted, fds)
                os.rename(base, base + ".old")
                os.symlink(outside, base)
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    solver._remove_profile(trusted, fds, name, profile_fd)
                self.assertTrue(Path(marker).exists())
                os.unlink(base)
                os.rename(base + ".old", base)
                shutil_target = os.path.join(base, name)
                os.rmdir(shutil_target)
                os.symlink(outside, shutil_target)
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    solver._remove_profile(trusted, fds, name, profile_fd)
                self.assertTrue(Path(marker).exists())
                os.close(profile_fd)
                solver._close_fds(fds)


class Phase4RuntimeSettingsTest(unittest.TestCase):
    def test_global_slots_validation_and_external_display_never_starts_xvfb(self) -> None:
        self.assertEqual(load_settings("worker", {}).global_chrome_slots, 1)
        maximum = 2**63 - chrome_slots.CHROME_SLOT_BASE_KEY
        for value in ("", "0", "invalid", str(maximum + 1)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "GLOBAL_CHROME_SLOTS"):
                load_settings("worker", {"GLOBAL_CHROME_SLOTS": value})
        self.assertEqual(load_settings("worker", {"GLOBAL_CHROME_SLOTS": str(maximum)}).global_chrome_slots, maximum)
        with self.assertRaisesRegex(ValueError, "ENABLE_XVFB"):
            load_settings("worker", {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24", "ENABLE_XVFB_VIRTUAL_DISPLAY": "true"})

        settings = SimpleNamespace(
            environment="production",
            enable_xvfb_virtual_display=False,
        )
        with mock.patch.object(service.platform, "system", return_value="Linux"), mock.patch.dict(
            os.environ, {"DISPLAY": ":99"}, clear=False
        ), mock.patch.object(service.subprocess, "Popen") as popen:
            self.assertIsNone(service._ensure_display(settings))
        popen.assert_not_called()


class Phase4ChromeSlotsUnitTest(unittest.TestCase):
    class Cursor:
        def __init__(self, connection, values):
            self.connection = connection
            self.values = iter(values)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            self.connection.queries.append((query, params))

        def fetchone(self):
            value = next(self.values)
            if isinstance(value, BaseException):
                raise value
            return (value,)

    class Connection:
        def __init__(self, values):
            self.values = values
            self.closed = False
            self.queries = []
            self.autocommit = False

        def cursor(self):
            return Phase4ChromeSlotsUnitTest.Cursor(self, self.values)

        def close(self):
            self.closed = True

    def test_acquisition_baseexceptions_close_and_partial_lock_unlocks(self) -> None:
        for failure in (KeyboardInterrupt(), asyncio.CancelledError()):
            connection = self.Connection([failure])
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                chrome_slots.database, "get_connection", return_value=connection
            ), self.assertRaisesRegex(chrome_slots.ChromeSlotError, "acquisition failed"):
                chrome_slots.try_acquire(1)
            self.assertTrue(connection.autocommit)
            self.assertTrue(connection.closed)

        connection = self.Connection([True, KeyboardInterrupt(), True])
        with mock.patch.object(chrome_slots.database, "get_connection", return_value=connection), mock.patch.object(
            chrome_slots, "ChromeSlot", side_effect=KeyboardInterrupt
        ), self.assertRaisesRegex(chrome_slots.ChromeSlotError, "acquisition failed"):
            chrome_slots.try_acquire(1)
        self.assertTrue(connection.closed)
        self.assertTrue(any("pg_advisory_unlock" in query for query, _ in connection.queries))

    def test_release_checks_result_closes_and_is_idempotent(self) -> None:
        for result in (False, KeyboardInterrupt()):
            connection = self.Connection([result])
            slot = chrome_slots.ChromeSlot(connection, 0, chrome_slots.CHROME_SLOT_BASE_KEY)
            with self.subTest(result=type(result).__name__), self.assertRaisesRegex(
                chrome_slots.ChromeSlotError, "release failed"
            ):
                slot.release()
            self.assertTrue(connection.closed)
            slot.release()


class Phase4XvfbTest(unittest.TestCase):
    class Process:
        def __init__(self, *, running=True, timeout=False):
            self.running = running
            self.timeout = timeout
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.timeout = False

        def wait(self, timeout):
            if self.timeout:
                raise service.subprocess.TimeoutExpired("xvfb", timeout)
            self.running = False
            return 0

    def tearDown(self) -> None:
        service._xvfb_proc = None
        service._xvfb_display = None
        os.environ.pop("DISPLAY", None)

    def test_development_owned_display_starts_verifies_and_stops(self) -> None:
        settings = SimpleNamespace(environment="development", enable_xvfb_virtual_display=True)
        process = self.Process()
        with mock.patch.object(service.platform, "system", return_value="Linux"), mock.patch.dict(
            os.environ, {}, clear=True
        ), mock.patch.object(service.subprocess, "Popen", return_value=process):
            service._xvfb_proc = service._ensure_display(settings)
            self.assertEqual(os.environ["DISPLAY"], ":99")
            service._stop_display()
            self.assertTrue(process.terminated)
            self.assertNotIn("DISPLAY", os.environ)
            self.assertIsNone(service._xvfb_proc)

    def test_stop_kills_after_timeout_and_does_not_unset_changed_display(self) -> None:
        process = self.Process(timeout=True)
        service._xvfb_proc = process
        service._xvfb_display = ":99"
        os.environ["DISPLAY"] = ":100"
        service._stop_display()
        self.assertTrue(process.killed)
        self.assertEqual(os.environ["DISPLAY"], ":100")

    def test_failed_shutdown_keeps_ownership_for_retry(self) -> None:
        process = self.Process(timeout=True)
        process.kill = mock.Mock()
        service._xvfb_proc = process
        service._xvfb_display = ":99"
        os.environ["DISPLAY"] = ":99"
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            service._stop_display()
        self.assertIs(service._xvfb_proc, process)
        self.assertEqual(service._xvfb_display, ":99")
        self.assertNotIn("DISPLAY", os.environ)
        process.timeout = False
        service._stop_display()
        self.assertIsNone(service._xvfb_proc)

    def test_failed_shutdown_then_start_restores_owned_display(self) -> None:
        settings = SimpleNamespace(environment="development", enable_xvfb_virtual_display=True)
        process = self.Process(timeout=True)
        process.kill = mock.Mock()
        service._xvfb_proc = process
        service._xvfb_display = ":99"
        os.environ.pop("DISPLAY", None)
        self.assertIs(service._ensure_display(settings), process)
        self.assertEqual(os.environ["DISPLAY"], ":99")

    def test_dead_retained_process_is_reaped_then_restarted(self) -> None:
        settings = SimpleNamespace(environment="development", enable_xvfb_virtual_display=True)
        dead = self.Process(running=False)
        replacement = self.Process()
        service._xvfb_proc = dead
        service._xvfb_display = ":99"
        os.environ.pop("DISPLAY", None)
        with mock.patch.object(service.platform, "system", return_value="Linux"), mock.patch.object(
            service.subprocess, "Popen", return_value=replacement
        ) as popen:
            self.assertIs(service._ensure_display(settings), replacement)
        popen.assert_called_once()
        self.assertEqual(os.environ["DISPLAY"], ":99")

    def test_failed_start_never_sets_display(self) -> None:
        settings = SimpleNamespace(environment="development", enable_xvfb_virtual_display=True)
        with mock.patch.object(service.platform, "system", return_value="Linux"), mock.patch.dict(
            os.environ, {}, clear=True
        ), mock.patch.object(service.subprocess, "Popen", return_value=self.Process(running=False)), self.assertRaisesRegex(
            RuntimeError, "failed to start"
        ):
            service._ensure_display(settings)
        self.assertNotIn("DISPLAY", os.environ)


class Phase4AdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            sync_queue_max_waiting=0,
            global_chrome_slots=2,
            job_queue_retry_after_seconds=1,
            solver_timeout=45,
            local_post_timeout=30,
        )
        self.patches = [
            mock.patch.object(service, "_settings", self.settings),
            mock.patch.object(service, "MAX_WORKERS", 2),
            mock.patch.object(service, "_active_count", 0),
            mock.patch.object(service, "_queued_count", 0),
            mock.patch.object(process_jobs.job_repository, "queue_metrics", return_value={}),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in reversed(self.patches)])

    def test_two_active_are_admitted_and_third_is_rejected_before_slot_or_chrome(self) -> None:
        slots = [FakeSlot(), FakeSlot()]
        with mock.patch.object(service, "_slot_acquirer", side_effect=slots) as acquire:
            first = service._acquire_sync_slot()
            second = service._acquire_sync_slot()
            third = service._acquire_sync_slot()
        self.assertIsNone(third)
        self.assertEqual(acquire.call_count, 2)
        self.assertEqual((service._active_count, service._queued_count), (2, 0))
        service._release_sync_slot(first)
        service._release_sync_slot(second)
        self.assertTrue(all(slot.released for slot in slots))
        self.assertEqual((service._active_count, service._queued_count), (0, 0))

    def test_http_overflow_is_generic_429_with_retry_after_and_no_chrome(self) -> None:
        first = FakeSlot()
        with mock.patch.object(service, "API_KEYS", (API_KEY,)), mock.patch.object(
            service, "_slot_acquirer", return_value=first
        ) as acquire, mock.patch.object(service, "_solve_and_post") as solve:
            service._active_count = 2
            response = request("POST", "/api/solve/", query={"nric": "safe"})
        self.assertEqual(response[0], 429)
        self.assertEqual(response[1]["retry-after"], "1")
        self.assertEqual(response[2], {"detail": "Solver is busy"})
        acquire.assert_not_called()
        solve.assert_not_called()

    def test_endpoint_releases_slot_after_success_exception_timeout_and_cancellation(self) -> None:
        outcomes = ({"ok": True}, RuntimeError("failure"), TimeoutError("timeout"), asyncio.CancelledError())
        for outcome in outcomes:
            slot = FakeSlot()
            kwargs = {"return_value": outcome} if isinstance(outcome, dict) else {"side_effect": outcome}
            with self.subTest(outcome=type(outcome).__name__), mock.patch.object(
                service, "_slot_acquirer", return_value=slot
            ), mock.patch.object(service, "_solve_and_post", **kwargs):
                try:
                    service.solve_endpoint("safe", 1, 1, None)
                except asyncio.CancelledError:
                    pass
            self.assertTrue(slot.released)
            self.assertEqual((service._active_count, service._queued_count), (0, 0))

    def test_cancellation_during_wait_and_acquisition_restores_counters(self) -> None:
        self.settings.sync_queue_max_waiting = 1
        service._active_count = 1
        with mock.patch.object(service, "MAX_WORKERS", 1), mock.patch.object(
            service._count_lock, "wait", side_effect=KeyboardInterrupt
        ), self.assertRaises(KeyboardInterrupt):
            service._acquire_sync_slot(lambda _: FakeSlot())
        self.assertEqual((service._active_count, service._queued_count), (1, 0))

        service._active_count = 0
        for failure in (KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                service._acquire_sync_slot(mock.Mock(side_effect=failure))
            self.assertEqual((service._active_count, service._queued_count), (0, 0))

    def test_waiting_is_bounded_and_restored(self) -> None:
        self.settings.sync_queue_max_waiting = 1
        with mock.patch.object(service, "MAX_WORKERS", 1), mock.patch.object(
            service, "_slot_acquirer", side_effect=[FakeSlot(), FakeSlot()]
        ):
            first = service._acquire_sync_slot()
            result: list[FakeSlot] = []
            waiter = threading.Thread(target=lambda: result.append(service._acquire_sync_slot()))
            waiter.start()
            for _ in range(100):
                with service._count_lock:
                    if service._queued_count == 1:
                        break
                threading.Event().wait(0.005)
            self.assertEqual(service._queued_count, 1)
            self.assertIsNone(service._acquire_sync_slot())
            service._release_sync_slot(first)
            waiter.join(1)
            self.assertFalse(waiter.is_alive())
            service._release_sync_slot(result[0])
        self.assertEqual((service._active_count, service._queued_count), (0, 0))

    def test_worker_baseexceptions_emit_one_sanitized_error_and_release(self) -> None:
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=0, global_chrome_slots=1)
        for stage in ("acquire", "claim", "release"):
            slot = FakeSlot()
            if stage == "release":
                slot.release = mock.Mock(side_effect=KeyboardInterrupt("sensitive"))
            acquire = mock.Mock(side_effect=KeyboardInterrupt("sensitive")) if stage == "acquire" else mock.Mock(return_value=slot)
            claim = mock.Mock(side_effect=asyncio.CancelledError("sensitive")) if stage == "claim" else mock.Mock(return_value=None)
            output = __import__("io").StringIO()
            with self.subTest(stage=stage), mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
                process_jobs, "load_settings", return_value=settings
            ), mock.patch.object(process_jobs.chrome_slots, "try_acquire", acquire), mock.patch.object(
                job_repository, "claim_pending_job", claim
            ), mock.patch("sys.argv", ["capsolve-worker"]), mock.patch("sys.stdout", output):
                self.assertEqual(process_jobs.main(), 1)
            records = [__import__("json").loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual((records[0]["event"], records[0]["error_code"]), ("worker_error", "internal_error"))
            self.assertNotIn("sensitive", output.getvalue())

    def test_worker_sensitive_systemexit_and_help_emit_one_sanitized_record(self) -> None:
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=0, global_chrome_slots=1)
        stages = (
            ("acquire", process_jobs.chrome_slots, "try_acquire"),
            ("claim", job_repository, "claim_pending_job"),
            ("config", process_jobs, "_worker_config"),
            ("process", process_jobs, "_process_job"),
        )
        job = {"id": 1, "ulid": "job", "attempts": 1, "max_attempts": 1, "nric": "safe"}
        for stage, target, attribute in stages:
            output = __import__("io").StringIO()
            slot = FakeSlot()
            patches = [
                mock.patch.object(process_jobs, "load_dotenv"),
                mock.patch.object(process_jobs, "load_settings", return_value=settings),
                mock.patch.object(process_jobs.chrome_slots, "try_acquire", return_value=slot),
                mock.patch.object(job_repository, "claim_pending_job", side_effect=[job, None]),
                mock.patch.object(process_jobs, "_worker_config", return_value={"config_source": "test"}),
                mock.patch.object(process_jobs, "_process_job"),
                mock.patch("sys.argv", ["capsolve-worker"]), mock.patch("sys.stdout", output),
            ]
            with self.subTest(stage=stage), __import__("contextlib").ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(mock.patch.object(target, attribute, side_effect=SystemExit("sensitive canary")))
                self.assertEqual(process_jobs.main(), 1)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertNotIn("sensitive", output.getvalue())

        for stage in ("finalization", "release"):
            output = __import__("io").StringIO()
            slot = FakeSlot()
            if stage == "release":
                slot.release = mock.Mock(side_effect=SystemExit(0))
            with self.subTest(stage=stage), mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
                process_jobs, "load_settings", return_value=settings
            ), mock.patch.object(process_jobs.chrome_slots, "try_acquire", return_value=slot), mock.patch.object(
                job_repository, "claim_pending_job", side_effect=[job, None]
            ), mock.patch.object(process_jobs, "_worker_config", return_value={"config_source": "test"}), mock.patch.object(
                process_jobs, "_process_job", side_effect=SystemExit(0) if stage == "finalization" else None
            ), mock.patch("sys.argv", ["capsolve-worker"]), mock.patch("sys.stdout", output):
                self.assertEqual(process_jobs.main(), 1)
            self.assertEqual(len(output.getvalue().splitlines()), 1)

        output = __import__("io").StringIO()
        with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
            process_jobs, "load_settings", side_effect=AssertionError("settings should not be loaded")
        ), mock.patch("sys.argv", ["capsolve-worker", "--help"]), mock.patch("sys.stdout", output):
            self.assertEqual(process_jobs.main(), 0)
        records = [__import__("json").loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["event"], records[0]["exit_status"]), ("worker_help", 0))
        self.assertIn("--limit", records[0]["help"])
        self.assertFalse(records[0]["queue_metrics_available"])

    def test_direct_solver_cli_is_disabled_and_never_prints_token(self) -> None:
        output = __import__("io").StringIO()
        with mock.patch.object(solver, "solve") as browser, mock.patch.object(
            solver, "post_local_result"
        ), mock.patch("sys.stdout", output):
            self.assertNotEqual(solver.main(), 0)
        browser.assert_not_called()
        self.assertNotIn("token-canary", output.getvalue())

    def test_all_direct_cli_paths_are_disabled_without_browser_or_token(self) -> None:
        for module in (solver, clientsend):
            output = __import__("io").StringIO()
            with self.subTest(module=module.__name__), mock.patch.object(solver, "solve") as browser, mock.patch(
                "sys.stdout", output
            ):
                self.assertNotEqual(module.main(), 0)
            browser.assert_not_called()
            self.assertNotIn("token-canary", output.getvalue())

    def test_worker_acquires_before_claim_and_releases_empty_queue_slot(self) -> None:
        settings = SimpleNamespace(job_batch_limit=1, job_reset_stale_minutes=0, global_chrome_slots=1)
        slot = FakeSlot()
        with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
            process_jobs, "load_settings", return_value=settings
        ), mock.patch.object(process_jobs.chrome_slots, "try_acquire", return_value=slot) as acquire, mock.patch.object(
            job_repository, "claim_pending_job", return_value=None
        ) as claim, mock.patch("sys.argv", ["capsolve-worker"]):
            self.assertEqual(process_jobs.main(), 0)
        acquire.assert_called_once_with(1)
        claim.assert_called_once_with(set())
        self.assertTrue(slot.released)


if __name__ == "__main__":
    unittest.main()
