"""Minimized identity regressions from the September saved-posting audit.

These tests supply parsed fields; no job-field parser expectations are changed.
Employer hostnames identify adapters, but posting IDs and content are invented.
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from jobcockpit.db.models import (
    EmailIngestionRun, JobAlertEmail, JobAlertLink, JobAlertSource, JobPosting, StoredArtifact,
)
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsProviderError
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.services import workflows
from jobcockpit.storage.artifacts import ArtifactStorageService


def posting(settings, url, company="Example", title="Software Engineering Co-op"):
    request = SimpleNamespace(
        source_type="gmail_alert", source_url=url, source_url_dedup_safe=True,
        skip_language_filter=False, source_archive=None,
        raw_text=f"{title}\nCompany: {company}\nApply URL: {url}\nBuild engineering tools.",
    )
    parsed = workflows.parse_job_text_with_settings(request.raw_text, url, settings)
    parsed.employer.company_name = company
    parsed.role.job_title = title
    parsed.role.normalized_title = title.casefold().replace(" ", "_")
    parsed.location.city = "Toronto"
    parsed.location.country = "Canada"
    parsed.employment_timing.raw_label = None
    parsed.employment_timing.start_date = None
    parsed.employment_timing.end_date = None
    parsed.employment_timing.duration_months = None
    return request, parsed


OFFICIAL_VARIANTS = [
    # Observed AMD captures use all three routes for the same numbered posting.
    ("https://careers.amd.com/careers-home/jobs/1234?icims=1", "https://careers.amd.com/careers-home/jobs/1234"),
    ("https://careers.amd.com/jobs/1234?icims=1", "https://careers.amd.com/students/jobs/1234?lang=en-us"),
    ("https://careers.planview.com/careers-home/jobs/1234/job?icims=1", "https://careers.planview.com/careers-home/jobs/1234?lang=en-us"),
    ("https://careers.planview.com/jobs/1234/job?icims=1", "https://careers.planview.com/jobs/1234?lang=en-us"),
    ("https://careers.trccompanies.com/jobs/1234", "https://careers.trccompanies.com/careers-home/jobs/1234"),
    ("https://jobs.ericsson.com/careers/job/123456789", "https://jobs.ericsson.com/careers/job/123456789?page=1&jobPipeline=careersite"),
    ("https://careers.cisco.com/global/en/job/1234567", "https://careers.cisco.com/global/en/job/CISCISGLOBAL1234567EXTERNALENGLOBAL/Intern"),
    ("https://amazon.jobs/en/jobs/1234567/software-intern", "https://www.amazon.jobs/en/jobs/1234567/software-intern"),
    ("https://jobs.intuit.com/job/-/-/123/456789", "https://jobs.intuit.com/job/toronto/intern/123/456789"),
    ("https://jobs.teck.com/job/Intern/12345-en_US/", "https://jobs.teck.com/job/Intern-other-slug/12345-en_US/"),
    ("https://careers.deloitte.ca/job/Intern/12345-en_US/", "https://careers.deloitte.ca/job/Intern/12345-en_US/"),
    ("https://careers.l3harris.com/job/toronto/intern/123/456789", "https://careers.l3harris.com/job/toronto/intern/123/456789"),
    ("https://careers.l3harris.com/en/job/toronto/intern/123/456789", "https://careers.l3harris.com/en/job/toronto/intern/123/456789"),
    ("https://example.wd3.myworkdayjobs.com/Careers/job/Toronto/Intern_R12345", "https://example.wd3.myworkdayjobs.com/en-US/Careers/job/Toronto/Intern_R12345"),
    ("https://www.linkedin.com/jobs/view/123456", "https://www.linkedin.com/jobs/view/123456"),
]


@pytest.mark.parametrize("url", [
    "https://careers.amd.com/careers-home/jobs/1234?icims=1",
    "https://careers.amd.com/students/jobs/1234?lang=en-us",
])
def test_observed_amd_routes_keep_distinct_posting_ids(jobcockpit_settings_factory, allow_public_dns, url):
    allow_public_dns("careers.amd.com")
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        request, parsed = posting(settings, url)
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        request, parsed = posting(settings, url.replace("1234", "5678"))
        request.raw_text += "\nAnother posting."
        second = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert first.created and second.created
        assert first.payload["id"] != second.payload["id"]


@pytest.mark.parametrize("timing", ["unspecified", "equivalent_labels", "conflicting_dates"])
def test_merge_retains_portal_locator_for_reconciliation_and_reingestion(jobcockpit_settings_factory, allow_public_dns, timing):
    """Observed official/Indeed duplicate: moved registry rows alone lost the alias."""
    from hashlib import sha256
    from jobcockpit.api.job_merge import merge_jobs_for_authoritative_identity
    from jobcockpit.ingestion.source_reconciliation import _candidate_already_saved
    from jobcockpit.services.job_deduplication import find_portal_identity_duplicate

    allow_public_dns({"careers.celestica.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    official = "https://careers.celestica.com/job/Example-Intern/1234567890/"
    portal = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
    with session_scope(settings) as db:
        request, parsed = posting(settings, official)
        if timing != "unspecified":
            parsed.employment_timing.start_date = "2027-01-01"
            parsed.employment_timing.end_date = "2027-08-31"
            parsed.employment_timing.duration_months = 8
            parsed.employment_timing.raw_label = "Jan 2027 to Aug 2027"
        canonical = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        request, parsed = posting(settings, portal)
        if timing != "unspecified":
            parsed.employment_timing.start_date = "2027-01-01"
            parsed.employment_timing.end_date = "2027-08-31" if timing == "equivalent_labels" else "2027-04-30"
            parsed.employment_timing.duration_months = 8 if timing == "equivalent_labels" else 4
            parsed.employment_timing.raw_label = "Winter 2027"
        parsed.location.city = "Montreal"  # Deliberately seed historical rows without auto-deduplication.
        duplicate = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert duplicate.created
        canonical_job = db.get(JobPosting, canonical.payload["id"])
        duplicate_job = db.get(JobPosting, duplicate.payload["id"])
        parsed.location.city = "Toronto"
        duplicate_job.location = parsed.location.model_dump(mode="json")
        duplicate_job.source = {**duplicate_job.source, "portal_source_urls": [portal]}
        db.flush()
        merge_jobs_for_authoritative_identity(canonical_job, {duplicate_job.id}, db,
            provider="verified_requisition", identity_hash=sha256(b"minimized observed duplicate").hexdigest(),
            identity_strength="authoritative")
        # The worker revalidates registry hits against retained locators.
        other = SimpleNamespace(id="another_job")
        assert _candidate_already_saved(db, other, portal)
        match = find_portal_identity_duplicate(request, parsed, db)
        if timing == "conflicting_dates":
            assert match is None
        else:
            assert match.id == canonical_job.id
        assert canonical_job.source["portal_source_urls"] == [portal]


def test_batch_merge_deduplicates_shared_pending_source_identities(jobcockpit_settings_factory, allow_public_dns, monkeypatch):
    """Observed AMD group: several historical copies share a portal identity."""
    from hashlib import sha256
    from jobcockpit.api.job_merge import merge_jobs_for_authoritative_identity
    from jobcockpit.api.page_capture_runs import sync_job_source_identities
    from jobcockpit.db.models import JobSourceIdentity

    allow_public_dns({"careers.amd.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    # Seed historical rows that existed before the AMD identity adapter.
    monkeypatch.setattr(workflows, "find_duplicate_job_posting", lambda *args, **kwargs: (None, None))
    urls = ["https://careers.amd.com/careers-home/jobs/1234",
            "https://careers.amd.com/careers-home/jobs/1234?icims=1",
            "https://careers.amd.com/students/jobs/1234?lang=en-us"]
    with session_scope(settings) as db:
        jobs = []
        for url in urls:
            request, parsed = posting(settings, url)
            result = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
            jobs.append(db.get(JobPosting, result.payload["id"]))
        for job in jobs[1:]:
            job.source = {**job.source, "portal_source_urls": ["https://www.linkedin.com/jobs/view/123456"]}
            sync_job_source_identities(db, job)
        db.flush()
        merge_jobs_for_authoritative_identity(jobs[0], {job.id for job in jobs[1:]}, db,
            provider="careers.amd.com", identity_hash=sha256(b"1234").hexdigest(), identity_strength="authoritative")
        assert all(job.source["merged_into_job_id"] == jobs[0].id for job in jobs[1:])
        portal_rows = db.scalars(select(JobSourceIdentity).where(JobSourceIdentity.provider == "linkedin")).all()
        assert len(portal_rows) == 1 and portal_rows[0].job_id == jobs[0].id


@pytest.mark.parametrize("urls", OFFICIAL_VARIANTS)
def test_authoritative_posting_identity_survives_url_and_parsed_identity_variations(
    jobcockpit_settings_factory, allow_public_dns, urls,
):
    from urllib.parse import urlparse
    allow_public_dns({urlparse(url).hostname for url in urls})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    results = []
    for index, url in enumerate(urls):
        request, parsed = posting(settings, url, company="Example Inc." if index else "Example",
                                  title="Stagiaire logiciel" if index else "Software Engineering Co-op")
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ))
    assert [result.created for result in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]


@pytest.mark.parametrize("blocked", ["different_id", "unsafe", "merged", "generic"])
def test_identity_reuse_does_not_collapse_distinct_or_unsafe_jobs(
    jobcockpit_settings_factory, allow_public_dns, blocked,
):
    allow_public_dns({"careers.planview.com", "careers.example.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    url = "https://careers.planview.com/jobs/1234/job"
    if blocked == "generic":
        url = "https://careers.example.com/apply"
    request, parsed = posting(settings, url)
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        if blocked == "merged":
            job = db.get(JobPosting, first.payload["id"])
            job.source = {**job.source, "merged_into_job_id": "another_job"}
    request, parsed = posting(settings, url.replace("1234", "9999") if blocked == "different_id" else url,
                              company="Other Employer" if blocked == "generic" else "Example")
    request.raw_text += "\nFresh source snapshot."
    request.source_url_dedup_safe = blocked != "unsafe"
    with session_scope(settings) as db:
        second = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
    assert second.created


def test_observed_verification_redirect_is_rejected_before_fetch(allow_public_dns):
    allow_public_dns({"cgi.njoyn.com", "validate.perfdrive.com"})
    seen = []
    def handle(request):
        seen.append(request.url.host)
        if request.url.host == "cgi.njoyn.com":
            return httpx.Response(302, headers={"Location": "https://validate.perfdrive.com/?ssa=private-session"})
        return httpx.Response(200, text="<h1>We apologize for the inconvenience...</h1><p>Please verify you are human.</p>")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        resolver = AtsApplyPageResolver(http_client=client)
        with pytest.raises(AtsProviderError, match="verification"):
            resolver.resolve("https://cgi.njoyn.com/Corp/xweb/XWeb.asp?Jobid=J0127-1234",
                             AtsPostingContext(company="Example", role="Software Co-op"))
    assert seen == ["cgi.njoyn.com"]


def test_caller_supplied_challenge_snapshot_cannot_reach_persistence(
    jobcockpit_settings_factory, allow_public_dns,
):
    allow_public_dns("validate.perfdrive.com")
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://validate.perfdrive.com/?ssa=private-session")
    request.raw_text = "We apologize for the inconvenience...\nPlease verify you are human."
    with session_scope(settings) as db:
        with pytest.raises(HTTPException, match="verification"):
            workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert not db.scalars(select(JobPosting)).all()
        assert not db.scalars(select(StoredArtifact)).all()


@pytest.mark.parametrize("conflict", [None, "manual_override", "changed_destination", "failed_link", "different_official"])
def test_legacy_email_alias_requires_unchanged_successful_destination(
    jobcockpit_settings_factory, allow_public_dns, conflict,
):
    allow_public_dns({"jobs.lever.co", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    official = "https://jobs.lever.co/example/12345678-1234-4234-8234-123456789012"
    portal = "https://www.linkedin.com/jobs/view/123456"
    request, parsed = posting(settings, official)
    request.source_resolution = {"kind": "official_discovery", "official_url": official}
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        db.add(JobAlertSource(job_alert_source_id="source", candidate_id="candidate", display_name="Alerts", gmail_query="query"))
        db.flush()
        db.add(EmailIngestionRun(email_ingestion_run_id="run", job_alert_source_id="source", query_used="query"))
        db.flush()
        db.add(JobAlertEmail(job_alert_email_id="email", email_ingestion_run_id="run", provider_message_id_hash="hash",
                             provider_thread_id_hash="hash", subject="Alert", sender_email_hash="hash",
                             sender_domain="example.test", received_at="2026-09-01", body_content_hash="hash"))
        db.flush()
        db.add(JobAlertLink(job_alert_link_id="link", job_alert_email_id="email", original_url=portal,
                           resolved_url=official, canonical_url=official, link_text="Intern", raw_text="",
                           surrounding_text="", classification="direct_posting", confidence=95,
                           ingestion_status="failed" if conflict == "failed_link" else "imported", linked_job_id=first.payload["id"]))
        job = db.get(JobPosting, first.payload["id"])
        if conflict == "manual_override":
            job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
        elif conflict == "changed_destination":
            job.source_url = official.replace("123456789012", "999999999999")
    request, parsed = posting(settings, portal)
    parsed.employment_timing.raw_label = "4 month assignment"  # provider wording, not a new identity
    if conflict == "different_official":
        request.portal_source_url = portal
        request.source_url = official.replace("123456789012", "999999999999")
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert result.created is (conflict is not None)
        if conflict is None:
            assert result.payload["id"] == first.payload["id"]
            assert portal in result.payload["source"]["portal_source_urls"]


def test_authoritative_duplicate_retains_new_portal_alias(jobcockpit_settings_factory, allow_public_dns):
    allow_public_dns({"careers.planview.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://careers.planview.com/jobs/1234/job")
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
    request, parsed = posting(settings, "https://careers.planview.com/jobs/1234?lang=en-us")
    request.portal_source_url = "https://www.linkedin.com/jobs/view/123456"
    with session_scope(settings) as db:
        second = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert second.payload["id"] == first.payload["id"]
        assert request.portal_source_url in second.payload["source"]["portal_source_urls"]


def test_direct_redirect_preserves_original_posting_identity(allow_public_dns):
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion.ats_adapters.base import AtsJob
    from jobcockpit.ingestion.posting_links import PostingLinkContext, prepare_posting_link

    allow_public_dns("careers.planview.com")
    original = "https://careers.planview.com/jobs/1234/job"
    result = prepare_posting_link(
        PostingLinkContext(source_type="gmail_alert", source_url=original, raw_text="Software Intern",
                           fetch_source_archive=True), settings=Settings(),
        posting_fetcher=SimpleNamespace(resolve=lambda *args, **kwargs: AtsJob(source_url=original + "?lang=en-us", raw_text="Software Intern\nBuild tools.")),
    )
    assert result.ingest_request.portal_source_url == original


def test_verification_failure_does_not_fall_back_to_email_title(allow_public_dns):
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion.ingestion_identity import PostingVerificationRequired
    from jobcockpit.ingestion.posting_links import PostingLinkContext, prepare_posting_link

    allow_public_dns("cgi.njoyn.com")
    def resolve(*args, **kwargs):
        raise PostingVerificationRequired()
    with pytest.raises(PostingVerificationRequired):
        prepare_posting_link(
            PostingLinkContext(source_type="gmail_alert", source_url="https://cgi.njoyn.com/Corp/xweb/XWeb.asp?Jobid=J0127-1234",
                               raw_text="Software Intern\nCompany: Example", fetch_source_archive=True),
            settings=Settings(), posting_fetcher=SimpleNamespace(resolve=resolve),
        )


# Minimized duties from the observed cross-board quality/data analyst posting.
QUALITY_DUTIES = (
    "Join the production part approval team and support products moving into production. "
    "Prepare documented results and coordinate submissions for assigned business units. "
    "Support engineering, manufacturing and sales teams with required document elements. "
    "Organize coordination meetings and maintain the current status of submissions. "
    "Update documents when product changes or customer requests require new information. "
    "Communicate pending changes and submission schedules to customer contacts. "
    "Collaborate with quality and production teams to meet agreed deadlines. "
    "Gather material declaration data and support engineering groups with document updates. "
    "Use spreadsheets, reporting dashboards and relational databases to analyze results. "
    "Bring strong organizational skills, analytical thinking and clear written communication. "
    "Manage multiple responsibilities independently while working closely with other teams. "
    "Students studying computer science, industrial engineering or business administration "
    "will gain practical experience with reporting tools and cross functional coordination."
)


@pytest.mark.parametrize("conflict", [None, "requisition", "company", "title", "term", "artifact_missing", "multiple_ids"])
def test_shared_labeled_requisition_uses_original_evidence_not_provider_summary(
    jobcockpit_settings_factory, allow_public_dns, conflict,
):
    allow_public_dns({"www.linkedin.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://www.linkedin.com/jobs/view/123456", company="Example Quality, Inc.")
    request.raw_text = "Requisition ID: 12345\n" + QUALITY_DUTIES
    parsed.role.description_raw = "Provider summary omits the original requisition."
    parsed.role.description_clean = parsed.role.description_raw
    parsed.employment_timing.start_date = "2027-01-01"
    parsed.employment_timing.duration_months = 12
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        if conflict == "artifact_missing":
            artifact = db.get(StoredArtifact, db.get(JobPosting, first.payload["id"]).raw_content_artifact_id)
            artifact.sha256 = "wrong-checksum"
    request, parsed = posting(settings, "https://ca.indeed.com/viewjob?jk=0123456789abcdef", company="Example Quality")
    request.raw_text = "Requisition ID: " + ("99999" if conflict == "requisition" else "12345") + "\n" + QUALITY_DUTIES
    if conflict == "company":
        parsed.employer.company_name = "Other Quality"
    elif conflict == "title":
        parsed.role.job_title = "Different Role"
        parsed.role.normalized_title = "different_role"
    elif conflict == "term":
        parsed.employment_timing.start_date = "2028-01-01"
    elif conflict == "multiple_ids":
        request.raw_text += "\nRequisition ID: 99999"
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert result.created is (conflict is not None)
        if conflict is None:
            assert result.payload["id"] == first.payload["id"]
            assert result.payload["employment_timing"]["duration_months"] == 12


@pytest.mark.parametrize("refresh", [False, True])
def test_verified_source_promotion_retains_portal_identity(jobcockpit_settings_factory, allow_public_dns, refresh):
    allow_public_dns({"www.linkedin.com", "careers.planview.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    portal = "https://www.linkedin.com/jobs/view/123456"
    official = "https://careers.planview.com/careers-home/jobs/1234"
    request, parsed = posting(settings, portal)
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        job = db.get(JobPosting, first.payload["id"])
        resolution = {"kind": "official_discovery", "official_url": official}
        if refresh:
            incoming, fields = posting(settings, official)
            incoming.source_resolution = resolution
            workflows.update_existing_job_from_ingest_payload(job, incoming, db, storage, settings,
                                                             parsed_job=fields, run_fit_analysis=False)
        else:
            workflows.apply_job_source_url(job, official, resolution, db)
        assert portal in job.source["portal_source_urls"]
    request.raw_text += "\nRefetched listing."
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert not result.created
        assert result.payload["id"] == first.payload["id"]


def test_source_reconciliation_detects_equivalent_employer_identity(jobcockpit_settings_factory, allow_public_dns):
    from jobcockpit.ingestion.source_reconciliation import _candidate_already_saved
    allow_public_dns("careers.planview.com")
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://careers.planview.com/careers-home/jobs/1234/job")
    with session_scope(settings) as db:
        workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert _candidate_already_saved(db, SimpleNamespace(id="different-job"),
                                       "https://careers.planview.com/careers-home/jobs/1234?lang=en-us")


def test_explicit_portal_identity_is_not_lost_when_old_employer_is_missing(jobcockpit_settings_factory, allow_public_dns):
    allow_public_dns({"jobs.teck.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    portal = "https://ca.indeed.com/viewjob?jk=0123456789abcdef"
    request, parsed = posting(settings, "https://jobs.teck.com/job/Intern/12345-en_US/")
    request.portal_source_url = portal
    parsed.employer.company_name = None
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
    request, parsed = posting(settings, portal)
    with session_scope(settings) as db:
        second = workflows.persist_job_ingest_result(request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False)
        assert not second.created
        assert second.payload["id"] == first.payload["id"]


@pytest.mark.parametrize("url", [
    "https://careers.planview.com.evil.test/jobs/1234",
    "https://careers.planview.com/jobs?job=1234",
    "https://careers.planview.com/jobs/1234/apply",
    "https://careers.planview.com:8443/jobs/1234",
    "https://user:@careers.planview.com/jobs/1234",
    "https://careers.example.com/jobs/1234",
    "https://jobs.ericsson.com/careers/jobs/123456789",
    "https://cgi.njoyn.com/Corp/xweb/XWeb.asp?Jobid=J0127-1234&Jobid=J0127-9999",
])
def test_employer_identity_rules_do_not_trust_lookalike_or_ambiguous_routes(url):
    from jobcockpit.ingestion.ingestion_identity import authoritative_ingestion_identity
    assert authoritative_ingestion_identity(url) is None
