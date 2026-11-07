import json
import os
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from argparse import Namespace
from pathlib import Path
from urllib.request import urlopen

import pytest

from scripts.run_uvicorn_dev import (
    changed_python_files,
    describe_windows_exit_code,
    diagnostic_event,
    run_supervisor,
    snapshot_python_files,
    start_native_crash_capture,
    worker_command,
)


def test_native_capture_is_opt_in_and_api_only(monkeypatch):
    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("Must not attach a debugger")

    monkeypatch.setattr("scripts.run_uvicorn_dev.subprocess.Popen", unexpected_spawn)
    monkeypatch.delenv("JOBCOCKPIT_DEV_PROCDUMP", raising=False)
    assert start_native_crash_capture(Namespace(app="jobcockpit.api.main:app")) is None
    monkeypatch.setenv("JOBCOCKPIT_DEV_PROCDUMP", "procdump64.exe")
    assert start_native_crash_capture(Namespace(app="mock_site:app")) is None
    monkeypatch.setattr("scripts.run_uvicorn_dev.sys.platform", "linux")
    assert start_native_crash_capture(Namespace(app="jobcockpit.api.main:app")) is None


def test_native_capture_reports_launch_failure_without_stopping_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBCOCKPIT_DEV_PROCDUMP", "missing-procdump.exe")
    monkeypatch.setattr("scripts.run_uvicorn_dev.sys.platform", "win32")

    def failed_spawn(*args, **kwargs):
        raise OSError("missing utility")

    monkeypatch.setattr("scripts.run_uvicorn_dev.subprocess.Popen", failed_spawn)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    path = tmp_path / "diagnostic.jsonl"
    args = Namespace(app="jobcockpit.api.main:app", diagnostic_log=str(path))
    assert start_native_crash_capture(args) is None
    event = json.loads(path.read_text())
    assert event["event"] == "native_crash_capture_failed"
    assert event["error"] == "missing utility"


