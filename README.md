# JobCockpit

JobCockpit is a local-first job application workstation for ingesting roles, tracking applications, generating truthful application materials, previewing Gmail job alerts, and safely testing browser assistance against a local ATS mock site.

The current MVP is intentionally single-user and local-only. It uses SQLite, filesystem artifact storage, a FastAPI backend, a Next.js dashboard, synthetic and read-only Google Gmail alert providers, and a Manifest V3 Chrome side panel for grounded answer drafts, reviewed native-field insertion on Workday/local mocks, and user-triggered posting capture.

Released under the [MIT license](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md)
for development and [SECURITY.md](SECURITY.md) for vulnerability reporting.
There is no user authentication: run services on loopback on a trusted computer.
This repository is a standalone source snapshot from October 17, 2026, with
public setup defaults. See [Sharing the source](docs/open-source-sharing.md).

## Current MVP Status

What works now:

- SQLite persistence through SQLAlchemy and Alembic.
- Local artifact-backed storage under `var/private/storage`.
- Paste, URL, Gmail fixture, Workday, Phenom/Oracle, Hanzilla, Eluta, Canadian Tech Internships 2026 and 2027, Canada Tech Internships Summer 2027, and ATS mock job ingestion.
- Job/application/package tracking APIs.
- Profile create/edit UI and APIs for contact info, skills, links, and resume uploads.
- Resume parsing for Markdown, TXT, DOCX, and text-based PDFs into private evidence items.
- Markdown resume and cover-letter draft generation.
- Truthfulness checks for obvious unsupported/private claims.
- Extension-safe document download tokens.
- Synthetic Gmail alert preview/import with local processed state.
- Local provider-aware ATS mock site.
- Chrome side panel with Astra drafts, individual answer review, verified native-field insertion, approved PDF attachment, local undo, and capture progress.
- Active opportunity filters, applied-job tracking, browser-health diagnostics, and restart recovery.
- Optional policy-controlled outbound job publication, disabled by default.

Current boundaries:

- Gmail mailbox mutation. Real Gmail access is read-only and local-token based.
- Custom ATS widget automation. Native Workday controls support reviewed insertion; custom comboboxes and drop zones require manual completion.
- Other ATS sites support draft/copy assistance; native insertion is limited to Workday and local mocks.

## Repository Layout

```text
apps/api                  FastAPI backend, SQLAlchemy models, Alembic migrations
apps/web                  Next.js dashboard
apps/chrome-extension     Manifest V3 application side panel
apps/ats-mock-site        Local provider-aware ATS mock site
data/fixtures             Synthetic development fixtures
docs                      Architecture and safety notes
scripts                   Local utility scripts
tests                     Backend, privacy, extension, and static tests
var                       Local runtime state, gitignored
```

## Prerequisites

Install:

- Python 3.12+
- `uv`
- Node.js 22+
- `pnpm` 9 (the version used by CI)
- Google Chrome or Chromium for extension testing

From a fresh checkout, install dependencies:

```bash
uv sync --locked
pnpm install --frozen-lockfile
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

The sample uses synthetic Gmail and deterministic local parsing and drafting.
External search, AI providers, rendered browser fallback, and outbound publication
are disabled. Add your own profile in the app; no candidate documents are bundled.
URL imports still contact the public sources you request. For browser integration
tests, install Chromium with `uv run playwright install chromium` (Linux may need
`--with-deps`).

## Developer Setup

Initialize local runtime state and database:

```bash
mkdir -p var
pnpm --silent python scripts/migrate.py
```

On Windows PowerShell, use this if `mkdir -p` is not available:

```powershell
New-Item -ItemType Directory -Force var
pnpm --silent python scripts/migrate.py
```

Start the local development stack:

```bash
pnpm dev
```

This runs migrations and starts the API and web dashboard. If `make` is available, this is equivalent:

```bash
make dev
```

Open:

- Web dashboard: `http://localhost:3000`
- API health: `http://localhost:8000/api/health`

If you changed ports in `.env`, use those values instead.

Useful variants:

```bash
pnpm dev:mock
pnpm dev:all
```

`pnpm dev:mock` also starts the local ATS mock site. `pnpm dev:all` also starts the Chrome extension TypeScript watcher, without the mock site.

The dev stack runs `uv sync` once, including the `invisible-browser` extra when `.env` selects the `invisible_playwright` rendered fallback, then uses the prepared environment selected by `UV_PROJECT_ENVIRONMENT` (default `.venv`) without repeating dependency resolution for each subprocess. If Windows reports a locked runtime file during dependency updates, preview active workers with `pnpm stop -- --dry-run` and stop them before retrying. The migration wrapper is safe to rerun; if it finds a local SQLite database with JobCockpit tables but no Alembic version row, it stamps the recognized schema before applying pending migrations.

