from datetime import UTC, datetime, timedelta

import pytest

from sqlalchemy import select

from jobcockpit.services.workflows import JobIngestPayload, persist_job_ingest_result
from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobPosting, JobSourceReconciliation, Application
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.source_reconciliation import (
    enqueue_source_reconciliation, process_next_source_reconciliation,
    schedule_source_reconciliations, MAX_ATTEMPTS,
)
from jobcockpit.ingestion.posting_resolution import PostingResolution, ResolvedExternalPosting
from jobcockpit.storage.artifacts import ArtifactStorageService


@pytest.mark.parametrize("url,expected", [
    ("https://careers.example.test/global/en/apply", True),
    ("https://example.com/careers", True),
    ("", True),
    ("https://example.test/about/careers/job_id%3DJOB_POSTING-3-1234/", False),
    ("https://example.test/career/junior-software-developer/", False),
    ("https://example.test/Corp/xweb/XWeb.asp?JobId=123456", False),
])
def test_generic_source_eligibility_preserves_detail_routes(tmp_path, url, expected):
    from jobcockpit.ingestion.source_reconciliation import source_reconciliation_eligible
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        job.source_url = url
        job.status_source_repair_required = False
        assert source_reconciliation_eligible(db, job) is expected


def test_source_worker_can_process_while_ingestion_lease_is_held(tmp_path):
    from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
    settings, _ = setup_job(tmp_path)
    class Missing:
        def resolve(self, hint):
            return PostingResolution(status="not_found", error="official_posting_not_found")
    with BackgroundWorkerLeaseGuard(settings, "ingestion") as lease:
        assert lease.acquired
        result = process_next_source_reconciliation(settings, resolver=Missing())
        assert result is not None


@pytest.mark.parametrize("raises", [False, True])
def test_reviewed_http_scope_already_closes_owned_clients(raises):
    from jobcockpit.integrations.official_careers import _http_client_scope
    try:
        with _http_client_scope(None, 1) as client:
            assert not client.is_closed
            if raises:
                raise RuntimeError("request failed")
    except RuntimeError:
        pass
    assert client.is_closed


def test_source_worker_shutdown_fences_inflight_result(tmp_path, monkeypatch, allow_public_dns):
    from threading import Event
    from jobcockpit.ingestion.source_reconciliation import SourceBackgroundWorker, SOURCE_WORKER_LEASE
    from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
    from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)
    settings.background_worker_shutdown_timeout_seconds = 0.01
    entered, release = Event(), Event()
    class Blocked(PostingDiscoveryResolver):
        def resolve(self, hint):
            entered.set()
            assert release.wait(5)
            return Found().resolve(hint)
    monkeypatch.setattr("jobcockpit.ingestion.posting_links.default_posting_resolver", lambda settings: Blocked())
    worker = SourceBackgroundWorker(settings)
    worker.start()
    try:
        assert entered.wait(3)
        worker.stop()
        with BackgroundWorkerLeaseGuard(settings, "replacement", lease_name=SOURCE_WORKER_LEASE) as lease:
            assert lease.acquired
    finally:
        release.set()
        worker.thread.join(3)
        assert not worker.thread.is_alive()
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url.startswith("https://www.linkedin.com/")
        assert db.get(JobSourceReconciliation, job_id).status != "resolved"


def test_ingestion_loop_does_not_execute_pending_source_discovery(tmp_path, monkeypatch):
    from jobcockpit.services.workflows import process_next_background_job
    settings, _ = setup_job(tmp_path)
    def unexpected(*args, **kwargs):
        pytest.fail("ingestion loop attempted source network work")
    monkeypatch.setattr("jobcockpit.ingestion.source_reconciliation.process_next_source_reconciliation", unexpected)
    assert process_next_background_job(settings) is None


def test_source_cli_reports_busy_without_borrowing_another_lease(tmp_path, capsys):
    from jobcockpit.cli import main
    from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
    from jobcockpit.ingestion.source_reconciliation import SOURCE_WORKER_LEASE
    import json
    settings, _ = setup_job(tmp_path)
    with BackgroundWorkerLeaseGuard(settings, "api-source", lease_name=SOURCE_WORKER_LEASE):
        assert main(["worker", "source", "--once"], settings=settings) == 0
    assert json.loads(capsys.readouterr().out)["worker_busy"]


