# Ingestion CLI output

JobCockpit ingestion commands emit one versioned machine-readable contract when
run with `--json`. This applies to Gmail processing and every
`jobcockpit ats ingest-*` shortcut.

The top-level fields are:

- `schema_version`: currently `1`.
- `operation`: `job_ingestion`.
- `status`: `completed`, `completed_with_warnings`, or `failed`.
- `provider`: the requested ATS provider or `gmail`.
- `runs`: one normalized entry per source run.
- `totals`: counts aggregated across `runs`.
- `alerts`: operator-facing warnings and errors.

Every run has the same fields: `run_id`, `provider`, `source_id`,
`source_url`, `status`, `counts`, `items`, `alerts`, and `metadata`. Fields that
do not apply to a provider are `null`; they are not removed.

Every item has the same fields: `item_id`, `source_url`, `resolved_url`,
`canonical_url`, `status`, `linked_job_id`, `error_message`, `retryable`, and
`metadata`.

The shared count fields are:

- `items_seen_count`
- `jobs_created_count`
- `jobs_reused_count`
- `items_skipped_count`
- `items_needing_review_count`
- `items_failed_count`
- `runs_failed_count`

Provider-specific evidence remains under each run's `metadata`. ATS metadata
contains imported jobs, filter skips, and link failures. Gmail metadata contains
the display name, processed messages, review count, and the always-empty
`mailbox_mutations` list. Gmail remains read-only.

Gmail item URLs, error text, and provider-derived metadata are intentionally
returned as `null` or empty values. Email links can contain addresses, tracking
parameters, and bearer-style tokens; they must not be written to Task Scheduler
logs. Local item IDs and linked job IDs remain available for investigation.

Example:

```json
{
  "schema_version": 1,
  "operation": "job_ingestion",
  "status": "completed",
  "provider": "hanzilla",
  "runs": [
    {
      "run_id": "ingestrun_example",
      "provider": "hanzilla",
      "source_id": null,
      "source_url": "https://jobs.hanzilla.co/internships/",
      "status": "completed",
      "counts": {
        "items_seen_count": 1,
        "jobs_created_count": 1,
        "jobs_reused_count": 0,
        "items_skipped_count": 0,
        "items_needing_review_count": 0,
        "items_failed_count": 0,
        "runs_failed_count": 0
      },
      "items": [],
      "alerts": [],
      "metadata": {}
    }
  ],
  "totals": {
    "items_seen_count": 1,
    "jobs_created_count": 1,
    "jobs_reused_count": 0,
    "items_skipped_count": 0,
    "items_needing_review_count": 0,
    "items_failed_count": 0,
    "runs_failed_count": 0
  },
  "alerts": []
}
```

The normalized output is a projection over existing persistence. ATS continues
to use `JobIngestionRun` and `JobIngestionItem`; Gmail continues to use
`EmailIngestionRun`, `JobAlertEmail`, and `JobAlertLink`.

## Shared ingestion audit

ATS and Gmail retain their provider-specific run and item tables, but both write the same append-only lifecycle events through `IngestionAuditService`:

- `ingestion.run_started`
- `ingestion.run_completed`
- `ingestion.run_failed`

Every event uses `entity_type="ingestion_run"` and the same versioned payload fields: provider, run ID, source ID, source URL, status, standardized counts, and a bounded error code. Gmail source URLs and raw error text are never included. Gmail start and failure events are committed outside the source-processing transaction so the audit remains durable when email and link writes roll back. Failed Gmail CLI output carries the same run ID as its audit events.
