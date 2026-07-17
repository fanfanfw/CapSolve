#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PGSERVICE = re.compile(r"[A-Za-z0-9_.-]+")
ALLOWED_UNTRACKED: set[str] = set()
CONFIG_FILES = {
    "systemd": "systemd.unit",
    "crontab": "crontab.txt",
    "nginx": "nginx.vhost",
    "cloudflare": "cloudflare-ingress.yaml",
}
MAX_METRIC_VALUE = 2**63 - 1
METRIC_NAMES = (
    "submit_count",
    "success_count",
    "failed_retry_count",
    "median_process_seconds",
    "peak_memory_bytes",
    "log_size_bytes",
    "oldest_pending_seconds",
)
BASELINE_KEYS = {
    "format_version",
    "state",
    "captured_at_utc",
    "deployment_commit",
    "rollback_commit",
    "environment_variable_names",
    "configuration_backups",
    "database",
    "baseline_24h",
}
DATABASE_KEYS = {
    "backup_file",
    "backup_sha256",
    "schema_file",
    "schema_sha256",
    "budi95_jobs_row_count",
    "source_database_identity_sha256",
}
RESTORE_KEYS = {
    "format_version",
    "status",
    "verified_at_utc",
    "backup_sha256",
    "source_budi95_jobs_row_count",
    "restored_budi95_jobs_row_count",
}
SCHEDULED_KEYS = {
    "format_version",
    "status",
    "captured_at_utc",
    "backup_file",
    "backup_sha256",
    "budi95_jobs_row_count",
    "source_database_identity_sha256",
}
MAX_BACKUP_RETENTION_HOURS = 24


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "baseline: invalid arguments\n")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _outside_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved
    raise ValueError("evidence directory must be outside the Git working tree")


def _env_names(path: Path) -> list[str]:
    names: set[str] = set()
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, _ = line.partition("=")
        name = name.strip()
        if not separator or not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment assignment at line {number}")
        names.add(name)
    return sorted(names)


def _service(value: str) -> str:
    if not PGSERVICE.fullmatch(value):
        raise argparse.ArgumentTypeError("PostgreSQL service name contains unsupported characters")
    return value


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError("value must be within the supported non-negative range") from None
    if not 0 <= parsed <= MAX_METRIC_VALUE:
        raise argparse.ArgumentTypeError("value must be within the supported non-negative range")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError("value must be a finite number within the supported non-negative range") from None
    if not math.isfinite(parsed) or not 0 <= parsed <= MAX_METRIC_VALUE:
        raise argparse.ArgumentTypeError("value must be a finite number within the supported non-negative range")
    return parsed


def _pg_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "PGPASSWORD",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
    ):
        env.pop(name, None)
    return env


def _run(command: list[str], *, stdout: Any = subprocess.PIPE, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        check=True,
        env=env if env is not None else _pg_env(),
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=stdout == subprocess.PIPE,
    )
    return result.stdout.strip() if stdout == subprocess.PIPE else ""


def _psql(query: str, *, service: str | None = None, env: dict[str, str] | None = None) -> str:
    command = ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    if service:
        command.extend(("--dbname", f"service={service}"))
    command.extend(("--command", query))
    return _run(command, env=env)


def _database_identity(service: str) -> str:
    identity = _psql(
        "SELECT (pg_control_system()).system_identifier::text || '|' || oid::text FROM pg_database WHERE datname = current_database()",
        service=service,
    )
    if not re.fullmatch(r"[0-9]+\|[0-9]+", identity):
        raise RuntimeError("stable PostgreSQL cluster/database identity is unavailable")
    return hashlib.sha256(identity.encode()).hexdigest()