Use `pnpm --silent cli <arguments>` for JobCockpit commands and
`pnpm --silent python <arguments>` for Python modules or maintenance scripts.
Both load the runtime selection from `.env` and use the prepared interpreter
without installing packages. `pnpm test` loads only the runtime selection,
keeping local service settings out of the test environment. See
[Runtime and worker operations](docs/runtime-operations.md) for the complete launch map,
recovery boundaries, and scheduled-task checks.

Python source changes under `apps/api` restart the API gracefully in place; the `pnpm dev` launcher and web dashboard remain running during the reload. Each run prints and stores a timestamped transcript, structured API process events, and a Python native-crash log under `var/logs/dev/`. The current paths are also recorded in `var/dev-pids.json`. Unexpected nonzero exits restart automatically with backoff, without a retry cap by default (`JOBCOCKPIT_DEV_API_RESTART_LIMIT=-1`). Set zero to disable recovery or a positive number to cap consecutive failures. In-flight requests can still fail during a restart.

You can still run services separately when debugging:

```bash
pnpm --silent python scripts/run_uvicorn_dev.py jobcockpit.api.main:app --app-dir apps/api --reload-dir apps/api --restart-limit -1 --port 8000
pnpm --filter @jobcockpit/web dev
pnpm --silent python scripts/run_uvicorn_dev.py mock_site:app --app-dir apps/ats-mock-site --reload-dir apps/ats-mock-site --restart-limit -1 --port 8001
```

## Environment Variables

Copy `.env.example` to `.env` before starting. The public sample explicitly disables
optional services; built-in defaults can enable external providers without that
file. Edit only your ignored local copy.

```bash
JOBCOCKPIT_DATABASE_URL=sqlite:///./var/jobcockpit.db
JOBCOCKPIT_ARTIFACT_ROOT=var/private/storage
# Leave JOBCOCKPIT_EXTENSION_TOKEN_SECRET unset for an installation-specific secret.
JOBCOCKPIT_DEV_HOST=127.0.0.1
JOBCOCKPIT_API_PORT=8000
JOBCOCKPIT_WEB_PORT=3000
JOBCOCKPIT_ATS_MOCK_PORT=8001
JOBCOCKPIT_DEV_API_RESTART_LIMIT=-1
# JOBCOCKPIT_API_BASE_URL=http://127.0.0.1:8000
```

Notes:

- If `JOBCOCKPIT_EXTENSION_TOKEN_SECRET` is omitted, the backend creates a local secret at `var/private/extension-token-secret`.
- `pnpm dev` reads `.env`, starts the API on `JOBCOCKPIT_API_PORT`, and starts the dashboard on `JOBCOCKPIT_WEB_PORT`. Use `pnpm dev:mock` to also start the ATS mock on `JOBCOCKPIT_ATS_MOCK_PORT`.
- `pnpm stop` reads the same configured ports and stops local dev listeners plus repository CLI workers launched through the configured runtime (plus legacy `.venv` workers); use `pnpm stop -- --dry-run` to preview exact-path matches.
- If Windows reports a listener whose PID cannot be killed, use `pnpm dev -- --next-ports` to start on free nearby ports without changing `.env`.
- Leave `JOBCOCKPIT_API_BASE_URL` commented for normal local development; `pnpm dev` derives it from `JOBCOCKPIT_DEV_HOST` and `JOBCOCKPIT_API_PORT` before starting the web dashboard.
- The backend URL fetch allowlist uses `JOBCOCKPIT_ATS_MOCK_PORT` for local mock job URLs.
- Before starting services, `pnpm dev` checks that the configured ports are available. If Windows reports a socket permission or in-use error, change the matching `JOBCOCKPIT_*_PORT` in `.env` or stop the process shown by `netstat -ano | findstr ":PORT"`.
- The Chrome extension requires localhost access and uses activeTab for public pages. Optional site access is requested one origin at a time. Set API, web and mock URLs in the side panel Settings when ports change.
- Do not commit `.env`, local databases, generated artifacts, Gmail tokens, or browser profiles.

### Optional external services

Enable only the integrations you intend to use, then restart the stack:

