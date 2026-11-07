# External Posting Discovery

Some job-alert and job-board links point to aggregators. LinkedIn and Indeed job links are handled as identity hints first: JobCockpit extracts the company, title, location, and source URL from the email or board row, then searches for an official/company posting elsewhere. If no official posting is found, JobCockpit can fall back to the public aggregator job posting itself.

The reusable link-processing boundary lives in `jobcockpit.ingestion.posting_links`:

- `classify_posting_link` classifies direct posting links, external job-board links, social links, and footer links.
- `process_posting_link` is the central source-neutral entrypoint for Gmail, future Outlook ingestion, and job-portal/listing sources.
- `PostingLinkContext` carries the source type, original URL, link text, surrounding text, and normalized raw row text.

External posting discovery lives behind that processor in `jobcockpit.ingestion.posting_discovery`:

- `ExternalJobBoardAdapter` implementations define source-specific URL matching without putting source rules in a giant classifier branch.
- `LinkedInJobBoardAdapter` recognizes LinkedIn job URLs, including nested paths such as `/comm/jobs/view/...`.
- `IndeedJobBoardAdapter` recognizes individual Indeed job URLs such as `/viewjob`, `/rc/clk`, and `/pagead/clk`.
- `ExternalPostingHint` carries source-neutral identity fields from Gmail, job boards, GitHub lists, or future discovery sources.
- `PostingDiscoveryResolver` verifies employer postings first, explicitly searches LinkedIn job details for Indeed hints, then uses the original Indeed URL as the last page fallback.
- `SerperSearchClient` is the sole built-in web-search client and maps Serper organic results into the shared search-result contract.
- Serper search results still pass through aggregator filtering, URL policy, ATS fetching, rendered validation, and confidence checks before import.
- `PrioritizedRenderedPageResolver` uses Chrome CDP first and invokes `CodexCliRenderedPageResolver` only when CDP cannot return a resolved page. URL-policy rejection is terminal and never falls through to browser control.
- `CodexCliRenderedPageResolver` is the no-new-process fallback that controls the user's existing Chrome session. It refuses standalone Playwright, CDP, Computer Use, or any path that launches a separate browser.
- Custom official-posting resolvers and search providers can still be registered through `jobcockpit.ingestion.llm_providers`; no Codex web-search provider is built in.

Gmail, posting retries, API URL ingestion, and portal/listing sources use the shared `process_posting_link` or `process_posting_links` boundary. Browser-backed resolution from any of those sources therefore follows the same CDP-first provider. If no resolver is configured, external links are stored locally with `ingestion_status="needs_external_resolution"` and no job is imported.

When a resolver imports an official/company posting, the fetched official page or ATS detail response is stored as a private `job_source_archive` artifact and exposed through the job source archive download URL. When official discovery finds no match, the fallback fetch stores the public LinkedIn or Indeed posting the same way and adds resolution evidence to the imported job text.

Search tries reviewed employer APIs/domains, exact and relaxed company/title queries,
then employer-domain searches. When general results do not identify a potential
employer domain, one company careers query supplies leads; at most two matching
domains receive an unquoted-title search. These leads grant no destination trust:
every fetched posting still passes URL policy and independent identity checks.
The generic phase uses at most five queries, followed by at most one targeted
`site:linkedin.com/jobs/view` query for an Indeed hint. Known Serper failures stop
further queries against the provider in that attempt.

LinkedIn selection considers the bounded result pool, requires an individual job
URL, and verifies fetched company, title, location and term plus description
content before replacement. Company jobs pages are never replacement postings.
If LinkedIn cannot be verified, the original Indeed URL is still tried. Failed
replacement metadata is never imported as if its page had been recovered.

When only company/title metadata can be retained, the result is a reviewable
`aggregator_structured_fallback`. The existing [source discovery queue](source-reconciliation.md)
can recover a verified description into that same job on a later attempt. A
missing identity remains `needs_external_resolution` on the email link.

Some ATS and aggregator pages block plain HTTPS fetches from `httpx`. For those
pages, enable a rendered fallback. The fallback opens only the exact posting URL,
inspects rendered content, does not perform general browsing/searching, and does
not click Apply, Submit, Continue, or e-signature controls.

Serper is JobCockpit's sole built-in web-search transport. Configure
official-posting discovery with:

