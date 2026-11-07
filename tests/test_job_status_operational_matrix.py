import os
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from jobcockpit.core.config import Settings
from jobcockpit.db.session import get_engine, init_database
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    ExternalPostingHint,
    PostingResolution,
    PrioritizedRenderedPageResolver,
    ResolvedExternalPosting,
    _codex_rendered_page_prompt,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _runner_test_tools() -> tuple[str, Path]:
    powershell = shutil.which("powershell.exe")
    csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not powershell or not csc.is_file():
        pytest.skip("Windows PowerShell and the .NET Framework compiler are required")
    return powershell, csc


def _prepare_runner_repository(
    tmp_path: Path,
    *,
    compile_stub: bool = True,
) -> tuple[Path, str]:
    powershell, csc = _runner_test_tools()
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    executable_directory = repository / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    executable_directory.mkdir(parents=True)
    shutil.copyfile(
        REPOSITORY_ROOT / "scripts" / "run_job_status_reconciliation.ps1",
        scripts / "run_job_status_reconciliation.ps1",
    )
    executable = executable_directory / "jobcockpit.exe"
    if compile_stub:
        source = repository / "RunnerStub.cs"
        source.write_text(
            r"""
using System;
using System.Threading;

public static class Program
{
    public static int Main()
    {
        string mode = Environment.GetEnvironmentVariable("JOBCOCKPIT_RUNNER_TEST_MODE");
        if (mode == "success_stderr")
        {
            Console.Error.WriteLine("benign warning");
            Console.Out.WriteLine(
                "{\"run_id\":\"status_scan_success\",\"status\":\"completed\"," +
                "\"selected\":3,\"checked\":3,\"open\":2,\"closed\":1," +
                "\"expired\":0,\"inconclusive\":0,\"failed\":0," +
                "\"deferred\":0,\"skipped\":0}"
            );
            return 0;
        }
        if (mode == "failure_stderr")
        {
            Console.Error.WriteLine(
                "{\"run_id\":\"status_scan_failed\",\"status\":\"failed\"," +
                "\"selected\":3,\"checked\":2,\"open\":1,\"closed\":0," +
                "\"expired\":0,\"inconclusive\":0,\"failed\":1," +
                "\"deferred\":0,\"skipped\":0,\"error\":\"durable failure\"}"
            );
            return 1;
        }
        if (mode == "sleep")
        {
            Thread.Sleep(60000);
            return 0;
        }
        return 2;
    }
}
""".strip(),
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(csc),
                "/nologo",
                f"/out:{executable}",
                str(source),
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    else:
        executable.write_text("not a Windows executable", encoding="utf-8")
    # Exercise the scheduled runner's Node boundary while retaining the native
    # stub's success, failure, and timeout behavior in its child process tree.
    (scripts / "cli.mjs").write_text(
        'import { spawn } from "node:child_process";\n'
        f'const child = spawn({json.dumps(str(executable))}, process.argv.slice(2), '
        '{stdio: "inherit", windowsHide: true});\n'
        'child.on("error", error => { console.error(error.message); process.exitCode = 1; });\n'
        'child.on("exit", code => { process.exitCode = code ?? 1; });\n',
        encoding="utf-8",
    )
    return repository, powershell


def _runner_log_record(repository: Path) -> dict:
    log_files = list((repository / "var" / "logs" / "job-status-reconciliation").glob("*.jsonl"))
    assert len(log_files) == 1
    records = [
        json.loads(line)
        for line in log_files[0].read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    return records[0]


class _PlaywrightContextManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Chromium:
    def __init__(self, browser):
        self.browser = browser

    def connect_over_cdp(self, _endpoint_url, **_kwargs):
        return self.browser


class _Playwright:
    def __init__(self, browser):
        self.chromium = _Chromium(browser)


class _Page:
    def __init__(self, url: str, *, title: str = "", body: str = ""):
        self.url = url
        self._title = title
        self._body = body
        self.closed = False
        self.popup_handlers = []
        self.hardening_verified = False

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def title(self):
        return self._title

    def locator(self, selector):
        assert selector == "body"
        return type(
            "_Locator",
            (),
            {"inner_text": lambda _self, timeout=None: self._body},
        )()

    def text_content(self, selector, **_kwargs):
        assert selector == "body"
        return self._body

    def on(self, event, handler):
        if event == "popup":
            self.popup_handlers.append(handler)

    def evaluate(self, _expression):
        self.hardening_verified = True
        return True

    def close(self, **_kwargs):
        self.closed = True


def _alembic_config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_revision_0012_survives_legacy_init_database_before_upgrade(tmp_path: Path):
    database_path = tmp_path / "legacy-before-status-reconciliation.db"
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        artifact_root=tmp_path / "private" / "storage",
    )
    config = _alembic_config(settings.database_url)
    command.upgrade(config, "20260727_0012")

    try:
        # Gmail processing, unresolved-link retry, the reprocess worker, and
        # setup_gmail_ingestion historically enter through this shared helper.
        init_database(settings)
        command.upgrade(config, "head")

        with sqlite3.connect(database_path) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            job_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(job_postings)").fetchall()
            }
            check_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(job_status_checks)").fetchall()
            }

        assert revision == "20260913_0032"
        assert {"job_status_scan_runs", "job_status_checks"} <= tables
        assert {
            "status_last_checked_at",
            "status_next_check_at",
            "status_check_result",
            "status_check_evidence_code",
            "status_check_failure_count",
            "status_check_not_found_count",
            "status_last_open_at",
            "status_last_open_evidence_code",
            "status_source_repair_required",
        } <= job_columns
        assert {"requested_url", "final_url", "candidate_kind", "diagnostics"} <= check_columns
    finally:
        get_engine(settings).dispose()


