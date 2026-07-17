from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "deployment" / "secure_nginx_ingress.py"
SPEC = importlib.util.spec_from_file_location("secure_nginx_ingress", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Phase5IngressLifecycleTest(unittest.TestCase):
    def test_directory_opens_only_after_socket_is_0660(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            directory = os.path.join(root, "ingress")
            os.mkdir(directory, 0o700)
            path = os.path.join(directory, "cloudflared.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen()
                self.assertNotEqual(os.stat(path).st_mode & 0o777, 0o660)
                MODULE.secure(directory, path, os.getuid(), os.getgid(), os.getuid())
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o710)

    def test_unauthorized_process_cannot_connect_before_or_after_transition(self):
        if os.getuid() == 0:
            self.skipTest("test expects an unprivileged current uid")
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            os.chmod(root, 0o700)
            directory = os.path.join(root, "ingress")
            os.mkdir(directory, 0o700)
            path = os.path.join(directory, "cloudflared.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen()
                command = [sys.executable, "-c", "import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])", path]
                os.chmod(path, 0)
                self.assertNotEqual(subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)
                os.chmod(path, 0o777)
                MODULE.secure(directory, path, os.getuid(), os.getgid(), os.getuid())
                os.chmod(path, 0)
                self.assertNotEqual(subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)

    def test_path_replacement_during_chmod_is_detected(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            directory = os.path.join(root, "ingress")
            os.mkdir(directory, 0o700)
            path = os.path.join(directory, "cloudflared.sock")
            original = socket.socket(socket.AF_UNIX)
            original.bind(path)
            replacement = socket.socket(socket.AF_UNIX)
            real_chmod = MODULE.os.chmod
            swapped = False
            def swap(target, mode, *args, **kwargs):
                nonlocal swapped
                if not swapped and str(target).startswith("/proc/self/fd/"):
                    swapped = True
                    os.unlink(path)
                    replacement.bind(path)
                return real_chmod(target, mode, *args, **kwargs)
            with mock.patch.object(MODULE.os, "chmod", side_effect=swap), self.assertRaises(SystemExit):
                MODULE.secure(directory, path, os.getuid(), os.getgid(), os.getuid())
            self.assertTrue(os.path.exists(path))
            original.close()
            replacement.close()
            os.unlink(path)

    def test_rejects_non_socket_and_insecure_parent(self):
        runtime = f"/run/user/{os.getuid()}"
        with tempfile.TemporaryDirectory(dir=runtime if os.path.isdir(runtime) else None) as root:
            directory = os.path.join(root, "ingress")
            os.mkdir(directory, 0o700)
            path = os.path.join(directory, "cloudflared.sock")
            Path(path).touch()
            with self.assertRaises(SystemExit):
                MODULE.secure(directory, path, os.getuid(), os.getgid(), os.getuid())
            os.unlink(path)
            os.chmod(directory, 0o710)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                with self.assertRaises(SystemExit):
                    MODULE.secure(directory, path, os.getuid(), os.getgid(), os.getuid())


if __name__ == "__main__":
    unittest.main()
