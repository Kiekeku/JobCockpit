"""Round-trip recovery through the real fallback formatter, DB, and worker."""
from dataclasses import replace
from functools import partial
from unittest.mock import patch

import pytest
from sqlalchemy import select

from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobPosting, JobReprocessRun, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsJob, SourceArchive
from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver, _structured_source_hint_resolution
from jobcockpit.ingestion.posting_links import PostingLinkContext, process_posting_link
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, PostingResolution, ResolvedExternalPosting, SearchResult
from jobcockpit.ingestion.source_reconciliation import process_next_source_reconciliation
from jobcockpit.services.workflows import persist_job_ingest_result
from jobcockpit.storage.artifacts import ArtifactStorageService

INDEED = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
LINKEDIN = "https://www.linkedin.com/jobs/view/1234567890"
OFFICIAL = "https://careers.exampleengineering.com/jobs/12345"
IDENTITY = "Title: Software Engineering Intern\nCompany: Example Engineering\nLocation: Calgary, AB"
DESCRIPTION = "Responsibilities\nBuild Python analytics tools and maintain customer reporting systems."
BLOCKED = "Page Title: Blocked - Indeed.com\nRequest Blocked\nYou have been blocked.\nTroubleshooting Cloudflare Errors"
AUTHWALL = "Page Title: Sign Up | LinkedIn\nJoin LinkedIn\nEmail\nPassword (6+ characters)\nBy clicking Agree & Join, you agree to the LinkedIn User Agreement."


def ingest_fallback(tmp_path, source_text=""):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        artifact_root=tmp_path / "storage", background_workers_enabled=False)
    init_database(settings)
    hint = ExternalPostingHint(source_url=INDEED, source_kind="indeed", company="Example Engineering",
        title="Software Engineering Intern", location="Calgary, AB", source_text=source_text)
    resolution = _structured_source_hint_resolution(hint, reason="official_posting_not_found")
    # Seed historical imports made before the completeness gate. Recovery must
    # still repair those rows even though new imports now reject these screens.
    with session_scope(settings) as db, patch("jobcockpit.ingestion.posting_links.require_complete_posting"):
        result = process_posting_link(PostingLinkContext(source_type="gmail_alert", source_url=INDEED,
            raw_text=IDENTITY, link_text=hint.title), db=db,
            storage=ArtifactStorageService(settings.artifact_root), settings=settings,
            posting_resolution=resolution, job_importer=partial(persist_job_ingest_result, run_fit_analysis=False))
        assert result.ingestion_status == "fallback_imported"
        job = db.get(JobPosting, result.linked_job_id)
        job.fit_analysis = {"summary": "Previous assessment"}
        previous = (job.raw_content_artifact_id, job.posting_snapshot_hash, dict(job.role), dict(job.fit_analysis))
    return settings, result.linked_job_id, previous


class FullPosting:
    def resolve(self, hint):
        return PostingResolution(status="resolved", posting=ResolvedExternalPosting(source_url=OFFICIAL,
            raw_text=IDENTITY + "\n" + DESCRIPTION, confidence=0.9,
            source_archive=SourceArchive(source_url=OFFICIAL, final_url=OFFICIAL, status_code=200,
                content_type="text/html", body=b"<p>Recovered posting</p>")))


@pytest.fixture(autouse=True)
def public_hosts(allow_public_dns):
    allow_public_dns({"ca.indeed.com", "www.linkedin.com", "careers.exampleengineering.com"})


@pytest.mark.parametrize("source_text", ["", IDENTITY + "\nEmail Row: Software Engineering Intern", BLOCKED, AUTHWALL])
@pytest.mark.parametrize("destination", [OFFICIAL, LINKEDIN])
@pytest.mark.parametrize("fit_provider", ["disabled", "heuristic"])
def test_real_ingestion_to_recovery_replaces_only_missing_content(tmp_path, monkeypatch, source_text, destination, fit_provider):
    settings, job_id, previous = ingest_fallback(tmp_path, source_text)
    settings.fit_analysis_provider = fit_provider
    render_urls = []

    class Search:
        def search(self, query, limit):
            if destination == OFFICIAL or "site:linkedin.com/jobs/view" in query:
                return [SearchResult(title="Software Engineering Intern", url=destination,
                    snippet="Example Engineering Calgary, AB")]
            return []

    class Apply:
        def resolve(self, url, context):
            posting = FullPosting().resolve(None).posting
            return AtsJob(source_url=url, raw_text=posting.raw_text, source_archive=posting.source_archive)

    class Render:
        def resolve(self, hint):
            render_urls.append(hint.source_url)
            result = FullPosting().resolve(hint)
            return replace(result, posting=replace(result.posting, source_url=hint.source_url,
                source_archive=replace(result.posting.source_archive,
                    source_url=hint.source_url, final_url=hint.source_url)))

    resolver = PostingDiscoveryResolver(search_client=Search(), apply_resolver=Apply(), rendered_fallback_resolver=Render())
    monkeypatch.setattr("jobcockpit.ingestion.posting_links.default_posting_resolver", lambda settings: resolver)
    result = process_next_source_reconciliation(settings)
    assert result["status"] == "resolved" and result["reason_code"] == "source_description_enriched"
    assert INDEED not in render_urls
    assert render_urls == ([LINKEDIN] if destination == LINKEDIN else [])
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == destination
        assert job.raw_content_artifact_id != previous[0] and job.posting_snapshot_hash != previous[1]
        storage = ArtifactStorageService(settings.artifact_root)
        snapshot = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert DESCRIPTION in storage.read_bytes(snapshot.storage_uri).decode()
        assert db.get(StoredArtifact, previous[0]) is not None
        assert job.source["source_resolution"]["original_url"] == INDEED
        assert job.source["source_resolution"]["verification"]["observed_content_sha256"]
        archive = db.get(StoredArtifact, job.source["source_archive_artifact_id"])
        assert storage.read_bytes(archive.storage_uri) == b"<p>Recovered posting</p>"
        assert job.fit_analysis == {}
        runs = list(db.scalars(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)))
        if fit_provider == "disabled":
            assert runs == []
        else:
            assert len(runs) == 1 and runs[0].run_type == "fit_analysis"
        assert len(list(db.scalars(select(JobPosting)))) == 1
    assert process_next_source_reconciliation(settings) is None