| Feature | Configuration and account | Data sent outside the app |
| --- | --- | --- |
| Gmail alerts | `JOBCOCKPIT_GMAIL_PROVIDER=google`; your own OAuth client and read-only authorization | Mailbox read requests |
| Posting search | Resolver `search`, search provider `serper`, and your own Serper key | Job/company search queries |
| Company research | `JOBCOCKPIT_COMPANY_PROFILE_RESEARCH_PROVIDER=codex_cli` | Employer research context |
| Cover letters | `JOBCOCKPIT_COVER_LETTER_GENERATOR_PROVIDER=codex_cli` | Selected candidate evidence and job text |
| Application answers | `JOBCOCKPIT_EXTENSION_ANSWER_PROVIDER=codex_cli` | Questions, selected candidate evidence, and reviewed answers |
| Rendered fallback | `JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=invisible_playwright` | Browser requests to posting sites; see [browser setup](docs/external-posting-discovery.md) |
| RankAlpha imports | Your own authorized `JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN` | Authenticated job-list requests |
| Outbound publication | Your own destination, write credential, and [publication policy](docs/publication.md) | Allowlisted job facts and generated editorial fields |

Codex integrations require the `codex` executable, your own authenticated account,
and access to the configured models. Change model settings to match your account.
External services have their own terms, availability, and costs; the MIT license
does not grant service access. Review all generated claims before use. Local
storage does not mean enabled external AI features run offline.

## Validation

For which corpus to use and when to run focused, representative, incremental, or
full checks, start with the [parser dataset map and decision table](docs/parser-validation.md).
It distinguishes approved golden expectations from real source artifacts, private
baselines, historical failure pins, and reviewed observations.

Run the Python test suite:

```bash
pnpm test
```

`pnpm test` includes every case in the versioned, anonymized golden parser corpus.
For the golden gate alone, or focused parser failure output, use:

```bash
pnpm --silent python scripts/parser_corpus_diff.py
```

Do not run both complete golden checks against unchanged code and fixtures.
The runner reports drift by case ID and exact field path. It partitions the corpus
across up to eight parser processes by default; pass `--workers 1` for serial
debugging or `--workers N` for an explicit limit. Add a privacy-safe minimized case
grounded in a reviewed real posting to `data/fixtures/parser/golden_cases.json`
before changing parser behavior; it should fail before the fix and pass afterward.
To inspect a focused case or save the current selected fields for a local
before/after comparison:

```bash
pnpm --silent python scripts/parser_corpus_diff.py --case academic_term_from_title
pnpm --silent python scripts/parser_corpus_diff.py --write-current var/parser-corpus-current.json
```

Current snapshots belong under `var/` and are not committed.

The pnpm test scripts run through `scripts/test.mjs`, which creates the local temp root, uses `uv run --no-sync`, a repo-local uv cache, disables pytest cache writes, and runs pytest in parallel with `pytest-xdist` by default, capped at 16 workers to avoid process oversubscription. The golden parser corpus is exposed to pytest as 16 deterministic chunks so those workers can share it. Tests that request `tmp_path` use the repo-local `.pytest-tmp` directory to avoid slow or inaccessible Windows temp roots. Run `uv sync` after dependency changes.

For the normal edit/verify loop, skip slower end-to-end tests:

```bash
pnpm test:fast
```

For a small change, select the affected test files or individual pytest test IDs:

```bash
pnpm test:fast --serial tests/test_web_dashboard_static.py tests/test_web_job_detail_static.py
pnpm test:fast --serial tests/test_job_parser_codex.py::test_parse_job_text_resolves_exact_term_schedule_independently_of_field_order
```

With no selectors, the existing full/fast/slow suite scope is unchanged. Marker
filters still apply to selected tests. Use the decision table for required final
checks: UI/documentation edits use scoped checks; parser changes retain the complete
golden gate, and shared parser changes retain full saved-posting validation.

Run only the slower marked tests before a larger handoff:

```bash
pnpm test:slow
```

If you need serial pytest output for debugging:

```bash
pnpm test:serial
```

You can also control the worker count for parallel runs:

```powershell
$env:PYTEST_XDIST_WORKERS = "4"; pnpm test
```

Run the privacy scan:

```bash
pnpm --silent python scripts/privacy_scan.py .
```

Build the web app and Chrome extension:

```bash
pnpm build
```

For touched Python files, run Ruff when available:

```bash
pnpm --silent python -m ruff check apps/api/jobcockpit tests
```

## End-User Guide

### 1. Start JobCockpit

Start the local app:

```bash
pnpm dev
```

This opens the backend and dashboard. Then open `http://localhost:3000`, or the `JOBCOCKPIT_WEB_PORT` you set in `.env`.

If you need to run pieces separately for troubleshooting:

