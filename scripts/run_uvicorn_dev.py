from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from typing import TextIO

import uvicorn


LOGGER = logging.getLogger("uvicorn.error")
WORKER_STOP_COMMAND = "stop"
WINDOWS_EXIT_CODES = {
    0xFFFFFFFF: "explicit or external exit with status -1",
    0xC0000005: "access violation",
    0xC0000017: "insufficient memory",
    0xC0000094: "integer divide by zero",
    0xC000013A: "interrupted by Ctrl+C or Ctrl+Break",
    0xC0000374: "heap corruption",
    0xC0000409: "stack buffer overrun or fail-fast termination",
}


def snapshot_python_files(reload_dirs: Iterable[Path]) -> dict[Path, bytes]:
    snapshot: dict[Path, bytes] = {}
    for reload_dir in reload_dirs:
        for path in reload_dir.rglob("*.py"):
            try:
                snapshot[path.resolve()] = hashlib.blake2s(path.read_bytes()).digest()
            except OSError:
                continue
    return snapshot


def changed_python_files(before: dict[Path, bytes], after: dict[Path, bytes]) -> list[Path]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def validate_python_changes(changes: list[Path], snapshot: dict[Path, bytes]) -> dict[str, str]:
    """Check changed bytes without importing or executing application code."""
    errors = {}
    for path in changes:
        if path not in snapshot:
            continue  # Deleted files still trigger a reload.
        try:
            content = path.read_bytes()
            if hashlib.blake2s(content).digest() != snapshot[path]:
                errors[str(path)] = "File changed during validation; waiting for a stable save"
                continue
            compile(content.decode("utf-8-sig"), str(path), "exec", dont_inherit=True)
        except (OSError, UnicodeError, SyntaxError) as error:
            detail = error.msg if isinstance(error, SyntaxError) else str(error)
            errors[str(path)] = f"{type(error).__name__}: {detail}"
    return errors


def describe_windows_exit_code(return_code: int) -> str:
    unsigned = return_code & 0xFFFFFFFF
    signed = unsigned if unsigned < 0x80000000 else unsigned - 0x100000000
    description = WINDOWS_EXIT_CODES.get(unsigned)
    details = f"{unsigned} (0x{unsigned:08X}; signed {signed})"
    return f"{details}: {description}" if description else details


def describe_exit_code(return_code: int) -> str:
    if sys.platform != "win32":
        return str(return_code)
    return describe_windows_exit_code(return_code)


def diagnostic_event(args: argparse.Namespace, event: str, **details: object) -> None:
    if not args.diagnostic_log:
        return
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "app": args.app,
        "pid": os.getpid(),
        **details,
    }
    path = Path(args.diagnostic_log)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{json.dumps(payload, sort_keys=True)}\n")
    except OSError as error:
        LOGGER.error("Could not write Uvicorn diagnostic event to %s: %s", path, error)


def enable_fault_log(args: argparse.Namespace) -> TextIO | None:
    if not args.crash_log:
        return None
    path = Path(args.crash_log)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a", encoding="utf-8")
        stream.write(
            f"\n{datetime.now(UTC).isoformat()} enabling Python fault handler "
            f"for pid={os.getpid()} app={args.app}\n"
        )
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
        return stream
    except OSError as error:
        LOGGER.error("Could not enable Python fault logging at %s: %s", path, error)
        return None


