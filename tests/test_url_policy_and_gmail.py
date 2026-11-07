import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobcockpit import cli
from jobcockpit.api.main import create_app
from jobcockpit.services.workflows import job_posting_snapshot_hash
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    EmailIngestionRun,
    JobAlertEmail,
    JobAlertEmailProcessingState,
    JobAlertLink,
    JobAlertSource,
    JobPosting,
    StoredArtifact,
)
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_links import (
    PostingLinkClassification,
    PostingLinkProcessingResult,
)
from jobcockpit.integrations.gmail_processing import (
    GmailProcessingError,
    preview_gmail_source,
    process_gmail_source,
)
from jobcockpit.schemas.requests import JobIngestRequest
from jobcockpit.storage.artifacts import ArtifactStorageService

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    return TestClient(create_app(settings))


def test_gmail_source_create_rejects_blank_query(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Blank alerts",
            "gmail_query": "   ",
            "sender_allowlist": ["alerts.invalid"],
        },
    )

    assert response.status_code == 422
    assert "gmail_query cannot be blank" in response.text


def test_unresolved_job_alert_links_are_listed_for_review(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_unresolved",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:7d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_unresolved",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_unresolved",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash="message_hash",
            provider_thread_id_hash="thread_hash",
            subject="5 New Internships Found in Canada",
            sender_email_hash="sender_hash",
            sender_domain="mapleradar.com",
            received_at="2026-06-08T12:00:00Z",
            body_content_hash="body_hash",
        )
        link = JobAlertLink(
            job_alert_link_id="link_unresolved",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=abc123",
            resolved_url="https://ca.indeed.com/viewjob?jk=abc123",
            canonical_url="https://ca.indeed.com/viewjob?jk=abc123",
            link_text="Software Engineer Intern",
            raw_text=(
                "Title: Software Engineer Intern\n"
                "Company: CAE\n"
                "Location: Saint-Laurent, Quebec, Canada"
            ),
            surrounding_text="Software Engineer Intern - CAE - Saint-Laurent, QC, CA",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="aggregator_fallback_failed: Apply page request failed",
        )
        db.add_all([source, run, email, link])

    client = TestClient(create_app(settings))
    response = client.get("/api/job-alert-links/unresolved")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0] == {
        "job_alert_link_id": "link_unresolved",
        "job_alert_source_id": "alertsrc_unresolved",
        "source_display_name": "MapleRadar jobs",
        "email_subject": "5 New Internships Found in Canada",
        "received_at": "2026-06-08T12:00:00Z",
        "link_text": "Software Engineer Intern",
        "original_url": "https://ca.indeed.com/viewjob?jk=abc123",
        "resolved_url": "https://ca.indeed.com/viewjob?jk=abc123",
        "canonical_url": "https://ca.indeed.com/viewjob?jk=abc123",
        "classification": "requires_external_resolution",
        "ingestion_status": "needs_external_resolution",
        "error_message": "aggregator_fallback_failed: Apply page request failed",
        "updated_at": payload[0]["updated_at"],
    }


def test_public_job_ingest_reports_language_filter_skip(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )

    def fake_process_posting_link(context, **kwargs):
        assert context.source_url == "http://localhost:8001/jobs/123"
        assert kwargs["language_filter_profile_data"]["language_filter_enabled"] is True
        assert kwargs["skip_language_filter"] is False
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="probable_job_posting",
                confidence=0.9,
                reason="job_terms_in_url_or_text",
            ),
            ingestion_status="skipped_by_language_filter",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message="unmet_required_language: French",
            payload={
                "language_filter": {
                    "required_languages": ["French"],
                    "unmet_languages": ["French"],
                    "reason": "unmet_required_language: French",
                }
            },
        )

    monkeypatch.setattr("jobcockpit.services.workflows.process_posting_link", fake_process_posting_link)
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "source_url": "http://localhost:8001/jobs/123",
            "raw_text": "Software Engineer Intern\nFrench required",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["ingestion_status"] == "skipped_by_language_filter"
    assert detail["error_message"] == "unmet_required_language: French"
    assert detail["language_filter"]["unmet_languages"] == ["French"]


def test_public_job_ingest_filters_pasted_required_language(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )
    client = TestClient(create_app(settings))
    payload = {
        "source_type": "paste",
        "raw_text": "Software Developer Intern\nCompany: Acme\nFrench required for this role.",
    }

    response = client.post("/api/jobs/ingest", json=payload)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["ingestion_status"] == "skipped_by_language_filter"
    assert detail["error_message"] == "unmet_required_language: French"
    assert detail["language_filter"]["unmet_languages"] == ["French"]
    with session_scope(settings) as db:
        assert db.query(JobPosting).count() == 0

    skipped_response = client.post(
        "/api/jobs/ingest", json={**payload, "skip_language_filter": True}
    )

    assert skipped_response.status_code == 201
    with session_scope(settings) as db:
        assert db.query(JobPosting).count() == 1


def test_unresolved_job_alert_link_retry_sanitizes_fallback_summary(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )
        source = JobAlertSource(
            job_alert_source_id="alertsrc_retry_one",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:7d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
            skip_language_filter=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_retry_one",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_retry_one",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash="message_retry_one",
            provider_thread_id_hash="thread_retry_one",
            subject="1 New Internship Found in Canada",
            sender_email_hash="sender_hash",
            sender_domain="mapleradar.com",
            received_at="2026-06-08T12:00:00Z",
            body_content_hash="body_hash",
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_retry_one",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=email.provider_message_id_hash,
            processing_status="needs_review",
            last_ingestion_run_id=run.email_ingestion_run_id,
            linked_job_count=0,
            skipped_link_count=0,
            needs_review_count=1,
        )
        link = JobAlertLink(
            job_alert_link_id="link_retry_one",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=abc123",
            resolved_url="https://ca.indeed.com/viewjob?jk=abc123",
            canonical_url="https://ca.indeed.com/viewjob?jk=abc123",
            link_text="Software Engineer Intern",
            raw_text=(
                "Title: Software Engineer Intern\n"
                "Company: CAE\n"
                "Location: Saint-Laurent, Quebec, Canada"
            ),
            surrounding_text="Software Engineer Intern - CAE - Saint-Laurent, QC, CA",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="codex_playwright_low_confidence",
        )
        db.add_all([run, email, state, link])

    def fake_process_posting_link(context, **kwargs):
        assert kwargs["language_filter_profile_data"]["language_filter_enabled"] is True
        assert kwargs["skip_language_filter"] is True
        assert context.source_url == "https://ca.indeed.com/viewjob?jk=abc123"
        assert "Company: CAE" in context.raw_text
        assert "Saint-Laurent, Quebec, Canada" in context.raw_text
        assert context.surrounding_text == "Software Engineer Intern - CAE - Saint-Laurent, QC, CA"
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="external_job_board_link",
            ),
            ingestion_status="fallback_imported",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            linked_job_id=None,
            created=False,
            error_message="codex_cli_failed: private provider output",
        )

    monkeypatch.setattr(
        "jobcockpit.ingestion.job_alert_link_retry.process_posting_link",
        fake_process_posting_link,
    )

    client = TestClient(create_app(settings))
    response = client.post("/api/job-alert-links/link_retry_one/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ingestion_status"] == "fallback_imported"
    assert payload["canonical_url"] == "https://ca.indeed.com/viewjob?jk=abc123"
    with session_scope(settings) as db:
        link = db.get(JobAlertLink, "link_retry_one")
        state = db.get(JobAlertEmailProcessingState, "emailstate_retry_one")
        assert link.ingestion_status == "fallback_imported"
        assert link.error_message == "official_discovery_failed"
        assert state.processing_status == "processed"
        assert state.linked_job_count == 1
        assert state.needs_review_count == 0


def test_unresolved_job_alert_link_retry_records_api_failure(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_retry_api_failure",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:7d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_retry_api_failure",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_retry_api_failure",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash="message_retry_api_failure",
            provider_thread_id_hash="thread_retry_api_failure",
            subject="1 New Internship Found in Canada",
            sender_email_hash="sender_hash",
            sender_domain="mapleradar.com",
            received_at="2026-06-08T12:00:00Z",
            body_content_hash="body_hash",
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_retry_api_failure",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=email.provider_message_id_hash,
            processing_status="needs_review",
            needs_review_count=1,
        )
        link = JobAlertLink(
            job_alert_link_id="link_retry_api_failure",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=abc123",
            resolved_url="https://ca.indeed.com/viewjob?jk=abc123",
            canonical_url="https://ca.indeed.com/viewjob?jk=abc123",
            link_text="Software Engineer Intern",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="codex_playwright_low_confidence",
        )
        db.add_all([run, email, state, link])

    def fake_process_posting_link(context, **kwargs):
        raise RuntimeError("playwright failed")

    monkeypatch.setattr(
        "jobcockpit.ingestion.job_alert_link_retry.process_posting_link",
        fake_process_posting_link,
    )

    client = TestClient(create_app(settings))
    response = client.post("/api/job-alert-links/link_retry_api_failure/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ingestion_status"] == "needs_external_resolution"
    assert payload["error_message"] == "retry_failed"
    with session_scope(settings) as db:
        link = db.get(JobAlertLink, "link_retry_api_failure")
        assert link.ingestion_status == "needs_external_resolution"
        assert link.error_message == "retry_failed"


