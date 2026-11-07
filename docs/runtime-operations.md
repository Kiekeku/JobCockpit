# Runtime and worker operations

This guide describes supported launch paths. A fresh clone contains no scheduled
tasks, service installation, browser profile, private runtime, or ingestion state.
Choose schedules and source IDs for your own installation. Inspect Windows Task
Scheduler and the live process tree when diagnosing duplicate or interrupted work.

## Prepared runtime

`pnpm dev` loads `.env` before `uv sync`, migration, and service startup.
`UV_PROJECT_ENVIRONMENT` selects the environment; `.venv` is the fallback.
`UV_PYTHON` selects the interpreter when uv prepares that environment.
Changing `UV_PYTHON` alone does not replace an already-running interpreter.

Use `pnpm --silent cli ...` for JobCockpit commands and
`pnpm --silent python ...` for Python scripts/modules. They share
`scripts/python-runtime.mjs`, load `.env`, and do not install packages.
`pnpm test` imports only the two runtime-selection settings before
`uv run --no-sync python -m pytest`; local service settings are not injected.
Extension packaging uses the prepared Python launcher too.

Direct `uv` commands do not read these values from the application's `.env`.
For explicit dependency maintenance in PowerShell, export the selected values
first (using the paths configured in your checkout), then use uv:

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'var/runtime/python312'
$env:UV_PYTHON = 'var/tools/python/cpython-3.12.13-windows-x86_64-none/python.exe'
uv sync --extra invisible-browser
```

The paths above are optional examples; substitute your installed interpreter.
Stop processes using an environment before replacing its native dependencies.
CI has a fresh checkout and no local runtime override; its uv preparation is
intentional and does not share the running Windows environment.

## Launch and recovery boundaries

| Path | Runtime and lifecycle |
| --- | --- |
| API and ATS mock | `dev.mjs` selects prepared Python; `run_uvicorn_dev.py` starts replacement workers using `sys.executable`, logs crashes, and retries unexpected nonzero exits with backoff. |
| API capture/reprocess, cover-letter, source and publication workers | Threads inside the API process inherit its runtime and TLS setup. Lease ownership prevents concurrent queue ownership. They return to lease acquisition after losing a lease. |
| Standalone CLI workers | `cli.mjs` runs `python -m jobcockpit.cli`; TLS setup runs before dispatch. Source/publication thread death returns nonzero. Manual launches have no automatic process restart. |
| Scheduled publication worker | `run_publication_worker.ps1` uses the CLI launcher. The installed task retries failures three times at one-minute intervals and has a five-minute trigger with duplicate instances ignored. |
| Scheduled ingestions | Configure your own tasks to invoke `run_ingestion_task.ps1`. The launcher preserves arguments, stdout/stderr and native exit status. Default timeout is 6,900 seconds, with process-tree termination and exit 124 recorded before a two-hour scheduler limit. No ingestion is automatically replayed on failure. |
| Scheduled status scans | `run_job_status_reconciliation.ps1` uses the CLI launcher, a bounded runtime plus cleanup grace, child-tree termination, and structured result logs. Review schedules and batch sizes before running the installer. |
| Scheduled Firefox rendering | `multiprocessing` spawn inherits the selected interpreter but starts fresh Python state. `_render_in_worker` installs TLS protection explicitly. Windows job ownership or POSIX process groups bound browser descendants to the request. |
| Parser validation process pools | Inherit the parent interpreter; their work is local parsing, not browser or network ingestion. |
| Codex document/parser subprocesses | External Codex executable, not another Python environment. Bounded subprocess calls use owned process-tree cleanup. |
| Chrome/Firefox/Playwright drivers | Browser executables and drivers, not Python entry points. Browser configuration and ownership apply; no hardcoded `.venv` Python launch is involved. |
| Frontend and extension watchers | Node/pnpm processes; Python environment selection does not apply. |
| Migration, corpus, privacy and packaging utilities | Use `pnpm --silent python ...` to select the same prepared environment. |

Native TLS mitigation redirects only the default Certifi bundle to in-memory
certificate data. Verification remains enabled. API and CLI entry points install
it, and spawned scheduled-browser workers install their own copy. See
[Crash capture and recovery](dev-crash-dumps.md) for evidence and limitations.

The API reload supervisor waits for a stable file snapshot, then verifies UTF-8
decoding and Python compilation before stopping its current worker. Invalid
edits leave the worker running and emit a `reload_rejected` diagnostic. All
changes since the last accepted snapshot must validate, including when another
file changes while the first remains invalid. Correcting the file permits a
normal reload. Supervisor changes take effect when the development stack is
restarted. `.editorconfig` declares UTF-8 for source editing; scripts must still
specify their write encoding explicitly.

## Stopping and observing

`pnpm stop -- --dry-run` previews verified services and workers.
Standalone CLI workers carry a repository marker in Python's `-X` options,
allowing discovery even when the manual Node command uses a relative script path.
Matching requires both the repository marker and the `jobcockpit.cli worker`
command; Gmail runs and another repository's workers are excluded. Absolute
scheduled Node launchers and legacy `.venv` entry points remain recognizable.
Task Scheduler may launch a scheduled worker again after it is stopped; disable
its task first when intentionally taking it offline for maintenance.

`latest.summary.json` describes the last finished run. Task Scheduler can show
an older failure or its running status code while a new attempt is active. Inspect task state, the
newest per-run log, and the database run record together. A successful `--help`
launch proves runtime selection and argument parsing, not a successful external
ingestion. Native recovery can interrupt in-flight work; it is not zero downtime.

Do not automatically rerun every failed ingestion to clear historical status.
Check for active runs first, and retry the intended source without duplicating
ongoing work. Full dumps, scheduler backups, source IDs and runtime logs remain
private under `var/`.

## Validation on your installation

Use the checks in [CONTRIBUTING.md](../CONTRIBUTING.md), then inspect local API
health, the selected interpreter, worker leases, and the logs for your own tasks.
Tests establish runtime contracts; they do not establish successful ingestion
from every external source. Keep failed-run evidence rather than clearing task
history to make a dashboard appear healthy. Do not include private logs or dumps
in public issues; provide minimized synthetic reproductions instead.
