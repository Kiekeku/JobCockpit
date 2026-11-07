# ATS Provider Ingestion

JobCockpit imports external ATS postings through provider adapters behind one endpoint:

```text
POST /api/jobs/ingest/ats
```

The endpoint validates the requested provider, applies the URL fetch policy to the source URL, lets the adapter fetch normalized raw-text job snapshots, then persists each snapshot through the normal artifact-backed job ingestion path.

`GET /api/ats/provider-defaults` returns form defaults for every registered
provider. The Sources page and CLI shortcuts share `ingestion/ats_defaults.py`
for source URLs, search text, link limits, and filter flags. Users can edit the
form or override CLI options. Employer-specific portals require a supplied URL.
`GET /api/ats/providers` retains its list response, and API requests retain their
existing validation and omitted-limit default of 10.

## Concurrent runs

Different sources may ingest concurrently, including API and CLI processes using
the same SQLite database. A renewable database lease excludes overlapping runs
for the same provider and source URL path (search queries and facets share that
scope). A conflicting request returns HTTP 409 before fetching. Retry it after
the active run finishes. A crashed owner's lease expires automatically.

Fetching, parsing, enrichment and artifact file preparation occur before the
short SQLite writer transaction. After acquiring `BEGIN IMMEDIATE`, ingestion
rechecks the snapshot hash, source URL and current indexed source identity. The
job, artifact metadata, follow-up queues and ingestion item result commit
together. A reused job discards redundant prepared files; a rolled-back import
removes its prepared files. Caller-owned capture transactions retain their
existing fences and rollback boundaries.

Writer acquisition retries SQLite busy/locked errors up to four attempts, with
the connection's five-second busy timeout and short exponential backoff. Parsing
and network calls are not repeated. Acquisition happens before a database
snapshot or write, so it does not require replaying the caller's transaction.
Exhausted retries retain the ordinary durable item-failure reporting. Scope
ownership is checked under the writer lock before writes and commits, preventing
an expired owner from publishing progress.
After rollback and scope exit, interrupted ATS runs are marked failed in a
separate transaction guarded by their run ID and running status. Committed items
are retained, pending items become retryable failures, and replacement runs and
leases are left intact.

Each process limits preparation to four source/adapter runs, four direct fetch
calls, four parse/enrichment calls,
two external resolutions and two rendered browser calls at once. Existing
provider-specific browser/profile guards still apply. These resource budgets
are per process; the database writer and source leases coordinate across
processes. Link preparation within a run uses a bounded pool; completed links
commit independently on the calling thread, without waiting for a slower link.
No SQLAlchemy session or artifact writer is passed to preparation workers.
Source runs use a separate semaphore from direct fetches, so an adapter waiting
for link workers never holds the fetch permits those workers need.

`JOBCOCKPIT_INGESTION_LINK_CONCURRENCY` controls the pool (default 4; range 1-4).
The effective pool is also capped by external-resolution concurrency and, when
rendered fallback is enabled, rendered-fallback concurrency (default 2).
Set ingestion concurrency to 1 to process links sequentially. Concurrent board
and Gmail runs share a process-wide cap of four link preparations and two per
original link hostname. Redirect destinations retain their existing URL policy
checks. Browser/profile guards and Eluta request pacing remain in force.

CS Jobs, all three GitHub internship boards, Hanzilla, Workday, and Phenom Oracle
fetch eligible details concurrently. Pagination remains sequential. RankAlpha's
feed already supplies posting bodies, and Oracle Candidate Experience accepts a
single detail URL; their outputs use the same shared preparation/write pipeline.
Eluta serializes its stateful page fetches and stops further page requests on a
verification challenge, while redirect resolution and downstream preparation
can overlap. Adapter results retain listing order; persistence completes in
completion order. Attempt limits include failures and are reserved before dispatch.

Gmail collects eligible links across selected messages before parallel preparation;
each message is checkpointed when its last link finishes (including failed links),
and messages with no pending links are checkpointed immediately. An interrupted
run therefore retains completed-message states for subsequent retries.
Mailbox reads and mutations remain unchanged (read-only). Per-link preparation,
URL-policy rejection, and write failures are recorded independently. The writer
rechecks duplicates, so two links resolving to the same job reuse one posting.
A failed import rolls back only its transaction and artifacts. Invalid source
configuration, listing failures, verification gates, and lost run ownership can
still stop a source/run.
Persistent Playwright launches also hold an OS file lock in the resolved profile
directory through browser cleanup and Playwright shutdown. Different profiles
can run concurrently; launches sharing a profile wait for its owner. The OS
releases the file lock if the owner process exits.

Each endpoint call creates a `JobIngestionRun` and writes the same shared `AuditLogEntry` lifecycle events used by Gmail ingestion:

- `ingestion.run_started`
- `ingestion.run_completed`
- `ingestion.run_failed`

