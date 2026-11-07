from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlparse

Clock = Callable[[], float]
Sleeper = Callable[[float], None]

_STARTUP_LOCK = threading.Lock()
_LOCAL_CDP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def connect_chrome_cdp(
    chromium: Any,
    *,
    endpoint_url: str,
    timeout_seconds: float,
    auto_start: bool,
    launcher: Callable[[], None],
    clock: Clock = time.monotonic,
    sleeper: Sleeper = time.sleep,
) -> Any:
    """Connect to one local CDP browser, starting it once when configured."""

    endpoint = validate_local_cdp_endpoint(endpoint_url).geturl()
    if timeout_seconds <= 0:
        raise ValueError("Chrome CDP timeout must be positive")
    deadline = clock() + timeout_seconds
    last_error: Exception | None = None

    try:
        return chromium.connect_over_cdp(
            endpoint,
            timeout=max(1, int(timeout_seconds * 1000)),
        )
    except Exception as error:
        if not auto_start:
            raise
        last_error = error

    remaining = deadline - clock()
    if remaining <= 0 or not _STARTUP_LOCK.acquire(timeout=remaining):
        raise last_error
    try:
        reconnect_timeout_ms = _remaining_timeout_ms(deadline, clock, cap_ms=1000)
        if reconnect_timeout_ms <= 0:
            raise last_error
        try:
            return chromium.connect_over_cdp(
                endpoint,
                timeout=reconnect_timeout_ms,
            )
        except Exception as error:
            last_error = error

        if _remaining_timeout_ms(deadline, clock) <= 0:
            raise last_error
        launcher()
        startup_deadline = min(deadline, clock() + 10.0)
        while True:
            retry_timeout_ms = _remaining_timeout_ms(
                startup_deadline,
                clock,
                cap_ms=1000,
            )
            if retry_timeout_ms <= 0:
                raise last_error
            try:
                return chromium.connect_over_cdp(
                    endpoint,
                    timeout=retry_timeout_ms,
                )
            except Exception as error:
                last_error = error
                remaining = startup_deadline - clock()
                if remaining <= 0:
                    raise last_error
                sleeper(min(0.25, remaining))
    finally:
        _STARTUP_LOCK.release()


def launch_chrome_cdp(
    *,
    endpoint_url: str,
    executable_path: str | Path | None,
    user_data_dir: str | Path,
    profile_directory: str | None,
    headless: bool,
    extra_args: tuple[str, ...] = (),
) -> None:
    endpoint = validate_local_cdp_endpoint(endpoint_url)
    chrome_path = chrome_executable_path(executable_path)
    profile_path = Path(user_data_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome_path),
        f"--remote-debugging-port={endpoint.port}",
        f"--user-data-dir={profile_path.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        *extra_args,
    ]
    if profile_arg := chrome_profile_directory_arg(profile_directory):
        args.append(profile_arg)
    if headless:
        args.append("--headless=new")
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def validate_local_cdp_endpoint(endpoint_url: str) -> ParseResult:
    parsed = urlparse(endpoint_url.strip())
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme not in {"http", "https"} or hostname not in _LOCAL_CDP_HOSTS:
        raise ValueError("Chrome CDP endpoint must be a localhost http(s) URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Chrome CDP endpoint must include a valid port") from error
    if port is None or port < 1:
        raise ValueError("Chrome CDP endpoint must include a nonzero port")
    return parsed


def chrome_executable_path(configured_path: str | Path | None) -> Path:
    candidates: list[Path] = []
    if configured_path is not None:
        candidates.append(Path(configured_path))
    for command in ("chrome.exe", "chrome"):
        if resolved := shutil.which(command):
            candidates.append(Path(resolved))
    candidates.extend(
        (
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        )
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("Chrome executable was not found for CDP auto-start")


def chrome_profile_directory_arg(profile_directory: str | None) -> str:
    if not profile_directory:
        return ""
    stripped = profile_directory.strip()
    if not stripped or any(char in stripped for char in ('"', "'", "\r", "\n")):
        raise ValueError("Invalid Chrome CDP profile directory")
    return f"--profile-directory={stripped}"


def _remaining_timeout_ms(
    deadline: float,
    clock: Clock,
    *,
    cap_ms: int | None = None,
) -> int:
    remaining_ms = int(max(0.0, deadline - clock()) * 1000)
    if cap_ms is not None:
        remaining_ms = min(remaining_ms, cap_ms)
    return remaining_ms
