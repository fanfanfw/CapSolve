from __future__ import annotations

import contextlib
import http.client
import io
import json
import logging
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import uvicorn

import service


API_KEY = "phase2-loopback-key"
MARKERS = (
    "NRIC-CANARY-123",
    "QUERY-CANARY-SQL-DBHOST-DBUSER-DBPASSWORD",
    "BODY-CANARY-SQL-DBHOST-DBUSER-DBPASSWORD",
    "HEADER-CANARY-SQL-DBHOST-DBUSER-DBPASSWORD",
    "CONFIG-CANARY-NRIC-TOKEN-CREDENTIAL-DSN-URL",
)


class Phase2UvicornAccessLogTest(unittest.TestCase):
    def test_entry_point_disables_native_access_log(self) -> None:
        settings = SimpleNamespace(api_host="127.0.0.1", api_port=8191)
        with mock.patch.object(service, "_configure", return_value=settings), mock.patch.object(
            service.uvicorn, "run"
        ) as run:
            service.run()
        run.assert_called_once_with(
            "service:app", host="127.0.0.1", port=8191, access_log=False
        )

    def test_real_loopback_uvicorn_never_logs_or_echoes_request_values(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        loggers = [logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")]
        for logger in loggers:
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)

        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            service.app,
            host="127.0.0.1",
            port=port,
            access_log=service.UVICORN_ACCESS_LOG,
            lifespan="off",
            log_config=None,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread_started = False
        responses = []

        def send(path: str, *, method: str = "POST", body: dict | None = None, api_key: str = API_KEY) -> tuple[int, dict]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            payload = json.dumps(body) if body is not None else None
            headers = {"x-api-key": api_key}
            if body is not None:
                headers["content-type"] = "application/json"
            try:
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        output = io.StringIO()
        try:
            settings = SimpleNamespace(
                solver_timeout=45,
                local_post_timeout=30,
                job_queue_retry_after_seconds=1,
                sync_queue_max_waiting=0,
                global_chrome_slots=1,
            )
            with mock.patch.object(service, "API_KEYS", (API_KEY,)), mock.patch.object(
                service, "_settings", settings
            ), mock.patch.object(service, "_slot_acquirer", return_value=mock.Mock(release=lambda: None)), mock.patch.object(
                service, "_solve_and_post", return_value={"status": 200, "body": {"ok": True}}
            ), mock.patch.object(
                service, "resolve_budi95_config", side_effect=RuntimeError(MARKERS[4])
            ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                thread.start()
                thread_started = True
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started, "Uvicorn did not start within five seconds")
                responses.extend(
                    [
                        send(f"/api/solve/?nric={MARKERS[0]}"),
                        send(f"/api/solve/?nric=safe&timeout={MARKERS[1]}"),
                        send("/api/budi95", body={"nric": {"value": MARKERS[2]}}),
                        send("/api/solve/?nric=safe", api_key=MARKERS[3]),
                        send("/api/budi95/config", method="GET"),
                    ]
                )
        finally:
            server.should_exit = True
            if thread_started:
                thread.join(timeout=5)
            listener.close()
            for logger in loggers:
                logger.removeHandler(handler)

        self.assertFalse(thread.is_alive(), "Uvicorn did not stop within five seconds")
        self.assertEqual(responses[0], (200, {"status": 200, "body": {"ok": True}}))
        self.assertEqual(responses[1], (422, {"detail": "Invalid request"}))
        self.assertEqual(responses[2], (422, {"detail": "Invalid request"}))
        self.assertEqual(responses[3], (401, {"detail": "Invalid API key."}))
        self.assertEqual(responses[4], (503, {"detail": "BUDI95 configuration is unavailable"}))
        rendered = (json.dumps(responses) + capture.getvalue() + output.getvalue()).lower()
        for marker in MARKERS:
            self.assertNotIn(marker.lower(), rendered)
        self.assertNotIn("/api/solve/?", rendered)


if __name__ == "__main__":
    unittest.main()
