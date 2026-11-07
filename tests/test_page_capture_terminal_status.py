from __future__ import annotations

import pytest
from sqlalchemy import select

from jobcockpit.services import workflows as api_main
from jobcockpit.api.page_capture_runs import sync_job_source_identities
from jobcockpit.db.models import (
    Application,
    AuditLogEntry,
    JobPosting,
    JobSourceIdentity,
    PageCaptureRun,
    PageCaptureSnapshot,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.llm_providers import register_posting_parser_provider


REMOVED_LINKEDIN_TEXT = "This job has been removed or the job ID is not valid."


def test_linkedin_removed_capture_updates_existing_job_without_parsing(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    class ParserThatMustNotRun:
        def parse(self, *_args, **_kwargs):
            raise AssertionError("terminal LinkedIn state must bypass parsing")

    register_posting_parser_provider(
        "unit_test_terminal_capture",
        lambda _settings: ParserThatMustNotRun(),
        replace=True,
    )
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://www.linkedin.com/jobs/view/1234567890"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Intern\nCompany: Acme\nPython",
        },
    ).json()
    with session_scope(settings) as db:
        stored_job = db.get(JobPosting, job["id"])
        stored_job.source_url = source_url
        stored_job.source = {**stored_job.source, "source_url": source_url}
    settings.posting_parser_provider = "unit_test_terminal_capture"
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "LinkedIn",
            "visible_text": REMOVED_LINKEDIN_TEXT,
            "target_job_id": job["id"],
        },
    ).json()

    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["result"]["status"] == "terminal_status_updated"
    refreshed = client.get(f"/api/jobs/{job['id']}").json()
    assert refreshed["status"] == "closed"
    assert refreshed["status_check"]["evidence_code"] == "linkedin_job_removed_or_invalid"
    assert refreshed["employer"]["company_name"] == "Acme"
    with session_scope(settings) as db:
        completed_audit = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "ingestion.run_completed",
                AuditLogEntry.entity_id == queued["capture_id"],
            )
        ).scalar_one()
        assert completed_audit.payload["counts"]["jobs_reused_count"] == 1
        assert completed_audit.payload["counts"]["items_skipped_count"] == 0


def test_indeed_terminal_capture_matches_across_owned_domains(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Platform Engineer\nCompany: Acme",
        },
    ).json()
    job_key = "3cab9868c348dd54"
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = f"https://ca.indeed.com/viewjob?jk={job_key}"
        stored.source = {**stored.source, "source_url": stored.source_url}
        sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": f"https://www.indeed.ca/viewjob?jk={job_key}",
            "page_title": "Platform Engineer",
            "visible_text": "This job has expired on Indeed",
        },
    ).json()
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["result"]["status"] == "terminal_status_updated"
    assert processed["result"]["evidence_code"] == "indeed_job_expired"
    refreshed = client.get(f"/api/jobs/{job['id']}").json()
    assert refreshed["status"] == "expired"


def test_unmatched_linkedin_removed_capture_is_skipped_without_creating_job(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://www.linkedin.com/jobs/view/2345678901",
            "page_title": "LinkedIn",
            "visible_text": REMOVED_LINKEDIN_TEXT,
        },
    ).json()

    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "skipped"
    assert processed["job_id"] is None
    assert processed["result"]["evidence_code"] == "linkedin_job_removed_or_invalid"
    with session_scope(settings) as db:
        assert list(db.execute(select(JobPosting)).scalars()) == []


def test_linkedin_terminal_capture_reconciles_authoritative_duplicates(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    jobs = [
        client.post(
            "/api/jobs/ingest",
            json={
                "source_type": "paste",
                "raw_text": f"Backend Intern {suffix}\nCompany: Acme",
            },
        ).json()
        for suffix in ("A", "B")
    ]
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    with session_scope(settings) as db:
        for payload in jobs:
            stored = db.get(JobPosting, payload["id"])
            stored.source_url = source_url
            stored.source = {**stored.source, "source_url": source_url}
            sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "LinkedIn",
            "visible_text": REMOVED_LINKEDIN_TEXT,
        },
    ).json()
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    canonical_id = processed["job_id"]
    duplicate_id = next(job["id"] for job in jobs if job["id"] != canonical_id)
    with session_scope(settings) as db:
        canonical = db.get(JobPosting, canonical_id)
        duplicate = db.get(JobPosting, duplicate_id)
        assert canonical.status == "closed"
        assert duplicate.source["merged_into_job_id"] == canonical_id
        run = db.get(PageCaptureRun, queued["capture_id"])
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        for artifact_id in (
            snapshot.raw_content_artifact_id,
            snapshot.source_archive_artifact_id,
        ):
            assert db.get(StoredArtifact, artifact_id).job_id == canonical_id


def test_linkedin_terminal_capture_closes_duplicates_when_merge_is_unsafe(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    jobs = [
        client.post(
            "/api/jobs/ingest",
            json={
                "source_type": "paste",
                "raw_text": f"Backend Intern {suffix}\nCompany: Acme",
            },
        ).json()
        for suffix in ("A", "B")
    ]
    source_url = "https://www.linkedin.com/jobs/view/4567890123"
    with session_scope(settings) as db:
        for index, payload in enumerate(jobs):
            stored = db.get(JobPosting, payload["id"])
            stored.source_url = source_url
            stored.source = {**stored.source, "source_url": source_url}
            sync_job_source_identities(db, stored)
            db.add(
                Application(
                    application_id=f"application_terminal_merge_{index}",
                    candidate_id="candidate_local",
                    job_id=stored.id,
                )
            )

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "LinkedIn",
            "visible_text": REMOVED_LINKEDIN_TEXT,
        },
    ).json()
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    for job in jobs:
        detail = client.get(f"/api/jobs/{job['id']}").json()
        assert detail["capture_processing"]["capture_id"] == queued["capture_id"]
        assert detail["capture_processing"]["status"] == "succeeded"
    with session_scope(settings) as db:
        stored_jobs = [db.get(JobPosting, job["id"]) for job in jobs]
        assert {job.status for job in stored_jobs} == {"closed"}
        assert all(not (job.source or {}).get("merged_into_job_id") for job in stored_jobs)
        deferred = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "extension.page_capture_merge_deferred",
                AuditLogEntry.entity_id == queued["capture_id"],
            )
        ).scalar_one()
        assert deferred.payload["error_code"] == "capture_merge_conflict"
        assert set(deferred.payload["job_ids"]) == {job["id"] for job in jobs}


@pytest.mark.parametrize("initial_status", ["active", "closed"])
def test_linkedin_terminal_capture_does_not_accept_mismatched_target(
    initial_status,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Intern\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = "https://www.linkedin.com/jobs/view/1111111111"
        stored.source = {**stored.source, "source_url": stored.source_url}
        stored.status = initial_status

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://www.linkedin.com/jobs/view/2222222222",
            "page_title": "LinkedIn",
            "visible_text": REMOVED_LINKEDIN_TEXT,
            "target_job_id": job["id"],
        },
    ).json()
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "skipped"
    assert processed["result"]["status"] == "terminal_status_not_applied"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == initial_status
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        identities = list(
            db.execute(
                select(JobSourceIdentity).where(
                    JobSourceIdentity.job_id == job["id"],
                    JobSourceIdentity.provider == run.provider,
                    JobSourceIdentity.identity_hash == run.identity_hash,
                )
            ).scalars()
        )
        assert identities == []
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        assert db.get(StoredArtifact, snapshot.raw_content_artifact_id).job_id is None
        assert db.get(StoredArtifact, snapshot.source_archive_artifact_id).job_id is None
