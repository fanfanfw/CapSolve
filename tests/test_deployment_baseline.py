from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deployment import baseline as baseline_tool


class BaselineEvidenceTest(unittest.TestCase):
    def _evidence(self, directory: str, source_identity: str = "a" * 64) -> tuple[Path, dict]:
        evidence = Path(directory)
        configuration = evidence / "configuration"
        configuration.mkdir()
        backups = {}
        for label, filename in baseline_tool.CONFIG_FILES.items():
            path = configuration / filename
            path.write_text(f"safe {label}\n", encoding="utf-8")
            backups[label] = {
                "file": f"configuration/{filename}",
                "sha256": baseline_tool._sha256(path),
                "original_path": f"/etc/{filename}",
                "resolved_path": f"/etc/real/{filename}",
                "symlink_chain": [
                    {"path": f"/etc/{filename}", "target": f"real/{filename}", "uid": 0, "gid": 0, "mode": 0o777}
                ],
                "file_uid": 0,
                "file_gid": 0,
                "file_mode": 0o640,
            }
        dump = evidence / "database.dump"
        dump.write_bytes(b"test archive")
        schema = evidence / "budi95_jobs.schema.sql"
        schema.write_text("CREATE TABLE budi95_jobs ();\n", encoding="utf-8")
        baseline = {
            "format_version": 1,
            "state": "CAPTURED_RESTORE_PENDING",
            "captured_at_utc": "2026-07-15T00:00:00+00:00",
            "deployment_commit": "b" * 40,
            "rollback_commit": "c" * 40,
            "environment_variable_names": ["SECOND_OPTION", "SERVICE_OPTION"],
            "configuration_backups": backups,
            "database": {
                "backup_file": dump.name,
                "backup_sha256": baseline_tool._sha256(dump),
                "schema_file": schema.name,
                "schema_sha256": baseline_tool._sha256(schema),
                "budi95_jobs_row_count": 7,
                "source_database_identity_sha256": source_identity,
            },
            "baseline_24h": {
                "status": "PENDING_NOT_AVAILABLE",
                "period_hours": 24,
                "metrics": {name: None for name in baseline_tool.METRIC_NAMES},
            },
        }
        (evidence / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        return evidence, baseline

    def test_backup_archive_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "database.dump"
            archive.write_bytes(b"archive")
            scratch = root / "rows.sql"
            with mock.patch.object(
                baseline_tool,
                "_run",
                side_effect=lambda command: scratch.write_text(
                    "COPY public.budi95_jobs (id) FROM stdin;\n1\n2\n\\.\n", encoding="utf-8"
                ),
            ):
                self.assertEqual(baseline_tool._archive_row_count(archive, scratch), 2)
            self.assertFalse(scratch.exists())

    def test_scheduled_backup_is_atomic_bounded_and_keeps_last_success_on_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            evidence = root / "scheduled-backup.json"
            evidence.write_text('{"status":"previous"}\n', encoding="utf-8")
            evidence.chmod(0o600)
            args = argparse.Namespace(backup_dir=str(root), evidence_file=str(evidence), source_pgservice="capsolve_backup", retention_hours=24)
            with mock.patch.object(baseline_tool, "_database_identity", return_value="a" * 64), mock.patch.object(baseline_tool, "_run", side_effect=RuntimeError("failed")), self.assertRaises(RuntimeError):
                baseline_tool.scheduled_backup(args)
            self.assertEqual(json.loads(evidence.read_text()), {"status": "previous"})
            self.assertEqual(list(root.glob("capsolve-*.dump")), [])

            def run(command, **kwargs):
                if command[0] == "pg_dump":
                    Path(command[command.index("--file") + 1]).write_bytes(b"custom archive")

            with mock.patch.object(baseline_tool, "_database_identity", return_value="a" * 64), mock.patch.object(baseline_tool, "_run", side_effect=run), mock.patch.object(baseline_tool, "_archive_row_count", return_value=7):
                self.assertEqual(baseline_tool.scheduled_backup(args), 0)
            record = json.loads(evidence.read_text())
            archive = root / record["backup_file"]
            self.assertEqual(record["backup_sha256"], baseline_tool._sha256(archive))
            self.assertNotIn("password", json.dumps(record).lower())
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)

    def test_scheduled_backup_deletes_expired_validated_dump_but_not_symlink_or_newest(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            evidence = root / "scheduled-backup.json"
            expired = root / "capsolve-20200101T000000Z.dump"
            expired.write_bytes(b"validated old")
            os.utime(expired, (1, 1))
            outside = root.parent / f"{root.name}-outside.dump"
            outside.write_bytes(b"outside")
            link = root / "capsolve-20200102T000000Z.dump"
            link.symlink_to(outside)
            (root / "restore-test.json").write_text(json.dumps({"format_version": 1, "status": "VERIFIED_ROW_COUNT_MATCH", "verified_at_utc": "2026-01-01T00:00:00+00:00", "backup_sha256": baseline_tool._sha256(expired), "source_budi95_jobs_row_count": 1, "restored_budi95_jobs_row_count": 1}))
            args = argparse.Namespace(backup_dir=str(root), evidence_file=str(evidence), source_pgservice="capsolve_backup", retention_hours=24)
            def run(command, **kwargs):
                if command[0] == "pg_dump":
                    Path(command[command.index("--file") + 1]).write_bytes(b"new")
            try:
                with mock.patch.object(baseline_tool, "_database_identity", return_value="a" * 64), mock.patch.object(baseline_tool, "_run", side_effect=run), mock.patch.object(baseline_tool, "_archive_row_count", return_value=1):
                    baseline_tool.scheduled_backup(args)
                self.assertFalse(expired.exists())
                self.assertTrue(link.is_symlink())
                self.assertTrue(outside.exists())
                self.assertTrue((root / json.loads(evidence.read_text())["backup_file"]).exists())
            finally:
                outside.unlink(missing_ok=True)

    def test_checkout_rejects_untracked_runtime_content(self) -> None:
        self.assertEqual(baseline_tool.ALLOWED_UNTRACKED, set())
        self.assertEqual(baseline_tool._unsafe_checkout_paths(""), [])
        self.assertEqual(baseline_tool._unsafe_checkout_paths("?? deployment/local.conf\n"), ["deployment/local.conf"])
        self.assertEqual(baseline_tool._unsafe_checkout_paths(" M service.py\n"), ["service.py"])
        with mock.patch.object(baseline_tool, "_git_commit", return_value="a" * 40), mock.patch.object(
            baseline_tool.subprocess,
            "run",
            return_value=baseline_tool.subprocess.CompletedProcess([], 0, stdout="?? deployment/local.conf\n"),
        ):
            with self.assertRaisesRegex(ValueError, "uncommitted changes"):
                baseline_tool._deployment_commit()

    def test_config_backup_preserves_path_symlink_owner_group_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.conf"
            real.write_text("configuration\n", encoding="utf-8")
            real.chmod(0o640)
            second = root / "second.conf"
            second.symlink_to("real.conf")
            first = root / "first.conf"
            first.symlink_to("second.conf")
            target = root / "captured.conf"

            record = baseline_tool._config_backup(str(first), target)

            real_stat = real.stat()
            first_stat = first.lstat()
            self.assertEqual(record["original_path"], str(first))
            self.assertEqual(record["resolved_path"], str(real))
            self.assertEqual([link["target"] for link in record["symlink_chain"]], ["second.conf", "real.conf"])
            self.assertEqual(record["symlink_chain"][0]["uid"], first_stat.st_uid)
            self.assertEqual(record["symlink_chain"][0]["gid"], first_stat.st_gid)
            self.assertEqual(record["symlink_chain"][0]["mode"], first_stat.st_mode & 0o7777)
            self.assertEqual(record["file_uid"], real_stat.st_uid)
            self.assertEqual(record["file_gid"], real_stat.st_gid)
            self.assertEqual(record["file_mode"], 0o640)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_config_backup_captures_ancestor_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_dir = root / "real"
            real_dir.mkdir()
            config = real_dir / "service.conf"
            config.write_text("configuration\n", encoding="utf-8")
            alias_dir = root / "alias"
            alias_dir.symlink_to("real", target_is_directory=True)

            record = baseline_tool._config_backup(str(alias_dir / "service.conf"), root / "captured.conf")

            self.assertEqual(record["resolved_path"], str(config))
            self.assertEqual([link["path"] for link in record["symlink_chain"]], [str(alias_dir)])
            self.assertEqual(record["symlink_chain"][0]["target"], "real")
            self.assertTrue(
                baseline_tool._symlink_chain_resolves(
                    record["original_path"], record["resolved_path"], record["symlink_chain"]
                )
            )

    def test_environment_inventory_keeps_names_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "service.env"
            source.write_text("SERVICE_OPTION=discarded-value\nexport SECOND_OPTION=also-discarded\n", encoding="utf-8")
            inventory = baseline_tool._env_names(source)
            self.assertEqual(inventory, ["SECOND_OPTION", "SERVICE_OPTION"])
            self.assertNotIn("discarded-value", json.dumps(inventory))
            self.assertNotIn("also-discarded", json.dumps(inventory))

    def test_stable_database_identity_is_route_independent_and_fail_closed(self) -> None:
        with mock.patch.object(baseline_tool, "_psql", return_value="123456789|16384"):
            self.assertEqual(baseline_tool._database_identity("socket_alias"), baseline_tool._database_identity("tcp_alias"))
        with mock.patch.object(baseline_tool, "_psql", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "stable PostgreSQL"):
                baseline_tool._database_identity("unprivileged")

    def test_restore_service_is_name_only(self) -> None:
        self.assertEqual(baseline_tool._service("capsolve_baseline_empty"), "capsolve_baseline_empty")
        for value in ("scheme://identity@server/db", "service=name credential=value", "name/path"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                baseline_tool._service(value)

    def test_restore_rejects_source_alias_before_restore(self) -> None:
        identity = hashlib.sha256(b"123456789|16384").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            evidence, _ = self._evidence(directory, identity)
            args = argparse.Namespace(
                evidence_dir=str(evidence), restore_pgservice="source_via_other_route", confirm_disposable_database=True
            )
            with mock.patch.object(baseline_tool, "_database_identity", return_value=identity), mock.patch.object(
                baseline_tool, "_target_is_empty"
            ) as empty, mock.patch.object(baseline_tool, "_restore_archive") as restore:
                with self.assertRaisesRegex(ValueError, "separate"):
                    baseline_tool.restore_test(args)
                empty.assert_not_called()
                restore.assert_not_called()

    def test_empty_target_query_covers_collations_and_publications(self) -> None:
        with mock.patch.object(baseline_tool, "_psql", return_value="1") as psql:
            self.assertFalse(baseline_tool._target_is_empty("disposable"))
        query = psql.call_args.args[0]
        self.assertIn("pg_collation", query)
        self.assertIn("pg_publication", query)
        self.assertIn("pg_subscription", query)
        self.assertIn("pg_foreign_server", query)
        self.assertIn("pg_user_mapping", query)

    def test_restore_rejects_nonempty_target_before_restore(self) -> None:
        for user_object in ("collation", "publication"):
            with self.subTest(user_object=user_object), tempfile.TemporaryDirectory() as directory:
                evidence, _ = self._evidence(directory)
                args = argparse.Namespace(
                    evidence_dir=str(evidence), restore_pgservice="nonempty_disposable", confirm_disposable_database=True
                )
                with mock.patch.object(baseline_tool, "_database_identity", return_value="d" * 64), mock.patch.object(
                    baseline_tool, "_target_is_empty", return_value=False
                ), mock.patch.object(baseline_tool, "_restore_archive") as restore:
                    with self.assertRaisesRegex(ValueError, "no non-system objects"):
                        baseline_tool.restore_test(args)
                    restore.assert_not_called()

    def test_restore_uses_named_service_without_dsn_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence, _ = self._evidence(directory)
            args = argparse.Namespace(
                evidence_dir=str(evidence), restore_pgservice="empty_disposable", confirm_disposable_database=True
            )
            with mock.patch.dict(os.environ, {"TEST_DATABASE_URL": "must-not-be-used"}, clear=False), mock.patch.object(
                baseline_tool, "_database_identity", return_value="d" * 64
            ), mock.patch.object(baseline_tool, "_target_is_empty", return_value=True), mock.patch.object(
                baseline_tool, "_restore_archive"
            ) as restore, mock.patch.object(baseline_tool, "_row_count", return_value=7):
                self.assertEqual(baseline_tool.restore_test(args), 0)
                restore.assert_called_once_with(evidence / "database.dump", "empty_disposable")

    def test_metrics_reject_nan_infinity_and_huge_values(self) -> None:
        for value in ("nan", "inf", "-inf", "1e1000000"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                baseline_tool._nonnegative_float(value)
        with self.assertRaises(argparse.ArgumentTypeError):
            baseline_tool._nonnegative_int("9" * 10000)
        for metric_value in (float("nan"), 10**400):
            with self.subTest(metric_value=str(metric_value)[:20]), tempfile.TemporaryDirectory() as directory:
                evidence, baseline = self._evidence(directory)
                baseline["baseline_24h"]["status"] = "RECORDED_PARTIAL"
                baseline["baseline_24h"]["metrics"]["median_process_seconds"] = metric_value
                (evidence / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "finite"):
                    baseline_tool._validate_baseline(evidence)

    def test_cli_argument_error_is_redacted(self) -> None:
        for arguments, marker in (
            (["restore-test", "/tmp/evidence", "--restore-pgservice", "bad=value"], "bad=value"),
            (["capture", "/tmp/evidence", "--submit-count", "9" * 10000], "9999999999"),
        ):
            with self.subTest(arguments=arguments[:2]):
                output = io.StringIO()
                with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
                    baseline_tool.parser().parse_args(arguments)
                self.assertNotIn(marker, output.getvalue())
                self.assertIn("baseline: invalid arguments", output.getvalue())

    def test_cli_error_is_redacted(self) -> None:
        class FakeParser:
            @staticmethod
            def parse_args() -> argparse.Namespace:
                return argparse.Namespace(handler=lambda args: (_ for _ in ()).throw(ValueError("sensitive-marker")))

        output = io.StringIO()
        with mock.patch.object(baseline_tool, "parser", return_value=FakeParser()), contextlib.redirect_stderr(output):
            self.assertEqual(baseline_tool.main(), 1)
        self.assertNotIn("sensitive-marker", output.getvalue())
        self.assertEqual(output.getvalue(), "baseline: operation failed; review prerequisites and protected operator logs\n")

    def test_validator_accepts_complete_secret_safe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence, baseline = self._evidence(directory)
            restore = {
                "format_version": 1,
                "status": "VERIFIED_ROW_COUNT_MATCH",
                "verified_at_utc": "2026-07-15T00:10:00+00:00",
                "backup_sha256": baseline["database"]["backup_sha256"],
                "source_budi95_jobs_row_count": 7,
                "restored_budi95_jobs_row_count": 7,
            }
            (evidence / "restore-test.json").write_text(json.dumps(restore), encoding="utf-8")
            loaded = baseline_tool._validate_baseline(evidence)
            baseline_tool._validate_restore(evidence, loaded)

            baseline["unexpected_value"] = "must-be-rejected"
            (evidence / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected or missing fields"):
                baseline_tool._validate_baseline(evidence)


if __name__ == "__main__":
    unittest.main()
