from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from typing import Mapping


_PRODUCTION_KEY = re.compile(r"[A-Za-z0-9_-]{43,128}")
_PLACEHOLDER_KEYS = {
    "development-only-change-me",
    "replace-with-a-strong-api-key",
    "your-api-key",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_CHROME_SLOT_BASE_KEY = 1_128_360_000
_MAX_GLOBAL_CHROME_SLOTS = 2**63 - _CHROME_SLOT_BASE_KEY
MAX_JOB_RETENTION_HOURS = 8760
MAX_PURGE_BATCH_LIMIT = 10_000
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def parse_host(value: str) -> str:
    if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid host")
    if value.startswith("["):
        if value.count("[") != 1 or value.count("]") != 1:
            raise ValueError("invalid host")
        end = value.index("]")
        host, suffix = value[1:end], value[end + 1 :]
        if "%" in host:
            raise ValueError("invalid host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("invalid host") from None
        if address.version != 6:
            raise ValueError("invalid host")
        normalized = str(address)
    else:
        if "[" in value or "]" in value or value.count(":") > 1:
            raise ValueError("invalid host")
        host, separator, port = value.partition(":")
        suffix = f":{port}" if separator else ""
        if host.endswith("."):
            raise ValueError("invalid host")
        try:
            normalized = str(ipaddress.ip_address(host))
        except ValueError:
            if len(host) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in host.split(".")):
                raise ValueError("invalid host") from None
            normalized = host.lower()
    if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit() or not 1 <= int(suffix[1:]) <= 65535):
        raise ValueError("invalid host")
    return normalized


@dataclass(frozen=True)
class Settings:
    environment: str
    api_keys: tuple[str, ...]
    api_ip_allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None
    allowed_hosts: tuple[str, ...]
    api_docs_enabled: bool
    api_host: str | None
    api_port: int
    forwarded_allow_ips: str
    uvicorn_uds: str | None
    uvicorn_socket_mode: int
    uvicorn_socket_parent_gid: int
    uvicorn_socket_gid: int | None
    job_queue_capacity: int
    job_queue_retry_after_seconds: int
    job_batch_limit: int
    job_max_attempts: int
    job_reset_stale_minutes: int
    job_retention_hours: int
    purge_batch_limit: int
    sync_queue_max_waiting: int
    max_workers: int
    global_chrome_slots: int
    db_connect_timeout: int
    db_port: int
    solver_timeout: int
    local_post_timeout: int
    budi95_config_cache_seconds: int
    budi95_config_fetch_timeout: int
    budi95_auto_resolve: bool
    budi95_force_env_config: bool
    enable_xvfb_virtual_display: bool


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = values.get(name)
    if raw is None:
        value = default
    else:
        text = raw.strip()
        if not re.fullmatch(r"-?[0-9]+", text):
            raise ValueError(f"{name} must be an integer")
        value = int(text)
    if value < minimum or maximum is not None and value > maximum:
        requirement = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be {requirement}")
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must use a documented boolean value")


