
from conftest import finish_generation
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from jobcockpit.services import workflows as api_main
from jobcockpit.api.job_merge import JobMergePlan, apply_authoritative_job_merge
from jobcockpit.api.main import create_app
from jobcockpit.services.workflows import job_posting_snapshot_hash
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    ApplicationPackage,
    ApplicationPackageDocument,
    AuditLogEntry,
    CandidateProfile,
    CoverLetterGenerationRun,
    EmailIngestionRun,
    EvidenceItem,
    GeneratedDocument,
    JobAlertEmail,
    JobAlertLink,
    JobAlertSource,
    JobReprocessRun,
    JobPosting,
    JobStatusCheck,
    JobSourceIdentity,
    JobStatusScanRun,
    StoredArtifact,
)
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsJob, SourceArchive
from jobcockpit.ingestion.parser import ParsedFit, parse_job_text
from jobcockpit.ingestion.posting_links import (
    PostingLinkClassification,
    PostingLinkProcessingResult,
)
from jobcockpit.ingestion.posting_resolution import PostingResolution, ResolvedExternalPosting
from jobcockpit.schemas.requests import JobIngestRequest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class AsyncCaptureTestClient(TestClient):
    """Expose explicit helpers for tests that need a completed queue run."""

    def process_capture(self, payload: dict) -> dict:
        queued_response = super().post(
            "/api/extension/page-captures",
            json=payload,
        )
        assert queued_response.status_code == 202
        queued = queued_response.json()
        processed = api_main.process_page_capture_run(
            queued["capture_id"], self.app.state.settings, self.app.state.storage
        )
        assert processed is not None
        assert processed["status"] not in {"queued", "running", "retry_scheduled"}
        return processed

    def capture_job(self, payload: dict):
        processed = self.process_capture(payload)
        assert processed.get("job_id")
        completed = super().get(f"/api/jobs/{processed['job_id']}")
        assert completed.status_code == 200
        return completed


def make_client(tmp_path: Path, **settings_overrides) -> TestClient:
    settings_overrides = {
        "posting_resolver_provider": "disabled",
        "posting_search_provider": "disabled",
        "posting_rendered_fallback_provider": "disabled",
        "posting_parser_provider": "heuristic",
        "fit_analysis_provider": "disabled",
        **settings_overrides,
    }
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        **settings_overrides,
    )
    init_database(settings)
    return AsyncCaptureTestClient(create_app(settings))


def test_job_ingest_exposes_denormalized_duration_months(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Software Developer Co-Op - Fall 2026\nCompany: TrendAI\nPython Docker",
            "source_url": None,
        },
    )

    assert response.status_code == 201
    job = response.json()
    assert job["employment_timing"]["duration_months"] == 4
    assert job["duration_months"] == 4


def test_job_opportunity_status_defaults_active_and_can_be_overridden(tmp_path: Path):
    client = make_client(tmp_path)
    created_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            "source_url": None,
        },
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["status"] == "active"
    assert [job["id"] for job in client.get("/api/jobs?status=active").json()] == [created["id"]]

    updated_response = client.patch(
        f"/api/jobs/{created['id']}/status",
        json={"status": "expired"},
    )

    assert updated_response.status_code == 200
    assert updated_response.json()["status"] == "expired"
    assert client.get(f"/api/jobs/{created['id']}").json()["status"] == "expired"
    assert client.get("/api/jobs?status=active").json() == []
    assert [job["id"] for job in client.get("/api/jobs?status=expired").json()] == [created["id"]]

    invalid_response = client.patch(
        f"/api/jobs/{created['id']}/status",
        json={"status": "maybe"},
    )
    assert invalid_response.status_code == 422


def test_job_opportunity_status_can_be_updated_in_bulk_atomically(tmp_path: Path):
    client = make_client(tmp_path)
    created_jobs = [
        client.post(
            "/api/jobs/ingest",
            json={
                "source_type": "paste",
                "raw_text": f"Platform Engineer {index}\nCompany: Acme\nRemote Canada",
                "source_url": None,
            },
        ).json()
        for index in range(2)
    ]

    updated_response = client.patch(
        "/api/jobs/status/bulk",
        json={"job_ids": [job["id"] for job in created_jobs], "status": "closed"},
    )

    assert updated_response.status_code == 200
    assert {job["id"] for job in updated_response.json()} == {
        job["id"] for job in created_jobs
    }
    assert {job["status"] for job in updated_response.json()} == {"closed"}

    failed_response = client.patch(
        "/api/jobs/status/bulk",
        json={"job_ids": [created_jobs[0]["id"], "missing-job"], "status": "active"},
    )

    assert failed_response.status_code == 404
    assert client.get(f"/api/jobs/{created_jobs[0]['id']}").json()["status"] == "closed"


def test_job_opportunity_status_bulk_update_rejects_empty_or_duplicate_ids(tmp_path: Path):
    client = make_client(tmp_path)

    assert client.patch(
        "/api/jobs/status/bulk", json={"job_ids": [], "status": "closed"}
    ).status_code == 422
    assert client.patch(
        "/api/jobs/status/bulk",
        json={"job_ids": ["same-job", "same-job"], "status": "closed"},
    ).status_code == 422


def test_job_source_can_be_replaced_with_user_supplied_authoritative_url(tmp_path: Path):
    client = make_client(tmp_path)
    fallback_url = "https://8.8.4.4/aggregator/jobs/123"
    authoritative_url = "https://1.1.1.1/jobs/official-123?utm_source=aggregator"
    created = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Embedded Software Engineer Intern\nCompany: Example Networks",
            "source_url": fallback_url,
        },
    ).json()

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, created["id"])
        persisted.source = {
            **persisted.source,
            "source_resolution": {
                "kind": "aggregator_fallback",
                "official_discovery_status": "not_found",
                "fallback_url": fallback_url,
            },
        }
        previous_data_quality = dict(persisted.data_quality)

    updated_response = client.patch(
        f"/api/jobs/{created['id']}/source",
        json={"source_url": authoritative_url},
    )

    assert updated_response.status_code == 200
    updated = updated_response.json()
    sanitized_url = "https://1.1.1.1/jobs/official-123"
    assert updated["source"]["source_url"] == sanitized_url
    assert updated["source"]["source_resolution"] == {
        "kind": "manual_override",
        "provider": "user",
        "status": "resolved",
        "path": "user_supplied_authoritative_source",
        "official_discovery_status": "resolved",
        "reason_code": "user_supplied_authoritative_source",
        "original_url": fallback_url,
        "resolved_url": sanitized_url,
        "canonical_url": sanitized_url,
        "official_url": sanitized_url,
        "fallback_url": fallback_url,
    }
    assert updated["data_quality"] == previous_data_quality

    blocked_response = client.patch(
        f"/api/jobs/{created['id']}/source",
        json={"source_url": "http://127.0.0.1:9999/private"},
    )
    assert blocked_response.status_code == 400
    assert "localhost_not_allowlisted" in blocked_response.json()["detail"]

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, created["id"])
        assert persisted.source_url == sanitized_url
        assert persisted.status_next_check_at is not None
        identities = list(
            db.execute(
                select(JobSourceIdentity).where(JobSourceIdentity.job_id == created["id"])
            ).scalars()
        )
        assert any(identity.public_source_url == sanitized_url for identity in identities)
        event = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "job.source_url_updated",
                AuditLogEntry.entity_id == created["id"],
            )
        ).scalar_one()
        assert event.payload == {
            "previous_source_url": fallback_url,
            "source_url": sanitized_url,
        }


def test_linkedin_no_longer_accepting_marker_closes_new_and_existing_job(tmp_path: Path):
    client = make_client(tmp_path)
    source_url = "https://www.linkedin.com/jobs/view/9999999999/"

    new_closed_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Firmware Developer\nCompany: Example Devices\nNo longer accepting applications"
            ),
            "source_url": "https://www.linkedin.com/jobs/view/9999999998/",
        },
    )

    assert new_closed_response.status_code == 201
    new_closed = new_closed_response.json()
    assert new_closed["status"] == "closed"
    assert new_closed["status_check"]["last_checked_at"] is not None
    assert new_closed["status_check"]["result"] == "closed"
    assert new_closed["status_check"]["evidence_code"] == "linkedin_no_longer_accepting"
    assert new_closed["status_check"]["evidence_label"] == (
        "LinkedIn no longer accepting applications"
    )

    initial_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Embedded Linux Developer\nCompany: Example Systems\nCalgary, AB",
            "source_url": source_url,
        },
    )

    assert initial_response.status_code == 201
    assert initial_response.json()["status"] == "active"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
    )
    immediately_due = client.patch(
        f"/api/jobs/{initial_response.json()['id']}/status",
        json={"status": "active"},
    ).json()
    assert immediately_due["status_check"]["next_check_at"] is not None

    previous_check_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        job = db.get(JobPosting, initial_response.json()["id"])
        job.status_last_checked_at = previous_check_at
        job.status_next_check_at = previous_check_at + timedelta(hours=24)
        job.status_check_result = "open"
        job.status_check_evidence_code = "posting_identity_match"
        job.status_check_failure_count = 0
        job.status_check_not_found_count = 1

    closed_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Embedded Linux Developer\n"
                "Company: Example Systems\n"
                "Calgary, AB\n"
                "No longer accepting applications"
            ),
            "source_url": source_url,
        },
    )

    assert closed_response.status_code == 201
    assert closed_response.json()["id"] == initial_response.json()["id"]
    assert closed_response.json()["status"] == "closed"
    status_check = closed_response.json()["status_check"]
    assert status_check["last_checked_at"] is not None
    assert status_check["next_check_at"] is None
    assert status_check["result"] == "closed"
    assert status_check["evidence_code"] == "linkedin_no_longer_accepting"
    assert status_check["evidence_label"] == "LinkedIn no longer accepting applications"
    assert status_check["failure_count"] == 0
    with session_scope(settings) as db:
        assert db.get(JobPosting, initial_response.json()["id"]).status_check_not_found_count == 0
        audit = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.entity_id == new_closed["id"],
                AuditLogEntry.event_type == "job.opportunity_status_updated",
            )
        ).scalar_one()
        assert audit.payload["source"] == "ingestion_snapshot"
        assert audit.payload["evidence_code"] == "linkedin_no_longer_accepting"
    assert client.get(f"/api/jobs/{initial_response.json()['id']}").json()["status"] == "closed"


def test_linkedin_removed_or_invalid_marker_closes_job_during_ingestion(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Software Development Intern\n"
                "Company: Example Systems\n"
                "Job not found\n"
                "This job may have been removed or the job ID is incorrect."
            ),
            "source_url": "https://www.linkedin.com/jobs/view/9999999977",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "closed"
    assert payload["status_check"]["result"] == "closed"
    assert payload["status_check"]["evidence_code"] == "linkedin_job_removed_or_invalid"


def test_job_reprocess_closes_linkedin_removed_page_without_reparsing_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    client = make_client(tmp_path)
    source_url = "https://www.linkedin.com/jobs/view/9999999977"
    created = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Software Development Intern\nCompany: Example Systems\nBuild Python services."
            ),
            "source_url": source_url,
        },
    ).json()
    original_artifact_id = created["source"]["raw_content_artifact_id"]
    original_description = created["role"]["description_clean"]

    class RemovedLinkedInResolver:
        def resolve(self, _hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=source_url,
                    raw_text=(
                        "Job not found\nThis job may have been removed or the job ID is incorrect."
                    ),
                    complete_body_text=(
                        "Job not found\nThis job may have been removed or the job ID is incorrect."
                    ),
                    confidence=0.0,
                    evidence_origin="browser_dom",
                ),
            )

    monkeypatch.setattr(
        api_main,
        "default_posting_resolver",
        lambda _settings: RemovedLinkedInResolver(),
    )

    queued = client.post(f"/api/jobs/{created['id']}/reprocess")
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings,
        client.app.state.storage,
    )
    payload = client.get(f"/api/jobs/{created['id']}").json()

    assert queued.status_code == 202
    assert processed["status"] == "succeeded"
    assert processed["result"]["status"] == "terminal_status_updated"
    assert processed["result"]["ingestion_status"] == "terminal_status_detected"
    assert processed["result"]["refreshed"] is False
    assert payload["status"] == "closed"
    assert payload["status_check"]["evidence_code"] == "linkedin_job_removed_or_invalid"
    assert payload["source"]["raw_content_artifact_id"] == original_artifact_id
    assert payload["role"]["description_clean"] == original_description


def test_auth_polluted_linkedin_snapshot_cannot_close_new_or_duplicate_job(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    source_url = "https://www.linkedin.com/jobs/view/9999999988/"
    polluted_text = (
        "Platform Engineer\nCompany: Acme\nSign in to continue\nNo longer accepting applications"
    )

    created = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": polluted_text,
            "source_url": source_url,
        },
    )

    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert created.json()["status_check"]["result"] == "inconclusive"
    assert created.json()["status_check"]["evidence_code"] == "authentication_wall"

    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": polluted_text + "\nUpdated alert",
            "source_url": source_url,
        },
    )

    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == created.json()["id"]
    assert duplicate.json()["status"] == "active"


def test_incidental_ats_marker_in_description_does_not_close_new_job(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": (
                "Platform Engineer\nCompany: Acme\nPosition is no longer available\nApply now"
            ),
            "source_url": "https://boards.greenhouse.io/acme/jobs/1234567",
        },
    )

    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "active"
    assert job["status_check"] is None


def test_snapshot_duplicate_cannot_close_job_from_unstored_source_url(tmp_path: Path):
    client = make_client(tmp_path)
    raw_text = (
        "Software Developer Intern\nCompany: Example Systems\nNo longer accepting applications"
    )
    initial = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": raw_text,
        },
    )
    assert initial.status_code == 201
    assert initial.json()["status"] == "active"

    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": raw_text,
            "source_url": "https://www.linkedin.com/jobs/view/8765432109",
        },
    )

    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == initial.json()["id"]
    assert duplicate.json()["status"] == "active"
    assert duplicate.json()["status_check"] is None


