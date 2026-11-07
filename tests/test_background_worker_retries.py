from __future__ import annotations

from jobcockpit.services import workflows as api_main
from jobcockpit.api.job_reprocess_runs import (
    claim_job_reprocess_run,
    enqueue_job_reprocess,
    record_job_reprocess_failure,
)
from jobcockpit.db.models import JobPosting, JobReprocessRun
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.llm_providers import register_posting_parser_provider


def test_job_reprocess_transient_failure_is_scheduled_for_retry(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job_payload = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Intern\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_payload["id"])
        run, queued = enqueue_job_reprocess(job, db)
        assert queued is True
        run_id = run.reprocess_run_id
        assert claim_job_reprocess_run(db, run_id) is not None

    payload = record_job_reprocess_failure(
        run_id,
        settings,
        RuntimeError("temporary provider failure"),
        retryable=True,
        error_code="provider_unavailable",
    )

    assert payload["status"] == "retry_scheduled"
    assert payload["attempts"] == 1
    assert payload["next_attempt_at"] is not None
    assert payload["retryable"] is True
    with session_scope(settings) as db:
        run = db.get(JobReprocessRun, run_id)
        assert run.status == "retry_scheduled"


def test_full_reprocess_provider_fallback_retries_without_mutating_job(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class FailingParser:
        last_error = "provider_timeout"

        def parse(self, *_args, **_kwargs):
            raise TimeoutError("provider timed out")

    register_posting_parser_provider(
        "unit_test_reprocess_provider_failure",
        lambda _settings: FailingParser(),
        replace=True,
    )
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job_payload = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original Role\nCompany: Original Co"},
    ).json()
    settings.posting_parser_provider = "unit_test_reprocess_provider_failure"
    with session_scope(settings) as db:
        run, _queued = enqueue_job_reprocess(
            db.get(JobPosting, job_payload["id"]), db
        )
        run_id = run.reprocess_run_id

    result = api_main.process_job_reprocess_run(
        run_id, settings, client.app.state.storage
    )

    assert result["status"] == "retry_scheduled"
    assert result["error_code"] == "posting_parser_fallback"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_payload["id"]).company_name == "Original Co"
