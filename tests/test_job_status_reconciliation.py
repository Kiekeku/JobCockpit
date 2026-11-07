from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from jobcockpit import cli

from jobcockpit.api.main import create_app
from jobcockpit.ingestion import status_reconciliation as reconciliation
from jobcockpit.ingestion.browser_cleanup import BrowserPageCleanupLease
from jobcockpit.ingestion.posting_discovery import ChromeCdpRenderedPageResolver
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    AuditLogEntry,
    JobPosting,
    JobStatusCheck,
    JobStatusScanRun,
    active_validation_state,
)
from jobcockpit.db.session import get_engine, init_database, session_scope
from jobcockpit.ingestion.status_reconciliation import (
    RenderRequired,
    StatusCheckDecision,
    StatusScanAlreadyRunningError,
    classify_http_status,
    infer_status_from_page,
    ordered_status_urls,
    retry_delay,
    run_status_reconciliation,
    status_report,
)
from jobcockpit.status_evidence import status_evidence_category


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
        fit_analysis_provider="disabled",
    )


def ingest_job(client: TestClient, *, source_url: str, company: str = "Acme") -> dict:
    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": f"Platform Engineer\nCompany: {company}\nRemote Canada",
            "source_url": source_url,
        },
    )
    assert response.status_code == 201
    job = response.json()
    assert job["status_validation"] == "unverified_active"
    return job


def test_active_validation_requires_a_recent_open_observation():
    now = datetime(2026, 8, 20, 18, tzinfo=UTC)

    assert active_validation_state("closed", "open", now, now=now) == "not_active"
    assert active_validation_state("active", None, None, now=now) == "unverified_active"
    assert (
        active_validation_state(
            "active",
            "inconclusive",
            now - timedelta(hours=1),
            now=now,
        )
        == "unverified_active"
    )
    assert (
        active_validation_state(
            "active",
            "open",
            now - timedelta(days=7, seconds=1),
            now=now,
        )
        == "unverified_active"
    )
    assert (
        active_validation_state(
            "active",
            "open",
            now - timedelta(days=7),
            now=now,
        )
        == "confirmed_open"
    )


def test_stale_open_job_api_exposes_stale_verification_category():
    checked_at = datetime.now(UTC) - timedelta(days=8)
    job = JobPosting(
        id="job_stale_open",
        source_type="url",
        posting_snapshot_hash="hash-stale-open",
        raw_content_artifact_id="artifact-stale-open",
        status="active",
        status_last_checked_at=checked_at,
        status_check_result="open",
        status_check_evidence_code="posting_identity_match",
        status_check_failure_count=0,
        source={},
        employer={},
        role={},
        employment_timing={},
        location={},
        eligibility={},
        compensation={},
        application={},
        fit_analysis={},
        data_quality={},
        created_at=checked_at,
        updated_at=checked_at,
    )

    payload = job.to_api()

    assert payload["status_validation"] == "unverified_active"
    assert payload["status_check"]["category"] == "stale"
    assert payload["status_check"]["category_label"] == "Open confirmation is stale"


@pytest.mark.parametrize(
    ("result", "evidence_code", "expected"),
    [
        ("inconclusive", "job_identity_mismatch", "identity"),
        ("inconclusive", "authentication_wall", "access"),
        ("inconclusive", "authentication_required", "access"),
        ("inconclusive", "response_too_large", "response_limit"),
        ("inconclusive", "direct_fetch_failed", "network"),
        ("unsupported", "stored_url_not_job_detail", "unsupported"),
        ("open", "posting_identity_match", "confirmed"),
        ("closed", "ats_no_longer_available", "terminal"),
    ],
)
def test_status_evidence_categories_are_stable(result, evidence_code, expected):
    assert status_evidence_category(result, evidence_code) == expected


def test_public_ashby_feed_binds_exact_posting_id(monkeypatch):
    source_url = "https://jobs.ashbyhq.com/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "ashby")
    target = reconciliation.JobStatusTarget(
        job_id="job_ashby_feed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda url, *_args, **kwargs: (
            httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": "8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
                            "title": "Platform Engineer",
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            ),
            url,
        ),
    )

    decision = reconciliation._public_provider_feed_check(target, candidate, Settings())

    assert decision.result == "open"
    assert decision.evidence_code == "ashby_public_feed_match"


def test_public_provider_feed_cache_coalesces_concurrent_board_requests(monkeypatch):
    posting_ids = (
        "8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        "54a6ab62-d21d-4c9a-ac62-aef79653b39f",
    )
    candidates = tuple(
        reconciliation.StatusUrlCandidate(
            f"https://jobs.ashbyhq.com/acme/{posting_id}",
            "official_ats",
            "ashby",
        )
        for posting_id in posting_ids
    )
    targets = tuple(
        reconciliation.JobStatusTarget(
            job_id=f"job_ashby_cache_{index}",
            status="active",
            source_type="gmail_alert",
            source_url=candidate.url,
            company="Acme",
            title=f"Platform Engineer {index}",
            location="Canada",
            candidates=(candidate,),
            failure_count=0,
            not_found_count=0,
            last_checked_at=None,
        )
        for index, candidate in enumerate(candidates)
    )
    request_count = 0
    request_count_lock = threading.Lock()

    def feed_response(url, *_args, **_kwargs):
        nonlocal request_count
        with request_count_lock:
            request_count += 1
        time.sleep(0.05)
        return (
            httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": posting_ids[0],
                            "title": "Platform Engineer 0",
                            "descriptionHtml": "x" * 100_000,
                        },
                        {
                            "id": posting_ids[1],
                            "title": "Platform Engineer 1",
                            "descriptionHtml": "y" * 100_000,
                        },
                    ]
                },
                request=httpx.Request("GET", url),
            ),
            url,
        )

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", feed_response)
    feed_cache = reconciliation._ProviderFeedCache()
    start = threading.Barrier(3)
    decisions = []

    def check(target, candidate):
        start.wait()
        decisions.append(
            reconciliation._public_provider_feed_check(
                target,
                candidate,
                Settings(),
                feed_cache=feed_cache,
            )
        )

    threads = [
        threading.Thread(target=check, args=(target, candidate))
        for target, candidate in zip(targets, candidates, strict=True)
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert request_count == 1
    assert len(decisions) == 2
    assert all(decision.result == "open" for decision in decisions)
    assert feed_cache.get(
        "https://api.ashbyhq.com/posting-api/job-board/acme"
    ) == {
        posting_ids[0]: "Platform Engineer 0",
        posting_ids[1]: "Platform Engineer 1",
    }


def test_public_provider_feed_with_missing_entry_identity_is_inconclusive_and_uncached(
    monkeypatch,
):
    source_url = "https://acme.recruitee.com/o/platform-engineer"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "recruitee")
    target = reconciliation.JobStatusTarget(
        job_id="job_recruitee_partial_feed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    feed_url = "https://acme.recruitee.com/api/offers"
    feed_cache = reconciliation._ProviderFeedCache()
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda url, *_args, **_kwargs: (
            httpx.Response(
                200,
                json={
                    "offers": [
                        {"slug": "different-role", "title": "Different Role"},
                        {"title": "Platform Engineer"},
                    ]
                },
                request=httpx.Request("GET", url),
            ),
            url,
        ),
    )

    decision = reconciliation._public_provider_feed_check(
        target,
        candidate,
        Settings(),
        feed_cache=feed_cache,
    )

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "provider_feed_identity_missing"
    assert decision.retryable is True
    assert feed_cache.get(feed_url) is None


def test_public_recruitee_feed_uses_bounded_provider_cap_and_missing_slug_is_closed(
    monkeypatch,
):
    source_url = "https://acme.recruitee.com/o/platform-engineer"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "recruitee")
    target = reconciliation.JobStatusTarget(
        job_id="job_recruitee_feed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    observed_caps: list[int] = []

    def feed_response(url, *_args, **kwargs):
        observed_caps.append(kwargs["max_response_bytes"])
        return (
            httpx.Response(
                200,
                json={"offers": [{"slug": "different-role", "title": "Different Role"}]},
                request=httpx.Request("GET", url),
            ),
            url,
        )

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", feed_response)

    decision = reconciliation._public_provider_feed_check(target, candidate, Settings())

    assert observed_caps == [reconciliation._MAX_PROVIDER_FEED_RESPONSE_BYTES]
    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "ats_no_longer_available"


def test_public_oracle_detail_empty_payload_is_closed(monkeypatch):
    source_url = (
        "https://acme.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/"
        "en/sites/CX_1/job/35268"
    )
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "oracle")
    target = reconciliation.JobStatusTarget(
        job_id="job_oracle_removed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda url, *_args, **_kwargs: (
            httpx.Response(
                200,
                json={"items": [], "count": 0},
                request=httpx.Request("GET", url),
            ),
            url,
        ),
    )

    decision = reconciliation._oracle_public_detail_check(target, candidate, Settings())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "ats_no_longer_available"


