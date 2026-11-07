# Architecture

JobCockpit is a single-user local-first application. The backend owns parsing, persistence, document generation, Gmail ingestion, truthfulness checks, and extension-safe document download. The frontend is a Next.js dashboard. The Chrome extension uses a persistent side panel for job-scoped question review and grounded AI drafts. Approved native-field insertion supports Workday and local mock flows; unknown controls remain manual. Posting capture uses activeTab after an explicit user click.

`api/extension_assistance.py` owns revisioned review sessions, answer approvals, and purpose-bound document access. `extension/answers.py` generates structured drafts through the existing Codex CLI; `extension/sessions.py` freezes and revalidates profile/job/evidence/personal-fact context around model work. `api/extension_memory.py` and `extension/memory.py` own selected-answer interpretation, explicit fact confirmation, scope/expiration, correction and deletion. Learning proposals and confirmed facts are separate tables; only confirmed facts can enter draft context. No database write transaction is held during inference. `content.ts` owns browser field identity, exact insertion, readback, and local undo; it does not call the model or choose answers. The side panel passes only reviewed plans to exact frame/document IDs. See `docs/chrome-extension.md` for scope, lifecycle, and remaining manual controls.

## Runtime boundaries

`api/main.py` constructs the FastAPI application, dependencies, lifespan, and HTTP
routes. `services/workflows.py` owns ingestion, reprocessing, and queued document
orchestration. CLI commands and ingestion integrations import that service module
without importing or constructing the HTTP application. Existing parsing,
document, storage, and queue modules retain their specialized contracts.

`services/worker_supervisor.py` provides the shared worker start/stop, lease,
polling, and recovery lifecycle. API work, cover-letter generation, and source
reconciliation retain independent leases and claim tokens. Provider/network work
runs outside write transactions; workers recheck ownership and input guards at
commit. SQLite remains the local MVP database.

## Posting ingestion

`ingestion/ats_defaults.py` owns portal URL, search, limit, and filter defaults.
CLI shortcuts read it directly; the Sources page reads the same catalog through
`GET /api/ats/provider-defaults`. Provider selection creates a fresh editable
form, and metadata refetches preserve edits. Employer-specific portals start
with a blank URL. The existing provider-list endpoint remains available.

Source adapters acquire content and produce the shared internal
`ingestion/inputs.py::JobIngestPayload`. HTTP request validation remains at the
transport boundary. The internal input retains browser identity and URL-safety
fields, portal aliases, source-resolution evidence, and optional archive bytes.

`ingestion/item_execution.py` owns the ATS/Gmail item lifecycle: bounded source
preparation, a transaction per completed item on the calling thread, rollback
cleanup for newly written artifacts, and a separate transaction for failure
records. Completion callbacks run after commit. ATS keeps its source counters;
Gmail keeps its message checkpoints, retry rules, and private-link redaction.
`integrations/gmail_runs.py` supplies the same durable run audit lifecycle to
the API and CLI. Gmail access remains read-only.

`prepare_posting_batch_item` returns a typed preparation that carries the filter
decision into `persist_posting_batch_item`. Saving does not repeat filters.
An index hit can defer source fetching, but the writer still checks the current
database record; a disappeared hit goes through normal source preparation.
Snapshot, source identity, and uniqueness checks remain authoritative inside
the write transaction.

`services/posting_state.py` shares artifact preparation and parsed-field mapping
between creation and update. Updates explicitly preserve manual review and
verified source aliases. Duplicate imports share one finalization step for
supplemental evidence, status, aliases, identities, and cover-letter scheduling.
Captured artifacts are adopted only after capture ownership checks succeed.

Reprocessing computes a `JobReprocessComputation` on a detached posting copy
using read-only database access. It prepares files and an explicit field change
set without inserting or flushing temporary database rows. The worker rechecks
its lease, claim, posting timestamp, and any refreshed source conflict before
applying the changes, artifact references, identities, audit, and document queue
effects together. Failed or superseded attempts remove their new files.

`ingestion/enrichment.py` centralizes fit scheduling. Disabled providers produce
no automatic fit work. Heuristic fitting can run inline; provider-backed fitting
is queued after import for manual, URL, ATS, and Gmail inputs. Capture and source
repair defer all enabled fitting because they hold guarded write transactions.
The import can complete while its `reprocess` state is queued. Fit workers use
the current profile and allowed evidence. A full refresh request can promote an
unclaimed fit run; the type of a running claim is preserved.

## Profile document uploads

`services/profile_uploads.py` shares bounded validation, private artifact storage,
parsing, latest-document metadata, and upload auditing for resumes and cover-letter
samples. Only resumes create claim evidence. Supported unreadable documents remain
stored with parse warnings. A failed transaction removes its newly stored bytes
and rolls back metadata, evidence, and latest-document pointers together.

