import json
from pathlib import Path
import subprocess

import pytest


def test_package_exposes_stop_script():
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["stop"] == "node scripts/stop.mjs"


def test_stop_script_uses_dev_ports_and_supports_dry_run():
    script = Path("scripts/stop.mjs").read_text(encoding="utf-8")

    assert "Usage: pnpm stop" in script
    assert "--dry-run" in script
    assert 'PID_FILE = "var/dev-pids.json"' in script
    assert "readPidFile" in script
    assert 'envPort("JOBCOCKPIT_API_PORT", 8000)' in script
    assert 'envPort("JOBCOCKPIT_WEB_PORT", 3000)' in script
    assert 'envPort("JOBCOCKPIT_ATS_MOCK_PORT", 8001)' in script
    assert "taskkill" in script
    assert "netstat" in script
    assert "configuredListeners" in script
    assert "verifyPidFileServices" in script
    assert "verifiedPidFileListeners" in script
    assert "verifyConfiguredListeners" in script
    assert "processLooksLikeJobCockpitService" in script
    assert "jobcockpit worker reprocess" in script
    assert "processCommandLine" in script
    assert "servicesForListeners" in script
    assert "ports.length === 0" in script
    assert 'commandLine.includes(String(service.port))' not in script
    assert "parentProcessId" in script
    assert "not verified; skipping" in script
    assert "still listening" in script
    assert "pnpm dev -- --next-ports" in script
    assert "one or more JobCockpit dev services are still active" in script


def test_stop_script_includes_only_repository_cli_workers():
    script = Path("scripts/stop.mjs").read_text(encoding="utf-8")

    assert "repositoryCliPath" in script
    assert 'resolve(process.cwd(), ".venv", "Scripts", "jobcockpit.exe")' in script
    assert "findRepoCliProcesses" in script
    assert "JOBCOCKPIT_STOP_CLI_PATH" in script
    assert "[IO.Path]::GetFullPath($_.Path) -ieq $target" in script
    assert '"exit 0"' in script
    assert "repoCliPids" in script
    assert "remainingRepoCliProcesses" in script
    assert "cli worker" in script


def test_dev_script_writes_stop_pid_file():
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert "--no-worker" in script
    assert "worker" in script
    assert "jobcockpit" in script
    assert "JOBCOCKPIT_BACKGROUND_WORKERS_ENABLED" in script
    assert 'name: "worker"' not in script
    assert 'PID_FILE = "var/dev-pids.json"' in script
    assert "writePidFile" in script
    assert "parentPid: process.pid" in script
    assert "cwd: process.cwd()" in script
    assert "unlinkSync(PID_FILE)" in script


def test_dev_script_only_starts_ats_mock_when_requested():
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert package["scripts"]["dev"] == "node scripts/dev.mjs"
    assert package["scripts"]["dev:mock"] == "node scripts/dev.mjs --mock"
    assert 'const includeMock = args.has("--mock")' in script


def test_dev_script_syncs_once_before_no_sync_subprocesses():
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert 'const uvSyncArgs = ["sync"]' in script
    assert 'uvSyncArgs.push("--extra", "invisible-browser")' in script
    assert 'await runOnce("sync", "uv", uvSyncArgs)' in script
    assert script.index('await runOnce("sync"') < script.index('await runOnce("migrate"')
    assert 'const uvRunArgs = ["run", "--no-sync"]' in script
    assert '[...uvRunArgs, "python", "scripts/migrate.py"]' in script
    assert script.count("...uvRunArgs") == 1
    assert "preview workers with `pnpm stop -- --dry-run`, stop them, and retry" in script


@pytest.mark.parametrize("provider", [None, "invisible_playwright", "invisible_playwright_first", "chrome_cdp", "chrome_cdp_first", "disabled"])
def test_dev_sync_preserves_selected_browser_dependency(provider):
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")
    # Execute only dependency selection, without syncing or starting services.
    selection = script[script.index('const uvSyncArgs ='):script.index('const host =')]
    env = {} if provider is None else {"JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER": provider}
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "const process = {env: " + json.dumps(env) + "};\n" + selection
         + "\nconsole.log(JSON.stringify(uvSyncArgs));"],
        capture_output=True, text=True, check=True,
    )
    expected = ["sync"]
    if provider in {None, "invisible_playwright", "invisible_playwright_first"}:
        expected += ["--extra", "invisible-browser"]
    assert json.loads(result.stdout) == expected


def test_dev_script_supports_next_available_ports():
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert "--next-ports" in script
    assert "chooseAvailablePort" in script
    assert "is unavailable; using" in script


def test_dev_script_uses_signal_free_python_reloader():
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert 'const needsShell = isWindows && service.command === "pnpm"' in script
    assert "shell: needsShell" in script
    assert "windowsHide: isWindows" in script
    assert "shell: false" in script
    assert 'process.env.UV_PROJECT_ENVIRONMENT ?? ".venv"' in script
    assert 'resolve(venvRoot, "Scripts", "python.exe")' in script
    assert 'command: venvPython' in script
    assert script.count('"scripts/run_uvicorn_dev.py"') == 2
    assert '"--reload-dir"' in script


def test_dev_script_persists_crash_diagnostics():
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")

    assert 'resolve(process.cwd(), "var", "logs", "dev")' in script
    assert "appendSessionLog" in script
    assert '"--diagnostic-log"' in script
    assert '"--crash-log"' in script
    assert 'envRestartLimit("JOBCOCKPIT_DEV_API_RESTART_LIMIT", -1)' in script
    assert "sessionLog: DEV_LOG_PATH" in script
    assert "apiDiagnosticLog: API_DIAGNOSTIC_LOG_PATH" in script
    assert "apiCrashLog: API_CRASH_LOG_PATH" in script


def test_dev_console_hides_only_successful_api_browser_health_poll():
    probe = """
import { shouldDisplayServiceLine } from './scripts/dev-output.mjs';
const lines = [
  ['api', 'INFO:     127.0.0.1:56377 - "GET /api/browser-health HTTP/1.1" 200 OK'],
  ['api', 'INFO:     127.0.0.1:56377 - "GET /api/browser-health HTTP/1.1" 500 Internal Server Error'],
  ['api', 'INFO:     127.0.0.1:56377 - "GET /api/health HTTP/1.1" 200 OK'],
  ['web', 'INFO:     127.0.0.1:56377 - "GET /api/browser-health HTTP/1.1" 200 OK'],
];
console.log(JSON.stringify(lines.map(([name, line]) => shouldDisplayServiceLine(name, line))));
"""

    result = subprocess.run(
        ["node", "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == [False, True, True, True]
    script = Path("scripts/dev.mjs").read_text(encoding="utf-8")
    assert "appendSessionLog(name, line" in script
    assert "shouldDisplayServiceLine(name, line)" in script