def test_public_oracle_detail_does_not_close_when_an_id_variant_is_unresolved(
    monkeypatch,
):
    source_url = (
        "https://acme.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/"
        "en/sites/CX_1/job/35268"
    )
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "oracle")
    target = reconciliation.JobStatusTarget(
        job_id="job_oracle_unresolved_variant",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    def detail_response(url, *_args, **_kwargs):
        response = (
            httpx.Response(503, request=httpx.Request("GET", url))
            if 'Id="J35268"' in url
            else httpx.Response(
                200,
                json={"items": [], "count": 0},
                request=httpx.Request("GET", url),
            )
        )
        return response, url

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", detail_response)

    decision = reconciliation._oracle_public_detail_check(target, candidate, Settings())

    assert decision is None


def test_bamboohr_detail_redirect_to_same_board_root_is_closed(monkeypatch):
    source_url = "https://acme.bamboohr.com/careers/123"
    final_url = "https://acme.bamboohr.com/careers"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "bamboohr")
    target = reconciliation.JobStatusTarget(
        job_id="job_bamboohr_removed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (
            httpx.Response(
                200,
                text="<html><body><h1>Careers at Acme</h1></body></html>",
                request=httpx.Request("GET", final_url),
            ),
            final_url,
        ),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "ats_no_longer_available"