def test_cli_retry_unresolved_links_scans_unresolved_rows(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_retry_cli",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:7d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_retry_cli",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_retry_cli",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash="message_retry_cli",
            provider_thread_id_hash="thread_retry_cli",
            subject="1 New Internship Found in Canada",
            sender_email_hash="sender_hash",
            sender_domain="mapleradar.com",
            received_at="2026-06-08T12:00:00Z",
            body_content_hash="body_hash",
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_retry_cli",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=email.provider_message_id_hash,
            processing_status="needs_review",
            needs_review_count=1,
        )
        link = JobAlertLink(
            job_alert_link_id="link_retry_cli",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=abc123",
            resolved_url="https://ca.indeed.com/viewjob?jk=abc123",
            canonical_url="https://ca.indeed.com/viewjob?jk=abc123",
            link_text="Software Engineer Intern",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="codex_playwright_low_confidence",
        )
        db.add_all([run, email, state, link])

    def fake_process_posting_link(context, **kwargs):
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="external_job_board_link",
            ),
            ingestion_status="needs_external_resolution",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message="still blocked",
        )

    monkeypatch.setattr(
        "jobcockpit.ingestion.job_alert_link_retry.process_posting_link",
        fake_process_posting_link,
    )

    exit_code = cli.main(["links", "retry-unresolved", "--limit", "1", "--json"], settings=settings)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["links_retried_count"] == 1
    assert payload["links_imported_count"] == 0
    assert payload["links"][0]["error_message"] == "official_discovery_failed"


def test_cli_retry_unresolved_links_keeps_success_when_later_link_fails(
    tmp_path: Path, monkeypatch, capsys
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
        posting_search_provider="disabled",
    )
    init_database(settings)
    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_retry_cli_partial",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:7d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_retry_cli_partial",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_retry_cli_partial",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash="message_retry_cli_partial",
            provider_thread_id_hash="thread_retry_cli_partial",
            subject="2 New Internships Found in Canada",
            sender_email_hash="sender_hash",
            sender_domain="mapleradar.com",
            received_at="2026-06-08T12:00:00Z",
            body_content_hash="body_hash",
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_retry_cli_partial",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=email.provider_message_id_hash,
            processing_status="needs_review",
            needs_review_count=2,
        )
        first_link = JobAlertLink(
            job_alert_link_id="link_retry_cli_success",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=success",
            resolved_url="https://ca.indeed.com/viewjob?jk=success",
            canonical_url="https://ca.indeed.com/viewjob?jk=success",
            link_text="Software Engineer Intern",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="codex_playwright_low_confidence",
        )
        second_link = JobAlertLink(
            job_alert_link_id="link_retry_cli_failure",
            job_alert_email_id=email.job_alert_email_id,
            original_url="https://ca.indeed.com/viewjob?jk=failure",
            resolved_url="https://ca.indeed.com/viewjob?jk=failure",
            canonical_url="https://ca.indeed.com/viewjob?jk=failure",
            link_text="Backend Intern",
            classification="requires_external_resolution",
            confidence=90,
            ingestion_status="needs_external_resolution",
            error_message="codex_playwright_low_confidence",
        )
        db.add_all([run, email, state, first_link, second_link])

    def fake_process_posting_link(context, **kwargs):
        if "success" not in context.source_url:
            raise RuntimeError("playwright failed")
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="external_job_board_link",
            ),
            ingestion_status="fallback_imported",
            original_url=context.source_url,
            resolved_url="https://careers.example/jobs/success",
            canonical_url="https://careers.example/jobs/success",
            linked_job_id=None,
            created=False,
        )

    monkeypatch.setattr(
        "jobcockpit.ingestion.job_alert_link_retry.process_posting_link",
        fake_process_posting_link,
    )

    exit_code = cli.main(["links", "retry-unresolved", "--json"], settings=settings)

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["links_retried_count"] == 2
    assert payload["links_imported_count"] == 1
    assert payload["links_failed_count"] == 1
    with session_scope(settings) as db:
        first_link = db.get(JobAlertLink, "link_retry_cli_success")
        second_link = db.get(JobAlertLink, "link_retry_cli_failure")
        assert first_link.ingestion_status == "fallback_imported"
        assert first_link.canonical_url == "https://careers.example/jobs/success"
        assert (
            db.get(JobAlertEmailProcessingState, "emailstate_retry_cli_partial").linked_job_count
            == 1
        )
        assert second_link.ingestion_status == "needs_external_resolution"
        assert second_link.error_message == "retry_failed"


def test_job_alert_source_update_edits_query_allowlist_and_enabled_state(tmp_path: Path):
    client = make_client(tmp_path)
    created = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Maple alerts",
            "gmail_query": "from:jobs@mapleradar.com newer_than:30d",
            "sender_allowlist": ["mapleradar.com"],
            "enabled": False,
            "skip_language_filter": True,
        },
    ).json()
    assert created["enabled"] is False
    assert created["skip_language_filter"] is True

    response = client.put(
        f"/api/job-alert-sources/{created['job_alert_source_id']}",
        json={
            "display_name": "LinkedIn alerts",
            "gmail_query": "from:linkedin.com newer_than:7d",
            "sender_allowlist": ["linkedin.com", "alerts.linkedin.com"],
            "enabled": False,
            "skip_language_filter": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_alert_source_id"] == created["job_alert_source_id"]
    assert payload["display_name"] == "LinkedIn alerts"
    assert payload["gmail_query"] == "from:linkedin.com newer_than:7d"
    assert payload["sender_allowlist"] == ["linkedin.com", "alerts.linkedin.com"]
    assert payload["enabled"] is False
    assert payload["skip_language_filter"] is False

    listed = client.get("/api/job-alert-sources").json()
    assert listed == [payload]


def test_job_alert_source_update_rejects_blank_query(tmp_path: Path):
    client = make_client(tmp_path)
    created = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Maple alerts",
            "gmail_query": "from:jobs@mapleradar.com newer_than:30d",
            "sender_allowlist": ["mapleradar.com"],
        },
    ).json()

    response = client.put(
        f"/api/job-alert-sources/{created['job_alert_source_id']}",
        json={
            "display_name": "Broken",
            "gmail_query": "   ",
            "sender_allowlist": ["mapleradar.com"],
            "enabled": True,
        },
    )

    assert response.status_code == 422
    assert "gmail_query cannot be blank" in response.text


def test_job_alert_source_delete_removes_source_history_but_keeps_imported_jobs(tmp_path: Path):
    client = make_client(tmp_path)
    created = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()
    source_id = created["job_alert_source_id"]

    preview = client.post("/api/job-alerts/gmail/preview", json={"source_id": source_id})
    assert preview.status_code == 200
    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source_id,
            "approved_message_review_ids": [preview.json()["messages"][0]["message_review_id"]],
        },
    )
    assert imported.status_code == 200

    response = client.delete(f"/api/job-alert-sources/{source_id}")

    assert response.status_code == 204
    assert client.get("/api/job-alert-sources").json() == []
    with session_scope(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
            artifact_root=tmp_path / "private" / "storage",
            extension_token_secret="test-secret",
            ats_mock_port=8001,
            gmail_provider="synthetic",
        )
    ) as db:
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"
        assert list(db.execute(select(JobAlertSource)).scalars()) == []
        assert list(db.execute(select(JobAlertEmailProcessingState)).scalars()) == []
        assert list(db.execute(select(EmailIngestionRun)).scalars()) == []
        assert list(db.execute(select(JobAlertEmail)).scalars()) == []
        assert list(db.execute(select(JobAlertLink)).scalars()) == []


