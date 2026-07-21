from __future__ import annotations

import argparse
import configparser
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import py_compile
import statistics
import subprocess
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT = ROOT / "deployment"
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "dist"}
SENSITIVE_COLUMNS = {"nric", "api_key", "token", "password", "dsn", "database_url", "db_password"}


def _json(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")))


def _python_sources() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*.py") if not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts))


def _sectioned(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with path.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or name in values:
            raise ValueError(f"invalid environment example: {path.name}")
        values[name] = value
    return values


def validate_artifacts() -> dict[str, int | str]:
    services = {
        "capsolve-api.service": ("capsolve-api", "capsolve-nginx", "/etc/capsolve/api.env"),
        "capsolve-worker.service": ("capsolve-worker", "capsolve-worker", "/etc/capsolve/worker.env"),
        "capsolve-purge.service": ("capsolve-purge", "capsolve-purge", "/etc/capsolve/purge.env"),
        "capsolve-backup.service": ("capsolve-backup", "capsolve-backup", "/etc/capsolve/backup.env"),
        "capsolve-xvfb.service": ("capsolve-xvfb", "capsolve-xvfb", None),
    }
    parsed = {}
    for name, (user, group, environment_file) in services.items():
        unit = _sectioned(DEPLOYMENT / name)["Service"]
        parsed[name] = unit
        if unit.get("User") != user or unit.get("Group") != group or user == "root" or unit.get("UMask") != "0077":
            raise ValueError(f"unsafe service identity: {name}")
        if unit.get("NoNewPrivileges") != "true" or "MemoryMax" not in unit or "TasksMax" not in unit:
            raise ValueError(f"missing service hardening: {name}")
        if environment_file is not None and unit.get("EnvironmentFile") != environment_file:
            raise ValueError(f"wrong environment boundary: {name}")
        executable = unit.get("ExecStart", "").split()[0]
        if name != "capsolve-xvfb.service" and not executable.startswith("/opt/capsolve/.venv/bin/"):
            raise ValueError(f"service does not execute locked environment directly: {name}")
    api = parsed["capsolve-api.service"]
    worker = parsed["capsolve-worker.service"]
    backup = parsed["capsolve-backup.service"]
    if api.get("Restart") != "on-failure" or api.get("KillMode") != "control-group":
        raise ValueError("API restart/orphan policy is incomplete")
    if api.get("User") == worker.get("User") or "capsolve-nginx" in worker.get("SupplementaryGroups", "").split() or api.get("Group") != "capsolve-nginx":
        raise ValueError("API and worker socket boundary is invalid")
    if worker.get("Type") != "oneshot" or backup.get("Type") != "oneshot":
        raise ValueError("timer services must be non-overlapping oneshots")
    if "--dbname" in backup.get("ExecStart", "") or "postgresql://" in backup.get("ExecStart", "") or "service=${CAPSOLVE_BACKUP_PGSERVICE}" in backup.get("ExecStart", ""):
        raise ValueError("backup command exposes connection configuration")
    if "scheduled-backup" not in backup.get("ExecStart", "") or "--source-pgservice ${CAPSOLVE_BACKUP_PGSERVICE}" not in backup.get("ExecStart", ""):
        raise ValueError("backup command does not use the baseline workflow")

    timer_policy = {
        "capsolve-worker.timer": ("capsolve-worker.service", "*-*-* *:*:00", "1s"),
        "capsolve-purge.timer": ("capsolve-purge.service", "*:0/30:00", None),
        "capsolve-backup.timer": ("capsolve-backup.service", "*-*-* 00:00:00 UTC", None),
    }
    for name, (target, schedule, accuracy) in timer_policy.items():
        timer = _sectioned(DEPLOYMENT / name)["Timer"]
        if timer.get("Unit") != target or timer.get("Persistent") != "true" or timer.get("OnCalendar") != schedule or timer.get("RandomizedDelaySec") != "0":
            raise ValueError(f"timer policy is incomplete: {name}")
        if accuracy is not None and timer.get("AccuracySec") != accuracy:
            raise ValueError(f"timer freshness policy is incomplete: {name}")

    api_env = _env(DEPLOYMENT / "api.env.example")
    worker_env = _env(DEPLOYMENT / "worker.env.example")
    purge_env = _env(DEPLOYMENT / "purge.env.example")
    backup_env = _env(DEPLOYMENT / "backup.env.example")
    required_common = {"ENVIRONMENT", "JOB_RETENTION_HOURS", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CONNECT_TIMEOUT"}
    if any(not required_common <= values.keys() for values in (api_env, worker_env, purge_env)):
        raise ValueError("environment examples omit required common settings")
    if {"API_CLIENTS_FILE", "API_IP_ALLOWLIST", "ALLOWED_HOSTS", "FORWARDED_ALLOW_IPS", "UVICORN_UDS"} - api_env.keys():
        raise ValueError("API environment is incomplete")
    fallback = {"LOCAL_POST_URL", "TURNSTILE_SITEURL", "TURNSTILE_SITEKEY"}
    shared = fallback | {"GLOBAL_CHROME_SLOTS", "DISPLAY", "SOLVER_TIMEOUT", "LOCAL_POST_TIMEOUT"}
    if fallback - worker_env.keys() or any(api_env[name] != worker_env[name] for name in shared):
        raise ValueError("shared solver environment is incomplete or inconsistent")
    private = {"TS_PROFILE_DIR", "HOME", "BUDI95_CONFIG_CACHE_FILE"}
    if any(api_env[name] == worker_env[name] for name in private):
        raise ValueError("API and worker writable paths are not isolated")
    if {"API_KEYS", "API_KEY", "API_IP_ALLOWLIST", "ALLOWED_HOSTS"} & worker_env.keys():
        raise ValueError("worker environment contains inbound API settings")
    if set(purge_env) != required_common | {"DB_PORT", "PURGE_BATCH_LIMIT"}:
        raise ValueError("purge environment exceeds its component boundary")
    backup_required = {"CAPSOLVE_BACKUP_DIR", "CAPSOLVE_BACKUP_EVIDENCE", "CAPSOLVE_BACKUP_PGSERVICE", "CAPSOLVE_BACKUP_RETENTION_HOURS", "PGSERVICEFILE", "PGPASSFILE"}
    if set(backup_env) != backup_required or Path(backup_env["CAPSOLVE_BACKUP_EVIDENCE"]).parent != Path(backup_env["CAPSOLVE_BACKUP_DIR"]):
        raise ValueError("backup environment is invalid")
    if any(value and "<REQUIRED_" not in value and name.endswith(("PASSWORD", "KEYS")) for values in (api_env, worker_env, purge_env, backup_env) for name, value in values.items()):
        raise ValueError("environment example contains a credential value")

    nginx = (DEPLOYMENT / "capsolve-nginx.conf.example").read_text(encoding="utf-8")
    if nginx.count("{") != nginx.count("}") or "listen unix:" not in nginx or "listen 127." in nginx or "listen 0.0.0.0" in nginx:
        raise ValueError("nginx listener boundary is invalid")
    for directive in ("proxy_set_header X-Forwarded-For $capsolve_client_ip", "proxy_read_timeout", "limit_req zone=capsolve_api"):
        if directive not in nginx:
            raise ValueError("nginx forwarding, timeout, or rate policy is incomplete")
    cloudflared = (DEPLOYMENT / "cloudflared-config.yml.example").read_text(encoding="utf-8")
    if "service: unix:/run/capsolve/ingress/cloudflared.sock" not in cloudflared or "http_status:404" not in cloudflared:
        raise ValueError("cloudflared ingress boundary is invalid")
    ingress_service = _sectioned(DEPLOYMENT / "capsolve-ingress-permissions.service")["Service"]
    ingress_path = _sectioned(DEPLOYMENT / "capsolve-ingress-permissions.path")["Path"]
    if ingress_service.get("User") != "root" or "secure_nginx_ingress.py /run/capsolve/ingress /run/capsolve/ingress/cloudflared.sock 0 cloudflared" not in ingress_service.get("ExecStart", "") or ingress_path.get("PathChanged") != "/run/capsolve/ingress/cloudflared.sock":
        raise ValueError("ingress permission lifecycle is incomplete")
    journal = _sectioned(DEPLOYMENT / "journald@capsolve.conf.example")["Journal"]
    if not {"SystemMaxUse", "SystemKeepFree", "MaxRetentionSec", "RateLimitBurst"} <= journal.keys():
        raise ValueError("journal retention policy is incomplete")
    ignored = [subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False).returncode for path in (".env", "dist/")]
    if any(ignored):
        raise ValueError("release-local paths are not ignored")
    return {"systemd_units": 10, "environment_examples": 4, "nginx": 1, "cloudflared": 1, "journald": 1, "logrotate": "not_applicable_journal_only"}


def _safe_test_database(environment: dict[str, str]) -> tuple[dict[str, str], str]:
    value = environment.get("TEST_DATABASE_URL", "").strip()
    if not value:
        return environment, "PENDING_NO_LOCAL_DISPOSABLE_CREDENTIAL"
    parsed = urlparse(value)
    database_name = parsed.path.lstrip("/").lower()
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not any(marker in database_name for marker in ("test", "temp", "disposable")):
        clean = dict(environment)
        clean.pop("TEST_DATABASE_URL", None)
        return clean, "REJECTED_NON_DISPOSABLE_OR_NON_LOCAL_URL"
    return environment, "ENABLED_LOCAL_DISPOSABLE"


def _postgres_test_modules() -> list[str]:
    tests = ROOT / "tests"
    paths = set(tests.glob("test_*_postgres.py"))
    paths.update(tests / name for name in ("test_health_observability.py", "test_retention_recovery.py"))
    return [f"tests.{path.stem}" for path in sorted(paths)]


def quality() -> int:
    try:
        sources = _python_sources()
        for path in sources:
            py_compile.compile(str(path), doraise=True)
        artifacts = validate_artifacts()
        _benchmark_self_check()
        _alert_self_check()
    except BaseException:
        _json("quality_gate_complete", status="FAIL", stage="compile_or_static_validation")
        return 1
    _json("quality_compile_complete", source_count=len(sources))
    _json("quality_artifacts_complete", **artifacts)
    environment, postgres = _safe_test_database(dict(os.environ))
    if postgres == "ENABLED_LOCAL_DISPOSABLE" and not environment.get("PGPASSWORD", "").strip():
        environment = dict(environment)
        environment.pop("TEST_DATABASE_URL", None)
        postgres = "PENDING_NO_LOCAL_DISPOSABLE_CREDENTIAL"
    clean_environment = dict(environment)
    clean_environment.pop("TEST_DATABASE_URL", None)
    clean_environment.pop("PGPASSWORD", None)
    postgres_modules = _postgres_test_modules()
    non_postgres = [f"tests.{path.stem}" for path in sorted((ROOT / "tests").glob("test_*.py")) if f"tests.{path.stem}" not in postgres_modules]
    commands = [("self_check", [sys.executable, "self_check.py"], clean_environment), ("unit_contract", [sys.executable, "-m", "unittest", "-v", *non_postgres], clean_environment)]
    if postgres == "ENABLED_LOCAL_DISPOSABLE":
        commands.append(("postgres", [sys.executable, "-m", "unittest", "-v", *postgres_modules], environment))
    commands.append(("full_discovery", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py", "-v"], clean_environment))
    for stage, command, command_environment in commands:
        result = subprocess.run(command, cwd=ROOT, env=command_environment, check=False)
        if result.returncode:
            _json("quality_gate_complete", status="FAIL", stage=stage, postgres=postgres)
            return result.returncode
    _json("quality_gate_complete", status="PASS", self_check="PASS", unit_contract="PASS", full_discovery="PASS", postgres=postgres)
    return 0


def evaluate_alerts(metrics: dict) -> dict[str, str]:
    required = {
        "queue_depth", "queue_capacity", "stale_processing_count", "readiness_ok", "worker_age_seconds",
        "memory_current_bytes", "memory_max_bytes", "disk_used_percent", "inode_used_percent",
        "cpu_sustained_percent", "config_resolver_errors",
    }
    if required - metrics.keys():
        raise ValueError("alert metrics are incomplete")
    checks = {
        "queue_capacity": "FAIL" if metrics["queue_depth"] >= metrics["queue_capacity"] * 0.8 else "PASS",
        "stale_processing": "FAIL" if metrics["stale_processing_count"] > 0 else "PASS",
        "readiness": "PASS" if metrics["readiness_ok"] is True else "FAIL",
        "worker_freshness": "FAIL" if metrics["worker_age_seconds"] > 180 else "PASS",
        "memory": "FAIL" if metrics["memory_current_bytes"] >= metrics["memory_max_bytes"] * 0.8 else "PASS",
        "disk": "FAIL" if metrics["disk_used_percent"] >= 80 else "PASS",
        "inode": "FAIL" if metrics["inode_used_percent"] >= 80 else "PASS",
        "cpu": "FAIL" if metrics["cpu_sustained_percent"] >= 70 else "PASS",
        "failed_ratio": "PENDING" if metrics.get("failed_ratio_threshold") is None or metrics.get("failed_ratio") is None else "FAIL" if metrics["failed_ratio"] >= metrics["failed_ratio_threshold"] else "PASS",
        "oldest_pending": "PENDING" if metrics.get("oldest_pending_sla_seconds") is None or metrics.get("oldest_pending_age_seconds") is None else "FAIL" if metrics["oldest_pending_age_seconds"] >= metrics["oldest_pending_sla_seconds"] else "PASS",
        "config_resolver": "FAIL" if metrics["config_resolver_errors"] > 0 or metrics.get("config_source") == "env" else "PASS" if metrics.get("config_source") in {"cache", "website"} else "PENDING",
    }
    return checks


def alerts(input_path: str) -> int:
    try:
        metrics = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if not isinstance(metrics, dict):
            raise ValueError
        checks = evaluate_alerts(metrics)
    except BaseException:
        _json("alert_check_complete", status="FAIL", checks={})
        return 1
    status = "FAIL" if "FAIL" in checks.values() else "PENDING" if "PENDING" in checks.values() else "PASS"
    _json("alert_check_complete", status=status, checks=checks)
    return int(status == "FAIL")


def _alert_self_check() -> None:
    checks = evaluate_alerts({"queue_depth": 8, "queue_capacity": 10, "stale_processing_count": 0, "readiness_ok": True, "worker_age_seconds": 60, "memory_current_bytes": 7, "memory_max_bytes": 10, "disk_used_percent": 70, "inode_used_percent": 70, "cpu_sustained_percent": 60, "config_source": "cache", "config_resolver_errors": 0})
    assert checks["queue_capacity"] == "FAIL" and checks["failed_ratio"] == "PENDING" and checks["config_resolver"] == "PASS"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("benchmark timestamps require UTC")
    return parsed


def _number(row: dict[str, str], name: str) -> float:
    value = float(row[name])
    if not math.isfinite(value) or value < 0:
        raise ValueError("benchmark numeric value is invalid")
    return value


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize(rows: list[dict[str, str]], *, failure_ratio_threshold: float | None = None, latency_sla_seconds: float | None = None, global_chrome_slots: int | None = None) -> dict:
    if not rows:
        raise ValueError("benchmark input is empty")
    if {row.get("mode") for row in rows} - {"async", "sync"} or {row.get("outcome") for row in rows} - {"success", "failure"} or {row.get("component") for row in rows} - {"api", "worker"}:
        raise ValueError("benchmark classification is invalid")
    starts = [_timestamp(row["submitted_at"]) for row in rows]
    ends = [_timestamp(row["completed_at"]) for row in rows]
    if any(end < start for start, end in zip(starts, ends)):
        raise ValueError("benchmark completion precedes submission")
    rounds = sorted({row["round"] for row in rows})
    if len(rounds) < 3:
        raise ValueError("benchmark requires at least three rounds")
    round_durations = []
    for round_id in rounds:
        indexes = [index for index, row in enumerate(rows) if row["round"] == round_id]
        duration = (max(ends[index] for index in indexes) - min(starts[index] for index in indexes)).total_seconds()
        round_durations.append(duration)
        if len(indexes) < 30 and duration < 900:
            raise ValueError("benchmark round is too small")
        if {rows[index]["mode"] for index in indexes} != {"async", "sync"} or {rows[index]["component"] for index in indexes} != {"api", "worker"}:
            raise ValueError("benchmark round is not a combined API/worker workload")
    latencies = [(end - start).total_seconds() for start, end in zip(starts, ends)]
    queue = [_number(row, "queue_seconds") for row in rows]
    solve = [_number(row, "solve_seconds") for row in rows]
    cpu = [_number(row, "cpu_percent") for row in rows]
    memory = [_number(row, "memory_bytes") for row in rows]
    memory_max = [_number(row, "memory_max_bytes") for row in rows]
    swap = [_number(row, "swap_bytes") for row in rows]
    chrome_tasks = [_number(row, "chrome_tasks") for row in rows]
    retries = sum(int(_number(row, "retries")) for row in rows)
    active_minutes = max(sum(round_durations) / 60, 1 / 60)
    failures = sum(row["outcome"] == "failure" for row in rows)
    successes = [row for row in rows if row["outcome"] == "success"]
    attempt_cost = len(rows) + retries
    failure_ratio = (failures + retries) / attempt_cost
    memory_headroom = min((limit - used) / limit for used, limit in zip(memory, memory_max) if limit > 0)
    threshold_status = "PENDING" if failure_ratio_threshold is None or latency_sla_seconds is None or global_chrome_slots is None else "PASS" if failure_ratio < failure_ratio_threshold and _percentile(latencies, 0.95) <= latency_sla_seconds else "FAIL"
    resource_status = "FAIL" if global_chrome_slots is not None and max(chrome_tasks) > global_chrome_slots else "PASS" if max(cpu) < 70 and max(swap) == 0 and memory_headroom >= 0.3 else "FAIL"
    result = {
        "status": "PASS" if threshold_status == resource_status == "PASS" else "FAIL" if "FAIL" in {threshold_status, resource_status} else "PENDING",
        "requests_sent": 0,
        "rows": len(rows),
        "rounds": len(rounds),
        "workload_attempts": attempt_cost,
        "async_throughput_per_minute": round(sum(row["mode"] == "async" for row in successes) / active_minutes, 3),
        "sync_throughput_per_minute": round(sum(row["mode"] == "sync" for row in successes) / active_minutes, 3),
        "aggregate_throughput_per_minute": round(len(successes) / active_minutes, 3),
        "latency_median_seconds": statistics.median(latencies),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "queue_latency_median_seconds": statistics.median(queue),
        "queue_latency_p95_seconds": _percentile(queue, 0.95),
        "solve_median_seconds": statistics.median(solve),
        "solve_p95_seconds": _percentile(solve, 0.95),
        "success": len(rows) - failures,
        "failure": failures,
        "failure_ratio": failure_ratio,
        "retry": retries,
        "cpu_sustained_peak_percent": max(cpu),
        "memory_peak_bytes": int(max(memory)),
        "memory_headroom_percent": round(memory_headroom * 100, 3),
        "swap_peak_bytes": int(max(swap)),
        "chrome_tasks_peak": int(max(chrome_tasks)),
        "queue_drain_seconds": max((max(ends[index] for index, row in enumerate(rows) if row["round"] == round_id) - min(starts[index] for index, row in enumerate(rows) if row["round"] == round_id)).total_seconds() for round_id in rounds),
        "resource_criteria": resource_status,
        "sla_threshold_criteria": threshold_status,
        "production_capacity_concurrency": "PENDING OPERATIONAL VERIFICATION" if threshold_status == "PENDING" else result_status(threshold_status, resource_status),
    }
    return result


def result_status(*values: str) -> str:
    return "PASS" if all(value == "PASS" for value in values) else "FAIL"


def _benchmark_self_check() -> None:
    rows = []
    for round_id in range(1, 4):
        for index in range(30):
            rows.append({"round": str(round_id), "mode": "async" if index % 2 else "sync", "component": "api" if index % 3 else "worker", "outcome": "success", "submitted_at": f"2026-01-0{round_id}T00:{index:02d}:00Z", "completed_at": f"2026-01-0{round_id}T00:{index:02d}:30Z", "queue_seconds": "5", "solve_seconds": "20", "retries": "0", "cpu_percent": "50", "memory_bytes": "60", "memory_max_bytes": "100", "swap_bytes": "0", "chrome_tasks": "1"})
    result = summarize(rows)
    assert result["rows"] == 90 and result["rounds"] == 3 and result["status"] == "PENDING"


def benchmark(input_path: str | None, approved_upstream_results: bool, failure_ratio_threshold: float | None, latency_sla_seconds: float | None, global_chrome_slots: int | None) -> int:
    if input_path is None:
        _json("benchmark_complete", status="DRY_RUN_LOCAL", requests_sent=0, real_nric_used=False, production_capacity_concurrency="PENDING OPERATIONAL VERIFICATION")
        return 0
    try:
        with Path(input_path).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or ())
            if {name.lower() for name in columns} & SENSITIVE_COLUMNS:
                raise ValueError("sensitive benchmark columns are forbidden")
            required = {"round", "mode", "component", "outcome", "submitted_at", "completed_at", "queue_seconds", "solve_seconds", "retries", "cpu_percent", "memory_bytes", "memory_max_bytes", "swap_bytes", "chrome_tasks"}
            if required - columns:
                raise ValueError("benchmark columns are incomplete")
            rows = list(reader)
        if any(row.get("source") == "approved_upstream" for row in rows) and not approved_upstream_results:
            raise ValueError("approved upstream result acknowledgement is required")
        result = summarize(rows, failure_ratio_threshold=failure_ratio_threshold, latency_sla_seconds=latency_sla_seconds, global_chrome_slots=global_chrome_slots)
    except BaseException:
        _json("benchmark_complete", status="FAIL", requests_sent=0, production_capacity_concurrency="PENDING OPERATIONAL VERIFICATION")
        return 1
    _json("benchmark_complete", **result)
    return int(result["status"] == "FAIL")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("quality")
    bench = subparsers.add_parser("benchmark")
    bench.add_argument("--input")
    bench.add_argument("--approved-upstream-results", action="store_true")
    bench.add_argument("--failure-ratio-threshold", type=float)
    bench.add_argument("--latency-sla-seconds", type=float)
    bench.add_argument("--global-chrome-slots", type=int)
    alert = subparsers.add_parser("alerts")
    alert.add_argument("--input", required=True)
    args = parser.parse_args()
    if args.command == "quality":
        return quality()
    if args.command == "alerts":
        return alerts(args.input)
    return benchmark(args.input, args.approved_upstream_results, args.failure_ratio_threshold, args.latency_sla_seconds, args.global_chrome_slots)


if __name__ == "__main__":
    raise SystemExit(main())