def test_bamboohr_detail_uses_open_graph_job_identity(monkeypatch):
    source_url = "https://sanjel.bamboohr.com/careers/237"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "bamboohr")
    target = reconciliation.JobStatusTarget(
        job_id="job_bamboohr_open_graph",
        status="active",
        source_type="ats_eluta",
        source_url=source_url,
        company="Sanjel Corporation",
        title="Application Developer (Co-op Student)",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    html = """
        <html>
          <head>
            <title>BambooHR</title>
            <meta property="og:title" content="Application Developer (Co-op Student)">
            <meta property="og:site_name" content="Sanjel Energy Services Inc">
          </head>
          <body></body>
        </html>
    """
    response = httpx.Response(
        200,
        text=html,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        request=httpx.Request("GET", source_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "open"
    assert decision.evidence_code == "posting_identity_match"


def test_bamboohr_open_graph_does_not_override_conflicting_json_ld(monkeypatch):
    source_url = "https://sanjel.bamboohr.com/careers/237"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "bamboohr")
    target = reconciliation.JobStatusTarget(
        job_id="job_bamboohr_conflicting_metadata",
        status="active",
        source_type="ats_eluta",
        source_url=source_url,
        company="Sanjel Corporation",
        title="Application Developer (Co-op Student)",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    html = """
        <html>
          <head>
            <meta property="og:title" content="Application Developer (Co-op Student)">
            <meta property="og:site_name" content="Sanjel Energy Services Inc">
            <script type="application/ld+json">
              {
                "@type": "JobPosting",
                "title": "Senior Accountant",
                "hiringOrganization": {"name": "Different Corporation"}
              }
            </script>
          </head>
          <body></body>
        </html>
    """
    response = httpx.Response(
        200,
        text=html,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        request=httpx.Request("GET", source_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "job_identity_mismatch"


def test_identity_matching_normalizes_punctuation_and_company_suffixes():
    assert reconciliation._identity_values_match(
        "TechInsights Inc.",
        "Circuit Analysis Co-op Student - Fall 2026",
        "Circuit Analysis Co-op Student — Fall 2026 | Careers at TechInsights",
    )
    assert not reconciliation._identity_values_match(
        "Different Company Inc.",
        "Circuit Analysis Co-op Student - Fall 2026",
        "Circuit Analysis Co-op Student — Fall 2026 | Careers at TechInsights",
    )


@pytest.mark.parametrize(
    ("expected", "returned"),
    [
        ("CMOS Co-op", "CMOS Coop"),
        ("Software Developer Intern", "Software Developer Internship"),
    ],
)
def test_title_identity_matching_normalizes_role_term_variants(expected, returned):
    assert reconciliation._title_values_match(expected, returned)
    assert reconciliation._identity_values_match(
        "Acme",
        expected,
        f"{returned} | Careers at Acme",
    )


@pytest.mark.parametrize("suffix", ["LLP", "LLC", "LP", "PLC"])
def test_identity_matching_ignores_common_partnership_and_company_suffixes(suffix):
    title = "Strategy and Business Design - Summer 2027 Co-op/Intern"

    assert reconciliation._identity_values_match(
        f"Deloitte {suffix}",
        title,
        f"{title} Job Details | Deloitte",
    )
    assert reconciliation._structured_identity_values_match(
        f"Deloitte {suffix}",
        title,
        returned_company="Deloitte",
        returned_title=title,
    )


@pytest.mark.parametrize("suffix", ["L.L.P.", "L.L.C.", "P.L.C."])
def test_identity_matching_ignores_dotted_corporate_suffixes(suffix):
    assert reconciliation._normalized_company_identity(f"Deloitte {suffix}") == "deloitte"


def test_company_identity_preserves_legal_term_tokens_inside_a_brand_name():
    assert (
        reconciliation._normalized_company_identity("LP Building Solutions")
        == "lp building solutions"
    )
    assert not reconciliation._structured_identity_values_match(
        "LP Building Solutions",
        "Software Engineer",
        returned_company="Building Solutions",
        returned_title="Software Engineer",
    )


def test_structured_identity_rejects_conflicting_numeric_title_tokens():
    assert not reconciliation._structured_identity_values_match(
        "Acme",
        "Data Scientist Intern 8 Months",
        returned_company="Acme Inc.",
        returned_title="Data Scientist Intern 4 Months",
    )


def test_generic_html_uses_job_posting_json_ld_for_identity(monkeypatch):
    source_url = "https://jobs.example.com/ca/en/job/123/data-scientist-intern"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "generic")
    target = reconciliation.JobStatusTarget(
        job_id="job_json_ld_identity",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Royal Bank of Canada",
        title="2027 Winter - GRM, Data Scientist Intern (8 Months)",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    html = """
        <html>
          <head>
            <title>2027 Winter - GRM, Data Scientist Intern (8 Months) | Jobs at RBC</title>
            <script type="application/ld+json">
              {
                "@type": "JobPosting",
                "title": "2027 Winter - GRM, Data Scientist Intern (8 Months)",
                "hiringOrganization": {"name": "Royal Bank of Canada"}
              }
            </script>
          </head>
          <body></body>
        </html>
    """
    response = httpx.Response(
        200,
        text=html,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        request=httpx.Request("GET", source_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "open"
    assert decision.evidence_code == "posting_identity_match"


def test_generic_html_does_not_mix_json_ld_title_and_company_identity(monkeypatch):
    source_url = "https://jobs.example.com/job/123/platform-engineer"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "generic")
    target = reconciliation.JobStatusTarget(
        job_id="job_json_ld_cross_field_identity",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Acme Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    html = """
        <html>
          <head>
            <title>Platform Engineer | Acme Careers</title>
            <script type="application/ld+json">
              {
                "@type": "JobPosting",
                "title": "Acme Platform Engineer",
                "hiringOrganization": {"name": "Different Corporation"}
              }
            </script>
          </head>
          <body></body>
        </html>
    """
    response = httpx.Response(
        200,
        text=html,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        request=httpx.Request("GET", source_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "job_identity_mismatch"


def test_structured_workday_identity_allows_equivalent_title_punctuation():
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Forensic-Disputes-Investigation_JR6569-2"
    )
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "workday")
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_title_normalization",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title=(
            "Co-op Intern Forensic Disputes Investigation FDI Calgary or Vancouver "
            "January April 2027"
        ),
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    job = SimpleNamespace(
        provider_job_id="JR6569",
        title=(
            "Co-op/Intern – Forensic Disputes & Investigation (FDI) Calgary or "
            "Vancouver | January – April 2027"
        ),
    )

    assert reconciliation._structured_workday_identity_matches(target, candidate, job)


def test_source_markers_map_to_closed_or_expired_only_for_expected_hosts():
    linkedin = infer_status_from_page(
        "https://www.linkedin.com/jobs/view/123",
        "Platform Engineer\nNo longer accepting applications",
    )
    indeed = infer_status_from_page(
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
        "Platform Engineer\nThis job has expired on Indeed",
    )
    workday = infer_status_from_page(
        "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Platform-Engineer_R123",
        "The job posting is no longer available.",
    )

    assert (linkedin.detected_status, linkedin.evidence_code) == (
        "closed",
        "linkedin_no_longer_accepting",
    )
    assert (indeed.detected_status, indeed.evidence_code) == (
        "expired",
        "indeed_job_expired",
    )
    assert (workday.detected_status, workday.evidence_code) == (
        "closed",
        "ats_no_longer_available",
    )
    assert (
        infer_status_from_page(
            "https://example.com/jobs/123",
            "This job has expired on Indeed",
        )
        is None
    )
    assert (
        infer_status_from_page(
            "https://www.linkedin.com/jobs/view/123",
            "The page you are looking for does not exist",
        )
        is None
    )
    assert (
        infer_status_from_page(
            "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            "The job posting is no longer available",
        )
        is None
    )


@pytest.mark.parametrize(
    "source_url",
    [
        "https://notgreenhouse.io/jobs/1234567",
        "https://jobs.lever.co/acme/search",
        "https://jobs.ashbyhq.com/acme/search",
        "https://job-boards.greenhouse.io/acme/jobs/search",
        "https://example.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/search",
    ],
)
def test_unvalidated_ats_urls_never_produce_definitive_closure(source_url: str):
    identity = reconciliation.posting_identity(source_url)
    assert identity.is_detail is False
    assert (
        infer_status_from_page(
            source_url,
            "The job posting is no longer available. The page you are looking for does not exist.",
        )
        is None
    )

    decision = classify_http_status(
        404,
        source_kind=identity.source_kind,
        generic_404_count=0,
        source_url=source_url,
    )
    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "generic_http_404_pending_confirmation"


@pytest.mark.parametrize(
    ("source_url", "source_kind"),
    [
        (
            "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Platform-Engineer_R123",
            "workday",
        ),
        ("https://job-boards.greenhouse.io/acme/jobs/1234567", "greenhouse"),
        (
            "https://jobs.lever.co/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
            "lever",
        ),
        (
            "https://jobs.ashbyhq.com/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
            "ashby",
        ),
        (
            "https://example.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123456",
            "oracle",
        ),
    ],
)
def test_validated_ats_detail_identity_allows_definitive_closure(
    source_url: str,
    source_kind: str,
):
    identity = reconciliation.posting_identity(source_url)
    assert identity.source_kind == source_kind
    assert identity.is_trusted_provider is True
    assert identity.is_detail is True
    assert identity.posting_id

    marker = infer_status_from_page(
        source_url,
        "The job posting is no longer available.",
    )
    assert marker is not None
    assert marker.result == "closed"

    missing = classify_http_status(
        404,
        source_kind=source_kind,
        generic_404_count=0,
        source_url=source_url,
    )
    assert missing.result == "closed"
    assert missing.evidence_code == "recognized_ats_detail_404"


def test_http_status_classification_never_treats_access_or_server_failures_as_closed():
    for status in (403, 429, 500, 503):
        result = classify_http_status(
            status,
            source_kind="workday",
            generic_404_count=0,
        )
        assert result.detected_status is None
        assert result.result == "inconclusive"

    assert (
        classify_http_status(
            410,
            source_kind="generic",
            generic_404_count=0,
        ).detected_status
        == "closed"
    )
    assert (
        classify_http_status(
            404,
            source_kind="workday",
            generic_404_count=0,
            source_url=(
                "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Platform-Engineer_R123"
            ),
        ).detected_status
        == "closed"
    )
    for source_kind, board_url in (
        ("workday", "https://example.wd5.myworkdayjobs.com/Careers"),
        ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs"),
        ("lever", "https://jobs.lever.co/acme"),
        ("oracle", "https://example.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job"),
        ("ashby", "https://jobs.ashbyhq.com/acme"),
    ):
        board_404 = classify_http_status(
            404,
            source_kind=source_kind,
            generic_404_count=0,
            source_url=board_url,
        )
        assert board_404.result == "inconclusive"
        assert board_404.evidence_code == "generic_http_404_pending_confirmation"
    assert (
        classify_http_status(
            404,
            source_kind="lever",
            generic_404_count=0,
            source_url="https://jobs.lever.co/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        ).evidence_code
        == "recognized_ats_detail_404"
    )
    first_404 = classify_http_status(
        404,
        source_kind="generic",
        generic_404_count=0,
    )
    second_404 = classify_http_status(
        404,
        source_kind="generic",
        generic_404_count=1,
    )
    assert first_404.detected_status is None
    assert first_404.evidence_code == "generic_http_404_pending_confirmation"
    assert second_404.detected_status == "closed"


def test_retry_backoff_and_stored_url_ordering():
    assert retry_delay("open", 99) == timedelta(hours=24)
    assert retry_delay("inconclusive", 0) == timedelta(hours=6)
    assert retry_delay("inconclusive", 1) == timedelta(hours=12)
    assert retry_delay("inconclusive", 2) == timedelta(hours=24)
    assert retry_delay("unsupported", 0) == timedelta(days=7)

    job = JobPosting(
        id="job_order",
        source_type="gmail_alert",
        source_url="https://www.linkedin.com/jobs/view/123",
        posting_snapshot_hash="a" * 64,
        raw_content_artifact_id="artifact_unused",
        company_name="Acme",
        normalized_title="Platform Engineer",
        source={
            "source_resolution": {
                "kind": "official_posting",
                "official_url": "https://acme.wd5.myworkdayjobs.com/Careers/job/x_R1",
                "fallback_url": "https://ca.indeed.com/viewjob?jk=abc",
            }
        },
        application={"external_apply_url": "https://jobs.acme.example/roles/1"},
    )

    assert [candidate.kind for candidate in ordered_status_urls(job)] == [
        "official_ats",
        "external_apply",
        "canonical_source",
        "aggregator_fallback",
    ]


@pytest.mark.parametrize(
    ("result", "detected_status"),
    [
        ("open", "closed"),
        ("inconclusive", "expired"),
        ("closed", None),
        ("closed", "expired"),
        ("expired", None),
        ("expired", "closed"),
    ],
)
def test_status_decision_rejects_impossible_terminal_state(result, detected_status):
    with pytest.raises(ValueError, match="detected_status"):
        StatusCheckDecision(
            result,
            "test_evidence",
            detected_status=detected_status,
        )


def test_reconciliation_closes_active_job_without_changing_application(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(client, source_url="https://www.linkedin.com/jobs/view/1234567890")
    application_response = client.post(f"/api/jobs/{created['id']}/applications")
    assert application_response.status_code == 201
    application_before = application_response.json()
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def closed_probe(_target, candidate):
        return StatusCheckDecision(
            result="closed",
            detected_status="closed",
            evidence_code="linkedin_no_longer_accepting",
            source_url=candidate.url,
            source_kind="linkedin",
            http_status=200,
            retry_after_seconds=172800,
        )

    summary = run_status_reconciliation(
        settings,
        limit=100,
        rendered_limit=30,
        max_runtime_seconds=60,
        checker=closed_probe,
        now=now,
    )

    assert summary["selected"] == 1
    assert summary["checked"] == 1
    assert summary["closed"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        application = db.execute(
            select(Application).where(Application.job_id == created["id"])
        ).scalar_one()
        check = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalar_one()
        audit = (
            db.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.entity_id == created["id"],
                    AuditLogEntry.event_type == "job.opportunity_status_updated",
                )
            )
            .scalars()
            .all()[-1]
        )
        assert job.status == "closed"
        assert job.status_next_check_at is None
        assert check.status_before == "active"
        assert check.status_after == "closed"
        assert application.to_api() == application_before
        assert audit.payload["source"] == "scheduled_status_check"

    detail = client.get(f"/api/jobs/{created['id']}").json()
    assert detail["status_validation"] == "not_active"
    assert detail["status_check"] == {
        "last_checked_at": now.isoformat(),
        "next_check_at": None,
        "result": "closed",
        "evidence_code": "linkedin_no_longer_accepting",
        "evidence_label": "LinkedIn no longer accepting applications",
        "category": "terminal",
        "category_label": "Closed or expired",
        "failure_count": 0,
    }


def test_dry_run_is_non_mutating_and_manual_reactivation_is_due(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(client, source_url="https://ca.indeed.com/viewjob?jk=abc123")

    def expired_probe(_target, candidate):
        return StatusCheckDecision(
            result="expired",
            detected_status="expired",
            evidence_code="indeed_job_expired",
            source_url=candidate.url,
            source_kind="indeed",
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        dry_run=True,
        checker=expired_probe,
        now=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    assert summary["expired"] == 1
    with session_scope(settings) as db:
        assert db.get(JobPosting, created["id"]).status == "active"
        assert db.execute(select(JobStatusCheck)).scalars().all() == []

    assert (
        client.patch(f"/api/jobs/{created['id']}/status", json={"status": "closed"}).status_code
        == 200
    )
    reactivated = client.patch(
        f"/api/jobs/{created['id']}/status",
        json={"status": "active"},
    ).json()
    assert reactivated["status_check"]["next_check_at"] is not None


def test_manual_reactivation_wins_over_an_in_flight_closed_result(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/2234567890",
    )
    run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    manual_update_at = run_at + timedelta(minutes=5)

    def close_after_manual_reactivation(target, candidate):
        with session_scope(settings) as db:
            job = db.get(JobPosting, target.job_id)
            job.status = "active"
            job.status_last_checked_at = None
            job.status_next_check_at = manual_update_at
            job.status_check_result = None
            job.status_check_evidence_code = None
            job.status_check_failure_count = 0
            job.status_check_not_found_count = 0
            job.updated_at = manual_update_at
        return StatusCheckDecision(
            "closed",
            "linkedin_no_longer_accepting",
            candidate.url,
            candidate.source_kind,
            detected_status="closed",
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        checker=close_after_manual_reactivation,
        now=run_at,
    )

    assert summary["selected"] == 1
    assert summary["checked"] == 1
    assert summary["closed"] == 0
    assert summary["skipped"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert job.status_last_checked_at is None
        assert _as_utc(job.status_next_check_at) == manual_update_at
        assert job.status_check_result is None
        assert job.status_check_evidence_code is None
        assert job.status_check_failure_count == 0
        assert job.status_check_not_found_count == 0
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_manual_update_committing_between_scan_read_and_write_wins_atomically(
    tmp_path: Path,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/3234567890",
    )
    run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    manual_update_at = run_at + timedelta(minutes=5)
    replacement_url = "https://www.linkedin.com/jobs/view/4234567890"
    engine = get_engine(settings)
    interleaved = False

    def interleave_manual_update(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        nonlocal interleaved
        normalized = " ".join(statement.casefold().split())
        if (
            interleaved
            or not normalized.startswith("update job_postings set")
            or "status_check_result" not in normalized
        ):
            return
        interleaved = True
        with session_scope(settings) as other_db:
            other_db.execute(
                update(JobPosting)
                .where(JobPosting.id == created["id"])
                .values(
                    status="active",
                    source_url=replacement_url,
                    status_last_checked_at=None,
                    status_next_check_at=manual_update_at,
                    status_check_result=None,
                    status_check_evidence_code=None,
                    status_check_failure_count=0,
                    status_check_not_found_count=0,
                    updated_at=manual_update_at,
                )
            )

    event.listen(engine, "before_cursor_execute", interleave_manual_update)
    try:
        summary = run_status_reconciliation(
            settings,
            checker=lambda _target, candidate: StatusCheckDecision(
                "closed",
                "linkedin_no_longer_accepting",
                candidate.url,
                candidate.source_kind,
                detected_status="closed",
                http_status=200,
            ),
            now=run_at,
        )
    finally:
        event.remove(engine, "before_cursor_execute", interleave_manual_update)

    assert interleaved is True
    assert summary["selected"] == 1
    assert summary["checked"] == 1
    assert summary["closed"] == 0
    assert summary["skipped"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert job.source_url == replacement_url
        assert _as_utc(job.status_next_check_at) == manual_update_at
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_rendered_budget_exhaustion_defers_without_failure(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/2345678901",
    )

    summary = run_status_reconciliation(
        settings,
        rendered_limit=0,
        max_runtime_seconds=60,
        now=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )

    assert summary["selected"] == 1
    assert summary["checked"] == 0
    assert summary["deferred"] == 1
    assert summary["failed"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert job.status_last_checked_at is None
        assert _as_utc(job.status_next_check_at) == datetime(2026, 7, 27, 14, tzinfo=UTC)
        assert job.status_check_failure_count == 0
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_budget_deferrals_yield_to_never_scanned_backlog_jobs(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created_ids = [
        ingest_job(
            client,
            source_url=f"https://www.linkedin.com/jobs/view/{3456789000 + index}",
            company=f"Backlog Company {index}",
        )["id"]
        for index in range(3)
    ]
    first_run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    first = run_status_reconciliation(
        settings,
        limit=2,
        rendered_limit=0,
        now=first_run_at,
    )
    second = run_status_reconciliation(
        settings,
        limit=2,
        rendered_limit=0,
        now=first_run_at + timedelta(hours=1),
    )

    assert first["selected"] == 2
    assert first["deferred"] == 2
    assert second["selected"] == 1
    assert second["deferred"] == 1
    with session_scope(settings) as db:
        jobs = {
            job.id: job
            for job in db.execute(
                select(JobPosting).where(JobPosting.id.in_(created_ids))
            ).scalars()
        }
        first_due_times = {_as_utc(jobs[job_id].status_next_check_at) for job_id in created_ids}
        assert first_run_at + timedelta(hours=2) in first_due_times
        assert first_run_at + timedelta(hours=3) in first_due_times


def test_reconcile_status_cli_emits_json_and_forwards_scheduler_defaults(
    tmp_path: Path, monkeypatch, capsys
):
    settings = make_settings(tmp_path)
    expected = {
        "run_id": "status_scan_123",
        "status": "completed",
        "selected": 3,
        "checked": 3,
        "open": 2,
        "closed": 1,
        "expired": 0,
        "inconclusive": 0,
        "failed": 0,
        "deferred": 0,
        "skipped": 0,
    }
    calls = []

    def fake_run(run_settings, **kwargs):
        calls.append((run_settings, kwargs))
        return expected

    monkeypatch.setattr(cli, "run_status_reconciliation", fake_run)
    exit_code = cli.main(["jobs", "reconcile-status", "--json"], settings=settings)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert calls == [
        (
            settings,
            {
                "limit": 100,
                "rendered_limit": 30,
                "max_runtime_seconds": 5400,
                "job_id": None,
                "force": False,
                "dry_run": False,
            },
        )
    ]


def test_reconcile_status_cli_failure_keeps_machine_readable_summary_shape(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = make_settings(tmp_path)

    def fail_run(*_args, **_kwargs):
        raise StatusScanAlreadyRunningError("scan already running")

    monkeypatch.setattr(cli, "run_status_reconciliation", fail_run)

    exit_code = cli.main(
        ["jobs", "reconcile-status", "--json"],
        settings=settings,
    )

    assert exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    payload = json.loads(output.err)
    assert payload == {
        "run_id": None,
        "status": "failed",
        "selected": 0,
        "checked": 0,
        "open": 0,
        "closed": 0,
        "expired": 0,
        "inconclusive": 0,
        "failed": 0,
        "deferred": 0,
        "skipped": 0,
        "error": "scan already running",
    }


def test_reconcile_status_cli_reports_and_persists_started_run_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    with TestClient(create_app(settings)) as client:
        created = ingest_job(
            client,
            source_url="https://www.linkedin.com/jobs/view/1234567890",
        )

    def direct_open(target, *_args, **_kwargs):
        candidate = target.candidates[0]
        return StatusCheckDecision(
            "open",
            "expected_posting_identity",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        )

    def fail_finish(*_args, **_kwargs):
        raise RuntimeError(
            "failed while finalizing https://careers.example.test/jobs/123?token=secret"
        )

    monkeypatch.setattr(reconciliation, "_direct_target_check", direct_open)
    monkeypatch.setattr(reconciliation, "_finish_run", fail_finish)

    exit_code = cli.main(
        [
            "jobs",
            "reconcile-status",
            "--json",
            "--job-id",
            created["id"],
        ],
        settings=settings,
    )

    assert exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    payload = json.loads(output.err)
    assert payload["run_id"].startswith("status_scan_")
    assert payload == {
        "run_id": payload["run_id"],
        "status": "failed",
        "selected": 1,
        "checked": 1,
        "open": 1,
        "closed": 0,
        "expired": 0,
        "inconclusive": 0,
        "failed": 0,
        "deferred": 0,
        "skipped": 0,
        "error": "failed while finalizing [url]",
    }

    report = status_report(settings, run_id=payload["run_id"])
    assert len(report["runs"]) == 1
    persisted = report["runs"][0]
    assert persisted["run_id"] == payload["run_id"]
    assert persisted["status"] == "failed"
    assert persisted["selected"] == 1
    assert persisted["checked"] == 1
    assert persisted["open"] == 1
    assert persisted["error_message"] == "failed while finalizing [url]"


def test_started_run_failure_never_reports_uncommitted_item_counts(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    with TestClient(create_app(settings)) as client:
        created = ingest_job(
            client,
            source_url="https://www.linkedin.com/jobs/view/1234567890",
        )

    def direct_open(target, *_args, **_kwargs):
        candidate = target.candidates[0]
        return StatusCheckDecision(
            "open",
            "expected_posting_identity",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        )

    def fail_item_persistence(*_args, **_kwargs):
        raise RuntimeError("simulated check insert failure")

    monkeypatch.setattr(reconciliation, "_direct_target_check", direct_open)
    monkeypatch.setattr(
        reconciliation,
        "_persist_decision",
        fail_item_persistence,
    )

    exit_code = cli.main(
        [
            "jobs",
            "reconcile-status",
            "--json",
            "--job-id",
            created["id"],
        ],
        settings=settings,
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["selected"] == 1
    assert payload["checked"] == 0
    assert payload["open"] == 0
    assert payload["error"] == "simulated check insert failure"

    with session_scope(settings) as db:
        run = db.get(JobStatusScanRun, payload["run_id"])
        job = db.get(JobPosting, created["id"])
        assert run.status == "failed"
        assert run.selected_count == 1
        assert run.checked_count == 0
        assert run.open_count == 0
        assert db.execute(select(JobStatusCheck)).scalars().all() == []
        assert job.status_last_checked_at is None


def test_reconcile_status_cli_sanitizes_pre_start_failure_urls(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = make_settings(tmp_path)

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("could not open https://example.test/?token=secret")

    monkeypatch.setattr(cli, "run_status_reconciliation", fail_run)

    exit_code = cli.main(["jobs", "reconcile-status", "--json"], settings=settings)

    assert exit_code == 1
    assert json.loads(capsys.readouterr().err)["error"] == "could not open [url]"


def test_status_report_cli_emits_json_for_invalid_limit(tmp_path: Path, capsys):
    settings = make_settings(tmp_path)

    exit_code = cli.main(
        ["jobs", "status-report", "--json", "--limit", "0"],
        settings=settings,
    )

    assert exit_code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "failed",
        "error": "--limit must be greater than zero",
    }


def test_status_report_cli_emits_json_for_query_failure(tmp_path: Path, monkeypatch, capsys):
    settings = make_settings(tmp_path)

    def fail_report(*_args, **_kwargs):
        raise RuntimeError("status schema missing")

    monkeypatch.setattr(cli, "status_report", fail_report)

    exit_code = cli.main(["jobs", "status-report", "--json"], settings=settings)

    assert exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "failed",
        "error": "status schema missing",
    }


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_delay"),
    (
        ("open", "active", timedelta(days=1)),
        ("inconclusive", "active", timedelta(days=10)),
        ("failed", "active", timedelta(days=10)),
        ("unsupported", "active", timedelta(days=7)),
        ("closed", "closed", None),
        ("expired", "expired", None),
    ),
)
def test_result_scheduling_follows_the_explicit_state_machine(
    tmp_path: Path,
    result: str,
    expected_status: str,
    expected_delay: timedelta | None,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    with TestClient(create_app(settings)) as client:
        created = ingest_job(
            client,
            source_url="https://www.linkedin.com/jobs/view/1234567890",
        )
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        checker=lambda _target, candidate: StatusCheckDecision(
            result,
            f"test_{result}",
            candidate.url,
            candidate.source_kind,
            detected_status=(result if result in {"closed", "expired"} else None),
            retryable=result in {"inconclusive", "failed"},
            retry_after_seconds=10 * 24 * 60 * 60,
        ),
        now=observed_at,
    )

    assert summary["checked"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == expected_status
        if expected_delay is None:
            assert job.status_next_check_at is None
        else:
            assert _as_utc(job.status_next_check_at) == observed_at + expected_delay


def test_windows_task_scripts_are_bounded_logged_on_and_idempotent():
    runner = Path("scripts/run_job_status_reconciliation.ps1").read_text(encoding="utf-8")
    installer = Path("scripts/install_job_status_reconciliation_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "scripts\\cli.mjs" in runner
    assert "jobs reconcile-status --json" in runner
    assert (
        '$startInfo.EnvironmentVariables["JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER"] = "invisible_playwright"'
        in runner
    )
    assert "var\\logs\\job-status-reconciliation" in runner
    assert "MaxRuntimeSeconds = 5400" in runner
    assert 'TaskName = "JobCockpit Status Reconciliation"' in installer
    assert "[int]$MidnightLimit = 1000" in installer
    assert "[int]$MidnightRenderedLimit = 1000" in installer
    assert "[int]$MidnightMaxRuntimeSeconds = 7200" in installer
    assert "[int]$AfternoonLimit = 100" in installer
    assert "[int]$AfternoonRenderedLimit = 30" in installer
    assert "[int]$AfternoonMaxRuntimeSeconds = 2700" in installer
    assert (
        'Register-ReconciliationTask `\n    -Name $TaskName `\n    -RunTime "00:00"'
        in installer
    )
    assert (
        'Register-ReconciliationTask `\n    -Name $AfternoonTaskName `\n'
        '    -RunTime "14:00"' in installer
    )
    assert '"-Limit {1} -RenderedLimit {2} -MaxRuntimeSeconds {3}"' in installer
    assert "New-ScheduledTaskTrigger `" in installer
    assert "        -Daily `" in installer
    assert "-RepetitionInterval" not in installer
    assert "-LogonType Interactive" in installer
    assert "-MultipleInstances IgnoreNew" in installer
    assert "$MaxRuntimeSeconds + 600" in installer
    assert "-AllowStartIfOnBatteries" in installer
    assert "-DontStopIfGoingOnBatteries" in installer
    assert "Register-ScheduledTask" in installer
    assert "-Force" in installer


def test_no_stored_url_is_skipped_and_retried_weekly(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            "source_url": None,
        },
    )
    created = response.json()
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)

    summary = run_status_reconciliation(settings, now=now)

    assert summary["checked"] == 1
    assert summary["skipped"] == 1
    assert summary["failed"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert _as_utc(job.status_next_check_at) == now + timedelta(days=7)
        assert job.status_check_failure_count == 0


def test_singleton_lock_blocks_live_run_and_recovers_stale_run(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_existing",
                status="running",
                started_at=now,
                request_payload={},
            )
        )

    with pytest.raises(StatusScanAlreadyRunningError):
        run_status_reconciliation(settings, now=now, max_runtime_seconds=60)

    with session_scope(settings) as db:
        existing = db.get(JobStatusScanRun, "status_scan_existing")
        existing.started_at = now - timedelta(hours=2)

    summary = run_status_reconciliation(settings, now=now, max_runtime_seconds=60)

    assert summary["status"] == "completed"
    with session_scope(settings) as db:
        assert db.get(JobStatusScanRun, "status_scan_existing").status == "abandoned"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_workday_403_requires_rendered_confirmation_before_closing(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    source_url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Platform-Engineer_R123"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "workday")
    target = reconciliation.JobStatusTarget(
        job_id="job_workday",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class ForbiddenApplyResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, url):
            request = httpx.Request("POST", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "forbidden",
                request=request,
                response=response,
            )

        def close(self):
            pass

    monkeypatch.setattr(
        reconciliation,
        "AtsApplyPageResolver",
        ForbiddenApplyResolver,
    )
    direct = reconciliation._workday_check(
        target,
        candidate,
        settings,
        reconciliation.UrlFetchPolicy(),
    )

    assert isinstance(direct, RenderRequired)
    assert direct.evidence_code == "workday_403_render_required"

    class ClosedRenderedResolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": "The job posting is no longer available.",
                    "evidence_origin": "browser_dom",
                    "confidence": 1.0,
                },
            }

    rendered = reconciliation._rendered_check(
        target,
        candidate,
        ClosedRenderedResolver(),
    )
    assert rendered.result == "closed"
    assert rendered.detected_status == "closed"
    assert rendered.evidence_code == "ats_no_longer_available"


def test_stored_public_http_workday_410_is_inconclusive(
    tmp_path: Path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    source_url = "http://acme.wd5.myworkdayjobs.com/Careers/job/Platform-Engineer_R123"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_http",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class GoneApplyResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, url):
            request = httpx.Request("GET", url)
            response = httpx.Response(410, request=request)
            raise httpx.HTTPStatusError(
                "gone",
                request=request,
                response=response,
            )

        def close(self):
            pass

    monkeypatch.setattr(
        reconciliation,
        "AtsApplyPageResolver",
        GoneApplyResolver,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        settings,
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "terminal_evidence_insecure_transport"


@pytest.mark.parametrize("error,expected_result,expected_evidence", [
    # October 4 LinkedIn replay: our guard rejected third-party requests, but
    # reconciliation stored authentication_wall and discarded the actual error.
    ("playwright_failed: rendered_url_policy_blocked: rendered_cross_origin_blocked",
     "failed", "rendered_url_policy_blocked"),
    ("playwright_url_policy_blocked: private_network_blocked",
     "failed", "rendered_url_policy_blocked"),
    ("chrome_cdp_url_policy_blocked: dns_resolution_failed",
     "failed", "rendered_url_policy_blocked"),
    ("codex_browser_url_policy_blocked: private_network_blocked",
     "failed", "rendered_url_policy_blocked"),
    ("playwright_failed: rendered_subresource_blocked: private_network_blocked",
     "failed", "rendered_url_policy_blocked"),
    ("auth_wall_unresolved", "inconclusive", "authentication_wall"),
    ("playwright_failed: blocked_rendered_page", "inconclusive", "authentication_wall"),
    ("playwright_failed: invisible_session_sign_in_required",
     "inconclusive", "authentication_wall"),
    ("codex_playwright_auth_wall", "inconclusive", "authentication_wall"),
])
def test_rendered_failure_reason_survives_scan_persistence(
    tmp_path, error, expected_result, expected_evidence,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(client, source_url="https://www.linkedin.com/jobs/view/9999999966")
    resolver_error = f"{error} https://jobs.example.test/check?token=private"

    class Resolver:
        def load_page(self, _hint):
            return {"status": "needs_review", "error": resolver_error}

    def check(target, candidate):
        decision = reconciliation._rendered_check(target, candidate, Resolver())
        assert decision.retryable
        assert decision.detected_status is None
        return decision

    summary = run_status_reconciliation(settings, checker=check)

    assert summary["checked"] == 1
    assert summary["failed"] == int(expected_result == "failed")
    assert summary["inconclusive"] == int(expected_result == "inconclusive")
    assert summary["status"] == (
        "completed_with_warnings" if expected_result == "failed" else "completed"
    )
    with session_scope(settings) as db:
        saved = db.get(JobPosting, created["id"])
        assert saved.status == "active"
        assert saved.status_check_result == expected_result
        assert saved.status_check_evidence_code == expected_evidence
        assert saved.status_check_failure_count == 1
        assert saved.status_next_check_at is not None
        check = db.execute(select(JobStatusCheck)).scalar_one()
        assert check.result == expected_result
        assert check.evidence_code == expected_evidence
        assert check.error_message == f"{error} [url]"
        assert check.status_before == check.status_after == "active"


def test_rendered_auth_wall_and_identity_mismatch_remain_inconclusive():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class AuthWallResolver:
        def resolve(self, _hint):
            return {
                "status": "needs_review",
                "error": "auth_wall_unresolved",
                "candidates": [
                    {
                        "source_url": source_url,
                        "raw_text": "Sign in to continue",
                        "confidence": 1.0,
                    }
                ],
            }

    decision = reconciliation._rendered_check(
        target,
        candidate,
        AuthWallResolver(),
    )
    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


@pytest.mark.parametrize("variant,expected_result", [
    ("equivalent", "open"),
    ("different_year", "inconclusive"),
    ("different_role", "inconclusive"),
    ("different_company", "inconclusive"),
    ("different_job_id", "inconclusive"),
    ("auth_wall", "inconclusive"),
])
def test_rendered_linkedin_title_uses_observed_identity_after_loading(
    tmp_path, allow_public_dns, variant, expected_result,
):
    from test_invisible_browser import browser_resolver
    from test_posting_discovery import FakePage

    # Observed failed titles reordered Co-op / Intern and contained a zero-width space.
    source_url = "https://www.linkedin.com/jobs/view/9999999977"
    allow_public_dns("www.linkedin.com")
    candidate = reconciliation.StatusUrlCandidate(source_url, "canonical_source", "linkedin")
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_title", status="active", source_type="gmail_alert",
        source_url=source_url, company="Acme",
        title="Platform Engineer Co-op / Intern (Winter 2027)", location="",
        candidates=(candidate,), failure_count=0, not_found_count=0, last_checked_at=None,
    )
    observed_title = "Platform Engineer Intern/Co-op\u200b (Winter 2027)"
    company = "Acme"
    returned_url = source_url
    if variant == "different_year":
        observed_title = observed_title.replace("2027", "2028")
    elif variant == "different_role":
        observed_title = observed_title.replace("Platform", "Security")
    elif variant == "different_company":
        company = "Other Employer"
    elif variant == "different_job_id":
        returned_url = source_url.replace("9999999977", "9999999978")
    body = f"{observed_title}\n{company}\nAbout the job\nBuild internal platforms."
    if variant == "auth_wall":
        body += "\nEmail or phone\nPassword"

    class ReturnedPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = returned_url

    page = ReturnedPage(returned_url, f"{observed_title} | {company} | LinkedIn", body)
    resolver, context, _ = browser_resolver(tmp_path, page)

    decision = reconciliation._rendered_check(target, candidate, resolver)

    assert decision.result == expected_result
    assert context.closed


def test_rendered_auth_wall_cannot_prove_open_from_matching_job_identity():
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_greenhouse_matching_auth_wall",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class MatchingIdentityAuthWallResolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": ("Sign in to continue\nAcme\nPlatform Engineer\nCanada"),
                    "evidence_origin": "browser_dom",
                    "confidence": 1.0,
                },
            }

    decision = reconciliation._rendered_check(
        target,
        candidate,
        MatchingIdentityAuthWallResolver(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_rendered_auth_wall_takes_precedence_over_ats_closed_marker():
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_greenhouse_auth_wall",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class AuthWallWithClosedMarkerResolver:
        def resolve(self, _hint):
            return {
                "status": "needs_review",
                "error": "auth_wall_unresolved",
                "candidates": [
                    {
                        "source_url": source_url,
                        "raw_text": "The page you are looking for does not exist.",
                        "evidence_origin": "browser_dom",
                        "confidence": 1.0,
                    }
                ],
            }

    decision = reconciliation._rendered_check(target, candidate, AuthWallWithClosedMarkerResolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_oversized_readable_ats_404_auth_prefix_remains_inconclusive(
    monkeypatch,
):
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_greenhouse_oversized_auth_wall",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    response = reconciliation._bounded_buffered_response(
        httpx.Response(
            404,
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", source_url),
            content=(
                b"<html><body><h1>Sign in to continue</h1>"
                + b"x" * reconciliation._MAX_RESPONSE_BYTES
                + b"</body></html>"
            ),
        )
    )
    assert response.extensions[reconciliation._RESPONSE_TOO_LARGE_EXTENSION] is True
    assert len(response.content) == reconciliation._MAX_RESPONSE_BYTES
    assert b"Sign in to continue" in response.content
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(
        target,
        candidate,
        Settings(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_successful_structured_workday_detail_is_open_without_company_in_raw_text(
    tmp_path: Path, monkeypatch
):
    settings = make_settings(tmp_path)
    source_url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Platform-Engineer_R123"
    candidate = reconciliation.StatusUrlCandidate(source_url, "official_ats", "workday")
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_open",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class StructuredWorkdayResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, _url):
            return SimpleNamespace(
                source_archive=SimpleNamespace(
                    final_url=(
                        "https://acme.wd5.myworkdayjobs.com/wday/cxs/"
                        "acme/Careers/job/Platform-Engineer_R123"
                    ),
                    status_code=200,
                    body=(
                        b'{"jobPostingInfo":{"title":"Platform Engineer",'
                        b'"jobDescription":"A retired position is no longer available, '
                        b'but this role replaces it."}}'
                    ),
                ),
                source_url=source_url,
                title="Platform Engineer",
                raw_text=(
                    "Platform Engineer\n"
                    "Req ID: R123\n"
                    "Location: Canada\n"
                    "Build and operate the platform."
                ),
                provider_job_id="R123",
            )

        def close(self):
            pass

    monkeypatch.setattr(
        reconciliation,
        "AtsApplyPageResolver",
        StructuredWorkdayResolver,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        settings,
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "open"
    assert decision.evidence_code == "workday_structured_job_match"


def test_workday_structured_response_requires_expected_job_identity(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    source_url = "https://acme.wd5.myworkdayjobs.com/Careers/job/Toronto/Platform-Engineer_R123"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_identity_mismatch",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class WrongStructuredWorkdayResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, _url):
            return SimpleNamespace(
                source_archive=SimpleNamespace(status_code=200),
                source_url=source_url,
                title="Financial Analyst",
                raw_text=(
                    "Financial Analyst\n"
                    "Req ID: R999\n"
                    "Location: Canada\n"
                    "Prepare quarterly financial reports."
                ),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        reconciliation,
        "AtsApplyPageResolver",
        WrongStructuredWorkdayResolver,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        settings,
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "workday_job_identity_mismatch"


def test_workday_rate_limit_preserves_retry_after(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    resolver_closed = False
    source_url = "https://acme.wd5.myworkdayjobs.com/Careers/job/Toronto/Platform-Engineer_R123"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_rate_limited",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class RateLimitedWorkdayResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, url):
            request = httpx.Request("GET", url)
            response = httpx.Response(
                429,
                headers={"Retry-After": "172800"},
                request=request,
            )
            raise httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

        def close(self):
            nonlocal resolver_closed
            resolver_closed = True

    monkeypatch.setattr(
        reconciliation,
        "AtsApplyPageResolver",
        RateLimitedWorkdayResolver,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        settings,
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "http_429_rate_limited"
    assert decision.retry_after_seconds == 172800
    assert resolver_closed is True


def test_runtime_budget_stops_scheduling_additional_direct_batches(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    for index in range(8):
        ingest_job(
            client,
            source_url=f"https://www.linkedin.com/jobs/view/{9000000000 + index}",
            company=f"Acme {index}",
        )

    calls = []

    def slow_probe(_target, candidate):
        calls.append(candidate.url)
        time.sleep(1.05)
        return StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        checker=slow_probe,
        max_runtime_seconds=1,
    )

    assert len(calls) <= 4
    assert summary["deferred"] >= 4
    with session_scope(settings) as db:
        jobs = db.execute(select(JobPosting)).scalars().all()
        checks = db.execute(select(JobStatusCheck)).scalars().all()
        completed_jobs = [job for job in jobs if job.status_last_checked_at is not None]
        assert len(completed_jobs) == summary["checked"]
        assert summary["checked"] <= len(calls)
        assert len(checks) == summary["checked"]
        assert {check.job_id for check in checks} == {job.id for job in completed_jobs}
        assert all(job.status_next_check_at is not None for job in jobs)
        assert all(job.status_check_failure_count == 0 for job in jobs)
    assert summary["checked"] + summary["deferred"] == summary["selected"]


def test_runtime_deadline_abandons_a_direct_worker_that_never_returns(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/9911000001",
        company="Deadline Direct",
    )
    started = threading.Event()
    release = threading.Event()
    summaries: list[dict] = []
    errors: list[BaseException] = []

    def stuck_probe(_target, _candidate):
        started.set()
        release.wait()
        return StatusCheckDecision("open", "posting_identity_match")

    def invoke() -> None:
        try:
            summaries.append(
                run_status_reconciliation(
                    settings,
                    checker=stuck_probe,
                    max_runtime_seconds=1,
                )
            )
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=invoke, daemon=True)
    runner.start()
    assert started.wait(timeout=1)
    runner.join(timeout=1.25)
    finished_before_release = not runner.is_alive()
    release.set()
    runner.join(timeout=3)

    assert finished_before_release is True
    assert errors == []
    assert summaries[0]["checked"] == 0
    assert summaries[0]["deferred"] == summaries[0]["selected"] == 1
    with session_scope(settings) as db:
        job = db.execute(select(JobPosting)).scalar_one()
        assert job.status == "active"
        assert job.status_last_checked_at is None
        assert job.status_next_check_at is not None
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_runtime_deadline_abandons_a_rendered_worker_that_never_returns(
    tmp_path: Path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/9911000002",
        company="Deadline Rendered",
    )
    started = threading.Event()
    release = threading.Event()
    summaries: list[dict] = []
    errors: list[BaseException] = []

    monkeypatch.setattr(
        reconciliation,
        "_direct_target_check",
        lambda target, *_args, **_kwargs: RenderRequired(
            "http_403_render_required",
            target.candidates[0].url,
            target.candidates[0].source_kind,
            http_status=403,
        ),
    )

    def stuck_rendered(*_args, **_kwargs):
        started.set()
        release.wait()
        return StatusCheckDecision("open", "posting_identity_match")

    monkeypatch.setattr(
        reconciliation,
        "_rendered_check_with_host_lock",
        stuck_rendered,
    )
    cleanup_requested = threading.Event()
    cleanup_calls: list[str] = []

    class ResolverWithCleanupLease:
        def cancel_active_pages(self) -> int:
            cleanup_calls.append("cancelled")
            cleanup_requested.set()
            release.set()
            return 1

    monkeypatch.setattr(
        reconciliation,
        "_rendered_resolver_for_remaining_budget",
        lambda *_args, **_kwargs: ResolverWithCleanupLease(),
    )

    def invoke() -> None:
        try:
            summaries.append(
                run_status_reconciliation(
                    settings,
                    rendered_limit=1,
                    max_runtime_seconds=1,
                )
            )
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=invoke, daemon=True)
    runner.start()
    assert started.wait(timeout=1)
    runner.join(timeout=1.25)
    finished_before_release = not runner.is_alive()
    release.set()
    runner.join(timeout=3)

    assert finished_before_release is True
    assert cleanup_requested.is_set()
    assert cleanup_calls == ["cancelled", "cancelled"]
    assert errors == []
    assert summaries[0]["checked"] == 0
    assert summaries[0]["deferred"] == summaries[0]["selected"] == 1


def test_firefox_scan_defers_four_second_budget_without_starting_or_failing_a_check(
    tmp_path: Path, monkeypatch,
):
    # October 1: a LinkedIn render started with ~4s left and reported a timeout.
    settings = make_settings(tmp_path).model_copy(update={
        "posting_rendered_fallback_provider": "invisible_playwright",
        "browser_health_enabled": True,
    })
    init_database(settings)
    client = TestClient(create_app(settings))
    job = ingest_job(client, source_url="https://www.linkedin.com/jobs/view/9911000003")
    monkeypatch.setattr(reconciliation, "_direct_target_check", lambda target, *_args, **_kwargs:
                        RenderRequired("http_403_render_required", target.candidates[0].url,
                                       target.candidates[0].source_kind, http_status=403))
    calls = []

    class Resolver:
        def load_page(self, hint):
            from jobcockpit.ingestion.posting_resolution import PostingResolution
            calls.append(hint.source_url)
            return PostingResolution(status="needs_review", error="playwright_timeout")

    monkeypatch.setattr(reconciliation, "_rendered_resolver_for_remaining_budget",
                        lambda *_args: Resolver())
    summary = run_status_reconciliation(settings, rendered_limit=1, max_runtime_seconds=4)
    assert calls == []
    assert summary["deferred"] == summary["selected"] == 1
    assert summary["checked"] == summary["failed"] == summary["inconclusive"] == 0
    with session_scope(settings) as db:
        saved = db.get(JobPosting, job["id"])
        assert saved.status == "active"
        assert saved.status_check_failure_count == 0
        assert db.execute(select(JobStatusCheck)).scalars().all() == []
    assert list((settings.artifact_root / "browser-health").rglob("*.json")) == []


def test_stale_run_recovery_uses_existing_run_runtime(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_long_running",
                status="running",
                started_at=now - timedelta(minutes=20),
                request_payload={"max_runtime_seconds": 5400},
            )
        )

    with pytest.raises(StatusScanAlreadyRunningError):
        run_status_reconciliation(
            settings,
            now=now,
            max_runtime_seconds=1,
        )

    with session_scope(settings) as db:
        assert db.get(JobStatusScanRun, "status_scan_long_running").status == "running"


def test_direct_fallback_preserves_longest_retry_after(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    candidates = (
        reconciliation.StatusUrlCandidate(
            "https://jobs.example.com/official/123",
            "official_ats",
            "generic",
        ),
        reconciliation.StatusUrlCandidate(
            "https://jobs.example.net/fallback/123",
            "canonical_source",
            "generic",
        ),
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_retry_after",
        status="active",
        source_type="gmail_alert",
        source_url=candidates[1].url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=candidates,
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    def candidate_decision(_target, candidate, _settings, **_kwargs):
        if candidate.kind == "official_ats":
            return StatusCheckDecision(
                "inconclusive",
                "http_429_rate_limited",
                candidate.url,
                candidate.source_kind,
                http_status=429,
                retryable=True,
                retry_after_seconds=172800,
            )
        return StatusCheckDecision(
            "inconclusive",
            "authentication_wall",
            candidate.url,
            candidate.source_kind,
            retryable=True,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", candidate_decision)

    decision = reconciliation._direct_target_check(
        target,
        settings,
        {},
        reconciliation.threading.Lock(),
    )

    assert decision.evidence_code == "authentication_wall"
    assert decision.retry_after_seconds == 172800


def test_confirmable_404_survives_an_inconclusive_rendered_fallback(
    tmp_path: Path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    official = reconciliation.StatusUrlCandidate(
        "https://careers.acme.example/roles/123",
        "official_ats",
        "generic",
    )
    linkedin = reconciliation.StatusUrlCandidate(
        "https://www.linkedin.com/jobs/view/1234567890",
        "aggregator_fallback",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_confirmable_404",
        status="active",
        source_type="gmail_alert",
        source_url=linkedin.url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(official, linkedin),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    def pending_404(_target, candidate, _settings, **_kwargs):
        assert candidate == official
        return StatusCheckDecision(
            "inconclusive",
            "generic_http_404_pending_confirmation",
            candidate.url,
            candidate.source_kind,
            http_status=404,
            retryable=True,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", pending_404)
    render_request = reconciliation._direct_target_check(
        target,
        settings,
        {},
        reconciliation.threading.Lock(),
    )

    assert isinstance(render_request, RenderRequired)
    assert render_request.prior_decision is not None
    assert render_request.prior_decision.evidence_code == "generic_http_404_pending_confirmation"

    rendered = StatusCheckDecision(
        "inconclusive",
        "authentication_wall",
        linkedin.url,
        "linkedin",
        retryable=True,
    )
    selected = reconciliation._prefer_confirmable_decision(
        rendered,
        render_request.prior_decision,
    )
    assert selected.evidence_code == "generic_http_404_pending_confirmation"
    assert selected.source_url == official.url


def test_definitive_rendered_open_wins_over_an_earlier_confirmable_404():
    prior = StatusCheckDecision(
        "inconclusive",
        "generic_http_404_pending_confirmation",
        "https://careers.acme.example/roles/123",
        "generic",
        http_status=404,
        retryable=True,
    )
    rendered = StatusCheckDecision(
        "open",
        "rendered_posting_identity_match",
        "https://www.linkedin.com/jobs/view/1234567890",
        "linkedin",
        http_status=200,
    )

    selected = reconciliation._prefer_confirmable_decision(rendered, prior)

    assert selected is rendered


def test_run_history_records_actual_completion_time(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    started_at = datetime.now(UTC) - timedelta(seconds=1)

    summary = run_status_reconciliation(settings, now=started_at)

    with session_scope(settings) as db:
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert _as_utc(run.finished_at) > _as_utc(run.started_at)


def test_redirected_ats_404_remains_inconclusive(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    candidate = reconciliation.StatusUrlCandidate(
        "https://boards.greenhouse.io/acme/jobs/123",
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_redirected_404",
        status="active",
        source_type="gmail_alert",
        source_url=candidate.url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    final_url = "https://boards.greenhouse.io/acme/jobs/search"
    response = httpx.Response(404, request=httpx.Request("GET", final_url))
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, settings)

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code in {
        "authentication_wall",
        "redirect_identity_mismatch",
    }


def test_generic_404_confirmation_requires_same_canonical_url():
    first_url = "https://jobs.example.com/postings/123"
    second_url = "https://jobs.example.com/postings/456"
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_404",
        status="active",
        source_type="gmail_alert",
        source_url=second_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(),
        failure_count=1,
        not_found_count=1,
        last_checked_at=datetime.now(UTC) - timedelta(hours=7),
        last_check_source_url=first_url,
    )

    assert reconciliation._eligible_generic_404_count(target, second_url) == 0
    assert reconciliation._eligible_generic_404_count(target, first_url) == 1


def test_check_timestamps_and_schedules_use_the_observation_time(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/8123456789",
    )
    run_started_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    observed_at = run_started_at + timedelta(hours=1, minutes=20)

    def open_probe(_target, candidate):
        return StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        checker=open_probe,
        now=run_started_at,
        observation_clock=lambda: observed_at,
    )

    assert summary["open"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        check = db.execute(select(JobStatusCheck)).scalar_one()
        assert _as_utc(job.status_last_checked_at) == observed_at
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(hours=24)
        assert _as_utc(check.checked_at) == observed_at


def test_generic_404_confirmation_uses_actual_observation_spacing():
    source_url = "https://careers.acme.example/jobs/123"
    first_observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_404_spacing",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(),
        failure_count=1,
        not_found_count=1,
        last_checked_at=first_observed_at,
        last_check_source_url=source_url,
    )

    assert (
        reconciliation._eligible_generic_404_count(
            target,
            source_url,
            observed_at=first_observed_at + timedelta(hours=5, minutes=59),
        )
        == 0
    )
    assert (
        reconciliation._eligible_generic_404_count(
            target,
            source_url,
            observed_at=first_observed_at + timedelta(hours=6),
        )
        == 1
    )


def test_status_report_job_filter_scopes_runs_and_checks(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    first = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/7123456789",
        company="First Company",
    )
    second = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/7123456790",
        company="Second Company",
    )

    def open_probe(_target, candidate):
        return StatusCheckDecision(
            "open", "posting_identity_match", candidate.url, candidate.source_kind
        )

    first_run = run_status_reconciliation(settings, job_id=first["id"], checker=open_probe)
    run_status_reconciliation(settings, job_id=second["id"], checker=open_probe)

    report = status_report(settings, job_id=first["id"])

    assert [run["run_id"] for run in report["runs"]] == [first_run["run_id"]]
    assert [check["job_id"] for check in report["checks"]] == [first["id"]]


def test_scheduled_render_batches_serialize_workday_requests():
    def request(source_kind: str, url: str):
        return SimpleNamespace(candidate=SimpleNamespace(source_kind=source_kind, url=url))

    linkedin = request("linkedin", "https://www.linkedin.com/jobs/view/7123456789")
    workday = request(
        "workday",
        "https://acme.wd5.myworkdayjobs.com/Careers/job/Toronto/Role_R123",
    )

    assert reconciliation._scheduled_render_batch_size([linkedin, linkedin], 0, 2) == 2
    assert reconciliation._scheduled_render_batch_size([workday, linkedin], 0, 2) == 1
    assert reconciliation._scheduled_render_batch_size([linkedin, workday], 0, 2) == 1
    assert (
        reconciliation._scheduled_render_batch_size(
            [request("ats_eluta", workday.candidate.url), linkedin], 0, 2
        )
        == 1
    )


def test_render_budget_deferral_preserves_retry_after_without_check_history(
    tmp_path: Path, monkeypatch
):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/9123456789",
    )
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def rate_limited_then_render(target, *_args, **_kwargs):
        candidate = target.candidates[0]
        return RenderRequired(
            "aggregator_render_required",
            candidate.url,
            candidate.source_kind,
            retry_after_seconds=172800,
        )

    monkeypatch.setattr(
        reconciliation,
        "_direct_target_check",
        rate_limited_then_render,
    )

    summary = run_status_reconciliation(
        settings,
        rendered_limit=0,
        now=now,
    )

    assert summary["deferred"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert _as_utc(job.status_next_check_at) == now + timedelta(hours=48)
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


@pytest.mark.parametrize("provider", ["playwright_profile", "invisible_playwright_first"])
def test_scheduled_rendering_rejects_non_cancellable_playwright_profile(
    tmp_path: Path, provider: str,
):
    settings = make_settings(tmp_path).model_copy(
        update={"posting_rendered_fallback_provider": provider}
    )

    with pytest.raises(
        ValueError,
        match="requires a cancellable rendered resolver",
    ):
        run_status_reconciliation(settings)


def test_rendered_resolver_finalizer_transfers_unresolved_cleanup_ownership(
    monkeypatch,
):
    transferred: list[object] = []

    class Resolver:
        def cancel_active_pages(self) -> int:
            return 0

    resolver = Resolver()
    monkeypatch.setattr(
        reconciliation,
        "retain_pending_browser_cleanup",
        lambda owner: transferred.append(owner) or 1,
    )

    assert reconciliation._finalize_rendered_resolver(resolver) == 1
    assert transferred == [resolver]


def test_rendered_resolver_finalizer_propagates_failed_durable_handoff(
    monkeypatch,
):
    class Resolver:
        def cancel_active_pages(self) -> int:
            return 0

    def fail_handoff(_owner):
        raise RuntimeError("durable cleanup handoff failed")

    monkeypatch.setattr(
        reconciliation,
        "retain_pending_browser_cleanup",
        fail_handoff,
    )

    with pytest.raises(RuntimeError, match="durable cleanup handoff failed"):
        reconciliation._finalize_rendered_resolver(Resolver())


def test_rendered_resolver_finalization_serializes_cancel_and_handoff(monkeypatch):
    events = []

    class FinalizationLock:
        def __enter__(self):
            events.append("lock_enter")

        def __exit__(self, *_args):
            events.append("lock_exit")

    class Resolver:
        cleanup_finalization_lock = FinalizationLock()

        def cancel_active_pages(self):
            events.append("cancel")
            return 0

    resolver = Resolver()

    def retain(owner):
        assert owner is resolver
        events.append("handoff")
        return 2

    monkeypatch.setattr(reconciliation, "retain_pending_browser_cleanup", retain)

    assert reconciliation._finalize_rendered_resolver(resolver) == 2
    assert events == ["lock_enter", "cancel", "handoff", "lock_exit"]


def test_rendered_worker_lifecycle_cancellation_before_attach_blocks_rendering(
    monkeypatch,
):
    lifecycle = reconciliation._RenderedWorkerLifecycle()
    resolver = object()
    finalized = []

    monkeypatch.setattr(
        reconciliation,
        "_finalize_rendered_resolver",
        lambda owner: finalized.append(owner) or 2,
    )

    assert lifecycle.phase is reconciliation._RenderedWorkerPhase.PENDING
    assert lifecycle.cancel() == 0
    assert lifecycle.phase is reconciliation._RenderedWorkerPhase.CANCEL_REQUESTED
    assert lifecycle.attach(resolver) is False

    assert lifecycle.finalize_worker() == 2
    assert lifecycle.phase is reconciliation._RenderedWorkerPhase.TERMINAL
    assert lifecycle.handed_off_count == 2
    assert lifecycle.cleanup_error is None
    assert finalized == [resolver]


def test_rendered_worker_lifecycle_retries_cleanup_failure_without_losing_resolver(
    monkeypatch,
):
    lifecycle = reconciliation._RenderedWorkerLifecycle()
    resolver = object()
    attempts = []

    def finalize(owner):
        assert owner is resolver
        attempts.append(owner)
        if len(attempts) == 1:
            raise RuntimeError("durable cleanup handoff failed")
        return 1

    monkeypatch.setattr(reconciliation, "_finalize_rendered_resolver", finalize)

    assert lifecycle.attach(resolver) is True
    with pytest.raises(RuntimeError, match="durable cleanup handoff failed"):
        lifecycle.finalize_worker()

    assert lifecycle.phase is reconciliation._RenderedWorkerPhase.TERMINAL
    assert isinstance(lifecycle.cleanup_error, RuntimeError)
    assert lifecycle.ensure_cleanup_owned() == 1
    assert lifecycle.cleanup_error is None
    assert lifecycle.handed_off_count == 1
    assert attempts == [resolver, resolver]


def test_rendered_budget_uses_one_trusted_navigation_per_job(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path).model_copy(
        update={
            "posting_rendered_fallback_provider": "chrome_cdp_first",
            "posting_rendered_fallback_timeout_seconds": 180,
            "posting_rendered_fallback_retries": 1,
            "posting_rendered_fallback_min_interval_seconds": 12.5,
            "playwright_auth_wall_wait_seconds": 120,
            "playwright_auth_wall_settle_seconds": 5,
        }
    )
    captured_settings = []

    def capture_settings(value, *, url_policy, trusted_dom_only):
        captured_settings.append((value, url_policy, trusted_dom_only))
        return object()

    monkeypatch.setattr(
        reconciliation,
        "default_rendered_page_resolver",
        capture_settings,
    )
    monkeypatch.setattr(reconciliation.time, "monotonic", lambda: 0)

    reconciliation._rendered_resolver_for_remaining_budget(settings, 100)

    bounded, url_policy, trusted_dom_only = captured_settings[0]
    assert url_policy.allowed_localhosts == set()
    assert trusted_dom_only is True
    assert bounded.posting_rendered_fallback_provider == "chrome_cdp_first"
    assert bounded.posting_rendered_fallback_retries == 0
    assert bounded.posting_rendered_fallback_min_interval_seconds == 0
    assert bounded.posting_rendered_fallback_timeout_seconds <= 25
    assert bounded.playwright_auth_wall_wait_seconds <= 25
    assert bounded.playwright_auth_wall_settle_seconds <= 25


def test_generic_404_confirmation_restarts_when_job_url_changes(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/9234567890",
    )
    first_url = "https://careers.acme.example/jobs/123"
    second_url = "https://careers.acme.example/jobs/456"
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.source_url = first_url
        job.application = {**job.application, "external_apply_url": first_url}

    def generic_404(target, candidate):
        return classify_http_status(
            404,
            source_kind="generic",
            generic_404_count=reconciliation._eligible_generic_404_count(
                target,
                candidate.url,
            ),
            source_url=candidate.url,
        )

    first_checked_at = datetime.now(UTC) - timedelta(hours=8)
    first = run_status_reconciliation(
        settings,
        checker=generic_404,
        now=first_checked_at,
    )
    assert first["inconclusive"] == 1

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status_check_not_found_count == 1
        job.source_url = second_url
        job.application = {**job.application, "external_apply_url": second_url}
        job.status_next_check_at = None

    second = run_status_reconciliation(
        settings,
        checker=generic_404,
        now=first_checked_at + timedelta(hours=7),
    )

    assert second["closed"] == 0
    assert second["inconclusive"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert job.status_check_not_found_count == 1
        checks = (
            db.execute(
                select(JobStatusCheck)
                .where(JobStatusCheck.job_id == created["id"])
                .order_by(JobStatusCheck.checked_at)
            )
            .scalars()
            .all()
        )
        assert [check.source_url for check in checks] == [
            first_url,
            second_url,
        ]


def test_abandoned_render_worker_cannot_orphan_a_late_registered_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    ingest_job(
        client,
        source_url="https://www.linkedin.com/jobs/view/9911000003",
        company="Late Target",
    )
    started = threading.Event()
    cancellation_latched = threading.Event()
    target_closed = threading.Event()

    class Resolver(ChromeCdpRenderedPageResolver):
        def cancel_active_pages(self) -> int:
            result = super().cancel_active_pages()
            cancellation_latched.set()
            return result

    resolver = Resolver(endpoint_url="http://127.0.0.1:9222")
    close_calls: list[str] = []

    def late_render(*_args, **_kwargs):
        started.set()
        assert cancellation_latched.wait(timeout=2.0)

        def close_target(target_id: str) -> bool:
            close_calls.append(target_id)
            if len(close_calls) >= 2:
                target_closed.set()
                return True
            return False

        resolver._register_cleanup_lease(
            BrowserPageCleanupLease(
                "late-deadline-target",
                close_target,
                "http://127.0.0.1:9222",
            )
        )
        assert target_closed.wait(timeout=1.0)
        return StatusCheckDecision("open", "posting_identity_match")

    monkeypatch.setattr(
        reconciliation,
        "_direct_target_check",
        lambda target, *_args, **_kwargs: RenderRequired(
            "http_403_render_required",
            target.candidates[0].url,
            target.candidates[0].source_kind,
            http_status=403,
        ),
    )
    monkeypatch.setattr(reconciliation, "_rendered_check_with_host_lock", late_render)
    monkeypatch.setattr(
        reconciliation,
        "_rendered_resolver_for_remaining_budget",
        lambda *_args, **_kwargs: resolver,
    )

    summary = run_status_reconciliation(
        settings,
        rendered_limit=1,
        max_runtime_seconds=1,
    )

    assert started.is_set()
    assert cancellation_latched.is_set()
    assert target_closed.wait(timeout=1.0)
    assert close_calls[:2] == ["late-deadline-target", "late-deadline-target"]
    assert summary["checked"] == 0
    assert summary["deferred"] == summary["selected"] == 1