def test_indeed_selected_result_snapshot_cannot_expire_duplicate_from_other_panel(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    source_url = "https://www.indeed.com/q-Platform-Engineer-jobs.html?vjk=c01efd207eb8d10a"
    initial = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            "source_url": source_url,
        },
    )
    assert initial.status_code == 201
    assert initial.json()["status"] == "active"

    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "source_url": source_url,
            "raw_text": (
                "Platform Engineer\n"
                "Company: Acme\n"
                "Job Post Details\n"
                "Registered Nurse\n"
                "Company: Hospital X\n"
                "This job has expired on Indeed\n"
                "Return to Search"
            ),
        },
    )

    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == initial.json()["id"]
    assert duplicate.json()["status"] == "active"
    assert duplicate.json()["status_check"] is None


def test_indeed_auth_wall_cannot_expire_matching_duplicate_snapshot(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    source_url = "https://www.indeed.com/q-Platform-Engineer-jobs.html?vjk=c01efd207eb8d10a"
    initial = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            "source_url": source_url,
        },
    )
    assert initial.status_code == 201
    assert initial.json()["status"] == "active"

    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "source_url": source_url,
            "raw_text": (
                "Platform Engineer\n"
                "Company: Acme\n"
                "Sign in to continue\n"
                "Job Post Details\n"
                "This job has expired on Indeed\n"
                "Reasons could include: the employer is not accepting applications\n"
                "Platform Engineer - job post\n"
                "Image\n"
                "Acme\n"
                "Return to Search"
            ),
        },
    )

    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == initial.json()["id"]
    assert duplicate.json()["status"] == "active"


def test_job_ingest_url_accepts_aggregator_fallback_import(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    source_resolution = {
        "kind": "aggregator_fallback",
        "official_discovery_status": "not_found",
        "official_discovery_error": "official_posting_not_found",
    }

    def fake_process_posting_link(context, **kwargs):
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
            linked_job_id="job_linkedin_fallback",
            payload={
                "id": "job_linkedin_fallback",
                "source_type": "url",
                "source_url": context.source_url,
                "source_resolution": source_resolution,
            },
            created=False,
            error_message="official_posting_not_found",
        )

    monkeypatch.setattr(api_main, "process_posting_link", fake_process_posting_link)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            "source_url": "https://www.linkedin.com/jobs/view/4425912315",
        },
    )

    assert response.status_code == 201
    assert response.json()["id"] == "job_linkedin_fallback"
    assert response.json()["source_resolution"] == source_resolution


def test_job_ingest_url_reports_linkedin_terminal_status(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    source_url = "https://www.linkedin.com/jobs/view/9999999977"

    def fake_process_posting_link(context, **kwargs):
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="external_job_board_link",
            ),
            ingestion_status="terminal_status_detected",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            detected_status="closed",
            status_evidence_code="linkedin_job_removed_or_invalid",
        )

    monkeypatch.setattr(api_main, "process_posting_link", fake_process_posting_link)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "LinkedIn job URL",
            "source_url": source_url,
        },
    )

    assert response.status_code == 410
    assert response.json()["detail"] == {
        "ingestion_status": "terminal_status_detected",
        "detected_status": "closed",
        "evidence_code": "linkedin_job_removed_or_invalid",
        "resolved_url": source_url,
    }


def test_job_ingest_persists_aggregator_fallback_source_resolution(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"

    class FakeResolver:
        posting_text = "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC"

        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": linkedin_url,
                    "raw_text": self.posting_text,
                    "confidence": 0.91,
                    "evidence": "Imported from source metadata after official posting search failed",
                },
                "source_resolution": {
                    "kind": "aggregator_fallback",
                    "official_discovery_status": "not_found",
                    "official_discovery_error": "official_posting_not_found",
                },
            }

    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            "source_url": linkedin_url,
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["source"]["source_resolution"] == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": linkedin_url,
    }
    assert created["data_quality"]["needs_manual_review"] is True

    fetched = client.get(f"/api/jobs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["source"]["source_resolution"] == created["source"]["source_resolution"]
    assert fetched.json()["data_quality"]["needs_manual_review"] is True

    with session_scope(client.app.state.settings) as db:
        legacy_duplicate = db.get(JobPosting, created["id"])
        legacy_duplicate.source = {
            key: value
            for key, value in legacy_duplicate.source.items()
            if key != "source_resolution"
        }
        legacy_duplicate.data_quality = {
            **legacy_duplicate.data_quality,
            "needs_manual_review": False,
        }

    duplicate_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            "source_url": linkedin_url,
        },
    )

    assert duplicate_response.status_code == 201
    duplicate = duplicate_response.json()
    assert duplicate["id"] == created["id"]
    assert duplicate["source"]["source_resolution"] == created["source"]["source_resolution"]
    assert duplicate["data_quality"]["needs_manual_review"] is True

    reviewed_response = client.post(f"/api/jobs/{created['id']}/reviewed")
    assert reviewed_response.json()["data_quality"]["needs_manual_review"] is False
    assert (
        reviewed_response.json()["data_quality"]["manual_review_completed_snapshot_hash"]
        == created["source"]["posting_snapshot_hash"]
    )
    acknowledged_duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            "source_url": linkedin_url,
        },
    ).json()
    assert acknowledged_duplicate["data_quality"]["needs_manual_review"] is False

    FakeResolver.posting_text = (
        "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC\nUpdated responsibilities"
    )
    changed_duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": FakeResolver.posting_text,
            "source_url": linkedin_url,
        },
    ).json()
    assert changed_duplicate["data_quality"]["needs_manual_review"] is True
    assert "manual_review_completed_at" not in changed_duplicate["data_quality"]
    changed_snapshot_hash = changed_duplicate["data_quality"]["manual_review_pending_snapshot_hash"]
    assert changed_snapshot_hash
    assert changed_snapshot_hash != created["source"]["posting_snapshot_hash"]

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, created["id"])
        api_main.apply_parsed_job_to_existing_posting(
            persisted,
            "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            client.app.state.settings,
            db=db,
        )

    pending_after_reparse = client.get(f"/api/jobs/{created['id']}").json()
    assert pending_after_reparse["data_quality"]["needs_manual_review"] is True
    assert (
        pending_after_reparse["data_quality"]["manual_review_pending_snapshot_hash"]
        == changed_snapshot_hash
    )
    changed_reviewed = client.post(f"/api/jobs/{created['id']}/reviewed").json()
    assert changed_reviewed["data_quality"]["needs_manual_review"] is False
    assert (
        changed_reviewed["data_quality"]["manual_review_completed_snapshot_hash"]
        == changed_snapshot_hash
    )
    assert "manual_review_pending_snapshot_hash" not in changed_reviewed["data_quality"]

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, created["id"])
        api_main.apply_parsed_job_to_existing_posting(
            persisted,
            "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            client.app.state.settings,
            db=db,
        )

    same_changed_duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": FakeResolver.posting_text,
            "source_url": linkedin_url,
        },
    ).json()
    assert same_changed_duplicate["data_quality"]["needs_manual_review"] is False
    assert (
        same_changed_duplicate["data_quality"]["manual_review_completed_snapshot_hash"]
        == changed_snapshot_hash
    )


def test_job_can_be_marked_reviewed_and_returns_to_discovered(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Software Developer Co-Op\nCompany: TrendAI\nPython Docker",
            "source_url": None,
        },
    )
    job = response.json()

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, job["id"])
        persisted.data_quality = {
            **persisted.data_quality,
            "needs_manual_review": True,
        }

    reviewed_response = client.post(f"/api/jobs/{job['id']}/reviewed")

    assert reviewed_response.status_code == 200
    reviewed = reviewed_response.json()
    assert reviewed["data_quality"]["needs_manual_review"] is False
    assert reviewed["data_quality"]["manual_review_completed_at"]
    assert (
        reviewed["data_quality"]["manual_review_completed_snapshot_hash"]
        == reviewed["source"]["posting_snapshot_hash"]
    )

    fetched = client.get(f"/api/jobs/{job['id']}").json()
    assert fetched["data_quality"] == reviewed["data_quality"]

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, job["id"])
        api_main.apply_parsed_job_to_existing_posting(
            persisted,
            "Software Developer Co-Op\nCompany: TrendAI\nPython Docker",
            client.app.state.settings,
            db=db,
        )

    reparsed = client.get(f"/api/jobs/{job['id']}").json()
    assert reparsed["data_quality"] == reviewed["data_quality"]

    with session_scope(client.app.state.settings) as db:
        persisted = db.get(JobPosting, job["id"])
        refreshed = api_main.update_existing_job_from_ingest_payload(
            persisted,
            api_main.JobIngestPayload(
                source_type="paste",
                raw_text="Software Developer Co-Op\nCompany: TrendAI\nPython Docker",
                source_url=None,
            ),
            db,
            client.app.state.storage,
            client.app.state.settings,
        )
        assert refreshed.payload["data_quality"] == reviewed["data_quality"]

    with session_scope(client.app.state.settings) as db:
        event = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "job.manual_review_completed",
                AuditLogEntry.entity_id == job["id"],
            )
        ).scalar_one()
        assert event.payload == {"previously_needed_review": True}


def test_reprocess_update_preserves_existing_company_when_blocked_page_parse_is_weak(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_parser_provider="heuristic",
        fit_analysis_provider="disabled",
    )
    init_database(settings)
    storage = api_main.ArtifactStorageService(settings.artifact_root)
    original = api_main.JobIngestPayload(
        source_type="url",
        source_url="https://www.linkedin.com/jobs/view/4425694183",
        raw_text=(
            "Software Developer Co-Op - Fall 2026\n"
            "Company: TrendAI\n"
            "Location: Ottawa, Ontario, Canada\n"
        ),
    )
    payload = api_main.JobIngestPayload(
        source_type="url",
        source_url="https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4425694183",
        raw_text=(
            "Page Title: Sign Up | LinkedIn\n"
            "Source URL: https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4425694183\n"
            "Source Evidence: Software Developer Co-Op - Fall 2026 TrendAI Title: Software Developer Co-Op - Fall 2026 Company: TrendAI\n"
            "Sign in to continue\n"
            "Email or phone\n"
        ),
    )

    with session_scope(settings) as db:
        created = api_main.persist_job_ingest(original, db, storage, settings)
        job = db.get(JobPosting, created["id"])
        result = api_main.update_existing_job_from_ingest_payload(
            job, payload, db, storage, settings
        )
        db.flush()

    assert result.payload["employer"]["company_name"] == "TrendAI"


def test_reprocess_update_marks_aggregator_fallback_for_manual_review(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_parser_provider="heuristic",
        fit_analysis_provider="disabled",
    )
    init_database(settings)
    storage = api_main.ArtifactStorageService(settings.artifact_root)
    source_url = "https://ca.indeed.com/viewjob?jk=gated"
    original = api_main.JobIngestPayload(
        source_type="url",
        source_url=source_url,
        raw_text="Platform Engineer\nCompany: Acme",
    )
    fallback = api_main.JobIngestPayload(
        source_type="url",
        source_url=source_url,
        raw_text="Title: Platform Engineer\nCompany: Acme\nApply URL: " + source_url,
        source_resolution={
            "kind": "aggregator_fallback",
            "path": "aggregator_structured_fallback",
            "official_discovery_status": "auth_blocked",
            "reason_code": "chrome_cdp_failed:_blocked_rendered_page",
            "fallback_url": source_url,
        },
    )

    with session_scope(settings) as db:
        created = api_main.persist_job_ingest(original, db, storage, settings)
        job = db.get(JobPosting, created["id"])
        result = api_main.update_existing_job_from_ingest_payload(
            job,
            fallback,
            db,
            storage,
            settings,
        )
        db.flush()

    assert result.payload["source"]["source_resolution"] == fallback.source_resolution
    assert result.payload["data_quality"]["needs_manual_review"] is True


def test_integrity_race_duplicate_marks_aggregator_fallback_for_manual_review(
    tmp_path: Path,
    monkeypatch,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_parser_provider="heuristic",
        fit_analysis_provider="disabled",
    )
    init_database(settings)
    storage = api_main.ArtifactStorageService(settings.artifact_root)
    source_url = "https://ca.indeed.com/viewjob?jk=race"
    original = api_main.JobIngestPayload(
        source_type="url",
        source_url=source_url,
        raw_text="Platform Engineer\nCompany: Acme",
    )
    fallback = api_main.JobIngestPayload(
        source_type="url",
        source_url=source_url,
        raw_text=original.raw_text,
        source_resolution={
            "kind": "aggregator_fallback",
            "path": "aggregator_structured_fallback",
            "official_discovery_status": "auth_blocked",
            "reason_code": "chrome_cdp_failed:_blocked_rendered_page",
            "fallback_url": source_url,
        },
    )

    with session_scope(settings) as db:
        created = api_main.persist_job_ingest(original, db, storage, settings)
        existing = db.get(JobPosting, created["id"])
        monkeypatch.setattr(
            api_main, "find_duplicate_job_posting_by_snapshot_hash", lambda *args: None
        )
        lookup_count = 0

        def simulate_racing_duplicate(*args, **kwargs):
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 1:
                return None, None
            return existing, "snapshot_hash"

        monkeypatch.setattr(api_main, "find_duplicate_job_posting", simulate_racing_duplicate)
        result = api_main.persist_job_ingest_result(fallback, db, storage, settings)
        db.flush()

    assert result.created is False
    assert result.payload["source"]["source_resolution"] == fallback.source_resolution
    assert result.payload["data_quality"]["needs_manual_review"] is True


