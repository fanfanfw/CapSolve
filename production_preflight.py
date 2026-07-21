from __future__ import annotations

import argparse
import grp
import hashlib
import http.client
import json
import os
import pwd
import re
import socket
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from settings import load_settings


MAX_EVIDENCE_BYTES = 64 * 1024
RPO_HOURS = 24
MAX_RTO_MINUTES = 60
EVIDENCE_KEYS = {"schema_version", "generated_at_utc", "purge_timer", "backup"}
TIMER_KEYS = {"service_unit", "timer_unit", "interval_seconds"}
RUNTIME_TIMER_KEYS = {"load_state", "active_state", "unit_file_state", "unit", "schedule", "persistent", "random_delay", "last_trigger", "next_elapse"}
BACKUP_KEYS = {
    "backup_retention_hours",
    "rpo_hours",
    "rto_minutes",
    "last_success_at_utc",
    "artifact_id",
    "artifact_basename",
    "artifact_sha256",
    "checksum_verified_at_utc",
    "restore_started_at_utc",
    "restore_verified_at_utc",
    "restore_duration_seconds",
    "source_row_count",
    "restored_row_count",
}
TIMER_POLICIES = {
    "capsolve-worker.timer": ("capsolve-worker.service", "*-*-* *:*:00", 60),
    "capsolve-purge.timer": ("capsolve-purge.service", "*:0/30:00", 1800),
    "capsolve-backup.timer": ("capsolve-backup.service", "*-*-* 00:00:00 UTC", 86400),
}
UNIT_NAMES = (
    "capsolve-api.service", "capsolve-xvfb.service", "capsolve-worker.service", "capsolve-worker.timer",
    "capsolve-purge.service", "capsolve-purge.timer", "capsolve-backup.service", "capsolve-backup.timer",
    "capsolve-ingress-permissions.service", "capsolve-ingress-permissions.path",
)
UNIT_PROPERTIES = (
    "LoadState", "ActiveState", "UnitFileState", "FragmentPath", "DropInPaths", "User", "Group",
    "SupplementaryGroups", "ExecStart", "EnvironmentFiles", "ReadWritePaths", "UMask", "NoNewPrivileges",
    "ProtectSystem", "ProtectHome", "PrivateTmp", "KillMode", "TimeoutStopUSec", "MemoryHigh", "MemoryMax",
    "TasksMax", "CPUQuotaPerSecUSec", "Result", "Unit", "TimersCalendar", "Persistent", "RandomizedDelayUSec",
    "LastTriggerUSec", "NextElapseUSecRealtime",
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid preflight arguments") from None


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid evidence timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid evidence timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("invalid evidence timestamp")
    return parsed


def _systemd_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid systemd timer timestamp") from None


def _integer(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("invalid evidence integer")
    return value


def _secure_json(path: Path, *, expected_uid: int = 0) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("evidence file is unavailable or unsafe") from None
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != expected_uid or stat.S_IMODE(details.st_mode) & ~0o600 or details.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence file ownership, mode, or size is unsafe")
        chunks = []
        size = 0
        while chunk := os.read(fd, min(8192, MAX_EVIDENCE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence file is too large")
        value = json.loads(b"".join(chunks))
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise ValueError("evidence must be an object")
    return value


def _secure_environment(path: Path, *, expected_uid: int = 0) -> dict[str, str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("environment file is unavailable or unsafe") from None
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != expected_uid or stat.S_IMODE(details.st_mode) != 0o600 or details.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("environment file ownership, mode, or size is unsafe")
        raw = os.read(fd, MAX_EVIDENCE_BYTES + 1).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("environment file is invalid") from None
    finally:
        os.close(fd)
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values or not value or "<REQUIRED_" in value or "REPLACE_" in value:
            raise ValueError("environment file is invalid")
        values[name] = value
    return values


def _validate_component_environments(directory: Path, *, expected_uid: int = 0, api_uid: int | None = None) -> dict[str, dict[str, str]]:
    values = {component: _secure_environment(directory / f"{component}.env", expected_uid=expected_uid) for component in ("api", "worker", "purge", "backup")}
    settings = {
        "api": load_settings("api", values["api"], api_clients_file_uid=expected_uid if api_uid is None else api_uid),
        "worker": load_settings("worker", values["worker"]),
        "purge": load_settings("purge", values["purge"]),
    }
    inbound = {"API_KEY", "API_KEYS", "API_CLIENTS_FILE", "API_IP_ALLOWLIST", "ALLOWED_HOSTS", "API_DOCS_ENABLED", "FORWARDED_ALLOW_IPS", "UVICORN_UDS", "UVICORN_SOCKET_MODE", "UVICORN_SOCKET_GID", "UVICORN_SOCKET_PARENT_GID"}
    fallback = {"LOCAL_POST_URL", "TURNSTILE_SITEURL", "TURNSTILE_SITEKEY"}
    private_solver = {"TS_PROFILE_DIR", "HOME", "BUDI95_CONFIG_CACHE_FILE"}
    shared_solver = {"DISPLAY", "ENABLE_XVFB_VIRTUAL_DISPLAY", "SOLVER_TIMEOUT", "LOCAL_POST_TIMEOUT", "BUDI95_AUTO_RESOLVE", "BUDI95_CONFIG_URL", "BUDI95_CONFIG_CACHE_SECONDS", "BUDI95_CONFIG_FETCH_TIMEOUT", "BUDI95_FORCE_ENV_CONFIG", *fallback}
    database = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CONNECT_TIMEOUT"}
    api_queue = {"JOB_QUEUE_CAPACITY", "JOB_QUEUE_RETRY_AFTER_SECONDS", "BUDI95_SUBMIT_RATE_LIMIT_PER_MINUTE", "BUDI95_READ_RATE_LIMIT_PER_MINUTE", "JOB_MAX_ATTEMPTS", "JOB_RETENTION_HOURS", "SYNC_QUEUE_MAX_WAITING", "MAX_WORKERS", "GLOBAL_CHROME_SLOTS"}
    worker_queue = {"JOB_BATCH_LIMIT", "JOB_MAX_ATTEMPTS", "JOB_RESET_STALE_MINUTES", "JOB_RETENTION_HOURS", "GLOBAL_CHROME_SLOTS"}
    allowed = {
        "api": {"ENVIRONMENT", *inbound, *api_queue, *database, *shared_solver, *private_solver},
        "worker": {"ENVIRONMENT", *worker_queue, *database, *shared_solver, *private_solver},
        "purge": {"ENVIRONMENT", "JOB_RETENTION_HOURS", "PURGE_BATCH_LIMIT", *database},
    }
    common = {"ENVIRONMENT", "JOB_RETENTION_HOURS", "DB_HOST", "DB_PORT", "DB_NAME", "DB_CONNECT_TIMEOUT"}
    api_worker_common = {"GLOBAL_CHROME_SLOTS", "JOB_MAX_ATTEMPTS", *shared_solver}
    fallback_required = any(settings[component].budi95_force_env_config or not settings[component].budi95_auto_resolve for component in ("api", "worker"))
    backup_required = {"CAPSOLVE_BACKUP_DIR", "CAPSOLVE_BACKUP_EVIDENCE", "CAPSOLVE_BACKUP_PGSERVICE", "CAPSOLVE_BACKUP_RETENTION_HOURS", "PGSERVICEFILE", "PGPASSFILE"}
    backup = values["backup"]
    backup_dir = Path(backup.get("CAPSOLVE_BACKUP_DIR", ""))
    backup_evidence = Path(backup.get("CAPSOLVE_BACKUP_EVIDENCE", ""))
    if (
        any(set(values[component]) - allowed[component] for component in ("api", "worker", "purge"))
        or {"ENVIRONMENT", "JOB_RETENTION_HOURS", *database} - values["api"].keys()
        or {"API_IP_ALLOWLIST", "ALLOWED_HOSTS", "API_DOCS_ENABLED", "FORWARDED_ALLOW_IPS", "UVICORN_UDS", "UVICORN_SOCKET_GID", "GLOBAL_CHROME_SLOTS", *shared_solver, *private_solver} - values["api"].keys()
        or not ({"API_KEY", "API_KEYS", "API_CLIENTS_FILE"} & values["api"].keys())
        or {"ENVIRONMENT", "JOB_RETENTION_HOURS", "JOB_BATCH_LIMIT", "JOB_MAX_ATTEMPTS", "JOB_RESET_STALE_MINUTES", "GLOBAL_CHROME_SLOTS", *database, *shared_solver, *private_solver} - values["worker"].keys()
        or {"ENVIRONMENT", "JOB_RETENTION_HOURS", "PURGE_BATCH_LIMIT", *database} - values["purge"].keys()
        or any(len({values[component][name] for component in ("api", "worker", "purge")}) != 1 for name in common)
        or any(values["api"][name] != values["worker"][name] for name in api_worker_common)
        or fallback_required and any(name not in values[component] for component in ("api", "worker") for name in fallback)
        or len({values[component]["DB_USER"] for component in ("api", "worker", "purge")}) != 3
        or any(values["api"][name] == values["worker"][name] for name in private_solver)
        or set(backup) != backup_required
        or not backup_dir.is_absolute()
        or backup_evidence.parent != backup_dir
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", backup.get("CAPSOLVE_BACKUP_PGSERVICE", ""))
        or backup.get("CAPSOLVE_BACKUP_RETENTION_HOURS") != values["purge"]["JOB_RETENTION_HOURS"]
    ):
        raise ValueError("component environment boundary is invalid")
    return values


def _systemctl_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", unit, f"--property={','.join(properties)}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode != 0 or set(values) != set(properties):
        raise ValueError("required unit state is unavailable")
    return values


def _systemctl_components() -> dict[str, dict[str, str]]:
    return {unit: _systemctl_show(unit, UNIT_PROPERTIES) for unit in UNIT_NAMES}


def _validate_component_states(states: dict[str, dict[str, str]]) -> None:
    active = {"capsolve-api.service", "capsolve-xvfb.service", "capsolve-worker.timer", "capsolve-purge.timer", "capsolve-backup.timer", "capsolve-ingress-permissions.path"}
    oneshot = {"capsolve-worker.service", "capsolve-purge.service", "capsolve-backup.service", "capsolve-ingress-permissions.service"}
    if (
        set(states) != set(UNIT_NAMES)
        or any(states[unit].get("LoadState") != "loaded" or states[unit].get("ActiveState") != "active" or states[unit].get("UnitFileState") != "enabled" for unit in active)
        or any(states[unit].get("LoadState") != "loaded" or states[unit].get("ActiveState") == "failed" for unit in oneshot)
        or states["capsolve-ingress-permissions.service"].get("Result") != "success"
    ):
        raise ValueError("required units are not operational")


def _validate_installed_units(states: dict[str, dict[str, str]], reviewed_directory: Path) -> None:
    for unit, properties in states.items():
        fragment = Path(properties["FragmentPath"])
        details = fragment.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022 or properties["DropInPaths"]:
            raise ValueError("installed unit artifact is unsafe")
        if fragment.read_bytes() != (reviewed_directory / unit).read_bytes():
            raise ValueError("installed unit artifact differs from reviewed artifact")
    api = states["capsolve-api.service"]
    worker = states["capsolve-worker.service"]
    if api["User"] == worker["User"] or "capsolve-nginx" in worker["SupplementaryGroups"].split():
        raise ValueError("API and worker identity boundary is invalid")


def _systemctl_timer(unit: str = "capsolve-purge.timer") -> dict[str, Any]:
    properties = _systemctl_show(unit, ("LoadState", "ActiveState", "UnitFileState", "Unit", "TimersCalendar", "Persistent", "RandomizedDelayUSec", "NextElapseUSecRealtime", "LastTriggerUSec"))
    return {
        "load_state": properties["LoadState"],
        "active_state": properties["ActiveState"],
        "unit_file_state": properties["UnitFileState"],
        "unit": properties["Unit"],
        "schedule": properties["TimersCalendar"],
        "persistent": properties["Persistent"],
        "random_delay": properties["RandomizedDelayUSec"],
        "next_elapse": _systemd_timestamp(properties["NextElapseUSecRealtime"]),
        "last_trigger": _systemd_timestamp(properties["LastTriggerUSec"]),
    }


def _validate_runtime_timers(timers: dict[str, dict[str, Any]], now: datetime) -> None:
    if set(timers) != set(TIMER_POLICIES):
        raise ValueError("runtime timer state is incomplete")
    for unit, (target, schedule, maximum_seconds) in TIMER_POLICIES.items():
        timer = timers[unit]
        if set(timer) != RUNTIME_TIMER_KEYS:
            raise ValueError("runtime timer state is incomplete")
        gap = timer["next_elapse"] - timer["last_trigger"]
        if timer["load_state"] != "loaded" or timer["active_state"] != "active" or timer["unit_file_state"] != "enabled" or timer["unit"] != target or schedule not in timer["schedule"] or timer["persistent"] != "yes" or timer["random_delay"] != "0" or gap.total_seconds() != maximum_seconds or timer["last_trigger"] > now or timer["next_elapse"] <= now:
            raise ValueError("runtime timer policy is not met")


def _validate_unit_artifacts(directory: Path, interval_seconds: int) -> None:
    required = {
        "capsolve-api.service": ("User=capsolve-api", "Group=capsolve-nginx", "ReadWritePaths=/run/capsolve/uvicorn /var/lib/capsolve-api"),
        "capsolve-worker.service": ("Type=oneshot", "User=capsolve-worker", "Group=capsolve-worker", "ReadWritePaths=/var/lib/capsolve-worker"),
        "capsolve-purge.service": ("Type=oneshot", "User=capsolve-purge"),
        "capsolve-backup.service": ("Type=oneshot", "User=capsolve-backup", "scheduled-backup", "--source-pgservice ${CAPSOLVE_BACKUP_PGSERVICE}"),
        "capsolve-xvfb.service": ("User=capsolve-xvfb",),
        "capsolve-worker.timer": ("OnCalendar=*-*-* *:*:00", "Persistent=true", "RandomizedDelaySec=0", "Unit=capsolve-worker.service"),
        "capsolve-purge.timer": ("OnCalendar=*:0/30:00", "Persistent=true", "Unit=capsolve-purge.service"),
        "capsolve-backup.timer": ("OnCalendar=*-*-* 00:00:00 UTC", "Persistent=true", "Unit=capsolve-backup.service"),
        "capsolve-ingress-permissions.service": ("Type=oneshot", "User=root", "secure_nginx_ingress.py", "ReadWritePaths=/run/capsolve/ingress"),
        "capsolve-ingress-permissions.path": ("PathChanged=/run/capsolve/ingress/cloudflared.sock", "Unit=capsolve-ingress-permissions.service"),
    }
    for name, fragments in required.items():
        content = (directory / name).read_text(encoding="utf-8")
        if any(fragment not in content for fragment in fragments):
            raise ValueError("deployment unit artifacts are inconsistent")
    if interval_seconds != 1800:
        raise ValueError("purge unit artifacts are inconsistent")


def _validate_identities() -> None:
    api = _systemctl_show("capsolve-api.service", ("User", "Group", "SupplementaryGroups"))
    worker = _systemctl_show("capsolve-worker.service", ("User", "Group", "SupplementaryGroups"))
    api_user = pwd.getpwnam("capsolve-api")
    worker_user = pwd.getpwnam("capsolve-worker")
    nginx_group = grp.getgrnam("capsolve-nginx")
    worker_groups = {group.gr_name for group in grp.getgrall() if worker_user.pw_name in group.gr_mem} | {grp.getgrgid(worker_user.pw_gid).gr_name}
    if api["User"] != api_user.pw_name or api["Group"] != nginx_group.gr_name or worker["User"] != worker_user.pw_name or worker["Group"] != "capsolve-worker" or 0 in {api_user.pw_uid, worker_user.pw_uid} or api_user.pw_uid == worker_user.pw_uid or nginx_group.gr_name in worker["SupplementaryGroups"].split() or nginx_group.gr_name in worker_groups:
        raise ValueError("API and worker identity boundary is invalid")


def _validate_private_directories(environments: dict[str, dict[str, str]]) -> None:
    identities = []
    for component in ("api", "worker"):
        path = Path(environments[component]["TS_PROFILE_DIR"])
        details = path.lstat()
        expected_uid = pwd.getpwnam(f"capsolve-{component}").pw_uid
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != expected_uid or stat.S_IMODE(details.st_mode) != 0o700:
            raise ValueError("Chrome profile directory boundary is invalid")
        identities.append((details.st_dev, details.st_ino))
    if len(set(identities)) != 2:
        raise ValueError("Chrome profile directories are not isolated")


def _validate_runtime_path(path: Path, kind: str, user: str, group: str, mode: int) -> None:
    details = path.lstat()
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISSOCK if kind == "socket" else stat.S_ISREG
    if not expected_type(details.st_mode) or details.st_uid != pwd.getpwnam(user).pw_uid or details.st_gid != grp.getgrnam(group).gr_gid or stat.S_IMODE(details.st_mode) != mode:
        raise ValueError("runtime path boundary is invalid")


def _validate_socket(path: str) -> None:
    socket_path = Path(path)
    _validate_runtime_path(socket_path.parent, "directory", "capsolve-api", "capsolve-nginx", 0o770)
    _validate_runtime_path(socket_path, "socket", "capsolve-api", "capsolve-nginx", 0o660)


def _validate_backup_paths(environments: dict[str, dict[str, str]], evidence: dict[str, Any]) -> Path:
    backup = environments["backup"]
    uid = pwd.getpwnam("capsolve-backup").pw_uid
    for name in ("PGSERVICEFILE", "PGPASSFILE"):
        path = Path(backup[name])
        if not path.is_absolute():
            raise ValueError("backup credential boundary is invalid")
        _validate_runtime_path(path, "file", "capsolve-backup", "capsolve-backup", 0o600)
    directory = Path(backup["CAPSOLVE_BACKUP_DIR"])
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("backup directory boundary is invalid")
    _validate_runtime_path(directory, "directory", "capsolve-backup", "capsolve-backup", 0o700)
    _validate_runtime_path(Path(backup["CAPSOLVE_BACKUP_EVIDENCE"]), "file", "capsolve-backup", "capsolve-backup", 0o600)
    return directory


def _hash_regular_file(path: Path, uid: int, gid: int) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_gid != gid or stat.S_IMODE(before.st_mode) != 0o600:
            raise ValueError("backup artifact boundary is invalid")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("backup artifact changed during verification")
        return digest.hexdigest()
    finally:
        os.close(fd)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=5)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def _readiness(path: str, host: str) -> None:
    connection = _UnixHTTPConnection(path)
    try:
        connection.request("GET", "/api/ready", headers={"Host": host})
        response = connection.getresponse()
        body = response.read(1024)
        if response.status != 200 or json.loads(body) != {"status": "ready"}:
            raise ValueError("API readiness failed")
    finally:
        connection.close()


def _validate_installed_proxy_config() -> None:
    nginx = Path("/etc/nginx/sites-enabled/capsolve").read_text(encoding="utf-8")
    cloudflared = Path("/etc/cloudflared/config.yml").read_text(encoding="utf-8")
    required = ("listen unix:/run/capsolve/ingress/cloudflared.sock", "server unix:/run/capsolve/uvicorn/api.sock", "access_log off", "real_ip_header CF-Connecting-IP", "proxy_set_header Forwarded \"\"", "proxy_set_header X-Real-IP \"\"", "proxy_set_header CF-Connecting-IP \"\"", "proxy_set_header X-Forwarded-For $capsolve_client_ip")
    if any(value not in nginx for value in required) or re.search(r"(?m)^\s*listen\s+(?!unix:)", nginx) or "service: unix:/run/capsolve/ingress/cloudflared.sock" not in cloudflared or any(marker in nginx + cloudflared for marker in ("<REQUIRED_", "REPLACE_")):
        raise ValueError("installed proxy configuration is unsafe")


def _validate_proxy_identities() -> None:
    nginx = _systemctl_show("nginx.service", ("User", "Group", "SupplementaryGroups"))
    cloudflared = _systemctl_show("cloudflared.service", ("User", "Group", "SupplementaryGroups"))
    if nginx["User"] not in {"", "root"} or "capsolve-nginx" not in nginx["SupplementaryGroups"].split() or cloudflared["User"] in {"", "root"} or cloudflared["Group"] != "cloudflared":
        raise ValueError("proxy identity boundary is invalid")
    _validate_runtime_path(Path("/run/capsolve/ingress"), "directory", "root", "cloudflared", 0o710)
    _validate_runtime_path(Path("/run/capsolve/ingress/cloudflared.sock"), "socket", "root", "cloudflared", 0o660)


def _validate_no_api_tcp_listener() -> None:
    inodes = set()
    for unit in ("capsolve-api.service",):
        main_pid = _systemctl_show(unit, ("MainPID",))["MainPID"]
        if not main_pid.isdigit() or main_pid == "0":
            raise ValueError("required process identity is unavailable")
        tasks = Path(f"/proc/{main_pid}/task").iterdir()
        for task in tasks:
            for descriptor in (task / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match:
                    inodes.add(match.group(1))
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        for line in table.read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                raise ValueError("unexpected CapSolve TCP listener")


def validate(
    values: Mapping[str, str],
    evidence: dict[str, Any],
    now: datetime,
    *,
    runtime_timer: dict[str, Any] | None = None,
    unit_directory: Path | None = None,
    backup_directory: Path | None = None,
) -> None:
    if values.get("ENVIRONMENT", "").strip() != "production":
        raise ValueError("production environment is required")
    settings = load_settings("purge", values)
    if set(evidence) != EVIDENCE_KEYS or evidence.get("schema_version") != 1:
        raise ValueError("invalid evidence schema")
    generated = _timestamp(evidence["generated_at_utc"])
    if generated > now or now - generated > timedelta(hours=RPO_HOURS):
        raise ValueError("evidence is stale")
    timer = evidence.get("purge_timer")
    if not isinstance(timer, dict) or set(timer) != TIMER_KEYS:
        raise ValueError("invalid purge timer evidence")
    interval = _integer(timer["interval_seconds"], minimum=1)
    if timer["service_unit"] != "capsolve-purge.service" or timer["timer_unit"] != "capsolve-purge.timer" or interval >= settings.job_retention_hours * 3600:
        raise ValueError("purge timer evidence is invalid")
    if unit_directory is not None:
        _validate_unit_artifacts(unit_directory, interval)
    if runtime_timer is not None:
        if set(runtime_timer) != RUNTIME_TIMER_KEYS:
            raise ValueError("invalid runtime timer state")
        gap = runtime_timer["next_elapse"] - runtime_timer["last_trigger"]
        if runtime_timer["load_state"] != "loaded" or runtime_timer["active_state"] != "active" or runtime_timer["unit_file_state"] != "enabled" or gap <= timedelta(0) or gap.total_seconds() >= settings.job_retention_hours * 3600 or runtime_timer["next_elapse"] <= now:
            raise ValueError("purge timer is not operational")
    backup = evidence.get("backup")
    if not isinstance(backup, dict) or set(backup) != BACKUP_KEYS:
        raise ValueError("invalid backup evidence")
    retention = _integer(backup["backup_retention_hours"], minimum=1)
    rpo = _integer(backup["rpo_hours"], minimum=1)
    rto = _integer(backup["rto_minutes"], minimum=1)
    duration = _integer(backup["restore_duration_seconds"])
    source = _integer(backup["source_row_count"])
    restored = _integer(backup["restored_row_count"])
    if retention > settings.job_retention_hours or rpo != RPO_HOURS or rto > MAX_RTO_MINUTES:
        raise ValueError("backup policy is not aligned")
    last_success = _timestamp(backup["last_success_at_utc"])
    checksum_at = _timestamp(backup["checksum_verified_at_utc"])
    restore_started = _timestamp(backup["restore_started_at_utc"])
    restore_at = _timestamp(backup["restore_verified_at_utc"])
    if not last_success <= checksum_at <= generated or not last_success <= restore_started <= restore_at <= generated:
        raise ValueError("backup evidence timestamps are incoherent")
    if now - last_success > timedelta(hours=RPO_HOURS) or duration != int((restore_at - restore_started).total_seconds()) or duration > rto * 60:
        raise ValueError("backup RPO or restore RTO is not met")
    if not isinstance(backup["artifact_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", backup["artifact_id"]) or not isinstance(backup["artifact_basename"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", backup["artifact_basename"]) or not isinstance(backup["artifact_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", backup["artifact_sha256"]):
        raise ValueError("invalid backup artifact identity")
    if restored != source:
        raise ValueError("restore row count mismatch")
    if backup_directory is not None:
        artifact = backup_directory / backup["artifact_basename"]
        if artifact.parent != backup_directory:
            raise ValueError("backup artifact is unavailable")
        identity = pwd.getpwnam("capsolve-backup")
        if _hash_regular_file(artifact, identity.pw_uid, grp.getgrnam("capsolve-backup").gr_gid) != backup["artifact_sha256"]:
            raise ValueError("backup artifact checksum mismatch")


def main() -> int:
    mode = "runtime"
    exit_status = 1
    try:
        parser = SafeArgumentParser(add_help=False)
        parser.add_argument("--help", action="store_true")
        parser.add_argument("--evidence")
        parser.add_argument("--environment-directory", default="/etc/capsolve")
        parser.add_argument("--static", action="store_true")
        args = parser.parse_args()
        mode = "static" if args.static else "runtime"
        if args.help:
            print(json.dumps({"event": "production_preflight_help", "mode": mode, "operational_ready": False, "exit_status": 0, "help": "Options: --evidence FILE, --static"}))
            return 0
        if not args.evidence:
            raise ValueError("evidence is required")
        evidence = _secure_json(Path(args.evidence))
        try:
            api_uid = pwd.getpwnam("capsolve-api").pw_uid
        except KeyError:
            api_uid = os.geteuid()
        component_environments = _validate_component_environments(Path(args.environment_directory), api_uid=api_uid)
        now = datetime.now(timezone.utc)
        if args.static:
            validate(component_environments["purge"], evidence, now, unit_directory=Path(__file__).with_name("deployment"))
        else:
            backup_directory = _validate_backup_paths(component_environments, evidence)
            validate(component_environments["purge"], evidence, now, backup_directory=backup_directory)
            states = _systemctl_components()
            _validate_component_states(states)
            _validate_installed_units(states, Path(__file__).with_name("deployment"))
            _validate_runtime_timers({unit: _systemctl_timer(unit) for unit in TIMER_POLICIES}, now)
            _validate_identities()
            _validate_private_directories(component_environments)
            socket_path = component_environments["api"]["UVICORN_UDS"]
            _validate_socket(socket_path)
            _validate_proxy_identities()
            _validate_installed_proxy_config()
            _readiness(socket_path, component_environments["api"]["ALLOWED_HOSTS"].split(",")[0])
            _validate_no_api_tcp_listener()
        exit_status = 0
    except BaseException:
        pass
    print(json.dumps({
        "event": "production_preflight_complete" if exit_status == 0 else "production_preflight_error",
        "mode": mode,
        "operational_ready": exit_status == 0 and mode == "runtime",
        "exit_status": exit_status,
    }))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