def setup_job(tmp_path, *, metadata_only=False):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
                        artifact_root=tmp_path / "storage", background_workers_enabled=False)
    init_database(settings)
    with session_scope(settings) as db:
        result = persist_job_ingest_result(JobIngestPayload(
            source_type="ats_csjobs", source_url="https://www.linkedin.com/jobs/view/12345",
            raw_text="Title: Software Engineering Intern\nCompany: Example Engineering\n"
            "Location: Calgary, AB\n" + ("" if metadata_only else "Develop software and test engineering systems."),
        ), db, ArtifactStorageService(settings.artifact_root), settings, run_fit_analysis=False)
    return settings, result.payload["id"]


class Found:
    def resolve(self, hint):
        return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
            source_url="https://careers.exampleengineering.com/jobs/12345",
            raw_text="Title: Software Engineering Intern\nCompany: Example Engineering\nLocation: Calgary",
            confidence=0.9,
        ))


def test_source_worker_follows_indeed_company_link_without_allowing_fallback(
    tmp_path, monkeypatch, allow_public_dns,
):
    from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver

    # Same discovery route as the observed Indeed-to-Workday AltaGas posting.
    allow_public_dns({"www.linkedin.com", "ca.indeed.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)
    source_url = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
    with session_scope(settings) as db:
        db.get(JobPosting, job_id).source_url = source_url
    calls = []

    class Render:
        def resolve(self, hint):
            calls.append("source")
            return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
                source_url=hint.source_url, raw_text="Title: Software Engineering Intern\n"
                "Company: Example Engineering\nLocation: Calgary\n"
                "Develop software and test engineering systems.",
                evidence_origin="browser_dom", confidence=.9,
            ))

        def resolve_company_apply(self, hint, posting):
            calls.append("company_link")
            return Found().resolve(hint)

    class NoSearch:
        def search(self, *args):
            pytest.fail("The worker should follow the company link before search")

    resolver = PostingDiscoveryResolver(search_client=NoSearch(), rendered_fallback_resolver=Render())
    monkeypatch.setattr("jobcockpit.ingestion.posting_links.default_posting_resolver", lambda settings: resolver)
    result = process_next_source_reconciliation(settings)
    assert resolver.allow_aggregator_fallback is False
    assert calls == ["source", "company_link"]
    assert result["status"] == "resolved"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url == "https://careers.exampleengineering.com/jobs/12345"


@pytest.mark.parametrize("linkedin", [False, True])
def test_metadata_only_retry_recovers_description_in_same_job(tmp_path, allow_public_dns, linkedin):
    from jobcockpit.db.models import StoredArtifact, JobReprocessRun
    allow_public_dns({"www.linkedin.com", "ca.indeed.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path, metadata_only=True)
    settings.fit_analysis_provider = "heuristic"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        previous_artifact = job.raw_content_artifact_id
        if linkedin:
            job.source_url = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
        original_url = job.source_url
        job.source = {**job.source, "source_resolution": {
            "kind": "aggregator_fallback", "path": "aggregator_structured_fallback",
        }}

    class FullPosting(Found):
        def resolve(self, hint):
            from dataclasses import replace
            result = super().resolve(hint)
            return replace(result, source_path="aggregator_rendered_fallback" if linkedin else "",
                posting=replace(result.posting,
                source_url="https://www.linkedin.com/jobs/view/54321" if linkedin else result.posting.source_url,
                raw_text=result.posting.raw_text +
                "\nResponsibilities\nBuild Python analytics tools and maintain customer reporting systems."))

    result = process_next_source_reconciliation(settings, resolver=FullPosting())
    assert result["status"] == "resolved"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.raw_content_artifact_id != previous_artifact
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert b"Build Python analytics" in ArtifactStorageService(settings.artifact_root).read_bytes(artifact.storage_uri)
        assert job.source["source_resolution"]["original_url"] == original_url
        assert len(list(db.scalars(select(JobPosting)))) == 1
        fit_run = db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id))
        assert fit_run.run_type == "fit_analysis" and fit_run.status == "queued"


def test_metadata_only_retry_rejects_identity_without_description(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path, metadata_only=True)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        old_url = job.source_url
        job.source = {**job.source, "source_resolution": {
            "kind": "aggregator_fallback", "path": "aggregator_structured_fallback",
        }}
    result = process_next_source_reconciliation(settings, resolver=Found())
    assert result["status"] != "resolved"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url == old_url