def test_job_ingestion_creates_raw_artifact_and_application_package(tmp_path: Path):
    client = make_client(tmp_path)

    ingest = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada\nSalary 70000",
            "source_url": None,
        },
    )
    assert ingest.status_code == 201
    job = ingest.json()
    assert job["role"]["job_title"] == "Acme Data Engineer"
    assert job["role"]["tech_stack"] == ["Python", "SQL", "FastAPI"]
    assert job["data_quality"]["missing_fields"] == []
    assert job["created_at"]
    assert job["source"]["raw_content_artifact_id"].startswith("artifact_")
    assert "source_archive_artifact_id" not in job["source"]
    assert "source_archive_download_url" not in job["source"]
    assert job["application_summary"]["applied_at"] is None

    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    assert application["job_id"] == job["id"]
    assert application["status"] == "draft"

    tracked_job = client.get(f"/api/jobs/{job['id']}").json()
    assert tracked_job["application_summary"]["application_id"] == application["application_id"]
    assert tracked_job["application_summary"]["application_status"] == "draft"
    assert tracked_job["application_summary"]["decision"] == "undecided"

    listed_job = client.get("/api/jobs").json()[0]
    assert listed_job["application_summary"]["application_id"] == application["application_id"]
    assert listed_job["created_at"] == job["created_at"]

    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada\nSalary 70000",
            "source_url": None,
        },
    ).json()
    assert duplicate["application_summary"]["application_id"] == application["application_id"]

    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    assert package["application_id"] == application["application_id"]
    assert package["status"] == "draft"

    resume = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    )
    assert resume.status_code == 201
    document = resume.json()
    assert document["document_type"] == "resume"
    assert document["artifact_id"].startswith("artifact_")


def test_mark_job_applied_creates_application_and_is_idempotent(tmp_path: Path):
    client = make_client(tmp_path)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Applied Workflow Engineer\nCompany: Acme\nPython SQL",
            "source_url": None,
        },
    ).json()

    first_response = client.post(f"/api/jobs/{job['id']}/applications/applied")

    assert first_response.status_code == 200
    first = first_response.json()
    assert first["job_id"] == job["id"]
    assert first["status"] == "applied"
    assert datetime.fromisoformat(first["applied_at"]).tzinfo is not None

    repeated_response = client.post(f"/api/jobs/{job['id']}/applications/applied")

    assert repeated_response.status_code == 200
    repeated = repeated_response.json()
    assert repeated["application_id"] == first["application_id"]
    assert repeated["applied_at"] == first["applied_at"]

    tracked_job = client.get(f"/api/jobs/{job['id']}").json()
    assert tracked_job["status"] == "active"
    assert tracked_job["application_summary"]["application_status"] == "applied"
    assert tracked_job["application_summary"]["applied_at"] == first["applied_at"]

    with session_scope(client.app.state.settings) as db:
        created_audit_entries = list(
            db.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.event_type == "application.created",
                    AuditLogEntry.entity_id == first["application_id"],
                )
            ).scalars()
        )
        audit_entries = list(
            db.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.event_type == "application.applied",
                    AuditLogEntry.entity_id == first["application_id"],
                )
            ).scalars()
        )
        assert len(created_audit_entries) == 1
        assert len(audit_entries) == 1
        assert audit_entries[0].payload["previous_status"] is None
        assert audit_entries[0].payload["job_id"] == job["id"]


def test_mark_job_applied_updates_existing_draft_application(tmp_path: Path):
    client = make_client(tmp_path)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Existing Draft Engineer\nCompany: Acme\nFastAPI",
            "source_url": None,
        },
    ).json()
    draft = client.post(f"/api/jobs/{job['id']}/applications").json()

    response = client.post(f"/api/jobs/{job['id']}/applications/applied")

    assert response.status_code == 200
    applied = response.json()
    assert applied["application_id"] == draft["application_id"]
    assert applied["status"] == "applied"
    assert applied["applied_at"] is not None


def test_required_cover_letter_is_created_and_marked_required_in_package(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/profile", json={"first_name": "Jane", "last_name": "Candidate", "email": "jane@example.test"})
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Example Intern\nSubmit your resume and a short cover letter.",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()

    package_response = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages"))

    assert package_response.status_code in {200, 201}
    package = package_response.json()
    documents = client.get(
        f"/api/application-packages/{package['application_package_id']}/documents"
    ).json()
    cover_letter = next(
        document for document in documents if document["document_type"] == "cover_letter"
    )
    assert cover_letter["required"] is True


def test_job_ingestion_uses_generic_ats_mock_source_type(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "ats_mock",
            "raw_text": "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada",
            "source_url": "http://localhost:8001/ats/generic/acme/jobs/123",
        },
    )
    legacy_response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "vendor_specific_mock",
            "raw_text": "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada",
            "source_url": "http://localhost:8001/ats/generic/acme/jobs/123",
        },
    )

    assert response.status_code == 201
    assert response.json()["source"]["source_type"] == "ats_mock"
    assert legacy_response.status_code == 422


def test_job_ingestion_enriches_fit_analysis_with_profile_context(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, fit_analysis_provider="codex_cli")
    profile_response = client.post(
        "/api/profile",
        json={
            "first_name": "Jane",
            "last_name": "Candidate",
            "email": "jane" + "@" + "example.invalid",
            "city": "Calgary",
            "province_state": "AB",
            "country": "Canada",
            "current_year": "3rd year",
            "graduation_date": "May 2027",
            "co_op_program": True,
            "summary": "Backend student focused on APIs.",
            "skills": ["Python", "SQL", "FastAPI"],
        },
    )
    assert profile_response.status_code == 201
    with session_scope(client.app.state.settings) as db:
        db.add(
            EvidenceItem(
                evidence_id="evidence_fit_allowed",
                candidate_id="candidate_local",
                source_type="resume_upload",
                claim="Built FastAPI services backed by SQL databases.",
                skills=["FastAPI", "SQL"],
                allowed_for_resume=True,
                allowed_for_cover_letter=True,
            )
        )
        db.add(
            EvidenceItem(
                evidence_id="evidence_fit_disallowed",
                candidate_id="candidate_local",
                source_type="resume_upload",
                claim="Disallowed private claim.",
                skills=[],
                allowed_for_resume=False,
                allowed_for_cover_letter=True,
            )
        )

    calls = []

    def fake_analyze_fit(
        parsed_job,
        *,
        raw_text,
        source_url,
        settings,
        profile_data,
        evidence_items,
    ):
        calls.append(
            {
                "title": parsed_job.role.job_title,
                "raw_text": raw_text,
                "source_url": source_url,
                "profile": profile_data,
                "evidence": [item.claim for item in evidence_items],
            }
        )
        return ParsedFit(
            fit_score=91,
            technical_match_score=95,
            eligibility_score=90,
            ranking_reason=["Profile evidence matches Python and SQL requirements"],
        )

    monkeypatch.setattr(api_main, "require_fit_analysis_with_settings", fake_analyze_fit, raising=False)

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Engineering Intern\nCompany: Globex\nLocation: Toronto, ON\nPython SQL",
            "source_url": None,
        },
    )

    assert response.status_code == 201
    imported = response.json()
    assert calls == []
    assert imported["reprocess"]["type"] == "fit_analysis"
    assert imported["reprocess"]["status"] == "queued"
    processed = api_main.process_next_job_reprocess(client.app.state.settings, client.app.state.storage)
    assert processed["status"] == "succeeded"
    payload = client.get(f"/api/jobs/{imported['id']}").json()
    assert payload["fit_analysis"]["fit_score"] == 91
    assert payload["fit_analysis"]["technical_match_score"] == 95
    assert calls[0]["profile"]["summary"] == "Backend student focused on APIs."
    assert calls[0]["evidence"] == ["Built FastAPI services backed by SQL databases."]
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, payload["id"])
        assert job.fit_analysis["fit_score"] == 91


def test_duplicate_job_ingestion_by_source_url_skips_fit_analysis(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        fit_analysis_provider="codex_cli",
        posting_rendered_fallback_provider="disabled",
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    source_url = "http://localhost:8001/ats/generic/acme/jobs/fit-duplicate"
    analyze_calls = []

    def fake_analyze_fit(
        parsed_job,
        *,
        raw_text,
        source_url,
        settings,
        profile_data,
        evidence_items,
    ):
        analyze_calls.append((parsed_job.role.job_title, raw_text, source_url))
        return ParsedFit(fit_score=82, ranking_reason=["Fit analysis ran"])

    monkeypatch.setattr(api_main, "require_fit_analysis_with_settings", fake_analyze_fit, raising=False)

    first = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "source_url": source_url,
        },
    )
    second = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs\nUpdated posting details",
            "source_url": source_url,
        },
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert analyze_calls == []
    assert first.json()["reprocess"]["reprocess_run_id"] == second.json()["reprocess"]["reprocess_run_id"]
    processed = api_main.process_next_job_reprocess(settings, client.app.state.storage)
    assert processed["status"] == "succeeded"
    assert analyze_calls == [
        ("Backend Developer", "Backend Developer\nCompany: Acme\nPython APIs", source_url)
    ]
    with session_scope(settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1


def test_url_job_ingestion_archives_source_response_for_audit_download(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    app = create_app(settings)
    client = TestClient(app)
    source_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    payload = api_main.JobIngestPayload(
        source_type="url",
        raw_text="Platform Engineer\nCompany: Acme\nPython APIs",
        source_url=source_url,
        source_archive=SourceArchive(
            source_url=source_url,
            final_url=source_url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=b"<html><h1>Platform Engineer</h1><p>Python APIs</p></html>",
            headers={"etag": '"posting-v1"'},
        ),
    )

    with session_scope(settings) as db:
        result = api_main.persist_job_ingest(payload, db, app.state.storage, settings)
        job_id = result["id"]
        archive_id = result["source"]["source_archive_artifact_id"]

    assert archive_id.startswith("artifact_")
    assert result["source"]["source_archive_download_url"] == (
        f"/api/jobs/{job_id}/source-archive/{archive_id}/download"
    )
    assert result["source"]["source_archive"] == {
        "artifact_id": archive_id,
        "source_url": source_url,
        "final_url": source_url,
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "headers": {"etag": '"posting-v1"'},
    }
    with session_scope(settings) as db:
        artifacts = {
            artifact.artifact_id: artifact
            for artifact in db.execute(select(StoredArtifact)).scalars()
        }
        archive_artifact = artifacts[archive_id]
        assert archive_artifact.artifact_type == "job_source_archive"
        assert archive_artifact.job_id == job_id
        assert archive_artifact.source_kind == "url_source_archive"
        assert archive_artifact.content_type == "text/html; charset=utf-8"

    download = client.get(result["source"]["source_archive_download_url"])

    assert download.status_code == 200
    assert download.content == b"<html><h1>Platform Engineer</h1><p>Python APIs</p></html>"
    assert download.headers["content-type"].startswith("text/html")
    assert (
        download.headers["content-disposition"]
        == "attachment; filename*=UTF-8''job-source-archive.html"
    )


def test_job_reprocess_reparses_existing_job_from_raw_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    client = make_client(
        tmp_path,
        fit_analysis_provider="disabled",
        posting_parser_provider="heuristic",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    raw_text = (
        "Page Title: Blocked - Indeed.com\n"
        "Source URL: https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926\n"
        "Source Evidence: Software Development Intern Software Development Intern "
        "\u00b7 Birdseye Solutions \u00b7 Mississauga, ON, CA Software Development Intern "
        "Company: Birdseye Solutions Location: Mississauga, Ontario, Canada\n"
        "Request Blocked\n"
        "You have been blocked.\n"
        "Location: Mississauga, Ontario, Canada"
    )
    fit_inputs: list[str] = []

    def capture_fit_input(parsed, *, raw_text, **_kwargs):
        fit_inputs.append(raw_text)

    monkeypatch.setattr(api_main, "enrich_parsed_job_fit_analysis", capture_fit_input)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": raw_text,
            "source_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        },
    ).json()

    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        job_row.company_name = "Page"
        job_row.normalized_title = "page_title_blocked_indeed_com"
        job_row.employer = {"company_name": "Page", "division": None, "industry": None}
        job_row.role = {
            **job_row.role,
            "job_title": "Page Title: Blocked - Indeed.com",
            "normalized_title": "page_title_blocked_indeed_com",
        }

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["status"] == "succeeded"
    payload = client.get(f"/api/jobs/{job['id']}").json()
    assert payload["employer"]["company_name"] == "Birdseye Solutions"
    assert payload["role"]["job_title"] == "Software Development Intern"
    assert payload["role"]["normalized_title"] == "software_development_intern"
    assert payload["role"]["description_raw"] == raw_text
    assert not payload["role"]["description_raw"].startswith("Title:")
    assert not payload["role"]["description_clean"].startswith(
        "Title: Software Development Intern"
    )
    assert fit_inputs[-1] == raw_text
    assert payload["reprocess"]["result"] == {
        "status": "snapshot_reparsed",
        "refreshed": False,
        "ingestion_status": "needs_external_resolution",
        "resolved_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        "error_message": "posting_resolver_not_configured",
        "source_resolution": {
            "official_discovery_status": "failed",
            "reason_code": "posting_resolver_not_configured",
            "official_discovery_error": "posting_resolver_not_configured",
        },
    }
    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        assert job_row.company_name == "Birdseye Solutions"
        assert job_row.normalized_title == "software_development_intern"
        assert job_row.role["description_raw"] == raw_text


def test_job_reprocess_reports_aggregator_fallback_refresh(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        fit_analysis_provider="disabled",
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            "source_url": "https://www.linkedin.com/jobs/view/4425912315",
        },
    ).json()
    source_resolution = {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": "https://www.linkedin.com/jobs/view/4425912315",
    }

    def fake_prepare_reprocess_source(job_row, raw_text, settings):
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="external_job_board_link",
            ),
            ingestion_status="fallback_imported",
            original_url="https://www.linkedin.com/jobs/view/4425912315",
            resolved_url="https://www.linkedin.com/jobs/view/4425912315",
            canonical_url="https://www.linkedin.com/jobs/view/4425912315",
            source_resolution=source_resolution,
            ingest_request=api_main.JobIngestPayload(
                source_type=job_row.source_type, raw_text=raw_text, source_url=job_row.source_url,
                source_resolution=source_resolution,
            ),
            error_message="official_posting_not_found",
        )

    monkeypatch.setattr(
        api_main, "prepare_reprocess_source", fake_prepare_reprocess_source
    )

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["status"] == "succeeded"
    payload = client.get(f"/api/jobs/{job['id']}").json()
    assert payload["reprocess"]["result"] == {
        "status": "fallback_refreshed",
        "refreshed": True,
        "ingestion_status": "fallback_imported",
        "resolved_url": "https://www.linkedin.com/jobs/view/4425912315",
        "error_message": "official_posting_not_found",
        "source_resolution": source_resolution,
    }


