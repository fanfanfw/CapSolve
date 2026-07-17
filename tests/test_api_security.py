from __future__ import annotations

import asyncio
import contextlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import uvicorn

import job_repository
import service
from settings import load_settings


API_KEY = "phase5-test-key"
ROOT = Path(__file__).parents[1]


def asgi_request(app, *, client: str, api_key: str = API_KEY, headers: dict[str, str] | None = None):
    messages: list[dict] = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    request_headers = {"host": "api.example.invalid", "x-api-key": api_key}
    request_headers.update(headers or {})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/budi95/result/missing",
        "raw_path": b"/api/budi95/result/missing",
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in request_headers.items()],
        "client": (client, 1234),
        "server": ("test", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    try:
        content = json.loads(body) if body else None
    except json.JSONDecodeError:
        content = body.decode()
    return start["status"], content


def security_settings(allowlist: str):
    networks = tuple(ipaddress.ip_network(value) for value in allowlist.split(","))
    return SimpleNamespace(
        allowed_hosts=("api.example.invalid",),
        api_ip_allowlist=networks,
    )


class Phase5ApplicationSecurityTest(unittest.TestCase):
    def test_layer_order_ip_before_key_and_host_before_business_controls(self):
        app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(
            service, "API_KEYS", (API_KEY,)
        ), mock.patch.object(job_repository, "get_job_by_ulid", return_value=None):
            self.assertEqual(asgi_request(app, client="192.0.2.11")[0], 403)
            self.assertEqual(asgi_request(app, client="192.0.2.10", api_key="wrong")[0], 401)
            self.assertEqual(asgi_request(app, client="192.0.2.10")[0], 404)
            self.assertEqual(
                asgi_request(app, client="192.0.2.10", headers={"host": "wrong.example.invalid"})[0],
                400,
            )

    def test_business_logic_ignores_all_forwarding_headers(self):
        forged = {
            "forwarded": "for=192.0.2.10",
            "x-forwarded-for": "192.0.2.10",
            "x-real-ip": "192.0.2.10",
            "cf-connecting-ip": "192.0.2.10",
        }
        app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(
            service, "API_KEYS", (API_KEY,)
        ):
            self.assertEqual(asgi_request(app, client="127.0.0.1", headers=forged)[0], 403)

    def test_exact_ipv4_ipv6_and_cidr_allowlists(self):
        cases = (
            ("192.0.2.10/32", "192.0.2.10", 404),
            ("192.0.2.0/24", "192.0.2.99", 404),
            ("2001:db8::10/128", "2001:db8::10", 404),
            ("2001:db8::/64", "2001:db8::99", 404),
            ("2001:db8::10/128", "2001:db8::11", 403),
        )
        for allowlist, client, expected in cases:
            with self.subTest(allowlist=allowlist, client=client):
                app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
                with mock.patch.object(service, "_settings", security_settings(allowlist)), mock.patch.object(
                    service, "API_KEYS", (API_KEY,)
                ), mock.patch.object(job_repository, "get_job_by_ulid", return_value=None):
                    self.assertEqual(asgi_request(app, client=client)[0], expected)

    def test_host_gate_supports_ipv6_and_rejects_malformed_or_multiple_host(self):
        settings = security_settings("127.0.0.1/32")
        settings.allowed_hosts = ("[::1]", "api.example.invalid")
        app = service.create_app(docs_enabled=False, allowed_hosts=settings.allowed_hosts)
        with mock.patch.object(service, "_settings", settings), mock.patch.object(service, "API_KEYS", (API_KEY,)), mock.patch.object(job_repository, "get_job_by_ulid", return_value=None):
            for host in ("[::1]", "[::1]:8443", "api.example.invalid:443"):
                self.assertEqual(asgi_request(app, client="127.0.0.1", headers={"host": host})[0], 404)
            for host in ("[::1", "::1", "[127.0.0.1]", "[fe80::1%eth0]", "[fe80::1%25eth0]", "api.example.invalid:bad"):
                self.assertEqual(asgi_request(app, client="127.0.0.1", headers={"host": host})[0], 400)
        headers = [(b"host", b"api.example.invalid"), (b"host", b"[::1]"), (b"x-api-key", API_KEY.encode())]
        self.assertEqual(self.raw_request(app, headers)[0], 400)

    def raw_request(self, app, headers):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/api/budi95/result/x", "raw_path": b"/api/budi95/result/x", "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
        asyncio.run(app(scope, receive, send))
        return next(message["status"] for message in messages if message["type"] == "http.response.start"), messages

    def test_outer_gate_precedes_router_redirect_and_validation(self):
        app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(service, "API_KEYS", (API_KEY,)):
            for path in ("/api/solve", "/api/solve/not-a-route", "/api/budi95//", "/api/budi95/result"):
                status_code, _ = self.raw_request(app, [(b"host", b"api.example.invalid"), (b"x-api-key", API_KEY.encode())]) if path == "/api/budi95/result/x" else self.path_request(app, path, "192.0.2.11", API_KEY)
                self.assertEqual(status_code, 403)
            self.assertEqual(self.path_request(app, "/api/budi95//", "192.0.2.10", "wrong")[0], 401)

    def path_request(self, app, path, client, key):
        messages = []
        async def receive(): return {"type": "http.request", "body": b"{malformed", "more_body": False}
        async def send(message): messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"bad=%", "headers": [(b"host", b"api.example.invalid"), (b"x-api-key", key.encode()), (b"content-type", b"application/json")], "client": (client, 1), "server": ("test", 80), "root_path": ""}
        asyncio.run(app(scope, receive, send))
        return next(message["status"] for message in messages if message["type"] == "http.response.start"), messages

    def test_real_uvicorn_business_like_path_variants_gate_before_normalization(self):
        app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
        variants = ("/api//budi95", "/api/./budi95", "/api/%62udi95", "/API/BUDI95", "/api\\budi95")
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(service, "API_KEYS", (API_KEY,)):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            config = uvicorn.Config(app, access_log=False, lifespan="off", log_config=None, proxy_headers=False)
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                for path in variants:
                    for key, expected in ((API_KEY, 403), ("wrong", 403)):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                        connection.request("GET", path, headers={"Host": "api.example.invalid", "x-api-key": key})
                        response = connection.getresponse()
                        self.assertEqual(response.status, expected, path)
                        response.read()
                        connection.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                listener.close()

        with mock.patch.object(service, "_settings", security_settings("127.0.0.1/32")), mock.patch.object(service, "API_KEYS", (API_KEY,)):
            for path in variants:
                self.assertEqual(self.path_request(app, path, "127.0.0.1", "wrong")[0], 401)
                self.assertIn(self.path_request(app, path, "127.0.0.1", API_KEY)[0], (400, 404))

    def test_disabled_docs_routes_are_not_registered(self):
        app = service.create_app(docs_enabled=False, allowed_hosts=("api.example.invalid",))
        with mock.patch.object(service, "_settings", security_settings("127.0.0.1/32")):
            for path in ("/docs", "/redoc", "/openapi.json"):
                self.assertFalse(any(getattr(route, "path", None) == path for route in app.routes))


