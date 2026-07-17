from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import uvicorn

import job_repository
import service


class Phase5UdsUvicornTest(unittest.TestCase):
    def test_real_prebound_uds_keeps_mode_and_serves(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            os.chmod(root, 0o750)
            path = os.path.join(root, "api.sock")
            listener, identity = service._secure_uds_listener(path, 0o660, os.getgid(), os.getgid())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)
            config = uvicorn.Config(service.create_app(docs_enabled=False), fd=listener.fileno(), access_log=False, lifespan="off", log_config=None)
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)
            client = socket.socket(socket.AF_UNIX)
            client.connect(path)
            client.close()
            server.should_exit = True
            thread.join(timeout=5)
            service._cleanup_uds(listener, path, identity)
            self.assertFalse(thread.is_alive())
            self.assertFalse(os.path.exists(path))

    def test_secure_uds_rejects_unsafe_parent_symlink_and_foreign_path(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            safe = os.path.join(root, "safe")
            os.mkdir(safe, 0o750)
            os.chmod(safe, 0o777)
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                service._secure_uds_listener(os.path.join(safe, "api.sock"), 0o660, os.getgid(), os.getgid())
            os.chmod(safe, 0o750)
            link = os.path.join(root, "link")
            os.symlink(safe, link)
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                service._secure_uds_listener(os.path.join(link, "api.sock"), 0o660, os.getgid(), os.getgid())
            path = os.path.join(safe, "api.sock")
            open(path, "w").close()
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                service._secure_uds_listener(path, 0o660, os.getgid(), os.getgid())

    def test_path_replacement_during_permission_setup_is_detected_and_preserved(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            os.chmod(root, 0o750)
            path = os.path.join(root, "api.sock")
            replacement = socket.socket(socket.AF_UNIX)
            real_chmod = service.os.chmod
            swapped = False

            def swap_then_chmod(target, mode, *args, **kwargs):
                nonlocal swapped
                if not swapped and str(target).startswith("/proc/self/fd/"):
                    swapped = True
                    os.unlink(path)
                    replacement.bind(path)
                return real_chmod(target, mode, *args, **kwargs)

            with mock.patch.object(service.os, "chmod", side_effect=swap_then_chmod), self.assertRaisesRegex(RuntimeError, "permission setup"):
                service._secure_uds_listener(path, 0o660, os.getgid(), os.getgid())
            self.assertTrue(os.path.exists(path))
            self.assertTrue(__import__("stat").S_ISSOCK(os.lstat(path).st_mode))
            replacement.close()
            os.unlink(path)

    def test_parent_path_replacement_does_not_redirect_bound_socket(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            parent = os.path.join(root, "parent")
            replacement = os.path.join(root, "replacement")
            os.mkdir(parent, 0o750)
            os.mkdir(replacement, 0o750)
            directory_fd, name = service._open_secure_parent(os.path.join(parent, "api.sock"), os.getgid())
            os.rename(parent, parent + ".old")
            os.rename(replacement, parent)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(f"/proc/self/fd/{directory_fd}/{name}")
            self.assertTrue(os.path.exists(os.path.join(parent + ".old", name)))
            self.assertFalse(os.path.exists(os.path.join(parent, name)))
            listener.close()
            os.unlink(name, dir_fd=directory_fd)
            os.close(directory_fd)

    def test_entrypoint_cleans_owned_socket_on_baseexception_without_unlinking_replacement(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            os.chmod(root, 0o750)
            path = os.path.join(root, "api.sock")
            settings = SimpleNamespace(
                forwarded_allow_ips="127.0.0.1",
                uvicorn_uds=path,
                uvicorn_socket_mode=0o660,
                uvicorn_socket_parent_gid=os.getgid(),
                uvicorn_socket_gid=os.getgid(),
            )
            with mock.patch.object(service, "_configure", return_value=settings), mock.patch.object(
                service.uvicorn, "run", side_effect=KeyboardInterrupt
            ), self.assertRaises(KeyboardInterrupt):
                service.run()
            self.assertFalse(os.path.exists(path))

            listener, identity = service._secure_uds_listener(path, 0o660, os.getgid(), os.getgid())
            os.unlink(path)
            replacement = socket.socket(socket.AF_UNIX)
            replacement.bind(path)
            service._cleanup_uds(listener, path, identity)
            self.assertTrue(os.path.exists(path))
            replacement.close()
            os.unlink(path)

    def test_real_uds_origin_resolves_only_native_xff_from_trusted_socket_peer(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "api.sock")
            app = service.create_app(
                docs_enabled=False,
                allowed_hosts=("api.example.invalid",),
                forwarded_allow_ips="127.0.0.1",
                uds_peer_ip="127.0.0.1",
            )
            config = uvicorn.Config(app, uds=path, access_log=False, lifespan="off", log_config=None, proxy_headers=False)
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            settings = SimpleNamespace(api_ip_allowlist=(__import__("ipaddress").ip_network("192.0.2.10/32"),))
            with open(os.devnull, "w") as sink, mock.patch.object(service, "_settings", settings), mock.patch.object(
                service, "API_KEYS", ("phase5-uds-key",)
            ), mock.patch.object(job_repository, "get_job_by_ulid", return_value=None), contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                thread.start()
                deadline = time.monotonic() + 5
                while not os.path.exists(path) and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(path))
                connection = http.client.HTTPConnection("localhost", timeout=3)
                connection.sock = socket.socket(socket.AF_UNIX)
                connection.sock.connect(path)
                connection.request(
                    "GET",
                    "/api/budi95/result/missing",
                    headers={
                        "Host": "api.example.invalid",
                        "x-api-key": "phase5-uds-key",
                        "X-Forwarded-For": "192.0.2.10",
                        "Forwarded": "for=203.0.113.9",
                        "X-Real-IP": "203.0.113.9",
                        "CF-Connecting-IP": "203.0.113.9",
                    },
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                self.assertEqual(json.loads(response.read()), {"detail": "Job not found"})
                connection.close()
            server.should_exit = True
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