def test_fit_analysis_reprocess_updates_only_fit_analysis_from_current_profile(
    tmp_path: Path, monkeypatch
):
    client = make_client(
        tmp_path,
        fit_analysis_provider="codex_cli",
        posting_parser_provider="heuristic",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    profile_response = client.post(
        "/api/profile",
        json={
            "first_name": "Jane",
            "last_name": "Candidate",
            "email": "jane" + "@" + "example.invalid",
            "summary": "Initial backend profile.",
            "skills": ["Python"],
        },
    )
    assert profile_response.status_code == 201
    with session_scope(client.app.state.settings) as db:
        db.add(
            EvidenceItem(
                evidence_id="fit_refresh_allowed",
                candidate_id="candidate_local",
                source_type="resume_upload",
                claim="Built Python services for internal tools.",
                skills=["Python"],
                allowed_for_resume=True,
                allowed_for_cover_letter=True,
            )
        )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "source_url": None,
        },
    ).json()
    original_company = job["employer"]["company_name"]
    original_title = job["role"]["job_title"]

    fit_calls = []

    def fake_analyze_fit(
        parsed_job,
        *,
        raw_text,
        source_url,
        settings,
        profile_data,
        evidence_items,
    ):
        fit_calls.append(
            {
                "title": parsed_job.role.job_title,
                "raw_text": raw_text,
                "profile_summary": profile_data["summary"],
                "evidence": [item.claim for item in evidence_items],
            }
        )
        return ParsedFit(fit_score=93, ranking_reason=["Updated profile fits Python API work"])

    monkeypatch.setattr(api_main, "analyze_fit_with_settings", fake_analyze_fit, raising=False)
    monkeypatch.setattr(
        api_main, "require_fit_analysis_with_settings", fake_analyze_fit, raising=False
    )
    profile_update = client.put(
        "/api/profile",
        json={
            "first_name": "Jane",
            "last_name": "Candidate",
            "email": "jane" + "@" + "example.invalid",
            "summary": "Updated backend profile.",
            "skills": ["Python", "FastAPI"],
        },
    )
    assert profile_update.status_code == 200

    response = client.post(f"/api/jobs/{job['id']}/fit-analysis")

    assert response.status_code == 202
    payload = response.json()
    assert payload["queued"] is False
    assert payload["reprocess"]["reprocess_run_id"] == job["reprocess"]["reprocess_run_id"]
    assert payload["reprocess"]["type"] == "fit_analysis"
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["type"] == "fit_analysis"
    assert processed["status"] == "succeeded"
    assert processed["result"] == {"status": "fit_analysis_refreshed", "fit_score": 93}
    refreshed = client.get(f"/api/jobs/{job['id']}").json()
    assert refreshed["fit_analysis"]["fit_score"] == 93
    assert refreshed["fit_analysis"]["ranking_reason"] == ["Updated profile fits Python API work"]
    assert refreshed["employer"]["company_name"] == original_company
    assert refreshed["role"]["job_title"] == original_title
    assert fit_calls == [
        {
            "title": original_title,
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "profile_summary": "Updated backend profile.",
            "evidence": ["Built Python services for internal tools."],
        }
    ]


def test_fit_analysis_reprocess_fails_when_provider_is_disabled(tmp_path: Path):
    client = make_client(
        tmp_path,
        fit_analysis_provider="disabled",
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "source_url": None,
        },
    ).json()
    original_fit_analysis = job["fit_analysis"]

    response = client.post(f"/api/jobs/{job['id']}/fit-analysis")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["type"] == "fit_analysis"
    assert processed["status"] == "failed"
    assert processed["error_code"] == "fit_analysis_provider_disabled"
    assert processed["error_message"] == "Fit analysis provider is disabled"
    refreshed = client.get(f"/api/jobs/{job['id']}").json()
    assert refreshed["fit_analysis"] == original_fit_analysis
    assert refreshed["reprocess"]["status"] == "failed"


def test_fit_analysis_reprocess_fails_when_analyzer_returns_no_result(tmp_path: Path):
    from jobcockpit.ingestion.llm_providers import register_fit_analyzer_provider

    class NullFitAnalyzer:
        last_error = ""

        def analyze(
            self,
            parsed_job,
            *,
            raw_text,
            source_url,
            profile_data,
            evidence_items,
        ):
            self.last_error = "unit_test_no_fit_analysis"
            return None

    register_fit_analyzer_provider(
        "unit_test_null_fit",
        lambda settings: NullFitAnalyzer(),
        replace=True,
    )
    client = make_client(
        tmp_path,
        fit_analysis_provider="unit_test_null_fit",
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "source_url": None,
        },
    ).json()

    response = client.post(f"/api/jobs/{job['id']}/fit-analysis")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["type"] == "fit_analysis"
    assert processed["status"] == "retry_scheduled"
    assert processed["attempts"] == 1
    assert processed["next_attempt_at"] is not None
    assert processed["error_code"] == "fit_analysis_unavailable"
    assert processed["error_message"] == "Fit analysis could not be completed"
    assert "unit_test_no_fit_analysis" not in str(processed)


def test_job_reprocess_endpoint_enqueues_without_running_pipeline(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Software Development Intern\nCompany: Birdseye Solutions",
            "source_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        },
    ).json()

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "reprocess pipeline should run in the queue worker, not in the POST request"
        )

    monkeypatch.setattr(api_main, "prepare_reprocess_source", fail_if_called)

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    payload = response.json()
    assert payload["queued"] is True
    assert payload["reprocess"]["status"] == "queued"
    assert payload["reprocess"]["job_id"] == job["id"]
    queued_run_id = payload["reprocess"]["reprocess_run_id"]
    assert payload["job"]["reprocess"]["reprocess_run_id"] == queued_run_id
    with session_scope(client.app.state.settings) as db:
        runs = list(db.execute(select(JobReprocessRun)).scalars())
        assert [run.reprocess_run_id for run in runs] == [queued_run_id]

    duplicate = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert duplicate.status_code == 202
    assert duplicate.json()["queued"] is False
    assert duplicate.json()["reprocess"]["reprocess_run_id"] == queued_run_id


def test_fit_analysis_reprocess_reuses_active_job_queue_run(tmp_path: Path):
    client = make_client(
        tmp_path,
        fit_analysis_provider="codex_cli",
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
            "source_url": None,
        },
    ).json()
    full_reprocess = client.post(f"/api/jobs/{job['id']}/reprocess")
    fit_reprocess = client.post(f"/api/jobs/{job['id']}/fit-analysis")

    assert full_reprocess.status_code == 202
    assert fit_reprocess.status_code == 202
    assert fit_reprocess.json()["queued"] is False
    assert (
        fit_reprocess.json()["reprocess"]["reprocess_run_id"]
        == full_reprocess.json()["reprocess"]["reprocess_run_id"]
    )
    assert fit_reprocess.json()["reprocess"]["type"] == "full_reprocess"


@pytest.mark.parametrize(
    ("existing_status", "age", "expected_queued", "expected_same_run", "expected_existing_status"),
    [
        ("queued", "fresh", False, True, "queued"),
        ("running", "fresh", False, True, "running"),
        ("running", "stale", True, False, "failed"),
        ("succeeded", "fresh", True, False, "succeeded"),
        ("failed", "fresh", True, False, "failed"),
    ],
)
def test_job_reprocess_endpoint_queue_state_matrix(
    tmp_path: Path,
    existing_status: str,
    age: str,
    expected_queued: bool,
    expected_same_run: bool,
    expected_existing_status: str,
):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Software Development Intern\nCompany: Birdseye Solutions",
            "source_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        },
    ).json()
    first = client.post(f"/api/jobs/{job['id']}/reprocess").json()
    existing_run_id = first["reprocess"]["reprocess_run_id"]
    timestamp = datetime.now(UTC)
    if age == "stale":
        timestamp = timestamp - api_main.JOB_REPROCESS_STALE_AFTER - timedelta(seconds=1)
    with session_scope(client.app.state.settings) as db:
        existing_run = db.get(JobReprocessRun, existing_run_id)
        existing_run.status = existing_status
        existing_run.started_at = timestamp if existing_status == "running" else None
        existing_run.updated_at = timestamp
        existing_run.finished_at = timestamp if existing_status in {"succeeded", "failed"} else None

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    payload = response.json()
    assert payload["queued"] is expected_queued
    if expected_same_run:
        assert payload["reprocess"]["reprocess_run_id"] == existing_run_id
    else:
        assert payload["reprocess"]["reprocess_run_id"] != existing_run_id
    with session_scope(client.app.state.settings) as db:
        existing_run = db.get(JobReprocessRun, existing_run_id)
        assert existing_run.status == expected_existing_status
        if age == "stale":
            assert existing_run.error_message == "reprocess_worker_stale"


def test_job_detail_marks_stale_running_reprocess_run(tmp_path: Path):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Software Development Intern\nCompany: Birdseye Solutions",
            "source_url": None,
        },
    ).json()
    first = client.post(f"/api/jobs/{job['id']}/reprocess").json()
    stale_timestamp = datetime.now(UTC) - api_main.JOB_REPROCESS_STALE_AFTER - timedelta(seconds=1)
    with session_scope(client.app.state.settings) as db:
        existing_run = db.get(JobReprocessRun, first["reprocess"]["reprocess_run_id"])
        existing_run.status = "running"
        existing_run.started_at = stale_timestamp
        existing_run.updated_at = stale_timestamp

    response = client.get(f"/api/jobs/{job['id']}")

    assert response.status_code == 200
    assert response.json()["reprocess"]["status"] == "running"
    assert response.json()["reprocess"]["stale"] is True


def test_job_reprocess_active_run_constraint_rejects_duplicate_active_rows(tmp_path: Path):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Software Development Intern\nCompany: Birdseye Solutions",
            "source_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        },
    ).json()

    with pytest.raises(IntegrityError):
        with session_scope(client.app.state.settings) as db:
            db.add(
                JobReprocessRun(
                    reprocess_run_id="reprocess_active_one", job_id=job["id"], status="queued"
                )
            )
            db.flush()
            db.add(
                JobReprocessRun(
                    reprocess_run_id="reprocess_active_two", job_id=job["id"], status="running"
                )
            )
            db.flush()


@pytest.mark.parametrize(
    ("existing_status", "expected_claimed", "expected_status", "expected_attempts"),
    [
        ("queued", True, "running", 1),
        ("running", False, "running", 0),
        ("succeeded", False, "succeeded", 0),
        ("failed", False, "failed", 0),
    ],
)
def test_job_reprocess_claim_state_matrix(
    tmp_path: Path,
    existing_status: str,
    expected_claimed: bool,
    expected_status: str,
    expected_attempts: int,
):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Software Development Intern\nCompany: Birdseye Solutions",
            "source_url": "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        },
    ).json()
    run_id = "reprocess_claim_matrix"
    with session_scope(client.app.state.settings) as db:
        db.add(JobReprocessRun(reprocess_run_id=run_id, job_id=job["id"], status=existing_status))

    with session_scope(client.app.state.settings) as db:
        claimed = api_main.claim_job_reprocess_run(db, run_id)

    assert (claimed is not None) is expected_claimed
    with session_scope(client.app.state.settings) as db:
        run = db.get(JobReprocessRun, run_id)
        assert run.status == expected_status
        assert run.attempts == expected_attempts


def test_job_reprocess_worker_drains_queue_and_updates_job(tmp_path: Path):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    source_url = "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926"
    raw_text = (
        "Page Title: Blocked - Indeed.com\n"
        f"Source URL: {source_url}\n"
        "Source Evidence: Software Development Intern Software Development Intern "
        "\u00b7 Birdseye Solutions \u00b7 Mississauga, ON, CA Software Development Intern "
        "Company: Birdseye Solutions Location: Mississauga, Ontario, Canada\n"
        "Request Blocked\n"
        "You have been blocked.\n"
        "Location: Mississauga, Ontario, Canada"
    )
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "gmail_alert", "raw_text": raw_text, "source_url": source_url},
    ).json()
    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        job_row.company_name = "Page"
        job_row.normalized_title = "page_title_blocked_indeed_com"
        job_row.employer = {"company_name": "Page", "division": None, "industry": None}
        job_row.role = {
            **job_row.role,
            "job_title": "Page Title: Blocked - Indeed.com",
            "normalized_title": "page_title_blocked_indeed_com",
        }

    enqueue = client.post(f"/api/jobs/{job['id']}/reprocess")
    run_id = enqueue.json()["reprocess"]["reprocess_run_id"]

    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )

    assert processed is not None
    assert processed["reprocess_run_id"] == run_id
    assert processed["status"] == "succeeded"
    assert processed["result"] == {
        "status": "snapshot_reparsed",
        "refreshed": False,
        "ingestion_status": "needs_external_resolution",
        "resolved_url": source_url,
        "error_message": "posting_resolver_not_configured",
        "source_resolution": {
            "official_discovery_status": "failed",
            "reason_code": "posting_resolver_not_configured",
            "official_discovery_error": "posting_resolver_not_configured",
        },
    }
    refreshed_job = client.get(f"/api/jobs/{job['id']}").json()
    assert refreshed_job["employer"]["company_name"] == "Birdseye Solutions"
    assert refreshed_job["role"]["job_title"] == "Software Development Intern"
    assert refreshed_job["reprocess"]["status"] == "succeeded"


