# Browser resolution health

The dashboard polls `/api/browser-health` every 30 seconds. It tracks new
Invisible Playwright attempts from ingestion, source discovery, and scheduled
availability scans. It does not reconstruct older runs or track HTTP-only work.

Counts cover the last 24 hours. The last successful resolution is searched for
across seven days. A successful browser resolution does not mean a job was
imported. A completed successful attempt for the same original URL automatically
clears earlier failures from the open-failure count, failure reasons, and recent
issues. The successful attempt must start after the failure finishes; an older
or overlapping attempt does not clear a newer failure. Matching uses the private
original-URL hash, so sanitized links cannot accidentally merge different jobs.
The dashboard shows recovered failures separately and retains their original
records and factual 24-hour totals. Needs-review and overdue attempts remain
visible until dismissed or completed. Each issue can be dismissed, or all
current issues can be dismissed together. Dismissal hides
the current outcome from the open-issue counts and recent list, while retaining
the original attempt record and the factual 24-hour totals. A later failure on
the same posting is a new attempt and remains visible. An overdue attempt that
later finishes with a failure appears again because its outcome changed.

Failures include empty bodies, timeouts, missing browser dependencies, blocked
access, worker errors, and cleanup failures. An attempt with no completion past
its expected budget is marked overdue, not conclusively failed. Known ATS frames
that are absent from text capture are flagged for review. Greenhouse job_app
frames matching the employer page's gh_jid are captured directly, including when
cross-origin element handles fail. A collapsed matching frame is expanded to its
loaded document height. Unsupported or ambiguous embeds still need review; this
does not detect or repair every visually blank page.

The pinned proxy allows 64 active connections per browser and 128 per process,
with at most 192 active or waiting handlers. A connection burst can wait up to
five seconds for a slot; closing the browser proxy interrupts that wait.
Persistent exhaustion is reported as `browser_proxy_capacity_exhausted`, distinct
from a site verification wall. Public-IP checks and authenticated proxy access
still apply to every admitted connection.

Eluta preparation cooldown and unresolved preparation retain their verification
classification. Eluta listing traversal stops at that gate, and CS Jobs does not
start a second browser fallback after its Eluta apply resolver has already
attempted session recovery. Other CS Jobs links and ordinary transport failures
retain their existing resolution and fallback behavior.

Private start and completion records are stored through ArtifactStorageService
under the configured artifact root's `browser-health` directory. This preserves
worker failure evidence independently of ingestion database transactions. URLs
exclude credentials, fragments, and query parameters except recognized posting
identifiers. Raw exception messages and page bodies are not stored. Records are
not automatically deleted; the dashboard reads a bounded recent window.
Dismissals are stored as separate private artifacts under
`browser-health-dismissals`; they do not delete or rewrite attempt records.
New failed attempts also show an allowlisted resolver error code. Recognized
HTTP error or sign-in pages can show the HTTP status and final host, plus only
the known Indeed `/auth` or `/account/login` path. Session IDs, query strings,
raw page titles, and arbitrary exception details are excluded. Older attempts
cannot be retroactively enriched.

Scheduled Firefox scans allow Indeed's observed verification dependencies at
`challenges.cloudflare.com` and `hagen.challenges.cloudflare.com`, on HTTPS port
443 only. Other source sites do not inherit this
exception, and private-network/DNS checks still apply. The scan first permits passive
verification. On an Indeed verification wall, the coordinator may grant at most
two session-preparation attempts per scan, shared across all its browser workers.
The second grant requires the previous worker's process tree to be cleaned up and
the configured preparation cooldown to pass (five minutes by default). Recovery
requires at least 90 seconds remaining for preparation and the guarded reload.
This reuses ingestion's observed verification controls, with the scheduled URL
policy, a maximum 60-second preparation timeout, and the same owned process tree
and hard scan deadline. Successful preparation is followed by one guarded reload
of the posting. Disabling automatic preparation disables this recovery too.
No application controls are operated. Its ordinary sign-in/verification wait is capped at ten seconds (or a lower
configured wait), so repeated access walls cannot consume the interactive wait
for every posting. Unresolved walls remain inconclusive; they do not mark jobs
closed or expired. Interactive ingestion retains its configured wait and
preparation behavior.
New records include allowlisted recovery grant/denial and recheck outcomes,
without turning a successful recovery into an open issue. These diagnostics do
not contain browser exception text or verification tokens.

Scheduled Firefox checks require at least 30 seconds remaining after acquiring
the host lock. Checks that cannot fit are deferred without launching a browser
or recording a timeout; the scan's hard deadline still applies to active workers.

Workday application-entry URLs ending in `/apply` are normalized to their job
detail route before API lookup. Company-link verification also retains locations
from the current Workday posting's location block when JSON-LD lists only a
remote province. Ingestion text precedence is unchanged. Reviewed employer names
include Dotdash Meredith, People Inc., and Investopedia Canada, Inc., scoped to
the observed Meredith Workday career site.
The company-link path retries an empty capture once when rendered retries are
enabled. It does not retry access-policy or cleanup failures; a second empty
capture stays unresolved and leaves the saved aggregator fallback available.

Diagnostic links retain provider-specific posting identifiers while removing
tracking and session parameters. Saved-job links use the hash of the original
URL, independently of the sanitized link. For matching saved jobs, the API also
reconstructs safe links from the stored URL to repair older diagnostic records.

Monitoring is enabled by default. `JOBCOCKPIT_BROWSER_HEALTH_ENABLED=false`
disables new records. The dashboard reports disabled monitoring, unreadable
records, and API failures explicitly.

Indeed discovery reads the rendered posting's title and employer when a URL-only
request lacks those hints. This requires the same Indeed job identity and a
substantial full-description section; blocked, truncated, or empty captures
still require review. An observed "Apply on company site" link is followed with
the guarded Firefox resolver before search. The destination must pass employer,
title, and description checks. This navigation is monitored as a separate
attempt; a failed destination leaves the readable Indeed fallback available.
Reviewed employer aliases include Foresters Financial and the Canadian legal
entity named in its Workday posting, The Independent Order of Foresters - Canada.
Background source reconciliation also follows this link before search.
Disabling aggregator fallbacks does not disable company-link discovery or permit
an unresolved company link to return the Indeed posting as a fallback.

Signed-in LinkedIn captures stop after the complete description component for
the requested job. Oversized recommendation lists do not make that description
truncated; oversized descriptions and mismatched job components still do.
Interview-process prose mentioning "email or phone call" is not a sign-in
prompt; credential field labels and actual authentication pages remain blocked.
Availability checks load the guarded page and then verify its identity. For a
matching LinkedIn posting URL, the observed document title can establish the
employer and exact role/year tokens despite spacing or Intern/Co-op ordering.
This does not relax ingestion's confidence threshold or accept another job ID.
Firefox also waits briefly for Workday to populate its page, including when
an Indeed Apply redirect lands there. Verified employer-posting resolutions
are classified as resolved in both the attempt and the saved source metadata.