```dotenv
JOBCOCKPIT_POSTING_RESOLVER_PROVIDER=search
JOBCOCKPIT_POSTING_SEARCH_PROVIDER=serper
JOBCOCKPIT_POSTING_SEARCH_MAX_RESULTS=5
JOBCOCKPIT_SERPER_API_KEY=
JOBCOCKPIT_SERPER_COUNTRY=ca
JOBCOCKPIT_SERPER_LANGUAGE=en
JOBCOCKPIT_SERPER_TIMEOUT_SECONDS=15
JOBCOCKPIT_SERPER_MAX_ATTEMPTS=2
JOBCOCKPIT_POSTING_EXTERNAL_RESOLUTION_CONCURRENCY=4
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=invisible_playwright
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_TIMEOUT_SECONDS=180
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_CONCURRENCY=1
```

Search results are filtered before any fetch. Candidate URLs remain subject to
the normal public-URL policy, redirect checks, ATS fetch and rendering paths,
and company/title confidence checks. On Serper failure, JobCockpit never falls
back to Codex search; it may retain a rendered aggregator fallback as
`needs_review` with the stable Serper failure code in the resolution trace.

Company-profile research also discovers candidate official pages with Serper,
fetches up to three safe pages, and gives Codex only a bounded untrusted evidence
packet. Codex synthesis runs with web search disabled.

The `playwright_profile` provider is the isolated-browser alternative: it launches
a separate persistent Playwright browser instead of reusing the user's current
Chrome process. Use it only when that isolation is intentional, not as a fallback
for sites that trust the existing interactive Chrome session:

```dotenv
JOBCOCKPIT_POSTING_SEARCH_PROVIDER=serper
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=playwright_profile
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_TIMEOUT_SECONDS=240
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_CONCURRENCY=1

# Optional. Defaults to ./var/playwright-profile, which is ignored by git.
JOBCOCKPIT_PLAYWRIGHT_USER_DATA_DIR=./var/playwright-profile

# Optional. Set to chrome to use installed Google Chrome instead of bundled Chromium.
JOBCOCKPIT_PLAYWRIGHT_CHANNEL=chrome

# Optional. Use when pointing USER_DATA_DIR at a Chrome User Data root.
# Examples: Default, Profile 1
JOBCOCKPIT_PLAYWRIGHT_PROFILE_DIRECTORY=Default

# Optional. Set false when you need to interactively sign in to the profile once.
JOBCOCKPIT_PLAYWRIGHT_HEADLESS=true
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_AUTO_PREPARE=true
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_TIMEOUT_SECONDS=60
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_COOLDOWN_SECONDS=300
```

