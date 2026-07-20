import hmac
import ipaddress
import json
import os
import platform
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import chrome_slots
from config_resolver import resolve_budi95_config
import database
import job_repository
from settings import Settings, load_settings, parse_host
from solver import load_dotenv, post_local_result, solve


API_HOST = "0.0.0.0"
API_PORT = 8191
MAX_WORKERS = 1
API_KEYS: tuple[str, ...] = ()
SOLVER_TIMEOUT = 45
LOCAL_POST_TIMEOUT = 30
UVICORN_ACCESS_LOG = False

_settings: Settings | None = None
_active_count = 0
_queued_count = 0
_count_lock = threading.Condition()
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], tuple[int, int]] = {}
_rate_limit_max_buckets = 10_000
_slot_wait_interval = 0.05
_slot_acquirer = chrome_slots.try_acquire
_xvfb_proc: Optional[subprocess.Popen] = None
_xvfb_display: str | None = None


def _ensure_display(settings: Settings) -> Optional[subprocess.Popen]:
    global _xvfb_proc, _xvfb_display
    if (
        platform.system() != "Linux"
        or settings.environment != "development"
        or not settings.enable_xvfb_virtual_display
    ):
        return None
    if _xvfb_proc is not None:
        if _xvfb_proc.poll() is None:
            if not os.environ.get("DISPLAY") and _xvfb_display is not None:
                os.environ["DISPLAY"] = _xvfb_display
            return _xvfb_proc
        try:
            _xvfb_proc.wait(timeout=5)
        except BaseException:
            raise RuntimeError("Xvfb cleanup failed") from None
        _xvfb_proc = None
        _xvfb_display = None
    if os.environ.get("DISPLAY"):
        return None
    display = os.environ.get("XVFB_DISPLAY", ":99")
    _xvfb_proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if _xvfb_proc.poll() is not None:
        try:
            _xvfb_proc.wait(timeout=5)
        except BaseException:
            raise RuntimeError("Xvfb cleanup failed") from None
        _xvfb_proc = None
        raise RuntimeError("Xvfb failed to start")
    os.environ["DISPLAY"] = display
    _xvfb_display = display
    _event("xvfb_started", display=display)
    return _xvfb_proc


def _stop_display() -> None:
    global _xvfb_proc, _xvfb_display
    process, display = _xvfb_proc, _xvfb_display
    if process is None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except BaseException:
        if display is not None and os.environ.get("DISPLAY") == display:
            os.environ.pop("DISPLAY", None)
        raise RuntimeError("Xvfb cleanup failed") from None
    _xvfb_proc = None
    _xvfb_display = None
    if display is not None and os.environ.get("DISPLAY") == display:
        os.environ.pop("DISPLAY", None)