def test_ats_providers_endpoint_lists_portal_import_providers(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/ats/providers")

    assert response.status_code == 200
    assert "workday" in response.json()


def test_url_source_ingest_rejects_blocked_private_network_url(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "source_url": "http://169.254.169.254/latest/meta-data",
            "raw_text": "Metadata Role\nPython SQL",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "URL is blocked by policy: private_network_blocked"


def test_url_source_ingest_rejects_malformed_port_without_500(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_search_provider="disabled",
    )
    init_database(settings)
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "source_url": "http://localhost:bad/jobs",
            "raw_text": "Malformed Port Role\nPython SQL",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "URL is blocked by policy: invalid_port"


def test_gmail_import_blocks_private_network_links_without_creating_jobs(
    tmp_path: Path, monkeypatch
):
    message = _synthetic_message(
        "synthetic-message-private-link",
        "synthetic-thread-private-link",
        "Suspicious role\nPython SQL",
        hrefs=["http://169.254.169.254/latest/meta-data"],
    )
    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_processing.SYNTHETIC_GMAIL_MESSAGES", [message]
    )
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [message["id_hash"]],
        },
    )

    assert imported.status_code == 200
    assert imported.json()["jobs_created_count"] == 0
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    with session_scope(settings) as db:
        assert list(db.execute(select(JobPosting)).scalars()) == []
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        link_status = link.ingestion_status
        link_error = link.error_message
        linked_job_count = state.linked_job_count
        skipped_link_count = state.skipped_link_count
    assert link_status == "blocked_by_url_policy"
    assert link_error == "private_network_blocked"
    assert linked_job_count == 0
    assert skipped_link_count == 1


def test_gmail_import_ignores_footer_links_without_reviewing_message(tmp_path: Path, monkeypatch):
    message = _synthetic_message(
        "synthetic-message-footer-link",
        "synthetic-thread-footer-link",
        "Manage preferences",
        hrefs=["https://mapleradar.com/preferences/manage?email=user%40example.invalid"],
    )
    message["html"] = (
        '<html><body><a href="https://mapleradar.com/preferences/manage?email=user%40example.invalid">'
        "Manage preferences</a></body></html>"
    )
    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_processing.SYNTHETIC_GMAIL_MESSAGES", [message]
    )
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [message["id_hash"]],
        },
    )

    assert imported.status_code == 200
    assert imported.json()["jobs_created_count"] == 0
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        link_status = link.ingestion_status
        link_classification = link.classification
        state_status = state.processing_status
        skipped_link_count = state.skipped_link_count
        needs_review_count = state.needs_review_count
    assert link_status == "ignored"
    assert link_classification == "unsubscribe_or_preferences"
    assert state_status == "processed"
    assert skipped_link_count == 1
    assert needs_review_count == 0


def test_gmail_import_requires_previewed_processing_state(tmp_path: Path):
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [_message_review_id("synthetic-message-001")],
        },
    )

    assert imported.status_code == 409
    assert imported.json()["detail"] == "Message must be previewed before import"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    with session_scope(settings) as db:
        assert list(db.execute(select(EmailIngestionRun)).scalars()) == []
        assert list(db.execute(select(JobAlertEmail)).scalars()) == []
        assert list(db.execute(select(JobPosting)).scalars()) == []
        ingestion_events = [
            (row.event_type, row.entity_id, row.payload)
            for row in db.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "ingestion_run")
                .order_by(AuditLogEntry.created_at)
            ).scalars()
        ]
    assert [event_type for event_type, _entity_id, _payload in ingestion_events] == [
        "ingestion.run_started",
        "ingestion.run_failed",
    ]
    assert ingestion_events[0][1] == ingestion_events[1][1]
    assert ingestion_events[1][2]["error_code"] == "gmail_processing_error"


def test_gmail_import_rejects_unknown_review_ids_without_creating_run(tmp_path: Path):
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()
    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": ["unknown-review-id"],
        },
    )

    assert imported.status_code == 400
    assert imported.json()["detail"] == "Unknown message review ID"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    with session_scope(settings) as db:
        assert list(db.execute(select(EmailIngestionRun)).scalars()) == []
        assert list(db.execute(select(JobAlertEmail)).scalars()) == []
        assert list(db.execute(select(JobPosting)).scalars()) == []


def test_gmail_import_rejects_empty_review_ids_without_creating_run(tmp_path: Path):
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()
    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={"source_id": source["job_alert_source_id"], "approved_message_review_ids": []},
    )

    assert imported.status_code == 400
    assert imported.json()["detail"] == "approved_message_review_ids cannot be empty"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    with session_scope(settings) as db:
        assert list(db.execute(select(EmailIngestionRun)).scalars()) == []
        assert list(db.execute(select(JobAlertEmail)).scalars()) == []
        assert list(db.execute(select(JobPosting)).scalars()) == []


def test_gmail_preview_exposes_review_ids_without_provider_message_ids(tmp_path: Path):
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )

    assert preview.status_code == 200
    message = preview.json()["messages"][0]
    assert message["message_review_id"] == _message_review_id("synthetic-message-001")
    assert "provider_message_id" not in message
    assert "synthetic-message-001" not in str(preview.json())


def test_gmail_preview_and_import_track_processed_state_locally(tmp_path: Path):
    client = make_client(tmp_path)

    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200
    assert preview.json()["messages"][0]["processing_status"] == "previewed"

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [_message_review_id("synthetic-message-001")],
        },
    )
    assert imported.status_code == 200
    payload = imported.json()
    assert payload["mailbox_mutations"] == []
    assert payload["processed_messages"][0]["processing_status"] == "processed"
    assert payload["jobs_created_count"] == 1

    preview_after_import = client.post(
        "/api/job-alerts/gmail/preview",
        json={"source_id": source["job_alert_source_id"]},
    )
    assert preview_after_import.status_code == 200
    assert preview_after_import.json()["messages"][0]["processing_status"] == "processed"

    repeated = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [_message_review_id("synthetic-message-001")],
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["processed_messages"] == []
    assert repeated.json()["jobs_created_count"] == 0

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    with session_scope(settings) as db:
        link = db.execute(
            select(JobAlertLink).where(JobAlertLink.linked_job_id.is_not(None))
        ).scalar_one()
        assert link.ingestion_status == "imported"
        assert link.linked_job_id is not None


def test_gmail_process_imports_allowed_messages_without_preview(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_auto_process",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
        )

    assert result["jobs_created_count"] == 1
    assert result["processed_messages"][0]["processing_status"] == "processed"
    assert result["mailbox_mutations"] == []
    with session_scope(settings) as db:
        assert db.execute(select(EmailIngestionRun)).scalar_one().messages_imported_count == 1
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"
        assert (
            db.execute(select(JobAlertEmailProcessingState)).scalar_one().processing_status
            == "processed"
        )


def test_gmail_processing_releases_write_lock_before_each_posting(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "jobcockpit.db"
    settings = Settings(
        database_url=f"sqlite:///{database_path}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "transaction-message",
        "transaction-thread",
        "Software Developer Intern\nCompany: Acme",
        hrefs=["https://www.linkedin.com/jobs/view/424242"],
    )
    lock_probes = []

    def probe_unlocked_database(contexts, **_kwargs):
        connection = sqlite3.connect(database_path, timeout=0.1)
        try:
            connection.execute("BEGIN IMMEDIATE")
            lock_probes.append("acquired")
            connection.rollback()
        finally:
            connection.close()
        context = contexts[0]
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="requires_external_resolution",
                    confidence=0.9,
                    reason="external_job_board_link",
                ),
                ingestion_status="needs_external_resolution",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                error_message="posting_resolver_not_configured",
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link",
        lambda context, **kwargs: probe_unlocked_database([context], **kwargs)[0],
    )
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_transaction_boundary",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Synthetic alerts",
                gmail_query="from:alerts.invalid newer_than:7d",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )
    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_transaction_boundary")
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert lock_probes == ["acquired"]
    assert result["messages_needing_review_count"] == 1


def test_gmail_processing_continues_after_one_posting_raises(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "failure-isolation-message",
        "failure-isolation-thread",
        "Software Developer Intern\nCompany: Acme",
        hrefs=[
            "https://www.linkedin.com/jobs/view/111111",
            "https://www.linkedin.com/jobs/view/222222",
        ],
    )
    processed_urls: list[str] = []

    def fail_first_posting(contexts, **_kwargs):
        context = contexts[0]
        processed_urls.append(context.source_url)
        if context.source_url.endswith("111111"):
            raise RuntimeError("synthetic posting failure")
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="requires_external_resolution",
                    confidence=0.9,
                    reason="external_job_board_link",
                ),
                ingestion_status="needs_external_resolution",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                error_message="posting_resolver_not_configured",
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link",
        lambda context, **kwargs: fail_first_posting([context], **kwargs)[0],
    )
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_failure_isolation",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Synthetic alerts",
                gmail_query="from:alerts.invalid newer_than:7d",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )
    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_failure_isolation")
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert sorted(processed_urls) == [
        "https://www.linkedin.com/jobs/view/111111",
        "https://www.linkedin.com/jobs/view/222222",
    ]
    assert result["messages_needing_review_count"] == 1
    assert result["unresolved_links_count"] == 2
    with session_scope(settings) as db:
        emails = list(db.execute(select(JobAlertEmail)).scalars())
        links = list(
            db.execute(select(JobAlertLink).order_by(JobAlertLink.original_url)).scalars()
        )
        assert len(emails) == 1
        assert [link.ingestion_status for link in links] == [
            "ready_for_review",
            "needs_external_resolution",
        ]
        assert links[0].error_message == "posting_processing_failed"