def test_chrome_cdp_closes_new_context_when_new_page_creation_fails():
    class FailingNewPageContext:
        def __init__(self):
            self.closed = False
            self.init_script_installed = False
            self.page_handlers = []

        def add_init_script(self, *, script):
            assert "RTCPeerConnection" in script
            self.init_script_installed = True

        def new_page(self):
            assert self.init_script_installed is True
            raise RuntimeError("new page failed")

        def on(self, event, handler):
            if event == "page":
                self.page_handlers.append(handler)

        def remove_listener(self, event, handler):
            if event == "page" and handler in self.page_handlers:
                self.page_handlers.remove(handler)

        def route(self, *_args):
            return None

        def unroute(self, *_args):
            return None

        def route_web_socket(self, *_args):
            return None

        def close(self):
            self.closed = True

    context = FailingNewPageContext()

    class Browser:
        contexts = []

        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            assert kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
            return context

    playwright = _Playwright(Browser())
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=1,
        retries=0,
        playwright_factory=lambda: _PlaywrightContextManager(playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4425912315/",
            source_kind="linkedin",
            company="Example",
            title="Software Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert "new page failed" in resolution.error
    assert context.closed is True


def test_chrome_cdp_first_closes_only_its_owned_context_before_fallback():
    source_url = "https://www.linkedin.com/jobs/view/4425912315/"
    user_page = _Page("https://mail.example.test/inbox", title="Inbox", body="Private mail")
    validation_page = _Page(source_url)

    class ExistingContext:
        def __init__(self):
            self.pages = [user_page]
            self.closed = False

        def cookies(self, urls):
            assert urls == [source_url]
            return []

        def close(self):
            self.closed = True

    class ValidationContext:
        def __init__(self):
            self.pages = [validation_page]
            self.closed = False
            self.init_script_installed = False

            self.page_handlers = []
        def add_init_script(self, *, script):
            assert "webkitRTCPeerConnection" in script
            self.init_script_installed = True

        def route(self, *_args):
            return None

        def unroute(self, *_args):
            return None

        def route_web_socket(self, *_args):
            return None

        def new_page(self):
            assert self.init_script_installed is True
            for handler in list(self.page_handlers):
                handler(validation_page)
            return validation_page

        def on(self, event, handler):
            if event == "page":
                self.page_handlers.append(handler)

        def remove_listener(self, event, handler):
            if event == "page" and handler in self.page_handlers:
                self.page_handlers.remove(handler)

        def new_cdp_session(self, page):
            assert page is validation_page

            class Session:
                def send(self, command, params=None):
                    if command == "Target.getTargetInfo":
                        return {"targetInfo": {"targetId": "validation-target"}}
                    if command == "Target.closeTarget":
                        assert params == {"targetId": "validation-target"}
                        validation_page.close()
                        return {"success": True}
                    raise AssertionError(command)

                def detach(self):
                    return None

            return Session()

        def close(self):
            validation_page.close()
            self.closed = True

    source_context = ExistingContext()
    validation_context = ValidationContext()

    class Browser:
        contexts = [source_context]

        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            assert kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
            return validation_context

    primary = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=1,
        retries=0,
        playwright_factory=lambda: _PlaywrightContextManager(_Playwright(Browser())),
    )

    class Fallback:
        def __init__(self):
            self.called = False

        def resolve(self, _hint):
            self.called = True
            assert validation_page.closed is True
            assert user_page.closed is False
            assert source_context.closed is False
            assert validation_context.closed is True
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=source_url,
                    raw_text=("Software Intern\nCompany: Example\nBuild production software."),
                    confidence=0.95,
                    evidence="fallback",
                ),
            )

    fallback = Fallback()
    resolver = PrioritizedRenderedPageResolver(
        primary_resolver=primary,
        fallback_resolver=fallback,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="Example",
            title="Software Intern",
        )
    )

    assert resolution.status == "resolved"
    assert fallback.called is True
    assert validation_page.closed is True
    assert validation_page.hardening_verified is True
    assert user_page.closed is False
    assert source_context.closed is False
    assert validation_context.closed is True


