import asyncio
import json
import os
import platform
import random
import shutil
import stat
import subprocess
import tempfile
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


def _get_profile_base_dir() -> str:
    """Return the dedicated base directory under which per-solve profiles are created."""
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        root = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
        return os.path.join(root, "capsolve_profiles")
    return "/tmp/capsolve_profiles"


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _directory_identity(info) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


class TrustedDirectories(list):
    def __init__(self, fds):
        super().__init__(fds)
        self.identities = tuple(_directory_identity(os.fstat(fd)) for fd in fds)


class TrustedProfileFd(int):
    def __new__(cls, fd):
        value = int.__new__(cls, fd)
        value.identity = _directory_identity(os.fstat(fd))
        return value


def _open_directory_chain(path: str, *, create_final: bool = False) -> TrustedDirectories:
    fds = [os.open("/", _DIR_FLAGS)]
    try:
        parts = [part for part in os.path.abspath(path).split(os.sep) if part]
        for index, part in enumerate(parts):
            if create_final and index == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fds[-1])
                except FileExistsError:
                    pass
            fds.append(os.open(part, _DIR_FLAGS, dir_fd=fds[-1]))
        return TrustedDirectories(fds)
    except BaseException:
        for fd in reversed(fds):
            os.close(fd)
        raise RuntimeError("Chrome profile path is unsafe") from None


def _open_profile_base() -> tuple[str, list[int]]:
    base = os.path.abspath(_get_profile_base_dir())
    fds = _open_directory_chain(base, create_final=True)
    info = os.fstat(fds[-1])
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _close_fds(fds)
        raise RuntimeError("Chrome profile base is unsafe")
    if stat.S_IMODE(info.st_mode) != 0o700:
        _close_fds(fds)
        raise RuntimeError("Chrome profile base is unsafe")
    return base, fds


def _close_fds(fds: list[int]) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _create_profile(base: str, base_fds: list[int]) -> tuple[str, str, int]:
    base_fd = base_fds[-1]
    anchor = f"/proc/self/fd/{base_fd}"
    if not os.path.isdir(anchor):
        raise RuntimeError("Chrome profile creation failed")
    name = None
    raw_fd = None
    created_identity = None
    try:
        anchored_path = tempfile.mkdtemp(prefix=f"capsolve-{os.getpid()}-", dir=anchor)
        name = os.path.basename(anchored_path)
        created = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        created_identity = _directory_identity(created)
        raw_fd = os.open(name, _DIR_FLAGS, dir_fd=base_fd)
        profile_fd = TrustedProfileFd(raw_fd)
        raw_fd = None
        info = os.fstat(profile_fd)
        if (hasattr(os, "getuid") and info.st_uid != os.getuid()) or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(profile_fd)
            raise RuntimeError
        return os.path.join(base, name), name, profile_fd
    except BaseException:
        if raw_fd is not None:
            os.close(raw_fd)
        if name is not None and created_identity is not None:
            try:
                visible = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if _directory_identity(visible) == created_identity and stat.S_ISDIR(visible.st_mode):
                    shutil.rmtree(name, dir_fd=base_fd)
            except BaseException:
                pass
        raise RuntimeError("Chrome profile creation failed") from None


def _is_owned_private_directory(info) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and (not hasattr(os, "getuid") or info.st_uid == os.getuid())
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _validate_profile(base: str, base_fds: list[int], profile_name: str, profile_fd: int) -> None:
    current_fds = _open_directory_chain(base)
    try:
        if len(current_fds) != len(base_fds) or any(
            _directory_identity(os.fstat(current)) != trusted_identity
            for current, trusted_identity in zip(current_fds, base_fds.identities)
        ):
            raise RuntimeError
        current_base = os.fstat(current_fds[-1])
        trusted_base = os.fstat(base_fds[-1])
        if _directory_identity(trusted_base) != base_fds.identities[-1]:
            raise RuntimeError
        if not _is_owned_private_directory(current_base) or not _is_owned_private_directory(trusted_base):
            raise RuntimeError
        visible = os.stat(profile_name, dir_fd=current_fds[-1], follow_symlinks=False)
        trusted = os.fstat(profile_fd)
        if _directory_identity(trusted) != profile_fd.identity or _directory_identity(visible) != profile_fd.identity:
            raise RuntimeError
        if not _is_owned_private_directory(visible) or not _is_owned_private_directory(trusted):
            raise RuntimeError
    except BaseException:
        raise RuntimeError("Chrome profile path is unsafe") from None
    finally:
        _close_fds(current_fds)