def test_gmail_process_counts_aggregator_fallback_import_as_linked_job(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "fallback-message",
        "fallback-thread",
        "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
        hrefs=["https://www.linkedin.com/jobs/view/4425912315"],
    )

    def fake_process_posting_links(contexts, **kwargs):
        context = contexts[0]
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="requires_external_resolution",
                    confidence=0.9,
                    reason="external_job_board_link",
                ),
                ingestion_status="fallback_imported",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                linked_job_id=None,
                created=True,
                error_message="codex_cli_failed: private provider output",
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link", lambda context, **kwargs: fake_process_posting_links([context], **kwargs)[0]
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_fallback_import",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 1
    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        assert link.ingestion_status == "fallback_imported"
        assert link.error_message == "official_discovery_failed"
        assert "private provider output" not in link.error_message
        assert state.processing_status == "processed"
        assert state.linked_job_count == 1
        assert state.needs_review_count == 0


def test_gmail_process_counts_duplicate_successful_import_as_reused_job(
    tmp_path: Path, monkeypatch
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "duplicate-message",
        "duplicate-thread",
        "Software Engineer Intern\nCompany: Zip\nBuild Python services.",
        hrefs=["https://jobs.example.com/software-engineer-intern"],
    )

    def fake_process_posting_links(contexts, **kwargs):
        context = contexts[0]
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="job_posting",
                    confidence=0.95,
                    reason="posting_link",
                ),
                ingestion_status="imported",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                linked_job_id=None,
                created=False,
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link", lambda context, **kwargs: fake_process_posting_links([context], **kwargs)[0]
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_duplicate_import",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_discovered_count"] == 1
    assert result["jobs_created_count"] == 0
    assert result["jobs_reused_count"] == 1
    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        assert link.ingestion_status == "imported"
        assert state.processing_status == "processed"
        assert state.linked_job_count == 1
        assert state.needs_review_count == 0


def test_gmail_process_imports_plain_text_only_links(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "plain-text-message",
        "plain-text-thread",
        "Plain text role\nPython SQL\nhttp://localhost:8001/ats/generic/acme/jobs/plain-text",
        hrefs=[],
    )
    message["html"] = ""

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_plain_text",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Plain text alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 1
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        assert link.ingestion_status == "imported"
        assert link.resolved_url == "http://localhost:8001/ats/generic/acme/jobs/plain-text"


def test_gmail_process_uses_link_text_when_digest_row_context_is_missing(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    job_url = "http://localhost:8001/ats/generic/acme/jobs/legaltech-intern"
    message = {
        "id": "mapleradar-link-text-message",
        "id_hash": _message_review_id("mapleradar-link-text-message"),
        "thread_hash": hashlib.sha256("mapleradar-link-text-thread".encode("utf-8")).hexdigest(),
        "subject": "Your daily digest of new Canadian internship opportunities.",
        "sender_hash": hashlib.sha256("mapleradar.com".encode("utf-8")).hexdigest(),
        "sender_domain": "mapleradar.com",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(b"mapleradar digest link text").hexdigest(),
        "plain_text": (f"Your daily digest of new Canadian internship opportunities.\n{job_url}\n"),
        "html": (
            f'<html><body><a href="{job_url}">'
            "Software Engineer - AI LegalTech Startup (Also hiring: Intern)"
            "</a></body></html>"
        ),
    }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_link_text_fallback",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:30d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 1
    with session_scope(settings) as db:
        job = db.execute(select(JobPosting)).scalar_one()
        title = job.role["job_title"]
        company = job.company_name
    assert title == "Software Engineer - AI LegalTech Startup (Also hiring: Intern)"
    assert company != "Your"


def test_gmail_process_imports_digest_link_with_row_context(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    junior_sql_url = "http://localhost:8001/ats/generic/acme/jobs/junior-sql-developer"
    ml_intern_url = "http://localhost:8001/ats/generic/acme/jobs/ml-intern"
    message = {
        "id": "mapleradar-digest-message",
        "id_hash": _message_review_id("mapleradar-digest-message"),
        "thread_hash": hashlib.sha256("mapleradar-thread".encode("utf-8")).hexdigest(),
        "subject": "Your daily digest of new Canadian internship opportunities.",
        "sender_hash": hashlib.sha256("mapleradar.com".encode("utf-8")).hexdigest(),
        "sender_domain": "mapleradar.com",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(b"mapleradar digest").hexdigest(),
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "DATA SCIENCE & ANALYTICS\n"
            "Junior SQL Developer \u00b7 Imagine Communications \u00b7 Scarborough, Ontario, Canada\n"
            "Co-op/Intern Developer, Machine Learning \u00b7 Kinaxis \u00b7 Ottawa, Ontario, Canada\n"
            "SWE\n"
            "Software Developer \u2013 Junior (Jamstack Architecture exp) \u00b7 Bevertec \u00b7 Toronto, Ontario, Canada"
        ),
        "html": f"""
        <html><body>
          <p>Your daily digest of new Canadian internship opportunities.</p>
          <h3>DATA SCIENCE &amp; ANALYTICS</h3>
          <p><a href="{junior_sql_url}">Junior SQL Developer</a> \u00b7 Imagine Communications \u00b7 Scarborough, Ontario, Canada</p>
          <p><a href="{ml_intern_url}">Co-op/Intern Developer, Machine Learning</a> \u00b7 Kinaxis \u00b7 Ottawa, Ontario, Canada</p>
        </body></html>
        """,
    }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_mapleradar_digest",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:30d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 2
    with session_scope(settings) as db:
        jobs = [
            {
                "title": job.role["job_title"],
                "company": job.company_name,
                "location": job.location,
            }
            for job in db.execute(select(JobPosting).order_by(JobPosting.source_url)).scalars()
        ]
    assert jobs[0]["title"] == "Junior SQL Developer"
    assert jobs[0]["company"] == "Imagine Communications"
    assert jobs[0]["location"]["city"] == "Scarborough"
    assert jobs[0]["location"]["province_state"] == "Ontario"
    assert jobs[0]["location"]["country"] == "Canada"
    assert jobs[1]["title"] == "Co-op/Intern Developer, Machine Learning"
    assert jobs[1]["company"] == "Kinaxis"
    assert jobs[1]["location"]["city"] == "Ottawa"


def test_generic_gmail_process_does_not_apply_mapleradar_digest_row_parsing(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    job_url = "http://localhost:8001/ats/generic/acme/jobs/generic-digest"
    message = {
        "id": "generic-digest-message",
        "id_hash": _message_review_id("generic-digest-message"),
        "thread_hash": hashlib.sha256("generic-digest-thread".encode("utf-8")).hexdigest(),
        "subject": "Your daily digest of roles",
        "sender_hash": hashlib.sha256("alerts.invalid".encode("utf-8")).hexdigest(),
        "sender_domain": "alerts.invalid",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(b"generic digest").hexdigest(),
        "plain_text": (
            "Your daily digest of roles\n"
            "Junior SQL Developer \u00b7 Imagine Communications \u00b7 Scarborough, Ontario, Canada"
        ),
        "html": (
            f'<html><body><p><a href="{job_url}">Junior SQL Developer</a>'
            " \u00b7 Imagine Communications \u00b7 Scarborough, Ontario, Canada</p></body></html>"
        ),
    }
    imported_raw_text = []

    class FakeImportResult:
        payload = {"id": None}
        created = True

    def capture_import(payload, db, storage, settings):
        imported_raw_text.append(payload.raw_text)
        return FakeImportResult()

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_generic_digest",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Generic digest",
            gmail_query="from:alerts.invalid newer_than:30d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            job_importer=capture_import,
        )

    assert result["jobs_created_count"] == 1
    assert imported_raw_text == [message["plain_text"]]


def test_gmail_process_resolves_linkedin_job_link_through_shared_resolver(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    linkedin_url = "https://www.linkedin.com/jobs/view/424242"
    official_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    message = _synthetic_message(
        "linkedin-message",
        "linkedin-thread",
        "Platform Engineer at Acme\nRemote Canada\nView job on LinkedIn",
        hrefs=[linkedin_url],
    )

    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": (
                        "Platform Engineer\n"
                        "Company: Acme\n"
                        "Apply URL: http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
                    ),
                    "confidence": 0.91,
                    "evidence": "Matched company and title on official careers page",
                },
            }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_linkedin",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="LinkedIn alerts",
            gmail_query="from:linkedin.com newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakePostingResolver(),
        )

    assert result["jobs_created_count"] == 1
    assert result["mailbox_mutations"] == []
    assert len(resolved_hints) == 1
    assert resolved_hints[0].source_kind == "linkedin"
    assert resolved_hints[0].source_url == linkedin_url
    assert resolved_hints[0].company == "Acme"
    assert resolved_hints[0].title == "Platform Engineer"

    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        job = db.execute(select(JobPosting)).scalar_one()
        assert link.original_url == linkedin_url
        assert link.resolved_url == official_url
        assert link.canonical_url == official_url
        assert link.ingestion_status == "imported"
        assert link.linked_job_id == job.id
        assert job.source_url == official_url


