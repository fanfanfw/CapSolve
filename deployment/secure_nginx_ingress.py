#!/usr/bin/env python3
"""Open the nginx ingress directory only after its Unix socket is secured."""

import grp
import os
import stat
import sys


def _open_directory(path: str, expected_uid: int) -> int:
    if not os.path.isabs(path):
        raise SystemExit("unsafe nginx ingress directory")
    descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.split(os.sep)[1:]
        for index, part in enumerate(parts):
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            if index == len(parts) - 1:
                if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise SystemExit("unsafe nginx ingress directory")
            elif info.st_uid not in {0, expected_uid} or stat.S_IMODE(info.st_mode) & 0o022:
                raise SystemExit("unsafe nginx ingress directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def secure(directory: str, path: str, uid: int, gid: int, directory_uid: int = 0) -> None:
    if os.path.dirname(path) != directory or os.path.basename(path) in {"", ".", ".."}:
        raise SystemExit("unsafe nginx ingress socket")
    directory_fd = _open_directory(directory, directory_uid)
    object_fd = None
    name = os.path.basename(path)
    try:
        object_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd)
        socket_info = os.fstat(object_fd)
        identity = (socket_info.st_dev, socket_info.st_ino)
        if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != uid:
            raise SystemExit("unsafe nginx ingress socket")
        os.chown(f"/proc/self/fd/{object_fd}", uid, gid)
        os.chmod(f"/proc/self/fd/{object_fd}", 0o660)
        verified = os.fstat(object_fd)
        path_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (verified.st_dev, verified.st_ino) != identity
            or (path_info.st_dev, path_info.st_ino) != identity
            or not stat.S_ISSOCK(verified.st_mode)
            or verified.st_uid != uid
            or verified.st_gid != gid
            or stat.S_IMODE(verified.st_mode) != 0o660
        ):
            raise SystemExit("nginx ingress socket verification failed")
        os.fchown(directory_fd, directory_uid, gid)
        os.fchmod(directory_fd, 0o710)
    finally:
        if object_fd is not None:
            os.close(object_fd)
        os.close(directory_fd)


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit("usage: secure_nginx_ingress.py DIRECTORY SOCKET NGINX_UID CLOUDFLARED_GID")
    directory, path, uid_text, gid_text = sys.argv[1:]
    secure(directory, path, int(uid_text), int(gid_text) if gid_text.isdigit() else grp.getgrnam(gid_text).gr_gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