def _event(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")))


def _configure() -> Settings:
    global API_HOST, API_PORT, MAX_WORKERS, API_KEYS, SOLVER_TIMEOUT, LOCAL_POST_TIMEOUT, _settings
    load_dotenv()
    _settings = load_settings("api")
    API_HOST = _settings.api_host
    API_PORT = _settings.api_port
    MAX_WORKERS = _settings.max_workers
    API_KEYS = _settings.api_keys
    SOLVER_TIMEOUT = _settings.solver_timeout
    LOCAL_POST_TIMEOUT = _settings.local_post_timeout
    return _settings


def _construction_docs_enabled() -> bool:
    if os.environ.get("ENVIRONMENT", "development").strip() == "production":
        return False
    return os.environ.get("API_DOCS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


async def _plain_response(send, status_code: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status_code, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def _host(headers: list[tuple[bytes, bytes]]) -> str | None:
    values = [value for name, value in headers if name.lower() == b"host"]
    if len(values) != 1:
        return None
    try:
        return parse_host(values[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


def _path_security(scope) -> tuple[bool, bool]:
    path = scope.get("path", "")
    raw = scope.get("raw_path", path.encode("utf-8", "surrogatepass"))
    try:
        raw_text = raw.decode("ascii")
        decoded = urllib.parse.unquote(raw_text, errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        raw_text, decoded = "", path
    lowered = decoded.lower().replace("\\", "/")
    segments = [segment for segment in lowered.split("/") if segment not in ("", ".")]
    collapsed = "/" + "/".join(segments)
    api_like = lowered.startswith("/api") or collapsed.startswith("/api")
    if path in {"/api/health", "/api/ready"} and raw_text == path:
        return False, True
    business = api_like and (
        collapsed.startswith("/api/solve")
        or collapsed.startswith("/api/budi95")
        or lowered != path
        or any(token in lowered for token in ("//", "/./", "/../", "%"))
    )
    canonical = (
        raw_text == path
        and path == path.lower()
        and "\\" not in path
        and "//" not in path
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
        and all(segment not in (".", "..") for segment in path.split("/"))
    )
    return business, canonical


def _api_key_valid(headers: list[tuple[bytes, bytes]]) -> bool:
    values = [value for name, value in headers if name.lower() == b"x-api-key"]
    return len(values) == 1 and any(
        hmac.compare_digest(values[0], key.encode("ascii")) for key in API_KEYS if key.isascii()
    )


def _rate_limit_kind(method: str, path: str) -> str | None:
    if method == "POST" and path in {"/api/budi95", "/api/budi95/", "/api/solve/"}:
        return "submit"
    result_prefix = "/api/budi95/result/"
    if method == "GET" and (
        path == "/api/budi95/config"
        or path == "/api/budi95/queue/status"
        or path.startswith(result_prefix) and "/" not in path[len(result_prefix) :] and len(path) > len(result_prefix)
    ):
        return "read"
    return None


def _rate_limit(client: str, kind: str, now: float | None = None) -> int | None:
    if _settings is None:
        return None
    try:
        client = str(ipaddress.ip_address(client))
    except ValueError:
        return 60
    limit = (
        getattr(_settings, "budi95_submit_rate_limit_per_minute", 0)
        if kind == "submit"
        else getattr(_settings, "budi95_read_rate_limit_per_minute", 0)
    )
    if limit == 0:
        return None
    timestamp = time.time() if now is None else now
    window = int(timestamp // 60)
    key = (client, kind)
    with _rate_limit_lock:
        stale = [bucket_key for bucket_key, (bucket_window, _) in _rate_limit_buckets.items() if bucket_window < window]
        for bucket_key in stale:
            _rate_limit_buckets.pop(bucket_key, None)
        if key not in _rate_limit_buckets and len(_rate_limit_buckets) >= _rate_limit_max_buckets:
            return max(1, 60 - int(timestamp % 60))
        bucket_window, count = _rate_limit_buckets.get(key, (window, 0))
        if bucket_window != window:
            bucket_window, count = window, 0
        if count >= limit:
            return max(1, 60 - int(timestamp % 60))
        _rate_limit_buckets[key] = (bucket_window, count + 1)
    return None


class BusinessAccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        business, canonical = _path_security(scope)
        if not business:
            return await self.app(scope, receive, send)
        networks = getattr(_settings, "api_ip_allowlist", None)
        try:
            address = ipaddress.ip_address(scope["client"][0])
        except (KeyError, TypeError, ValueError):
            return await _plain_response(send, 403, b'{"detail":"Forbidden"}')
        if networks is not None and not any(address in network for network in networks):
            return await _plain_response(send, 403, b'{"detail":"Forbidden"}')
        if not API_KEYS:
            return await _plain_response(send, 500, b'{"detail":"API key is not configured on server."}')
        key_headers = [value for name, value in scope["headers"] if name.lower() == b"x-api-key"]
        if not key_headers:
            return await _plain_response(send, 401, b'{"detail":"Missing x-api-key header."}')
        if not _api_key_valid(scope["headers"]):
            return await _plain_response(send, 401, b'{"detail":"Invalid API key."}')
        if not canonical:
            return await _plain_response(send, 400, b'{"detail":"Invalid request"}')
        kind = _rate_limit_kind(scope["method"], scope["path"])
        retry_after = _rate_limit(str(address), kind) if kind else None
        if retry_after is not None:
            await send({
                "type": "http.response.start",
                "status": status.HTTP_429_TOO_MANY_REQUESTS,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ],
            })
            return await send({"type": "http.response.body", "body": b'{"detail":"Rate limit exceeded"}'})
        return await self.app(scope, receive, send)


class UdsPeerMiddleware:
    def __init__(self, app, peer_ip: str):
        self.app = app
        self.peer_ip = peer_ip

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan" and scope.get("client") is None:
            scope = {**scope, "client": (self.peer_ip, 0)}
        return await self.app(scope, receive, send)


class FixedTrustedHostMiddleware(TrustedHostMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        host = _host(scope["headers"])
        allowed = {parse_host(value) for value in self.allowed_hosts}
        if host is None or host not in allowed:
            return await _plain_response(send, 400, b"Invalid host header")
        return await self.app(scope, receive, send)


class LifespanSecurityMiddleware:
    def __init__(self, app, allowed_hosts=None, forwarded_allow_ips=None, uds_peer_ip=None):
        self.app = app
        hosts = allowed_hosts or ("localhost", "127.0.0.1", "testserver")
        secured = BusinessAccessMiddleware(app)
        secured = ProxyHeadersMiddleware(secured, trusted_hosts=forwarded_allow_ips) if forwarded_allow_ips else secured
        if uds_peer_ip:
            secured = UdsPeerMiddleware(secured, uds_peer_ip)
        self.secured = FixedTrustedHostMiddleware(secured, allowed_hosts=list(hosts))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        return await self.secured(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _xvfb_proc
    settings = _configure()
    docs_registered = app.openapi_url is not None
    if docs_registered != settings.api_docs_enabled:
        raise RuntimeError("API docs setting changed after application construction")
    app.user_middleware[0].kwargs.update(
        allowed_hosts=settings.allowed_hosts,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        uds_peer_ip="127.0.0.1" if settings.uvicorn_uds else None,
    )
    app.middleware_stack = app.build_middleware_stack()
    _xvfb_proc = _ensure_display(settings)
    bind = settings.uvicorn_uds or f"http://{settings.api_host}:{settings.api_port}"
    _event("api_started", bind=bind, max_workers=MAX_WORKERS)
    try:
        yield
    finally:
        _stop_display()


api_router = APIRouter(prefix="/api")
api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if all(error.get("type") == "missing" and error.get("input") is None for error in errors):
        return JSONResponse(status_code=422, content={"detail": errors})
    return JSONResponse(status_code=422, content={"detail": "Invalid request"})


class Budi95SubmitRequest(BaseModel):
    nric: str


def verify_client_ip(request: Request) -> None:
    networks = getattr(_settings, "api_ip_allowlist", None)
    if networks is None:
        return
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from None
    if not any(address in network for network in networks):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def verify_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    if not API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key is not configured on server.",
        )
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing x-api-key header.",
        )
    try:
        candidate = x_api_key.encode("ascii")
    except UnicodeEncodeError:
        candidate = b""
    if not candidate or not any(
        hmac.compare_digest(candidate, configured_key.encode("ascii"))
        for configured_key in API_KEYS
        if configured_key.isascii()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )


def _mask_sitekey(sitekey: str) -> str:
    return f"{sitekey[:7]}...{sitekey[-4:]}" if len(sitekey) > 11 else "..."


def _is_config_error(exc: Exception) -> bool:
    if isinstance(exc, (urllib.error.URLError, socket.gaierror)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.gaierror, TimeoutError)):
        return True
    text = str(exc).lower()
    return "connection refused" in text or "timed out" in text


def _solve_and_post(nric: str, timeout: int, post_timeout: int) -> dict:
    config = resolve_budi95_config()
    token = solve(config.turnstile_sitekey, config.turnstile_siteurl, timeout=timeout)
    try:
        return post_local_result(config.local_post_url, nric, token, timeout=post_timeout)
    except Exception as exc:
        if not _is_config_error(exc):
            raise
        config = resolve_budi95_config(force_refresh=True)
        token = solve(config.turnstile_sitekey, config.turnstile_siteurl, timeout=timeout)
        return post_local_result(config.local_post_url, nric, token, timeout=post_timeout)


def _acquire_sync_slot(acquire_slot=None):
    global _active_count, _queued_count
    settings = _settings
    acquire_slot = acquire_slot or _slot_acquirer
    if settings is None:
        raise chrome_slots.ChromeSlotError("Chrome slot acquisition failed")
    waiting_limit = settings.sync_queue_max_waiting
    global_slots = settings.global_chrome_slots
    retry_after = settings.job_queue_retry_after_seconds
    local_limit = min(MAX_WORKERS, global_slots)
    deadline = time.monotonic() + retry_after

    with _count_lock:
        if _active_count + _queued_count >= local_limit + waiting_limit:
            return None
        _queued_count += 1

    queued = True
    active = False
    try:
        while True:
            with _count_lock:
                while _active_count >= local_limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    _count_lock.wait(remaining)
                _queued_count -= 1
                queued = False
                _active_count += 1
                active = True

            slot = acquire_slot(global_slots)
            if slot is not None:
                active = False
                return slot

            with _count_lock:
                _active_count -= 1
                active = False
                if time.monotonic() >= deadline or _queued_count >= waiting_limit:
                    _count_lock.notify()
                    return None
                _queued_count += 1
                queued = True
                _count_lock.notify()
                _count_lock.wait(min(_slot_wait_interval, max(0, deadline - time.monotonic())))
    finally:
        with _count_lock:
            if queued:
                _queued_count -= 1
            if active:
                _active_count -= 1
            if queued or active:
                _count_lock.notify()


def _release_sync_slot(slot) -> None:
    global _active_count
    try:
        slot.release()
    finally:
        with _count_lock:
            _active_count -= 1
            _count_lock.notify()


@api_router.get(
    "/health",
    summary="API process health",
    description=(
        "Shallow liveness check for the API process. The workers, active, and queued fields "
        "describe only the legacy synchronous POST /api/solve/ flow in this API process; "
        "they are not BUDI95 PostgreSQL queue metrics. This endpoint does not check PostgreSQL."
    ),
)
def health():
    with _count_lock:
        return {
            "status": "ok",
            "workers": MAX_WORKERS,
            "active": _active_count,
            "queued": _queued_count,
        }


@api_router.get(
    "/ready",
    summary="PostgreSQL readiness",
    description=(
        "Checks whether PostgreSQL accepts a lightweight SELECT 1 query. Returns 200 when the "
        "database is ready and 503 when unavailable. It does not check Chrome, BUDI95 upstream, "
        "worker activity, or queue capacity."
    ),
)
def ready():
    timeout = _settings.db_connect_timeout if _settings else 3
    try:
        usable = database.is_ready(timeout)
    except BaseException:
        usable = False
    _event("readiness", ready=usable)
    if not usable:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "unavailable"})
    return {"status": "ready"}


@api_router.post(
    "/solve/",
    summary="Legacy synchronous solve",
    description=(
        "Legacy synchronous flow that solves and posts within one request. Health endpoint "
        "workers/active/queued counters refer only to this route. New integrations should use "
        "POST /api/budi95 and poll GET /api/budi95/result/{ulid}."
    ),
    deprecated=True,
)
def solve_endpoint(
    nric: str = Query(..., min_length=1),
    timeout: int | None = Query(None, ge=1),
    post_timeout: int | None = Query(None, ge=1),
    _: None = Depends(verify_client_ip),
    __: None = Depends(verify_api_key),
):
    if _settings is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Solver is unavailable")
    timeout = timeout if timeout is not None else _settings.solver_timeout
    post_timeout = post_timeout if post_timeout is not None else _settings.local_post_timeout
    retry_after = _settings.job_queue_retry_after_seconds
    try:
        slot = _acquire_sync_slot()
    except BaseException:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Solver is unavailable",
            headers={"Retry-After": str(retry_after)},
        ) from None
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Solver is busy",
            headers={"Retry-After": str(retry_after)},
        )

    started = time.monotonic()
    try:
        result = _solve_and_post(nric, timeout, post_timeout)
        _event("sync_solve_complete", outcome="success", duration_seconds=round(time.monotonic() - started, 3))
        return result
    except Exception:
        _event("sync_solve_complete", outcome="failure", error_code="solve_failed", duration_seconds=round(time.monotonic() - started, 3))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error_code": "solve_failed", "message": "Unable to process subsidy"},
        )
    finally:
        _release_sync_slot(slot)