To attach to an already-running Chrome profile and retain the existing-Chrome Codex
path as a fallback, use the `chrome_cdp_first` rendered provider. Use `chrome_cdp`
instead when failure to connect to CDP should stop browser resolution immediately.
Start Chrome with a local debugging port, for
example from PowerShell:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\JobCockpit\ChromeCdpProfile"
```

Then configure JobCockpit:

```dotenv
JOBCOCKPIT_POSTING_SEARCH_PROVIDER=serper
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=chrome_cdp_first
JOBCOCKPIT_CHROME_CDP_ENDPOINT_URL=http://127.0.0.1:9222
JOBCOCKPIT_CHROME_CDP_AUTO_START=false
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_TIMEOUT_SECONDS=240
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_CONCURRENCY=1
```

The CDP endpoint is restricted to localhost because it exposes browser control.
JobCockpit opens and closes a new tab but does not terminate the Chrome process.
Automatic CDP Chrome launching is disabled by default and must be explicitly enabled.
Install browser binaries once after enabling the direct Playwright provider:

```bash
pnpm --silent python -m playwright install chromium
```

Avoid pointing JobCockpit at a daily Chrome profile while Chrome is already
running. Prefer the default `./var/playwright-profile` or a dedicated Chrome
profile because Chromium-based profiles can be locked or damaged when multiple
browser instances use the same profile at the same time.

### Firefox ingestion browser

The optional `invisible_playwright` provider uses patched Firefox with its own
persistent profile, with no automatic Chrome CDP fallback. It covers aggregator
resolution, direct posting fallback, official candidate rendering, Eluta,
Canadian Tech Internships, CSJobs, and scheduled status reconciliation.
Legacy providers (`invisible_playwright_first`, `chrome_cdp`, `chrome_cdp_first`,
`playwright_profile`, and `codex_cli`) remain explicit troubleshooting options.
The browser dependency is pinned in the `invisible-browser` extra. The public
sample disables rendered fallback until you configure a provider explicitly.

Stop local JobCockpit processes before syncing dependencies on Windows. If `.env`
selects a separate Python environment, first export its uv settings as shown in
[runtime maintenance](runtime-operations.md#prepared-runtime), then run:

```powershell
uv sync --extra invisible-browser
$env:INVISIBLE_PLAYWRIGHT_CACHE_DIR = Join-Path (Get-Location) 'var/invisible-playwright-cache'
pnpm --silent python -m invisible_playwright fetch
```

Select the provider in `.env` and restart the API/workers:

```dotenv
JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=invisible_playwright
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_USER_DATA_DIR=./var/invisible-playwright-profile
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_SEED=42
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_TIMEZONE=America/Denver
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_LOCALE=en-US
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_AUTO_PREPARE=true
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_TIMEOUT_SECONDS=60
JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_COOLDOWN_SECONDS=300
JOBCOCKPIT_PLAYWRIGHT_HEADLESS=true
```

The timezone and locale are explicit so a failed upstream egress lookup does not
prevent launching. Set them appropriately for your connection. The fixed seed and
separate profile retain a consistent browser identity and session between runs.
Indeed and Eluta use separate directories under the configured root:
`indeed/seed-42` and `eluta/seed-42` for the default seed. Changing the seed selects
a different directory; existing legacy profiles at the root are left intact.
Keep that directory under ignored `var/`; never point it at a Chrome profile.
The engine cache also defaults to `var/invisible-playwright-cache`; an explicit
`INVISIBLE_PLAYWRIGHT_CACHE_DIR` process environment variable overrides that path.
Use `JOBCOCKPIT_PLAYWRIGHT_HEADLESS=false` for an interactive sign-in or challenge.
The existing browser timeout, challenge-wait, retry, pacing, and concurrency
settings still apply. The upstream launcher manages browser startup separately
from the page-navigation timeout.

For configuration experiments, the pinned upstream launcher also supports:

- `timezone="auto"` and `locale="auto"`, selectable through the corresponding
  JobCockpit environment settings, to derive the values from the connection.
  Explicit values avoid the extra external lookup during browser startup.
- Hidden headed rendering with `headless=True` on Windows. This is different
  from native headless rendering, selected with the process environment variable
  `INVPW_TRUE_HEADLESS=1`. Unset that variable to restore the default renderer.
- A persistent `profile_dir` paired with a fixed `seed`. Keep the same seed for
  a reused profile and use a new directory when testing another identity.

Upstream `humanize=True` is already the default and affects pointer interactions;
JobCockpit's preparation step initiates recognized verification interactions.
The upstream `prep_recaptcha` option
is disabled when a persistent profile is supplied and generates synthetic cookie
values rather than establishing a verified session. It is not a JobCockpit setting.
See the upstream [configuration guide](https://github.com/feder-cr/invisible_playwright/blob/main/docs/configuration.md)
and [launcher contract](https://github.com/feder-cr/invisible_playwright/blob/main/src/invisible_playwright/launcher.py).

This setting covers shared posting discovery across sites, Eluta's search/detail
HTML fallback, and employer-link browser fallbacks in Canadian Tech Internships
and CSJobs. Direct HTTP/API paths remain first where already supported. Eluta's
Chrome fallback retains its persistent source session. Automatic verification
interactions remain restricted to the observed Indeed and Eluta flows. Resolution traces identify
`invisible_playwright`; browser DOM evidence, final-URL checks, the local
DNS-pinning proxy, WebRTC/service-worker restrictions, and page cleanup remain
in place. No external proxy subscription is needed.
The pinned Firefox wrapper cannot intercept WebSockets. Both ordinary loads and
preparation retain native WebSocket and dedicated/shared worker APIs, routing
their connections through the validating proxy. Firefox's loopback proxy bypass
and direct-connection fallback are disabled. Real-engine tests verify that these
APIs work for allowed destinations and cannot connect to unapproved local ports.
Removing the APIs caused Indeed's "Browser not supported" verification page.

Automatic preparation runs only after the guarded loader encounters a verification
or blocked page on Indeed or Eluta. It closes that browser, acquires the same
profile lock, and opens a temporary preparation context. This context preserves
native worker/WebSocket APIs while keeping the validating proxy, URL/DNS checks,
and WebRTC, WebTransport, and service-worker restrictions. Connections and
redirects may reach any validated public HTTP(S) destination. The final preparation
page must belong to the source provider or an approved verification origin before
the app inspects or interacts with it.

Preparation may click one recognized Indeed verification checkbox, or an Eluta
reCAPTCHA checkbox and then the matching `/sandbox` verification form's Continue
button. It never operates application forms or generic Continue buttons. Unknown
or unsuccessful challenges return `needs_review`. The default preparation wait
is 60 seconds; a failed attempt applies a persisted five-minute cooldown shared
across importers. Set `JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_AUTO_PREPARE=false` to retain
passive verification handling.

Eluta search preparation uses a simple `q=co-op` search on the same origin because
observed verification forms corrupt complex encoded search destinations. The
form must still match that preparation URL exactly. Once the session is prepared,
the guarded loader reloads the original search URL and verifies its query identity;
bootstrap search results cannot substitute for the requested results.

After preparation, the browser closes and the ordinary guarded loader reopens
the same profile and reloads the original URL. Identity, content, and confidence
checks still govern the result; reaching a challenge completion page alone does
not approve a posting. The observed empty "Authenticating..." transition does
not finish preparation. Indeed sign-in redirects and HTTP error pages are rejected
as posting content and reported separately in browser health. Successful resolution evidence records preparation when
it ran. Profiles are reused on subsequent imports and preparation is attempted
again only if verification returns. Verify live source behavior separately from
local test results; neither browser compatibility nor challenge acceptance is
guaranteed across future site changes.

For comparison, retry the same failed URLs with this provider, then restore
`JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER=invisible_playwright` (or your previous
value) and restart. Compare full posting content, job identity, and resolution
traces; a successful navigation alone does not establish successful ingestion.
Changing the browser does not force already completed imports to run again.
Keep the extra installed during the experiment: `uv sync` or `uv run --exact`
without `--extra invisible-browser` removes optional packages. Existing launchers that
use `uv run --no-sync` retain the installed extra.

Scheduled status reconciliation uses Invisible Playwright in a dedicated worker
process per rendered check. The coordinator terminates the owned process tree
on completion, cancellation, or the scan deadline, including Firefox and its
driver. Windows uses a kill-on-close Job Object, established before rendering,
to retain ownership even if a worker crashes. Workers do not write job status.
Scheduled origin restrictions remain in force; automatic session preparation and
retries are disabled to preserve the one-navigation budget. The PowerShell runner
explicitly selects `invisible_playwright`. Chrome is never an automatic fallback.

The optional runtime regression uses only local fixture pages and verifies
capture, automatic preparation and guarded reload, session reuse, worker/socket
network isolation, private-redirect blocking, and process cleanup:

```powershell
$env:JOBCOCKPIT_TEST_INVISIBLE_BROWSER = '1'
pnpm --silent python -m pytest tests/test_invisible_browser_runtime.py -q
Remove-Item Env:JOBCOCKPIT_TEST_INVISIBLE_BROWSER
```

Codex CLI parsing is a separate optional step. Enable it when generic or aggregator fallback
pages need richer company, timing, deadline, compensation, and requirements extraction than the
heuristic parser can provide:

```dotenv
JOBCOCKPIT_POSTING_PARSER_PROVIDER=codex_cli
JOBCOCKPIT_POSTING_PARSER_TIMEOUT_SECONDS=60
JOBCOCKPIT_POSTING_PARSER_MAX_INPUT_CHARS=12000
```

Profile-aware fit analysis is a separate optional LLM step. Ingestion queues
provider-backed analysis after saving the posting; the fit worker updates only
the `fit_analysis` field on the job record. Disabled providers do not queue
automatic work, and local heuristic fitting may run inline:

```dotenv
JOBCOCKPIT_FIT_ANALYSIS_PROVIDER=codex_cli
JOBCOCKPIT_FIT_ANALYSIS_TIMEOUT_SECONDS=60
JOBCOCKPIT_FIT_ANALYSIS_MAX_INPUT_CHARS=16000
```

The fit-analysis prompt includes:

- `source_url`: the resolved job URL, when present.
- `job`: the parsed job object, including source, employer, role, timing,
  location, eligibility, compensation, application, fit_analysis, and
  data_quality. Long role descriptions are capped before prompt construction.
- `posting_text`: the raw posting text capped by
  `JOBCOCKPIT_FIT_ANALYSIS_MAX_INPUT_CHARS`.
- `candidate_profile`: only `summary`, `skills`, `city`, `province_state`,
  `country`, `current_year`, `graduation_date`, `gpa`, and `co_op_program`.
  The summary is capped at 1000 characters; skills are capped at 30 items and
  80 characters each.
- `allowed_evidence`: only evidence rows where `allowed_for_resume=true`, with
  each row limited to `claim`, `skills`, and `source_type`. The prompt includes
  at most 20 allowed evidence rows; claims are capped at 500 characters, and
  each row includes at most 12 skills capped at 80 characters each.

The fit-analysis prompt does not include profile email, phone, street address,
postal code, full name, LinkedIn URL, GitHub URL, cover-letter custom
instructions, disallowed evidence rows, raw resume files, private artifacts, or
Gmail message content.

To refresh fit after profile or evidence changes without re-fetching the
posting, use the job detail page's `Refresh fit` action or call:

```http
POST /api/jobs/{job_id}/fit-analysis
```

This enqueues a `fit_analysis` run in the same background worker table used by
full job re-processing. The worker reads the stored job snapshot for posting
context, loads the current profile and allowed evidence, and updates only the
job's `fit_analysis` field.

Board and Gmail ingestion share `execute_posting_items`, which uses
`prepare_posting_batch_item` in a bounded worker pool and
`persist_posting_batch_item` on the calling thread for duplicate checks and
independent database transactions. The typed preparation retains the filter
decision so persistence does not filter the same input again. Preparation includes
direct fetches as well as external resolution and never receives a database
session or artifact writer. Gmail pools links across selected messages.
`JOBCOCKPIT_INGESTION_LINK_CONCURRENCY` defaults to 4; external-resolution and
rendered-fallback settings cap the effective pool. See
[concurrency and failure handling](ats-provider-ingestion.md#concurrent-runs).
The standalone `process_posting_links` entrypoint remains available for callers
that own their own batch transaction.

Adapter-backed ingestions that already fetched a trusted posting snapshot set
`trusted_posting=true` and `fetch_source_archive=false`. Those snapshots are
imported directly even when the retained apply URL points to an external job
board such as LinkedIn or Indeed. The external URL remains the application URL,
and the provider detail URL remains in `portal_source_url`; Chrome/Codex
resolution is reserved for links that do not already include a trusted posting
snapshot.

Codex CLI remains available for parsing, rendered-page inspection, fit analysis,
company-profile synthesis, and document generation. It is never given web-search
capability. Company-profile synthesis receives only the pages fetched through the
public URL policy and runs with search disabled.

Codex calls use the model and reasoning configured by
`JOBCOCKPIT_CODEX_CLI_MODEL` and `JOBCOCKPIT_CODEX_CLI_REASONING_EFFORT`,
plus `--ignore-user-config`, `--cd <workspace>`, and `--sandbox read-only`.
Set `JOBCOCKPIT_CODEX_CLI_IGNORE_USER_CONFIG=false` only when a non-searching
Codex provider intentionally needs global configuration. CDP itself does not
require global Codex config. Keep
`JOBCOCKPIT_CODEX_CLI_RENDERED_IGNORE_USER_CONFIG=false` only when
`chrome_cdp_first` should fall back to Codex browser control.

To plug in another full posting resolver, register a provider during application
startup and set `JOBCOCKPIT_POSTING_RESOLVER_PROVIDER` to that provider name:

```python
from jobcockpit.ingestion.llm_providers import register_posting_resolver_provider
from jobcockpit.ingestion.posting_discovery import PostingResolution


class MyPostingResolver:
    def resolve(self, hint):
        return PostingResolution(status="not_found", error="not implemented")


register_posting_resolver_provider("my_resolver", lambda settings: MyPostingResolver())
```

To plug in a custom web-search backend, register a
provider during application startup and set `JOBCOCKPIT_POSTING_SEARCH_PROVIDER`
to that provider name:

```python
from jobcockpit.ingestion.llm_providers import register_search_provider
from jobcockpit.ingestion.posting_discovery import SearchResult


class MySearchClient:
    def search(self, query: str, limit: int):
        return [SearchResult(title="Example", url="https://careers.example/jobs/1")]


register_search_provider("my_search", lambda settings: MySearchClient())
```

Safety rules:

- Prefer official/company postings over aggregator pages.
- Block configured aggregator hosts from official-search candidates and redirect targets.
- Fetch the original aggregator job URL only through the explicit fallback policy for recognized LinkedIn or Indeed job links.
- Apply the normal URL fetch policy to every candidate official posting URL.
- Keep unresolved or low-confidence results reviewable and retryable instead of importing automatically.
- Keep Gmail read-only; this flow does not mutate mailbox state.
