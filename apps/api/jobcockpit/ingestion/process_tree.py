from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from typing import Any


def run_with_process_tree_cleanup(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    timeout = kwargs.pop("timeout", None)
    cleanup_poll_interval = float(kwargs.pop("cleanup_poll_interval", 5.0))
    cleanup_timeout = float(kwargs.pop("cleanup_timeout", 5.0))
    capture_output = kwargs.pop("capture_output", False)
    input_supplied = "input" in kwargs
    input_data = kwargs.pop("input", None)
    if input_supplied:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)

    process = subprocess.Popen(args, **kwargs)
    observed_playwright_pids: set[int] = set()
    deadline = time.monotonic() + float(timeout) if timeout is not None else None
    input_pending = input_supplied
    try:
        while True:
            observed_playwright_pids.update(_owned_playwright_descendant_pids(process.pid))
            poll_timeout = cleanup_poll_interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout=timeout)
                poll_timeout = min(poll_timeout, remaining)
            try:
                if input_pending:
                    input_pending = False
                    stdout, stderr = process.communicate(
                        input=input_data,
                        timeout=poll_timeout,
                    )
                else:
                    stdout, stderr = process.communicate(timeout=poll_timeout)
                break
            except subprocess.TimeoutExpired:
                if deadline is None:
                    continue
                if time.monotonic() >= deadline:
                    raise
    except subprocess.TimeoutExpired as error:
        kill_process_tree(process.pid, force=False)
        try:
            stdout, stderr = process.communicate(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(process.pid, force=True)
            try:
                stdout, stderr = process.communicate(timeout=cleanup_timeout)
            except subprocess.TimeoutExpired:
                stdout = error.output
                stderr = error.stderr
        raise subprocess.TimeoutExpired(
            cmd=error.cmd,
            timeout=error.timeout,
            output=stdout,
            stderr=stderr,
        ) from error
    finally:
        observed_playwright_pids.update(_owned_playwright_descendant_pids(process.pid))
        cleanup_owned_playwright_processes(observed_playwright_pids)

    return subprocess.CompletedProcess(args=args, returncode=process.returncode, stdout=stdout, stderr=stderr)


def kill_process_tree(pid: int, *, force: bool = True) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", *(["/F"] if force else [])],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        return
    signum = signal.SIGKILL if force else signal.SIGTERM
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(pid, signum)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def cleanup_owned_playwright_processes(pids: set[int]) -> int:
    if os.name != "nt" or not pids:
        return 0
    processes = _windows_processes()
    by_pid = {process["pid"]: process for process in processes}
    killed = 0
    for pid in sorted(pids):
        process = by_pid.get(pid)
        if process is None:
            continue
        if not _looks_like_jobcockpit_playwright_process(process["command"].lower()):
            continue
        kill_process_tree(pid)
        killed += 1
    return killed


def _windows_processes() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine,Name | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    processes: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
            parent_pid = int(row.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        processes.append(
            {
                "pid": pid,
                "parent_pid": parent_pid,
                "name": str(row.get("Name") or ""),
                "command": str(row.get("CommandLine") or ""),
                "created_at": str(row.get("CreationDate") or ""),
            }
        )
    return processes


def _looks_like_jobcockpit_playwright_process(command: str) -> bool:
    return any(
        marker in command
        for marker in (
            "playwright_chromiumdev_profile-",
            "playwright-core\\lib\\entry\\clidaemon.js",
            "playwright-core/lib/entry/clidaemon.js",
            "@playwright\\mcp",
            "@playwright/mcp",
            "playwright-mcp",
        )
    )


def _owned_playwright_descendant_pids(root_pid: int) -> set[int]:
    if os.name != "nt":
        return set()
    processes = _windows_processes()
    descendant_pids = _descendant_pids(root_pid, processes)
    return {
        process["pid"]
        for process in processes
        if process["pid"] in descendant_pids and _looks_like_jobcockpit_playwright_process(process["command"].lower())
    }


def _descendant_pids(root_pid: int, processes: list[dict[str, Any]]) -> set[int]:
    children_by_parent: dict[int, list[int]] = {}
    for process in processes:
        children_by_parent.setdefault(int(process["parent_pid"]), []).append(int(process["pid"]))

    descendants: set[int] = set()
    stack = list(children_by_parent.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        stack.extend(children_by_parent.get(pid, []))
    return descendants
