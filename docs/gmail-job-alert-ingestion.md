# Gmail Job Alert Ingestion

Gmail is a read-only job discovery adapter. Preview reads synthetic or real Gmail messages, extracts links, classifies them, and shows proposed imports. Import creates or updates JobCockpit records only after approval.

Processed state is stored locally in `job_alert_email_processing_states`. The application must never send, draft, label, archive, delete, star, or mark Gmail messages read/unread.

Imports hold a renewable database lease for the candidate and configured Gmail
source through the final progress commit. Overlapping imports of that source
return HTTP 409 before fetching; different sources may run concurrently. The
shared writer rechecks job identity across Gmail and ATS imports, while email
processing state remains specific to its configured source. An expired owner
cannot commit more progress. See [Concurrent runs](ats-provider-ingestion.md#concurrent-runs)
for transaction, retry and resource limits.

## Real Gmail Setup

Synthetic fixtures remain the default with `JOBCOCKPIT_GMAIL_PROVIDER=synthetic`. For real Gmail, enable the Gmail API in Google Cloud, create an OAuth desktop-app client, download the client JSON, and set:

```powershell
JOBCOCKPIT_GMAIL_PROVIDER=google
JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH=var/private/gmail/client_secret.json
JOBCOCKPIT_GMAIL_OAUTH_TOKEN_PATH=var/private/gmail/token.json
JOBCOCKPIT_GMAIL_MAX_RESULTS=25
JOBCOCKPIT_GMAIL_SCAN_MAX_RESULTS=250
```

Authorize once:

```powershell
pnpm --silent cli gmail auth
```

The OAuth flow requests only `https://www.googleapis.com/auth/gmail.readonly`. The stored token is local-only and must stay under `var/private/gmail/` or another ignored/private path.

To configure the `.env` values, create a placeholder client-secret file, and create or update a sender-scoped Gmail alert source in the local database, run:

```powershell
pnpm --silent python scripts/setup_gmail_ingestion.py `
  --sender jobs@mapleradar.com `
  --display-name "MapleRadar jobs" `
  --newer-than 30d
```

The script is idempotent. It preserves existing `.env` values unrelated to Gmail, does not overwrite an existing `var/private/gmail/client_secret.json`, and updates an existing source when the Gmail query already exists. Replace the placeholder client-secret file with the Google Cloud OAuth desktop-app JSON before running `pnpm --silent cli gmail auth`.

## Sample Gmail Queries

Keep queries scoped to known job-alert senders before using scheduled processing. Good starting points:

```text
from:linkedin.com/jobs newer_than:14d
from:linkedin.com newer_than:14d (job OR jobs)
from:glassdoor.com newer_than:14d (job OR jobs)
from:indeed.com newer_than:14d (job OR jobs)
from:greenhouse.io newer_than:30d (application OR job)
from:lever.co newer_than:30d (application OR job)
subject:(job OR role OR career OR alert) newer_than:7d
(from:linkedin.com OR from:indeed.com OR from:glassdoor.com) newer_than:14d
```

Pair sender-scoped Gmail queries with matching `sender_allowlist` domains on the JobCockpit source. Avoid broad queries such as `newer_than:30d` or `job`; they can scan unrelated mailbox pages during automatic processing.

## LinkedIn, Indeed, And Aggregator Links

LinkedIn and Indeed job links are classified as `requires_external_resolution` and converted into a reusable posting hint with company, title, location, source URL, and surrounding evidence text. The shared resolver in [External Posting Discovery](external-posting-discovery.md) searches for an official company posting first and imports that official URL after the normal URL policy passes.

LinkedIn Job Alert and Job Recommendation emails use a source-specific Gmail adapter. Configure the source with a sender-scoped query such as:

```text
from:jobalerts-noreply@linkedin.com {in:inbox in:spam}
```

The adapter accepts only numeric LinkedIn job-detail paths (`/jobs/view/{id}` and `/comm/jobs/view/{id}`), removes tracking parameters, and deduplicates company-logo and job-card links to `https://www.linkedin.com/jobs/view/{id}`. Search, navigation, alert-management, marketing, and footer links are excluded before ingestion. Job-card title, company, and location text is passed to the existing interest filter, language filter, and external posting resolver.

Indeed Job Alert emails use the same source-specific path. Configure the source with:

```text
from:donotreply@jobalert.indeed.com {in:inbox in:spam}
```

The Indeed adapter normalizes links carrying a stable `jk` job key to `https://ca.indeed.com/viewjob?jk={id}` and deduplicates repeated links for that job. Sponsored `/pagead/clk` cards without `jk` receive an irreversible local `jobcard` identity derived from the ad token so their title, company, and location evidence still reaches official-posting resolution or `needs_review`; personalized click parameters are never persisted. Search pages, alert-management links, and footer links are excluded.

The explicit `in:spam` term tells the read-only Google provider to include Spam in the search. Matching messages are processed in place alongside Inbox messages; JobCockpit does not remove `SPAM`, add `INBOX`, mark messages read, or otherwise change mailbox state. Sources without an explicit Spam or Trash query continue to exclude those folders.

If no official posting is found, the resolver falls back to fetching the original public LinkedIn or Indeed job URL, imports it only when the fetched page contains the expected job identity, and stores that fetched page as a private source archive. If external posting discovery is disabled or fallback confidence is too low, the link is stored as `needs_external_resolution`, the message remains `needs_review`, and a later run can retry it.

## Automatic Local Processing

Scheduled processing uses the backend CLI and does not require the FastAPI server:

```powershell
pnpm --silent cli gmail process --json
```

Run it from the repository root, or set the Windows Task Scheduler "Start in" directory to the repository root. This command loads `.env`, opens the configured SQLite database, processes every enabled Gmail job alert source, imports probable job links, deduplicates jobs, and writes an `EmailIngestionRun`. It skips messages already marked `processed` locally. `JOBCOCKPIT_GMAIL_MAX_RESULTS` controls preview/page size; `JOBCOCKPIT_GMAIL_SCAN_MAX_RESULTS` controls how many matching messages scheduled processing and approved imports may scan to get past already processed pages or newer Gmail arrivals.

Gmail list and message reads are paced at one request per second by default
(`JOBCOCKPIT_GMAIL_REQUEST_MIN_INTERVAL_SECONDS`). Previews and ingestion threads
share that pacing within a process. Separate processes may share Google's quota;
transient 403 rate limits, 429 responses, and server failures retry the same read
up to six times (`JOBCOCKPIT_GMAIL_READ_MAX_RETRIES`), with exponential backoff
and jitter. Earlier messages and pagination position are retained during retries.
Authentication, permission, and daily-limit errors fail without retrying.
Provider cooldowns longer than 64 seconds also remain explicit failures.
This follows [Google's error handling guidance](https://developers.google.com/workspace/gmail/api/guides/handle-errors).
The scan limit stays unchanged; a full 250-message fetch normally takes about
four to five minutes before job-link processing begins.

To process one source:

```powershell
pnpm --silent cli gmail process --source-id JOB_ALERT_SOURCE_ID --json
```

The LinkedIn and Indeed sources use this same source-specific command for on-demand runs; no separate scheduler or mailbox mutation is required.

`pnpm --silent cli` loads `.env` before selecting Python, including
`UV_PROJECT_ENVIRONMENT`, and runs `python -m jobcockpit.cli` in the prepared
environment. It does not install or update packages. Prepare the environment
with `pnpm dev` before use. This avoids Windows executable locks when another
worker is running. Ordinary `uv run jobcockpit ...` can try to reinstall the CLI;
application `.env` settings do not configure uv's environment selection.

For Windows scheduled ingestion, `scripts/run_ingestion_task.ps1` uses the same
launcher and writes stdout, stderr and an exit-status summary under
`var/scheduled-tasks/<TaskKey>/`. Its `-UvArguments "run jobcockpit ..."` parameter
is retained for existing task definitions; the runner translates that prefix to
the prepared-runtime launcher. Existing local wrappers can forward their
parameters to this tracked script. Keep package synchronization separate from
scheduled execution, and retain nonzero exit codes as task failures.

The JSON output includes source count, processed message count, created job count, per-source details, and `mailbox_mutations`, which must remain an empty list.

Use `--provider synthetic` or `--provider google` to override `JOBCOCKPIT_GMAIL_PROVIDER` for one run.

## Retry Unresolved Links

External LinkedIn or Indeed links that could not be resolved are stored as unresolved job alert links. You can retry them later from the Sources UI or from the CLI.

Retry every unresolved link, oldest first:

```powershell
pnpm --silent cli links retry-unresolved --json
```

Retry a bounded batch:

```powershell
pnpm --silent cli links retry-unresolved --limit 2 --json
```

Retry one saved unresolved link:

```powershell
pnpm --silent cli links retry-unresolved --link-id JOB_ALERT_LINK_ID --json
```

The command reuses the same centralized posting-link processor as Gmail and job-portal sources. Each link is retried in its own database transaction, so a later Playwright/Codex/network failure does not roll back earlier successful retries. Failed retry attempts are recorded back on the unresolved link as `retry_failed: ...`.