Run records include provider, source URL, search text, seen/created/duplicate counts, status, alert level, and any error message. Recent runs are returned by `GET /api/ingestion-runs` and are shown in the web dashboard's Ingestion runs panel. Adapter failures are logged by the backend and surfaced as `alert_level="error"` run records.

Portal jobs are committed independently. A blocked, expired, or otherwise failing link is recorded
without rolling back jobs that were already imported from the same portal run. Each discovered link
has a durable `JobIngestionItem` row containing its source, resolved and canonical URLs, processing
status, linked job ID, error, and retryability. Runs with one or more item failures finish as
`completed_with_warnings`; failures that prevent the adapter from producing a batch still mark the
run as `failed`. Persisted item results are available from
`GET /api/ingestion-runs/{run_id}/items`.

The response from `POST /api/jobs/ingest/ats` includes:

- `jobs_seen_count`
- `jobs_created_count`
- `jobs_duplicate_count`
- `ingestion_run`
- `alerts`
- `jobs`
- `items`

## Providers

### Shared Apply-Page Handlers

Reusable ATS apply-page extraction lives in `jobcockpit.ingestion.ats_handlers`. Job-board adapters, Gmail link ingestion, and future discovery sources should call `AtsApplyPageResolver.resolve(apply_url, AtsPostingContext(...))` instead of implementing provider-specific Apply URL parsing inline.

Aggregator links such as LinkedIn and Indeed job links should be converted to `ExternalPostingHint` values and routed through `jobcockpit.ingestion.posting_discovery`. That keeps company/title extraction reusable across Gmail, general job boards, and future discovery adapters while still verifying candidate official postings through this shared apply-page resolver before using the explicit public aggregator fallback.

The shared resolver currently handles:

- Workday CXS detail data for supported Workday apply URLs.
- schema.org `JobPosting` JSON-LD used by Ashby, Lever, iCIMS-style pages, and other structured job pages.
- OpenGraph metadata and visible HTML for Greenhouse, SmartRecruiters, Oracle Cloud Candidate Experience, and generic server-rendered job pages.
- Redirect limits and URL policy checks for every Apply URL before fetch.

Generic HTML pages are converted into normalized posting text first. By default,
`jobcockpit.ingestion.parser.parse_job_text` performs deterministic heuristic parsing on that text.
Set `JOBCOCKPIT_POSTING_PARSER_PROVIDER=codex_cli` to let `CodexCliPostingParser` parse the
same bounded posting text into the full `ParsedJob` shape. Codex parsing is optional: failures,
timeouts, invalid JSON, or validation errors fall back to the heuristic result, and structured
provider adapters such as Workday remain the first choice when available. Codex CLI calls are
run with the JobCockpit workspace pinned through `--cd` and with global user config ignored by
default, so unrelated Codex settings do not alter ingestion behavior.

Alternative LLM parsers can be registered without changing the ingestion flow. Register a
provider with `jobcockpit.ingestion.llm_providers.register_posting_parser_provider`, return an
object with `parse(raw_text, source_url=None, baseline=None)`, and set
`JOBCOCKPIT_POSTING_PARSER_PROVIDER` to that provider name. The parser should return the same
`ParsedJob` contract used by the built-in parser.

### Workday

Provider name:

```text
workday
```

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"workday","source_url":"https://suncor.wd1.myworkdayjobs.com/Suncor_External","search_text":"engineer","limit":5}'
```

The Workday adapter accepts public `myworkdayjobs.com` listing/detail URLs and supported `myworkdaysite.com/recruiting/.../job/...` detail URLs. It calls Workday CXS listing/detail endpoints, sends `search_text` as `searchText`, pages with offset/limit, and stores imported rows with `source_type="workday"` for compatibility with the original MVP implementation.

### Phenom / Oracle

Provider name:

```text
phenom_oracle
```

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"phenom_oracle","source_url":"https://careers.atco.com/global/en/search-results","search_text":"engineer","limit":5}'
```

The Phenom/Oracle adapter currently supports ATCO public careers URLs under `careers.atco.com/global/en`. The site is a Phenom careers wrapper whose job apply links point to Oracle Candidate Experience. The adapter calls:

- `POST https://careers.atco.com/widgets` with `ddoKey="refineSearch"` for search results.
- `POST https://careers.atco.com/widgets` with `ddoKey="jobDetail"` for each selected result.

It sends `search_text` as `keywords`, pages with `from`/`size`, converts HTML descriptions to text, keeps the Oracle apply URL in the stored raw snapshot, and stores imported rows with `source_type="ats_phenom_oracle"`.

