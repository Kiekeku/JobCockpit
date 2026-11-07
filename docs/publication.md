# Optional outbound publication

Disabled by default. No destination, audience or selection values ship with JC.
Keep the policy under ignored `var/private/` and the separate write credential in
an ignored environment file. Do not reuse an inbound/read credential.

## Configuration

Run `pnpm --silent python scripts/migrate.py` before enabling the feature on an existing
database. Set `JOBCOCKPIT_PUBLICATION_POLICY_PATH` to a local JSON policy,
`JOBCOCKPIT_PUBLICATION_WRITE_TOKEN` to a destination write credential, and
`JOBCOCKPIT_PUBLICATION_ENABLED=true`. Restart the API to load configuration.
The normal API background-worker lifecycle starts the publication worker.
Alternatively use `pnpm --silent cli worker publication` or append `--once`.
Only one worker may own the publication lease.

The policy schema is `PublicationPolicy` in
`apps/api/jobcockpit/services/publication_policy.py`. It requires:

- `endpoint`: credential-free HTTPS upsert URL, with no query string or redirect.
- `rules`: declarative predicates over posting data. A leaf specifies `field`,
  `op` and `value`; groups use `all` or `any`. Supported operators are `eq`,
  `gte`, `in`, `between`, `regex`, and `missing`. Missing comparison data fails closed.
- `excluded_providers` and `source_hosts`: inbound provenance that must not be
  republished. Include all aliases of the destination.
- `audience_brief`: public editorial context, never private applicant details.
- `display_term`, `standard_duration_months`, and `country_names`: display
  configuration and explicit source-to-destination country mapping.
- Optional `assessment_model`; otherwise the existing Codex CLI model is used.

The policy and thresholds are deployment choices, not public product defaults.
Invalid configuration fails closed; validate it before enabling. The current
adapter expects the record contract below; this is not an arbitrary webhook.

## Processing and synchronization

Committed ingestion, later enrichment/scoring, and status changes enqueue a
durable outbox row in the same database transaction. No LLM or HTTP work runs
inside that transaction. Rolled-back ingestion cannot publish.

The worker generates `summary`, `verdict` and `tags` from allowlisted posting
facts using JC's existing Codex CLI runner. It does not load applicant profiles,
resumes, application notes, or private suitability explanations. Longer or
unknown commitments carry explicit caveats. Availability must have dated source
evidence; unverified imports are held rather than receiving fabricated verification.

Delivery is a complete JSON upsert with stable `id`, company, title, country,
term, summary, verdict, tags, URL, status, statusEvidence, verified date and
available deadline/next-check/status-change fields. The receiver must accept
`Authorization: Bearer ...`, return 200/201 with `data.id` matching the request,
and the matching top-level `publicationRevision`, and upsert by `id`.
The payload's positive integer `publicationRevision` is persisted with each
committed input change. The receiver must atomically accept only higher revisions,
acknowledge equal revisions without rewriting them, and reject older revisions
(including unversioned writes against a versioned record). Deploy the receiver's
schema and revision-aware contract before enabling publication. Older strict
receivers reject this new field; missing revision acknowledgements are not success.
Do not restore or reset an outbox's revision history while its remote records exist.
Status updates reuse that identity and cached assessment
when editorial inputs have not changed. Once delivery may have occurred,
lifecycle corrections are not suppressed by initial selection thresholds.

Retries reuse the stable identity. Timeouts, rate limits and server failures
back off; permanent HTTP failures and exhausted retries remain visible.
The worker validates its lease and input version before sending and before
acknowledging. A newer change remains queued even if an older request completes.
The HTTP boundary is at-least-once, not exactly-once; destination idempotency is
required. An already in-flight request cannot be recalled on shutdown; atomic
receiver revision checks prevent it from overwriting a newer delivery.
Connections are pinned to the policy-approved public IP addresses and ignore
ambient HTTP proxies. Redirects are never followed.

Outbound IDs are recorded before HTTP delivery. Inbound requests to the configured
source exclude those provider IDs before fetching/processing posting links,
independently of changes to employer URLs. This also protects uncertain delivery
outcomes and retained identities after local deletion. Echo protection continues
with sending disabled while the local policy remains configured. Other destination
records remain eligible for ordinary ingestion but cannot be exported again.

## Operations

For unattended local delivery on Windows, run
`powershell -File scripts/install_publication_worker_task.ps1`.
This starts a hidden continuous worker under the current signed-in user, starts
it again at logon, and checks every five minutes that the task is running.
Overlapping starts are ignored; the existing queue lease also coordinates with
the API's worker. It handles both new publications and committed status-check
updates independently of how ingestion or status reconciliation was launched.
The normal status reconciliation schedules must remain enabled to discover
employer closures. The computer must be awake and the user signed in.
Applied jobs that have been published (or may have been delivered) remain eligible
for due status checks while publication is enabled. Unpublished applied jobs keep
the normal exclusion from status scanning.

Logs are under `var/logs/publication/`; durable delivery results and retries are
in `job_publications`. After changing local configuration, restart the scheduled
task to load it. To disable unattended delivery, disable and stop the
`JobCockpit Publication Worker` task and set the publication flag to false.
JC owns status updates for its published records; the site's source-file scan
must preserve the database overlay and must not replace JC revision numbers.

These local-only routes exist only when the feature is enabled:

- `GET /api/publications`: delivery state and safe attempt history.
- `POST /api/publications/enqueue-existing`: read-only preview of previously
  unqueued eligible jobs; default `dry_run=true`, optional `limit` up to 500.
- `POST /api/publications/enqueue-existing?dry_run=false`: explicitly enqueue
  that batch. Repeat to process additional batches.
- `POST /api/publications/{publication_id}/retry`: retry after correcting a failure.

Enabling does not automatically backfill existing inventory. Future committed
posting changes are evaluated normally. Disabling stops new queueing and sending;
it does not delete remote posts or delivery identities. Keep the local policy and
outbox database to retain loop protection. No token is returned by these routes.

Validate with `pnpm --silent python -m pytest tests/test_publication.py tests/test_rankalpha_jobs_ingestion.py`.