@api_router.get("/budi95/config", include_in_schema=False)
def get_budi95_config(
    force_refresh: bool = Query(False),
    _: None = Depends(verify_client_ip),
    __: None = Depends(verify_api_key),
):
    try:
        config = resolve_budi95_config(force_refresh=force_refresh)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BUDI95 configuration is unavailable",
        ) from None
    return {
        "local_post_url": config.local_post_url,
        "turnstile_siteurl": config.turnstile_siteurl,
        "turnstile_sitekey": _mask_sitekey(config.turnstile_sitekey),
        "source": config.source,
    }


@api_router.post("/budi95", status_code=status.HTTP_200_OK)
@api_router.post("/budi95/", status_code=status.HTTP_200_OK, include_in_schema=False)
def submit_budi95_job(
    request: Budi95SubmitRequest,
    _: None = Depends(verify_client_ip),
    __: None = Depends(verify_api_key),
):
    nric = request.nric.strip()
    if not nric:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="nric is required")
    if len(nric) > 32:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="nric must be at most 32 characters")

    retry_after = _settings.job_queue_retry_after_seconds if _settings else 60
    try:
        job = job_repository.create_job(
            nric,
            _settings.job_max_attempts if _settings else None,
            _settings.job_queue_capacity if _settings else None,
        )
    except job_repository.QueueFullError:
        _event("async_submit", outcome="rejected", reason="queue_full")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Job queue is full",
            headers={"Retry-After": str(retry_after)},
        ) from None
    except Exception:
        _event("async_submit", outcome="rejected", reason="queue_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job queue is unavailable",
            headers={"Retry-After": str(retry_after)},
        ) from None
    _event("async_submit", outcome="accepted")
    return job_repository.public_submit_response(job)