def _target_is_empty(service: str) -> bool:
    output = _psql(
        "SELECT count(*) FROM ("
        "SELECT n.oid FROM pg_namespace n WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'public') AND n.nspname !~ '^pg_toast' "
        "UNION ALL SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public' AND t.typtype IN ('c', 'd', 'e', 'r') "
        "UNION ALL SELECT e.oid FROM pg_extension e WHERE e.extname <> 'plpgsql' "
        "UNION ALL SELECT c.oid FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT c.oid FROM pg_conversion c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT o.oid FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT o.oid FROM pg_opclass o JOIN pg_namespace n ON n.oid = o.opcnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT o.oid FROM pg_opfamily o JOIN pg_namespace n ON n.oid = o.opfnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT s.oid FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT t.oid FROM pg_ts_config t JOIN pg_namespace n ON n.oid = t.cfgnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT t.oid FROM pg_ts_dict t JOIN pg_namespace n ON n.oid = t.dictnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT t.oid FROM pg_ts_parser t JOIN pg_namespace n ON n.oid = t.prsnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT t.oid FROM pg_ts_template t JOIN pg_namespace n ON n.oid = t.tmplnamespace WHERE n.nspname = 'public' "
        "UNION ALL SELECT c.oid FROM pg_cast c WHERE c.oid >= 16384 "
        "UNION ALL SELECT t.oid FROM pg_transform t "
        "UNION ALL SELECT a.oid FROM pg_am a WHERE a.oid >= 16384 "
        "UNION ALL SELECT l.oid FROM pg_language l WHERE l.lanname NOT IN ('internal', 'c', 'sql', 'plpgsql') "
        "UNION ALL SELECT f.oid FROM pg_foreign_data_wrapper f "
        "UNION ALL SELECT s.oid FROM pg_foreign_server s "
        "UNION ALL SELECT u.oid FROM pg_user_mapping u "
        "UNION ALL SELECT e.oid FROM pg_event_trigger e "
        "UNION ALL SELECT p.oid FROM pg_publication p "
        "UNION ALL SELECT s.oid FROM pg_subscription s WHERE s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "UNION ALL SELECT d.oid FROM pg_default_acl d "
        "UNION ALL SELECT l.oid FROM pg_largeobject_metadata l "
        "UNION ALL SELECT r.setdatabase FROM pg_db_role_setting r WHERE r.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "UNION ALL SELECT d.objoid FROM pg_shdescription d WHERE d.classoid = 'pg_database'::regclass AND d.objoid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "UNION ALL SELECT s.objoid FROM pg_shseclabel s WHERE s.classoid = 'pg_database'::regclass AND s.objoid = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ") objects",
        service=service,
    )
    if not output.isdigit():
        raise RuntimeError("unable to verify disposable restore database emptiness")
    return output == "0"


def _row_count(*, service: str | None = None, env: dict[str, str] | None = None) -> int:
    output = _psql("SELECT count(*) FROM public.budi95_jobs", service=service, env=env)
    if not output.isdigit():
        raise RuntimeError("PostgreSQL returned an invalid row count")
    return int(output)


def _restore_archive(archive: Path, service: str) -> None:
    _run(
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            f"service={service}",
            str(archive),
        ]
    )


def _archive_row_count(archive: Path, scratch: Path) -> int:
    _run(
        [
            "pg_restore",
            "--data-only",
            "--table",
            "public.budi95_jobs",
            "--file",
            str(scratch),
            str(archive),
        ]
    )
    count = 0
    in_copy = False
    found = False
    try:
        with scratch.open(encoding="utf-8") as output:
            for line in output:
                if line.startswith("COPY public.budi95_jobs ") and line.rstrip().endswith(" FROM stdin;"):
                    in_copy = True
                    found = True
                elif in_copy and line == "\\.\n":
                    in_copy = False
                elif in_copy:
                    count += 1
    finally:
        scratch.unlink(missing_ok=True)
    if not found or in_copy:
        raise RuntimeError("unable to count budi95_jobs rows in backup archive")
    return count