def test_mapleradar_external_resolution_preserves_digest_location_hint(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    linkedin_url = "https://www.linkedin.com/jobs/view/4424269238"
    official_url = "http://localhost:8001/ats/generic/rivian/jobs/applications-intern"
    message = {
        "id": "mapleradar-linkedin-location-message",
        "id_hash": _message_review_id("mapleradar-linkedin-location-message"),
        "thread_hash": hashlib.sha256(
            "mapleradar-linkedin-location-thread".encode("utf-8")
        ).hexdigest(),
        "subject": "15 New Internships Found in Canada",
        "sender_hash": hashlib.sha256("mapleradar.com".encode("utf-8")).hexdigest(),
        "sender_domain": "mapleradar.com",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(b"mapleradar linkedin location").hexdigest(),
        "plain_text": (
            "SWE\n"
            "Software Engineering Intern, Applications - Fall/Spring Co-op "
            "\u00b7 Rivian and Volkswagen Group Technologies \u00b7 Toronto, ON, CA"
        ),
        "html": (
            f'<html><body><p><a href="{linkedin_url}">'
            "Software Engineering Intern, Applications - Fall/Spring Co-op</a>"
            " \u00b7 Rivian and Volkswagen Group Technologies \u00b7 Toronto, ON, CA</p></body></html>"
        ),
    }

    class FakePostingResolver:
        def resolve(self, hint):
            assert hint.location == "Toronto, Ontario, Canada"
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": (
                        "Software Engineering Intern, Applications - Fall/Spring Co-op\n"
                        "Company: Rivian and Volkswagen Group Technologies"
                    ),
                    "confidence": 0.91,
                    "evidence": "Official careers page matched title and company",
                },
            }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_mapleradar_linkedin_location",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:30d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakePostingResolver(),
        )

    assert result["jobs_created_count"] == 1
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        job = db.execute(select(JobPosting)).scalar_one()
        assert "Rivian and Volkswagen Group Technologies" in link.raw_text
        assert "Location: Toronto, Ontario, Canada" in link.raw_text
        assert "Toronto, ON, CA" in link.surrounding_text
        assert job.location["city"] == "Toronto"
        assert job.location["province_state"] == "Ontario"
        assert job.location["country"] == "Canada"


def test_gmail_process_keeps_unresolved_external_link_retryable(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_search_provider="disabled",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    linkedin_url = "https://www.linkedin.com/jobs/view/424242"
    official_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    message = _synthetic_message(
        "linkedin-retry-message",
        "linkedin-retry-thread",
        "Platform Engineer at Acme\nRemote Canada\nView job on LinkedIn",
        hrefs=[linkedin_url],
    )
    unresolved_calls = []

    class FakeUnresolvedPostingResolver:
        def resolve(self, hint):
            unresolved_calls.append(hint)
            return {
                "status": "needs_review",
                "error": "codex_cli_failed: private provider output",
                "candidates": [
                    {
                        "source_url": official_url,
                        "raw_text": "private rendered page content",
                        "confidence": 0.72,
                        "evidence": "private candidate evidence",
                    }
                ],
            }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_linkedin_retry",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="LinkedIn alerts",
            gmail_query="from:linkedin.com newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        first_result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakeUnresolvedPostingResolver(),
        )

    assert unresolved_calls
    assert first_result["jobs_created_count"] == 0
    assert first_result["jobs_discovered_count"] == 0
    assert first_result["messages_needing_review_count"] == 1
    assert first_result["unresolved_links_count"] == 1
    assert first_result["processed_messages"][0]["processing_status"] == "needs_review"
    with session_scope(settings) as db:
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        link = db.execute(select(JobAlertLink)).scalar_one()
        assert state.processing_status == "needs_review"
        assert state.needs_review_count == 1
        assert link.ingestion_status == "needs_external_resolution"
        assert link.error_message == "codex_cli_failed"
        assert "private provider output" not in link.error_message
        assert "private candidate evidence" not in link.error_message

    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_linkedin_retry")
        repeated_result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakeUnresolvedPostingResolver(),
        )

    assert repeated_result["jobs_created_count"] == 0
    assert repeated_result["jobs_discovered_count"] == 0
    assert repeated_result["messages_needing_review_count"] == 1
    assert repeated_result["unresolved_links_count"] == 1
    assert repeated_result["processed_messages"][0]["processing_status"] == "needs_review"
    with session_scope(settings) as db:
        assert len(list(db.execute(select(JobAlertEmail)).scalars())) == 1
        assert len(list(db.execute(select(JobAlertLink)).scalars())) == 1

    class FakePostingResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": (
                        "Platform Engineer\n"
                        "Company: Acme\n"
                        "Apply URL: http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
                    ),
                    "confidence": 0.91,
                    "evidence": "Matched company and title on official careers page",
                },
            }

    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_linkedin_retry")
        second_result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakePostingResolver(),
        )

    assert second_result["jobs_created_count"] == 1
    assert second_result["jobs_discovered_count"] == 1
    assert second_result["messages_needing_review_count"] == 0
    assert second_result["unresolved_links_count"] == 0
    assert second_result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        all_links = list(db.execute(select(JobAlertLink)).scalars())
        imported_links = [link for link in all_links if link.ingestion_status == "imported"]
        assert state.processing_status == "processed"
        assert state.needs_review_count == 0
        assert len(all_links) == 1
        assert len(imported_links) == 1
        assert imported_links[0].resolved_url == official_url


def test_gmail_process_reuses_legacy_tracking_link_row_on_retry(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_search_provider="disabled",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    canonical_url = "https://www.linkedin.com/jobs/view/4424269238"
    tracking_url = (
        "https://xgvsd7lt.r.us-east-1.awstrack.me/L0/"
        "https:%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4424269238/1/0100019e940080d4/token"
    )
    official_url = "http://localhost:8001/ats/generic/roche/jobs/ml-intern"
    title = "2026 Intern - Data Scientist / Machine Learning Engineer"
    message = {
        "id": "mapleradar-legacy-tracking-message",
        "id_hash": _message_review_id("mapleradar-legacy-tracking-message"),
        "thread_hash": hashlib.sha256(
            "mapleradar-legacy-tracking-thread".encode("utf-8")
        ).hexdigest(),
        "subject": "15 New Internships Found in Canada",
        "sender_hash": hashlib.sha256("mapleradar.com".encode("utf-8")).hexdigest(),
        "sender_domain": "mapleradar.com",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(b"mapleradar legacy tracking").hexdigest(),
        "plain_text": f"SWE\n{title} \u00b7 Roche \u00b7 Mississauga, ON, CA",
        "html": (
            f'<html><body><p><a href="{tracking_url}">{title}</a>'
            " \u00b7 Roche \u00b7 Mississauga, ON, CA</p></body></html>"
        ),
    }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_mapleradar_legacy_tracking",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="MapleRadar jobs",
            gmail_query="from:jobs@mapleradar.com newer_than:30d",
            sender_allowlist=["mapleradar.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        db.add(
            EmailIngestionRun(
                email_ingestion_run_id="emailrun_legacy_tracking",
                job_alert_source_id=source.job_alert_source_id,
                provider="gmail",
                query_used=source.gmail_query,
                status="completed",
                messages_imported_count=1,
                jobs_created_count=0,
            )
        )
        db.flush()
        db.add(
            JobAlertEmail(
                job_alert_email_id="email_legacy_tracking",
                email_ingestion_run_id="emailrun_legacy_tracking",
                provider_message_id_hash=message["id_hash"],
                provider_thread_id_hash=message["thread_hash"],
                subject=message["subject"],
                sender_email_hash=message["sender_hash"],
                sender_domain=message["sender_domain"],
                received_at=message["received_at"],
                body_content_hash=message["body_hash"],
            )
        )
        db.flush()
        db.add(
            JobAlertEmailProcessingState(
                processing_state_id="emailstate_legacy_tracking",
                job_alert_source_id=source.job_alert_source_id,
                provider_message_id_hash=message["id_hash"],
                processing_status="needs_review",
                last_ingestion_run_id="emailrun_legacy_tracking",
                linked_job_count=0,
                skipped_link_count=0,
                needs_review_count=1,
            )
        )
        db.add(
            JobAlertLink(
                job_alert_link_id="link_legacy_tracking",
                job_alert_email_id="email_legacy_tracking",
                original_url=tracking_url,
                resolved_url=tracking_url,
                canonical_url=tracking_url,
                link_text=title,
                classification="requires_external_resolution",
                confidence=90,
                ingestion_status="needs_external_resolution",
                error_message="posting_resolver_not_configured",
            )
        )

    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": (
                        f"{title}\nCompany: Roche\nLocation: Mississauga, Ontario, Canada"
                    ),
                    "confidence": 0.91,
                    "evidence": "Official careers page matched title and company",
                },
            }

    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_mapleradar_legacy_tracking")
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakePostingResolver(),
        )

    assert result["jobs_created_count"] == 1
    assert resolved_hints[0].source_url == canonical_url
    with session_scope(settings) as db:
        links = list(db.execute(select(JobAlertLink)).scalars())
        assert len(links) == 1
        assert links[0].job_alert_link_id == "link_legacy_tracking"
        assert links[0].original_url == tracking_url
        assert links[0].resolved_url == official_url
        assert links[0].canonical_url == official_url
        assert links[0].ingestion_status == "imported"