def _get_budi95_result(ulid: str) -> dict:
    try:
        job = job_repository.get_job_by_ulid(ulid)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job result is unavailable",
        ) from None
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job_repository.public_result_response(job)


@api_router.get("/budi95/result/{ulid}")
def get_budi95_result(
    ulid: str,
    _: None = Depends(verify_client_ip),
    __: None = Depends(verify_api_key),
):
    return _get_budi95_result(ulid)


@api_router.get(
    "/budi95/queue/status",
    summary="BUDI95 asynchronous queue status",
    description=(
        "Reports PostgreSQL-backed BUDI95 queue capacity, pending and processing jobs, available "
        "slots, oldest pending age, stale processing count, and configured Chrome concurrency. "
        "Use this endpoint—not /api/health—to monitor asynchronous BUDI95 work."
    ),
)
def get_budi95_queue_status(
    _: None = Depends(verify_client_ip),
    __: None = Depends(verify_api_key),
):
    if _settings is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job queue is unavailable")
    try:
        metrics = job_repository.queue_metrics(_settings.job_reset_stale_minutes)
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Job queue is unavailable") from None
    depth = int(metrics["queue_depth"])
    capacity = _settings.job_queue_capacity
    return {
        "capacity": capacity,
        "depth": depth,
        "pending": int(metrics["pending_count"]),
        "processing": int(metrics["processing_count"]),
        "available": max(capacity - depth, 0),
        "oldest_pending_age_seconds": metrics["oldest_pending_age_seconds"],
        "stale_processing": int(metrics["stale_processing_count"]),
        "worker": {
            "model": "scheduled",
            "processing": int(metrics["processing_count"]),
            "max_concurrent_solves": _settings.global_chrome_slots,
        },
    }


