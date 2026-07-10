import hmac
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.error
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from config_resolver import resolve_budi95_config
import job_repository
from solver import load_dotenv, post_local_result, solve


load_dotenv()

API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", os.environ.get("PORT", 8191)))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 4))
API_KEYS = tuple(
    key.strip()
    for key in os.environ.get("API_KEYS", os.environ.get("API_KEY", "")).split(",")
    if key.strip()
)
TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "").strip()
TURNSTILE_SITEURL = os.environ.get("TURNSTILE_SITEURL", "").strip()
LOCAL_POST_URL = os.environ.get("LOCAL_POST_URL", "").strip()
SOLVER_TIMEOUT = int(os.environ.get("SOLVER_TIMEOUT", 45))
LOCAL_POST_TIMEOUT = int(os.environ.get("LOCAL_POST_TIMEOUT", 30))

_worker_sem = threading.Semaphore(MAX_WORKERS)
_active_count = 0
_queued_count = 0
_count_lock = threading.Lock()
_xvfb_proc: Optional[subprocess.Popen] = None


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _ensure_display() -> Optional[subprocess.Popen]:
    if platform.system() != "Linux":
        return None
    if os.environ.get("DISPLAY") and not _env_truthy("ENABLE_XVFB_VIRTUAL_DISPLAY"):
        return None
    display = os.environ.get("XVFB_DISPLAY", ":99")
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    time.sleep(0.5)
    print(f"[service] started Xvfb on {display}")
    return xvfb


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _xvfb_proc
    _xvfb_proc = _ensure_display()
    print(f"[service] FastAPI solver running on http://{API_HOST}:{API_PORT}")
    print(f"[service] worker pool: {MAX_WORKERS} concurrent Chrome instances")
    try:
        yield
    finally:
        if _xvfb_proc:
            _xvfb_proc.terminate()


app = FastAPI(
    title="EzSolver API",
    version="0.1.0",
    description="REST API for BUDI95 quota lookup through Turnstile solving.",
    lifespan=lifespan,
)
api_router = APIRouter(prefix="/api")
api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


class Budi95SubmitRequest(BaseModel):
    nric: str


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
    if not any(hmac.compare_digest(x_api_key, configured_key) for configured_key in API_KEYS):
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


@api_router.get("/health")
def health():
    with _count_lock:
        return {
            "status": "ok",
            "workers": MAX_WORKERS,
            "active": _active_count,
            "queued": _queued_count,
        }


@api_router.post("/solve/")
def solve_endpoint(
    nric: str = Query(..., min_length=1),
    timeout: int = Query(SOLVER_TIMEOUT, ge=1),
    post_timeout: int = Query(LOCAL_POST_TIMEOUT, ge=1),
    _: None = Depends(verify_api_key),
):
    global _active_count, _queued_count
    log_nric = f"{nric[:1]}***{nric[-1:]}"

    with _count_lock:
        _queued_count += 1

    print(f"[service] queued — nric={log_nric!r} (active={_active_count}/{MAX_WORKERS} queued={_queued_count})")
    _worker_sem.acquire()

    with _count_lock:
        _queued_count -= 1
        _active_count += 1

    started = time.time()
    try:
        print(f"[service] solving nric={log_nric!r} (active={_active_count}/{MAX_WORKERS})")
        result = _solve_and_post(nric, timeout, post_timeout)
        elapsed = round(time.time() - started, 2)
        print(f"[service] solved in {elapsed}s")
        return result
    except Exception as exc:
        elapsed = round(time.time() - started, 2)
        print(f"[service] error after {elapsed}s: {str(exc).replace(nric, log_nric)}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        with _count_lock:
            _active_count -= 1
        _worker_sem.release()


@api_router.get("/budi95/config", include_in_schema=False)
def get_budi95_config(
    force_refresh: bool = Query(False),
    _: None = Depends(verify_api_key),
):
    config = resolve_budi95_config(force_refresh=force_refresh)
    return {
        "local_post_url": config.local_post_url,
        "turnstile_siteurl": config.turnstile_siteurl,
        "turnstile_sitekey": _mask_sitekey(config.turnstile_sitekey),
        "source": config.source,
    }


@api_router.post("/budi95", status_code=status.HTTP_200_OK)
def submit_budi95_job(
    request: Budi95SubmitRequest,
    _: None = Depends(verify_api_key),
):
    nric = request.nric.strip()
    if not nric:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="nric is required")

    try:
        job = job_repository.create_job(nric)
    except Exception:
        return {"status": False, "id_no": nric, "ulid": None, "message": "Unable to create"}
    return job_repository.public_submit_response(job)


def _get_budi95_result(ulid: str) -> dict:
    job = job_repository.get_job_by_ulid(ulid)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job_repository.public_result_response(job)


@api_router.get("/budi95/result/{ulid}")
def get_budi95_result(
    ulid: str,
    _: None = Depends(verify_api_key),
):
    return _get_budi95_result(ulid)


app.include_router(api_router)


def run() -> None:
    uvicorn.run("service:app", host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    run()