def test_gmail_process_scans_past_processed_first_page(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_max_results=1,
        gmail_scan_max_results=2,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    processed_message = _synthetic_message(
        "processed-message",
        "processed-thread",
        "Already processed role\nPython SQL",
    )
    next_page_message = _synthetic_message(
        "next-page-message",
        "next-page-thread",
        "Next page role\nPython SQL",
    )

    def fake_fetch_messages(self, *, source, settings, max_results=None):
        assert max_results == 2
        return [processed_message, next_page_message]

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        fake_fetch_messages,
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_google_scan",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Google alerts",
            gmail_query="from:alerts.invalid newer_than:30d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        db.add(
            JobAlertEmailProcessingState(
                processing_state_id="emailstate_processed",
                job_alert_source_id=source.job_alert_source_id,
                provider_message_id_hash=processed_message["id_hash"],
                processing_status="processed",
            )
        )
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
        )

    assert result["jobs_created_count"] == 1
    assert [message["message_review_id"] for message in result["processed_messages"]] == [
        next_page_message["id_hash"]
    ]
    with session_scope(settings) as db:
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"


def test_gmail_import_scans_past_newer_messages_after_preview(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_max_results=1,
        gmail_scan_max_results=2,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    reviewed_message = _synthetic_message(
        "reviewed-message",
        "reviewed-thread",
        "Reviewed role\nPython SQL",
    )
    newer_message = _synthetic_message(
        "newer-message",
        "newer-thread",
        "Newer role\nPython SQL",
    )
    calls = []

    def fake_fetch_messages(self, *, source, settings, max_results=None):
        calls.append(max_results)
        if len(calls) == 1:
            return [reviewed_message]
        if max_results == 2:
            return [newer_message, reviewed_message]
        return [newer_message]

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        fake_fetch_messages,
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_google_import_scan",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Google alerts",
            gmail_query="from:alerts.invalid newer_than:30d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        preview = preview_gmail_source(db=db, source=source, settings=settings)
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            approved_message_review_ids=[reviewed_message["id_hash"]],
            require_preview=True,
        )

    assert [message["message_review_id"] for message in preview["messages"]] == [
        reviewed_message["id_hash"]
    ]
    assert calls == [None, 2]
    assert result["jobs_created_count"] == 1
    assert [message["message_review_id"] for message in result["processed_messages"]] == [
        reviewed_message["id_hash"]
    ]


def test_cli_gmail_process_all_enabled_sources_is_schedulable(tmp_path: Path, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_cli_process",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Synthetic alerts",
                gmail_query="from:alerts.invalid newer_than:7d",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )

    exit_code = cli.main(["gmail", "process", "--json"], settings=settings)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "gmail"
    assert payload["status"] == "completed"
    assert payload["totals"]["jobs_created_count"] == 1
    assert len(payload["runs"]) == 1
    assert len(payload["runs"][0]["items"]) == 2
    assert payload["totals"]["items_skipped_count"] == 1
    assert payload["runs"][0]["metadata"]["mailbox_mutations"] == []

    with session_scope(settings) as db:
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"
        ingestion_events = [
            (row.event_type, row.entity_id, row.payload)
            for row in db.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "ingestion_run")
                .order_by(AuditLogEntry.created_at)
            ).scalars()
        ]
    assert [event_type for event_type, _entity_id, _payload in ingestion_events] == [
        "ingestion.run_started",
        "ingestion.run_completed",
    ]
    assert ingestion_events[0][1] == ingestion_events[1][1]
    assert ingestion_events[1][2]["counts"]["jobs_created_count"] == 1
    assert ingestion_events[1][2]["counts"]["items_skipped_count"] == 1