def test_job_reprocess_persists_unresolved_discovery_trace(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    source_url = "https://ca.indeed.com/viewjob?jk=unresolved-reprocess-trace"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme",
            "source_url": source_url,
        },
    ).json()
    existing_resolution = {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "fallback_url": source_url,
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
    }
    trace_update = {
        "official_discovery_status": "failed",
        "reason_code": "codex_cli_failed",
        "official_discovery_error": "codex_cli_failed",
        "attempts": [
            {
                "stage": "official_search",
                "provider": "codex_cli",
                "status": "needs_review",
                "reason_code": "codex_cli_failed",
                "candidate_count": 0,
            }
        ],
    }

    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        job_row.source = {
            **job_row.source,
            "source_resolution": existing_resolution,
        }

    def unresolved_reprocess(*args, **kwargs):
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="requires_external_resolution",
                confidence=0.9,
                reason="unit_test",
            ),
            ingestion_status="needs_external_resolution",
            original_url=source_url,
            resolved_url=source_url,
            canonical_url=source_url,
            error_message="codex_cli_failed",
            source_resolution=trace_update,
        )

    monkeypatch.setattr(
        api_main,
        "prepare_reprocess_source",
        unresolved_reprocess,
    )

    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        result = api_main.execute_job_reprocess(
            job_row,
            db,
            client.app.state.storage,
            client.app.state.settings,
        )
        persisted_resolution = job_row.source["source_resolution"]

    assert result["status"] == "snapshot_reparsed"
    assert result["source_resolution"] == {
        **existing_resolution,
        **trace_update,
    }
    assert persisted_resolution == result["source_resolution"]


def test_merge_reprocess_source_resolution_replaces_run_scoped_trace():
    job = SimpleNamespace(
        source={
            "platform": "url",
            "source_resolution": {
                "kind": "aggregator_fallback",
                "path": "aggregator_structured_fallback",
                "fallback_url": "https://ca.indeed.com/viewjob?jk=trace-replacement",
                "official_discovery_status": "timeout",
                "reason_code": "codex_cli_timeout",
                "official_discovery_error": "codex_cli_timeout",
                "attempts": [
                    {
                        "stage": "official_search",
                        "provider": "codex_cli",
                        "status": "needs_review",
                        "reason_code": "codex_cli_timeout",
                    }
                ],
                "trace_truncated": True,
            },
        }
    )

    merged = api_main.merge_reprocess_source_resolution(
        job,
        {
            "official_discovery_status": "failed",
            "reason_code": "posting_resolver_not_configured",
            "official_discovery_error": "posting_resolver_not_configured",
        },
    )

    assert merged["kind"] == "aggregator_fallback"
    assert "attempts" not in merged
    assert "trace_truncated" not in merged


def test_merge_reprocess_source_resolution_replaces_complete_identity():
    job = SimpleNamespace(
        source={
            "platform": "url",
            "source_resolution": {
                "kind": "aggregator_fallback",
                "path": "aggregator_structured_fallback",
                "fallback_url": "https://ca.indeed.com/viewjob?jk=old-fallback",
                "official_discovery_status": "failed",
                "reason_code": "codex_cli_failed",
                "official_discovery_error": "codex_cli_failed",
            },
        }
    )
    official_resolution = {
        "kind": "official_discovery",
        "path": "discovered_ats",
        "official_url": "https://careers.acme.example/jobs/platform-engineer",
        "official_discovery_status": "candidates_found",
        "reason_code": "official_candidates_found",
    }

    merged = api_main.merge_reprocess_source_resolution(job, official_resolution)

    assert merged == official_resolution
    assert job.source["source_resolution"] == official_resolution


def test_job_reprocess_refetches_external_source_before_snapshot_fallback(
    tmp_path: Path, monkeypatch
):
    client = make_client(tmp_path, posting_parser_provider="heuristic")
    source_url = "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926"
    raw_text = (
        "Page Title: Blocked - Indeed.com\n"
        f"Source URL: {source_url}\n"
        "Source Evidence: Software Development Intern · Birdseye Solutions · Mississauga, ON, CA\n"
        "Request Blocked\n"
        "You have been blocked."
    )
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "gmail_alert", "raw_text": raw_text, "source_url": source_url},
    ).json()
    original_artifact_id = job["source"]["raw_content_artifact_id"]
    archived_html = b"<html><h1>Software Development Intern</h1><p>Python SQL APIs</p></html>"
    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "http://localhost:8001/ats/generic/birdseye/jobs/software-development-intern",
                    "raw_text": (
                        "Software Development Intern\n"
                        "Company: Birdseye Solutions\n"
                        "Location: Mississauga, ON, Canada\n"
                        "Build Python and SQL services for video monitoring workflows."
                    ),
                    "confidence": 0.95,
                    "evidence": "resolved from company career portal",
                    "source_archive": {
                        "source_url": source_url,
                        "final_url": "http://localhost:8001/ats/generic/birdseye/jobs/software-development-intern",
                        "status_code": 200,
                        "content_type": "text/html; charset=utf-8",
                        "body": archived_html,
                        "headers": {"etag": "posting-v2"},
                    },
                },
            }

    monkeypatch.setattr(
        api_main, "default_posting_resolver", lambda settings: FakePostingResolver()
    )

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["status"] == "succeeded"
    assert processed["result"]["status"] == "refreshed"
    assert processed["result"]["refreshed"] is True
    assert processed["result"]["ingestion_status"] == "imported"
    payload = client.get(f"/api/jobs/{job['id']}").json()
    assert resolved_hints[0].company == "Birdseye Solutions"
    assert resolved_hints[0].title == "Software Development Intern"
    assert payload["source"]["source_url"] == (
        "http://localhost:8001/ats/generic/birdseye/jobs/software-development-intern"
    )
    assert payload["source"]["raw_content_artifact_id"] != original_artifact_id
    assert payload["source"]["source_archive_artifact_id"].startswith("artifact_")
    assert payload["role"]["description_clean"].startswith("Software Development Intern")
    assert "Request Blocked" not in payload["role"]["description_clean"]
    assert payload["role"]["tech_stack"] == ["Python", "SQL"]
    assert payload["data_quality"]["missing_fields"] == []
    assert payload["data_quality"]["needs_manual_review"] is False
    download = client.get(payload["source"]["source_archive_download_url"])
    assert download.status_code == 200
    assert download.content == archived_html
    with session_scope(client.app.state.settings) as db:
        artifacts = list(db.execute(select(StoredArtifact)).scalars())
        assert len(artifacts) == 3
        refreshed = db.get(JobPosting, job["id"])
        assert refreshed.raw_content_artifact_id == payload["source"]["raw_content_artifact_id"]


def test_job_reprocess_auth_polluted_resolver_error_falls_back_to_snapshot(
    tmp_path: Path, monkeypatch
):
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
    )
    authwall_url = (
        "https://www.linkedin.com/authwall?sessionRedirect="
        "https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4425694183"
    )
    raw_text = (
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn\n"
        f"Source URL: {authwall_url}\n"
        "Software Developer Co-Op - Fall 2026\n"
        "TrendAI  Ottawa, Ontario, Canada"
    )

    with session_scope(client.app.state.settings) as db:
        job = api_main.persist_job_ingest(
            api_main.JobIngestPayload(
                source_type="url", raw_text=raw_text, source_url=authwall_url
            ),
            db,
            client.app.state.storage,
            client.app.state.settings,
        )
    with session_scope(client.app.state.settings) as db:
        job_row = db.get(JobPosting, job["id"])
        job_row.company_name = "Software"
        job_row.employer = {"company_name": "Software", "division": None, "industry": None}

    class FailingPostingResolver:
        def resolve(self, hint):
            raise RuntimeError("unit test resolver timeout")

    monkeypatch.setattr(
        api_main, "default_posting_resolver", lambda settings: FailingPostingResolver()
    )
    response = client.post(f"/api/jobs/{job['id']}/reprocess")
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    payload = client.get(f"/api/jobs/{job['id']}").json()

    assert response.status_code == 202
    assert processed["status"] == "succeeded"
    assert processed["result"] == {
        "status": "snapshot_reparsed",
        "refreshed": False,
        "ingestion_status": "needs_external_resolution",
        "resolved_url": "https://www.linkedin.com/jobs/view/4425694183",
        "error_message": "posting_resolver_failed",
        "source_resolution": {
            "official_discovery_status": "failed",
            "reason_code": "posting_resolver_failed",
            "official_discovery_error": "posting_resolver_failed",
        },
    }
    assert payload["employer"]["company_name"] == "TrendAI"
    assert payload["reprocess"]["status"] == "succeeded"


def test_job_reprocess_uses_configured_posting_resolver_provider(tmp_path: Path):
    from jobcockpit.ingestion.llm_providers import register_posting_resolver_provider

    source_url = "https://ca.indeed.com/viewjob?jk=provider-reprocess"
    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "http://localhost:8001/ats/generic/acme/jobs/platform-engineer",
                    "raw_text": (
                        "Platform Engineer\n"
                        "Company: Acme\n"
                        "Location: Remote Canada\n"
                        "Build Python APIs for developer platforms."
                    ),
                    "confidence": 0.95,
                    "evidence": "resolved from configured provider",
                },
            }

    register_posting_resolver_provider(
        "unit_test_reprocess_resolver",
        lambda settings: FakePostingResolver(),
        replace=True,
    )
    client = make_client(
        tmp_path,
        posting_parser_provider="heuristic",
        posting_resolver_provider="unit_test_reprocess_resolver",
        posting_search_provider="disabled",
    )
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Title: Platform Engineer\n"
                "Company: Acme\n"
                "Location: Remote Canada\n"
                "Page Title: Blocked - Indeed.com\n"
                f"Source URL: {source_url}\n"
                "Source Evidence: Platform Engineer - Acme - Remote Canada\n"
                "Request Blocked"
            ),
            "source_url": source_url,
        },
    ).json()

    response = client.post(f"/api/jobs/{job['id']}/reprocess")
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    payload = client.get(f"/api/jobs/{job['id']}").json()

    assert response.status_code == 202
    assert processed["status"] == "succeeded"
    assert processed["result"]["ingestion_status"] == "imported"
    assert resolved_hints[0].source_url == source_url
    assert resolved_hints[0].company == "Acme"
    assert resolved_hints[0].title == "Platform Engineer"
    assert (
        payload["source"]["source_url"]
        == "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    )
    assert payload["role"]["job_title"] == "Platform Engineer"


def test_job_reprocess_rejects_source_url_conflict_before_writing_artifacts(
    tmp_path: Path, monkeypatch
):
    client = make_client(tmp_path, posting_parser_provider="heuristic")
    resolved_url = "http://localhost:8001/ats/generic/birdseye/jobs/software-development-intern"
    existing = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": (
                "Customer Success Associate\n"
                "Company: Different Company\n"
                "Location: Remote Canada\n"
                "Support customer onboarding."
            ),
            "source_url": resolved_url,
        },
    )
    assert existing.status_code == 201
    source_url = "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Page Title: Blocked - Indeed.com\n"
                f"Source URL: {source_url}\n"
                "Source Evidence: Software Development Intern · Birdseye Solutions · Mississauga, ON, CA\n"
                "Request Blocked\n"
                "You have been blocked."
            ),
            "source_url": source_url,
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        artifact_count = len(list(db.execute(select(StoredArtifact)).scalars()))

    class FakePostingResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": resolved_url,
                    "raw_text": (
                        "Software Development Intern\n"
                        "Company: Birdseye Solutions\n"
                        "Location: Mississauga, ON, Canada\n"
                        "Build Python and SQL services."
                    ),
                    "confidence": 0.95,
                    "evidence": "resolved from company career portal",
                },
            }

    monkeypatch.setattr(
        api_main, "default_posting_resolver", lambda settings: FakePostingResolver()
    )

    response = client.post(f"/api/jobs/{job['id']}/reprocess")

    assert response.status_code == 202
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed["status"] == "failed"
    assert processed["error_code"] == "job_reprocess_rejected"
    assert processed["error_message"] == "Job reprocessing was rejected"
    with session_scope(client.app.state.settings) as db:
        artifacts = list(db.execute(select(StoredArtifact)).scalars())
        unchanged = db.get(JobPosting, job["id"])
        assert len(artifacts) == artifact_count
        assert unchanged.source_url == source_url


def test_url_job_ingest_endpoint_fetches_source_archive_before_persisting(
    tmp_path: Path, monkeypatch
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    source_url = "http://localhost:8001/careers/platform-engineer"
    archived_html = b"<html><h1>Platform Engineer</h1><p>Company: Acme</p></html>"

    class FakeApplyPageResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, apply_url, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nPython APIs",
                source_archive=SourceArchive(
                    source_url=source_url,
                    final_url=source_url,
                    status_code=200,
                    content_type="text/html; charset=utf-8",
                    body=archived_html,
                    headers={},
                ),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_links.AtsApplyPageResolver", FakeApplyPageResolver
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "url",
            "raw_text": "Email teaser should not be used as the archive",
            "source_url": source_url,
        },
    )

    assert response.status_code == 201
    job = response.json()
    archive_url = job["source"]["source_archive_download_url"]
    assert job["employer"]["company_name"] == "Acme"
    assert job["source"]["source_archive"]["source_url"] == source_url

    download = client.get(archive_url)

    assert download.status_code == 200
    assert download.content == archived_html