```bash
pnpm --silent python scripts/run_uvicorn_dev.py jobcockpit.api.main:app --app-dir apps/api --reload-dir apps/api --restart-limit -1 --port 8000
pnpm --filter @jobcockpit/web dev
```

The dashboard shows imported jobs, tracked applications, draft counts, and safety status.

### 2. Import A Job Posting

The dashboard currently reads data but does not expose every write workflow. Use the API for MVP imports.

On Windows PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest `
  -ContentType "application/json" `
  -Body '{"source_type":"paste","raw_text":"Acme Data Engineer\nPython SQL FastAPI\nRemote Canada","source_url":null}'
```

On macOS/Linux:

```bash
curl -X POST http://localhost:8000/api/jobs/ingest \
  -H "Content-Type: application/json" \
  -d '{"source_type":"paste","raw_text":"Acme Data Engineer\nPython SQL FastAPI\nRemote Canada","source_url":null}'
```

Refresh the dashboard after importing.

Import from a Workday careers site:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"workday","source_url":"https://suncor.wd1.myworkdayjobs.com/Suncor_External","search_text":"engineer","limit":5}'
```

Import from the ATCO Phenom/Oracle careers wrapper:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"phenom_oracle","source_url":"https://careers.atco.com/global/en/search-results","search_text":"engineer","limit":5}'
```

Import from Hanzilla internships:

```powershell
pnpm --silent cli ats ingest-hanzilla --search-text python --limit 5
```

The Hanzilla shortcut posts to the running local API at `http://127.0.0.1:8000` by default. Use `--skip-interest-filter`, `--skip-language-filter`, or `--api-base-url` when needed. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"hanzilla","source_url":"https://jobs.hanzilla.co/internships/","search_text":"python","limit":5}'
```

Import from the default Eluta co-op search:

```powershell
pnpm --silent cli ats ingest-eluta --limit 20
```

The Eluta shortcut posts to the running local API at `http://127.0.0.1:8000` by default and skips the role-interest filter by default for the broad co-op search. Use `--source-url`, `--skip-language-filter`, or `--api-base-url` when needed. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"eluta","source_url":"https://www.eluta.ca/search?q=co-op+field%3A%22Computing+-+Software+%26+Web+Development%22&l=&qc=","limit":20,"skip_interest_filter":true}'
```

Import from the Canadian Tech Internships GitHub list:

```powershell
pnpm --silent cli ats ingest-canadian-tech --limit 30
```

The Canadian Tech shortcut defaults to the public GitHub README URL, `--search-text software`, and `--limit 30`, meaning up to 30 matching README apply links are scanned. Use `--search-text`, `--limit`, `--skip-interest-filter`, `--skip-language-filter`, or `--api-base-url` when needed. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"canadian_tech_internships","source_url":"https://github.com/negarprh/Canadian-Tech-Internships-2026?tab=readme-ov-file","search_text":"software","limit":30}'
```

Import from the Canadian Tech Internships 2027 GitHub list:

```powershell
pnpm --silent cli ats ingest-canadian-tech-2027 --limit 30
```

This shortcut imports the `README.md` list in the Canadian Tech Internships 2027 repository. It ignores the introductory Markdown before the listings table and defaults to `--search-text software` and `--limit 30`. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"canadian_tech_internships_2027","source_url":"https://github.com/negarprh/Canadian-Tech-Internships-2027?tab=readme-ov-file","search_text":"software","limit":30}'
```

Import from the separate Canada Tech Internships Summer 2027 GitHub list:

```powershell
pnpm --silent cli ats ingest-canada-tech-2027 --limit 30
```

This shortcut defaults to the public `michelleokolie/canada-tech-internships-summer-2027` README, `--search-text software`, and `--limit 30`. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"canada_tech_internships_summer_2027","source_url":"https://github.com/michelleokolie/canada-tech-internships-summer-2027","search_text":"software","limit":30}'
```

Import from the authenticated RankAlpha jobs API:

Set this only in the ignored local `.env` file, then restart the API:

```dotenv
JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN=replace-with-current-token
```

```powershell
pnpm --silent cli ats ingest-rankalpha `
  --source-url 'https://jobs.rankalpha.io/api/jobs?status=Open&country=Canada&sort=rank' `
  --limit 50
```

The RankAlpha shortcut sends the filtered `/api/jobs` URL to the running local JobCockpit API.
The backend adds the bearer token from `JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN`; tokens are never
accepted in the URL or the local ingestion request. Repeated `status`, `country`, `company`, and
`location` parameters retain RankAlpha's OR behavior, repeated `tag` parameters retain its AND
behavior, and different categories remain combined with AND. `--search-text` replaces `q` while
preserving the other query parameters. JobCockpit follows RankAlpha pagination until it imports
`--limit` non-duplicate jobs or reaches the final page.