def _remove_profile(base: str, base_fds: list[int], profile_name: str, profile_fd: int) -> None:
    try:
        _validate_profile(base, base_fds, profile_name, profile_fd)
        shutil.rmtree(profile_name, dir_fd=base_fds[-1])
    except BaseException:
        raise RuntimeError("Chrome profile cleanup failed") from None


class BrowserCleanupError(RuntimeError):
    pass


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _start_xvfb_if_needed() -> Optional[subprocess.Popen]:
    """On Linux, start a hidden virtual display when needed or enabled."""
    if platform.system() != "Linux" or os.environ.get("ENVIRONMENT", "development") != "development":
        return None
    if os.environ.get("DISPLAY") or not _env_truthy("ENABLE_XVFB_VIRTUAL_DISPLAY"):
        return None
    display = os.environ.get("XVFB_DISPLAY", ":99")
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        raise RuntimeError("Xvfb failed to start")
    os.environ["DISPLAY"] = display
    proc._capsolve_display = display
    return proc


def _stop_xvfb(proc: subprocess.Popen) -> None:
    display = getattr(proc, "_capsolve_display", None)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    if display is not None and os.environ.get("DISPLAY") == display:
        os.environ.pop("DISPLAY", None)


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


class BrowserOwner:
    def __init__(self, profile_dir: str):
        self.profile_dir = profile_dir
        config = uc.Config(
            browser_executable_path=_find_chrome(),
            headless=False,
            user_data_dir=profile_dir,
            browser_args=_get_browser_args(),
        )
        self.browser = uc.Browser(config)

    async def start(self):
        await self.browser.start()
        return self.browser


async def _start_browser(profile_or_owner):
    owner = profile_or_owner if isinstance(profile_or_owner, BrowserOwner) else BrowserOwner(profile_or_owner)
    return await owner.start()


async def _stop_browser(browser) -> bool:
    operation_failed = False
    try:
        await browser.aclose()
    except BaseException:
        operation_failed = True
    process = getattr(browser, "_process", None)
    if process is not None and process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
        except BaseException:
            pass
    if process is not None and process.returncode is None or process is None and operation_failed:
        raise BrowserCleanupError("Browser cleanup failed")
    return operation_failed


async def _solve(sitekey: str, siteurl: str, timeout: int) -> str:
    profile_base, base_fds = _open_profile_base()
    profile_dir = None
    profile_name = None
    profile_fd = None
    owner = None
    browser = None

    try:
        profile_dir, profile_name, profile_fd = _create_profile(profile_base, base_fds)
        _validate_profile(profile_base, base_fds, profile_name, profile_fd)
        owner = BrowserOwner(profile_dir)
        browser = await _start_browser(owner)
        owner.browser = browser
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
        try:
            operation_failed = await _stop_browser(owner.browser) if owner is not None else False
            if profile_name is not None and profile_fd is not None:
                _remove_profile(profile_base, base_fds, profile_name, profile_fd)
            if operation_failed:
                raise BrowserCleanupError("Browser cleanup failed")
        finally:
            if profile_fd is not None:
                os.close(profile_fd)
            _close_fds(base_fds)

    if not token:
        raise TimeoutError(f"Turnstile token not obtained within {timeout}s")

    return token


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return asyncio.run(_solve(sitekey, siteurl, timeout))


def load_dotenv(path: str = ".env") -> None:
    if os.environ.get("ENVIRONMENT", "").strip() == "production":
        return
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
    print("Direct solver CLI is disabled; use the API or queued worker.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