def test_extension_page_capture_creates_job_with_private_source_archive(tmp_path: Path):
    client = make_client(tmp_path)
    source_url = "https://www.linkedin.com/jobs/view/4425912315"

    response = client.capture_job(
        {
            "source_url": source_url,
            "page_title": "Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn",
            "visible_text": "Intern, Digital Ship NLP (8 Months)\nCompany: Seaspan ULC\nLocation: North Vancouver, BC",
            "html": "<html><body><h1>Intern, Digital Ship NLP (8 Months)</h1><p>Company: Seaspan ULC</p></body></html>",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["source"]["source_type"] == "browser_capture"
    assert job["source"]["source_url"] == source_url
    assert job["source"]["source_resolution"] == {
        "kind": "browser_capture",
        "provider": "user_browser_capture",
        "status": "captured",
        "reason_code": "user_submitted_page_capture",
    }
    assert job["source"]["source_archive_artifact_id"].startswith("artifact_")
    assert job["source"]["source_archive"]["content_type"] == "text/html"
    assert job["source"]["source_archive"]["status_code"] == 0

    archive_response = client.get(job["source"]["source_archive_download_url"])
    assert archive_response.status_code == 200
    assert b"Company: Seaspan ULC" in archive_response.content


def test_extension_page_capture_queues_enabled_fit_analysis_instead_of_running_inline(
    tmp_path: Path,
):
    calls: list[str] = []

    class FitAnalyzerThatMustRunInWorker:
        def analyze(self, *args, **kwargs):
            calls.append("analyze")
            raise AssertionError("fit analysis must not run in the capture request")

    from jobcockpit.ingestion.llm_providers import register_fit_analyzer_provider

    register_fit_analyzer_provider(
        "unit_test_deferred_fit",
        lambda _settings: FitAnalyzerThatMustRunInWorker(),
        replace=True,
    )
    client = make_client(tmp_path, fit_analysis_provider="unit_test_deferred_fit")

    response = client.capture_job(
        {
            "source_url": "https://jobs.example.com/postings/backend-intern",
            "page_title": "Backend Intern | Acme",
            "visible_text": "Backend Intern\nCompany: Acme\nPython APIs",
        },
    )

    assert response.status_code == 200
    assert calls == []
    assert response.json()["reprocess"]["type"] == "fit_analysis"
    assert response.json()["reprocess"]["status"] == "queued"
    with session_scope(client.app.state.settings) as db:
        runs = list(db.execute(select(JobReprocessRun)).scalars())
        assert len(runs) == 1
        assert runs[0].run_type == "fit_analysis"


def test_extension_page_capture_extracts_commencing_term_timing(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.capture_job(
        {
            "source_url": "https://ca.indeed.com/viewjob?jk=e228efbdd8873209",
            "page_title": "Student WestJetter- Data Science- 12 month term",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet\n"
                "Location: Calgary AB\n"
                "Posting Opening Date: June 23, 2026\n"
                "Posting Close Date: July 7, 2026\n"
                "Note: This is a 12-month term commencing on August 31, 2026\n"
                "Python SQL machine learning"
            ),
            "html": "<html><body><p>Note: This is a 12-month term commencing on August 31, 2026</p></body></html>",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["duration_months"] == 12
    assert job["employment_timing"]["raw_label"] == "12-month term commencing on August 31, 2026"
    assert job["employment_timing"]["timing_type"] == "fixed_term"
    assert job["employment_timing"]["start_date"] == "2026-08-31"
    assert job["employment_timing"]["end_date"] == "2027-07-31"
    assert job["employment_timing"]["duration_months"] == 12
    assert job["application"]["application_deadline"] == "2026-07-07"


def test_extension_page_capture_accepts_public_url_without_server_fetch_policy(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.capture_job(
        {
            "source_url": "https://ca.indeed.com/viewjob?jk=abc123",
            "page_title": "Backend Intern - Indeed",
            "visible_text": "Backend Intern\nCompany: Acme\nPython APIs",
        },
    )

    assert response.status_code == 200
    assert response.json()["source"]["source_url"] == "https://ca.indeed.com/viewjob?jk=abc123"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_url": "file:///C:/private/posting.html",
            "page_title": "Private file",
            "visible_text": "Backend Intern\nCompany: Acme",
        },
        {
            "source_url": "https://example.com/jobs/1",
            "page_title": "Empty",
            "visible_text": "   ",
        },
        {
            "source_url": "https://example.com/jobs/1",
            "page_title": "Too large",
            "visible_text": "x" * 200_001,
        },
    ],
)
def test_extension_page_capture_rejects_invalid_or_oversized_payloads(
    tmp_path: Path, payload: dict
):
    client = make_client(tmp_path)

    response = client.post("/api/extension/page-captures", json=payload)

    assert response.status_code == 422


def test_extension_page_capture_filters_required_language_before_persisting(tmp_path: Path):
    client = make_client(tmp_path)
    with session_scope(client.app.state.settings) as db:
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
    payload = {
        "source_url": "https://jobs.example.com/postings/backend-intern",
        "page_title": "Backend Intern | Acme",
        "visible_text": "Backend Intern\nCompany: Acme\nFrench required for this role.",
    }

    processed = client.process_capture(payload)

    assert processed["status"] == "skipped"
    assert processed["result"]["status"] == "skipped_by_language_filter"
    assert processed["result"]["reason"] == "unmet_required_language"
    with session_scope(client.app.state.settings) as db:
        assert db.query(JobPosting).count() == 0

    skipped_response = client.capture_job({**payload, "skip_language_filter": True})

    assert skipped_response.status_code == 200
    with session_scope(client.app.state.settings) as db:
        assert db.query(JobPosting).count() == 1


def test_extension_page_capture_updates_target_job(tmp_path: Path):
    client = make_client(tmp_path)
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Old Backend Intern\nCompany: Acme",
            "source_url": None,
        },
    ).json()

    response = client.capture_job(
        {
            "source_url": "https://jobs.example.com/postings/backend-intern",
            "page_title": "Backend Intern | Acme",
            "visible_text": "Backend Intern\nCompany: Acme\nLocation: Toronto, ON, Canada\nPython APIs",
            "target_job_id": original["id"],
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["id"] == original["id"]
    assert updated["employer"]["company_name"] == "Acme"
    assert updated["source"]["source_type"] == "browser_capture"
    with session_scope(client.app.state.settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1


def test_extension_page_capture_updates_existing_job_matching_application_apply_url(tmp_path: Path):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet\n"
                "Apply URL: https://www.linkedin.com/jobs/view/4430892685/"
            ),
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet Airlines\n"
                "Location: Calgary, Alberta, Canada\n"
                "Python SQL"
            ),
            "html": "<html><body><h1>Student WestJetter- Data Science- 12 month term</h1></body></html>",
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["id"] == original["id"]
    assert updated["source"]["source_type"] == "browser_capture"
    assert updated["source"]["source_url"] == indeed_url
    assert updated["source"]["source_archive"]["content_type"] == "text/html"
    with session_scope(client.app.state.settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1


def test_extension_page_capture_prefers_apply_url_match_when_duplicate_source_url_exists(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet Airlines\n"
                "Location: Calgary, Alberta, Canada\n"
                "Python SQL"
            ),
            "html": "<html><body><h1>Student WestJetter- Data Science- 12 month term</h1></body></html>",
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["id"] == original["id"]
    assert updated["source"]["source_type"] == "browser_capture"
    assert updated["source"]["source_url"] == indeed_url
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        assert len(list(db.execute(select(JobPosting)).scalars())) == 2


def test_extension_page_capture_retires_tied_apply_url_match(tmp_path: Path):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": "https://www.linkedin.com/jobs/view/4430892686/",
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        for job_id in (original["id"], duplicate["id"]):
            job = db.get(JobPosting, job_id)
            job.application = {
                **job.application,
                "external_apply_url": indeed_url,
            }

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet Airlines\n"
                "Location: Calgary, Alberta, Canada\n"
                "Python SQL"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    repeat_response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL Updated",
        },
    )

    assert repeat_response.status_code == 200
    assert repeat_response.json()["id"] == original["id"]
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        assert len(list(db.execute(select(JobPosting)).scalars())) == 2


def test_extension_page_capture_relinks_job_alert_duplicate_before_removing_it(tmp_path: Path):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }
        db.add(
            JobAlertSource(
                job_alert_source_id="source_extension_duplicate",
                candidate_id="candidate_local",
                display_name="Indeed alerts",
                gmail_query="from:alerts@example.invalid",
            )
        )
        db.flush()
        db.add(
            EmailIngestionRun(
                email_ingestion_run_id="emailrun_extension_duplicate",
                job_alert_source_id="source_extension_duplicate",
                provider="gmail",
                query_used="from:alerts@example.invalid",
                status="completed",
            )
        )
        db.flush()
        db.add(
            JobAlertEmail(
                job_alert_email_id="email_extension_duplicate",
                email_ingestion_run_id="emailrun_extension_duplicate",
                provider_message_id_hash="message_hash_extension_duplicate",
                provider_thread_id_hash="provider_thread_hash_extension_duplicate",
                subject="WestJet job alert",
                sender_email_hash="sender_hash_extension_duplicate",
                sender_domain="example.com",
                received_at="2026-06-23T00:00:00+00:00",
                body_content_hash="body_hash_extension_duplicate",
            )
        )
        db.flush()
        db.add(
            JobAlertLink(
                job_alert_link_id="link_extension_duplicate",
                job_alert_email_id="email_extension_duplicate",
                original_url=indeed_url,
                resolved_url=indeed_url,
                canonical_url=indeed_url,
                link_text="Student WestJetter- Data Science",
                classification="requires_external_resolution",
                confidence=90,
                ingestion_status="fallback_imported",
                linked_job_id=duplicate["id"],
            )
        )

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet Airlines\n"
                "Location: Calgary, Alberta, Canada\n"
                "Python SQL"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        link = db.get(JobAlertLink, "link_extension_duplicate")
        assert link.linked_job_id == original["id"]


def test_extension_page_capture_rejects_merge_with_conflicting_applications(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    client.post(f"/api/jobs/{original['id']}/applications")
    client.post(f"/api/jobs/{duplicate['id']}/applications")
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    processed = client.process_capture(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": (
                "Student WestJetter- Data Science- 12 month term\n"
                "Company: WestJet Airlines\n"
                "Location: Calgary, Alberta, Canada\n"
                "Python SQL"
            ),
        },
    )

    assert processed["status"] == "failed"
    assert processed["error_code"] == "capture_merge_conflict"
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row is not None
        assert not duplicate_row.source.get("merged_into_job_id")

    listed_jobs = client.get("/api/jobs").json()
    listed_ids = {job["id"] for job in listed_jobs}
    assert original["id"] in listed_ids
    assert duplicate["id"] in listed_ids
    with session_scope(client.app.state.settings) as db:
        active_ids = {
            row.id for row in db.execute(text("SELECT id FROM active_job_postings")).all()
        }
    assert original["id"] in active_ids
    assert duplicate["id"] in active_ids


def test_jobs_list_reads_active_job_postings_view(tmp_path: Path):
    client = make_client(tmp_path)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Intern\nCompany: Acme",
            "source_url": None,
        },
    ).json()
    with session_scope(client.app.state.settings) as db:
        active_ids = {
            row.id for row in db.execute(text("SELECT id FROM active_job_postings")).all()
        }
        assert job["id"] in active_ids
        db.execute(text("DROP VIEW active_job_postings"))
        db.execute(text("CREATE VIEW active_job_postings AS SELECT * FROM job_postings WHERE 0"))

    assert client.get("/api/jobs").json() == []


def create_merged_duplicate_pair(client: TestClient) -> tuple[dict, dict, str]:
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    client.post(f"/api/jobs/{duplicate['id']}/applications")
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL",
        },
    )
    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row is not None
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
    return original, duplicate, indeed_url


def duplicate_application_id(client: TestClient, duplicate_job_id: str) -> str:
    with session_scope(client.app.state.settings) as db:
        application = db.execute(
            select(Application).where(Application.job_id == duplicate_job_id)
        ).scalar_one()
        return application.application_id


def create_merged_duplicate_pair_with_duplicate_package(
    client: TestClient,
) -> tuple[dict, dict, dict]:
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    client.post(f"/api/jobs/{duplicate['id']}/applications")
    duplicate_application = duplicate_application_id(client, duplicate["id"])
    duplicate_package = finish_generation(client, client.post(f"/api/applications/{duplicate_application}/packages")).json()
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL",
        },
    )
    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    return original, duplicate, duplicate_package


def test_extension_page_capture_merges_duplicate_with_same_snapshot_hash(tmp_path: Path):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    capture_payload = {
        "source_url": indeed_url,
        "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
        "visible_text": (
            "Student WestJetter- Data Science- 12 month term\n"
            "Company: WestJet Airlines\n"
            "Location: Calgary, Alberta, Canada\n"
            "Python SQL"
        ),
    }
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.capture_job(capture_payload).json()
    assert duplicate["id"] != original["id"]
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    response = client.capture_job(capture_payload)

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        assert len(list(db.execute(select(JobPosting)).scalars())) == 2


def test_extension_page_capture_retains_duplicate_status_check_history(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=retained-history"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme Systems",
            "source_url": "https://www.linkedin.com/jobs/view/4430892686/",
        },
    ).json()
    checked_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(client.app.state.settings) as db:
        for job_id in (original["id"], duplicate["id"]):
            job = db.get(JobPosting, job_id)
            job.application = {
                **job.application,
                "external_apply_url": indeed_url,
            }
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_merge_history",
                status="completed",
                started_at=checked_at,
                finished_at=checked_at,
                request_payload={"job_id": duplicate["id"]},
            )
        )
        db.flush()
        db.add(
            JobStatusCheck(
                status_check_id="status_check_merge_history",
                status_scan_run_id="status_scan_merge_history",
                job_id=duplicate["id"],
                source_url=duplicate["source"]["source_url"],
                source_kind="linkedin",
                result="open",
                detected_status=None,
                evidence_code="posting_identity_match",
                http_status=200,
                retryable=False,
                status_before="active",
                status_after="active",
                checked_at=checked_at,
            )
        )
        duplicate_job = db.get(JobPosting, duplicate["id"])
        duplicate_job.status_last_open_at = checked_at
        duplicate_job.status_last_open_evidence_code = "posting_identity_match"

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Platform Engineer | Acme",
            "visible_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        history = db.get(JobStatusCheck, "status_check_merge_history")
        assert duplicate_row is not None
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        assert duplicate_row.status_next_check_at is None
        assert history.job_id == original["id"]
        assert history.source_url == duplicate["source"]["source_url"]
        assert history.result == "open"
        assert history.evidence_code == "posting_identity_match"
        assert history.checked_at.replace(tzinfo=UTC) == checked_at
        canonical_row = db.get(JobPosting, original["id"])
        assert canonical_row.status_last_open_at.replace(tzinfo=UTC) == checked_at
        assert canonical_row.status_last_open_evidence_code == "posting_identity_match"


