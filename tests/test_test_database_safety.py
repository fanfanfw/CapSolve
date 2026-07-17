from __future__ import annotations

import unittest
from unittest import mock

import tests.test_queue_postgres as postgres_test


class Phase2PostgresUrlSafetyTest(unittest.TestCase):
    def test_accepts_only_explicit_loopback_test_database_fields(self) -> None:
        self.assertEqual(
            postgres_test.safe_test_connection_kwargs("postgresql://127.0.0.1:55432/capsolve_test"),
            {"host": "127.0.0.1", "port": 55432, "dbname": "capsolve_test", "user": "postgres"},
        )
        self.assertEqual(
            postgres_test.safe_test_connection_kwargs("postgresql://[::1]/testdb"),
            {"host": "::1", "port": 5432, "dbname": "testdb", "user": "postgres"},
        )

    def test_rejects_redirects_userinfo_fragments_and_unsafe_targets(self) -> None:
        unsafe = (
            "postgresql://127.0.0.1/capsolve_test?host=remote.invalid",
            "postgresql://127.0.0.1/capsolve_test?hostaddr=203.0.113.1",
            "postgresql://127.0.0.1/capsolve_test?service=production",
            "postgresql://127.0.0.1/capsolve_test?options=-csearch_path=public",
            "postgresql://user@127.0.0.1/capsolve_test",
            "postgresql://user:password@127.0.0.1/capsolve_test",
            "postgresql://127.0.0.1/capsolve_test#host=remote.invalid",
            "postgresql://db.internal/capsolve_test",
            "postgresql://127.0.0.1/production",
            "service=capsolve_test",
        )
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                postgres_test.safe_test_connection_kwargs(url)

    def test_connect_receives_constructed_kwargs_not_original_url(self) -> None:
        connection = mock.MagicMock()
        with mock.patch.object(postgres_test.psycopg2, "connect", return_value=connection) as connect:
            kwargs = postgres_test.safe_test_connection_kwargs("postgresql://localhost:55432/capsolve_test")
            postgres_test.psycopg2.connect(**kwargs)
        connect.assert_called_once_with(host="localhost", port=55432, dbname="capsolve_test", user="postgres")


if __name__ == "__main__":
    unittest.main()
