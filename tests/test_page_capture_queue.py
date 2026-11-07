from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from jobcockpit.services import workflows as api_main
from jobcockpit.db.models import (
    AuditLogEntry,
    JobPosting,
    PageCaptureRun,
    PageCaptureSnapshot,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.llm_providers import register_posting_parser_provider


def test_page_capture_enqueues_without_invoking_parser(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    calls: list[str] = []

    class ParserThatMustNotRunInRequest:
        def parse(self, *_args, **_kwargs):
            calls.append("parse")
            raise AssertionError("capture request must not invoke the parser")

    register_posting_parser_provider(
        "unit_test_async_capture_request",
        lambda _settings: ParserThatMustNotRunInRequest(),
        replace=True,
    )
    settings = jobcockpit_settings_factory(
        posting_parser_provider="unit_test_async_capture_request"
    )
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/backend-intern",
            "page_title": "Backend Intern | Acme",
            "visible_text": "Backend Intern\nCompany: Acme\nPython APIs",
            "html": "<html><body><h1>Backend Intern</h1></body></html>",
        },
    )

    assert response.status_code == 202
    assert calls == []
    payload = response.json()
    assert payload["capture_id"].startswith("capture_")
    assert payload["status"] == "queued"
    assert payload["status_url"] == (f"/api/extension/page-captures/{payload['capture_id']}")
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, payload["capture_id"])
        assert run is not None
        assert run.status == "queued"
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        raw_artifact = db.get(StoredArtifact, snapshot.raw_content_artifact_id)
        archive_artifact = db.get(StoredArtifact, snapshot.source_archive_artifact_id)
        assert raw_artifact is not None
        assert raw_artifact.privacy_class == "private"
        assert raw_artifact.job_id is None
        assert archive_artifact is not None
        assert archive_artifact.privacy_class == "private"
        assert db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "extension.page_capture_queued",
                AuditLogEntry.entity_id == run.capture_id,
            )
        ).scalar_one_or_none()


def test_page_capture_rejects_invalid_explicit_port_without_creating_queue_state(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com:notaport/backend-intern",
            "page_title": "Backend Intern | Acme",
            "visible_text": "Backend Intern\nCompany: Acme",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Source URL is not a valid capture URL"
    with session_scope(settings) as db:
        assert db.execute(select(PageCaptureRun)).scalars().all() == []
        assert db.execute(select(PageCaptureSnapshot)).scalars().all() == []
        assert db.execute(select(StoredArtifact)).scalars().all() == []


def test_page_capture_worker_creates_job_and_exposes_status(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/data-intern",
            "page_title": "Data Intern | Acme",
            "visible_text": "Data Intern\nCompany: Acme\nLocation: Calgary, AB\nPython SQL",
        },
    ).json()

    result = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert result is not None
    assert result["status"] == "succeeded"
    assert result["job_id"].startswith("job_")
    status = client.get(queued["status_url"])
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    job = client.get(f"/api/jobs/{result['job_id']}").json()
    assert job["employer"]["company_name"] == "Acme"
    assert job["capture_processing"]["capture_id"] == queued["capture_id"]
    assert job["capture_processing"]["status"] == "succeeded"


def test_failed_targeted_capture_leaves_job_unchanged_and_can_be_retried(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class FailingParser:
        last_error = "codex_cli_timeout"

        def parse(self, *_args, **_kwargs):
            raise TimeoutError("private parser timeout detail")

    register_posting_parser_provider(
        "unit_test_async_capture_failure",
        lambda _settings: FailingParser(),
        replace=True,
    )
    settings = jobcockpit_settings_factory(
        posting_parser_provider="unit_test_async_capture_failure"
    )
    client = jobcockpit_client_factory(settings)
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Original Role\nCompany: Original Co",
        },
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/new-role",
            "page_title": "New Role | New Co",
            "visible_text": "New Role\nCompany: New Co\nPython",
            "target_job_id": original["id"],
        },
    ).json()

    for _attempt in range(3):
        with session_scope(settings) as db:
            run = db.get(PageCaptureRun, queued["capture_id"])
            run.next_attempt_at = datetime.now(UTC)
        api_main.process_page_capture_run(queued["capture_id"], settings, client.app.state.storage)

    job = client.get(f"/api/jobs/{original['id']}").json()
    assert job["employer"]["company_name"] == "Original Co"
    assert job["capture_processing"]["status"] == "failed"
    assert job["capture_processing"]["attempts"] == 3
    assert job["capture_processing"]["retryable"] is True
    assert job["capture_processing"]["error_code"] == "codex_cli_timeout"
    assert job["capture_processing"]["error_message"] == ("Configured posting parser timed out")
    assert "private parser timeout detail" not in job["capture_processing"]["error_message"]

    retry = client.post(f"/api/extension/page-captures/{queued['capture_id']}/retry")
    assert retry.status_code == 202
    assert retry.json()["capture_id"] != queued["capture_id"]
    with session_scope(settings) as db:
        retried = db.get(PageCaptureRun, retry.json()["capture_id"])
        assert retried.retry_of_capture_id == queued["capture_id"]
        assert retried.snapshot_id == db.get(
            PageCaptureRun, queued["capture_id"]
        ).snapshot_id
        assert db.get(JobPosting, original["id"]).company_name == "Original Co"


def test_invalid_parser_response_fails_capture_without_retry(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class InvalidParser:
        last_error = "codex_cli_invalid_parsed_job: role.job_title missing"

        def parse(self, *_args, **_kwargs):
            return None

    register_posting_parser_provider(
        "unit_test_async_capture_invalid_response",
        lambda _settings: InvalidParser(),
        replace=True,
    )
    settings = jobcockpit_settings_factory(
        posting_parser_provider="unit_test_async_capture_invalid_response"
    )
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/invalid-parser-response",
            "page_title": "Software Engineer Intern | Acme",
            "visible_text": "Software Engineer Intern\nCompany: Acme\nBuild Python services.",
        },
    ).json()

    result = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retryable"] is False
    assert result["error_code"] == "codex_cli_invalid_parsed_job"
    assert result["error_message"] == "Configured posting parser returned an invalid response"


def test_invalid_json_parser_response_is_retried(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class InvalidJsonParser:
        last_error = "codex_cli_invalid_json"

        def parse(self, *_args, **_kwargs):
            return None

    register_posting_parser_provider(
        "unit_test_async_capture_invalid_json",
        lambda _settings: InvalidJsonParser(),
        replace=True,
    )
    settings = jobcockpit_settings_factory(
        posting_parser_provider="unit_test_async_capture_invalid_json"
    )
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/invalid-json-response",
            "page_title": "Software Engineer Intern | Acme",
            "visible_text": "Software Engineer Intern\nCompany: Acme\nBuild Python services.",
        },
    ).json()

    result = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert result["status"] == "retry_scheduled"
    assert result["attempts"] == 1
    assert result["retryable"] is True
    assert result["error_code"] == "codex_cli_invalid_json"
    assert result["error_message"] == "Configured posting parser returned an invalid response"
