from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import process_jobs


CANARY = "NRIC_MARKER token=TURNSTILE API_KEY=secret dsn=postgresql://user:pass@host/db upstream-body"


class Phase3WorkerUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(process_jobs.job_repository, "queue_metrics", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def summary(self) -> dict:
        return {
            "claimed": 0,
            "success": 0,
            "failed": 0,
            "retried": 0,
            "lost_claim": 0,
            "exceptions": 0,
            "non_2xx": 0,
            "reset_stale": 0,
            "config_source": "test",
            "config_refreshed": 0,
        }

    def run_main(self, *, reset_stale: int = 0, argv: list[str] | None = None) -> tuple[int, str]:
        settings = SimpleNamespace(job_batch_limit=2, job_reset_stale_minutes=reset_stale, global_chrome_slots=1)
        output = io.StringIO()
        with tempfile.TemporaryFile() as fd_output:
            saved = (os.dup(1), os.dup(2))
            try:
                os.dup2(fd_output.fileno(), 1)
                os.dup2(fd_output.fileno(), 2)
                with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
                    process_jobs, "load_settings", return_value=settings
                ), mock.patch.object(
                    process_jobs.chrome_slots, "try_acquire", return_value=mock.Mock(release=lambda: None)
                ), mock.patch.object(
                    process_jobs.job_repository, "queue_metrics", return_value={}
                ), mock.patch("sys.argv", argv or ["capsolve-worker"]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = process_jobs.main()
            finally:
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
            fd_output.seek(0)
            return result, output.getvalue() + fd_output.read().decode()

    def native_noise(self, label: str) -> None:
        payload = f"{label} {CANARY}\n".encode()
        os.write(1, payload)
        os.write(2, payload)
        subprocess.run(
            [sys.executable, "-c", "import os; os.write(1, %r); os.write(2, %r)" % (payload, payload)],
            check=True,
        )

    def assert_safe_cli_failure(self, result: int, output: str) -> dict:
        self.assertNotEqual(result, 0)
        for marker in (CANARY, "NRIC_MARKER", "TURNSTILE", "API_KEY", "postgresql://", "user:pass", "upstream-body", "Traceback"):
            self.assertNotIn(marker, output)
        records = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "worker_error")
        self.assertEqual(records[0]["error_code"], "internal_error")
        return records[0]

    def test_invalid_arguments_emit_one_controlled_record(self) -> None:
        for arguments in (
            ["capsolve-worker", "--limit", "0"],
            ["capsolve-worker", "--limit", CANARY],
            ["capsolve-worker", "--reset-stale-minutes", "-1"],
            ["capsolve-worker", "--unknown", CANARY],
        ):
            with self.subTest(arguments=arguments[1:]):
                result, output = self.run_main(argv=arguments)
                summary = self.assert_safe_cli_failure(result, output)
                self.assertEqual(summary["claimed"], 0)

    def test_zero_row_finalization_is_only_a_lost_claim(self) -> None:
        job = {"ulid": "job-1", "attempts": 2, "max_attempts": 3, "nric": "test"}
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1}

        summary = self.summary()
        with mock.patch.object(process_jobs, "solve", return_value="token"), mock.patch.object(
            process_jobs, "post_local_result", return_value={"status": 200, "body": {"ok": True}}
        ), mock.patch.object(process_jobs.job_repository, "mark_job_success", return_value=False) as finalize:
            process_jobs._process_job(job, config, summary, SimpleNamespace())
        finalize.assert_called_once_with("job-1", 2, 200, {"ok": True})
        self.assertEqual((summary["success"], summary["lost_claim"]), (0, 1))
        self.assertEqual((summary["event"], summary["error_code"], summary["outcome"]), (
            "worker_failure", "lost_claim", "lost_claim"
        ))

        summary = self.summary()
        with mock.patch.object(process_jobs, "solve", side_effect=RuntimeError("controlled")), mock.patch.object(
            process_jobs.job_repository, "mark_job_failed", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            process_jobs._process_job(job, config, summary, SimpleNamespace())
        self.assertEqual((summary["failed"], summary["retried"], summary["lost_claim"]), (0, 0, 1))

    def test_repository_rejects_uncontrolled_failure_before_connecting(self) -> None:
        with mock.patch.object(process_jobs.job_repository.database, "get_connection") as connect:
            with self.assertRaisesRegex(ValueError, "unsupported failure code"):
                process_jobs.job_repository.mark_job_failed("job-1", 1, "raw sensitive detail")
        connect.assert_not_called()

    def test_empty_queue_does_not_resolve_worker_config(self) -> None:
        order: list[str] = []
        with mock.patch.object(
            process_jobs.job_repository, "claim_pending_job", side_effect=lambda _: order.append("claim")
        ), mock.patch.object(
            process_jobs, "_worker_config", side_effect=lambda _: order.append("config")
        ) as config:
            result, output = self.run_main()

        self.assertEqual(result, 0)
        self.assertEqual(order, ["claim"])
        config.assert_not_called()
        summary = json.loads(output)
        self.assertEqual((summary["claimed"], summary["config_source"]), (0, None))

    def test_cli_boundary_sanitizes_config_stale_reset_and_claim_exceptions(self) -> None:
        job = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        cases = (
            (
                "config",
                0,
                mock.patch.object(process_jobs.job_repository, "claim_pending_job", return_value=job),
                mock.patch.object(process_jobs, "_worker_config", side_effect=RuntimeError(CANARY)),
                1,
            ),
            (
                "stale",
                1,
                mock.patch.object(process_jobs.job_repository, "reset_stale_processing_jobs", side_effect=RuntimeError(CANARY)),
                mock.patch.object(process_jobs.job_repository, "claim_pending_job"),
                0,
            ),
            (
                "claim",
                0,
                mock.patch.object(process_jobs.job_repository, "claim_pending_job", side_effect=RuntimeError(CANARY)),
                mock.patch.object(process_jobs, "_worker_config"),
                0,
            ),
        )
        for name, reset_stale, first_patch, second_patch, expected_claimed in cases:
            with self.subTest(boundary=name), first_patch, second_patch, mock.patch.object(
                process_jobs.job_repository, "mark_job_failed", return_value=True
            ):
                result, output = self.run_main(reset_stale=reset_stale)
            summary = self.assert_safe_cli_failure(result, output)
            self.assertEqual(summary["claimed"], expected_claimed)
            expected_retried = 1 if name == "config" else 0
            self.assertEqual((summary["success"], summary["failed"], summary["retried"], summary["lost_claim"]), (0, 0, expected_retried, 0))

    def test_cli_boundary_sanitizes_success_failure_and_outer_processing_exceptions(self) -> None:
        job = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1, "config_source": "test"}

        with mock.patch.object(process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]), mock.patch.object(
            process_jobs, "_worker_config", return_value=config
        ), mock.patch.object(process_jobs, "solve", return_value="token"), mock.patch.object(
            process_jobs, "post_local_result", return_value={"status": 200, "body": {}}
        ), mock.patch.object(process_jobs.job_repository, "mark_job_success", side_effect=RuntimeError(CANARY)), mock.patch.object(
            process_jobs.job_repository, "mark_job_failed"
        ) as fail:
            result, output = self.run_main()
        summary = self.assert_safe_cli_failure(result, output)
        fail.assert_not_called()
        self.assertEqual((summary["claimed"], summary["success"], summary["lost_claim"]), (1, 0, 0))

        with mock.patch.object(process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]), mock.patch.object(
            process_jobs, "_worker_config", return_value=config
        ), mock.patch.object(process_jobs, "solve", side_effect=RuntimeError(CANARY)), mock.patch.object(
            process_jobs.job_repository, "mark_job_failed", side_effect=RuntimeError(CANARY)
        ):
            result, output = self.run_main()
        summary = self.assert_safe_cli_failure(result, output)
        self.assertEqual((summary["claimed"], summary["exceptions"], summary["failed"], summary["retried"], summary["lost_claim"]), (1, 1, 0, 0, 0))

        with mock.patch.object(process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]), mock.patch.object(
            process_jobs, "_worker_config", return_value=config
        ), mock.patch.object(process_jobs, "_process_job", side_effect=RuntimeError(CANARY)):
            result, output = self.run_main()
        summary = self.assert_safe_cli_failure(result, output)
        self.assertEqual((summary["claimed"], summary["success"], summary["failed"], summary["lost_claim"]), (1, 0, 0, 0))

    def test_main_suppresses_native_config_solver_and_upstream_noise(self) -> None:
        job = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1, "config_source": "test"}

        def noisy_config(*args, **kwargs):
            self.native_noise("FDNOISE_CONFIG")
            return config

        def noisy_solve(*args, **kwargs):
            self.native_noise("FDNOISE_SOLVER")
            return "token"

        def noisy_post(*args, **kwargs):
            self.native_noise("FDNOISE_UPSTREAM")
            return {"status": 200, "body": {"ok": True}}

        with mock.patch.object(
            process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]
        ), mock.patch.object(process_jobs, "_worker_config", side_effect=noisy_config), mock.patch.object(
            process_jobs, "solve", side_effect=noisy_solve
        ), mock.patch.object(process_jobs, "post_local_result", side_effect=noisy_post), mock.patch.object(
            process_jobs.job_repository, "mark_job_success", return_value=False
        ):
            result, output = self.run_main()

        self.assertEqual(result, 0)
        records = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(records), 1)
        summary = records[0]
        self.assertEqual((summary["event"], summary["error_code"], summary["outcome"]), (
            "worker_failure", "lost_claim", "lost_claim"
        ))
        self.assertEqual((summary["claimed"], summary["success"], summary["lost_claim"]), (1, 0, 1))
        for marker in (CANARY, "FDNOISE_CONFIG", "FDNOISE_SOLVER", "FDNOISE_UPSTREAM"):
            self.assertNotIn(marker, output)

    def test_main_suppresses_native_force_refresh_and_restores_descriptors_on_exception(self) -> None:
        job = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1, "config_source": "test"}
        solve_calls = 0

        def noisy_solve(*args, **kwargs):
            nonlocal solve_calls
            solve_calls += 1
            self.native_noise("FDNOISE_SOLVER")
            if solve_calls == 1:
                raise process_jobs.urllib.error.URLError("connection refused")
            return "token"

        def noisy_config(*args, **kwargs):
            self.native_noise("FDNOISE_REFRESH")
            return config

        with mock.patch.object(
            process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]
        ), mock.patch.object(process_jobs, "_worker_config", side_effect=noisy_config), mock.patch.object(
            process_jobs, "solve", side_effect=noisy_solve
        ), mock.patch.object(process_jobs, "post_local_result", return_value={"status": 200, "body": {}}), mock.patch.object(
            process_jobs.job_repository, "mark_job_success", return_value=True
        ):
            result, output = self.run_main()

        self.assertEqual(result, 0)
        records = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["success"], records[0]["config_refreshed"]), (1, 1))
        self.assertNotIn(CANARY, output)
        self.assertNotIn("FDNOISE_REFRESH", output)
        self.assertNotIn("FDNOISE_SOLVER", output)

        def noisy_failure(*args, **kwargs):
            self.native_noise("FDNOISE_RAISING_CONFIG")
            raise RuntimeError(CANARY)

        with mock.patch.object(process_jobs.job_repository, "claim_pending_job", return_value=job), mock.patch.object(
            process_jobs, "_worker_config", side_effect=noisy_failure
        ), mock.patch.object(process_jobs.job_repository, "mark_job_failed", return_value=True):
            result, output = self.run_main()
        summary = self.assert_safe_cli_failure(result, output)
        self.assertEqual(summary["claimed"], 1)
        self.assertNotIn("FDNOISE_RAISING_CONFIG", output)

    def test_main_emits_one_failure_record_for_retry_terminal_lost_and_non_2xx(self) -> None:
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1, "config_source": "test"}
        cases = (
            ("retry", 2, RuntimeError(CANARY), None, True, "retried", (0, 1, 0, 1, 0)),
            ("terminal", 1, RuntimeError(CANARY), None, True, "failed", (0, 0, 1, 1, 0)),
            ("lost", 2, RuntimeError(CANARY), None, False, "lost_claim", (0, 0, 0, 1, 1)),
            ("non_2xx", 2, None, {"status": 503, "body": {}}, True, "retried", (1, 1, 0, 0, 0)),
        )
        for name, max_attempts, solve_error, result, finalized, outcome, counters in cases:
            job = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": max_attempts, "nric": "test"}
            solve_patch = mock.patch.object(
                process_jobs, "solve", return_value="token", side_effect=solve_error
            )
            with self.subTest(outcome=name), mock.patch.object(
                process_jobs.job_repository, "claim_pending_job", side_effect=[job, None]
            ), mock.patch.object(process_jobs, "_worker_config", return_value=config), solve_patch, mock.patch.object(
                process_jobs, "post_local_result", return_value=result
            ), mock.patch.object(process_jobs.job_repository, "mark_job_failed", return_value=finalized):
                exit_code, output = self.run_main()

            self.assertEqual(exit_code, 0)
            records = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(len(records), 1)
            summary = records[0]
            self.assertEqual((summary["event"], summary["error_code"], summary["outcome"]), (
                "worker_failure", "upstream_unavailable" if name == "non_2xx" else "internal_error", outcome
            ))
            self.assertEqual(
                (summary["non_2xx"], summary["retried"], summary["failed"], summary["exceptions"], summary["lost_claim"]),
                counters,
            )
            for marker in (CANARY, "NRIC_MARKER", "TURNSTILE", "API_KEY", "postgresql://", "Traceback"):
                self.assertNotIn(marker, output)

    def test_prior_failure_then_claim_error_emits_only_final_worker_error(self) -> None:
        first = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        config = {"sitekey": "key", "siteurl": "url", "post_url": "post", "solver_timeout": 1, "post_timeout": 1, "config_source": "test"}
        with mock.patch.object(
            process_jobs.job_repository, "claim_pending_job", side_effect=[first, RuntimeError(CANARY)]
        ), mock.patch.object(process_jobs, "_worker_config", return_value=config), mock.patch.object(
            process_jobs, "solve", side_effect=RuntimeError(CANARY)
        ), mock.patch.object(process_jobs.job_repository, "mark_job_failed", return_value=True):
            result, output = self.run_main()

        summary = self.assert_safe_cli_failure(result, output)
        self.assertNotIn("outcome", summary)
        self.assertEqual((summary["claimed"], summary["retried"], summary["exceptions"]), (1, 1, 1))

    def test_main_claims_one_at_a_time_reuses_config_and_excludes_seen_job(self) -> None:
        settings = SimpleNamespace(job_batch_limit=2, job_reset_stale_minutes=0, global_chrome_slots=1)
        first = {"id": 7, "ulid": "job-1", "attempts": 1, "max_attempts": 2, "nric": "test"}
        claim_exclusions: list[set[int]] = []

        def claim(excluded: set[int]):
            claim_exclusions.append(set(excluded))
            return first if not excluded else None

        with mock.patch.object(process_jobs, "load_dotenv"), mock.patch.object(
            process_jobs, "load_settings", return_value=settings
        ), mock.patch.object(process_jobs.chrome_slots, "try_acquire", return_value=mock.Mock(release=lambda: None)), mock.patch.object(
            process_jobs, "_worker_config", return_value={"config_source": "test"}
        ) as config, mock.patch.object(
            process_jobs.job_repository, "claim_pending_job", side_effect=claim
        ), mock.patch.object(process_jobs, "_process_job") as process, mock.patch(
            "sys.argv", ["capsolve-worker"]
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(process_jobs.main(), 0)

        self.assertEqual(claim_exclusions, [set(), {7}])
        config.assert_called_once_with(settings)
        process.assert_called_once_with(first, {"config_source": "test"}, mock.ANY, settings)


if __name__ == "__main__":
    unittest.main()