def create_app(
    *,
    docs_enabled: bool | None = None,
    allowed_hosts: tuple[str, ...] | None = None,
    forwarded_allow_ips: str | None = None,
    uds_peer_ip: str | None = None,
) -> FastAPI:
    enabled = _construction_docs_enabled() if docs_enabled is None else docs_enabled
    application = FastAPI(
        title="EzSolver API",
        version="0.1.0",
        description="REST API for BUDI95 quota lookup through Turnstile solving.",
        lifespan=lifespan,
        docs_url="/docs" if enabled else None,
        redoc_url="/redoc" if enabled else None,
        openapi_url="/openapi.json" if enabled else None,
    )
    application.add_exception_handler(RequestValidationError, request_validation_error)
    application.add_middleware(
        LifespanSecurityMiddleware,
        allowed_hosts=allowed_hosts,
        forwarded_allow_ips=forwarded_allow_ips,
        uds_peer_ip=uds_peer_ip,
    )
    application.include_router(api_router)
    return application


load_dotenv()
app = create_app()


def _open_secure_parent(path: str, parent_gid: int) -> tuple[int, str]:
    if not os.path.isabs(path) or os.path.basename(path) in {"", ".", ".."}:
        raise RuntimeError("Unsafe Uvicorn socket path")
    parts = os.path.dirname(path).split(os.sep)[1:]
    directory_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            info = os.fstat(directory_fd)
            final = index == len(parts) - 1
            if info.st_uid not in {0, os.getuid()}:
                raise RuntimeError("Unsafe Uvicorn socket directory")
            if final:
                if stat.S_IMODE(info.st_mode) not in {0o750, 0o770} or info.st_gid != parent_gid:
                    raise RuntimeError("Unsafe Uvicorn socket directory")
            elif stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError("Unsafe Uvicorn socket directory")
        return directory_fd, os.path.basename(path)
    except OSError:
        os.close(directory_fd)
        raise RuntimeError("Unsafe Uvicorn socket directory") from None
    except BaseException:
        os.close(directory_fd)
        raise


