import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def run_test_launcher(tmp_path):
    record_path = tmp_path / "launches.json"
    preload = tmp_path / "record-test-launches.mjs"
    preload.write_text(
        """
import childProcess from "node:child_process";
import { EventEmitter } from "node:events";
import { writeFileSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";

const calls = [];
function record(kind, command, args) {
  calls.push({ kind, command, args });
  writeFileSync(process.env.TEST_LAUNCH_RECORD, JSON.stringify(calls), "utf8");
}
childProcess.spawnSync = (command, args) => {
  record("build", command, args);
  return { status: 0 };
};
childProcess.spawn = (command, args) => {
  record("pytest", command, args);
  const child = new EventEmitter();
  process.nextTick(() => child.emit(
    "exit", Number(process.env.TEST_CHILD_EXIT || 0), process.env.TEST_CHILD_SIGNAL || null,
  ));
  return child;
};
syncBuiltinESMExports();
""",
        encoding="utf-8",
    )

    def run(*args, child_exit=0, child_signal=""):
        env = {
            **os.environ,
            "TEST_LAUNCH_RECORD": str(record_path),
            "TEST_CHILD_EXIT": str(child_exit),
            "TEST_CHILD_SIGNAL": child_signal,
            "PYTEST_XDIST_WORKERS": "3",
        }
        result = subprocess.run(
            ["node", "--import", preload.as_uri(), "scripts/test.mjs", *args],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        calls = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else []
        return result, calls

    return run


def test_focused_launcher_forwards_files_and_parameterized_node_ids(run_test_launcher):
    selectors = [
        "tests/test_web_dashboard_static.py",
        "tests/test_employment_timing_matrix.py::"
        "test_employment_timing_evidence_matrix[typed_end_without_start]",
    ]
    result, calls = run_test_launcher("--", "--fast", "--serial", *selectors)

    assert result.returncode == 0, result.stderr
    assert [call["kind"] for call in calls] == ["pytest"]
    args = calls[0]["args"]
    pytest_args = args[args.index("pytest") + 1:]
    assert args[-len(selectors):] == selectors
    assert pytest_args[pytest_args.index("-m") + 1] == "not slow"
    assert "-n" not in args
    assert "--no-sync" in args
    assert "no:cacheprovider" in args


def test_focused_launcher_keeps_extension_build_and_worker_override(run_test_launcher):
    selector = "tests/test_extension_submit_guard.py"
    result, calls = run_test_launcher(selector, "--slow")

    assert result.returncode == 0, result.stderr
    assert [call["kind"] for call in calls] == ["build", "pytest"]
    assert calls[0]["args"] == ["scripts/build-extension.mjs"]
    args = calls[1]["args"]
    pytest_args = args[args.index("pytest") + 1:]
    assert args[-1] == selector
    assert args[args.index("-n") + 1] == "3"
    assert pytest_args[pytest_args.index("-m") + 1] == "slow"


def test_default_launcher_still_runs_the_complete_suite(run_test_launcher):
    result, calls = run_test_launcher()

    assert result.returncode == 0, result.stderr
    assert [call["kind"] for call in calls] == ["build", "pytest"]
    args = calls[1]["args"]
    assert args[args.index("-n") + 1] == "3"
    assert "-m" not in args[args.index("pytest") + 1:]
    assert not any(arg.startswith("tests/") for arg in args)


@pytest.mark.parametrize(
    "args",
    [
        ("--unknown",),
        ("--fast", "--slow"),
        ("tests/test_does_not_exist.py",),
        ("tests/../scripts/parser_corpus_diff.py",),
        ("scripts/parser_corpus_diff.py",),
        ("tests",),
    ],
)
def test_launcher_rejects_invalid_selection_before_launching(run_test_launcher, args):
    result, calls = run_test_launcher(*args)

    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert calls == []


@pytest.mark.parametrize("exit_code,signal,expected", [(7, "", 7), (0, "SIGTERM", 1)])
def test_focused_launcher_propagates_pytest_failure(run_test_launcher, exit_code, signal, expected):
    result, calls = run_test_launcher(
        "--fast", "--serial", "tests/test_web_dashboard_static.py",
        child_exit=exit_code, child_signal=signal,
    )

    assert result.returncode == expected
    assert [call["kind"] for call in calls] == ["pytest"]