ATS ingestion is adapter-backed. Use `provider` to choose the adapter and `search_text` for targeted searches rather than importing an entire careers site. `limit` caps non-excluded detail links attempted per run and therefore also caps imported jobs; excluded links do not consume the budget. The Workday adapter uses Workday CXS listing/detail endpoints for `myworkdayjobs.com` and supported `myworkdaysite.com` public URLs. The Phenom/Oracle adapter currently accepts ATCO `careers.atco.com` search/detail URLs, searches the Phenom `refineSearch` widget API, reads each `jobDetail` payload, keeps the Oracle apply URL in the raw snapshot, and records imported jobs with `source_type="ats_phenom_oracle"`. The Hanzilla adapter accepts `jobs.hanzilla.co` listing/detail URLs, filters the static job cards by `search_text`, reads each Hanzilla detail page, keeps the direct company apply URL in the raw snapshot, and records imported jobs with `source_type="ats_hanzilla"`. The Eluta adapter accepts `eluta.ca` search/detail URLs, pages through `pg=` search results, reads each `/spl/...` detail page, resolves Eluta `/direct/i` apply redirects, and records imported jobs with `source_type="ats_eluta"`. For Eluta, leave `search_text` empty to use the full `q=` expression in the source URL; supplying `search_text` replaces that `q=` value. The Canadian Tech Internships adapter reads the GitHub README table, skips closed rows, filters open rows by `search_text`, and delegates each Apply URL to the shared `jobcockpit.ingestion.ats_handlers` resolver. That shared resolver handles Workday, Greenhouse, Ashby, SmartRecruiters, Lever, iCIMS, Oracle Cloud, and generic HTML/metadata cases so job boards, Gmail links, and future sources can reuse the same ATS extraction logic.

Every `POST /api/jobs/ingest/ats` call records a `JobIngestionRun` plus `AuditLogEntry` rows for run start and completion or failure. The API response includes `jobs_seen_count`, `jobs_created_count`, `jobs_duplicate_count`, `ingestion_run`, and `alerts`; recent runs are available at `GET /api/ingestion-runs` and shown in the dashboard's Ingestion runs panel. Failed adapter requests are logged through the backend logger and surfaced as `alert_level="error"` run records.
Each portal link also receives a durable `JobIngestionItem` record. Successful jobs commit independently, item-level failures roll back only that link and its newly written artifacts, and mixed-result runs finish as `completed_with_warnings`. Retrieve the per-link statuses later with `GET /api/ingestion-runs/{run_id}/items`.


Job postings are deduplicated before storing new artifacts. Exact duplicate snapshots match `posting_snapshot_hash`; URL-backed imports also skip a posting when the same canonical `source_url` is already present, even if the listing text changed. Duplicate skips are counted on the ingestion run and returned as warning alerts.

### 3. Track A Job And Generate Documents

Create an application for a job:

```powershell
curl.exe -X POST http://localhost:8000/api/jobs/JOB_ID/applications
```

Create an application package:

```powershell
curl.exe -X POST http://localhost:8000/api/applications/APPLICATION_ID/packages
```

Generate a resume draft:

```powershell
curl.exe -X POST http://localhost:8000/api/application-packages/PACKAGE_ID/resume-draft
```

Generate a cover letter draft:

```powershell
curl.exe -X POST http://localhost:8000/api/application-packages/PACKAGE_ID/cover-letter-draft
```

Ingestion automatically queues a private cover-letter draft when parsing determines
that the posting requires one. A dedicated cover-letter worker creates a draft
application package and leaves both the package and document unapproved for review.
The API starts this worker separately from page-capture and reprocessing work. It can
also be run as a standalone process with `pnpm --silent cli worker cover-letter`.
Inspect the latest request with:

```powershell
curl.exe http://localhost:8000/api/jobs/JOB_ID/cover-letter-generation
```

The job detail page provides the preferred cover-letter workflow. Configure
`JOBCOCKPIT_COVER_LETTER_GENERATOR_PROVIDER=codex_cli`, restart JobCockpit,
open a job, and use its **Codex writing desk** to select a profile PDF resume,
generate a new versioned draft, preview the rendered PDF, and download it for
review. Codex receives the saved candidate profile, evidence explicitly allowed
for cover letters, the selected resume, the posting, optional custom
instructions, and an optional cover-letter sample. When search is enabled,
Codex may use it only for useful employer background; it must not replace the
supplied posting or introduce candidate claims. A configured provider failure
returns an error and does not silently create a heuristic fallback.