@pytest.mark.parametrize("source_text", [
    IDENTITY + "\n" + DESCRIPTION,
    IDENTITY + "\nBuild analytics tools.",
    IDENTITY + "\nSource Evidence: About the Role Build, test, and deploy live product features.",
])
def test_complete_or_uncertain_saved_content_is_preserved(tmp_path, source_text):
    settings, job_id, previous = ingest_fallback(tmp_path, source_text)
    assert process_next_source_reconciliation(settings, resolver=FullPosting())["status"] == "resolved"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == OFFICIAL
        assert (job.raw_content_artifact_id, job.posting_snapshot_hash, job.role, job.fit_analysis) == previous
        assert db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)) is None


def test_provenance_only_candidate_stays_retryable_without_mutating_snapshot(tmp_path):
    settings, job_id, previous = ingest_fallback(tmp_path)
    with session_scope(settings) as db:
        artifact = db.get(StoredArtifact, previous[0])
        persisted_raw = ArtifactStorageService(settings.artifact_root).read_bytes(artifact.storage_uri).decode()

    class MetadataCandidate(FullPosting):
        def resolve(self, hint):
            result = super().resolve(hint)
            return replace(result, posting=replace(result.posting, raw_text=persisted_raw))

    assert process_next_source_reconciliation(settings, resolver=MetadataCandidate())["status"] == "retry_scheduled"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == INDEED
        assert (job.raw_content_artifact_id, job.posting_snapshot_hash, job.role, job.fit_analysis) == previous
        assert db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)) is None


@pytest.mark.parametrize("failure_stage", ["parse", "after_artifact_write"])
def test_failed_recovery_rolls_back_content_artifacts_and_fit_queue(tmp_path, monkeypatch, failure_stage):
    settings, job_id, previous = ingest_fallback(tmp_path)
    settings.fit_analysis_provider = "heuristic"
    files_before = {path.relative_to(settings.artifact_root) for path in settings.artifact_root.rglob("*") if path.is_file()}
    with session_scope(settings) as db:
        artifacts_before = set(db.scalars(select(StoredArtifact.artifact_id)))

    def fail(*args, **kwargs):
        raise RuntimeError("injected recovery failure")

    if failure_stage == "parse":
        monkeypatch.setattr("jobcockpit.ingestion.parser.parse_job_text_with_settings", fail)
    else:
        # The fit enqueue occurs after the update has written both artifacts.
        monkeypatch.setattr("jobcockpit.services.workflows.enqueue_job_reprocess", fail)
    result = process_next_source_reconciliation(settings, resolver=FullPosting())
    assert result["status"] == "retry_scheduled"
    assert result["reason_code"] == ("source_enrichment_parse_failed" if failure_stage == "parse" else "source_enrichment_failed")
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == INDEED
        assert (job.raw_content_artifact_id, job.posting_snapshot_hash, job.role, job.fit_analysis) == previous
        assert set(db.scalars(select(StoredArtifact.artifact_id))) == artifacts_before
        assert db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)) is None
    assert {path.relative_to(settings.artifact_root) for path in settings.artifact_root.rglob("*") if path.is_file()} == files_before


def test_concurrent_manual_edit_discards_stale_recovery_and_retries(tmp_path):
    settings, job_id, previous = ingest_fallback(tmp_path)

    class EditedDuringFetch(FullPosting):
        def resolve(self, hint):
            with session_scope(settings) as db:
                db.get(JobPosting, job_id).compensation = {"salary_text": "Manually verified pay"}
            return super().resolve(hint)

    result = process_next_source_reconciliation(settings, resolver=EditedDuringFetch())
    assert result["status"] == "retry_scheduled"
    assert result["reason_code"] == "source_changed_during_discovery"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert job.source_url == INDEED and job.raw_content_artifact_id == previous[0]
        assert job.compensation == {"salary_text": "Manually verified pay"}
        assert job.fit_analysis == previous[3]
        assert db.scalar(select(JobReprocessRun).where(JobReprocessRun.job_id == job_id)) is None


def test_unreadable_snapshot_does_not_authorize_content_replacement(tmp_path, monkeypatch):
    settings, job_id, previous = ingest_fallback(tmp_path)

    def unreadable(*args, **kwargs):
        raise OSError("snapshot unreadable")

    monkeypatch.setattr(ArtifactStorageService, "read_bytes", unreadable)
    result = process_next_source_reconciliation(settings, resolver=FullPosting())
    assert result["status"] == "resolved" and result["reason_code"] == "official_posting_resolved"
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        assert (job.raw_content_artifact_id, job.posting_snapshot_hash, job.role, job.fit_analysis) == previous