def _path_identity(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISSOCK(info.st_mode) else None


def _secure_uds_listener(path: str, mode: int, parent_gid: int, socket_gid: int | None):
    directory_fd, name = _open_secure_parent(path, parent_gid)
    listener = socket.socket(socket.AF_UNIX)
    object_fd = None
    identity = None
    try:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise RuntimeError("Unsafe Uvicorn socket path")
            os.unlink(name, dir_fd=directory_fd)
        listener.bind(f"/proc/self/fd/{directory_fd}/{name}")
        object_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd)
        info = os.fstat(object_fd)
        identity = (info.st_dev, info.st_ino)
        if socket_gid is not None:
            os.chown(f"/proc/self/fd/{object_fd}", -1, socket_gid)
        os.chmod(f"/proc/self/fd/{object_fd}", mode)
        info = os.fstat(object_fd)
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != mode
            or socket_gid is not None and info.st_gid != socket_gid
            or _path_identity(directory_fd, name) != identity
        ):
            raise RuntimeError("Uvicorn socket permission setup failed")
        listener.listen(2048)
        return listener, (directory_fd, object_fd, name, *identity)
    except BaseException:
        listener.close()
        if identity is not None and _path_identity(directory_fd, name) == identity:
            os.unlink(name, dir_fd=directory_fd)
        if object_fd is not None:
            os.close(object_fd)
        os.close(directory_fd)
        raise


def _cleanup_uds(listener: socket.socket, path: str, identity) -> None:
    listener.close()
    directory_fd, object_fd, name, device, inode = identity
    try:
        if _path_identity(directory_fd, name) == (device, inode):
            os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(object_fd)
        os.close(directory_fd)


def run() -> None:
    settings = _configure()
    forwarded_allow_ips = getattr(settings, "forwarded_allow_ips", None)
    if forwarded_allow_ips is None:  # Backward-compatible test/settings-double path.
        uvicorn.run("service:app", host=settings.api_host, port=settings.api_port, access_log=UVICORN_ACCESS_LOG)
        return
    options = {
        "access_log": UVICORN_ACCESS_LOG,
        "proxy_headers": True,
        "forwarded_allow_ips": forwarded_allow_ips,
    }
    if settings.uvicorn_uds:
        listener, identity = _secure_uds_listener(
            settings.uvicorn_uds,
            settings.uvicorn_socket_mode,
            settings.uvicorn_socket_parent_gid,
            settings.uvicorn_socket_gid,
        )
        try:
            uvicorn.run("service:app", fd=listener.fileno(), **options)
        finally:
            _cleanup_uds(listener, settings.uvicorn_uds, identity)
    else:
        uvicorn.run("service:app", host=settings.api_host, port=settings.api_port, **options)


if __name__ == "__main__":
    run()