class Phase5SettingsTest(unittest.TestCase):
    def production(self, **overrides):
        values = {
            "ENVIRONMENT": "production",
            "JOB_RETENTION_HOURS": "24",
            "API_KEY": "aB3_-xY9" + "z" * 35,
            "API_IP_ALLOWLIST": "192.0.2.10/32,2001:db8::/64",
            "ALLOWED_HOSTS": "api.example.invalid",
            "API_DOCS_ENABLED": "false",
            "FORWARDED_ALLOW_IPS": "127.0.0.1/32,::1/128",
            "UVICORN_UDS": "/run/capsolve/uvicorn/api.sock",
            "UVICORN_SOCKET_MODE": "0660",
            "UVICORN_SOCKET_PARENT_GID": str(os.getgid()),
            "UVICORN_SOCKET_GID": str(os.getgid()),
        }
        values.update(overrides)
        return values

    def test_production_wildcards_docs_tcp_and_invalid_proxy_peers_fail_closed(self):
        cases = (
            ({"API_IP_ALLOWLIST": "*"}, "API_IP_ALLOWLIST"),
            ({"ALLOWED_HOSTS": "*"}, "ALLOWED_HOSTS"),
            ({"FORWARDED_ALLOW_IPS": "*"}, "FORWARDED_ALLOW_IPS"),
            ({"FORWARDED_ALLOW_IPS": ""}, "FORWARDED_ALLOW_IPS"),
            ({"FORWARDED_ALLOW_IPS": "not-an-ip"}, "FORWARDED_ALLOW_IPS"),
            ({"API_DOCS_ENABLED": "true"}, "API_DOCS_ENABLED"),
            ({"API_HOST": "127.0.0.1"}, "API_HOST"),
            ({"UVICORN_UDS": "relative.sock"}, "UVICORN_UDS"),
            ({"UVICORN_SOCKET_MODE": "0666"}, "UVICORN_SOCKET_MODE"),
        )
        for override, message in cases:
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, message):
                load_settings("api", self.production(**override))

    def test_production_requires_explicit_numeric_socket_target_gid(self):
        for value in (None, "", "not-a-gid"):
            values = self.production()
            if value is None:
                values.pop("UVICORN_SOCKET_GID")
            else:
                values["UVICORN_SOCKET_GID"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "UVICORN_SOCKET_GID"):
                load_settings("api", values)

    def test_configured_hosts_reject_scoped_ipv6_and_malformed_brackets(self):
        for host in ("[fe80::1%eth0]", "[fe80::1%25eth0]", "[[::1]]"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "ALLOWED_HOSTS"):
                load_settings("api", {"API_KEY": "dev", "ALLOWED_HOSTS": host})

    def test_development_is_loopback_only(self):
        settings = load_settings("api", {"API_KEY": "dev", "API_HOST": "::1"})
        self.assertEqual(settings.api_host, "::1")
        for host in ("0.0.0.0", "::", "192.0.2.1", "localhost"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "loopback"):
                load_settings("api", {"API_KEY": "dev", "API_HOST": host})

    def test_entry_point_passes_prebound_uds_fd_to_uvicorn(self):
        settings = load_settings("api", self.production())
        listener = mock.Mock()
        listener.fileno.return_value = 17
        with mock.patch.object(service, "_configure", return_value=settings), mock.patch.object(
            service, "_secure_uds_listener", return_value=(listener, (1, 2))
        ) as bind, mock.patch.object(service, "_cleanup_uds") as cleanup, mock.patch.object(
            service.uvicorn, "run"
        ) as run:
            service.run()
        bind.assert_called_once_with("/run/capsolve/uvicorn/api.sock", 0o660, os.getgid(), os.getgid())
        run.assert_called_once_with(
            "service:app",
            fd=17,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1/32,::1/128",
        )
        cleanup.assert_called_once_with(listener, "/run/capsolve/uvicorn/api.sock", (1, 2))