def test_codex_chrome_fallback_prompt_owns_and_closes_only_its_tabs():
    prompt = _codex_rendered_page_prompt(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4425912315/",
            source_kind="linkedin",
            company="Example",
            title="Software Intern",
        )
    ).casefold()

    assert "open a new tab" in prompt
    assert "close every tab created by this validation" in prompt
    assert "including on an error or timeout" in prompt
    assert "never close or navigate any pre-existing tab" in prompt


def test_runner_logs_missing_cli_failure_as_jsonl(tmp_path: Path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required for the task-runner contract")

    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    runner = scripts / "run_job_status_reconciliation.ps1"
    shutil.copyfile(
        REPOSITORY_ROOT / "scripts" / "run_job_status_reconciliation.ps1",
        runner,
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    log_files = list((repository / "var" / "logs" / "job-status-reconciliation").glob("*.jsonl"))
    assert len(log_files) == 1
    records = [
        json.loads(line)
        for line in log_files[0].read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["exit_code"] != 0
    assert records[0]["result"]["status"] == "failed"
    assert records[0]["result"]["error"]


def test_runner_ignores_benign_stderr_when_cli_succeeds(tmp_path: Path):
    repository, powershell = _prepare_runner_repository(tmp_path)
    environment = os.environ.copy()
    environment["JOBCOCKPIT_RUNNER_TEST_MODE"] = "success_stderr"

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "scripts" / "run_job_status_reconciliation.ps1"),
            "-MaxRuntimeSeconds",
            "10",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    record = _runner_log_record(repository)
    assert record["exit_code"] == 0
    assert record["result"]["run_id"] == "status_scan_success"
    assert record["result"]["status"] == "completed"
    assert record["result"]["open"] == 2
    assert "benign warning" not in json.dumps(record)


def test_runner_preserves_failure_json_from_stderr_and_native_exit(
    tmp_path: Path,
):
    repository, powershell = _prepare_runner_repository(tmp_path)
    environment = os.environ.copy()
    environment["JOBCOCKPIT_RUNNER_TEST_MODE"] = "failure_stderr"

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "scripts" / "run_job_status_reconciliation.ps1"),
            "-MaxRuntimeSeconds",
            "10",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    record = _runner_log_record(repository)
    assert record["exit_code"] == 1
    assert record["result"]["run_id"] == "status_scan_failed"
    assert record["result"]["selected"] == 3
    assert record["result"]["checked"] == 2
    assert record["result"]["failed"] == 1
    assert record["result"]["error"] == "durable failure"


def test_runner_hard_timeout_kills_child_and_logs_stable_result(tmp_path: Path):
    repository, powershell = _prepare_runner_repository(tmp_path)
    environment = os.environ.copy()
    environment["JOBCOCKPIT_RUNNER_TEST_MODE"] = "sleep"
    started = time.monotonic()

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "scripts" / "run_job_status_reconciliation.ps1"),
            "-MaxRuntimeSeconds",
            "1",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 124
    assert elapsed < 15
    record = _runner_log_record(repository)
    assert record["exit_code"] == 124
    assert record["result"]["status"] == "failed"
    assert record["result"]["error_code"] == "runner_timeout"
    assert record["result"]["checked"] == 0


def test_runner_logs_structured_failure_when_launcher_child_cannot_start(tmp_path: Path):
    repository, powershell = _prepare_runner_repository(
        tmp_path,
        compile_stub=False,
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "scripts" / "run_job_status_reconciliation.ps1"),
            "-MaxRuntimeSeconds",
            "1",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode != 0
    record = _runner_log_record(repository)
    assert record["exit_code"] != 0
    assert record["result"]["status"] == "failed"
    assert record["result"]["error_code"] == "invalid_cli_json"
    assert record["result"]["error"]


def test_task_installer_preflights_repository_cli_before_registration():
    installer = (
        REPOSITORY_ROOT / "scripts" / "install_job_status_reconciliation_task.ps1"
    ).read_text(encoding="utf-8")

    executable_reference = installer.find("scripts\\cli.mjs")
    executable_preflight = installer.find("Test-Path -LiteralPath $jobCockpit")
    registration = installer.find("Register-ScheduledTask")

    assert 0 <= executable_reference < executable_preflight < registration
    assert executable_preflight < installer.find("runtime preflight failed") < registration
