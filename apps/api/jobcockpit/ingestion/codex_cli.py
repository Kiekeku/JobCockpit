from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobcockpit.ingestion.process_tree import run_with_process_tree_cleanup


Runner = Callable[..., subprocess.CompletedProcess]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexCliRunResult:
    stdout: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0


def codex_exec_args(
    command: str,
    *,
    search: bool = False,
    workspace: str | Path | None = None,
    ignore_user_config: bool = True,
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox: str = "read-only",
    prompt_arg: str | None = None,
    config_overrides: dict[str, Any] | None = None,
    ephemeral: bool = False,
    skip_git_repo_check: bool = False,
) -> list[str]:
    args = [command]
    if search:
        args.append("--search")
    args.extend(["-a", "never", "exec"])
    if model is not None:
        args.extend(["--model", model])
    if reasoning_effort is not None:
        args.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    if ignore_user_config:
        args.append("--ignore-user-config")
    if workspace is not None:
        args.extend(["--cd", str(workspace)])
    args.extend(["--sandbox", sandbox])
    for key, value in (config_overrides or {}).items():
        args.extend(["--config", f"{key}={json.dumps(value)}"])
    if ephemeral:
        args.append("--ephemeral")
    if skip_git_repo_check:
        args.append("--skip-git-repo-check")
    if prompt_arg is not None:
        args.append(prompt_arg)
    return args


def codex_subprocess_text_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "encoding": "utf-8",
        "errors": "replace",
        "text": True,
    }
    kwargs.update(overrides)
    return kwargs


def run_codex_prompt(
    command: str,
    *,
    runner: Runner | None = None,
    prompt: str,
    timeout_seconds: int = 120,
    search: bool = False,
    workspace: str | Path | None = None,
    ignore_user_config: bool = True,
    model: str | None = None,
    reasoning_effort: str | None = None,
    prompt_as_stdin: bool = True,
    unavailable_error: str = "codex_cli_unavailable",
    timeout_error: str = "codex_cli_timeout",
    failure_prefix: str = "codex_cli_failed",
    operation: str = "codex_prompt",
    config_overrides: dict[str, Any] | None = None,
    ephemeral: bool = False,
    skip_git_repo_check: bool = False,
) -> CodexCliRunResult:
    started_at = time.monotonic()

    def finish(*, stdout: str = "", error: str = "") -> CodexCliRunResult:
        elapsed_seconds = time.monotonic() - started_at
        logger.info(
            "Codex CLI operation=%s elapsed_seconds=%.3f model=%s reasoning_effort=%s "
            "search=%s outcome=%s",
            operation,
            elapsed_seconds,
            model or "default",
            reasoning_effort or "default",
            search,
            "error" if error else "succeeded",
        )
        return CodexCliRunResult(
            stdout=stdout,
            error=error,
            elapsed_seconds=elapsed_seconds,
        )

    kwargs = codex_subprocess_text_kwargs(timeout=timeout_seconds)
    if prompt_as_stdin:
        kwargs["input"] = prompt
    effective_runner = (
        run_with_process_tree_cleanup
        if runner is None or runner is subprocess.run
        else runner
    )
    try:
        completed = effective_runner(
            codex_exec_args(
                command,
                search=search,
                workspace=workspace,
                ignore_user_config=ignore_user_config,
                model=model,
                reasoning_effort=reasoning_effort,
                prompt_arg="-" if prompt_as_stdin else prompt,
                config_overrides=config_overrides,
                ephemeral=ephemeral,
                skip_git_repo_check=skip_git_repo_check,
            ),
            **kwargs,
        )
    except OSError as error:
        return finish(error=f"{unavailable_error}: {error}")
    except subprocess.TimeoutExpired:
        return finish(error=timeout_error)
    if completed.returncode != 0:
        return finish(error=codex_cli_failure_message(completed, prefix=failure_prefix))
    return finish(stdout=str(completed.stdout or ""))


def json_object_from_output(output: str) -> dict[str, Any] | None:
    text = output.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, json.JSONDecodeError, ValueError):
            return None
    return payload if isinstance(payload, dict) else None


def json_array_from_output(output: str) -> list[Any] | None:
    text = output.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError, ValueError):
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, json.JSONDecodeError, ValueError):
            return None
    return payload if isinstance(payload, list) else None


def codex_cli_failure_message(
    completed: subprocess.CompletedProcess,
    *,
    prefix: str = "codex_cli_failed",
) -> str:
    error_text = str((completed.stderr or completed.stdout or "")).strip()
    if error_text:
        return f"{prefix}: {' '.join(error_text.split())[:240]}"
    return f"{prefix}: exit_code_{completed.returncode}"