def _production_key_is_repeated(key: str) -> bool:
    return any(len(key) % size == 0 and key == key[:size] * (len(key) // size) for size in range(1, 9))


def _api_keys(values: Mapping[str, str], production: bool) -> tuple[str, ...]:
    rotation_value = values.get("API_KEYS", "")
    if rotation_value.strip():
        keys = tuple(key.strip() for key in rotation_value.split(","))
        if not all(keys):
            raise ValueError("API key configuration is invalid for production")
    else:
        key = values.get("API_KEY", "").strip()
        keys = (key,) if key else ()
    if not keys:
        raise ValueError("API key configuration is required")
    if production:
        for key in keys:
            if (
                not _PRODUCTION_KEY.fullmatch(key)
                or key.lower() in _PLACEHOLDER_KEYS
                or _production_key_is_repeated(key)
            ):
                raise ValueError("API key configuration is invalid for production")
    return keys


def _api_allowlist(
    values: Mapping[str, str], production: bool
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None:
    raw = values.get("API_IP_ALLOWLIST", "*").strip()
    if raw == "*":
        if production:
            raise ValueError("API_IP_ALLOWLIST must contain explicit networks in production")
        return None
    entries = tuple(entry.strip() for entry in raw.split(","))
    if not all(entries):
        raise ValueError("API_IP_ALLOWLIST must not be empty or contain empty entries")
    try:
        return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)
    except ValueError:
        raise ValueError("API_IP_ALLOWLIST contains an invalid IP network") from None


def _allowed_hosts(values: Mapping[str, str], production: bool) -> tuple[str, ...]:
    default = "" if production else "localhost,127.0.0.1"
    hosts = tuple(host.strip() for host in values.get("ALLOWED_HOSTS", default).split(","))
    if not all(hosts):
        raise ValueError("ALLOWED_HOSTS must not be empty or contain empty entries")
    if production and any("*" in host for host in hosts):
        raise ValueError("ALLOWED_HOSTS must be explicit in production")
    has_dns_hostname = False
    for host in hosts:
        try:
            normalized = parse_host(host)
        except ValueError:
            raise ValueError("ALLOWED_HOSTS contains an invalid host") from None
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            has_dns_hostname = True
    if production and not has_dns_hostname:
        raise ValueError("ALLOWED_HOSTS must contain a DNS hostname in production")
    return hosts


def _forwarded_allow_ips(values: Mapping[str, str], production: bool) -> str:
    raw = values.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1").strip()
    if not raw:
        raise ValueError("FORWARDED_ALLOW_IPS must not be empty")
    entries = tuple(entry.strip() for entry in raw.split(","))
    if not all(entries) or production and "*" in entries:
        raise ValueError("FORWARDED_ALLOW_IPS must contain explicit peers in production")
    try:
        for entry in entries:
            if entry != "*":
                network = ipaddress.ip_network(entry, strict=False)
                if production and not network.is_loopback:
                    raise ValueError
                if production and network.num_addresses > 1:
                    raise ValueError
    except ValueError:
        raise ValueError("FORWARDED_ALLOW_IPS contains an invalid peer network") from None
    return ",".join(entries)


def _uvicorn_bind(values: Mapping[str, str], production: bool) -> tuple[str | None, str | None]:
    default_uds = "/run/capsolve/uvicorn/api.sock" if production else ""
    uds = values.get("UVICORN_UDS", default_uds).strip() or None
    host = values.get("API_HOST", "127.0.0.1").strip() or None
    if production:
        if not uds or not os.path.isabs(uds) or uds.endswith(os.sep):
            raise ValueError("UVICORN_UDS must be an absolute socket path in production")
        if "API_HOST" in values and host:
            raise ValueError("API_HOST must be unset in production; use UVICORN_UDS")
        return None, uds
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        raise ValueError("API_HOST must be a loopback IP address") from None
    if not address.is_loopback:
        raise ValueError("API_HOST must be a loopback IP address")
    if uds:
        raise ValueError("UVICORN_UDS is production-only")
    return host, None


def db_connect_timeout(values: Mapping[str, str] | None = None) -> int:
    return _integer(values if values is not None else os.environ, "DB_CONNECT_TIMEOUT", 3, 1)


def _api_port(values: Mapping[str, str]) -> int:
    if "API_PORT" in values:
        return _integer(values, "API_PORT", 8191, 1, 65535)
    return _integer(values, "PORT", 8191, 1, 65535)


def _socket_mode(values: Mapping[str, str], production: bool) -> int:
    raw = values.get("UVICORN_SOCKET_MODE", "0660").strip()
    if not re.fullmatch(r"0[0-7]{3}", raw):
        raise ValueError("UVICORN_SOCKET_MODE must be a four-digit octal mode")
    mode = int(raw, 8)
    if production and mode != 0o660:
        raise ValueError("UVICORN_SOCKET_MODE must be 0660 in production")
    return mode


def _group_id(values: Mapping[str, str], name: str, production: bool, default: int | None = None) -> int | None:
    raw = values.get(name)
    if raw is None or not raw.strip():
        if production and name == "UVICORN_SOCKET_GID":
            raise ValueError("UVICORN_SOCKET_GID is required in production")
        return default
    if not raw.strip().isdecimal():
        raise ValueError(f"{name} must be a numeric group ID")
    return int(raw)


def load_settings(component: str, values: Mapping[str, str] | None = None) -> Settings:
    if component not in {"api", "worker", "purge"}:
        raise ValueError("unknown settings component")
    source = values if values is not None else os.environ
    if "JOB_MAX_ATTEMPS" in source:
        raise ValueError("Invalid configuration")
    environment = source.get("ENVIRONMENT", "development").strip()
    if environment not in {"development", "production"}:
        raise ValueError("ENVIRONMENT must be development or production")
    production = environment == "production"
    if production and ("JOB_RETENTION_HOURS" not in source or not source["JOB_RETENTION_HOURS"].strip()):
        raise ValueError("JOB_RETENTION_HOURS is required in production")
    runtime_source: Mapping[str, str] = {} if component == "purge" else source
    enable_xvfb = _boolean(runtime_source, "ENABLE_XVFB_VIRTUAL_DISPLAY", False)
    if production and enable_xvfb:
        raise ValueError("ENABLE_XVFB_VIRTUAL_DISPLAY must be false in production")

    api_keys: tuple[str, ...] = ()
    api_ip_allowlist = None
    allowed_hosts: tuple[str, ...] = ()
    api_docs_enabled = _boolean(runtime_source, "API_DOCS_ENABLED", not production)
    api_host: str | None = None
    uvicorn_uds: str | None = None
    forwarded_allow_ips = "127.0.0.1,::1"
    uvicorn_socket_parent_gid = os.getgid()
    uvicorn_socket_gid = None
    if component == "api":
        api_keys = _api_keys(source, production)
        api_ip_allowlist = _api_allowlist(source, production)
        allowed_hosts = _allowed_hosts(source, production)
        forwarded_allow_ips = _forwarded_allow_ips(source, production)
        api_host, uvicorn_uds = _uvicorn_bind(source, production)
        uvicorn_socket_parent_gid = _group_id(source, "UVICORN_SOCKET_PARENT_GID", production, os.getgid())
        uvicorn_socket_gid = _group_id(source, "UVICORN_SOCKET_GID", production)
        if production and api_docs_enabled:
            raise ValueError("API_DOCS_ENABLED must be false in production")

    return Settings(
        environment=environment,
        api_keys=api_keys,
        api_ip_allowlist=api_ip_allowlist,
        allowed_hosts=allowed_hosts,
        api_docs_enabled=api_docs_enabled,
        api_host=api_host,
        api_port=_api_port(runtime_source),
        forwarded_allow_ips=forwarded_allow_ips,
        uvicorn_uds=uvicorn_uds,
        uvicorn_socket_mode=_socket_mode(runtime_source, production),
        uvicorn_socket_parent_gid=uvicorn_socket_parent_gid,
        uvicorn_socket_gid=uvicorn_socket_gid,
        job_queue_capacity=_integer(runtime_source, "JOB_QUEUE_CAPACITY", 100, 1),
        job_queue_retry_after_seconds=_integer(runtime_source, "JOB_QUEUE_RETRY_AFTER_SECONDS", 60, 1),
        job_batch_limit=_integer(runtime_source, "JOB_BATCH_LIMIT", 5, 1),
        job_max_attempts=_integer(runtime_source, "JOB_MAX_ATTEMPTS", 3, 1),
        job_reset_stale_minutes=_integer(runtime_source, "JOB_RESET_STALE_MINUTES", 30, 0),
        job_retention_hours=_integer(source, "JOB_RETENTION_HOURS", 24, 1, MAX_JOB_RETENTION_HOURS),
        purge_batch_limit=_integer(source, "PURGE_BATCH_LIMIT", 1000, 1, MAX_PURGE_BATCH_LIMIT),
        sync_queue_max_waiting=_integer(runtime_source, "SYNC_QUEUE_MAX_WAITING", 0, 0),
        max_workers=_integer(runtime_source, "MAX_WORKERS", 1, 1),
        global_chrome_slots=_integer(runtime_source, "GLOBAL_CHROME_SLOTS", 1, 1, _MAX_GLOBAL_CHROME_SLOTS),
        db_connect_timeout=db_connect_timeout(source),
        db_port=_integer(source, "DB_PORT", 5432, 1, 65535),
        solver_timeout=_integer(runtime_source, "SOLVER_TIMEOUT", 45, 1),
        local_post_timeout=_integer(runtime_source, "LOCAL_POST_TIMEOUT", 30, 1),
        budi95_config_cache_seconds=_integer(runtime_source, "BUDI95_CONFIG_CACHE_SECONDS", 1800, 0),
        budi95_config_fetch_timeout=_integer(runtime_source, "BUDI95_CONFIG_FETCH_TIMEOUT", 10, 1),
        budi95_auto_resolve=_boolean(runtime_source, "BUDI95_AUTO_RESOLVE", True),
        budi95_force_env_config=_boolean(runtime_source, "BUDI95_FORCE_ENV_CONFIG", False),
        enable_xvfb_virtual_display=enable_xvfb,
    )