@pytest.mark.parametrize("structured_fallback", [False, True])
def test_import_enqueues_once_and_source_only_update_preserves_snapshot(tmp_path, allow_public_dns, structured_fallback):
    from dataclasses import replace
    from jobcockpit.db.models import JobReprocessRun
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        if structured_fallback:
            job.source = {**job.source, "source_resolution": {
                "kind": "aggregator_fallback", "path": "aggregator_structured_fallback",
            }}
        job.fit_analysis = {"summary": "Existing assessment"}
        before = (job.raw_content_artifact_id, job.posting_snapshot_hash, dict(job.role), dict(job.fit_analysis))
        queued = db.get(JobSourceReconciliation, job_id)
        assert queued is not None and queued.status == "pending"
        assert enqueue_source_reconciliation(db, job) is queued
        assert len(list(db.scalars(select(JobSourceReconciliation)))) == 1
    class CompletePosting(Found):
        def resolve(self, hint):
            result = super().resolve(hint)
            return replace(result, posting=replace(result.posting, raw_text=result.posting.raw_text +
                "\nBuild Python analytics tools and maintain customer reporting systems."))

    result = process_next_source_reconciliation(settings, resolver=CompletePosting())
    assert result["status"] == "resolved"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == "https://careers.exampleengineering.com/jobs/12345"
        assert (job.raw_content_artifact_id, job.posting_snapshot_hash, job.role, job.fit_analysis) == before
        assert db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)) is None
        assert job.source["source_resolution"]["original_url"].startswith("https://www.linkedin.com/")


def test_source_change_during_discovery_cannot_be_overwritten(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)

    class Changed(Found):
        def resolve(self, hint):
            with session_scope(settings) as db:
                job = db.get(JobPosting, job_id)
                job.source_url = "https://example.test/user-selected"
                job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
            return super().resolve(hint)

    assert process_next_source_reconciliation(settings, resolver=Changed())["status"] == "cancelled"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url == "https://example.test/user-selected"


def test_fit_update_during_discovery_retries_fresh_inputs(tmp_path):
    # Observed Bombardier ingestion starts fit and source workers together.
    settings, job_id = setup_job(tmp_path)

    class ConcurrentFit:
        def resolve(self, hint):
            with session_scope(settings) as db:
                db.get(JobPosting, job_id).fit_analysis = {"summary": "Updated assessment"}
            return PostingResolution(status="not_found", error="official_posting_not_found")

    result = process_next_source_reconciliation(settings, resolver=ConcurrentFit())
    assert result["status"] == "retry_scheduled"
    assert result["reason_code"] == "source_changed_during_discovery"
    assert result["next_attempt_at"] is not None
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url.startswith("https://www.linkedin.com/")
        db.get(JobSourceReconciliation, job_id).next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    class Missing:
        def resolve(self, hint):
            return PostingResolution(status="not_found", error="official_posting_not_found")
    assert process_next_source_reconciliation(settings, resolver=Missing())["attempts"] == 2


def test_source_hint_retains_original_and_portal_candidate_links(tmp_path):
    from jobcockpit.ingestion.source_reconciliation import _hint
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        job.source_url = "https://www.eluta.ca/sandbox?destination=/direct/i"
        job.source = {**job.source,
            "portal_source_url": "https://www.eluta.ca/spl/software-intern-example",
            "source_resolution": {"original_url": "https://jobs.exampleengineering.com/jobs/123"}}
        assert _hint(job).candidate_urls == (
            "https://jobs.exampleengineering.com/jobs/123",
            "https://www.eluta.ca/spl/software-intern-example",
        )


@pytest.mark.parametrize("manual,attempts,expected", [(False, 1, 1), (True, 1, 0), (False, MAX_ATTEMPTS, 0)])
def test_sweep_recovers_only_eligible_legacy_source_change_cancellations(tmp_path, manual, attempts, expected):
    from jobcockpit.ingestion.source_reconciliation import sweep_source_reconciliations
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        run = db.get(JobSourceReconciliation, job_id)
        run.status, run.reason_code = "cancelled", "source_changed_during_discovery"
        run.attempts, run.next_attempt_at = attempts, None
        if manual:
            job = db.get(JobPosting, job_id)
            job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
    assert sweep_source_reconciliations(settings).enqueued == expected
    with session_scope(settings) as db:
        run = db.get(JobSourceReconciliation, job_id)
        assert run.status == ("retry_scheduled" if expected else "cancelled")
        assert run.attempts == attempts