def start_native_crash_capture(args: argparse.Namespace) -> subprocess.Popen | None:
    """Attach to the real interpreter PID, not Windows' venv launcher PID."""
    executable = os.environ.get("JOBCOCKPIT_DEV_PROCDUMP")
    if sys.platform != "win32" or not executable or args.app != "jobcockpit.api.main:app":
        return None
    import ctypes

    dump_dir = Path("var/logs/dev/dumps").resolve()
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        # Keep captured evidence without letting a crash loop fill the disk.
        if sum(1 for _ in dump_dir.glob("*.dmp")) >= 5:
            LOGGER.warning("Native dump limit reached (5); retained dumps: %s", dump_dir)
            diagnostic_event(args, "native_crash_capture_limit_reached", dump_dir=str(dump_dir))
            return None
        monitor_log = dump_dir / f"procdump-{os.getpid()}-{time.time_ns()}.log"
        with monitor_log.open("w", encoding="utf-8") as stream:
            monitor = subprocess.Popen(
                [str(Path(executable).resolve()), "-accepteula", "-ma", "-e",
                 str(os.getpid()), str(dump_dir)],
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        deadline = time.monotonic() + 10
        while monitor.poll() is None and time.monotonic() < deadline:
            if ctypes.windll.kernel32.IsDebuggerPresent():
                diagnostic_event(args, "native_crash_capture_attached",
                                 monitor_pid=monitor.pid, dump_dir=str(dump_dir))
                LOGGER.info("Native crash capture attached to API pid %s; dumps: %s",
                            os.getpid(), dump_dir)
                return monitor
            time.sleep(0.1)
        LOGGER.error("Native crash capture did not confirm attachment; inspect %s", monitor_log)
        diagnostic_event(args, "native_crash_capture_unconfirmed", log=str(monitor_log))
        return monitor
    except OSError as error:
        LOGGER.error("Could not start native crash capture: %s", error)
        diagnostic_event(args, "native_crash_capture_failed", error=str(error))
        return None


def worker_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        args.app,
        "--app-dir",
        args.app_dir,
    ]
    for reload_dir in args.reload_dir:
        command.extend(("--reload-dir", reload_dir))
    command.extend(
        (
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--reload-delay",
            str(args.reload_delay),
            "--log-level",
            args.log_level,
        )
    )
    if args.diagnostic_log:
        command.extend(("--diagnostic-log", args.diagnostic_log))
    if args.crash_log:
        command.extend(("--crash-log", args.crash_log))
    command.append("--worker")
    return command


def start_worker(args: argparse.Namespace) -> subprocess.Popen[str]:
    process_options: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "text": True,
    }
    if sys.platform == "win32":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    return subprocess.Popen(worker_command(args), **process_options)


def stop_worker(worker: subprocess.Popen[str], timeout: float = 10.0) -> None:
    if worker.poll() is not None:
        return
    try:
        if worker.stdin is not None:
            worker.stdin.write(f"{WORKER_STOP_COMMAND}\n")
            worker.stdin.flush()
        worker.wait(timeout=timeout)
        return
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        pass

    worker.terminate()
    try:
        worker.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait()


def log_reload(changes: list[Path]) -> None:
    display = ", ".join(str(path) for path in changes[:5])
    if len(changes) > 5:
        display += f", ... {len(changes) - 5} more"
    LOGGER.warning("Python source changes detected in %s. Reloading...", display)


def run_supervisor(args: argparse.Namespace) -> int:
    reload_dirs = [Path(path).resolve() for path in args.reload_dir]
    previous = snapshot_python_files(reload_dirs)
    unexpected_restarts = 0
    diagnostic_event(
        args,
        "supervisor_started",
        executable=sys.executable,
        reload_dirs=[str(path) for path in reload_dirs],
        restart_limit=args.restart_limit,
    )

    while True:
        started_at = time.monotonic()
        worker = start_worker(args)
        diagnostic_event(args, "worker_spawned", worker_pid=worker.pid)
        LOGGER.info("Uvicorn worker started with pid %s", worker.pid)
        reload_requested = False
        pending = None
        rejected = None
        try:
            while worker.poll() is None:
                time.sleep(args.reload_delay)
                current = snapshot_python_files(reload_dirs)
                changes = changed_python_files(previous, current)
                if changes:
                    # Allow an editor's save to settle before reading it again.
                    if current != pending:
                        pending = current
                        continue
                    if current == rejected:
                        continue
                    errors = validate_python_changes(changes, current)
                    if errors:
                        rejected = current
                        LOGGER.error("Keeping the current API worker; invalid Python edits: %s",
                                     errors)
                        diagnostic_event(args, "reload_rejected", worker_pid=worker.pid,
                                         errors=errors)
                        continue
                    previous = current
                    log_reload(changes)
                    diagnostic_event(
                        args,
                        "reload_requested",
                        worker_pid=worker.pid,
                        changed_files=[str(path) for path in changes],
                    )
                    reload_requested = True
                    stop_worker(worker)
                    break
        except KeyboardInterrupt:
            diagnostic_event(args, "supervisor_interrupted", worker_pid=worker.pid)
            stop_worker(worker)
            return 130

        return_code = worker.wait()
        # A stable worker starts a new failure streak, including after source reloads.
        if time.monotonic() - started_at >= args.restart_reset_after:
            unexpected_restarts = 0
        diagnostic_event(
            args,
            "worker_exited",
            worker_pid=worker.pid,
            return_code=return_code,
            exit_description=describe_exit_code(return_code),
            reload_requested=reload_requested,
        )
        if not reload_requested:
            if return_code != 0:
                LOGGER.error(
                    "Uvicorn worker pid %s exited unexpectedly with %s",
                    worker.pid,
                    describe_exit_code(return_code),
                )
                if return_code in {130, 0xC000013A, -1073741510}:
                    return return_code
                if args.restart_limit == -1 or unexpected_restarts < args.restart_limit:
                    unexpected_restarts += 1
                    delay = min(
                        args.restart_max_delay,
                        args.restart_delay * (2 ** min(unexpected_restarts - 1, 16)),
                    )
                    LOGGER.warning(
                        "Restarting Uvicorn worker in %.1fs after unexpected exit (%s/%s)",
                        delay,
                        unexpected_restarts,
                        "unlimited" if args.restart_limit == -1 else args.restart_limit,
                    )
                    diagnostic_event(
                        args,
                        "unexpected_restart_requested",
                        worker_pid=worker.pid,
                        restart_number=unexpected_restarts,
                        delay_seconds=delay,
                    )
                    try:
                        time.sleep(delay)
                    except KeyboardInterrupt:
                        return 130
                    continue
            diagnostic_event(
                args,
                "supervisor_exiting",
                return_code=return_code,
                exit_description=describe_exit_code(return_code),
            )
            return return_code