ATCO job detail URLs can also be imported directly:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"phenom_oracle","source_url":"https://careers.atco.com/global/en/job/18677/Building-Operator-5th-or-4th-Class-Power-Engineer","limit":1}'
```

### Hanzilla

Provider name:

```text
hanzilla
```

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"hanzilla","source_url":"https://jobs.hanzilla.co/internships/","search_text":"python","limit":5}'
```

The Hanzilla adapter supports public pages under `jobs.hanzilla.co`, including the internships listing and individual `/jobs/.../` detail pages. Hanzilla is a static student-job board that links out to employer application pages, so the adapter:

- Reads listing pages and extracts `.job-card` entries already loaded in the HTML.
- Uses `search_text` as a local all-term filter against each card's search text, title, company, location, and visible text.
- Fetches each selected Hanzilla detail page to read schema.org `JobPosting` data, visible summary text, and the direct company apply link.
- Stores the direct company apply URL as the imported job source URL when available.
- Records imported rows with `source_type="ats_hanzilla"`.

Hanzilla detail URLs can also be imported directly:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"hanzilla","source_url":"https://jobs.hanzilla.co/jobs/software-developer-co-op-hyperconverged-solutions-fall-term-ross-video-703375871/","limit":1}'
```

### Eluta

Provider name:

```text
eluta
```

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"eluta","source_url":"https://www.eluta.ca/search?q=co-op+sort%3Arank+field%3A%22Computing+-+Software+%26+Web+Development%22+category_id%3A72&pg=1","limit":20}'
```

The Eluta adapter supports public pages under `eluta.ca`, including `/search` result pages and individual `/spl/...` detail pages. Eluta encodes target filters such as `field:` and `category_id:` inside the `q=` query, so leave `search_text` empty to use the source URL's full query expression. Supplying `search_text` replaces the `q=` value while preserving the other query parameters.

Eluta's rendered fallback uses `invisible_playwright` by default for both search
and detail pages. HTTP is attempted first. Chrome and legacy fallback chains
require explicit selection; disabled or unsupported providers never silently
select Chrome. See [installation and switching instructions](external-posting-discovery.md#firefox-ingestion-browser).

The adapter:

- Tries a certificate-verified, DNS-pinned HTML request before opening the configured browser, and switches the rest of the current run to rendered fetching when the HTML response is blocked or unusable.
- Applies `JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS` between Eluta requests for both the primary HTML path and rendered fallback.
- Reads `.organic-job` search results from the HTML.
- Follows only same-origin `/spl/...` detail links from listing rows.
- Pages through search results with the `pg=` query parameter until `limit` non-excluded detail links are attempted or Eluta stops exposing a next-page link.
- Reads each detail page for title, employer, location, posted timestamp, summary, and description.
- Resolves Eluta `/direct/i` apply redirects without following employer pages.
- Stores the resolved employer apply URL as the imported job source URL when available.
- Records imported rows with `source_type="ats_eluta"`.
- Stops the current crawl after the first detail page requires human verification, preserving any jobs already collected and recording the gated link as a retryable warning.

Eluta detail URLs can also be imported directly:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"eluta","source_url":"https://www.eluta.ca/spl/software-developer-co-op-fall-term-411136c44c9af16e2bb21e5741517d11","limit":1}'
```

### Canadian Tech Internships

Provider name:

```text
canadian_tech_internships
```

Example:

```powershell
pnpm --silent cli ats ingest-canadian-tech --limit 30
```

The shortcut defaults to the public GitHub README URL, `--search-text software`, and `--limit 30`, meaning up to 30 matching README apply links are scanned. The equivalent direct API call is:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"canadian_tech_internships","source_url":"https://github.com/negarprh/Canadian-Tech-Internships-2026?tab=readme-ov-file","search_text":"software","limit":30}'
```

The Canadian Tech Internships adapter supports the public `negarprh/Canadian-Tech-Internships-2026` GitHub README. The README is only the discovery source; imported job snapshots come from the actual employer posting linked by each `Apply` cell through the shared `AtsApplyPageResolver`.

The adapter:

- Fetches the raw GitHub README for stable markdown parsing.
- Reads the `Company`, `Role`, `Location`, `Apply`, and `Date Posted` table columns.
- Skips closed rows and rows without an open `Apply` link.
- Carries the previous company name for continuation rows in the list.
- Uses `search_text` as a local all-term filter against company, role, location, date, and apply URL.
- Follows each selected `Apply` URL to collect real posting content, capped by `limit` before duplicate and stale-link skips are applied.
- Uses Workday CXS detail data for supported Workday apply URLs, including `myworkdayjobs.com` and public `myworkdaysite.com/recruiting/...` links.
- Uses schema.org `JobPosting` JSON-LD for ATS pages that expose it, including Ashby, Lever, and iCIMS-style postings.
- Uses OpenGraph metadata and visible HTML text for server-rendered boards such as Greenhouse and SmartRecruiters.
- Uses OpenGraph title/description metadata for Oracle Cloud Candidate Experience pages when the page body is a JavaScript shell.
- Blocks private-network, localhost, unsupported-protocol, and DNS-failed apply URLs before fetching them.
- Records imported rows with `source_type="ats_canadian_tech_internships"`.