def test_cli_gmail_process_commits_successful_source_when_later_source_fails(
    tmp_path: Path, monkeypatch, capsys
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add_all(
            [
                JobAlertSource(
                    job_alert_source_id="alertsrc_1_success",
                    candidate_id="candidate_local",
                    provider="gmail",
                    display_name="Synthetic alerts",
                    gmail_query="from:alerts.invalid newer_than:7d",
                    sender_allowlist=["alerts.invalid"],
                    enabled=True,
                ),
                JobAlertSource(
                    job_alert_source_id="alertsrc_2_fail",
                    candidate_id="candidate_local",
                    provider="gmail",
                    display_name="Broken alerts",
                    gmail_query="from:broken.invalid newer_than:7d",
                    sender_allowlist=["alerts.invalid"],
                    enabled=True,
                ),
            ]
        )
    real_process_gmail_source = cli.process_gmail_source

    def fail_second_source(**kwargs):
        if kwargs["source"].job_alert_source_id == "alertsrc_2_fail":
            raise GmailProcessingError(400, "bad source")
        return real_process_gmail_source(**kwargs)

    monkeypatch.setattr(cli, "process_gmail_source", fail_second_source)

    exit_code = cli.main(["gmail", "process", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "alertsrc_2_fail: bad source" in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "completed_with_warnings"
    assert payload["totals"]["jobs_created_count"] == 1
    assert [run["status"] for run in payload["runs"]] == ["completed", "failed"]
    assert payload["runs"][1]["source_id"] == "alertsrc_2_fail"
    assert payload["runs"][1]["alerts"] == ["bad source"]

    with session_scope(settings) as db:
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"
        states = [
            (state.job_alert_source_id, state.processing_status)
            for state in db.execute(select(JobAlertEmailProcessingState)).scalars()
        ]
    assert states == [("alertsrc_1_success", "processed")]


def test_cli_gmail_process_emits_json_when_all_sources_fail(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_all_fail",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Broken alerts",
                gmail_query="from:broken.invalid newer_than:7d",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )

    def fail_source(**kwargs):
        raise GmailProcessingError(502, "provider unavailable")

    monkeypatch.setattr(cli, "process_gmail_source", fail_source)

    exit_code = cli.main(["gmail", "process", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "alertsrc_all_fail: provider unavailable" in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["totals"]["jobs_created_count"] == 0
    assert payload["totals"]["items_failed_count"] == 0
    assert payload["totals"]["runs_failed_count"] == 1
    assert payload["runs"][0]["source_id"] == "alertsrc_all_fail"
    assert payload["runs"][0]["alerts"] == ["provider unavailable"]


def test_gmail_import_stores_linked_job_count_per_message(tmp_path: Path, monkeypatch):
    messages = [
        _synthetic_message(
            "synthetic-message-001",
            "synthetic-thread-001",
            "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada",
        ),
        _synthetic_message(
            "synthetic-message-002",
            "synthetic-thread-002",
            "Beta Platform Engineer\nPython TypeScript SQL\nRemote Canada",
        ),
    ]
    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_processing.SYNTHETIC_GMAIL_MESSAGES", messages
    )
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [
                _message_review_id("synthetic-message-001"),
                _message_review_id("synthetic-message-002"),
            ],
        },
    )

    assert imported.status_code == 200
    assert imported.json()["jobs_created_count"] == 2
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    from jobcockpit.db.session import session_scope

    with session_scope(settings) as db:
        linked_job_counts = [
            state.linked_job_count
            for state in db.execute(select(JobAlertEmailProcessingState)).scalars()
        ]
    assert sorted(linked_job_counts) == [1, 1]


def test_job_ingest_duplicate_race_preserves_surrounding_email_import_transaction(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    app = create_app(settings)
    payload = {
        "source_type": "gmail_alert",
        "raw_text": "Backend Developer\nPython APIs",
        "source_url": "http://localhost:8001/ats/generic/acme/jobs/race",
    }
    request_model = JobIngestRequest(**payload)
    snapshot_hash = job_posting_snapshot_hash(request_model)
    parsed = parse_job_text(request_model.raw_text, request_model.source_url)
    original_write_bytes = app.state.storage.write_bytes
    competing_job_id = "job_gmail_competing_race"
    competing_inserted = False

    def write_and_insert_competing_job(**kwargs):
        nonlocal competing_inserted
        artifact = original_write_bytes(**kwargs)
        if not competing_inserted:
            competing_inserted = True
            competing_artifact = original_write_bytes(
                artifact_type="job_snapshot",
                content=b"competing gmail race snapshot",
                original_file_name="job-posting.txt",
                content_type="text/plain",
                candidate_id="candidate_local",
                privacy_class="private",
            )
            with session_scope(settings) as competing_db:
                competing_db.add(StoredArtifact.from_record(competing_artifact))
                competing_db.flush()
                competing_db.add(
                    JobPosting(
                        id=competing_job_id,
                        source_type=request_model.source_type,
                        source_url=request_model.source_url,
                        posting_snapshot_hash=snapshot_hash,
                        raw_content_artifact_id=competing_artifact.artifact_id,
                        company_name=parsed.employer.company_name,
                        normalized_title=parsed.role.normalized_title,
                        role_type=parsed.role.role_type,
                        application_deadline=parsed.application.application_deadline,
                        source=parsed.source.model_dump(),
                        employer=parsed.employer.model_dump(),
                        role=parsed.role.model_dump(),
                        employment_timing=parsed.employment_timing.model_dump(),
                        location=parsed.location.model_dump(),
                        eligibility=parsed.eligibility.model_dump(),
                        compensation=parsed.compensation.model_dump(),
                        application=parsed.application.model_dump(),
                        fit_analysis=parsed.fit_analysis.model_dump(),
                        data_quality=parsed.data_quality.model_dump(),
                    )
                )
        return artifact

    app.state.storage.write_bytes = write_and_insert_competing_job
    ingest_endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/jobs/ingest"
    )

    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_race",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Race source",
                gmail_query="from:alerts.invalid",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )

    with session_scope(settings) as db:
        db.add(
            EmailIngestionRun(
                email_ingestion_run_id="emailrun_race",
                job_alert_source_id="alertsrc_race",
                provider="gmail",
                query_used="from:alerts.invalid",
                status="running",
            )
        )
        db.add(
            JobAlertEmail(
                job_alert_email_id="email_race",
                email_ingestion_run_id="emailrun_race",
                provider_message_id_hash="message_hash_race",
                provider_thread_id_hash="race-thread-hash",
                subject="Race alert",
                sender_email_hash="sender_hash_race",
                sender_domain="alerts.invalid",
                received_at="2026-06-02T12:00:00Z",
                body_content_hash="body_hash_race",
            )
        )
        db.add(
            JobAlertLink(
                job_alert_link_id="link_race",
                job_alert_email_id="email_race",
                original_url=request_model.source_url,
                resolved_url=request_model.source_url,
                canonical_url=request_model.source_url,
                link_text="Software Job",
                classification="probable_job_posting",
                confidence=90,
            )
        )

        result = ingest_endpoint(request_model, db, app.state.storage)

    assert result["id"] == competing_job_id
    with session_scope(settings) as db:
        assert db.get(EmailIngestionRun, "emailrun_race") is not None
        assert db.get(JobAlertEmail, "email_race") is not None
        assert db.get(JobAlertLink, "link_race") is not None


def test_gmail_import_creates_distinct_jobs_for_distinct_links_in_one_message(
    tmp_path: Path, monkeypatch
):
    message = _synthetic_message(
        "synthetic-message-multi-link",
        "synthetic-thread-multi-link",
        "Fresh roles matching Python\nPython SQL FastAPI\nRemote Canada",
        hrefs=[
            "http://localhost:8001/ats/generic/acme/jobs/data-engineer",
            "http://localhost:8001/ats/generic/acme/jobs/platform-engineer",
        ],
    )
    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_processing.SYNTHETIC_GMAIL_MESSAGES", [message]
    )
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [message["id_hash"]],
        },
    )

    assert imported.status_code == 200
    assert imported.json()["jobs_created_count"] == 2
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    with session_scope(settings) as db:
        jobs = list(db.execute(select(JobPosting)).scalars())
        source_urls = {job.source_url for job in jobs}
        snapshot_hashes = {job.posting_snapshot_hash for job in jobs}
    assert source_urls == {
        "http://localhost:8001/ats/generic/acme/jobs/data-engineer",
        "http://localhost:8001/ats/generic/acme/jobs/platform-engineer",
    }
    assert len(snapshot_hashes) == 2


def test_gmail_preview_and_import_honor_sender_allowlist(tmp_path: Path, monkeypatch):
    messages = [
        _synthetic_message(
            "synthetic-message-allowed",
            "synthetic-thread-allowed",
            "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada",
            sender_domain="alerts.invalid",
        ),
        _synthetic_message(
            "synthetic-message-blocked",
            "synthetic-thread-blocked",
            "Beta Platform Engineer\nPython TypeScript SQL\nRemote Canada",
            sender_domain="spam.invalid",
        ),
    ]
    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_processing.SYNTHETIC_GMAIL_MESSAGES", messages
    )
    client = make_client(tmp_path)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()

    preview = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )
    assert preview.status_code == 200
    assert [message["message_review_id"] for message in preview.json()["messages"]] == [
        _message_review_id("synthetic-message-allowed")
    ]
    assert "synthetic-message-allowed" not in str(preview.json())

    imported = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [_message_review_id("synthetic-message-allowed")],
        },
    )

    assert imported.status_code == 200
    assert imported.json()["jobs_created_count"] == 1
    assert [message["message_review_id"] for message in imported.json()["processed_messages"]] == [
        _message_review_id("synthetic-message-allowed")
    ]
    assert "synthetic-message-allowed" not in str(imported.json())


def _synthetic_message(
    message_id: str,
    thread_id: str,
    plain_text: str,
    sender_domain: str = "alerts.invalid",
    hrefs: list[str] | None = None,
) -> dict:
    hrefs = hrefs or ["http://localhost:8001/ats/generic/acme/jobs/123"]
    links = "\n".join(f'<a href="{href}">Software Job</a>' for href in hrefs)
    return {
        "id": message_id,
        "id_hash": _message_review_id(message_id),
        "thread_hash": hashlib.sha256(thread_id.encode("utf-8")).hexdigest(),
        "subject": "New jobs matching Python",
        "sender_hash": hashlib.sha256(sender_domain.encode("utf-8")).hexdigest(),
        "sender_domain": sender_domain,
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": hashlib.sha256(plain_text.encode("utf-8")).hexdigest(),
        "plain_text": plain_text,
        "html": f"<html><body>{links}</body></html>",
    }


