from __future__ import annotations

import subprocess

import pytest

from jobcockpit.ingestion import process_tree


def test_process_tree_runner_kills_tree_on_timeout(monkeypatch):
    killed_calls = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self, args, **kwargs):
            self.args = args
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if timeout is not None and not killed_calls:
                raise subprocess.TimeoutExpired(self.args, timeout=timeout)
            return "partial stdout", "partial stderr"

    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: set())
    monkeypatch.setattr(process_tree, "kill_process_tree", lambda pid, force=True: killed_calls.append((pid, force)))

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        process_tree.run_with_process_tree_cleanup(
            ["codex", "exec"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=0,
        )

    assert killed_calls == [(4242, False)]
    assert exc_info.value.output == "partial stdout"
    assert exc_info.value.stderr == "partial stderr"


def test_process_tree_runner_force_kills_when_graceful_cleanup_hangs(monkeypatch):
    killed_calls = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self, args, **kwargs):
            self.args = args
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            raise subprocess.TimeoutExpired(
                self.args,
                timeout=timeout,
                output=f"stdout {self.calls}",
                stderr=f"stderr {self.calls}",
            )

    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: set())
    monkeypatch.setattr(process_tree, "kill_process_tree", lambda pid, force=True: killed_calls.append((pid, force)))

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        process_tree.run_with_process_tree_cleanup(
            ["codex", "exec"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=0,
            cleanup_timeout=0,
        )

    assert killed_calls == [(4242, False), (4242, True)]
    assert exc_info.value.output is None
    assert exc_info.value.stderr is None


def test_process_tree_runner_starts_posix_process_group(monkeypatch):
    popen_kwargs = {}

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            popen_kwargs.update(kwargs)

        def communicate(self, timeout=None):
            return "stdout", "stderr"

    monkeypatch.setattr(process_tree.os, "name", "posix")
    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: set())

    completed = process_tree.run_with_process_tree_cleanup(
        ["codex", "exec"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert popen_kwargs["start_new_session"] is True


def test_process_tree_runner_sends_subprocess_run_input_through_communicate(monkeypatch):
    popen_kwargs = {}
    communicate_calls = []

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            popen_kwargs.update(kwargs)

        def communicate(self, input=None, timeout=None):
            communicate_calls.append((input, timeout))
            return "stdout", "stderr"

    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: set())

    completed = process_tree.run_with_process_tree_cleanup(
        ["codex", "exec", "-"],
        input="Prompt body",
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "input" not in popen_kwargs
    assert popen_kwargs["stdin"] == subprocess.PIPE
    assert communicate_calls == [("Prompt body", 5.0)]

def test_posix_kill_process_tree_terminates_process_group(monkeypatch):
    killed_groups = []
    killed_pids = []

    monkeypatch.setattr(process_tree.os, "name", "posix")
    monkeypatch.setattr(process_tree.os, "killpg", lambda pid, sig: killed_groups.append((pid, sig)), raising=False)
    monkeypatch.setattr(process_tree.os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))

    process_tree.kill_process_tree(4242, force=False)

    assert killed_groups == [(4242, process_tree.signal.SIGTERM)]
    assert killed_pids == []


def test_process_tree_runner_cleans_observed_playwright_descendants(monkeypatch):
    cleaned_pids = []

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            pass

        def communicate(self, timeout=None):
            return "stdout", "stderr"

    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: {5001})
    monkeypatch.setattr(
        process_tree,
        "cleanup_owned_playwright_processes",
        lambda pids: cleaned_pids.extend(sorted(pids)) or len(pids),
    )

    completed = process_tree.run_with_process_tree_cleanup(
        ["codex", "exec"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert cleaned_pids == [5001]


def test_process_tree_runner_cleans_descendants_created_during_final_wait(monkeypatch):
    cleaned_pids = []
    scan_results = iter([set(), {5002}])

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            pass

        def communicate(self, timeout=None):
            return "stdout", "stderr"

    monkeypatch.setattr(process_tree.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(process_tree, "_owned_playwright_descendant_pids", lambda pid: next(scan_results))
    monkeypatch.setattr(
        process_tree,
        "cleanup_owned_playwright_processes",
        lambda pids: cleaned_pids.extend(sorted(pids)) or len(pids),
    )

    completed = process_tree.run_with_process_tree_cleanup(
        ["codex", "exec"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert cleaned_pids == [5002]


def test_owned_playwright_descendant_discovery_ignores_unrelated_processes(monkeypatch):
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree,
        "_windows_processes",
        lambda: [
            {"pid": 100, "parent_pid": 0, "name": "codex.exe", "command": "codex exec", "created_at": ""},
            {"pid": 101, "parent_pid": 100, "name": "node.exe", "command": "node helper", "created_at": ""},
            {
                "pid": 102,
                "parent_pid": 101,
                "name": "node.exe",
                "command": "node @playwright/mcp",
                "created_at": "",
            },
            {
                "pid": 200,
                "parent_pid": 999,
                "name": "chrome.exe",
                "command": "chrome --user-data-dir=d:\\Temp\\playwright_chromiumdev_profile-other",
                "created_at": "",
            },
        ],
    )

    assert process_tree._owned_playwright_descendant_pids(100) == {102}
