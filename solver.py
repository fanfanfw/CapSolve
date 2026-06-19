import asyncio
import json
import os
import platform
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional
"""
MADE BY ISMOILOFF. GOOD LUCK HAVE FUN, THIS IS JUST PROJECT, USE IT ON UR OWN RISKS!

"""
import nodriver as uc


def _find_chrome() -> str:
    """Return the Chrome executable path, checking common locations per OS."""
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]

    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "Chrome not found in default locations. "
        "Set the CHROME_PATH environment variable to your Chrome executable."
    )


def _get_profile_dir() -> str:
    """Return a persistent Chrome profile directory for the current OS."""
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
        return os.path.join(base, "ts_profile")
    return "/tmp/ts_profile"


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _start_xvfb_if_needed() -> Optional[subprocess.Popen]:
    """On Linux, start a hidden virtual display when needed or enabled."""
    if platform.system() != "Linux":
        return None
    if os.environ.get("DISPLAY") and not _env_truthy("ENABLE_XVFB_VIRTUAL_DISPLAY"):
        return None
    display = os.environ.get("XVFB_DISPLAY", ":99")
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    time.sleep(0.5)
    return proc


def _get_browser_args() -> list[str]:
    args = [
        "--ozone-platform=x11",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
    ]
    extra = os.environ.get("CHROME_ARGS")
    if extra:
        args.extend(arg for arg in extra.split() if arg)
    return args


async def _solve(sitekey: str, siteurl: str, timeout: int) -> str:
    browser = await uc.start(
        browser_executable_path=_find_chrome(),
        headless=False,
        user_data_dir=_get_profile_dir(),
        browser_args=_get_browser_args(),
    )

    try:
        page = await browser.get(siteurl)
        await asyncio.sleep(random.uniform(2.0, 3.0))

        # Inject widget into the live page DOM
        await page.evaluate(f"""
            (() => {{
                if (document.getElementById('_ts_box')) return;
                window._tsToken = null;
                const wrap = document.createElement('div');
                wrap.id = '_ts_box';
                wrap.style = 'position:fixed;top:20px;left:20px;z-index:2147483647;';
                document.body.appendChild(wrap);
                window._tsLoad = function () {{
                    turnstile.render('#_ts_box', {{
                        sitekey: '{sitekey}',
                        callback: function(token) {{ window._tsToken = token; }}
                    }});
                }};
                const s = document.createElement('script');
                s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_tsLoad&render=explicit';
                s.async = true;
                document.head.appendChild(s);
            }})();
        """)

        # Give Turnstile time to load and potentially auto-complete (invisible mode)
        await asyncio.sleep(5.0)

        async def get_token() -> Optional[str]:
            return await page.evaluate("""
                (() => {
                    if (window._tsToken) return window._tsToken;
                    const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"]');
                    return (inp && inp.value) ? inp.value : null;
                })()
            """)

        async def get_cf_iframe_rect() -> Optional[dict]:
            raw = await page.evaluate("""
                JSON.stringify((() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const src = f.src || f.getAttribute('src') || '';
                        if (!src.includes('challenges.cloudflare.com')) continue;
                        const r = f.getBoundingClientRect();
                        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                    }
                    return null;
                })())
            """)
            if raw and raw != 'null':
                return json.loads(raw)
            return None

        async def do_click(rect: Optional[dict]):
            if rect:
                cx = rect["x"] + 28 + random.uniform(-3, 3)
                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                print(f"[solver] clicking Cloudflare iframe at ({cx:.0f}, {cy:.0f})")
            else:
                # Widget is fixed at top:20px left:20px
                cx = 20 + 28 + random.uniform(-3, 3)
                cy = 20 + 32 + random.uniform(-3, 3)
                print(f"[solver] iframe not in DOM, clicking fixed position ({cx:.0f}, {cy:.0f})")
            await page.mouse_move(cx - 80, cy - 20)
            await asyncio.sleep(random.uniform(0.15, 0.25))
            await page.mouse_move(cx, cy)
            await asyncio.sleep(random.uniform(0.08, 0.15))
            await page.mouse_click(cx, cy)

        # Check if already auto-solved (invisible widget)
        token = await get_token()
        if token:
            return token

        # Wait up to 10s for the visible checkbox iframe to appear
        rect = None
        for _ in range(20):
            rect = await get_cf_iframe_rect()
            if rect:
                break
            await asyncio.sleep(0.5)

        # Click loop: click, wait, retry up to 3 times
        deadline = asyncio.get_event_loop().time() + timeout
        click_count = 0
        last_click = 0.0

        while asyncio.get_event_loop().time() < deadline:
            token = await get_token()
            if token:
                break

            now = asyncio.get_event_loop().time()
            if click_count == 0 or (not token and now - last_click > 8):
                if click_count >= 3:
                    await asyncio.sleep(0.3)
                    continue
                await do_click(rect)
                last_click = asyncio.get_event_loop().time()
                click_count += 1
                # After a click, refresh iframe rect in case it moved
                await asyncio.sleep(1.0)
                rect = await get_cf_iframe_rect() or rect
                continue

            await asyncio.sleep(0.3)

    finally:
        browser.stop()

    if not token:
        raise TimeoutError(f"Turnstile token not obtained within {timeout}s")

    return token


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return asyncio.run(_solve(sitekey, siteurl, timeout))


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path(__file__).resolve().parent / env_path
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def post_local_result(endpoint: str, nric: str, captchadata: str, timeout: int = 30) -> dict:
    parsed = urllib.parse.urlparse(endpoint)

    body = json.dumps({
        "nric": nric,
        "captchadata": captchadata,
    }).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return {"status": resp.status, "body": data}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return {"status": exc.code, "body": data}


def main() -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("sitekey", nargs="?", default=os.environ.get("TURNSTILE_SITEKEY"))
    parser.add_argument("siteurl", nargs="?", default=os.environ.get("TURNSTILE_SITEURL"))
    parser.add_argument("--nric")
    parser.add_argument("--post-url", default=os.environ.get("LOCAL_POST_URL"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("SOLVER_TIMEOUT", 45)))
    parser.add_argument("--post-timeout", type=int, default=int(os.environ.get("LOCAL_POST_TIMEOUT", 30)))
    args = parser.parse_args()

    if not args.sitekey:
        raise ValueError("sitekey argument or TURNSTILE_SITEKEY env is required")
    if not args.siteurl:
        raise ValueError("siteurl argument is required")

    xvfb = _start_xvfb_if_needed()
    try:
        token = solve(args.sitekey, args.siteurl, timeout=args.timeout)
        if not args.post_url:
            print(token)
            return 0
        if not args.nric:
            raise ValueError("--nric is required when --post-url is used")
        result = post_local_result(args.post_url, args.nric, token, timeout=args.post_timeout)
        print(json.dumps({"captchadata": token, "result": result}, indent=2))
        return 0
    finally:
        if xvfb:
            xvfb.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