def _git_commit(value: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{value}^{{commit}}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _unsafe_checkout_paths(status: str) -> list[str]:
    unsafe = []
    for line in status.splitlines():
        path = line[3:]
        if line.startswith("?? ") and path in ALLOWED_UNTRACKED:
            continue
        unsafe.append(path)
    return unsafe


def _deployment_commit() -> str:
    commit = _git_commit("HEAD")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if _unsafe_checkout_paths(status.stdout):
        raise ValueError("deployed checkout contains uncommitted changes")
    return commit


def _absolute_parts(path: Path) -> list[str]:
    return list(Path(os.path.normpath(path)).parts[1:])


def _symlink_chain_resolves(original: str, resolved: str, chain: list[dict[str, Any]]) -> bool:
    current = Path("/")
    pending = _absolute_parts(Path(original))
    index = 0
    while pending:
        candidate = current / pending.pop(0)
        if index < len(chain) and candidate == Path(chain[index]["path"]):
            target = Path(chain[index]["target"])
            index += 1
            destination = target if target.is_absolute() else current / target
            pending = _absolute_parts(destination) + pending
            current = Path("/")
        else:
            current = candidate
    return index == len(chain) and current == Path(resolved)


def _config_backup(source_value: str, target: Path) -> dict[str, Any]:
    source = Path(source_value).expanduser().absolute()
    current = Path("/")
    pending = _absolute_parts(source)
    symlink_chain = []
    seen = set()
    while pending:
        candidate = current / pending.pop(0)
        if not candidate.is_symlink():
            current = candidate
            continue
        if candidate in seen:
            raise ValueError("configuration symlink loop detected")
        seen.add(candidate)
        link_stat = candidate.lstat()
        link_target = os.readlink(candidate)
        symlink_chain.append(
            {
                "path": str(candidate),
                "target": link_target,
                "uid": link_stat.st_uid,
                "gid": link_stat.st_gid,
                "mode": stat.S_IMODE(link_stat.st_mode),
            }
        )
        destination = Path(link_target) if Path(link_target).is_absolute() else current / link_target
        pending = _absolute_parts(destination) + pending
        current = Path("/")
    resolved = current.resolve(strict=True)
    resolved_stat = resolved.stat()
    if not stat.S_ISREG(resolved_stat.st_mode):
        raise ValueError("configuration source must resolve to a regular file")
    shutil.copyfile(resolved, target)
    target.chmod(0o600)
    return {
        "file": f"configuration/{target.name}",
        "sha256": _sha256(target),
        "original_path": str(source),
        "resolved_path": str(resolved),
        "symlink_chain": symlink_chain,
        "file_uid": resolved_stat.st_uid,
        "file_gid": resolved_stat.st_gid,
        "file_mode": stat.S_IMODE(resolved_stat.st_mode),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def scheduled_backup(args: argparse.Namespace) -> int:
    destination = _outside_repo(Path(args.backup_dir))
    evidence = _outside_repo(Path(args.evidence_file))
    if evidence.parent != destination:
        raise ValueError("backup evidence must be inside the protected backup directory")
    if not args.source_pgservice or not PGSERVICE.fullmatch(args.source_pgservice):
        raise ValueError("invalid PostgreSQL service name")
    if not 1 <= args.retention_hours <= MAX_BACKUP_RETENTION_HOURS:
        raise ValueError("backup retention is invalid")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = destination.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("backup directory ownership or mode is unsafe")
    source_identity = _database_identity(args.source_pgservice)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"capsolve-{stamp}.dump"
    temporary = destination / f".{final.name}.tmp"
    try:
        _run([
            "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
            "--file", str(temporary), "--dbname", f"service={args.source_pgservice}",
        ])
        temporary.chmod(0o600)
        row_count = _archive_row_count(temporary, destination / f".{final.name}.rows.tmp")
        if _database_identity(args.source_pgservice) != source_identity:
            raise RuntimeError("source database identity changed during backup")
        checksum = _sha256(temporary)
        temporary.replace(final)
        _write_json(evidence, {
            "format_version": 1,
            "status": "CAPTURED_RESTORE_PENDING",
            "captured_at_utc": _utc_now(),
            "backup_file": final.name,
            "backup_sha256": checksum,
            "budi95_jobs_row_count": row_count,
            "source_database_identity_sha256": source_identity,
        })
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - args.retention_hours * 3600
        for path in destination.glob("capsolve-*.dump"):
            details = path.lstat()
            if path != final and stat.S_ISREG(details.st_mode) and details.st_mtime < cutoff:
                path.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print("Scheduled backup complete; disposable restore verification remains pending")
    return 0


def _metrics(args: argparse.Namespace) -> dict[str, Any]:
    values = {name: getattr(args, name) for name in METRIC_NAMES}
    supplied = [value is not None for value in values.values()]
    status = "RECORDED_COMPLETE" if all(supplied) else "RECORDED_PARTIAL" if any(supplied) else "PENDING_NOT_AVAILABLE"
    return {"status": status, "period_hours": 24, "metrics": values}


def capture(args: argparse.Namespace) -> int:
    destination = _outside_repo(Path(args.evidence_dir))
    if destination.exists():
        raise ValueError("evidence directory already exists")
    deployment_commit = _deployment_commit()
    rollback_commit = _git_commit(args.rollback_commit)
    environment_names = _env_names(Path(args.environment_file).expanduser().resolve(strict=True))
    metrics = _metrics(args)
    source_identity = _database_identity(args.source_pgservice)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    temporary.chmod(0o700)
    try:
        config_dir = temporary / "configuration"
        config_dir.mkdir(mode=0o700)
        hashes: dict[str, dict[str, Any]] = {}
        for label, filename in CONFIG_FILES.items():
            hashes[label] = _config_backup(getattr(args, label), config_dir / filename)

        schema = temporary / "budi95_jobs.schema.sql"
        backup = temporary / "database.dump"
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(backup),
                "--dbname",
                f"service={args.source_pgservice}",
            ]
        )
        _run(
            [
                "pg_restore",
                "--schema-only",
                "--table",
                "public.budi95_jobs",
                "--file",
                str(schema),
                str(backup),
            ]
        )
        schema.chmod(0o600)
        backup.chmod(0o600)
        row_count = _archive_row_count(backup, temporary / "database.rows.sql")
        if _database_identity(args.source_pgservice) != source_identity:
            raise RuntimeError("source database identity changed during capture")

        manifest = {
            "format_version": 1,
            "state": "CAPTURED_RESTORE_PENDING",
            "captured_at_utc": _utc_now(),
            "deployment_commit": deployment_commit,
            "rollback_commit": rollback_commit,
            "environment_variable_names": environment_names,
            "configuration_backups": hashes,
            "database": {
                "backup_file": backup.name,
                "backup_sha256": _sha256(backup),
                "schema_file": schema.name,
                "schema_sha256": _sha256(schema),
                "budi95_jobs_row_count": row_count,
                "source_database_identity_sha256": source_identity,
            },
            "baseline_24h": metrics,
        }
        _write_json(temporary / "baseline.json", manifest)
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print("Phase 0 capture complete; restore remains pending")
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} has unexpected or missing fields")


