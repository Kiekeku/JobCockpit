# Windows API crash dumps

For native API crashes such as `0xC0000409`, download Microsoft's
[ProcDump](https://learn.microsoft.com/en-us/sysinternals/downloads/procdump)
and extract it into `var/tools/procdump`. Verify its Microsoft Authenticode
signature before use. Add this local `.env` setting:

```dotenv
JOBCOCKPIT_DEV_PROCDUMP=var/tools/procdump/procdump64.exe
```

Start `pnpm dev` and check for `Native crash capture attached to API pid`.
The API worker attaches ProcDump to its own interpreter PID, which can differ
from the Windows virtual-environment launcher PID shown by the supervisor.
Each replacement worker attaches a new monitor after reload or automatic restart.
ProcDump exits when its worker exits. Other Python processes are not monitored.
The launcher accepts the Sysinternals license when starting the configured tool.

Unhandled native exceptions produce full memory dumps in `var/logs/dev/dumps/`.
The same directory contains per-worker ProcDump logs. Attachment failures are
reported in the console and session diagnostic JSONL; they do not prevent startup.
Python exceptions handled by FastAPI do not trigger dumps. No system-wide debugger
or Windows registry setting is installed. Collection pauses once five dumps exist
in this directory; existing evidence is retained. Move reviewed dumps to an archive
subdirectory to allow further collection.

Dumps contain process memory and may include private job data or credentials.
Keep them local and out of git; `var/` is already ignored. Inspect a crash dump with
WinDbg (`!analyze -v`) to identify the exception subcode, native module and stack.
Delete old dumps when no longer needed; full dumps can be large.

To disable capture, remove the setting and restart `pnpm dev`.

## Recovery and the September 2026 certificate-loading workaround

`pnpm dev` now defaults to `JOBCOCKPIT_DEV_API_RESTART_LIMIT=-1`: unlimited recovery
after unexpected nonzero API exits. Rapid failures back off at 1, 2, 4, 8, 16,
then 30 seconds. A worker running for 60 seconds resets the failure streak. Set
zero to disable restarts, or a positive number to cap consecutive failures.
Ctrl+C/Break stops the supervisor instead of causing a restart. In-flight requests
can fail during a crash and are not automatically replayed; recovery is not zero
downtime. Logs and dumps remain available for diagnosis.

The captured crashes had `FAST_FAIL_INVALID_ARG` in OpenSSL certificate loading:
HTTPX during Eluta redirect resolution and Requests during Invisible's egress-IP
lookup. Both used Certifi's `cacert.pem`, and the Windows CRT considered its file
descriptor closed. This establishes the failure boundary, not who invalidated it.

On Windows, API and CLI initialization install a narrowly scoped `SSLContext` method
wrapper for the default Certifi path. It reads that bundle with Python and passes
the same PEM certificates as `cadata` to OpenSSL, bypassing the native FILE/CRT
read path for both HTTP libraries and the browser dependency. Custom CA files,
directories, explicit CA data, client keys, and certificate/hostname verification
are unchanged. The roots are a process-lifetime snapshot; restart after updating
Certifi. This is a workaround, not a claim that descriptor ownership was fixed.

The diagnosed runtime was Python 3.12.10 / OpenSSL 3.0.16. A separate environment
with Python 3.12.13 / OpenSSL 3.5.5 was prepared for local validation. To select a
separately validated environment for development, set standard uv variables in
the local `.env` (paths below are this checkout's optional local arrangement):

```dotenv
UV_PROJECT_ENVIRONMENT=var/runtime/python312
UV_PYTHON=var/tools/python/cpython-3.12.13-windows-x86_64-none/python.exe
```

`pnpm dev` uses that environment consistently for sync, migrations and API launch.
Use `pnpm --silent cli <arguments>` for manual commands; it loads the same `.env`
and launches the prepared Python directly, without package synchronization or
the Windows `jobcockpit.exe` entry point. The scheduled ingestion runner uses
this same launcher. Plain `uv run jobcockpit ...` does not inherit the dev
launcher's environment settings and can fail trying to replace an executable
held by a running worker.
The standalone publication-worker and status-reconciliation task scripts use
the same launcher, including the runtime override and CLI TLS workaround.
Existing scheduled task definitions do not need reinstallation; their scripts
are read on each launch. Restart a long-running standalone worker to pick up
the new launcher. `pnpm stop -- --dry-run` also identifies repository workers
started through the absolute `scripts/cli.mjs` path used by these tasks.
Without the override it continues to use `.venv`. Worker diagnostic events include
Python and OpenSSL versions. Runtime replacement alone is not a proven fix; retain
crash capture and the recovery policy while observing real ingestion runs.