def test_authoritative_merge_does_not_transfer_repair_from_a_different_source(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    shared_apply_url = "https://ca.indeed.com/viewjob?jk=shared-merge-source"
    canonical = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Platform Engineer\nCompany: Acme Systems",
            "source_url": "https://www.linkedin.com/jobs/view/4430892686/",
        },
    ).json()
    checked_at = datetime(2026, 8, 24, 12, tzinfo=UTC)

    with session_scope(client.app.state.settings) as db:
        for job_id in (canonical["id"], duplicate["id"]):
            job = db.get(JobPosting, job_id)
            job.application = {**job.application, "external_apply_url": shared_apply_url}
        duplicate_job = db.get(JobPosting, duplicate["id"])
        duplicate_job.status_source_repair_required = True
        duplicate_job.status_next_check_at = None
        mismatch_sources = (
            duplicate["source"]["source_url"],
            duplicate["source"]["source_url"],
            duplicate["source"]["source_url"],
            shared_apply_url,
        )
        for offset, mismatch_source in enumerate(mismatch_sources):
            run_id = f"status_scan_distinct_repair_source_{offset}"
            observation_at = checked_at + timedelta(days=offset)
            db.add(
                JobStatusScanRun(
                    status_scan_run_id=run_id,
                    status="completed",
                    started_at=observation_at,
                    finished_at=observation_at,
                    request_payload={"job_id": duplicate["id"]},
                )
            )
            db.flush()
            db.add(
                JobStatusCheck(
                    status_check_id=f"status_check_distinct_repair_source_{offset}",
                    status_scan_run_id=run_id,
                    job_id=duplicate["id"],
                    source_url=mismatch_source,
                    source_kind="linkedin" if offset < 3 else "indeed",
                    result="inconclusive",
                    detected_status=None,
                    evidence_code="redirect_identity_mismatch",
                    http_status=200,
                    retryable=True,
                    status_before="active",
                    status_after="active",
                    checked_at=observation_at,
                )
            )
        db.flush()

        apply_authoritative_job_merge(
            JobMergePlan(
                canonical_job_id=canonical["id"],
                duplicate_job_ids=(duplicate["id"],),
                provider="indeed",
                identity_hash="shared-merge-source",
            ),
            db,
        )
        canonical_job = db.get(JobPosting, canonical["id"])
        assert canonical_job.status_source_repair_required is False


def test_extension_page_capture_cancels_duplicate_reprocess_runs_when_marking_merged(
    tmp_path: Path, monkeypatch
):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    duplicate_run_id = "reprocess_duplicate_queued"
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }
        db.add(
            JobReprocessRun(
                reprocess_run_id=duplicate_run_id, job_id=duplicate["id"], status="queued"
            )
        )

    def fail_if_reprocesses_hidden_job(*_args, **_kwargs):
        raise AssertionError("merged duplicate reprocess run should not be processed")

    monkeypatch.setattr(
        api_main, "prepare_reprocess_source", fail_if_reprocesses_hidden_job
    )
    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL",
        },
    )

    assert response.status_code == 200
    processed = api_main.process_next_job_reprocess(
        client.app.state.settings, client.app.state.storage
    )
    assert processed is None
    with session_scope(client.app.state.settings) as db:
        run = db.get(JobReprocessRun, duplicate_run_id)
        assert run.status == "cancelled"
        assert run.error_message == "job_merged_into_another_posting"


@pytest.mark.parametrize("request_status", ["queued", "waiting_for_profile"])
def test_extension_page_capture_cancels_duplicate_cover_letter_request_when_marking_merged(
    tmp_path: Path,
    request_status: str,
):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student Data Science term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student Data Science term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    run_id = "cover_letter_duplicate_queued"
    with session_scope(client.app.state.settings) as db:
        canonical = db.get(JobPosting, original["id"])
        canonical.application = {
            **canonical.application,
            "external_apply_url": indeed_url,
        }
        duplicate_row = db.get(JobPosting, duplicate["id"])
        db.add(
            CoverLetterGenerationRun(
                generation_run_id=run_id,
                job_id=duplicate_row.id,
                posting_snapshot_hash=duplicate_row.posting_snapshot_hash,
                status=request_status,
            )
        )

    response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student Data Science term - WestJet Airlines",
            "visible_text": "Student Data Science term\nCompany: WestJet Airlines\nPython SQL",
        },
    )

    assert response.status_code == 200
    with session_scope(client.app.state.settings) as db:
        run = db.get(CoverLetterGenerationRun, run_id)
        assert run.status == "cancelled"
        assert run.error_message == "job_merged_into_another_posting"
        assert run.result["merged_into_job_id"] == original["id"]


def test_extension_page_capture_stale_target_job_updates_canonical_without_reactivating_duplicate(
    tmp_path: Path,
):
    client = make_client(tmp_path)
    original, duplicate, indeed_url = create_merged_duplicate_pair(client)

    response = client.capture_job(
        {
            "target_job_id": duplicate["id"],
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL Updated",
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == original["id"]
    assert response.json()["source"]["source_type"] == "browser_capture"
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]
        assert duplicate_row.source_url is None
        active_ids = {
            row.id for row in db.execute(text("SELECT id FROM active_job_postings")).all()
        }
        assert original["id"] in active_ids
        assert duplicate["id"] not in active_ids


def test_merged_duplicate_rejects_stale_job_read(tmp_path: Path):
    client = make_client(tmp_path)
    _original, duplicate, _indeed_url = create_merged_duplicate_pair(client)

    response = client.get(f"/api/jobs/{duplicate['id']}")

    assert response.status_code == 409
    assert response.json()["detail"] == "Job was merged into another posting"


@pytest.mark.parametrize("endpoint", ["reprocess", "fit-analysis"])
def test_merged_duplicate_rejects_stale_reprocess_endpoints(tmp_path: Path, endpoint: str):
    client = make_client(tmp_path)
    _original, duplicate, _indeed_url = create_merged_duplicate_pair(client)

    response = client.post(f"/api/jobs/{duplicate['id']}/{endpoint}")

    assert response.status_code == 409
    assert response.json()["detail"] == "Job was merged into another posting"
    with session_scope(client.app.state.settings) as db:
        active_runs = list(
            db.execute(
                select(JobReprocessRun).where(
                    JobReprocessRun.job_id == duplicate["id"],
                    JobReprocessRun.status.in_(["queued", "running"]),
                )
            ).scalars()
        )
        assert active_runs == []


def test_merged_duplicate_rejects_stale_application_creation(tmp_path: Path):
    client = make_client(tmp_path)
    _original, duplicate, _indeed_url = create_merged_duplicate_pair(client)

    response = client.post(f"/api/jobs/{duplicate['id']}/applications")

    assert response.status_code == 409
    assert response.json()["detail"] == "Job was merged into another posting"


def test_merged_duplicate_hides_stale_application_from_application_list(tmp_path: Path):
    client = make_client(tmp_path)
    original, duplicate, _indeed_url = create_merged_duplicate_pair(client)
    moved_application_id = duplicate_application_id(client, original["id"])

    response = client.get("/api/applications")

    assert response.status_code == 200
    listed_applications = response.json()
    assert {application["job_id"] for application in listed_applications} == {original["id"]}
    assert moved_application_id in {
        application["application_id"] for application in listed_applications
    }


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/applications/{application_id}", None),
        ("post", "/api/applications/{application_id}/decision", {"decision": "would_apply"}),
        ("post", "/api/applications/{application_id}/packages", None),
    ],
)
def test_merged_duplicate_rejects_stale_application_endpoints(
    tmp_path: Path,
    method: str,
    path: str,
    payload: dict | None,
):
    client = make_client(tmp_path)
    original, _duplicate, _indeed_url = create_merged_duplicate_pair(client)
    moved_application_id = duplicate_application_id(client, original["id"])

    url = path.format(application_id=moved_application_id)
    response = (
        getattr(client, method)(url, json=payload)
        if payload is not None
        else getattr(client, method)(url)
    )

    assert response.status_code in {200, 201}


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/application-packages/{package_id}", None),
        ("post", "/api/application-packages/{package_id}/resume-draft", None),
        ("post", "/api/application-packages/{package_id}/cover-letter-draft", {}),
        ("post", "/api/application-packages/{package_id}/truthfulness-check", None),
        ("get", "/api/application-packages/{package_id}/documents", None),
        (
            "post",
            "/api/extension/packages/{package_id}/session",
            {"document_ids": ["doc_stale"], "expires_in_seconds": 60},
        ),
    ],
)
def test_merged_duplicate_rejects_stale_package_endpoints(
    tmp_path: Path,
    method: str,
    path: str,
    payload: dict | None,
):
    client = make_client(tmp_path)
    _original, _duplicate, duplicate_package = create_merged_duplicate_pair_with_duplicate_package(
        client
    )

    url = path.format(package_id=duplicate_package["application_package_id"])
    response = (
        getattr(client, method)(url, json=payload)
        if payload is not None
        else getattr(client, method)(url)
    )

    assert not (
        response.status_code == 409
        and response.json().get("detail") == "Job was merged into another posting"
    )


def test_merged_duplicate_rejects_previously_approved_extension_document_download(tmp_path: Path):
    client = make_client(tmp_path)
    indeed_url = "https://ca.indeed.com/viewjob?jk=e228efbdd8873209"
    original = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet",
            "source_url": "https://www.linkedin.com/jobs/view/4430892685/",
        },
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines",
            "source_url": indeed_url,
        },
    ).json()
    client.post(f"/api/jobs/{duplicate['id']}/applications")
    duplicate_application = duplicate_application_id(client, duplicate["id"])
    package = finish_generation(client, client.post(f"/api/applications/{duplicate_application}/packages")).json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    check_response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )
    assert check_response.status_code == 200
    session_response = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [document["document_id"]], "expires_in_seconds": 60},
    )
    assert session_response.status_code == 200
    download_url = session_response.json()["documents"][0]["download_url"]
    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, original["id"])
        job.application = {
            **job.application,
            "external_apply_url": indeed_url,
        }

    merge_response = client.capture_job(
        {
            "source_url": indeed_url,
            "page_title": "Student WestJetter- Data Science- 12 month term - WestJet Airlines",
            "visible_text": "Student WestJetter- Data Science- 12 month term\nCompany: WestJet Airlines\nPython SQL",
        },
    )
    assert merge_response.status_code == 200
    assert merge_response.json()["id"] == original["id"]

    response = client.get(download_url)

    assert response.status_code == 200


def test_process_job_reprocess_run_cancels_stale_merged_duplicate_run(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    original, duplicate, _indeed_url = create_merged_duplicate_pair(client)
    run_id = "reprocess_stale_merged_duplicate"
    with session_scope(client.app.state.settings) as db:
        db.add(JobReprocessRun(reprocess_run_id=run_id, job_id=duplicate["id"], status="queued"))

    def fail_if_reprocesses_hidden_job(*_args, **_kwargs):
        raise AssertionError("merged duplicate reprocess run should not be processed")

    monkeypatch.setattr(
        api_main, "prepare_reprocess_source", fail_if_reprocesses_hidden_job
    )
    processed = api_main.process_job_reprocess_run(
        run_id, client.app.state.settings, client.app.state.storage
    )

    assert processed["status"] == "cancelled"
    assert processed["error_message"] == "job_merged_into_another_posting"
    assert processed["result"] == {"merged_into_job_id": original["id"]}
    with session_scope(client.app.state.settings) as db:
        duplicate_row = db.get(JobPosting, duplicate["id"])
        assert duplicate_row.source["merged_into_job_id"] == original["id"]


def test_extension_page_capture_duplicate_source_url_returns_existing_job(tmp_path: Path):
    client = make_client(tmp_path)
    payload = {
        "source_url": "https://www.linkedin.com/jobs/view/4425912315",
        "page_title": "Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn",
        "visible_text": "Intern, Digital Ship NLP (8 Months)\nCompany: Seaspan ULC\nLocation: North Vancouver, BC",
    }

    first = client.capture_job(payload)
    second = client.capture_job(
        {**payload, "visible_text": payload["visible_text"] + "\nUpdated"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    with session_scope(client.app.state.settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1


def test_generated_resume_passes_truthfulness_when_claims_are_grounded(tmp_path: Path):
    client = make_client(tmp_path)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    client.post(f"/api/application-packages/{package['application_package_id']}/resume-draft")

    response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "passed"
    assert result["blocking_errors"] == []


def test_generated_documents_are_attached_to_package_manifest(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()

    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    listed_documents = client.get(
        f"/api/application-packages/{package['application_package_id']}/documents"
    ).json()

    assert len(listed_documents) == 1
    assert listed_documents == [
        {
            **document,
            "application_package_document_id": listed_documents[0][
                "application_package_document_id"
            ],
            "approved_for_upload": False,
            "required": True,
        }
    ]
    with session_scope(settings) as db:
        package_document = db.execute(select(ApplicationPackageDocument)).scalar_one()
        assert package_document.application_package_id == package["application_package_id"]
        assert package_document.generated_document_id == document["document_id"]
        assert package_document.artifact_id == document["artifact_id"]
        assert package_document.document_type == "resume"
        assert package_document.required is True
        assert package_document.approved_for_upload is False


def test_extension_session_requires_package_document_manifest(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    check_response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )
    assert check_response.status_code == 200
    assert check_response.json()["status"] == "passed"
    with session_scope(settings) as db:
        package_documents = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id
                == package["application_package_id"]
            )
        ).scalars()
        for package_document in package_documents:
            db.delete(package_document)

    session_response = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [document["document_id"]], "expires_in_seconds": 60},
    )

    assert session_response.status_code == 404
    assert session_response.json()["detail"] == "Package document not found"
    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        document_row = db.get(GeneratedDocument, document["document_id"])
        assert package_row.approved_for_extension is False
        assert document_row.approved_by_user is False


def test_truthfulness_check_flags_unsupported_claims(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    with session_scope(settings) as db:
        document_row = db.get(GeneratedDocument, document["document_id"])
        document_row.content_preview = "Experienced with Kubernetes production operations."

    response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "blocked"
    assert "unsupported_claim" in result["blocking_errors"]


def test_extension_download_requires_approved_package_document_and_scoped_token(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()

    default_token_response = client.get(
        f"/api/extension/documents/{document['document_id']}/download",
        params={"token": "test-secret"},
    )
    assert default_token_response.status_code == 403

    unchecked_approval = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [document["document_id"]], "expires_in_seconds": 60},
    )
    assert unchecked_approval.status_code == 403

    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        document_row = db.get(GeneratedDocument, document["document_id"])
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id
                == package["application_package_id"],
                ApplicationPackageDocument.generated_document_id == document["document_id"],
            )
        ).scalar_one()
        package_row.approved_for_extension = True
        document_row.approved_by_user = True
        package_document.approved_for_upload = True

    unaudited_approval = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [document["document_id"]], "expires_in_seconds": 60},
    )
    assert unaudited_approval.status_code == 403

    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        document_row = db.get(GeneratedDocument, document["document_id"])
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id
                == package["application_package_id"],
                ApplicationPackageDocument.generated_document_id == document["document_id"],
            )
        ).scalar_one()
        package_row.approved_for_extension = False
        package_row.approved_at = None
        document_row.approved_by_user = False
        package_document.approved_for_upload = False

    check_response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )
    assert check_response.status_code == 200
    assert check_response.json()["status"] == "passed"

    session_response = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [document["document_id"]], "expires_in_seconds": 60},
    )
    assert session_response.status_code == 200
    session_payload = session_response.json()
    assert session_payload["package_id"] == package["application_package_id"]
    token = session_payload["documents"][0]["token"]
    assert session_payload["documents"][0]["document_id"] == document["document_id"]
    assert session_payload["documents"][0]["download_url"].startswith(
        f"/api/extension/documents/{document['document_id']}/download?token="
    )

    approved_response = client.get(
        f"/api/extension/documents/{document['document_id']}/download",
        params={"token": token},
    )
    assert approved_response.status_code == 200
    assert b"Synthetic Candidate" in approved_response.content
    assert approved_response.headers["content-disposition"].startswith(
        "attachment; filename*=UTF-8''"
    )

    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        document_row = db.get(GeneratedDocument, document["document_id"])
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id
                == package["application_package_id"],
                ApplicationPackageDocument.generated_document_id == document["document_id"],
            )
        ).scalar_one()
        assert package_row.approved_for_extension is True
        assert document_row.approved_by_user is True
        assert package_document.approved_for_upload is True
        package_document.approved_for_upload = False

    unapproved_response = client.get(
        f"/api/extension/documents/{document['document_id']}/download",
        params={"token": token},
    )
    assert unapproved_response.status_code == 403
    assert unapproved_response.json()["detail"] == "Document is not approved for extension download"