def _validate_timestamp(value: Any, label: str) -> None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {label} timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError(f"invalid {label} timestamp")


def _validate_baseline(evidence: Path) -> dict[str, Any]:
    baseline = _load_json(evidence / "baseline.json")
    _exact_keys(baseline, BASELINE_KEYS, "baseline")
    if baseline["format_version"] != 1:
        raise ValueError("unsupported baseline format")
    if baseline["state"] != "CAPTURED_RESTORE_PENDING":
        raise ValueError("invalid baseline state")
    _validate_timestamp(baseline["captured_at_utc"], "capture")
    if not isinstance(baseline["deployment_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40,64}", baseline["deployment_commit"]
    ):
        raise ValueError("invalid deployment commit")
    if not isinstance(baseline["rollback_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40,64}", baseline["rollback_commit"]
    ):
        raise ValueError("invalid rollback commit")
    names = baseline["environment_variable_names"]
    if (
        not isinstance(names, list)
        or not all(isinstance(name, str) for name in names)
        or names != sorted(set(names))
        or not all(ENV_NAME.fullmatch(name) for name in names)
    ):
        raise ValueError("environment variable inventory must contain names only")

    backups = baseline["configuration_backups"]
    if not isinstance(backups, dict) or set(backups) != set(CONFIG_FILES):
        raise ValueError("configuration backup inventory is incomplete")
    config_keys = {
        "file",
        "sha256",
        "original_path",
        "resolved_path",
        "symlink_chain",
        "file_uid",
        "file_gid",
        "file_mode",
    }
    for label, expected_filename in CONFIG_FILES.items():
        record = backups[label]
        if (
            not isinstance(record, dict)
            or set(record) != config_keys
            or record["file"] != f"configuration/{expected_filename}"
            or not isinstance(record["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or not isinstance(record["original_path"], str)
            or not Path(record["original_path"]).is_absolute()
            or not isinstance(record["resolved_path"], str)
            or not Path(record["resolved_path"]).is_absolute()
            or not isinstance(record["symlink_chain"], list)
            or not all(
                isinstance(link, dict)
                and set(link) == {"path", "target", "uid", "gid", "mode"}
                and isinstance(link["path"], str)
                and Path(link["path"]).is_absolute()
                and isinstance(link["target"], str)
                and isinstance(link["uid"], int)
                and link["uid"] >= 0
                and isinstance(link["gid"], int)
                and link["gid"] >= 0
                and isinstance(link["mode"], int)
                and 0 <= link["mode"] <= 0o7777
                for link in record["symlink_chain"]
            )
            or not _symlink_chain_resolves(record["original_path"], record["resolved_path"], record["symlink_chain"])
            or not all(isinstance(record[key], int) and record[key] >= 0 for key in ("file_uid", "file_gid"))
            or not isinstance(record["file_mode"], int)
            or not 0 <= record["file_mode"] <= 0o7777
        ):
            raise ValueError("invalid configuration backup record")
        if _sha256(evidence / record["file"]) != record["sha256"]:
            raise ValueError("configuration backup checksum mismatch")

    database = baseline["database"]
    if not isinstance(database, dict):
        raise ValueError("database evidence must be an object")
    _exact_keys(database, DATABASE_KEYS, "database evidence")
    for file_key, hash_key in (("backup_file", "backup_sha256"), ("schema_file", "schema_sha256")):
        if not isinstance(database[file_key], str) or not isinstance(database[hash_key], str):
            raise ValueError("invalid database evidence record")
        path = evidence / database[file_key]
        if path.parent != evidence or _sha256(path) != database[hash_key]:
            raise ValueError("database evidence checksum mismatch")
    if not isinstance(database["budi95_jobs_row_count"], int) or database["budi95_jobs_row_count"] < 0:
        raise ValueError("invalid source row count")
    if not isinstance(database["source_database_identity_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", database["source_database_identity_sha256"]
    ):
        raise ValueError("invalid source database identity")

    metrics = baseline["baseline_24h"]
    if (
        not isinstance(metrics, dict)
        or set(metrics) != {"status", "period_hours", "metrics"}
        or metrics["period_hours"] != 24
        or not isinstance(metrics["metrics"], dict)
    ):
        raise ValueError("invalid 24-hour baseline")
    if set(metrics["metrics"]) != set(METRIC_NAMES):
        raise ValueError("invalid 24-hour metric fields")
    values = list(metrics["metrics"].values())
    if not all(
        value is None
        or isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
        and 0 <= value <= MAX_METRIC_VALUE
        for value in values
    ):
        raise ValueError("recorded metrics must be finite and within the supported non-negative range")
    expected_status = (
        "RECORDED_COMPLETE"
        if all(value is not None for value in values)
        else "RECORDED_PARTIAL"
        if any(value is not None for value in values)
        else "PENDING_NOT_AVAILABLE"
    )
    if metrics["status"] != expected_status:
        raise ValueError("invalid 24-hour metric status")
    return baseline


def _validate_scheduled(evidence: Path) -> dict[str, Any]:
    scheduled = _load_json(evidence / "scheduled-backup.json")
    _exact_keys(scheduled, SCHEDULED_KEYS, "scheduled backup")
    if scheduled["format_version"] != 1 or scheduled["status"] != "CAPTURED_RESTORE_PENDING":
        raise ValueError("scheduled backup is invalid")
    _validate_timestamp(scheduled["captured_at_utc"], "scheduled backup")
    if not isinstance(scheduled["backup_file"], str) or Path(scheduled["backup_file"]).name != scheduled["backup_file"]:
        raise ValueError("scheduled backup file is invalid")
    archive = evidence / scheduled["backup_file"]
    if not isinstance(scheduled["backup_sha256"], str) or _sha256(archive) != scheduled["backup_sha256"]:
        raise ValueError("scheduled backup checksum mismatch")
    if not isinstance(scheduled["budi95_jobs_row_count"], int) or scheduled["budi95_jobs_row_count"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", scheduled["source_database_identity_sha256"]):
        raise ValueError("scheduled backup evidence is invalid")
    return scheduled


def _validate_restore(evidence: Path, baseline: dict[str, Any]) -> None:
    restore = _load_json(evidence / "restore-test.json")
    _exact_keys(restore, RESTORE_KEYS, "restore evidence")
    if restore["format_version"] != 1 or restore["status"] != "VERIFIED_ROW_COUNT_MATCH":
        raise ValueError("restore is not verified")
    _validate_timestamp(restore["verified_at_utc"], "restore verification")
    expected = baseline["database"]["budi95_jobs_row_count"]
    if restore["backup_sha256"] != baseline["database"]["backup_sha256"]:
        raise ValueError("restore used a different backup")
    if restore["source_budi95_jobs_row_count"] != expected or restore["restored_budi95_jobs_row_count"] != expected:
        raise ValueError("restored row count does not match source")


def restore_test(args: argparse.Namespace) -> int:
    if not args.confirm_disposable_database:
        raise ValueError("--confirm-disposable-database is required")
    evidence = _outside_repo(Path(args.evidence_dir))
    if (evidence / "baseline.json").is_file():
        database = _validate_baseline(evidence)["database"]
        backup_name = database["backup_file"]
        backup_sha256 = database["backup_sha256"]
        source_count = database["budi95_jobs_row_count"]
        source_identity = database["source_database_identity_sha256"]
    else:
        scheduled = _validate_scheduled(evidence)
        backup_name = scheduled["backup_file"]
        backup_sha256 = scheduled["backup_sha256"]
        source_count = scheduled["budi95_jobs_row_count"]
        source_identity = scheduled["source_database_identity_sha256"]
    target_identity = _database_identity(args.restore_pgservice)
    if target_identity == source_identity:
        raise ValueError("restore database must be separate from the source database")
    if not _target_is_empty(args.restore_pgservice):
        raise ValueError("restore database must contain no non-system objects")
    backup = evidence / backup_name
    _restore_archive(backup, args.restore_pgservice)
    restored_count = _row_count(service=args.restore_pgservice)
    if restored_count != source_count:
        raise RuntimeError("restore row count does not match captured source row count")
    _write_json(
        evidence / "restore-test.json",
        {
            "format_version": 1,
            "status": "VERIFIED_ROW_COUNT_MATCH",
            "verified_at_utc": _utc_now(),
            "backup_sha256": backup_sha256,
            "source_budi95_jobs_row_count": source_count,
            "restored_budi95_jobs_row_count": restored_count,
        },
    )
    print("Phase 0 restore verified; row counts match")
    return 0


def validate(args: argparse.Namespace) -> int:
    evidence = _outside_repo(Path(args.evidence_dir))
    baseline = _validate_baseline(evidence)
    if args.require_restore:
        _validate_restore(evidence, baseline)
    print("Phase 0 evidence valid")
    return 0


def parser() -> argparse.ArgumentParser:
    root = SafeArgumentParser(description="Capture and verify secret-safe Phase 0 operational evidence outside Git")
    commands = root.add_subparsers(dest="command", required=True)

    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("evidence_dir")
    capture_parser.add_argument("--environment-file", required=True)
    capture_parser.add_argument("--systemd", required=True)
    capture_parser.add_argument("--crontab", required=True)
    capture_parser.add_argument("--nginx", required=True)
    capture_parser.add_argument("--cloudflare", required=True)
    capture_parser.add_argument("--source-pgservice", required=True, type=_service)
    capture_parser.add_argument("--rollback-commit", required=True)
    for name in METRIC_NAMES:
        value_type = _nonnegative_float if name in {"median_process_seconds", "oldest_pending_seconds"} else _nonnegative_int
        capture_parser.add_argument(f"--{name.replace('_', '-')}", type=value_type)
    capture_parser.set_defaults(handler=capture)

    restore_parser = commands.add_parser("restore-test")
    restore_parser.add_argument("evidence_dir")
    restore_parser.add_argument("--restore-pgservice", required=True, type=_service)
    restore_parser.add_argument("--confirm-disposable-database", action="store_true")
    restore_parser.set_defaults(handler=restore_test)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("evidence_dir")
    validate_parser.add_argument("--require-restore", action="store_true")
    validate_parser.set_defaults(handler=validate)

    scheduled_parser = commands.add_parser("scheduled-backup")
    scheduled_parser.add_argument("--backup-dir", required=True)
    scheduled_parser.add_argument("--evidence-file", required=True)
    scheduled_parser.add_argument("--source-pgservice", required=True, type=_service)
    scheduled_parser.add_argument("--retention-hours", required=True, type=_nonnegative_int)
    scheduled_parser.set_defaults(handler=scheduled_backup)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return args.handler(args)
    except (OSError, OverflowError, ValueError, RuntimeError, subprocess.CalledProcessError):
        print("baseline: operation failed; review prerequisites and protected operator logs", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
