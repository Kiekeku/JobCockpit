# Original company source reconciliation

Trusted board snapshots import immediately. A board URL is recorded as an
`aggregator_fallback` with discovery `not_attempted`, rather than an official
source. Importing a snapshot does not establish employer authority.

`job_source_reconciliations` stores one durable discovery lifecycle per job,
separately from content reprocessing and availability checks. A dedicated API
source worker schedules missing rows for eligible saved jobs in batches of 25.
It uses the `source_reconciliation_queue` lease, so slow external discovery does
not occupy the capture/reprocessing worker. Reviewed HTTP clients already close
within each request's context manager, including failures.

For standalone operation, use `pnpm --silent cli worker source`; add `--once`
to process at most one due job. A busy source lease is reported without borrowing
another worker's identity. Restart existing API/worker processes after upgrading
so the old shared-worker implementation is no longer running.

Eligible jobs are active, unmerged, unapplied, and have a recognized board URL,
a source-repair flag, or a missing URL/recognized listing or apply route.
Unknown generic detail routes are retained, including query-based job identities;
a generic-path heuristic failure alone does not prove that a posting needs repair.
Manual source overrides are excluded. Merely importing a fallback successfully
does not remove it from this queue.

## Verification and search limits

Search tries reviewed employer APIs/domains, the original query, a query
without mandatory location/term text, then up to two employer-domain searches.
One company careers query can discover domain leads. Metadata-only Indeed jobs
also receive a targeted LinkedIn detail search before remaining unresolved.
URLs are deduplicated across batches;
at most `min(3 * posting_search_max_results, 20)` candidates are fetched. A
successful verification ends discovery immediately. Provider failures remain
in the attempt history, and an immediate generic provider failure prevents
another generic query against the same provider.

The expected employer, title, and location are not passed into candidate page
extraction. The apply-page adapter returns its observed title and text separately
from the provenance hints retained for ordinary ingestion. Search snippets and
expected identity cannot prove a candidate's identity. Verification requires a
detail URL, employer authority, matching observed title, observed location when
specified, and compatible employment timing when specified. Reviewed aliases are
shared by search, company-name comparison, and career-site authority checks.
Unknown cities, title variants, or ambiguous timing can still require review.

At most two candidates per resolution receive rendered recovery after an HTTP
failure or an insufficient page. URL-policy failures and HTTP 404/410 responses
are not retried through the renderer. Rendered recovery requires independent
browser DOM evidence, preserved URL identity, and the same content checks.

## Queue lifecycle and persistence

The queue exposes pending, running, retry_scheduled, resolved, attempted,
unavailable, blocked, and cancelled states. Transient/unresolved outcomes receive
up to five attempts, with delays of one hour, six hours, one day, and seven days.
Exhaustion stops automatic retries. Manual retry starts a new bounded cycle and
retains historical attempts. Configuration/policy blocks and duplicate candidate
identities require review.

The supervisor lease fences claims and commits. Network discovery runs outside
database transactions. Before committing, the worker checks its claim and the
source URL/metadata, job identity, timing, snapshot/artifact, application metadata,
active status, and current applied/manual-override policy. A changed job cancels
that result. Interrupted claims retain a failure entry and use bounded backoff.

A verified replacement uses the same source-update boundary as manual source
editing: source metadata, source identities, status-check invalidation, and an
audit entry update together. The original snapshot bytes, parser-owned fields,
and snapshot hash remain intact for jobs that already have a description.
The worker reads the stored snapshot before treating an `aggregator_structured_fallback`
job as incomplete: that path also contains complete descriptions. A shared content
assessment excludes persisted discovery status, resolution evidence, source URLs,
and repeated identity fields. It distinguishes descriptions, metadata-only content,
observed blocked/authwall pages, and uncertain content. Short uncertain text or a
description retained only inside legacy source evidence is protected, as are
unreadable snapshots; these retain source-only updates. Metadata-only and blocked jobs receive the verified
description and optional source archive through the existing job-update and
artifact-storage boundaries. Parsing happens before the guarded writer; a changed
job cancels the result. The job ID is retained, and outdated fit analysis is
cleared and, when a fit provider is enabled, a fit-analysis refresh is queued
against the recovered description. An identity-only
response does not count as enrichment. Original/fallback URLs and a verification
content hash remain in provenance. A candidate already associated with another
saved job is left for review; discovery does not merge jobs automatically.
Successful same-URL repairs also resume ordinary availability checking.

Recovery regressions round-trip actual structured fallback output through the link
processor, artifact storage, database, discovery resolver, and worker. They cover
employer and LinkedIn recovery, complete-content preservation, provenance-only
candidate rejection, parse/write rollback, concurrent edits, unreadable snapshots,
and one fit refresh after successful replacement. The content assessment does not
alter snapshot bytes or parser grammar. Read-only corpus audits classify stored
evidence; they do not run network discovery or rewrite saved jobs.

The September 6 Indeed discovery regressions use the observed ATCO employer-name
variants (five matching saved email contexts) and Teledyne listing fallback
(three matching contexts). Tests use minimized service-contract examples rather
than private posting documents. Parser grammar and golden expectations are unchanged.

Job detail shows discovery state, retry time, and attempt history. Operators can
also use `GET /api/jobs/{id}/source-reconciliation` and
`POST /api/jobs/{id}/source-reconciliation/retry`.

## Upgrade and validation

Apply the migration with `pnpm --silent python scripts/migrate.py`, then restart the
application/worker. Revision `20260905_0027` only creates the queue; it does not
rewrite existing job rows. Existing unresolved jobs become eligible for scheduling
when the upgraded worker runs.

The source-discovery regressions are grounded in the September 4 saved-posting
audit: trusted CSJobs imports, engineering-role and pharmacy-city mismatches,
numeric student-listing URLs, career shells, AMD legal/Canada aliases, and Hexagon
official-site discovery. Committed tests minimize the source structures and do
not contain saved job IDs or private posting documents. This change does not
alter shared parser grammar or golden expected projections; the golden corpus
remains the parser correctness gate. Private audit and saved-parser reports stay
under `var/reports/source-audit-20260904/`.