def test_extension_session_rejects_explicit_empty_document_list(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    check_response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )
    assert check_response.status_code == 200
    assert check_response.json()["status"] == "passed"

    session_response = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"document_ids": [], "expires_in_seconds": 60},
    )

    assert session_response.status_code == 400
    assert session_response.json()["detail"] == "document_ids cannot be empty"
    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        document_row = db.get(GeneratedDocument, document["document_id"])
        assert package_row.approved_for_extension is False
        assert document_row.approved_by_user is False


def test_extension_session_rejects_omitted_document_list_without_approving_documents(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    client.post("/api/profile", json={"first_name": "Jane", "last_name": "Candidate", "email": "jane@example.test"})
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    resume = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()
    cover_letter = finish_generation(client, client.post(
        f"/api/application-packages/{package['application_package_id']}/cover-letter-draft"
    )).json()
    check_response = client.post(
        f"/api/application-packages/{package['application_package_id']}/truthfulness-check"
    )
    assert check_response.status_code == 200
    assert check_response.json()["status"] == "passed"

    session_response = client.post(
        f"/api/extension/packages/{package['application_package_id']}/session",
        json={"expires_in_seconds": 60},
    )

    assert session_response.status_code == 400
    assert session_response.json()["detail"] == "document_ids cannot be empty"
    with session_scope(settings) as db:
        package_row = db.get(ApplicationPackage, package["application_package_id"])
        resume_row = db.get(GeneratedDocument, resume["document_id"])
        cover_letter_row = db.get(GeneratedDocument, cover_letter["document_id"])
        assert package_row.approved_for_extension is False
        assert resume_row.approved_by_user is False
        assert cover_letter_row.approved_by_user is False


def test_duplicate_job_ingestion_does_not_create_orphan_artifacts(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    payload = {
        "source_type": "paste",
        "raw_text": "Backend Developer\nPython APIs",
        "source_url": None,
    }

    first = client.post("/api/jobs/ingest", json=payload)
    second = client.post("/api/jobs/ingest", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201

    with session_scope(settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1
        artifacts = list(db.execute(select(StoredArtifact)).scalars())
        assert len(artifacts) == 1
        assert artifacts[0].artifact_id == first.json()["source"]["raw_content_artifact_id"]


def test_duplicate_job_ingestion_skips_configured_parser(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_parser_provider="codex_cli",
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    payload = {
        "source_type": "paste",
        "raw_text": "Backend Developer\nPython APIs",
        "source_url": None,
    }
    parse_calls = []

    def fake_parse_job_text(
        raw_text,
        source_url,
        parser_settings,
        *,
        source_type=None,
        title_hint=None,
    ):
        parse_calls.append(
            (raw_text, source_url, parser_settings.posting_parser_provider, source_type)
        )
        return parse_job_text(raw_text, source_url)

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", fake_parse_job_text)

    first = client.post("/api/jobs/ingest", json=payload)
    second = client.post("/api/jobs/ingest", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert parse_calls == [("Backend Developer\nPython APIs", None, "codex_cli", "paste")]


def test_url_job_ingestion_deduplicates_same_source_url_with_changed_text(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    source_url = "http://localhost:8001/ats/generic/acme/jobs/123"
    first_payload = {
        "source_type": "url",
        "raw_text": "Backend Developer\nCompany: Acme\nPython APIs",
        "source_url": source_url,
    }
    changed_payload = {
        "source_type": "url",
        "raw_text": "Backend Developer\nCompany: Acme\nPython APIs\nUpdated posting details",
        "source_url": source_url,
    }

    first = client.post("/api/jobs/ingest", json=first_payload)
    second = client.post("/api/jobs/ingest", json=changed_payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    with session_scope(settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1
        assert len(list(db.execute(select(StoredArtifact)).scalars())) == 1


def test_duplicate_job_ingestion_unique_race_returns_existing_job_and_removes_artifact(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    app = create_app(settings)
    payload = {
        "source_type": "paste",
        "raw_text": "Backend Developer\nPython APIs",
        "source_url": None,
    }
    request_model = JobIngestRequest(**payload)
    snapshot_hash = job_posting_snapshot_hash(request_model)
    parsed = parse_job_text(request_model.raw_text, request_model.source_url)
    original_write_bytes = app.state.storage.write_bytes
    competing_job_id = "job_competing_race"
    competing_inserted = False

    def write_and_insert_competing_job(**kwargs):
        nonlocal competing_inserted
        artifact = original_write_bytes(**kwargs)
        if not competing_inserted:
            competing_inserted = True
            competing_artifact = original_write_bytes(
                artifact_type="job_snapshot",
                content=b"competing race snapshot",
                original_file_name="job-posting.txt",
                content_type="text/plain",
                candidate_id="candidate_local",
                privacy_class="private",
            )
            with session_scope(settings) as db:
                db.add(StoredArtifact.from_record(competing_artifact))
                db.flush()
                db.add(
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
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/jobs/ingest", json=payload)

    assert response.status_code == 201
    assert response.json()["id"] == competing_job_id
    with session_scope(settings) as db:
        assert len(list(db.execute(select(JobPosting)).scalars())) == 1
        assert len(list(db.execute(select(StoredArtifact)).scalars())) == 1
    assert len(list((settings.artifact_root / "job-snapshots").iterdir())) == 1


def test_job_ingest_result_reports_unique_race_as_existing_job(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = create_app(settings).state.storage
    payload = {
        "source_type": "gmail_alert",
        "raw_text": "Backend Developer\nPython APIs",
        "source_url": "http://localhost:8001/ats/generic/acme/jobs/race",
    }
    request_model = JobIngestRequest(**payload)
    snapshot_hash = job_posting_snapshot_hash(request_model)
    parsed = parse_job_text(request_model.raw_text, request_model.source_url)
    original_write_bytes = storage.write_bytes
    competing_job_id = "job_result_competing_race"
    competing_inserted = False

    def write_and_insert_competing_job(**kwargs):
        nonlocal competing_inserted
        artifact = original_write_bytes(**kwargs)
        if not competing_inserted:
            competing_inserted = True
            competing_artifact = original_write_bytes(
                artifact_type="job_snapshot",
                content=b"competing result race snapshot",
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

    storage.write_bytes = write_and_insert_competing_job

    with session_scope(settings) as db:
        result = api_main.persist_job_ingest_result(request_model, db, storage, settings)

    assert result.created is False
    assert result.payload["id"] == competing_job_id


def test_persist_job_ingest_uses_configured_posting_parser(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        posting_parser_provider="codex_cli",
    )
    init_database(settings)
    storage = create_app(settings).state.storage
    request_model = JobIngestRequest(
        source_type="paste",
        raw_text="Backend Engineering Intern\nPython SQL",
        source_url=None,
    )
    parsed = parse_job_text(
        "Backend Engineering Intern\nCompany: Globex\nLocation: Toronto, ON, Canada\nPython SQL",
        None,
    )
    calls = []

    def fake_parse_job_text(
        raw_text,
        source_url,
        parser_settings,
        *,
        source_type=None,
        title_hint=None,
    ):
        calls.append((raw_text, source_url, parser_settings.posting_parser_provider, source_type))
        return parsed

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", fake_parse_job_text)

    with session_scope(settings) as db:
        result = api_main.persist_job_ingest_result(request_model, db, storage, settings)

    assert calls == [("Backend Engineering Intern\nPython SQL", None, "codex_cli", "paste")]
    assert result.payload["employer"]["company_name"] == "Globex"
    assert result.payload["location"]["city"] == "Toronto"


def test_employment_timing_reparse_updates_only_timing_and_records_audit(tmp_path: Path):
    client = make_client(tmp_path)
    raw_text = (
        "Industrialization Co-op\n"
        "Company: Example Manufacturing\n"
        "Duration 4-8 Months (Negotiable)\n"
        "Python SQL"
    )
    created = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": raw_text, "source_url": None},
    ).json()

    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job is not None
        original_company = job.company_name
        original_role = job.role
        original_compensation = job.compensation
        job.employment_timing = {
            "raw_label": "Posted 2026-08-01",
            "timing_type": "posted_date_fallback",
            "start_date": "2026-08-01",
            "end_date": None,
            "duration_months": None,
            "duration_label": None,
            "hours_per_week_min": None,
            "hours_per_week_max": None,
            "schedule_type": None,
        }
        job.duration_months = None
        job.data_quality = {
            **job.data_quality,
            "missing_fields": [
                "employment_timing.end_date",
                "role.tech_stack",
            ],
            "ambiguous_fields": [
                "employment_timing.start_date",
                "compensation.pay_min",
            ],
            "inferred_fields": [
                "employment_timing.start_date",
                "location.city",
            ],
            "needs_manual_review": True,
        }

        changed = api_main.apply_parsed_employment_timing_to_existing_posting(
            job,
            raw_text,
            db=db,
        )
        assert changed is True
        assert job.employment_timing["raw_label"] == "4-8 Months"
        assert job.employment_timing["timing_type"] == "fixed_term"
        assert job.employment_timing["duration_label"] == "4-8 Months"
        assert job.duration_months is None
        assert job.company_name == original_company
        assert job.role == original_role
        assert job.compensation == original_compensation
        assert job.data_quality["ambiguous_fields"] == [
            "compensation.pay_min",
            "employment_timing.duration_months",
        ]
        assert job.data_quality["missing_fields"] == ["role.tech_stack"]
        assert job.data_quality["inferred_fields"] == ["location.city"]
        assert job.data_quality["needs_manual_review"] is True

        unchanged = api_main.apply_parsed_employment_timing_to_existing_posting(
            job,
            raw_text,
            db=db,
        )
        assert unchanged is False
        events = list(
            db.scalars(
                select(AuditLogEntry).where(
                    AuditLogEntry.event_type == "job.employment_timing_reparsed",
                    AuditLogEntry.entity_id == job.id,
                )
            )
        )
        assert len(events) == 1
        assert events[0].payload["posting_snapshot_hash"] == job.posting_snapshot_hash


def test_employment_timing_reparse_clears_resolved_timing_review_state(tmp_path: Path):
    client = make_client(tmp_path)
    raw_text = (
        "Graduate Intern\n"
        "The scheduled start date for this position is September 8th, 2026.\n"
        "Python"
    )
    created = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": raw_text, "source_url": None},
    ).json()

    with session_scope(client.app.state.settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job is not None
        job.employment_timing = {
            **job.employment_timing,
            "raw_label": "Posted 2026-08-01",
            "timing_type": "posted_date_fallback",
            "start_date": "2026-08-01",
        }
        job.data_quality = {
            **job.data_quality,
            "missing_fields": ["employment_timing.end_date"],
            "ambiguous_fields": ["employment_timing.start_date"],
            "inferred_fields": ["employment_timing.start_date"],
            "needs_manual_review": True,
        }

        changed = api_main.apply_parsed_employment_timing_to_existing_posting(
            job,
            raw_text,
            db=db,
        )

        assert changed is True
        assert job.employment_timing["start_date"] == "2026-09-08"
        assert job.data_quality["missing_fields"] == []
        assert job.data_quality["ambiguous_fields"] == []
        assert job.data_quality["inferred_fields"] == []
        assert job.data_quality["needs_manual_review"] is False