def test_unavailable_sources_retry_with_history_and_bounded_backoff(tmp_path, allow_public_dns):
    allow_public_dns("www.linkedin.com")
    settings, job_id = setup_job(tmp_path)

    class Missing:
        def resolve(self, hint):
            return PostingResolution(status="not_found", error="official_posting_not_found")

    result = process_next_source_reconciliation(settings, resolver=Missing())
    assert result["status"] == "retry_scheduled"
    assert result["outcome"] == "unavailable"
    assert process_next_source_reconciliation(settings, resolver=Missing()) is None
    with session_scope(settings) as db:
        run = db.get(JobSourceReconciliation, job_id)
        assert len(run.history) == 1
        assert run.next_attempt_at.replace(tzinfo=UTC) >= datetime.now(UTC) + timedelta(minutes=50)
        assert db.get(JobPosting, job_id).source["source_resolution"]["official_discovery_status"] == "not_found"
    for _ in range(MAX_ATTEMPTS - 1):
        with session_scope(settings) as db:
            db.get(JobSourceReconciliation, job_id).next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        result = process_next_source_reconciliation(settings, resolver=Missing())
    assert result["status"] == "unavailable"
    assert result["attempts"] == MAX_ATTEMPTS
    assert len(result["history"]) == MAX_ATTEMPTS
    assert result["next_attempt_at"] is None
    assert process_next_source_reconciliation(settings, resolver=Missing()) is None


def test_repair_backlog_is_scheduled_and_same_url_validation_unparks_status_checks(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        db.delete(db.get(JobSourceReconciliation, job_id))
        job = db.get(JobPosting, job_id)
        job.source_url = "https://careers.exampleengineering.com/jobs/12345"
        job.status_source_repair_required = True
        job.status_next_check_at = None
        db.flush()
        assert schedule_source_reconciliations(db) == 1
    assert process_next_source_reconciliation(settings, resolver=Found())["status"] == "resolved"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert not job.status_source_repair_required
        assert job.status_next_check_at is not None


def test_application_submitted_during_discovery_is_not_changed(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)

    class Submitted(Found):
        def resolve(self, hint):
            with session_scope(settings) as db:
                db.add(Application(application_id="applied_test", candidate_id="candidate_local", job_id=job_id,
                                   status="applied", applied_at=datetime.now(UTC)))
            return super().resolve(hint)

    assert process_next_source_reconciliation(settings, resolver=Submitted())["status"] == "cancelled"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url.startswith("https://www.linkedin.com/")


def test_replaced_claim_cannot_commit_its_discovery(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)

    class Replaced(Found):
        def resolve(self, hint):
            with session_scope(settings) as db:
                db.get(JobSourceReconciliation, job_id).claim_token = "new_claim"
            return super().resolve(hint)

    assert process_next_source_reconciliation(settings, resolver=Replaced()) is None
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url.startswith("https://www.linkedin.com/")


def test_duplicate_candidate_is_kept_for_review(tmp_path, allow_public_dns):
    allow_public_dns({"www.linkedin.com", "careers.exampleengineering.com"})
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        persist_job_ingest_result(JobIngestPayload(
            source_type="url", source_url="https://careers.exampleengineering.com/jobs/12345",
            raw_text="Title: Software Engineering Intern\nCompany: Example Engineering\n"
            "Location: Calgary, AB\nDevelop software and test engineering systems. Additional role details.",
        ), db, ArtifactStorageService(settings.artifact_root), settings, run_fit_analysis=False)
    result = process_next_source_reconciliation(settings, resolver=Found())
    assert result["status"] == "blocked"
    assert result["reason_code"] == "candidate_matches_another_saved_job"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).source_url.startswith("https://www.linkedin.com/")


def test_queue_status_and_manual_retry_api_preserve_attempt_history(tmp_path, allow_public_dns):
    from fastapi.testclient import TestClient
    from jobcockpit.api.main import create_app
    allow_public_dns("www.linkedin.com")
    settings, job_id = setup_job(tmp_path)
    with session_scope(settings) as db:
        run = db.get(JobSourceReconciliation, job_id)
        run.status = "unavailable"
        run.attempts = MAX_ATTEMPTS
        run.next_attempt_at = None
        run.history = [{"at": datetime.now(UTC).isoformat(), "outcome": "unavailable", "reason_code": "official_posting_not_found"}]
    with TestClient(create_app(settings)) as client:
        status = client.get(f"/api/jobs/{job_id}/source-reconciliation")
        assert status.status_code == 200
        assert status.json()["eligible"]
        retry = client.post(f"/api/jobs/{job_id}/source-reconciliation/retry")
        assert retry.status_code == 200
        assert retry.json()["status"] == "pending"
        assert len(retry.json()["history"]) == 1