def test_native_capture_attaches_to_interpreter_pid(monkeypatch, tmp_path):
    import ctypes

    calls = []
    monitor = SimpleNamespace(pid=987, poll=lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBCOCKPIT_DEV_PROCDUMP", "tools/procdump64.exe")
    monkeypatch.setattr("scripts.run_uvicorn_dev.sys.platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(IsDebuggerPresent=lambda: True)), raising=False)

    def spawn(command, **options):
        calls.append((command, options))
        return monitor

    monkeypatch.setattr("scripts.run_uvicorn_dev.subprocess.Popen", spawn)
    path = tmp_path / "diagnostic.jsonl"
    args = Namespace(app="jobcockpit.api.main:app", diagnostic_log=str(path))
    assert start_native_crash_capture(args) is monitor
    command, options = calls[0]
    assert command[1:4] == ["-accepteula", "-ma", "-e"]
    assert command[4] == str(os.getpid())
    assert Path(command[5]).is_relative_to(tmp_path / "var")
    assert options["stdin"] == subprocess.DEVNULL
    assert json.loads(path.read_text())["event"] == "native_crash_capture_attached"


def test_native_capture_retains_existing_dumps_and_caps_collection(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBCOCKPIT_DEV_PROCDUMP", "procdump.exe")
    monkeypatch.setattr("scripts.run_uvicorn_dev.sys.platform", "win32")
    dump_dir = tmp_path / "var/logs/dev/dumps"
    dump_dir.mkdir(parents=True)
    for i in range(5):
        (dump_dir / f"crash-{i}.dmp").write_bytes(b"existing evidence")
    args = Namespace(app="jobcockpit.api.main:app", diagnostic_log=None)
    assert start_native_crash_capture(args) is None
    assert len(list(dump_dir.glob("*.dmp"))) == 5


def test_python_snapshot_detects_add_change_and_delete(tmp_path: Path):
    first = tmp_path / "first.py"
    ignored = tmp_path / "ignored.txt"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    ignored.write_text("not Python\n", encoding="utf-8")

    initial = snapshot_python_files([tmp_path])
    assert set(initial) == {first.resolve()}

    first.write_text("VALUE = 2\n", encoding="utf-8")
    first.touch()
    second = tmp_path / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")
    updated = snapshot_python_files([tmp_path])

    assert set(changed_python_files(initial, updated)) == {
        first.resolve(),
        second.resolve(),
    }

    first.unlink()
    after_delete = snapshot_python_files([tmp_path])
    assert changed_python_files(updated, after_delete) == [first.resolve()]


def test_worker_command_starts_a_fresh_python_interpreter():
    args = type(
        "Args",
        (),
        {
            "app": "example:app",
            "app_dir": "example",
            "reload_dir": ["example"],
            "host": "127.0.0.1",
            "port": 8765,
            "reload_delay": 0.1,
            "log_level": "warning",
            "diagnostic_log": None,
            "crash_log": None,
        },
    )()

    command = worker_command(args)

    assert command[0] == sys.executable
    assert Path(command[1]).resolve() == Path("scripts/run_uvicorn_dev.py").resolve()
    assert command[-1] == "--worker"


def test_windows_negative_one_exit_code_is_actionable():
    description = describe_windows_exit_code(-1)

    assert "0xFFFFFFFF" in description
    assert "signed -1" in description
    assert "explicit or external exit" in description


def test_diagnostic_event_persists_process_context(tmp_path: Path):
    path = tmp_path / "api.jsonl"
    args = Namespace(app="example:app", diagnostic_log=str(path))

    diagnostic_event(args, "worker_exited", worker_pid=123, return_code=-1)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event"] == "worker_exited"
    assert payload["app"] == "example:app"
    assert payload["pid"] == os.getpid()
    assert payload["worker_pid"] == 123
    assert payload["return_code"] == -1


def test_supervisor_restarts_one_unexpected_worker_exit(monkeypatch):
    workers = []

    class ExitedWorker:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return -1

        def wait(self):
            return -1

    def fake_start_worker(_args):
        worker = ExitedWorker(100 + len(workers))
        workers.append(worker)
        return worker

    monkeypatch.setattr("scripts.run_uvicorn_dev.start_worker", fake_start_worker)
    monkeypatch.setattr("scripts.run_uvicorn_dev.time.sleep", lambda _seconds: None)
    args = Namespace(
        app="example:app",
        reload_dir=[],
        diagnostic_log=None,
        restart_limit=1,
        restart_delay=0,
        restart_max_delay=30,
        restart_reset_after=60,
    )

    assert run_supervisor(args) == -1
    assert [worker.pid for worker in workers] == [100, 101]


@pytest.mark.parametrize(
    "limit,exits,expected_delays",
    [
        (-1, [(-1, 0)] * 4 + [(-1, 61), (0, 0)], [1, 2, 4, 4, 1]),
        (1, [(-1, 61), (-1, 61), (0, 0)], [1, 1]),
        (0, [(-1, 0)], []),
        (-1, [(130, 0)], []),
        (-1, [(0xC000013A, 0)], []),
    ],
)
def test_supervisor_restart_backoff_and_stable_reset(monkeypatch, limit, exits, expected_delays):
    clock = [0]
    delays = []
    outcomes = iter(exits)

    class Worker:
        pid = 123

        def __init__(self):
            self.code, self.lifetime = next(outcomes)

        def poll(self):
            return self.code

        def wait(self):
            clock[0] += self.lifetime
            return self.code

    monkeypatch.setattr("scripts.run_uvicorn_dev.start_worker", lambda _: Worker())
    monkeypatch.setattr("scripts.run_uvicorn_dev.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("scripts.run_uvicorn_dev.time.sleep", delays.append)
    args = Namespace(app="example:app", reload_dir=[], diagnostic_log=None,
                     restart_limit=limit, restart_delay=1, restart_max_delay=4,
                     restart_reset_after=60)
    assert run_supervisor(args) == exits[-1][0]
    assert delays == expected_delays


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _read_response(port: int) -> str | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
            return response.read().decode("utf-8")
    except OSError:
        return None


def _wait_for_response(port: int, expected: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_response(port) == expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"API did not return {expected!r} before timeout")


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def test_reload_imports_changed_application_in_a_fresh_interpreter(tmp_path: Path):
    app_file = tmp_path / "reload_app.py"
    app_file.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "def root(): return 'before'\n",
        encoding="utf-8",
    )
    port = _available_port()
    diagnostic_log = tmp_path / "api.jsonl"
    crash_log = tmp_path / "api-crash.log"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/run_uvicorn_dev.py",
            "reload_app:app",
            "--app-dir",
            str(tmp_path),
            "--reload-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--reload-delay",
            "0.05",
            "--log-level",
            "warning",
            "--diagnostic-log",
            str(diagnostic_log),
            "--crash-log",
            str(crash_log),
        ],
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
        text=True,
    )
    try:
        _wait_for_response(port, '"before"')
        # Reproduce the Windows-1252 smart apostrophe that interrupted ingestion.
        app_file.write_bytes(b"VALUE = '\x92'\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in diagnostic_log.read_text().splitlines()]
            if any(event["event"] == "reload_rejected" for event in events):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Invalid UTF-8 edit was not rejected before reload")
        _wait_for_response(port, '"before"')
        assert len([e for e in events if e["event"] == "worker_booted"]) == 1

        # Editing a second file must not let the still-invalid first file reload.
        (tmp_path / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        time.sleep(0.25)
        _wait_for_response(port, '"before"')
        app_file.write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/')\n"
            "def root(): return 'after'\n",
            encoding="utf-8",
        )
        _wait_for_response(port, '"after"')
    finally:
        _stop_process_group(process)

    events = [
        json.loads(line)
        for line in diagnostic_log.read_text(encoding="utf-8").splitlines()
    ]
    booted_pids = [event["pid"] for event in events if event["event"] == "worker_booted"]
    assert len(booted_pids) >= 2
    assert len(set(booted_pids)) == len(booted_pids)
    assert any(event["event"] == "reload_requested" for event in events)
    assert crash_log.exists()


def test_unlimited_recovery_survives_three_real_worker_crashes(tmp_path: Path):
    (tmp_path / "crashing_app.py").write_text(
        "import asyncio, os\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "def root(): return os.getpid()\n"
        "@app.get('/crash')\n"
        "async def crash():\n"
        "    asyncio.get_running_loop().call_later(0.1, os._exit, 23)\n"
        "    return 'scheduled'\n",
        encoding="utf-8",
    )
    port = _available_port()
    diagnostic_log = tmp_path / "api.jsonl"
    process = subprocess.Popen(
        [sys.executable, "scripts/run_uvicorn_dev.py", "crashing_app:app",
         "--app-dir", str(tmp_path), "--reload-dir", str(tmp_path),
         "--port", str(port), "--reload-delay", "0.05", "--log-level", "warning",
         "--restart-limit", "-1", "--restart-delay", "0.05",
         "--restart-max-delay", "0.1", "--diagnostic-log", str(diagnostic_log)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        start_new_session=sys.platform != "win32",
    )
    previous = None
    pids = set()
    try:
        for iteration in range(4):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = _read_response(port)
                if current and current != previous:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Worker did not recover")
            pids.add(current)
            previous = current
            assert process.poll() is None
            if iteration < 3:
                with urlopen(f"http://127.0.0.1:{port}/crash", timeout=2) as response:
                    assert response.status == 200
    finally:
        _stop_process_group(process)
    assert len(pids) == 4
    events = [json.loads(line) for line in diagnostic_log.read_text().splitlines()]
    restarts = [e for e in events if e["event"] == "unexpected_restart_requested"]
    assert [e["delay_seconds"] for e in restarts] == [0.05, 0.1, 0.1]