## Dashboard reads

The dashboard uses `GET /api/job-summaries`, with a default page size of 100 and
maximum of 500. This projection reads only displayed JSON fields and status
metadata, with application summaries in the same query. Full descriptions and
evidence remain available through the job detail endpoint. `/api/jobs` retains
its full response for existing consumers.

Search (`q`, `term`), status/verification filters, missing-term/review/applied
filters, sorting (`created_at`, `fit`, `term`), and paging run on the server.
Filtering and sorting currently operate on the compact projection in Python,
preserving the existing computed availability and timing presentation. They do
not load full ORM postings or evidence. Global dashboard counts accompany each
page. If the local dataset becomes substantially larger, moving these computed
filters and pagination into SQL is a further optimization.

Latest reprocess and capture histories are selected using SQL window functions,
so only the selected run for each job is materialized. Revision `20260905_0028`
adds the supporting reprocess index and persistent generation request inputs.

ATS ingestion builds an ID-only normalized source-URL lookup once per batch,
updates it after successful imports, and loads the matching job only when needed.

## Durable manual document generation

Manual cover-letter requests return HTTP 202 with a persisted `run` and a
`result_url`. This applies to the job draft endpoint, package draft endpoint, and
package creation when a cover letter is required. Resume-only package creation
remains synchronous. The web detail page polls generation state and refreshes
the document workspace when processing finishes.

Clients may supply a `request_id`. Retrying the same ID and inputs returns the
same run, including after completion; a new ID requests a new revision. Reusing
an ID with different inputs is rejected. The queue records the selected resume
artifact and hash, target package if supplied, and posting snapshot guard.
Workers validate those inputs before generation and before committing documents.
When a posting refresh supersedes a manual run, the replacement retains the
resume and package selections and receives a new request ID. The original run
remains cancelled with an audit link to its replacement.
An already queued automatic request can adopt a manual request before processing
starts; a conflicting active request is rejected.

`GET /api/cover-letter-generation-runs/{run_id}/result` returns the completed
workspace (or the document/package view specified in `result_url`). Pending or
changed-input requests return 409; failed generation returns a sanitized error.
Generation creates unapproved documents and preserves existing review/download
guards. Word revision uploads remain synchronous local document operations.

## Source reconciliation scheduling

The source worker reads at most 100 unscheduled candidate rows per sweep, every
60 seconds by default (`SOURCE_SWEEP_INTERVAL_SECONDS`). A cursor advances past
ineligible candidates and restarts after the final page. The read session closes
before candidates are rechecked and enqueued in short individual write
transactions. Idle queue claims do not scan postings. Ingestion still enqueues
eligible new work directly; periodic sweeps discover older unresolved work.
Collision checks use the indexed identity registry first, verifying matches
against current URL fields because registry entries can be historical. A compact
URL projection covers legacy postings without registry entries; neither path
loads full postings, descriptions, or evidence into the write transaction.

## SQLite constraint repair

Revision `20260905_0029` restores omissions found in the local schema: snapshot
uniqueness, one application per candidate/job, one email processing state per
source/message, eight document/email foreign-key relationships, and the capture
sequence index. Existing healthy databases are not rebuilt, and previously
applied migrations remain unchanged.

The repair requires an online Alembic migration. It checks duplicate keys and
orphaned references before rebuilding affected tables, preserves existing rows,
indexes, views, and triggers, and checks foreign-key integrity afterward. An
explicit SQLite transaction covers table replacement and revision advancement;
failure rolls back the entire repair. The migration connection uses SQLite's
default foreign-key enforcement setting of OFF for batch replacement, while
normal application connections continue to enforce foreign keys. A downgrade
retains these constraints because they are already part of the preceding schema
contract.

Before repairing an existing database, take a SQLite backup and validate the
migration on a copy. Run `pnpm --silent python scripts/migrate.py` to apply it. Do not
delete or merge conflicting rows automatically if the preflight rejects data.

## September 2026 validation

On the local 1,879-posting dataset, a read-only three-request probe measured a
100-row dashboard response of 162,985 bytes versus 52,751,224 bytes for the full
jobs endpoint. Median local request times were approximately 200 ms and 666 ms,
respectively. These are local measurements, not a browser or provider benchmark.
The normalized ATS identity lookup took approximately 38 ms and materialized no
ORM postings.

Validation included 3,024 passing tests, all 1,476 golden parser cases with zero
field differences, touched-file Ruff checks, the privacy scan, and production
web/extension builds. No parser grammar or approved golden projection changed.
