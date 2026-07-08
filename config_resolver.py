from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
import time
import urllib.parse
import urllib.request


DEFAULT_CONFIG_URL = "https://www.budirakyat.gov.my/eligibility-check"
DEFAULT_CACHE_FILE = "/tmp/capsolve_budi95_config.json"
DEFAULT_CACHE_SECONDS = 1800
DEFAULT_FETCH_TIMEOUT = 10
DEFAULT_MAX_JS_FETCHES = 30


@dataclass(frozen=True)
class Budi95Config:
    local_post_url: str
    turnstile_siteurl: str
    turnstile_sitekey: str
    source: str


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def resolve_budi95_config(force_refresh: bool = False) -> Budi95Config:
    from solver import load_dotenv

    load_dotenv()

    if env_bool("BUDI95_FORCE_ENV_CONFIG", False):
        return resolve_from_env()
    if not env_bool("BUDI95_AUTO_RESOLVE", True):
        return resolve_from_env()

    if not force_refresh:
        cached = read_cache()
        if cached:
            return cached

    website_error = None
    try:
        config = resolve_from_website()
        write_cache(config)
        return config
    except Exception as exc:
        website_error = exc

    try:
        return resolve_from_env()
    except Exception as env_error:
        raise RuntimeError(
            "Failed to resolve BUDI95 config from website "
            f"({website_error}) and env fallback ({env_error})"
        ) from env_error


def resolve_from_env() -> Budi95Config:
    values = {
        "local_post_url": os.environ.get("LOCAL_POST_URL", "").strip(),
        "turnstile_siteurl": os.environ.get("TURNSTILE_SITEURL", "").strip(),
        "turnstile_sitekey": os.environ.get("TURNSTILE_SITEKEY", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        env_names = {
            "local_post_url": "LOCAL_POST_URL",
            "turnstile_siteurl": "TURNSTILE_SITEURL",
            "turnstile_sitekey": "TURNSTILE_SITEKEY",
        }
        raise ValueError("Missing required env vars: " + ", ".join(env_names[name] for name in missing))
    return Budi95Config(source="env", **values)


def resolve_from_website() -> Budi95Config:
    config_url = os.environ.get("BUDI95_CONFIG_URL", DEFAULT_CONFIG_URL).strip() or DEFAULT_CONFIG_URL
    timeout = env_int("BUDI95_CONFIG_FETCH_TIMEOUT", DEFAULT_FETCH_TIMEOUT)
    html = _fetch_text(config_url, timeout)
    texts = [html]
    max_fetches = DEFAULT_MAX_JS_FETCHES
    seen = set()
    pending = _local_script_urls(html, config_url)

    while pending and len(seen) < max_fetches:
        script_url = pending.pop(0)
        if script_url in seen:
            continue
        seen.add(script_url)
        try:
            js = _fetch_text(script_url, timeout)
        except Exception:
            continue
        texts.append(js)
        for chunk_url in _local_js_urls(js, script_url):
            if chunk_url not in seen and chunk_url not in pending:
                pending.append(chunk_url)

    return parse_config_from_js("\n".join(texts), config_url)


def parse_config_from_js(js_text: str, siteurl: str) -> Budi95Config:
    api_base = _match_required(r"https://[^\"'`\s]+/api/", js_text, "API base URL")
    sitekey = _match_sitekey(js_text)

    if "pub_getcountinfo" not in js_text:
        raise ValueError("Could not find pub_getcountinfo endpoint marker")
    version = _match_required(r"\bv1\b", js_text, "API version")

    return Budi95Config(
        local_post_url=f"{api_base}portalsvc/{version}/pub_getcountinfo",
        turnstile_siteurl=_default_siteurl(siteurl),
        turnstile_sitekey=sitekey,
        source="website",
    )


def read_cache() -> Budi95Config | None:
    path = os.environ.get("BUDI95_CONFIG_CACHE_FILE", DEFAULT_CACHE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    cached_at = data.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        return None
    if time.time() - cached_at > env_int("BUDI95_CONFIG_CACHE_SECONDS", DEFAULT_CACHE_SECONDS):
        return None

    try:
        config = Budi95Config(
            local_post_url=str(data["local_post_url"]).strip(),
            turnstile_siteurl=str(data["turnstile_siteurl"]).strip(),
            turnstile_sitekey=str(data["turnstile_sitekey"]).strip(),
            source="cache",
        )
    except KeyError:
        return None

    if not config.local_post_url or not config.turnstile_siteurl or not config.turnstile_sitekey:
        return None
    return config


def write_cache(config: Budi95Config) -> None:
    path = os.environ.get("BUDI95_CONFIG_CACHE_FILE", DEFAULT_CACHE_FILE)
    data = asdict(config) | {"cached_at": time.time()}
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file)


def _fetch_text(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "CapSolve config resolver"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _local_script_urls(html: str, page_url: str) -> list[str]:
    urls = []
    for match in re.finditer(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html, re.I):
        url = urllib.parse.urljoin(page_url, match.group(1))
        if _is_local_js_url(url, page_url):
            urls.append(url)
    return sorted(dict.fromkeys(urls), key=lambda url: ("main-" not in url, url))


def _local_js_urls(js_text: str, base_url: str) -> list[str]:
    urls = []
    for match in re.finditer(r"[\"'`]([^\"'`]+\.js(?:[?#][^\"'`]*)?)[\"'`]", js_text):
        url = urllib.parse.urljoin(base_url, match.group(1))
        if _is_local_js_url(url, base_url):
            urls.append(url)
    return sorted(dict.fromkeys(urls))


def _is_local_js_url(url: str, base_url: str) -> bool:
    base = urllib.parse.urlparse(base_url)
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc == base.netloc and parsed.path.endswith(".js")


def _match_sitekey(text: str) -> str:
    match = re.search(r"CLOUDFLARE_TURNSTILE_SITEID[^\"'`]+[\"'`](0x[0-9A-Za-z_-]+)[\"'`]", text)
    if match:
        return match.group(1)
    return _match_required(r"0x[0-9A-Za-z_-]+", text, "Turnstile sitekey")


def _match_required(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"Could not find {label}")
    return match.group(0)


def _default_siteurl(config_url: str) -> str:
    parsed = urllib.parse.urlsplit(config_url)
    if parsed.query:
        return config_url
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "type=individual", parsed.fragment))
