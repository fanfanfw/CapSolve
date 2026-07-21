from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import psycopg2
from psycopg2 import sql

import database
import production_preflight
import purge_jobs
from deployment import ops
import solver
from settings import MAX_JOB_RETENTION_HOURS, MAX_PURGE_BATCH_LIMIT, load_settings
from tests.test_queue_postgres import safe_test_connection_kwargs


CANARY = "NRIC-sensitive postgresql://user:password@public.example/capsolve"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def evidence(now: datetime, **backup_overrides):
    backup = {
        "backup_retention_hours": 24,
        "rpo_hours": 24,
        "rto_minutes": 60,
        "last_success_at_utc": (now - timedelta(minutes=10)).isoformat(),
        "artifact_id": "backup-20260716T115000Z",
        "artifact_basename": "capsolve.dump",
        "artifact_sha256": "a" * 64,
        "checksum_verified_at_utc": (now - timedelta(minutes=9)).isoformat(),
        "restore_started_at_utc": (now - timedelta(minutes=8)).isoformat(),
        "restore_verified_at_utc": (now - timedelta(minutes=7)).isoformat(),
        "restore_duration_seconds": 60,
        "source_row_count": 7,
        "restored_row_count": 7,
    }
    backup.update(backup_overrides)
    return {
        "schema_version": 1,
        "generated_at_utc": now.isoformat(),
        "purge_timer": {
            "service_unit": "capsolve-purge.service",
            "timer_unit": "capsolve-purge.timer",
            "interval_seconds": 1800,
        },
        "backup": backup,
    }


def runtime_timer(now: datetime, **overrides):
    value = {
        "load_state": "loaded",
        "active_state": "active",
        "unit_file_state": "enabled",
        "unit": "capsolve-purge.service",
        "schedule": "*:0/30:00",
        "persistent": "yes",
        "random_delay": "0",
        "last_trigger": now - timedelta(minutes=30),
        "next_elapse": now + timedelta(minutes=30),
    }
    value.update(overrides)
    return value


