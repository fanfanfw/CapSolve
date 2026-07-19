from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from config_resolver import Budi95Config
import job_repository
import service


API_KEY = "contract-test-key"
CANARY = "S1234567A SQL db.internal dbuser dbpassword"
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "api_contract_500f2e5.json").read_text(encoding="utf-8"))
BASELINE = GOLDEN["fixtures"]


def request(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict | None = None,
    api_key: str | None = API_KEY,
) -> tuple[int, dict[str, str], dict]:
    payload = json.dumps(body).encode() if body is not None else b""
    messages: list[dict] = []
    received = False

    async def receive() -> dict:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    headers = [(b"host", b"testserver")]
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(service.app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    return start["status"], response_headers, json.loads(response_body)


class Phase2GoldenHttpTest(unittest.TestCase):
    def test_baseline_artifact_provenance_and_true_leaks(self) -> None:
        self.assertEqual(GOLDEN["baseline_commit"], "500f2e5")
        self.assertIn("git worktree add --detach", GOLDEN["procedure"])
        self.assertIn(CANARY, json.dumps(BASELINE["solve_raw_failure"]))
        self.assertIn(CANARY, json.dumps(BASELINE["result_failed"]))
        self.assertIn(CANARY, json.dumps(BASELINE["submit_malformed_type"]))
        self.assertIn(CANARY, json.dumps(BASELINE["solve_malformed_timeout"]))
        self.assertEqual(BASELINE["health"]["body"]["workers"], 1)

    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            job_queue_capacity=3,
            job_queue_retry_after_seconds=17,
            job_max_attempts=4,
            job_reset_stale_minutes=30,
            budi95_submit_rate_limit_per_minute=0,
            budi95_read_rate_limit_per_minute=0,
            solver_timeout=45,
            local_post_timeout=30,
            sync_queue_max_waiting=0,
            global_chrome_slots=1,
        )
        self.patches = [
            mock.patch.object(service, "API_KEYS", (API_KEY,)),
            mock.patch.object(service, "_settings", self.settings),
            mock.patch.object(service, "MAX_WORKERS", 1),
            mock.patch.object(service, "_active_count", 0),
            mock.patch.object(service, "_queued_count", 0),
            mock.patch.object(service, "_slot_acquirer", return_value=mock.Mock(release=lambda: None)),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in reversed(self.patches)])

    def assert_json(self, response: tuple[int, dict[str, str], dict], status: int, body: dict) -> None:
        actual_status, headers, actual_body = response
        self.assertEqual(actual_status, status)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(actual_body, body)

    def assert_golden(self, response: tuple[int, dict[str, str], dict], name: str) -> None:
        status_code, headers, body = response
        expected = BASELINE[name]
        self.assertEqual(
            {"status": status_code, "headers": {"content-type": headers["content-type"]}, "body": body},
            expected,
        )

    def assert_no_leak(self, response: tuple[int, dict[str, str], dict]) -> None:
        rendered = json.dumps(response).lower()
        for value in ("s1234567a", "sql", "db.internal", "dbuser", "dbpassword"):
            self.assertNotIn(value, rendered)

    def test_solve_success_and_safe_existing_failures_match_captured_head_golden(self) -> None:
        with mock.patch.object(service, "_solve_and_post", return_value=BASELINE["solve_success"]["body"]):
            self.assert_golden(request("POST", "/api/solve/", query={"nric": "S1234567A"}), "solve_success")

        self.assert_golden(request("POST", "/api/solve/"), "solve_missing_nric")
        self.assert_golden(
            request("POST", "/api/solve/", query={"nric": "S1234567A"}, api_key=None), "solve_missing_key"
        )
        with mock.patch.object(service, "API_KEYS", ("another-key",)):
            self.assert_golden(request("POST", "/api/solve/", query={"nric": "S1234567A"}), "solve_invalid_key")

    def test_approved_solve_failure_hardening_replaces_head_canary_leak(self) -> None:
        self.assertIn(CANARY, json.dumps(BASELINE["solve_raw_failure"]))
        with mock.patch.object(service, "_solve_and_post", side_effect=RuntimeError(f"failure {CANARY}")):
            response = request("POST", "/api/solve/", query={"nric": "S1234567A"})
        self.assert_json(response, 500, {"error_code": "solve_failed", "message": "Unable to process subsidy"})
        self.assert_no_leak(response)

    def test_config_normal_and_allowed_force_refresh_match_head_golden(self) -> None:
        configs = {
            False: Budi95Config("https://api.example.invalid/result", "https://site.example.invalid", "0x1234567890abcdef", "cache"),
            True: Budi95Config("https://api.example.invalid/result", "https://site.example.invalid", "0xabcdef1234567890", "website"),
        }
        with mock.patch.object(service, "resolve_budi95_config", side_effect=lambda force_refresh=False: configs[force_refresh]) as resolve:
            self.assert_golden(request("GET", "/api/budi95/config"), "config_normal")
            self.assert_golden(
                request("GET", "/api/budi95/config", query={"force_refresh": "true"}), "config_force"
            )
        self.assertEqual([call.kwargs for call in resolve.call_args_list], [{"force_refresh": False}, {"force_refresh": True}])

    def test_config_resolver_failure_is_generic_and_absent_from_output(self) -> None:
        canary = "NRIC_MARK token=secret credential=secret postgresql://user:pass@db.internal/app https://secret.invalid/path"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(service, "resolve_budi95_config", side_effect=RuntimeError(canary)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = request("GET", "/api/budi95/config")
        self.assert_json(response, 503, {"detail": "BUDI95 configuration is unavailable"})
        for rendered in (json.dumps(response), stdout.getvalue(), stderr.getvalue()):
            for marker in ("nric_mark", "token=secret", "credential=secret", "postgresql://", "user:pass", "secret.invalid"):
                self.assertNotIn(marker, rendered.lower())

    def test_both_submit_forms_preserve_exact_head_success_and_trim_nric(self) -> None:
        for path in ("/api/budi95", "/api/budi95/"):
            with self.subTest(path=path), mock.patch.object(
                job_repository,
                "create_job",
                return_value={"nric": "S1234567A", "ulid": "a" * 32},
            ) as create:
                self.assert_golden(
                    request("POST", path, body={"nric": "  S1234567A  "}),
                    "submit_slash" if path.endswith("/") else "submit",
                )
                create.assert_called_once_with("S1234567A", 4, 3)

    def test_queue_status_reports_capacity_usage_and_worker_model(self) -> None:
        self.settings.job_queue_capacity = 3
        self.settings.global_chrome_slots = 1
        metrics = {
            "queue_depth": 4,
            "pending_count": 3,
            "processing_count": 1,
            "oldest_pending_age_seconds": 12.5,
            "stale_processing_count": 1,
        }
        with mock.patch.object(job_repository, "queue_metrics", return_value=metrics) as queue_metrics:
            self.assert_json(
                request("GET", "/api/budi95/queue/status"),
                200,
                {
                    "capacity": 3,
                    "depth": 4,
                    "pending": 3,
                    "processing": 1,
                    "available": 0,
                    "oldest_pending_age_seconds": 12.5,
                    "stale_processing": 1,
                    "worker": {"model": "scheduled", "processing": 1, "max_concurrent_solves": 1},
                },
            )
        queue_metrics.assert_called_once_with(30)

    def test_queue_status_requires_key_and_sanitizes_repository_failure(self) -> None:
        self.assert_json(
            request("GET", "/api/budi95/queue/status", api_key=None),
            401,
            {"detail": "Missing x-api-key header."},
        )
        output = io.StringIO()
        with mock.patch.object(job_repository, "queue_metrics", side_effect=RuntimeError(CANARY)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = request("GET", "/api/budi95/queue/status")
        self.assert_json(response, 503, {"detail": "Job queue is unavailable"})
        self.assert_no_leak(response)
        self.assertNotIn(CANARY.lower(), output.getvalue().lower())

    def test_rate_limit_route_classification_matches_real_routes_only(self) -> None:
        self.assertEqual(service._rate_limit_kind("POST", "/api/budi95"), "submit")
        self.assertEqual(service._rate_limit_kind("POST", "/api/budi95/"), "submit")
        self.assertEqual(service._rate_limit_kind("GET", "/api/budi95/result/job-id"), "read")
        self.assertEqual(service._rate_limit_kind("GET", "/api/budi95/queue/status"), "read")
        self.assertIsNone(service._rate_limit_kind("GET", "/api/budi95/result/"))
        self.assertIsNone(service._rate_limit_kind("GET", "/api/budi95/result/job-id/extra"))
        self.assertIsNone(service._rate_limit_kind("POST", "/api/budi95/not-a-route"))

    def test_budi95_rate_limits_are_separate_per_client_and_kind(self) -> None:
        self.settings.budi95_submit_rate_limit_per_minute = 1
        self.settings.budi95_read_rate_limit_per_minute = 2
        with service._rate_limit_lock:
            service._rate_limit_buckets.clear()
        self.assertIsNone(service._rate_limit("192.0.2.10", "submit", 120.0))
        self.assertEqual(service._rate_limit("192.0.2.10", "submit", 121.0), 59)
        self.assertIsNone(service._rate_limit("192.0.2.11", "submit", 121.0))
        self.assertIsNone(service._rate_limit("192.0.2.10", "read", 121.0))
        self.assertIsNone(service._rate_limit("192.0.2.10", "read", 122.0))
        self.assertEqual(service._rate_limit("192.0.2.10", "read", 123.0), 57)
        self.assertIsNone(service._rate_limit("192.0.2.10", "submit", 180.0))

    def test_equivalent_ipv6_spellings_share_one_rate_limit(self) -> None:
        self.settings.budi95_read_rate_limit_per_minute = 1
        with service._rate_limit_lock:
            service._rate_limit_buckets.clear()
        self.assertIsNone(service._rate_limit("2001:db8::1", "read", 120.0))
        self.assertEqual(service._rate_limit("2001:0DB8:0:0:0:0:0:1", "read", 121.0), 59)

    def test_rate_limit_bucket_count_is_hard_bounded(self) -> None:
        self.settings.budi95_read_rate_limit_per_minute = 1
        with service._rate_limit_lock:
            service._rate_limit_buckets.clear()
            service._rate_limit_buckets.update({(f"192.0.2.{index}", "read"): (2, 1) for index in range(service._rate_limit_max_buckets)})
        self.assertEqual(service._rate_limit("198.51.100.1", "read", 121.0), 59)
        self.assertEqual(len(service._rate_limit_buckets), service._rate_limit_max_buckets)
        self.assertIsNone(service._rate_limit("198.51.100.1", "read", 180.0))

    def test_rate_limited_business_request_returns_429_with_retry_after(self) -> None:
        self.settings.budi95_read_rate_limit_per_minute = 1
        with service._rate_limit_lock:
            service._rate_limit_buckets.clear()
        metrics = {
            "queue_depth": 0,
            "pending_count": 0,
            "processing_count": 0,
            "oldest_pending_age_seconds": None,
            "stale_processing_count": 0,
        }
        with mock.patch.object(job_repository, "queue_metrics", return_value=metrics):
            self.assertEqual(request("GET", "/api/budi95/queue/status")[0], 200)
            response = request("GET", "/api/budi95/queue/status")
        self.assert_json(response, 429, {"detail": "Rate limit exceeded"})
        self.assertIn("retry-after", response[1])

    def test_malformed_type_canaries_are_generic_and_absent_from_logs(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            body_response = request("POST", "/api/budi95", body={"nric": {"value": CANARY}})
            query_response = request("POST", "/api/solve/", query={"nric": "safe", "timeout": CANARY})
        for response in (body_response, query_response):
            self.assert_json(response, 422, {"detail": "Invalid request"})
            self.assert_no_leak(response)
        self.assert_no_leak((0, {}, {"logs": output.getvalue()}))

    def test_submit_validation_queue_full_and_unavailable_are_controlled(self) -> None:
        self.assert_json(request("POST", "/api/budi95", body={"nric": "  "}), 422, {"detail": "nric is required"})
        self.assert_json(
            request("POST", "/api/budi95", body={"nric": "x" * 33}),
            422,
            {"detail": "nric must be at most 32 characters"},
        )
        for error, expected_status, detail in (
            (job_repository.QueueFullError(), 429, "Job queue is full"),
            (RuntimeError(CANARY), 503, "Job queue is unavailable"),
        ):
            with self.subTest(status=expected_status), mock.patch.object(job_repository, "create_job", side_effect=error):
                response = request("POST", "/api/budi95", body={"nric": "S1234567A"})
            self.assert_json(response, expected_status, {"detail": detail})
            self.assertEqual(response[1]["retry-after"], "17")
            self.assert_no_leak(response)

    def test_all_safe_result_states_and_health_match_head_golden(self) -> None:
        states = [
            {"status": "pending"},
            {"status": "processing"},
            {"status": "success", "response_body": {"results": {"success": True}}},
        ]
        for row in states:
            with self.subTest(state=row["status"]), mock.patch.object(job_repository, "get_job_by_ulid", return_value=row):
                self.assert_golden(
                    request("GET", "/api/budi95/result/" + "a" * 32), f"result_{row['status']}"
                )

        with mock.patch.object(job_repository, "get_job_by_ulid", return_value=None):
            self.assert_golden(request("GET", "/api/budi95/result/missing"), "result_not_found")
        self.assert_golden(request("GET", "/api/health"), "health")

    def test_result_repository_failure_is_generic_and_does_not_leak(self) -> None:
        canary = "NRIC_MARK token=turnstile credential=https://user:pass@host/body"
        output = io.StringIO()
        with mock.patch.object(job_repository, "get_job_by_ulid", side_effect=RuntimeError(canary)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = request("GET", "/api/budi95/result/" + "c" * 32)
        self.assert_json(response, 503, {"detail": "Job result is unavailable"})
        rendered = json.dumps({"response": response, "logs": output.getvalue()}).lower()
        for marker in ("nric_mark", "token=turnstile", "credential=", "user:pass"):
            self.assertNotIn(marker, rendered)

    def test_approved_failed_result_hardening_replaces_legacy_stored_error(self) -> None:
        self.assertIn(CANARY, json.dumps(BASELINE["result_failed"]))
        with mock.patch.object(job_repository, "get_job_by_ulid", return_value={"status": "failed", "error": CANARY}):
            response = request("GET", "/api/budi95/result/" + "b" * 32)
        self.assert_json(
            response,
            200,
            {
                "status": False,
                "job_status": "failed",
                "message": "Unable to process subsidy",
                "data": {"error_code": "job_failed", "message": "Unable to process subsidy"},
            },
        )
        self.assert_no_leak(response)


if __name__ == "__main__":
    unittest.main()