class Phase5RealUvicornProxyTest(unittest.TestCase):
    def serve_once(self, trusted_peers: str, headers: dict[str, str]):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            service.create_app(
                docs_enabled=False,
                allowed_hosts=("api.example.invalid",),
                forwarded_allow_ips=trusted_peers,
            ),
            access_log=False,
            lifespan="off",
            log_config=None,
            proxy_headers=False,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/api/budi95/result/missing", headers=headers)
            response = connection.getresponse()
            result = response.status, response.read()
        finally:
            connection.close()
            server.should_exit = True
            thread.join(timeout=5)
            listener.close()
        self.assertFalse(thread.is_alive())
        return result

    def test_untrusted_direct_uvicorn_peer_cannot_spoof_any_forwarding_header(self):
        forged = {
            "Host": "api.example.invalid",
            "x-api-key": API_KEY,
            "Forwarded": "for=192.0.2.10",
            "X-Forwarded-For": "192.0.2.10",
            "X-Real-IP": "192.0.2.10",
            "CF-Connecting-IP": "192.0.2.10",
        }
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(
            service, "API_KEYS", (API_KEY,)
        ):
            self.assertEqual(self.serve_once("192.0.2.1", forged)[0], 403)

    def test_authenticated_nginx_hop_uses_only_synthesized_single_xff(self):
        headers = {
            "Host": "api.example.invalid",
            "x-api-key": API_KEY,
            "X-Forwarded-For": "192.0.2.10",
            "Forwarded": "for=203.0.113.9",
            "X-Real-IP": "203.0.113.9",
            "CF-Connecting-IP": "203.0.113.9",
        }
        with mock.patch.object(service, "_settings", security_settings("192.0.2.10/32")), mock.patch.object(
            service, "API_KEYS", (API_KEY,)
        ), mock.patch.object(job_repository, "get_job_by_ulid", return_value=None):
            self.assertEqual(self.serve_once("127.0.0.1", headers)[0], 404)


