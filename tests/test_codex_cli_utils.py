from __future__ import annotations

import subprocess

from jobcockpit.ingestion import codex_cli
from jobcockpit.ingestion.codex_cli import (
    codex_cli_failure_message,
    json_array_from_output,
    json_object_from_output,
    run_codex_prompt,
)


def test_run_codex_prompt_uses_process_tree_runner_by_default(monkeypatch):
    calls = []

    def process_tree_runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(
        codex_cli,
        "run_with_process_tree_cleanup",
        process_tree_runner,
        raising=False,
    )

    result = codex_cli.run_codex_prompt("codex-test", prompt="Prompt body")

    assert result.stdout == '{"ok": true}'
    assert calls[0][1]["input"] == "Prompt body"

def test_json_object_from_output_accepts_wrapped_codex_output():
    assert json_object_from_output('notes before {"answer": 42} notes after') == {"answer": 42}


def test_json_array_from_output_accepts_wrapped_codex_output():
    assert json_array_from_output('prefix [{"url": "https://example.test"}] suffix') == [
        {"url": "https://example.test"}
    ]


def test_codex_cli_failure_message_normalizes_output_and_allows_prefix():
    completed = subprocess.CompletedProcess(
        ["codex"],
        2,
        stdout="",
        stderr="first line\nsecond line",
    )

    assert codex_cli_failure_message(completed, prefix="codex_playwright_failed") == (
        "codex_playwright_failed: first line second line"
    )


def test_run_codex_prompt_supports_stdin_prompt_and_reports_errors():
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='{"ok": true}', stderr="")

    result = run_codex_prompt(
        "codex-test",
        runner=runner,
        prompt="Prompt body",
        timeout_seconds=7,
        workspace="E:/workspace",
        ignore_user_config=False,
        model="gpt-6-luna",
        reasoning_effort="max",
        prompt_as_stdin=True,
    )

    assert result.stdout == '{"ok": true}'
    assert result.error == ""
    args, kwargs = calls[0]
    assert args == [
        "codex-test",
        "-a",
        "never",
        "exec",
        "--model",
        "gpt-6-luna",
        "--config",
        'model_reasoning_effort="max"',
        "--cd",
        "E:/workspace",
        "--sandbox",
        "read-only",
        "-",
    ]
    assert args[-1] == "-"
    assert kwargs["input"] == "Prompt body"
    assert kwargs["timeout"] == 7


def test_run_codex_prompt_supports_prompt_argument_mode():
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    result = run_codex_prompt(
        "codex-test",
        runner=runner,
        prompt="Search prompt",
        timeout_seconds=9,
        search=True,
        prompt_as_stdin=False,
    )

    assert result.error == ""
    assert "--search" in calls[0][0]
    assert calls[0][0][-1] == "Search prompt"
    assert "input" not in calls[0][1]


def test_run_codex_prompt_converts_runner_failures_to_error_codes():
    def unavailable_runner(args, **kwargs):
        raise OSError("missing")

    def timeout_runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 3)

    assert run_codex_prompt("codex-test", runner=unavailable_runner, prompt="x").error.startswith(
        "codex_cli_unavailable:"
    )
    assert run_codex_prompt("codex-test", runner=timeout_runner, prompt="x").error == "codex_cli_timeout"
