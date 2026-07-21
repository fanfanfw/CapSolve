from __future__ import annotations

import unittest
from unittest import mock

import job_repository


class FakeCursor:
    def __init__(self, outstanding: int, fail_on: str | None = None):
        self.outstanding = outstanding
        self.fail_on = fail_on
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if self.fail_on and self.fail_on in normalized:
            raise RuntimeError("database operation failed")

    def fetchone(self):
        if "COUNT(*)" in self.executed[-1][0]:
            return {"count": self.outstanding}
        return {"nric": self.executed[-1][1][1], "ulid": self.executed[-1][1][0]}


class FakeConnection:
    def __init__(self, outstanding: int, *, fail_on: str | None = None, fail_commit: bool = False):
        self.cursor_instance = FakeCursor(outstanding, fail_on)
        self.fail_commit = fail_commit
        self.close_count = 0
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type:
            self.rolled_back = True
            return False
        if self.fail_commit:
            raise RuntimeError("commit failed")
        return False

    def cursor(self, **kwargs):
        return self.cursor_instance

    def close(self):
        self.close_count += 1


class Phase2AdmissionUnitTest(unittest.TestCase):
    def test_lock_then_exact_outstanding_count_then_insert(self) -> None:
        connection = FakeConnection(outstanding=2)
        with mock.patch.object(job_repository.database, "get_connection", return_value=connection), mock.patch.object(
            job_repository, "new_ulid", return_value="a" * 32
        ):
            job = job_repository.create_job("S1234567A", max_attempts=4, capacity=3)

        statements = connection.cursor_instance.executed
        self.assertEqual(statements[0], ("SELECT pg_advisory_xact_lock(%s)", (job_repository.QUEUE_ADMISSION_LOCK_KEY,)))
        self.assertEqual(
            statements[1],
            ("SELECT COUNT(*) FROM budi95_jobs WHERE status IN ('pending', 'processing')", ()),
        )
        self.assertTrue(statements[2][0].startswith("INSERT INTO budi95_jobs"))
        self.assertEqual(statements[2][1], ("a" * 32, "S1234567A", 4, "legacy", "legacy"))
        self.assertEqual(job, {"nric": "S1234567A", "ulid": "a" * 32})
        self.assertNotEqual(job_repository.QUEUE_ADMISSION_LOCK_KEY, 0)
        self.assertEqual(connection.close_count, 1)

    def test_at_capacity_rejects_without_insert_or_delete(self) -> None:
        connection = FakeConnection(outstanding=3)
        with mock.patch.object(job_repository.database, "get_connection", return_value=connection):
            with self.assertRaises(job_repository.QueueFullError):
                job_repository.create_job("S1234567A", max_attempts=3, capacity=3)

        statements = [sql for sql, _ in connection.cursor_instance.executed]
        self.assertEqual(len(statements), 2)
        self.assertFalse(any("INSERT" in sql or "DELETE" in sql or "UPDATE" in sql for sql in statements))
        self.assertTrue(connection.rolled_back)
        self.assertEqual(connection.close_count, 1)

    def test_sql_and_commit_failures_close_once(self) -> None:
        for connection in (
            FakeConnection(outstanding=0, fail_on="INSERT INTO"),
            FakeConnection(outstanding=0, fail_commit=True),
        ):
            with self.subTest(commit=connection.fail_commit), mock.patch.object(
                job_repository.database, "get_connection", return_value=connection
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    job_repository.create_job("S1234567A", max_attempts=3, capacity=3)
            self.assertEqual(connection.close_count, 1)
            self.assertEqual(connection.rolled_back, not connection.fail_commit)


if __name__ == "__main__":
    unittest.main()
