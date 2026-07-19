from __future__ import annotations

import os
import secrets
import unittest
from unittest import mock

from settings import load_settings
import service


VALID_KEY = secrets.token_urlsafe(32)


def production(**overrides: str) -> dict[str, str]:
    values = {
        "ENVIRONMENT": "production",
        "JOB_RETENTION_HOURS": "24",
        "API_KEY": VALID_KEY,
        "API_IP_ALLOWLIST": "192.0.2.10/32,2001:db8::/64",
        "ALLOWED_HOSTS": "api.example.invalid",
        "UVICORN_SOCKET_GID": str(os.getgid()),
    }
    values.update(overrides)
    return values


class Phase1SettingsTest(unittest.TestCase):
    def test_non_ascii_inbound_api_key_returns_existing_invalid_key_401(self) -> None:
        with mock.patch.object(service, "API_KEYS", ("valid-ascii-key",)):
            with self.assertRaises(service.HTTPException) as raised:
                service.verify_api_key("unsupported-\N{SNOWMAN}")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Invalid API key.")

    def test_api_startup_fails_without_production_key(self) -> None:
        values = production()
        values.pop("API_KEY")
        with mock.patch.object(service, "load_dotenv"), mock.patch.dict(service.os.environ, values, clear=True):
            with self.assertRaisesRegex(ValueError, "API key"):
                service._configure()

    def test_generated_key_and_networks_are_accepted_without_exposing_key(self) -> None:
        settings = load_settings("api", production())
        self.assertEqual(settings.api_keys, (VALID_KEY,))
        self.assertEqual([str(network) for network in settings.api_ip_allowlist or ()], ["192.0.2.10/32", "2001:db8::/64"])

    def test_api_keys_nonempty_fully_overrides_api_key(self) -> None:
        second = secrets.token_urlsafe(32)
        settings = load_settings("api", production(API_KEY="not-active", API_KEYS=f"{VALID_KEY},{second}"))
        self.assertEqual(settings.api_keys, (VALID_KEY, second))
        with self.assertRaisesRegex(ValueError, "API key configuration"):
            load_settings("api", production(API_KEYS=f"{VALID_KEY},"))

    def test_production_requires_api_security_configuration(self) -> None:
        cases = [
            ({"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24", "API_IP_ALLOWLIST": "192.0.2.1", "ALLOWED_HOSTS": "api.example.invalid"}, "API key"),
            (production(API_IP_ALLOWLIST="*"), "explicit networks"),
            (production(API_IP_ALLOWLIST=""), "must not be empty"),
            ({key: value for key, value in production().items() if key != "ALLOWED_HOSTS"}, "must not be empty"),
            (production(ALLOWED_HOSTS=""), "must not be empty"),
            (production(ALLOWED_HOSTS="*"), "explicit"),
        ]
        for values, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                load_settings("api", values)

    def test_invalid_production_keys_are_rejected_without_echoing_values(self) -> None:
        repeat_units = ["Ab3_-X9q"[:size] * ((42 + size) // size) for size in range(1, 9)]
        invalid = [
            "a" * 42,
            "a" * 129,
            "A" * 42 + ".",
            "development-only-change-me",
            "replace-with-a-strong-api-key",
            *repeat_units,
        ]
        for key in invalid:
            with self.subTest(length=len(key)):
                with self.assertRaises(ValueError) as raised:
                    load_settings("api", production(API_KEY=key))
                self.assertNotIn(key, str(raised.exception))

    def test_invalid_cidr_error_does_not_expose_key_or_input(self) -> None:
        invalid = "not-a-network-sensitive-marker"
        with self.assertRaises(ValueError) as raised:
            load_settings("api", production(API_IP_ALLOWLIST=invalid))
        message = str(raised.exception)
        self.assertNotIn(VALID_KEY, message)
        self.assertNotIn(invalid, message)

    def test_production_comma_lists_reject_empty_members(self) -> None:
        cases = {
            "API_IP_ALLOWLIST": (",192.0.2.1", "192.0.2.1,", "192.0.2.1,,2001:db8::1"),
            "ALLOWED_HOSTS": (",api.example.invalid", "api.example.invalid,", "api.example.invalid,,other.example.invalid"),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, position=value.find(",")), self.assertRaises(ValueError) as raised:
                    load_settings("api", production(**{name: value}))
                self.assertIn(name, str(raised.exception))
                self.assertNotIn(value, str(raised.exception))
                self.assertNotIn(VALID_KEY, str(raised.exception))

    def test_production_allowed_hosts_requires_and_accepts_dns_hostname(self) -> None:
        with self.assertRaisesRegex(ValueError, "DNS hostname"):
            load_settings("api", production(ALLOWED_HOSTS="192.0.2.10,[2001:db8::1]"))
        self.assertEqual(load_settings("api", production()).allowed_hosts, ("api.example.invalid",))

    def test_development_wildcard_is_supported(self) -> None:
        settings = load_settings("api", {"ENVIRONMENT": "development", "API_KEY": "development-key", "API_IP_ALLOWLIST": "*"})
        self.assertIsNone(settings.api_ip_allowlist)

    def test_worker_and_purge_do_not_require_inbound_api_settings(self) -> None:
        for component in ("worker", "purge"):
            with self.subTest(component=component):
                settings = load_settings(component, {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": "24"})
                self.assertEqual(settings.api_keys, ())
                self.assertEqual(settings.job_retention_hours, 24)

    def test_invalid_environment_boolean_and_typo_are_not_accepted(self) -> None:
        for environment in ("staging", "PRODUCTION", ""):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, "ENVIRONMENT"):
                load_settings("worker", {"ENVIRONMENT": environment})
        for name in ("API_DOCS_ENABLED", "BUDI95_AUTO_RESOLVE", "BUDI95_FORCE_ENV_CONFIG", "ENABLE_XVFB_VIRTUAL_DISPLAY"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "documented boolean"):
                load_settings("worker", {name: "enabled"})
        with self.assertRaises(ValueError):
            load_settings("worker", {"JOB_MAX_ATTEMPS": "9"})
        with self.assertRaisesRegex(ValueError, "JOB_RETENTION_HOURS"):
            load_settings("purge", {"ENVIRONMENT": "production", "JOB_RETENTION_HOURS": ""})

    def test_every_integer_setting_rejects_invalid_or_out_of_range_values(self) -> None:
        minimums = {
            "API_PORT": 1,
            "PORT": 1,
            "JOB_QUEUE_CAPACITY": 1,
            "JOB_QUEUE_RETRY_AFTER_SECONDS": 1,
            "BUDI95_SUBMIT_RATE_LIMIT_PER_MINUTE": 0,
            "BUDI95_READ_RATE_LIMIT_PER_MINUTE": 0,
            "JOB_BATCH_LIMIT": 1,
            "JOB_MAX_ATTEMPTS": 1,
            "JOB_RESET_STALE_MINUTES": 0,
            "JOB_RETENTION_HOURS": 1,
            "SYNC_QUEUE_MAX_WAITING": 0,
            "MAX_WORKERS": 1,
            "GLOBAL_CHROME_SLOTS": 1,
            "DB_CONNECT_TIMEOUT": 1,
            "DB_PORT": 1,
            "SOLVER_TIMEOUT": 1,
            "LOCAL_POST_TIMEOUT": 1,
            "BUDI95_CONFIG_CACHE_SECONDS": 0,
            "BUDI95_CONFIG_FETCH_TIMEOUT": 1,
        }
        for name, minimum in minimums.items():
            invalid_values = ["", "not-an-integer", str(minimum - 1)]
            for value in invalid_values:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    load_settings("worker", {name: value})


if __name__ == "__main__":
    unittest.main()
