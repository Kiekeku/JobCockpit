from __future__ import annotations

from pathlib import Path

import pytest

from jobcockpit.ingestion.chrome_cdp import (
    connect_chrome_cdp,
    launch_chrome_cdp,
    validate_local_cdp_endpoint,
)


def test_shared_cdp_connector_rechecks_under_lock_before_launching() -> None:
    endpoint_started = False
    connect_timeouts: list[int] = []
    launches: list[str] = []

    class Chromium:
        def connect_over_cdp(self, endpoint_url: str, *, timeout: int):
            connect_timeouts.append(timeout)
            if not endpoint_started:
                raise RuntimeError("connection refused")
            return "browser"

    def launch() -> None:
        nonlocal endpoint_started
        launches.append("launch")
        endpoint_started = True

    browser = connect_chrome_cdp(
        Chromium(),
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=5,
        auto_start=True,
        launcher=launch,
    )

    assert browser == "browser"
    assert launches == ["launch"]
    assert len(connect_timeouts) == 3
    assert connect_timeouts[0] == 5000
    assert 1 <= connect_timeouts[1] <= 1000
    assert 1 <= connect_timeouts[2] <= 1000


def test_shared_cdp_lifecycle_rejects_nonlocal_endpoint() -> None:
    with pytest.raises(ValueError, match="localhost"):
        validate_local_cdp_endpoint("http://example.com:9222")


def test_shared_cdp_launcher_builds_one_dedicated_profile_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    commands: list[list[str]] = []

    class Process:
        pass

    monkeypatch.setattr(
        "jobcockpit.ingestion.chrome_cdp.subprocess.Popen",
        lambda args, **_kwargs: commands.append(list(args)) or Process(),
    )

    launch_chrome_cdp(
        endpoint_url="http://127.0.0.1:9333",
        executable_path=chrome,
        user_data_dir=tmp_path / "profile",
        profile_directory="Default",
        headless=True,
    )

    assert len(commands) == 1
    assert "--remote-debugging-port=9333" in commands[0]
    assert any(value.startswith("--user-data-dir=") for value in commands[0])
    assert "--profile-directory=Default" in commands[0]
    assert "--headless=new" in commands[0]