class Phase7UnitTest(unittest.TestCase):
    def test_production_components_require_explicit_bounded_retention(self):
        for component in ("api", "worker", "purge"):
            for value in (None, "", "invalid", "0", str(MAX_JOB_RETENTION_HOURS + 1), "9" * 1000):
                values = {"ENVIRONMENT": "production"}
                if value is not None:
                    values["JOB_RETENTION_HOURS"] = value
                with self.subTest(component=component, value=value), self.assertRaises(ValueError):
                    load_settings(component, values)
        self.assertEqual(load_settings("worker", {}).job_retention_hours, 24)
        self.assertEqual(load_settings("purge", {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"}).job_retention_hours, 24)

    def test_purge_profile_and_batch_bounds(self):
        settings = load_settings("purge", {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24", "SOLVER_TIMEOUT": "invalid", "API_KEY": "unused"})
        self.assertEqual(settings.purge_batch_limit, 1000)
        for value in ("", "0", "invalid", str(MAX_PURGE_BATCH_LIMIT + 1), "9" * 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_settings("purge", {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24", "PURGE_BATCH_LIMIT": value})

    def test_preflight_policy_timestamp_counts_and_artifact_adversaries(self):
        now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
        values = {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"}
        production_preflight.validate(values, evidence(now), now, runtime_timer=runtime_timer(now))
        failures = (
            evidence(now, backup_retention_hours=25),
            evidence(now, rpo_hours=25),
            evidence(now, rto_minutes=61),
            evidence(now, artifact_id="../secret"),
            evidence(now, artifact_basename="path/secret.dump"),
            evidence(now, artifact_sha256="invalid"),
            evidence(now, source_row_count=True, restored_row_count=True),
            evidence(now, source_row_count=-1, restored_row_count=-1),
            evidence(now, restored_row_count=6),
            evidence(now, checksum_verified_at_utc=(now - timedelta(minutes=11)).isoformat()),
            evidence(now, restore_started_at_utc=(now - timedelta(minutes=6)).isoformat()),
            evidence(now, restore_duration_seconds=59),
            evidence(now, restore_duration_seconds=3601, restore_started_at_utc=(now - timedelta(minutes=67)).isoformat()),
        )
        for invalid in failures:
            with self.subTest(backup=invalid["backup"]), self.assertRaises(ValueError):
                production_preflight.validate(values, invalid, now, runtime_timer=runtime_timer(now))
        future = evidence(now)
        future["generated_at_utc"] = (now + timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            production_preflight.validate(values, future, now, runtime_timer=runtime_timer(now))

    def test_static_validates_artifacts_but_never_claims_operational(self):
        now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
        values = {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"}
        production_preflight.validate(values, evidence(now), now, unit_directory=Path("deployment"))
        output = io.StringIO()
        current = datetime.now(timezone.utc)
        with mock.patch("sys.argv", ["capsolve-production-preflight", "--static", "--evidence", "/evidence"]), mock.patch.object(
            production_preflight, "_secure_json", return_value=evidence(current)
        ), mock.patch.object(production_preflight, "_validate_component_environments", return_value={"purge": values}) as environments, mock.patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(production_preflight.main(), 0)
        environments.assert_called_once_with(Path("/etc/capsolve"), api_uid=os.geteuid())
        record = json.loads(output.getvalue())
        self.assertEqual(record["mode"], "static")
        self.assertFalse(record["operational_ready"])

    def test_systemctl_real_property_output_and_runtime_failures(self):
        stdout = "\n".join((
            "LoadState=loaded",
            "ActiveState=active",
            "UnitFileState=enabled",
            "Unit=capsolve-purge.service",
            "TimersCalendar=*:0/30:00",
            "Persistent=yes",
            "RandomizedDelayUSec=0",
            "NextElapseUSecRealtime=Thu 2026-07-16 13:00:00 UTC",
            "LastTriggerUSec=Thu 2026-07-16 12:00:00 UTC",
        ))
        with mock.patch.object(production_preflight.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)) as run:
            timer = production_preflight._systemctl_timer()
        self.assertEqual(timer["next_elapse"] - timer["last_trigger"], timedelta(hours=1))
        self.assertEqual(run.call_args.args[0][:3], ["systemctl", "show", "capsolve-purge.timer"])
        now = datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc)
        values = {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"}
        for override in (
            {"load_state": "not-found"},
            {"active_state": "inactive"},
            {"unit_file_state": "disabled"},
            {"next_elapse": now + timedelta(hours=24), "last_trigger": now},
            {"next_elapse": now - timedelta(seconds=1)},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                production_preflight.validate(values, evidence(now), now, runtime_timer=runtime_timer(now, **override))

    def test_production_dotenv_and_component_preflight_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / ".env"
            shared.write_text("API_KEY=shared-secret\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
                solver.load_dotenv(str(shared))
                self.assertNotIn("API_KEY", os.environ)

            directory = Path(temporary) / "environments"
            directory.mkdir()
            registry = directory / "api-clients.json"
            registry.write_text(json.dumps({"clients": [{"id": "production", "credentials": [{"id": "prod-app", "key": secrets.token_urlsafe(32)}]}]}), encoding="utf-8")
            registry.chmod(0o600)
            examples = {"api": "api.env.example", "worker": "worker.env.example", "purge": "purge.env.example", "backup": "backup.env.example"}
            replacements = {
                "<REQUIRED_GENERATED_URLSAFE_KEY>": secrets.token_urlsafe(32),
                "<REQUIRED_CLIENT_IP_OR_CIDR>": "192.0.2.1/32",
                "<REQUIRED_CAPSOLVE_HOSTNAME>": "api.example.invalid",
                "<REQUIRED_CAPSOLVE_NGINX_GROUP_ID>": str(os.getgid()),
                "<REQUIRED_APPROVED_HOURS>": "24",
                "<REQUIRED_API_DB_PASSWORD>": "api-db-secret",
                "<REQUIRED_WORKER_DB_PASSWORD>": "worker-db-secret",
                "<REQUIRED_PURGE_DB_PASSWORD>": "purge-db-secret",
                "<REQUIRED_FALLBACK_LOCAL_POST_URL>": "https://api.example.invalid/result",
                "<REQUIRED_FALLBACK_TURNSTILE_SITEURL>": "https://www.example.invalid/eligibility",
                "<REQUIRED_FALLBACK_TURNSTILE_SITEKEY>": "site-key",
            }
            for component, example in examples.items():
                content = (Path("deployment") / example).read_text(encoding="utf-8")
                content = content.replace("/etc/capsolve/api-clients.json", str(registry))
                for old, new in replacements.items():
                    content = content.replace(old, new)
                path = directory / f"{component}.env"
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
            production_preflight._validate_component_environments(directory, expected_uid=os.geteuid())
            worker = directory / "worker.env"
            original = worker.read_text(encoding="utf-8")
            for content in (
                original + "API_KEY=forbidden\n",
                original + "API_HOST=127.0.0.1\n",
                original.replace("JOB_RETENTION_HOURS=24", "JOB_RETENTION_HOURS=48"),
                original.replace("GLOBAL_CHROME_SLOTS=1", "GLOBAL_CHROME_SLOTS=2"),
            ):
                worker.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    production_preflight._validate_component_environments(directory, expected_uid=os.geteuid())
            worker.write_text(original, encoding="utf-8")
            for content in (
                original.replace("TURNSTILE_SITEKEY=site-key", "TURNSTILE_SITEKEY=other-key"),
                "\n".join(line for line in original.replace("BUDI95_FORCE_ENV_CONFIG=false", "BUDI95_FORCE_ENV_CONFIG=true").splitlines() if not line.startswith(("LOCAL_POST_URL=", "TURNSTILE_SITEURL=", "TURNSTILE_SITEKEY="))) + "\n",
            ):
                worker.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    production_preflight._validate_component_environments(directory, expected_uid=os.geteuid())
            worker.write_text(original, encoding="utf-8")
            worker.chmod(0o640)
            with self.assertRaises(ValueError):
                production_preflight._validate_component_environments(directory, expected_uid=os.geteuid())

        good = {
            unit: {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
            for unit in production_preflight.UNIT_NAMES
        }
        good["capsolve-ingress-permissions.service"]["Result"] = "success"
        good["capsolve-worker.service"]["ActiveState"] = "inactive"
        good["capsolve-worker.service"]["UnitFileState"] = "static"
        good["capsolve-purge.service"]["ActiveState"] = "inactive"
        good["capsolve-purge.service"]["UnitFileState"] = "static"
        good["capsolve-backup.service"]["ActiveState"] = "inactive"
        good["capsolve-backup.service"]["UnitFileState"] = "static"
        production_preflight._validate_component_states(good)
        bad = {unit: dict(state) for unit, state in good.items()}
        bad["capsolve-worker.timer"]["ActiveState"] = "inactive"
        with self.assertRaises(ValueError):
            production_preflight._validate_component_states(bad)

    def test_evidence_requires_expected_owner_safe_mode_size_and_no_symlink(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            path.write_text(json.dumps(evidence(now)), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(production_preflight._secure_json(path, expected_uid=os.geteuid())["schema_version"], 1)
            with self.assertRaises(ValueError):
                production_preflight._secure_json(path, expected_uid=os.geteuid() + 1)
            path.chmod(0o640)
            with self.assertRaises(ValueError):
                production_preflight._secure_json(path, expected_uid=os.geteuid())
            path.chmod(0o600)
            link = path.with_name("link.json")
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                production_preflight._secure_json(link, expected_uid=os.geteuid())
            path.write_bytes(b" " * (production_preflight.MAX_EVIDENCE_BYTES + 1))
            with self.assertRaises(ValueError):
                production_preflight._secure_json(path, expected_uid=os.geteuid())

    def test_malformed_cli_output_is_generic_and_bounded(self):
        cases = (
            (purge_jobs.main, ["capsolve-purge-jobs", "--limit", CANARY]),
            (purge_jobs.main, ["capsolve-purge-jobs", "--limit", str(MAX_PURGE_BATCH_LIMIT + 1)]),
            (production_preflight.main, ["capsolve-production-preflight", "--evidence", CANARY]),
        )
        for function, arguments in cases:
            output = io.StringIO()
            with self.subTest(arguments=arguments), mock.patch("sys.argv", arguments), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(function(), 1)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertNotIn(CANARY, output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_quality_gate_includes_postgres_and_observability_tests(self):
        artifacts = ops.validate_artifacts()
        self.assertEqual((artifacts["systemd_units"], artifacts["environment_examples"]), (10, 4))
        modules = ops._postgres_test_modules()
        self.assertIn("tests.test_queue_postgres", modules)
        self.assertIn("tests.test_worker_postgres", modules)
        self.assertIn("tests.test_chrome_slots_postgres", modules)
        self.assertIn("tests.test_health_observability", modules)
        self.assertIn("tests.test_retention_recovery", modules)

    def test_identity_timer_alert_and_benchmark_artifacts(self):
        api = ops._sectioned(Path("deployment/capsolve-api.service"))["Service"]
        worker = ops._sectioned(Path("deployment/capsolve-worker.service"))["Service"]
        self.assertEqual((api["User"], worker["User"]), ("capsolve-api", "capsolve-worker"))
        self.assertNotIn("capsolve-nginx", worker.get("SupplementaryGroups", ""))
        self.assertNotEqual(ops._env(Path("deployment/api.env.example"))["TS_PROFILE_DIR"], ops._env(Path("deployment/worker.env.example"))["TS_PROFILE_DIR"])
        checks = ops.evaluate_alerts({"queue_depth": 8, "queue_capacity": 10, "stale_processing_count": 1, "readiness_ok": False, "worker_age_seconds": 181, "memory_current_bytes": 80, "memory_max_bytes": 100, "disk_used_percent": 80, "inode_used_percent": 80, "cpu_sustained_percent": 70, "config_source": "env", "config_resolver_errors": 1})
        self.assertTrue(all(checks[name] == "FAIL" for name in ("queue_capacity", "stale_processing", "readiness", "worker_freshness", "memory", "disk", "inode", "cpu", "config_resolver")))
        self.assertEqual((checks["failed_ratio"], checks["oldest_pending"]), ("PENDING", "PENDING"))
        rows = []
        for round_id in range(1, 4):
            for index in range(30):
                rows.append({"round": str(round_id), "mode": "async" if index % 2 else "sync", "component": "api" if index % 3 else "worker", "outcome": "success", "submitted_at": f"2026-01-0{round_id}T00:{index:02d}:00Z", "completed_at": f"2026-01-0{round_id}T00:{index:02d}:30Z", "queue_seconds": "5", "solve_seconds": "20", "retries": "1", "cpu_percent": "50", "memory_bytes": "60", "memory_max_bytes": "100", "swap_bytes": "0", "chrome_tasks": "1"})
        benchmark = ops.summarize(rows)
        self.assertEqual((benchmark["rounds"], benchmark["rows"], benchmark["status"]), (3, 90, "PENDING"))
        self.assertEqual(benchmark["workload_attempts"], 180)

    def test_config_resolver_alert_sources(self):
        metrics = {"queue_depth": 0, "queue_capacity": 10, "stale_processing_count": 0, "readiness_ok": True, "worker_age_seconds": 60, "memory_current_bytes": 50, "memory_max_bytes": 100, "disk_used_percent": 50, "inode_used_percent": 50, "cpu_sustained_percent": 50, "config_resolver_errors": 0}
        for source in ("cache", "website"):
            self.assertEqual(ops.evaluate_alerts({**metrics, "config_source": source})["config_resolver"], "PASS")
        self.assertEqual(ops.evaluate_alerts({**metrics, "config_source": "env"})["config_resolver"], "FAIL")
        for source in (None, "unknown"):
            self.assertEqual(ops.evaluate_alerts({**metrics, "config_source": source})["config_resolver"], "PENDING")
        self.assertEqual(ops.evaluate_alerts(metrics)["config_resolver"], "PENDING")
        for source in ("cache", "website", "env", None, "unknown"):
            self.assertEqual(ops.evaluate_alerts({**metrics, "config_source": source, "config_resolver_errors": 1})["config_resolver"], "FAIL")

    def test_benchmark_throughput_excludes_inter_round_gaps(self):
        def rows(gap: timedelta):
            result = []
            for round_id in range(3):
                start = datetime(2026, 1, 1, tzinfo=timezone.utc) + gap * round_id
                for index in range(30):
                    submitted = start + timedelta(minutes=index)
                    result.append({"round": str(round_id), "mode": "async" if index % 2 else "sync", "component": "api" if index % 3 else "worker", "outcome": "success", "submitted_at": submitted.isoformat(), "completed_at": (submitted + timedelta(seconds=30)).isoformat(), "queue_seconds": "5", "solve_seconds": "20", "retries": "0", "cpu_percent": "50", "memory_bytes": "60", "memory_max_bytes": "100", "swap_bytes": "0", "chrome_tasks": "1"})
            return result

        adjacent = ops.summarize(rows(timedelta(minutes=30)))
        gapped = ops.summarize(rows(timedelta(days=365)))
        fields = ("async_throughput_per_minute", "sync_throughput_per_minute", "aggregate_throughput_per_minute")
        self.assertEqual(tuple(gapped[field] for field in fields), tuple(adjacent[field] for field in fields))
        self.assertEqual(tuple(gapped[field] for field in fields), (0.508, 0.508, 1.017))

    def test_benchmark_approval_requires_slots_and_counts_retries(self):
        rows = []
        for round_id in range(3):
            for index in range(30):
                submitted = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=round_id, minutes=index)
                rows.append({"round": str(round_id), "mode": "async" if index % 2 else "sync", "component": "api" if index % 3 else "worker", "outcome": "success", "submitted_at": submitted.isoformat(), "completed_at": (submitted + timedelta(seconds=30)).isoformat(), "queue_seconds": "5", "solve_seconds": "20", "retries": "0", "cpu_percent": "50", "memory_bytes": "60", "memory_max_bytes": "100", "swap_bytes": "0", "chrome_tasks": "1"})
        self.assertEqual(ops.summarize(rows, failure_ratio_threshold=.1, latency_sla_seconds=60)["status"], "PENDING")
        self.assertEqual(ops.summarize(rows, failure_ratio_threshold=.1, latency_sla_seconds=60, global_chrome_slots=1)["status"], "PASS")
        retrying = [dict(row, retries="1") for row in rows]
        self.assertEqual(ops.summarize(retrying, failure_ratio_threshold=.1, latency_sla_seconds=60, global_chrome_slots=1)["status"], "FAIL")
        excessive = [dict(row, chrome_tasks="2") for row in rows]
        self.assertEqual(ops.summarize(excessive, failure_ratio_threshold=.1, latency_sla_seconds=60, global_chrome_slots=1)["status"], "FAIL")

    def test_mocked_runtime_preflight_requires_every_runtime_boundary(self):
        now = datetime.now(timezone.utc)
        component_environments = {
            "api": {"UVICORN_UDS": "/run/capsolve/uvicorn/api.sock", "ALLOWED_HOSTS": "api.example.invalid"},
            "purge": {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"},
            "backup": {"CAPSOLVE_BACKUP_DIR": "/var/backups/capsolve"},
        }
        timers = {unit: runtime_timer(now, unit=target, schedule=schedule, last_trigger=now - timedelta(seconds=seconds // 2), next_elapse=now + timedelta(seconds=seconds // 2)) for unit, (target, schedule, seconds) in production_preflight.TIMER_POLICIES.items()}
        output = io.StringIO()
        with mock.patch("sys.argv", ["capsolve-production-preflight", "--evidence", "/evidence"]), mock.patch.object(production_preflight, "_secure_json", return_value=evidence(now)), mock.patch.object(production_preflight, "_validate_component_environments", return_value=component_environments), mock.patch.object(production_preflight, "validate"), mock.patch.object(production_preflight, "_validate_backup_paths", return_value=Path("/var/backups/capsolve")), mock.patch.object(production_preflight, "_systemctl_components", return_value={}), mock.patch.object(production_preflight, "_validate_component_states") as states, mock.patch.object(production_preflight, "_validate_installed_units"), mock.patch.object(production_preflight, "_systemctl_timer", side_effect=lambda unit: timers[unit]), mock.patch.object(production_preflight, "_validate_identities") as identities, mock.patch.object(production_preflight, "_validate_private_directories") as profiles, mock.patch.object(production_preflight, "_validate_socket") as socket_check, mock.patch.object(production_preflight, "_validate_proxy_identities"), mock.patch.object(production_preflight, "_validate_installed_proxy_config") as proxies, mock.patch.object(production_preflight, "_readiness") as readiness, mock.patch.object(production_preflight, "_validate_no_api_tcp_listener") as listeners, contextlib.redirect_stdout(output):
            self.assertEqual(production_preflight.main(), 0)
        self.assertTrue(json.loads(output.getvalue())["operational_ready"])
        states.assert_called_once()
        identities.assert_called_once()
        profiles.assert_called_once()
        socket_check.assert_called_once()
        proxies.assert_called_once()
        readiness.assert_called_once()
        listeners.assert_called_once()

    def test_runbook_sequence_and_journal_namespace(self):
        runbook = Path("deployment/README.md").read_text(encoding="utf-8")
        self.assertNotRegex(runbook, r"journalctl (?!--namespace=capsolve)")
        rollout = runbook[runbook.index("## Rollout and canary"):]
        static = rollout.index("static production preflight")
        api = rollout.index("enable/start Xvfb and API")
        runtime = rollout.index("only now run runtime production preflight")
        self.assertLess(static, api)
        self.assertLess(api, runtime)
        self.assertIn("solver diagnostics may still be plain text", runbook)

    def test_migration_files_run_base_before_attribution(self):
        import migrate_sql
        self.assertEqual([path.name for path in migrate_sql.sql_files()], ["001_budi95_jobs.sql", "002_job_attribution.sql"])

    def test_static_database_and_systemd_artifacts(self):
        postgres = Path("deployment/postgresql.conf.example").read_text()
        hba = Path("deployment/pg_hba.conf.example").read_text()
        grants = Path("deployment/postgres_least_privilege.sql").read_text()
        schema = Path("sql/001_budi95_jobs.sql").read_text()
        self.assertIn("listen_addresses = 'localhost'", postgres)
        self.assertNotRegex(postgres, r"listen_addresses\s*=\s*['\"](?:\*|0\.0\.0\.0|::)['\"]")
        self.assertNotRegex(hba, r"0\.0\.0\.0/0|::/0")
        for variable in ("expected_db", "api_role", "worker_role", "purge_role"):
            self.assertIn(variable, grants)
        self.assertIn("current_database()", grants)
        self.assertIn("NOSUPERUSER", grants)
        self.assertNotIn("PASSWORD", grants.upper())
        self.assertNotIn("terminal_processed_at", schema)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is not set; disposable PostgreSQL Phase 7 tests skipped")
class Phase7PostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kwargs = safe_test_connection_kwargs(TEST_DATABASE_URL)
        cls.schema = "capsolve_phase7_" + uuid.uuid4().hex
        conn = psycopg2.connect(**cls.kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{cls.schema}"')
                cursor.execute(f'SET LOCAL search_path = "{cls.schema}"')
                sql = Path("sql/001_budi95_jobs.sql").read_text()
                cursor.execute(sql)
                cursor.execute(sql)
                cursor.execute(Path("sql/002_job_attribution.sql").read_text())
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = psycopg2.connect(**cls.kwargs)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
        finally:
            conn.close()

    def setUp(self):
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("TRUNCATE budi95_jobs RESTART IDENTITY")
        finally:
            conn.close()

    def connection(self, *args, **kwargs):
        del args, kwargs
        return psycopg2.connect(**self.kwargs, options=f"-c search_path={self.schema}")

    def insert(self, marker, status, processed_at):
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("INSERT INTO budi95_jobs (ulid, nric, status, processed_at) VALUES (%s, %s, %s, %s) RETURNING id", (uuid.uuid4().hex, marker, status, processed_at))
                return cursor.fetchone()[0]
        finally:
            conn.close()

    def markers(self):
        conn = self.connection()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute("SELECT nric FROM budi95_jobs ORDER BY id")
                return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    def test_purge_exact_order_boundary_null_concurrency_and_idempotency(self):
        cutoff = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
        old = cutoff - timedelta(seconds=2)
        for marker, status, timestamp in (
            ("first", "success", old), ("second", "failed", old), ("third", "success", cutoff - timedelta(seconds=1)),
            ("boundary", "success", cutoff), ("new", "failed", cutoff + timedelta(seconds=1)), ("null", "success", None),
            ("pending", "pending", old), ("processing", "processing", old),
        ):
            self.insert(marker, status, timestamp)
        with mock.patch.object(database, "get_connection", side_effect=self.connection):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                dry_runs = list(pool.map(lambda _: purge_jobs.purge(cutoff, 2, True)[:2], range(2)))
            self.assertEqual(dry_runs, [(2, 0), (2, 0)])
            self.assertEqual(purge_jobs.purge(cutoff, 2, False)[:2], (2, 2))
            self.assertEqual(purge_jobs.purge(cutoff, 2, False)[:2], (1, 1))
            self.assertEqual(purge_jobs.purge(cutoff, 2, False)[:2], (0, 0))
        self.assertEqual(self.markers(), ["boundary", "new", "null", "pending", "processing"])

    def test_dry_run_plain_select_does_not_wait_on_locked_row(self):
        cutoff = datetime.now(timezone.utc)
        row_id = self.insert("locked", "success", cutoff - timedelta(hours=1))
        locker = self.connection()
        try:
            with locker.cursor() as cursor:
                cursor.execute("SELECT id FROM budi95_jobs WHERE id = %s FOR UPDATE", (row_id,))
            started = time.monotonic()
            with mock.patch.object(database, "get_connection", side_effect=self.connection):
                self.assertEqual(purge_jobs.purge(cutoff, 1, True)[:2], (1, 0))
            self.assertLess(time.monotonic() - started, 1)
        finally:
            locker.rollback()
            locker.close()

    def _tools(self):
        conn = psycopg2.connect(**self.kwargs)
        try:
            server_major = int(conn.server_version // 10000)
        finally:
            conn.close()
        candidates = [(shutil.which("pg_dump"), shutil.which("pg_restore"))]
        root = Path("/usr/lib/postgresql")
        if root.is_dir():
            candidates.extend((str(path / "pg_dump"), str(path / "pg_restore")) for path in root.glob("*/bin"))
        for dump, restore in candidates:
            if dump and restore and Path(dump).is_file() and Path(restore).is_file():
                major = int(re.search(r"(\d+)", subprocess.run([dump, "--version"], check=True, capture_output=True, text=True).stdout).group(1))
                if major == server_major:
                    return dump, restore
        raise unittest.SkipTest("matching PostgreSQL client tools unavailable")

    def test_named_service_backup_restore_and_actual_least_privilege_grants(self):
        suffix = uuid.uuid4().hex[:10]
        source_name = f"capsolve_source_test_{suffix}"
        restore_name = f"capsolve_restore_test_{suffix}"
        roles = {kind: f"capsolve_{kind}_{suffix}" for kind in ("api", "worker", "purge")}
        admin = psycopg2.connect(**self.kwargs)
        admin.autocommit = True
        try:
            with admin.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE "{source_name}"')
                cursor.execute(f'CREATE DATABASE "{restore_name}"')
            with tempfile.TemporaryDirectory() as temporary:
                service_file = Path(temporary) / "pg_service.conf"
                services = {
                    "admin": (source_name, self.kwargs["user"]),
                    "backup": (source_name, self.kwargs["user"]),
                    "restore": (restore_name, self.kwargs["user"]),
                    **{kind: (source_name, role) for kind, role in roles.items()},
                }
                service_file.write_text("".join(f"[{name}]\nhost={self.kwargs['host']}\nport={self.kwargs['port']}\ndbname={database_name}\nuser={user}\n" for name, (database_name, user) in services.items()))
                service_file.chmod(0o600)
                pgpass_file = Path(temporary) / "pgpass"
                env = {**os.environ, "PGSERVICEFILE": str(service_file), "PGPASSFILE": str(pgpass_file)}
                admin_password = env.pop("PGPASSWORD", "")
                role_passwords = {}

                def escaped(value):
                    return str(value).replace("\\", "\\\\").replace(":", "\\:")

                def write_pgpass():
                    entries = [
                        (services[name][0], services[name][1], admin_password)
                        for name in ("admin", "backup", "restore")
                    ]
                    entries.extend((source_name, roles[kind], password) for kind, password in role_passwords.items())
                    pgpass_file.write_text("".join(
                        f"{escaped(self.kwargs['host'])}:{self.kwargs['port']}:{escaped(database_name)}:{escaped(user)}:{escaped(password)}\n"
                        for database_name, user, password in entries
                    ))
                    pgpass_file.chmod(0o600)

                def safe_stderr(result):
                    stderr = result.stderr
                    for password in (admin_password, *role_passwords.values()):
                        if password:
                            stderr = stderr.replace(password, "[redacted]")
                    return stderr

                write_pgpass()
                psql = ["psql", "-X", "--set", "ON_ERROR_STOP=1"]
                result = subprocess.run([*psql, "--dbname=service=admin", "--file=sql/001_budi95_jobs.sql"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, safe_stderr(result))
                grant = [*psql, "--dbname=service=admin", "-v", f"expected_db={source_name}", "-v", f"api_role={roles['api']}", "-v", f"worker_role={roles['worker']}", "-v", f"purge_role={roles['purge']}", "--file=deployment/postgres_least_privilege.sql"]
                for _ in range(2):
                    result = subprocess.run(grant, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, safe_stderr(result))

                role_passwords = {kind: secrets.token_urlsafe(32) for kind in roles}
                with admin.cursor() as cursor:
                    for kind, role in roles.items():
                        cursor.execute(
                            sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(role)),
                            (role_passwords[kind],),
                        )
                write_pgpass()

                mismatch = subprocess.run([*psql, "--dbname=service=restore", "-v", "expected_db=wrong", "-v", f"api_role={roles['api']}", "-v", f"worker_role={roles['worker']}", "-v", f"purge_role={roles['purge']}", "--file=deployment/postgres_least_privilege.sql"], env=env, capture_output=True, text=True)
                self.assertNotEqual(mismatch.returncode, 0, safe_stderr(mismatch))

                allowed = {"api": "SELECT count(*) FROM budi95_jobs; INSERT INTO budi95_jobs (ulid,nric) VALUES ('a','a');", "worker": "SELECT count(*) FROM budi95_jobs; UPDATE budi95_jobs SET updated_at=NOW() WHERE false;", "purge": "SELECT count(*) FROM budi95_jobs; DELETE FROM budi95_jobs WHERE false;"}
                forbidden = {"api": "DELETE FROM budi95_jobs;", "worker": "INSERT INTO budi95_jobs (ulid,nric) VALUES ('w','w');", "purge": "UPDATE budi95_jobs SET updated_at=NOW();"}
                for kind in roles:
                    allowed_result = subprocess.run([*psql, f"--dbname=service={kind}", "--command", allowed[kind]], env=env, capture_output=True, text=True)
                    self.assertEqual(allowed_result.returncode, 0, safe_stderr(allowed_result))
                    forbidden_result = subprocess.run([*psql, f"--dbname=service={kind}", "--command", forbidden[kind]], env=env, capture_output=True, text=True)
                    self.assertNotEqual(forbidden_result.returncode, 0, safe_stderr(forbidden_result))
                with admin.cursor() as cursor:
                    cursor.execute("SELECT bool_and(NOT rolsuper) FROM pg_roles WHERE rolname = ANY(%s)", (list(roles.values()),))
                    self.assertEqual(cursor.fetchone(), (True,))

                dump, restore = self._tools()
                archive = Path(temporary) / "capsolve.dump"
                subprocess.run([dump, "--dbname=service=backup", "--format=custom", "--no-owner", "--no-privileges", "--file", str(archive)], check=True, env=env, capture_output=True)
                restore_started = time.monotonic()
                subprocess.run([restore, "--dbname=service=restore", "--exit-on-error", "--no-owner", "--no-privileges", str(archive)], check=True, env=env, capture_output=True)
                restore_duration = time.monotonic() - restore_started
                self.assertLessEqual(restore_duration, production_preflight.MAX_RTO_MINUTES * 60)
                counts = []
                for service in ("backup", "restore"):
                    result = subprocess.run([*psql, f"--dbname=service={service}", "--tuples-only", "--no-align", "--command", "SELECT count(*) FROM budi95_jobs"], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, safe_stderr(result))
                    counts.append(int(result.stdout.strip()))
                self.assertEqual(counts[0], counts[1])
        finally:
            with admin.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN (%s, %s)", (source_name, restore_name))
                cursor.execute(f'DROP DATABASE IF EXISTS "{source_name}"')
                cursor.execute(f'DROP DATABASE IF EXISTS "{restore_name}"')
                for role in roles.values():
                    cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
            admin.close()


if __name__ == "__main__":
    unittest.main()