A live scan of the current README shows the board includes multiple ATS surfaces rather than one provider: Workday, Greenhouse, Oracle Cloud Candidate Experience, Ashby, SmartRecruiters, Lever, iCIMS, and smaller one-off job boards. The adapter therefore treats the GitHub table as discovery only and extracts posting details from the linked employer page.

### Canadian Tech Internships 2027

Provider name:

```text
canadian_tech_internships_2027
```

Example:

```powershell
pnpm --silent cli ats ingest-canadian-tech-2027 --limit 30
```

This source reads the exact `README-2027.md` file in `negarprh/Canadian-Tech-Internships-2026`. It locates the five-column listings table by its `Company`, `Role`, `Location`, `Apply`, and `Date Posted` header, so headings, paragraphs, lists, badges, separators, or unrelated tables before the listings are ignored. It otherwise shares the 2026 source's continuation-row handling, closed-row filtering, apply-page resolution, URL policy, deduplication, and scanned-link limit. Imported rows use `source_type="ats_canadian_tech_internships_2027"`.

### Canada Tech Internships Summer 2027

Provider name:

```text
canada_tech_internships_summer_2027
```

Example:

```powershell
pnpm --silent cli ats ingest-canada-tech-2027 --limit 30
```

The adapter supports the public `michelleokolie/canada-tech-internships-summer-2027` GitHub README and reads its `Company`, `Role`, `Location`, `Application/Link`, and `Date Posted` columns. It uses the same shared GitHub collection and apply-page resolver as the Canadian Tech Internships 2026 source, including cross-source duplicate skipping, per-link stale/timeout handling, URL policy enforcement, search filtering, and scanned-link limits. Imported rows use `source_type="ats_canada_tech_internships_summer_2027"`.

### RankAlpha Jobs API

Provider name:

```text
rankalpha_jobs
```

Put the bearer token in the ignored local `.env` file and restart the backend:

```dotenv
JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN=replace-with-current-token
```

Then use the CLI shortcut:

```powershell
pnpm --silent cli ats ingest-rankalpha `
  --source-url 'https://jobs.rankalpha.io/api/jobs?tag=Python&tag=SQL&sort=deadline&order=asc' `
  --limit 50
```

Or call the local API directly:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"rankalpha_jobs","source_url":"https://jobs.rankalpha.io/api/jobs?country=United%20States&workAuthorizationFit=Likely%20eligible%20for%20Canadian%20student","limit":50}'
```

The adapter is read-only and only accepts HTTPS requests to
`https://jobs.rankalpha.io/api/jobs`. It rejects token-like query parameters, reads the bearer
token from backend settings, passes filters through to RankAlpha, and supports repeated query
values. Multiple status, country, company, or location values therefore remain OR conditions;
multiple tags remain an AND condition; and different categories remain combined with AND.
Supplying JobCockpit `search_text` replaces the RankAlpha `q` value while preserving every other
filter. `applied_facets` can also provide scalar or repeated values.

The adapter verifies the documented `data`, `pagination`, and `appliedFilters` response envelope,
pages until the JobCockpit import limit is filled, and does not count already-imported canonical
job URLs against that limit. Each accepted job is normalized into parser-friendly text, while its
exact API job object is stored as a private JSON source snapshot. The source archive retains only
safe response metadata and never stores the Authorization header. Imported rows use
`source_type="ats_rankalpha_jobs"`.

## Adapter Contract

Adapters implement `AtsProviderAdapter` from `jobcockpit.ingestion.ats_adapters.base`:

- `provider`: stable provider key used in API requests.
- `validate_source_url(source_url)`: reject unsupported hosts, protocols, or paths with `AtsProviderError`.
- `fetch_jobs(AtsFetchRequest)`: return `AtsJob` rows with a canonical source URL and raw text.

Register new adapters in `jobcockpit.ingestion.ats_adapters.registry`.

## Safety Notes
- An item-level failure rolls back only that item's database writes and removes artifacts written by that item.
- Batch-level adapter or source-validation failures still produce a failed ingestion-run audit record.

- Adapters are read-only. They must not apply, authenticate, submit, or mutate ATS state.
- Provider URL validation is adapter-specific and runs before fetch.
- The global URL fetch policy still blocks unsupported protocols and private-network destinations.
- Imported job snapshots are stored as private artifacts. Exact duplicates are matched by `posting_snapshot_hash`; URL-backed imports also skip an existing canonical `source_url` before writing a new artifact.
- Duplicate skips are counted on the ingestion run and returned as warning alerts.
- Use targeted `search_text` values and conservative `limit` values for live sites.