Run the truthfulness check:

```powershell
curl.exe -X POST http://localhost:8000/api/application-packages/PACKAGE_ID/truthfulness-check
```

List package documents:

```powershell
curl.exe http://localhost:8000/api/application-packages/PACKAGE_ID/documents
```

Generated files are stored as local artifacts under the configured `JOBCOCKPIT_ARTIFACT_ROOT`.

### 4. Personal Profile Setup

Applications are scoped to the local candidate ID `candidate_local`. Set up that profile from the dashboard Profile panel:

1. Enter full name, email, phone, location, links, summary, and skills.
2. Click Create profile. Later edits use the same panel and save through the update path.
3. Upload an existing resume as Markdown, TXT, DOCX, or PDF.
4. Review extracted evidence and toggle whether each item may be used in resumes or cover letters.

Text-based PDFs are parsed with `pypdf`. Scanned or encrypted PDFs are stored privately but may not produce evidence; add or edit evidence manually after upload.

Create the profile through the API:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/profile `
  -ContentType "application/json" `
  -Body '{"full_name":"Jane Candidate","email":"jane.candidate\u0040example.invalid","phone":"local-only","location":"Calgary, AB","links":["https://portfolio.example.invalid/jane"],"summary":"Backend engineer focused on local-first systems.","skills":["Python","FastAPI","SQL"]}'
```

Edit the profile later:

```powershell
Invoke-RestMethod -Method Put -Uri http://localhost:8000/api/profile `
  -ContentType "application/json" `
  -Body '{"full_name":"Jane Candidate","email":"jane.candidate\u0040example.invalid","phone":"local-phone-updated","location":"Calgary, AB","links":["https://portfolio.example.invalid/jane"],"summary":"Backend engineer focused on local-first systems.","skills":["Python","FastAPI","SQLite"]}'
```

Upload a resume:

```powershell
curl.exe -X POST http://localhost:8000/api/profile/resume -F "file=@C:\path\to\resume.pdf"
```

Generated resume and cover-letter drafts use the saved profile fields and allowed evidence. Without a saved profile, drafts still fall back to `Synthetic Candidate`.

### 5. Gmail Job Alert Setup

The default Gmail provider is synthetic so a fresh local checkout works without Google Cloud credentials. Real Gmail processing is available through the read-only Gmail API.

Both providers store processed state locally. JobCockpit never labels, archives, deletes, stars, sends, drafts, or marks messages read/unread.

Create a synthetic Gmail alert source:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/job-alert-sources `
  -ContentType "application/json" `
  -Body '{"display_name":"Synthetic alerts","gmail_query":"from:alerts.invalid newer_than:7d","sender_allowlist":["alerts.invalid"]}'
```

For real Gmail, keep each query narrow enough for unattended processing. Good starting queries:

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

Prefer sender-scoped queries plus `sender_allowlist` values that match the same domains. Avoid broad queries such as `newer_than:30d` or `job`, because scheduled processing may scan unrelated mail.

Preview messages:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/job-alerts/gmail/preview `
  -ContentType "application/json" `
  -Body '{"source_id":"JOB_ALERT_SOURCE_ID"}'
```

Import approved previewed messages:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/job-alerts/gmail/import `
  -ContentType "application/json" `
  -Body '{"source_id":"JOB_ALERT_SOURCE_ID","approved_message_review_ids":["MESSAGE_REVIEW_ID"]}'
```

The import flow extracts job-like links, applies the URL safety policy, deduplicates jobs, and records processed state locally.

To use real Gmail:

1. Create a Google Cloud OAuth client for a desktop app and enable the Gmail API.
2. Download the OAuth client JSON into `var/private/gmail/client_secret.json`.
3. Set these values in `.env`:

```powershell
JOBCOCKPIT_GMAIL_PROVIDER=google
JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH=var/private/gmail/client_secret.json
JOBCOCKPIT_GMAIL_OAUTH_TOKEN_PATH=var/private/gmail/token.json
JOBCOCKPIT_GMAIL_MAX_RESULTS=25
JOBCOCKPIT_GMAIL_SCAN_MAX_RESULTS=250
```

4. Authorize read-only Gmail access once:

```powershell
pnpm --silent cli gmail auth
```

This opens a local browser OAuth flow and stores the refresh token at `JOBCOCKPIT_GMAIL_OAUTH_TOKEN_PATH`, which is ignored by git.

You can also use the local setup script to write the Gmail `.env` values, create a placeholder `client_secret.json`, and create or update a sender-scoped alert source in the SQLite database:

```powershell
pnpm --silent python scripts/setup_gmail_ingestion.py `
  --sender jobs@mapleradar.com `
  --display-name "MapleRadar jobs" `
  --newer-than 30d
```

