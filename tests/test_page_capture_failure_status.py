from __future__ import annotations

from datetime import UTC, datetime

from jobcockpit.services import workflows as api_main
from jobcockpit.db.models import JobPosting, PageCaptureRun
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.llm_providers import register_posting_parser_provider


def test_authoritative_url_matched_job_exposes_capture_failure_after_retries(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class FailingParser:
        def parse(self, *_args, **_kwargs):
            raise TimeoutError("parser timed out")

    register_posting_parser_provider(
        "unit_test_url_matched_capture_failure",
        lambda _settings: FailingParser(),
        replace=True,
    )
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://www.linkedin.com/jobs/view/4567890123"
    original = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original Role\nCompany: Original Co"},
    ).json()
    with session_scope(settings) as db:
        job = db.get(JobPosting, original["id"])
        job.source_url = source_url
        job.source = {**job.source, "source_url": source_url}
    settings.posting_parser_provider = "unit_test_url_matched_capture_failure"
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Updated Role | Updated Co",
            "visible_text": "Updated Role\nCompany: Updated Co\nPython",
        },
    ).json()

    for _attempt in range(3):
        with session_scope(settings) as db:
            run = db.get(PageCaptureRun, queued["capture_id"])
            run.next_attempt_at = datetime.now(UTC)
        api_main.process_page_capture_run(
            queued["capture_id"], settings, client.app.state.storage
        )

    job = client.get(f"/api/jobs/{original['id']}").json()
    assert job["employer"]["company_name"] == "Original Co"
    assert job["capture_processing"]["capture_id"] == queued["capture_id"]
    assert job["capture_processing"]["status"] == "failed"
    assert job["capture_processing"]["attempts"] == 3