def stop_worker_from_stdin(server: uvicorn.Server) -> None:
    command = sys.stdin.readline().strip().casefold()
    if command in {"", WORKER_STOP_COMMAND}:
        server.should_exit = True


def run_worker(args: argparse.Namespace) -> int:
    fault_log = enable_fault_log(args)
    diagnostic_event(args, "worker_booted", executable=sys.executable,
                     python_version=sys.version, openssl_version=ssl.OPENSSL_VERSION)
    server: uvicorn.Server | None = None
    try:
        app_dir = Path(args.app_dir).resolve()
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
        server = uvicorn.Server(
            uvicorn.Config(
                args.app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
            )
        )
        # Config has now initialized console logging. Retain the monitor until exit;
        # killing a debugger during shutdown could interfere with dump collection.
        native_monitor = start_native_crash_capture(args)
        stop_listener = Thread(
            target=stop_worker_from_stdin,
            args=(server,),
            name="jobcockpit-uvicorn-worker-stop",
            daemon=True,
        )
        stop_listener.start()
        server.run()
        if native_monitor is not None and native_monitor.poll() is not None:
            LOGGER.warning("Native crash capture monitor exited before the API worker")
        diagnostic_event(args, "worker_stopped", server_started=server.started)
        return 0
    except BaseException as error:
        diagnostic_event(
            args,
            "worker_exception",
            exception_type=type(error).__name__,
            exception=str(error),
            traceback=traceback.format_exc(),
        )
        raise
    finally:
        if fault_log is not None:
            faulthandler.disable()
            fault_log.flush()
            fault_log.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Uvicorn with signal-free source reloads.")
    parser.add_argument("app")
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--reload-dir", action="append", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reload-delay", type=float, default=0.25)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--diagnostic-log")
    parser.add_argument("--crash-log")
    parser.add_argument("--restart-limit", type=int, default=1)
    parser.add_argument("--restart-delay", type=float, default=1.0)
    parser.add_argument("--restart-max-delay", type=float, default=30.0)
    parser.add_argument("--restart-reset-after", type=float, default=60.0)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.restart_limit < -1:
        raise SystemExit("--restart-limit must be -1 (unlimited), zero, or greater")
    if not 0 <= args.restart_delay <= args.restart_max_delay or args.restart_reset_after <= 0:
        raise SystemExit("Restart delays must be non-negative and reset interval positive")
    if args.worker:
        return run_worker(args)
    fault_log = enable_fault_log(args)
    try:
        return run_supervisor(args)
    except BaseException as error:
        diagnostic_event(
            args,
            "supervisor_exception",
            exception_type=type(error).__name__,
            exception=str(error),
            traceback=traceback.format_exc(),
        )
        raise
    finally:
        if fault_log is not None:
            faulthandler.disable()
            fault_log.flush()
            fault_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