The script preserves unrelated `.env` values and does not overwrite an existing client-secret file. Replace the placeholder secret with the Google Cloud OAuth desktop-app JSON before running `pnpm --silent cli gmail auth`.

For scheduled local processing, use the CLI. It does not require the API server to be running; it loads `.env`, opens the local database, processes all enabled Gmail alert sources, and prints a JSON summary suitable for Task Scheduler logs:

```powershell
pnpm --silent cli gmail process --json
```

Run it from the repository root, or set the Windows Task Scheduler "Start in" directory to the repository root.

To process one source:

```powershell
pnpm --silent cli gmail process --source-id JOB_ALERT_SOURCE_ID --json
```

The CLI automatically imports probable job links from allowed messages, applies the same URL safety policy and dedupe rules as the API, skips already processed messages, and records an `EmailIngestionRun`. It uses `JOBCOCKPIT_GMAIL_PROVIDER`, or you can override one run with `--provider synthetic` or `--provider google`. `JOBCOCKPIT_GMAIL_MAX_RESULTS` controls the preview/page size, while `JOBCOCKPIT_GMAIL_SCAN_MAX_RESULTS` controls how many matching Gmail messages scheduled processing and approved imports may scan past already processed or newly arrived messages.

Rendered LinkedIn/Indeed fallback can use Codex CLI, a direct persistent Playwright profile, or Chrome CDP attach. Set `JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=playwright_profile` to reuse `JOBCOCKPIT_PLAYWRIGHT_USER_DATA_DIR`, or `chrome_cdp` to attach to an already-running local Chrome started with `--remote-debugging-port`. See `docs/external-posting-discovery.md` for Chrome profile and CDP setup options.

Retry unresolved LinkedIn/Indeed links from the CLI after changing resolver settings or when a site was temporarily blocked:

```powershell
pnpm --silent cli links retry-unresolved --json
pnpm --silent cli links retry-unresolved --limit 2 --json
pnpm --silent cli links retry-unresolved --link-id JOB_ALERT_LINK_ID --json
```

The Sources UI also exposes a per-link Retry button for unresolved job links. CLI retries process each link in its own transaction and record failed attempts back on the link as `retry_failed: ...`.

### Scheduled active-job status reconciliation

The status reconciler checks stored posting URLs only. It skips closed and expired
opportunities, plus any job marked as applied. It never searches by title or company
and it does not refresh snapshots, parsing, fit analysis, or application records.
Run the database migration before the first check:

```powershell
pnpm --silent python scripts/migrate.py
```

Review a non-mutating sample, then a small real run:

```powershell
pnpm --silent cli jobs reconcile-status --dry-run --limit 100 --rendered-limit 30 --json
pnpm --silent cli jobs reconcile-status --limit 5 --rendered-limit 5 --json
pnpm --silent cli jobs status-report --json
```