def _message_review_id(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def test_gmail_retry_preserves_imported_links_before_interest_filter(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    href = "https://jobs.example.com/accounting-intern"
    message = _synthetic_message(
        "interest-retry-imported-message",
        "interest-retry-imported-thread",
        "Accounting Intern\nCompany: LedgerCo\nExcel reconciliation",
        hrefs=[href],
    )

    class FailingResolver:
        def resolve(self, hint):
            raise AssertionError("already imported links should not be reprocessed")

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                },
            )
        )
        source = JobAlertSource(
            job_alert_source_id="alertsrc_interest_retry_imported",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_interest_retry_imported",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_interest_retry_imported",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash=message["id_hash"],
            provider_thread_id_hash=message["thread_hash"],
            subject=message["subject"],
            sender_email_hash=message["sender_hash"],
            sender_domain=message["sender_domain"],
            received_at=message["received_at"],
            body_content_hash=message["body_hash"],
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_interest_retry_imported",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=message["id_hash"],
            processing_status="needs_review",
            last_ingestion_run_id=run.email_ingestion_run_id,
            linked_job_count=1,
            skipped_link_count=0,
            needs_review_count=1,
        )
        artifact = storage.write_bytes(
            artifact_type="job_snapshot",
            content=b"Accounting Intern snapshot",
            original_file_name="job-posting.txt",
            content_type="text/plain",
            candidate_id="candidate_local",
            privacy_class="private",
        )
        job = JobPosting(
            id="job_interest_retry_imported",
            source_type="gmail_alert",
            source_url=href,
            posting_snapshot_hash="interest_retry_imported_hash",
            raw_content_artifact_id=artifact.artifact_id,
            company_name="LedgerCo",
            normalized_title="Accounting Intern",
            role_type="internship",
        )
        link = JobAlertLink(
            job_alert_link_id="link_interest_retry_imported",
            job_alert_email_id=email.job_alert_email_id,
            original_url=href,
            resolved_url=href,
            canonical_url=href,
            link_text="Accounting Intern",
            raw_text="Title: Accounting Intern\nCompany: LedgerCo",
            surrounding_text="Accounting Intern - LedgerCo",
            classification="direct_posting",
            confidence=95,
            ingestion_status="imported",
            linked_job_id=job.id,
            error_message=None,
        )
        db.add(StoredArtifact.from_record(artifact))
        db.flush()
        db.add_all([run, email, state, job])
        db.flush()
        db.add(link)

    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_interest_retry_imported")
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FailingResolver(),
        )

    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.get(JobAlertLink, "link_interest_retry_imported")
        state = db.get(JobAlertEmailProcessingState, "emailstate_interest_retry_imported")
        assert link.ingestion_status == "imported"
        assert link.linked_job_id == "job_interest_retry_imported"
        assert link.error_message is None
        assert state.linked_job_count == 1
        assert state.skipped_link_count == 0
        assert state.needs_review_count == 0


def test_gmail_process_counts_language_filter_skips_as_processed(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "language-skip-message",
        "language-skip-thread",
        "Software Developer Intern\nCompany: Acme\nFrench required for this role.",
        hrefs=["https://jobs.example.com/software-developer-intern"],
    )
    received_kwargs = {}

    def fake_process_posting_links(contexts, **kwargs):
        received_kwargs.update(kwargs)
        context = contexts[0]
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="probable_job_posting",
                    confidence=0.9,
                    reason="job_terms_in_url_or_text",
                ),
                ingestion_status="skipped_by_language_filter",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                error_message="unmet_required_language: French",
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link", lambda context, **kwargs: fake_process_posting_links([context], **kwargs)[0]
    )

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )
        source = JobAlertSource(
            job_alert_source_id="alertsrc_language_skip",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
            skip_language_filter=False,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 0
    assert result["processed_messages"][0]["processing_status"] == "processed"
    assert received_kwargs["language_filter_profile_data"]["language_filter_enabled"] is True
    assert received_kwargs["skip_language_filter"] is False
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        assert link.ingestion_status == "skipped_by_language_filter"
        assert link.error_message == "unmet_required_language: French"
        assert state.skipped_link_count == 1
        assert state.linked_job_count == 0
        assert state.needs_review_count == 0
        assert db.execute(select(JobPosting)).all() == []


def test_gmail_process_counts_terminal_status_as_processed(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    href = "https://www.linkedin.com/jobs/view/1234567890"
    message = _synthetic_message(
        "terminal-status-message",
        "terminal-status-thread",
        "Software Engineer\nCompany: ExampleCo",
        hrefs=[href],
    )

    def fake_process_posting_links(contexts, **kwargs):
        context = contexts[0]
        return [
            PostingLinkProcessingResult(
                classification=PostingLinkClassification(
                    classification="requires_external_resolution",
                    confidence=0.99,
                    reason="linkedin_job_url",
                ),
                ingestion_status="terminal_status_detected",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                detected_status="closed",
                status_evidence_code="linkedin_job_removed_or_invalid",
            )
        ]

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link", lambda context, **kwargs: fake_process_posting_links([context], **kwargs)[0]
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_terminal_status",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 0
    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        assert link.ingestion_status == "terminal_status_detected"
        assert link.error_message == "linkedin_job_removed_or_invalid"
        assert state.skipped_link_count == 1
        assert state.linked_job_count == 0
        assert state.needs_review_count == 0
        assert db.execute(select(JobPosting)).all() == []


def test_gmail_retry_does_not_reprocess_terminal_status_link(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    href = "https://www.linkedin.com/jobs/view/1234567890"
    message = _synthetic_message(
        "terminal-status-retry-message",
        "terminal-status-retry-thread",
        "Software Engineer\nCompany: ExampleCo",
        hrefs=[href],
    )

    def fail_if_reprocessed(*args, **kwargs):
        raise AssertionError("terminal-status links should not be reprocessed")

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link", lambda context, **kwargs: fail_if_reprocessed([context], **kwargs)[0]
    )

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_terminal_status_retry",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        run = EmailIngestionRun(
            email_ingestion_run_id="emailrun_terminal_status_retry",
            job_alert_source_id=source.job_alert_source_id,
            provider="gmail",
            query_used=source.gmail_query,
            status="completed",
        )
        email = JobAlertEmail(
            job_alert_email_id="email_terminal_status_retry",
            email_ingestion_run_id=run.email_ingestion_run_id,
            provider="gmail",
            provider_message_id_hash=message["id_hash"],
            provider_thread_id_hash=message["thread_hash"],
            subject=message["subject"],
            sender_email_hash=message["sender_hash"],
            sender_domain=message["sender_domain"],
            received_at=message["received_at"],
            body_content_hash=message["body_hash"],
        )
        state = JobAlertEmailProcessingState(
            processing_state_id="emailstate_terminal_status_retry",
            job_alert_source_id=source.job_alert_source_id,
            provider_message_id_hash=message["id_hash"],
            processing_status="needs_review",
            last_ingestion_run_id=run.email_ingestion_run_id,
            linked_job_count=0,
            skipped_link_count=0,
            needs_review_count=1,
        )
        link = JobAlertLink(
            job_alert_link_id="link_terminal_status_retry",
            job_alert_email_id=email.job_alert_email_id,
            original_url=href,
            resolved_url=href,
            canonical_url=href,
            link_text="Software Job",
            raw_text="Software Engineer\nCompany: ExampleCo",
            surrounding_text="",
            classification="requires_external_resolution",
            confidence=99,
            ingestion_status="terminal_status_detected",
            linked_job_id=None,
            error_message="linkedin_job_removed_or_invalid",
        )
        db.add(source)
        db.flush()
        db.add(run)
        db.flush()
        db.add(email)
        db.flush()
        db.add_all([state, link])

    with session_scope(settings) as db:
        source = db.get(JobAlertSource, "alertsrc_terminal_status_retry")
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=object(),
        )

    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.get(JobAlertLink, "link_terminal_status_retry")
        state = db.get(JobAlertEmailProcessingState, "emailstate_terminal_status_retry")
        assert link.ingestion_status == "terminal_status_detected"
        assert link.error_message == "linkedin_job_removed_or_invalid"
        assert state.skipped_link_count == 1
        assert state.linked_job_count == 0
        assert state.needs_review_count == 0


def test_gmail_process_skips_links_by_profile_role_interest(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "interest-skip-message",
        "interest-skip-thread",
        "Accounting Intern\nCompany: LedgerCo\nExcel reconciliation",
        hrefs=["https://jobs.example.com/accounting-intern"],
    )

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                },
            )
        )
        source = JobAlertSource(
            job_alert_source_id="alertsrc_interest_skip",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 0
    assert result["processed_messages"][0]["processing_status"] == "processed"
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        state = db.execute(select(JobAlertEmailProcessingState)).scalar_one()
        assert link.ingestion_status == "skipped_by_interest_filter"
        assert link.error_message == "excluded_keyword"
        assert state.skipped_link_count == 1
        assert state.linked_job_count == 0
        assert db.execute(select(JobPosting)).all() == []


def test_gmail_process_does_not_treat_student_as_a_target_role(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    message = _synthetic_message(
        "modifier-skip-message",
        "modifier-skip-thread",
        "Engineering Student\nCompany: PowerCo\nSubstation design",
        hrefs=["https://jobs.example.com/engineering-student"],
    )

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "target_role_include_keywords": ["software", "student"],
                    "target_role_exclude_keywords": [],
                },
            )
        )
        source = JobAlertSource(
            job_alert_source_id="alertsrc_modifier_skip",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Synthetic alerts",
            gmail_query="from:alerts.invalid newer_than:7d",
            sender_allowlist=["alerts.invalid"],
            enabled=True,
        )
        db.add(source)
        db.flush()

        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
        )

    assert result["jobs_created_count"] == 0
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        assert link.ingestion_status == "skipped_by_interest_filter"
        assert link.error_message == "missing_include_keyword"
        assert db.execute(select(JobPosting)).all() == []