class Phase5ProxyArtifactTest(unittest.TestCase):
    def test_nginx_and_tunnel_artifacts_define_only_permission_bound_unix_hops(self):
        nginx = (ROOT / "deployment/capsolve-nginx.conf.example").read_text()
        tunnel = (ROOT / "deployment/cloudflared-config.yml.example").read_text()
        self.assertIn("listen unix:/run/capsolve/ingress/cloudflared.sock", nginx)
        self.assertGreaterEqual(nginx.count("access_log off;"), 2)
        self.assertTrue((ROOT / "deployment/secure_nginx_ingress.py").exists())
        self.assertIn("server unix:/run/capsolve/uvicorn/api.sock", nginx)
        self.assertNotRegex(nginx, r"(?m)^\s*listen\s+(?:\[|[0-9])")
        self.assertIn('proxy_set_header Forwarded ""', nginx)
        self.assertIn("proxy_set_header X-Forwarded-For $capsolve_client_ip", nginx)
        self.assertIn('proxy_set_header X-Real-IP ""', nginx)
        self.assertIn('proxy_set_header CF-Connecting-IP ""', nginx)
        self.assertIn("map $http_cf_connecting_ip $capsolve_cf_candidate", nginx)
        self.assertIn('if ($capsolve_cf_candidate = "") { return 403; }', nginx)
        self.assertIn("real_ip_header CF-Connecting-IP", nginx)
        self.assertIn("service: unix:/run/capsolve/ingress/cloudflared.sock", tunnel)
        self.assertNotRegex(tunnel, r"service:\s*https?://(?:0\.0\.0\.0|127\.0\.0\.1|localhost)")
        self.assertIn("<REQUIRED_CAPSOLVE_HOSTNAME>", nginx)
        self.assertIn("<REQUIRED_TUNNEL_UUID>", tunnel)

    @unittest.skipUnless(shutil.which("cloudflared"), "cloudflared is not installed")
    def test_cloudflared_artifact_validates_after_placeholder_substitution(self):
        artifact = (ROOT / "deployment/cloudflared-config.yml.example").read_text()
        with tempfile.TemporaryDirectory() as root:
            credentials = Path(root) / "credentials.json"
            credentials.write_text('{"AccountTag":"test","TunnelSecret":"dGVzdA==","TunnelID":"00000000-0000-0000-0000-000000000001"}')
            rendered = artifact.replace("<REQUIRED_TUNNEL_UUID>", "00000000-0000-0000-0000-000000000001")
            rendered = rendered.replace("/etc/cloudflared/00000000-0000-0000-0000-000000000001.json", str(credentials))
            rendered = rendered.replace("<REQUIRED_CAPSOLVE_HOSTNAME>", "api.example.invalid")
            config = Path(root) / "config.yml"
            config.write_text(rendered)
            result = subprocess.run([shutil.which("cloudflared"), "tunnel", "--config", str(config), "ingress", "validate"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(shutil.which("nginx"), "nginx is not installed")
    def test_nginx_artifact_passes_syntax_check_in_temp_prefix(self):
        artifact = (ROOT / "deployment/capsolve-nginx.conf.example").read_text()
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "nginx.conf"
            rendered = artifact.replace("<REQUIRED_CAPSOLVE_HOSTNAME>", "api.example.invalid")
            rendered = rendered.replace("/run/capsolve/ingress/cloudflared.sock", f"{root}/ingress.sock")
            rendered = rendered.replace("/run/capsolve/uvicorn/api.sock", f"{root}/uvicorn.sock")
            config.write_text(
                f"pid {root}/nginx.pid;\nerror_log {root}/error.log;\nevents {{}}\nhttp {{\naccess_log {root}/access.log;\n{rendered}\n}}\n"
            )
            result = subprocess.run(
                [shutil.which("nginx"), "-t", "-p", root, "-c", str(config)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(shutil.which("nginx"), "nginx is not installed")
    def test_real_nginx_strips_forged_headers_and_forwards_one_canonical_xff_without_logs(self):
        artifact = (ROOT / "deployment/capsolve-nginx.conf.example").read_text()
        canary = "PHASE5-NGINX-CANARY"
        with tempfile.TemporaryDirectory() as root:
            ingress = os.path.join(root, "ingress.sock")
            backend = os.path.join(root, "backend.sock")
            captured = []
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(backend)
            listener.listen()
            stop = threading.Event()

            def serve_backend():
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except OSError:
                        return
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += connection.recv(4096)
                    captured.append(data.decode("latin1"))
                    connection.sendall(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                    connection.close()

            backend_thread = threading.Thread(target=serve_backend, daemon=True)
            backend_thread.start()
            rendered = artifact.replace("<REQUIRED_CAPSOLVE_HOSTNAME>", "api.example.invalid")
            rendered = rendered.replace("/run/capsolve/ingress/cloudflared.sock", ingress)
            rendered = rendered.replace("/run/capsolve/uvicorn/api.sock", backend)
            config = Path(root) / "nginx.conf"
            config.write_text(f"pid {root}/nginx.pid;\nerror_log {root}/error.log;\nevents {{}}\nhttp {{\naccess_log {root}/access.log;\n{rendered}\n}}\n")
            nginx = subprocess.Popen([shutil.which("nginx"), "-p", root, "-c", str(config), "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(ingress) and nginx.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(ingress))

                def send(headers):
                    client = socket.socket(socket.AF_UNIX)
                    client.connect(ingress)
                    request = "GET /path?" + canary + " HTTP/1.1\r\nHost: api.example.invalid\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers) + "Connection: close\r\n\r\n"
                    client.sendall(request.encode())
                    response = b""
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                    client.close()
                    return int(response.split(b" ", 2)[1])

                forged = [("Forwarded", "for=203.0.113.9"), ("X-Forwarded-For", "203.0.113.9"), ("X-Real-IP", "203.0.113.9")]
                valid = ("192.0.2.10", "2001:db8::10", "2001:0db8:0000:0000:0000:0000:0000:0010", "::ffff:192.0.2.1")
                for address in valid:
                    self.assertEqual(send([("CF-Connecting-IP", address), *forged]), 204)
                for headers in (
                    [],
                    [("CF-Connecting-IP", "")],
                    [("CF-Connecting-IP", "invalid")],
                    [("CF-Connecting-IP", "192.0.2.10:443")],
                    [("CF-Connecting-IP", "1...2")],
                    [("CF-Connecting-IP", "999.1.1.1")],
                    [("CF-Connecting-IP", "2001:::1")],
                    [("CF-Connecting-IP", "192.0.2.10, 203.0.113.9")],
                    [("CF-Connecting-IP", "192.0.2.10"), ("CF-Connecting-IP", "203.0.113.9")],
                ):
                    self.assertEqual(send(headers), 403)
                stop.set()
                listener.close()
                backend_thread.join(timeout=2)
                if os.path.exists(backend):
                    os.unlink(backend)
                self.assertIn(send([("CF-Connecting-IP", "192.0.2.10")]), (502, 504))
            finally:
                nginx.terminate()
                nginx.wait(timeout=5)
                stop.set()
                listener.close()
                backend_thread.join(timeout=2)
            self.assertEqual(len(captured), 4)
            self.assertIn("X-Forwarded-For: 192.0.2.10\r\n", captured[0])
            self.assertIn("X-Forwarded-For: 2001:db8::10\r\n", captured[1])
            self.assertIn("X-Forwarded-For: 2001:db8::10\r\n", captured[2])
            self.assertIn("X-Forwarded-For: ::ffff:192.0.2.1\r\n", captured[3])
            for request in captured:
                self.assertNotIn("Forwarded:", request)
                self.assertNotIn("X-Real-IP:", request)
                self.assertNotIn("CF-Connecting-IP:", request)
                self.assertEqual(request.lower().count("x-forwarded-for:"), 1)
            access_log = Path(root, "access.log")
            self.assertNotIn(canary, access_log.read_text() if access_log.exists() else "")
            error_log = Path(root, "error.log")
            self.assertNotIn(canary, error_log.read_text() if error_log.exists() else "")

    def test_process_without_socket_permission_cannot_connect(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o755)
            for name in ("cloudflared.sock", "uvicorn.sock"):
                with self.subTest(socket=name):
                    path = os.path.join(root, name)
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(path)
                        listener.listen()
                        os.chmod(path, 0)
                        script = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])"
                        result = subprocess.run(
                            [os.sys.executable, "-c", script, path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        self.assertNotEqual(result.returncode, 0)
                    os.unlink(path)


if __name__ == "__main__":
    unittest.main()