Install or update the logged-on-user Windows task after reviewing those results:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_job_status_reconciliation_task.ps1
```

The installer creates two local-time tasks with independent throughput budgets. The
midnight task processes up to 1,000 oldest-due active jobs, permits up to 1,000
rendered checks, and runs for at most two hours. The 2 PM task processes up to 100
jobs, permits up to 30 rendered checks, and runs for at most 45 minutes. Override
the `MidnightLimit`, `MidnightRenderedLimit`, `MidnightMaxRuntimeSeconds`,
`AfternoonLimit`, `AfternoonRenderedLimit`, or `AfternoonMaxRuntimeSeconds`
installer parameters to tune either profile. Each task ignores overlapping starts;
the reconciler's singleton lease also prevents the two profiles from running at the
same time. The runner appends one JSON record per run under
`var/logs/job-status-reconciliation/`.

Scheduled rendered checks allow scripts and supporting resources from any public
HTTP(S) domain. No asset-domain whitelist is required. The browser loads assets
natively, preserving redirected URLs, module imports, stylesheet URLs, cookies,
compression, integrity checks and request bodies. The proxy validates DNS and
connects only to the validated numeric addresses. Localhost, private networks and
unsupported protocols remain blocked.

Navigation, redirects, workers and WebSockets use the same public-DNS boundary.
Public redirect hops are allowed to complete normally, with no navigation gate or
asset queue. Before accepting a capture, the renderer checks the final URL against
the source/provider origins and verifies the posting's identity and content.
Visiting an intermediate public domain does not make it an approved final source.
Browser deadlines and process cleanup remain enforced. The pinned Firefox wrapper
still needs a version-specific repair for route commands; page service-worker
registration stays disabled.

Extend a provider's accepted final-page origins with a JSON map in `.env`, for example:

```dotenv
JOBCOCKPIT_SCHEDULED_RENDERED_EXTRA_DOMAIN_GROUPS={"jobs.vendor.example":["careers.vendor.example"]}
```

Keys are source-domain suffixes and values are additional provider-owned domain
suffixes. Wildcards and empty groups are rejected.


### 6. Chrome Extension Setup

Fast path: build the extension and launch a dedicated Chrome dev profile with it loaded:

```bash
pnpm extension:dev
```

The command uses `var/chrome-extension-dev-profile`, loads `apps/chrome-extension`, and opens `http://localhost:3000` by default, or the `JOBCOCKPIT_WEB_PORT` value from `.env`. The launcher prefers Chrome for Testing, Chromium, or Playwright's bundled Chromium before system Google Chrome because managed Chrome installations can ignore command-line unpacked extension loading. By default it passes `--disable-extensions-except` so the profile is isolated to JobCockpit; use `pnpm extension:dev -- --allow-other-extensions` when you need to install Web Store extensions such as Codex in the same dev profile. If Chrome or Chromium is not in a standard location, pass it explicitly:

```powershell
pnpm extension:dev -- --chrome "C:\Path\To\chrome.exe"
```

If you force a managed system Google Chrome and the extension does not appear on `chrome://extensions`, use the manual fallback below or run with a Chrome for Testing/Chromium executable instead.

Manual fallback: build the extension:

```bash
pnpm --filter @jobcockpit/chrome-extension build
```

Load it in Chrome:

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click Load unpacked.
4. Select `apps/chrome-extension`.
5. Click the extension icon on the application page to open its side panel and grant temporary access.
6. After rebuilding, reload the extension and application page. The panel header shows version and build ID.

Choose the matching saved job, confirm it, and **Scan current page**. Review the source context, optionally **Draft answers with Astra**, edit and approve individual answers, then **Fill approved answers**. The same flow works on the local native mock at `http://localhost:8001/apply/native-file-input`.

Native Workday fields support exact reviewed insertion. Custom controls stay visible for manual completion; other sites support draft/copy only. No action clicks submit, continue, apply, or signature controls. Local undo cannot reverse a site's autosaves.

Review each PDF and its exact target before attaching. Static profile resumes are bound to an approved package for the selected job; generated PDFs need existing package clearance. **Learn from my answers** lets you select completed page or panel answers, review Astra's proposed personal facts, and confirm their country, term or application limits before reuse. **My facts** provides corrections, expiration, deletion and original source answers. See [the extension guide](docs/chrome-extension.md) for model settings, approval expiry, tests, and runtime-only packaging (`pnpm extension:package`).

### 7. Extension Document Tokens

After a package passes truthfulness checks, create a short-lived extension session:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/extension/packages/PACKAGE_ID/session `
  -ContentType "application/json" `
  -Body '{"document_ids":["DOCUMENT_ID"],"expires_in_seconds":300}'
```

The response includes download URLs for approved package documents. These URLs require signed short-lived tokens and only work for documents attached to the approved package manifest.

## Safety And Privacy

JobCockpit is not a mass-apply bot.

It should not:

- Auto-submit applications.
- Fabricate candidate claims.
- Mutate Gmail mailboxes.
- Read arbitrary local files.
- Expose unapproved documents to the extension.
- Commit local runtime state or private artifacts.

Run the privacy scan before sharing changes:

```bash
pnpm --silent python scripts/privacy_scan.py .
```

## Helpful Docs

- [Sharing the source](docs/open-source-sharing.md)
- [Contributing](CONTRIBUTING.md)
- [Security and private data](SECURITY.md)
- [Runtime and worker operations](docs/runtime-operations.md)
- [Browser health](docs/browser-health.md)
- [Optional outbound publication](docs/publication.md)

- `docs/architecture.md`
- `docs/sqlite-alembic.md`
- `docs/artifact-storage.md`
- `docs/gmail-job-alert-ingestion.md`
- `docs/chrome-extension.md`
- `docs/document-upload-safety.md`
- `docs/local-ats-mock-site.md`
- `docs/ats-provider-ingestion.md`
- `docs/workday-ingestion.md`
- `docs/url-fetch-policy.md`
- `docs/responsible-ai.md`
