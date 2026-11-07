import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher_repo(tmp_path):
    repository = tmp_path / "repository with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    for name in ("cli.mjs", "env.mjs", "python-runtime.mjs", "python.mjs",
                 "run_ingestion_task.ps1", "run_publication_worker.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    package = repository / "jobcockpit"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args':sys.argv[1:], 'python':sys.executable, "
        "'value':os.environ.get('LAUNCHER_TEST_VALUE')}))\n"
        "print('test diagnostic', file=sys.stderr)\n"
        "if os.environ.get('LAUNCHER_TEST_HEARTBEAT'):\n"
        " import pathlib, time\n"
        " for _ in range(300):\n"
        "  pathlib.Path(os.environ['LAUNCHER_TEST_HEARTBEAT']).write_text(str(time.time()))\n"
        "  time.sleep(.1)\n"
        "sys.exit(int(os.environ.get('LAUNCHER_TEST_EXIT', '0')))\n"
    )
    (repository / ".env").write_text(
        f'UV_PROJECT_ENVIRONMENT="{Path(sys.prefix).as_posix()}"\n'
        'LAUNCHER_TEST_VALUE="value with spaces"\n'
    )
    env = os.environ.copy()
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("LAUNCHER_TEST_VALUE", None)
    return repository, env


def test_cli_uses_configured_runtime_and_preserves_arguments_and_exit(launcher_repo):
    repository, env = launcher_repo
    env["LAUNCHER_TEST_EXIT"] = "23"
    arguments = ["gmail", "process", "--source-id", "source with spaces", "--search-text="]
    result = subprocess.run(
        ["node", str(repository / "scripts/cli.mjs"), *arguments],
        cwd=repository.parent, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 23
    payload = json.loads(result.stdout)
    assert payload["args"] == arguments
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["value"] == "value with spaces"
    assert "test diagnostic" in result.stderr


def test_cli_honors_shell_override_and_does_not_create_missing_environment(launcher_repo):
    repository, env = launcher_repo
    missing = repository / "missing environment"
    env["UV_PROJECT_ENVIRONMENT"] = str(missing)
    result = subprocess.run(
        ["node", str(repository / "scripts/cli.mjs"), "--help"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert str(missing) in result.stderr
    assert not missing.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler wrapper")
@pytest.mark.parametrize("exit_code", [0, 23])
def test_scheduled_launcher_preserves_arguments_logs_and_status(launcher_repo, exit_code):
    repository, env = launcher_repo
    env["LAUNCHER_TEST_EXIT"] = str(exit_code)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(repository / "scripts/run_ingestion_task.ps1"), "-TaskKey", "test-ingestion",
         "-UvArguments", 'run jobcockpit gmail process --source-id "source with spaces" '
         '--search-text= --json'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == exit_code, result.stderr
    summary = json.loads((repository / "var/scheduled-tasks/test-ingestion/latest.summary.json")
                         .read_text(encoding="utf-8-sig"))
    assert summary["exit_code"] == exit_code
    assert summary["succeeded"] is (exit_code == 0)
    payload = json.loads(Path(summary["stdout_log"]).read_text())
    assert payload["args"] == ["gmail", "process", "--source-id", "source with spaces",
                               "--search-text=", "--json"]
    assert "test diagnostic" in Path(summary["stderr_log"]).read_text()


def test_cli_installs_tls_workaround_before_dispatch(monkeypatch):
    from jobcockpit import cli

    events = []
    monkeypatch.setattr(cli, "install_windows_certifi_workaround", lambda: events.append("tls"))

    def process(_args, _settings):
        events.append("process")
        return 0

    monkeypatch.setattr(cli, "process_gmail_command", process)
    assert cli.main(["gmail", "process", "--json"], settings=object()) == 0
    assert events == ["tls", "process"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scheduled process tree")
def test_ingestion_timeout_stops_child_and_records_failure(launcher_repo):
    repository, env = launcher_repo
    heartbeat = repository / "heartbeat"
    env["LAUNCHER_TEST_HEARTBEAT"] = str(heartbeat)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(repository / "scripts/run_ingestion_task.ps1"), "-TaskKey", "timeout-test",
         "-UvArguments", "run jobcockpit gmail process --json", "-MaxRuntimeSeconds", "2"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 124, result.stderr
    summary = json.loads((repository / "var/scheduled-tasks/timeout-test/latest.summary.json")
                         .read_text(encoding="utf-8-sig"))
    assert summary["exit_code"] == 124
    assert summary["succeeded"] is False
    assert "runtime limit" in summary["launch_error"]
    before = heartbeat.read_text()
    time.sleep(.3)
    assert heartbeat.read_text() == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scheduled worker")
@pytest.mark.parametrize("exit_code", [0, 23])
def test_publication_worker_uses_configured_runtime_and_preserves_exit(launcher_repo, exit_code):
    repository, env = launcher_repo
    env["LAUNCHER_TEST_EXIT"] = str(exit_code)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(repository / "scripts/run_publication_worker.ps1")],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == exit_code, result.stderr
    log_dir = repository / "var/logs/publication"
    payload = json.loads(next(log_dir.glob("*.stdout.log")).read_text())
    assert payload["args"] == ["worker", "publication"]
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert "test diagnostic" in next(log_dir.glob("*.stderr.log")).read_text()


def test_stop_worker_matching_requires_this_repository_and_worker_command(tmp_path):
    launcher = str(tmp_path / "repo with spaces/scripts/cli.mjs")
    other = str(tmp_path / "other/scripts/cli.mjs")
    commands = [
        f'node "{launcher}" worker publication',
        f'node "{launcher}" worker source',
        f'node "{launcher}" gmail process --source-id worker',
        f'node "{other}" worker publication',
        'node scripts/cli.mjs worker publication',
        f'python.exe -X "jobcockpit_root={Path(launcher).parent.parent}" -m jobcockpit.cli worker source',
        f'python.exe -X "jobcockpit_root={Path(other).parent.parent}" -m jobcockpit.cli worker source',
        f'python.exe -X "jobcockpit_root={Path(launcher).parent.parent}" -m jobcockpit.cli gmail process',
    ]
    program = (
        f'import {{isRepositoryCliWorker}} from '
        f'{json.dumps((ROOT / "scripts/cli-processes.mjs").as_uri())};\n'
        f'console.log(JSON.stringify({json.dumps(commands)}.map(command => '
        f'isRepositoryCliWorker(command, {json.dumps(launcher)}))));'
    )
    result = subprocess.run(["node", "--input-type=module", "-e", program],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == [True, True, False, False, False, True, False, False]


def test_python_launcher_uses_same_environment_without_sync(launcher_repo):
    repository, env = launcher_repo
    result = subprocess.run(
        ["node", str(repository / "scripts/python.mjs"), "-c",
         "import sys; print(sys.executable); sys.exit(17)"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 17
    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()


def test_runtime_only_env_loading_does_not_import_service_settings(launcher_repo):
    repository, env = launcher_repo
    (repository / ".env").write_text('UV_PROJECT_ENVIRONMENT=chosen-runtime\n'
                                     'JOBCOCKPIT_PUBLICATION_ENABLED=true\n')
    env.pop("JOBCOCKPIT_PUBLICATION_ENABLED", None)
    program = (
        f'import {{loadDotEnv}} from {json.dumps((repository / "scripts/env.mjs").as_uri())};'
        'loadDotEnv(".env", ["UV_PROJECT_ENVIRONMENT", "UV_PYTHON"]);'
        'console.log(JSON.stringify([process.env.UV_PROJECT_ENVIRONMENT,'
        'process.env.JOBCOCKPIT_PUBLICATION_ENABLED ?? null]));'
    )
    result = subprocess.run(["node", "--input-type=module", "-e", program],
                            cwd=repository, env=env, capture_output=True, text=True,
                            check=True, timeout=10)
    assert json.loads(result.stdout) == ["chosen-runtime", None]
