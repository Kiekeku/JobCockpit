from __future__ import annotations

import gzip
import socket
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion import status_reconciliation as reconciliation
from jobcockpit.ingestion.ats_adapters.base import AtsResponseTooLargeError
from jobcockpit.ingestion.ats_handlers.apply import AtsApplyPageResolver
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    ExternalPostingHint,
)


def _greenhouse_target() -> tuple[
    reconciliation.JobStatusTarget,
    reconciliation.StatusUrlCandidate,
]:
    return _greenhouse_target_for_url(
        "https://boards.greenhouse.io/acme/jobs/1234567"
    )


def _greenhouse_target_for_url(source_url: str) -> tuple[
    reconciliation.JobStatusTarget,
    reconciliation.StatusUrlCandidate,
]:
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    return (
        reconciliation.JobStatusTarget(
            job_id="job_greenhouse_regression",
            status="active",
            source_type="gmail_alert",
            source_url=source_url,
            company="Acme",
            title="Software Developer Intern",
            location="Canada",
            candidates=(candidate,),
            failure_count=0,
            not_found_count=0,
            last_checked_at=None,
        ),
        candidate,
    )


def test_target_uses_display_title_instead_of_normalized_identity_slug():
    selected_at = datetime(2026, 7, 27, tzinfo=UTC)
    job = SimpleNamespace(
        id="job_display_title",
        status="active",
        source_type="gmail_alert",
        source_url="https://boards.greenhouse.io/acme/jobs/1234567",
        company_name="Acme",
        normalized_title="software_developer_intern",
        role={
            "job_title": "Software Developer Intern",
            "normalized_title": "software_developer_intern",
        },
        location={"country": "Canada"},
        source={},
        application={},
        status_check_failure_count=0,
        status_check_not_found_count=0,
        status_last_checked_at=None,
        updated_at=selected_at,
    )

    target = reconciliation._target_from_job(job)

    assert target.title == "Software Developer Intern"
    assert reconciliation._identity_matches(
        target,
        "Software Developer Intern at Acme",
    )


def test_real_workday_apply_resolver_preserves_structured_title():
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/"
        "Platform-Engineer_R123"
    )

    def workday_detail(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/wday/cxs/acme/Careers/job/Platform-Engineer_R123"
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "jobPostingInfo": {
                    "title": "Platform Engineer",
                    "jobReqId": "R123",
                    "location": "Canada",
                    "jobDescription": "<p>Build and operate the platform.</p>",
                    "externalUrl": source_url,
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(workday_detail)) as client:
        resolver = AtsApplyPageResolver(http_client=client)
        job = resolver.resolve(source_url)

    assert job.title == "Platform Engineer"
    assert job.provider_job_id == "R123"


def test_workday_structured_identity_requires_matching_requisition_and_title():
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_structured_identity",
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

    assert reconciliation._structured_workday_identity_matches(
        target,
        candidate,
        SimpleNamespace(title="Platform Engineer", provider_job_id="R123"),
    )
    assert not reconciliation._structured_workday_identity_matches(
        target,
        candidate,
        SimpleNamespace(title="Platform Engineer", provider_job_id="R999"),
    )
    assert not reconciliation._structured_workday_identity_matches(
        target,
        candidate,
        SimpleNamespace(title="Different Role", provider_job_id="R123"),
    )


def test_direct_dns_resolution_failure_is_retryable_inconclusive(monkeypatch):
    target, candidate = _greenhouse_target()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror()),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.retryable is True
    assert decision.evidence_code == "url_policy_dns_resolution_failed"


def test_workday_dns_resolution_failure_is_retryable_inconclusive(monkeypatch):
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_dns_failure",
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
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror()),
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        Settings(),
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.retryable is True
    assert decision.evidence_code == "url_policy_dns_resolution_failed"


def test_direct_closure_signal_must_be_visible_page_content(monkeypatch):
    target, candidate = _greenhouse_target()
    hidden_marker_html = """
        <html>
          <head>
            <script>
              window.localizedMessages = {
                closed: "Job is no longer available"
              };
            </script>
          </head>
          <body>
            <h1>Software Developer Intern</h1>
            <div>Acme</div>
          </body>
        </html>
    """
    visible_marker_html = """
        <html>
          <body>
            <h1>Job is no longer available</h1>
          </body>
        </html>
    """
    returned_html = hidden_marker_html

    def fetch(*_args, **_kwargs):
        return httpx.Response(200, text=returned_html), candidate.url

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", fetch)

    live_decision = reconciliation._direct_check(
        target,
        candidate,
        Settings(),
    )
    assert live_decision.result == "open"
    assert live_decision.detected_status is None

    returned_html = visible_marker_html
    closed_decision = reconciliation._direct_check(
        target,
        candidate,
        Settings(),
    )
    assert isinstance(closed_decision, reconciliation.RenderRequired)
    assert closed_decision.evidence_code == "html_terminal_marker_render_required"
    assert closed_decision.http_status == 200


def test_generic_http_to_https_upgrade_can_close_from_terminal_status(monkeypatch):
    source_url = "http://jobs.acme.example/careers/123"
    final_url = "https://jobs.acme.example/careers/123"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_upgrade_closed",
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
    response = httpx.Response(
        410,
        request=httpx.Request("GET", final_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "http_410_gone"
    assert decision.source_url == final_url


@pytest.mark.parametrize(
    "route",
    (
        "result",
        "results",
        "search",
        "search-results",
        "archive",
        "list",
    ),
)
def test_generic_redirected_search_route_cannot_close_from_terminal_status(
    monkeypatch,
    route: str,
):
    source_url = "https://jobs.acme.example/careers/123"
    final_url = f"https://jobs.acme.example/{route}/123"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_search_redirect",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=1,
        last_checked_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    response = httpx.Response(
        410,
        request=httpx.Request("GET", final_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_generic_benign_redirect_can_prove_open_by_page_identity(monkeypatch):
    source_url = "https://careers.acme.example/jobs/123"
    final_url = "https://careers.acme.example/en-US/jobs/123/"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_redirect_open",
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
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text="<h1>Platform Engineer</h1><p>Acme</p><p>Build reliable services.</p>",
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "open"
    assert decision.detected_status is None
    assert decision.source_url == final_url
    assert decision.evidence_code == "posting_identity_match"


@pytest.mark.parametrize(
    "final_url",
    (
        "http://boards.greenhouse.io/acme/jobs/1234567",
        "https://boards.greenhouse.io:8443/acme/jobs/1234567",
    ),
)
def test_provider_unsafe_transport_cannot_close_from_http_410(
    monkeypatch, final_url: str
):
    target, candidate = _greenhouse_target()
    response = httpx.Response(
        410,
        request=httpx.Request("GET", final_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


@pytest.mark.parametrize(
    "final_url",
    (
        "http://boards.greenhouse.io/acme/jobs/1234567",
        "https://boards.greenhouse.io:8443/acme/jobs/1234567",
    ),
)
def test_rendered_provider_unsafe_transport_cannot_close_from_banner(final_url: str):
    target, candidate = _greenhouse_target()

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": final_url,
                    "raw_text": "The job posting is no longer available",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


@pytest.mark.parametrize(
    ("source_url", "banner"),
    (
        (
            "http://www.linkedin.com/jobs/view/platform-engineer-4425912315",
            "No longer accepting applications",
        ),
        (
            "http://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            "This job has expired on Indeed",
        ),
        (
            "http://boards.greenhouse.io/acme/jobs/1234567",
            "The job posting is no longer available",
        ),
        (
            "https://!@www.linkedin.com/jobs/view/platform-engineer-4425912315",
            "No longer accepting applications",
        ),
    ),
)
def test_insecure_public_terminal_banner_is_only_inconclusive(
    source_url: str,
    banner: str,
):
    decision = reconciliation.infer_status_from_page(source_url, banner)

    assert decision is not None
    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "terminal_evidence_insecure_transport"


@pytest.mark.parametrize(
    ("source_url", "page_text"),
    (
        (
            "https://www.linkedin.com/jobs/view/platform-engineer-4425912315",
            "Sign in to continue\nNo longer accepting applications",
        ),
        (
            "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
            (
                "Sign in to continue\n"
                "Job Post Details\n"
                "This job has expired on Indeed\n"
                "Software Engineer - job post\n"
                "Acme\n"
                "Job details"
            ),
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "Authentication required\nThe job posting is no longer available",
        ),
    ),
)
def test_shared_classifier_auth_wall_precedes_terminal_marker(
    source_url: str,
    page_text: str,
):
    decision = reconciliation.infer_status_from_page(
        source_url,
        page_text,
        evidence_scope="ingestion_snapshot",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is not None
    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_shared_classifier_terminal_marker_without_auth_remains_terminal():
    decision = reconciliation.infer_status_from_page(
        "https://www.linkedin.com/jobs/view/platform-engineer-4425912315",
        "No longer accepting applications",
        evidence_scope="ingestion_snapshot",
    )

    assert decision is not None
    assert decision.result == "closed"
    assert decision.detected_status == "closed"


@pytest.mark.parametrize("status_code", (404, 410))
def test_stored_public_http_status_cannot_close_job(
    monkeypatch,
    status_code: int,
):
    source_url = "http://boards.greenhouse.io/acme/jobs/1234567"
    target, candidate = _greenhouse_target_for_url(source_url)
    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", source_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "terminal_evidence_insecure_transport"


@pytest.mark.parametrize(
    ("status_code", "generic_404_count"),
    (
        (410, 0),
        (404, 1),
    ),
)
def test_shared_http_classifier_rejects_public_http_terminal_evidence(
    status_code: int,
    generic_404_count: int,
):
    decision = reconciliation.classify_http_status(
        status_code,
        source_kind="generic",
        generic_404_count=generic_404_count,
        source_url="http://careers.acme.example/jobs/platform-engineer",
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "terminal_evidence_insecure_transport"


@pytest.mark.parametrize(
    ("status_code", "generic_404_count", "expected_evidence"),
    (
        (410, 0, "http_410_gone"),
        (404, 1, "generic_http_404_confirmed"),
    ),
)
def test_shared_http_classifier_accepts_https_terminal_evidence(
    status_code: int,
    generic_404_count: int,
    expected_evidence: str,
):
    decision = reconciliation.classify_http_status(
        status_code,
        source_kind="generic",
        generic_404_count=generic_404_count,
        source_url="https://careers.acme.example/jobs/platform-engineer",
    )

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == expected_evidence


def test_stored_public_http_rendered_banner_cannot_close_job():
    source_url = "http://boards.greenhouse.io/acme/jobs/1234567"
    target, candidate = _greenhouse_target_for_url(source_url)

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": "The job posting is no longer available",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "terminal_evidence_insecure_transport"


def test_stored_public_http_upgrade_to_matching_https_can_close_job(monkeypatch):
    source_url = "http://boards.greenhouse.io/acme/jobs/1234567"
    final_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    target, candidate = _greenhouse_target_for_url(source_url)
    response = httpx.Response(
        410,
        request=httpx.Request("GET", final_url),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "http_410_gone"
    assert decision.source_url == final_url


def test_generic_redirect_to_board_card_cannot_prove_open(monkeypatch):
    source_url = "https://careers.acme.example/jobs/123"
    final_url = "https://careers.acme.example/jobs"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_generic_redirect_board",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=1,
        last_checked_at=None,
    )
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text=(
            "<main><h1>Open jobs</h1>"
            "<article><h2>Platform Engineer</h2><p>Acme</p></article></main>"
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"



def test_redirected_terminal_page_for_different_provider_job_never_closes(
    monkeypatch,
):
    target, candidate = _greenhouse_target()
    final_url = "https://boards.greenhouse.io/acme/jobs/7654321"
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text=(
            "<h1>Software Developer Intern</h1>"
            "<p>Acme</p>"
            "<p>Job is no longer available</p>"
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_rendered_generic_redirect_can_prove_open_by_dom_identity():
    source_url = "https://careers.acme.example/jobs/123"
    final_url = "https://careers.acme.example/en-US/jobs/123/"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_rendered_generic_redirect_open",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": final_url,
                    "raw_text": "Platform Engineer\nAcme\nBuild reliable services.",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "open"
    assert decision.source_url == final_url
    assert decision.evidence_code == "rendered_posting_identity_match"



def test_rendered_generic_redirect_to_board_card_cannot_prove_open():
    source_url = "https://careers.acme.example/jobs/123"
    final_url = "https://careers.acme.example/jobs"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_rendered_generic_redirect_board",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": final_url,
                    "raw_text": "Open jobs\nPlatform Engineer\nAcme",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_rendered_terminal_page_for_different_provider_job_never_closes():
    target, candidate = _greenhouse_target()
    final_url = "https://boards.greenhouse.io/acme/jobs/7654321"

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": final_url,
                    "raw_text": (
                        "Software Developer Intern\n"
                        "Acme\n"
                        "Job is no longer available"
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_different_provider_job_id_cannot_prove_open_by_repeated_identity(
    monkeypatch,
):
    target, candidate = _greenhouse_target()
    final_url = "https://boards.greenhouse.io/acme/jobs/7654321"
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text=(
            "<h1>Software Developer Intern</h1>"
            "<p>Acme</p>"
            "<p>Build reliable services.</p>"
        ),
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, final_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_rendered_different_provider_job_id_cannot_prove_open():
    target, candidate = _greenhouse_target()
    final_url = "https://boards.greenhouse.io/acme/jobs/7654321"

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": final_url,
                    "raw_text": (
                        "Software Developer Intern\n"
                        "Acme\n"
                        "Build reliable services."
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "redirect_identity_mismatch"


def test_html_content_type_without_tags_still_requires_render_confirmation(
    monkeypatch,
):
    target, candidate = _greenhouse_target()
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        text="Position is no longer available",
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, candidate.url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert isinstance(decision, reconciliation.RenderRequired)
    assert decision.evidence_code == "html_terminal_marker_render_required"


@pytest.mark.parametrize("status_code", [404, 410])
def test_non_200_authentication_body_takes_precedence_over_closure(
    monkeypatch,
    status_code: int,
):
    target, candidate = _greenhouse_target()

    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (
            httpx.Response(status_code, text="Sign in to continue"),
            candidate.url,
        ),
    )

    decision = reconciliation._direct_check(
        target,
        candidate,
        Settings(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_shared_classifier_ignores_hidden_terminal_marker():
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"

    decision = reconciliation.infer_status_from_page(
        source_url,
        """
        <html>
          <head><script>const closed = "Job is no longer available";</script></head>
          <body><h1>Software Developer Intern</h1><div>Acme</div></body>
        </html>
        """,
    )

    assert decision is None


@pytest.mark.parametrize("status_code", [404, 410])
def test_workday_authentication_body_takes_precedence_over_closure(
    monkeypatch,
    status_code: int,
):
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_auth",
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
        def __init__(self, **_kwargs):
            pass

        def resolve(self, url: str):
            request = httpx.Request("GET", url)
            response = httpx.Response(
                status_code,
                request=request,
                text="Sign in to continue",
            )
            response.raise_for_status()

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "AtsApplyPageResolver", AuthWallResolver)
    decision = reconciliation._workday_check(
        target,
        candidate,
        Settings(),
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_trusted_provider_listing_page_cannot_prove_job_is_open(monkeypatch):
    source_url = "https://boards.greenhouse.io/acme"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_greenhouse_listing",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Software Developer Intern",
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
                text="Software Developer Intern at Acme",
            ),
            source_url,
        ),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "unsupported"
    assert decision.evidence_code == "stored_url_not_job_detail"


def test_trusted_provider_listing_candidate_is_skipped_before_http_classification(
    monkeypatch,
):
    listing = reconciliation.StatusUrlCandidate(
        "https://boards.greenhouse.io/acme",
        "official_ats",
        "greenhouse",
    )
    detail = reconciliation.StatusUrlCandidate(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        "canonical_source",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_skip_greenhouse_listing",
        status="active",
        source_type="gmail_alert",
        source_url=detail.url,
        company="Acme",
        title="Software Developer Intern",
        location="Canada",
        candidates=(listing, detail),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    checked: list[str] = []

    def check(_target, candidate, _settings, **_kwargs):
        checked.append(candidate.url)
        assert candidate is detail
        return reconciliation.StatusCheckDecision(
            "closed",
            "http_410_gone",
            candidate.url,
            candidate.source_kind,
            "closed",
            410,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", check)

    decision = reconciliation._direct_target_check(
        target,
        Settings(),
        {},
        reconciliation.threading.Lock(),
    )

    assert decision.result == "closed"
    assert decision.source_url == detail.url
    assert checked == [detail.url]


@pytest.mark.parametrize("status_code", [404, 410])
def test_trusted_provider_listing_http_status_cannot_close_job(
    monkeypatch,
    status_code: int,
):
    source_url = "https://boards.greenhouse.io/acme"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_greenhouse_listing_status",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Software Developer Intern",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=1,
        last_checked_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    fetch_called = False

    def fetch(*_args, **_kwargs):
        nonlocal fetch_called
        fetch_called = True
        return httpx.Response(status_code), source_url

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", fetch)

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "unsupported"
    assert decision.detected_status is None
    assert fetch_called is False


def test_direct_fetch_caps_streamed_response_before_buffering():
    source_url = "https://careers.example.com/jobs/123"
    payload = b"x" * (reconciliation._MAX_RESPONSE_BYTES + 1)
    get_called = False

    class StreamContext:
        def __enter__(self):
            response = httpx.Response(
                200,
                stream=httpx.ByteStream(payload),
                request=httpx.Request("GET", source_url),
            )
            if "Content-Length" in response.headers:
                del response.headers["Content-Length"]
            return response

        def __exit__(self, *_args):
            return False

    class StreamingClient:
        def stream(self, method: str, url: str, **kwargs):
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert method == "GET"
            assert url == source_url
            return StreamContext()

        def get(self, _url: str):
            nonlocal get_called
            get_called = True
            raise AssertionError("buffered get must not be used")

    response = reconciliation._bounded_http_get(
        StreamingClient(),
        source_url,
    )

    assert get_called is False
    assert len(response.content) == reconciliation._MAX_RESPONSE_BYTES
    assert response.extensions[reconciliation._RESPONSE_TOO_LARGE_EXTENSION] is True


def test_declared_oversized_error_retains_auth_prefix_before_status_classification(
    monkeypatch,
):
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    payload = b"<html><body><h1>Sign in to continue</h1></body></html>"

    class StreamContext:
        def __enter__(self):
            return httpx.Response(
                404,
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Length": str(reconciliation._MAX_RESPONSE_BYTES + 100),
                },
                stream=httpx.ByteStream(payload),
                request=httpx.Request("GET", source_url),
            )

        def __exit__(self, *_args):
            return False

    class StreamingClient:
        def stream(self, method: str, url: str, **kwargs):
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert method == "GET"
            assert url == source_url
            return StreamContext()

    response = reconciliation._bounded_http_get(
        StreamingClient(),
        source_url,
    )

    assert response.extensions[reconciliation._RESPONSE_TOO_LARGE_EXTENSION] is True
    assert b"Sign in to continue" in response.content

    target, candidate = _greenhouse_target_for_url(source_url)
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, source_url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_direct_fetch_rejects_compressed_response_without_decoding():
    source_url = "https://careers.example.com/jobs/123"
    expanded_body = b"x" * (reconciliation._MAX_RESPONSE_BYTES * 10)

    def compressed_response(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
            },
            content=gzip.compress(expanded_body),
            request=request,
        )

    with httpx.Client(
        transport=httpx.MockTransport(compressed_response),
    ) as client:
        response = reconciliation._bounded_http_get(
            client,
            source_url,
        )

    assert response.content == b""
    assert "Content-Encoding" not in response.headers
    assert response.extensions[reconciliation._RESPONSE_TOO_LARGE_EXTENSION] is False
    assert (
        response.extensions[reconciliation._RESPONSE_UNSUPPORTED_ENCODING_EXTENSION]
        is True
    )


def test_bounded_http_get_stops_when_total_deadline_expires_during_body(
    monkeypatch,
):
    source_url = "https://careers.example.com/jobs/123"
    monotonic_time = [100.0]
    response_closed = False

    class StreamingResponse:
        status_code = 200
        headers = httpx.Headers()
        request = httpx.Request("GET", source_url)
        extensions = {}

        def iter_raw(self):
            yield b"first"
            monotonic_time[0] = 101.0
            yield b"second"

        def close(self):
            nonlocal response_closed
            response_closed = True

    class StreamContext:
        def __enter__(self):
            return StreamingResponse()

        def __exit__(self, *_args):
            return False

    class StreamingClient:
        def stream(self, method: str, url: str, **kwargs):
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert method == "GET"
            assert url == source_url
            return StreamContext()

    monkeypatch.setattr(
        reconciliation.time,
        "monotonic",
        lambda: monotonic_time[0],
    )

    with pytest.raises(reconciliation.StatusRuntimeBudgetExhausted):
        reconciliation._bounded_http_get(
            StreamingClient(),
            source_url,
            deadline=100.5,
        )

    assert response_closed is False
def test_inline_html_terminal_banner_is_detected_as_one_visible_line():
    decision = reconciliation.infer_status_from_page(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        (
            "<html><body><h1>The job posting is "
            "<strong>no longer available</strong>.</h1></body></html>"
        ),
        evidence_scope="rendered_page",
    )

    assert decision is not None
    assert decision.result == "closed"
    assert decision.evidence_code == "ats_no_longer_available"


def test_stylesheet_hidden_html_terminal_marker_requires_render_confirmation():
    source_url = "https://boards.greenhouse.io/acme/jobs/1234567"
    html = (
        "<html><head><style>.retired{display:none}</style></head><body>"
        "<div class='retired'>Position is no longer available</div>"
        "<h1>Live Platform Engineer</h1></body></html>"
    )

    assert reconciliation.infer_status_from_page(source_url, html) is None
    assert reconciliation.infer_status_from_page(
        source_url,
        html,
        evidence_scope="ingestion_snapshot",
    ) is None


def test_inline_html_incidental_marker_in_description_is_not_terminal():
    decision = reconciliation.infer_status_from_page(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        (
            "<html><body><h1>Platform Engineer</h1>"
            "<p>A retired position is <strong>no longer available</strong>, "
            "but this role replaces it.</p><button>Apply now</button></body></html>"
        ),
    )

    assert decision is None




def test_incidental_ats_marker_in_live_description_is_not_terminal():
    decision = reconciliation.infer_status_from_page(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        (
            "Platform Engineer\n"
            "If this position is no longer available, see our other openings.\n"
            "Apply now"
        ),
    )

    assert decision is None


@pytest.mark.parametrize(
    "hidden_markup",
    (
        "<dialog><p>Position is no longer available</p></dialog>",
        "<details><p>Position is no longer available</p></details>",
        "<div popover>Position is no longer available</div>",
        "<select><option>Position is no longer available</option></select>",
        "<p style='opacity: 0'>Position is no longer available</p>",
    ),
)
def test_non_visible_or_control_text_cannot_close_live_ats_job(hidden_markup):
    decision = reconciliation.infer_status_from_page(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        f"<html><body>{hidden_markup}<h1>Live Platform Engineer</h1></body></html>",
    )

    assert decision is None


def test_ingestion_snapshot_does_not_treat_ats_description_as_status_banner():
    decision = reconciliation.infer_status_from_page(
        "https://boards.greenhouse.io/acme/jobs/1234567",
        "Platform Engineer\nPosition is no longer available\nApply now",
        evidence_scope="ingestion_snapshot",
    )

    assert decision is None


def test_http_410_closes_even_when_its_error_body_is_oversized(monkeypatch):

    target, candidate = _greenhouse_target()
    response = httpx.Response(
        410,
        content=b"Job is no longer available",
        extensions={reconciliation._RESPONSE_TOO_LARGE_EXTENSION: True},
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, candidate.url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "http_410_gone"


def test_recognized_ats_404_with_unreadable_encoding_is_inconclusive(
    monkeypatch,
):
    target, candidate = _greenhouse_target()
    response = httpx.Response(
        404,
        content=b"",
        extensions={
            reconciliation._RESPONSE_UNSUPPORTED_ENCODING_EXTENSION: True,
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, candidate.url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "response_content_encoding_unsupported"


def test_readable_ats_404_auth_wall_remains_inconclusive(monkeypatch):
    target, candidate = _greenhouse_target()
    response = httpx.Response(
        404,
        headers={"Content-Type": "text/html; charset=utf-8"},
        text="<html><body><h1>Sign in to continue</h1></body></html>",
    )
    monkeypatch.setattr(
        reconciliation,
        "_get_with_safe_redirects",
        lambda *_args, **_kwargs: (response, candidate.url),
    )

    decision = reconciliation._direct_check(target, candidate, Settings())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "authentication_wall"


def test_workday_403_reaches_render_fallback_despite_body_limit_markers(
    monkeypatch,
):
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_encoded_403",
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

    class AllowAllPolicy:
        def check(self, _url):
            return SimpleNamespace(
                allowed=True,
                reason="public_hostname",
                connect_host="93.184.216.34",
                connect_hosts=("93.184.216.34",),
            )

    class FakePinnedClient:
        def __init__(self, *_args, **_kwargs):
            self.url_policy = AllowAllPolicy()

        def close(self):
            pass

    def forbidden_response(_client, url, **_kwargs):
        return httpx.Response(
            403,
            content=b"",
            request=httpx.Request("GET", url),
            extensions={
                reconciliation._RESPONSE_TOO_LARGE_EXTENSION: True,
                reconciliation._RESPONSE_UNSUPPORTED_ENCODING_EXTENSION: True,
            },
        )

    monkeypatch.setattr(reconciliation, "PinnedHttpClient", FakePinnedClient)
    monkeypatch.setattr(reconciliation, "_bounded_http_get", forbidden_response)

    decision = reconciliation._workday_check(
        target,
        candidate,
        Settings(),
        AllowAllPolicy(),
    )

    assert isinstance(decision, reconciliation.RenderRequired)
    assert decision.evidence_code == "workday_403_render_required"
    assert decision.http_status == 403


def test_workday_structured_response_is_bounded_and_inconclusive_when_oversized(
    monkeypatch,
):
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_oversized",
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
    client_closed = False

    class OversizedClient:
        def __init__(
            self,
            _timeout_seconds: float,
            *,
            deadline=None,
            allowed_localhosts=None,
        ):
            pass

        def get(self, url: str, **_kwargs):
            raise AtsResponseTooLargeError(
                "response too large",
                request=httpx.Request("GET", url),
            )

        def close(self):
            nonlocal client_closed
            client_closed = True

    monkeypatch.setattr(
        reconciliation,
        "_BoundedStatusHttpClient",
        OversizedClient,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        Settings(),
        reconciliation.UrlFetchPolicy(),
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "response_too_large"
    assert client_closed is True


def test_workday_stream_deadline_is_deferred_without_failure_observation(
    monkeypatch,
):
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_workday_deadline",
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
    client_closed = False

    class DeadlineClient:
        def __init__(
            self,
            _timeout_seconds: float,
            *,
            deadline: float | None = None,
            allowed_localhosts=None,
        ):
            assert deadline is not None

        def get(self, _url: str, **_kwargs):
            raise reconciliation.StatusRuntimeBudgetExhausted(
                "runtime exhausted while streaming"
            )

        def close(self):
            nonlocal client_closed
            client_closed = True

    monkeypatch.setattr(
        reconciliation,
        "_BoundedStatusHttpClient",
        DeadlineClient,
    )

    decision = reconciliation._workday_check(
        target,
        candidate,
        Settings(),
        reconciliation.UrlFetchPolicy(),
        deadline=time.monotonic() + 30,
    )

    assert isinstance(decision, reconciliation.DeferredRequired)
    assert decision.evidence_code == "runtime_budget_exhausted"
    assert decision.prior_decision is None
    assert client_closed is True


def test_rendered_aggregator_listing_page_cannot_prove_job_is_open():
    source_url = "https://www.linkedin.com/company/acme/jobs"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )

    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_listing",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Software Developer Intern",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": "Software Developer Intern at Acme",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "rendered_identity_inconclusive"


def test_truncated_rendered_page_can_close_from_complete_linkedin_banner():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_truncated_closed",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "No longer accepting applications\n"
                        "Platform Engineer at Acme\n"
                        + ("x" * 20_000)
                    ),
                    "complete_body_text": "No longer accepting applications",
                    "content_truncated": True,
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "closed"
    assert decision.evidence_code == "linkedin_no_longer_accepting"


def test_rendered_linkedin_removed_or_invalid_page_is_closed():
    source_url = "https://www.linkedin.com/jobs/view/9999999977"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_removed",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Example Systems",
        title="Software Development Intern",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "Job not found\n"
                        "This job may have been removed or the job ID is incorrect."
                    ),
                    "complete_body_text": (
                        "Job not found\n"
                        "This job may have been removed or the job ID is incorrect."
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "closed"
    assert decision.evidence_code == "linkedin_job_removed_or_invalid"


def test_truncated_rendered_identity_without_complete_banner_is_inconclusive():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_truncated_identity",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "Platform Engineer at Acme\n"
                        "No longer accepting appli"
                    ),
                    "complete_body_text": "Platform Engineer at Acme",
                    "content_truncated": True,
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "rendered_content_truncated"


def test_model_generated_rendered_marker_cannot_close_job():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_model_output_marker",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "No longer accepting applications\n"
                        "Platform Engineer at Acme"
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "model_output",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "rendered_evidence_untrusted"


def test_trusted_rendered_candidate_is_checked_after_untrusted_primary():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_trusted_candidate_after_model",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "needs_review",
                "posting": {
                    "source_url": source_url,
                    "raw_text": "Platform Engineer at Acme",
                    "confidence": 1.0,
                    "evidence_origin": "model_output",
                },
                "candidates": [
                    {
                        "source_url": source_url,
                        "raw_text": (
                            "No longer accepting applications\n"
                            "Platform Engineer at Acme"
                        ),
                        "confidence": 1.0,
                        "evidence_origin": "browser_dom",
                    },
                ],
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "closed"
    assert decision.evidence_code == "linkedin_no_longer_accepting"



def test_trusted_rendered_terminal_evidence_survives_visible_sign_in_prompt():
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "linkedin",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_linkedin_visible_sign_in",
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

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "Platform Engineer\n"
                        "Acme\n"
                        "Sign in to view more jobs\n"
                        "No longer accepting applications"
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "linkedin_no_longer_accepting"

def test_scheduled_rendering_uses_only_trusted_cdp_stage_of_configured_chain():
    settings = Settings(
        posting_rendered_fallback_provider="chrome_cdp_first",
        posting_rendered_fallback_retries=1,
        scheduled_rendered_extra_domain_groups={
            "jobs.vendor.example": ("assets.vendor-cdn.example",),
        },
    )

    resolver = reconciliation._rendered_resolver_for_remaining_budget(
        settings,
        time.monotonic() + 60,
    )

    assert resolver is not None
    assert isinstance(resolver, ChromeCdpRenderedPageResolver)
    assert resolver.retries == 0
    extra_asset = resolver.url_policy.check_browser_final_url(
        "https://jobs.vendor.example/openings/123",
        "https://assets.vendor-cdn.example/runtime.js",
    )
    assert extra_asset.allowed


def test_scheduled_cdp_page_is_closed_when_rendering_raises():
    source_url = "https://www.linkedin.com/jobs/view/4425912315/"

    class ValidationPage:
        def __init__(self) -> None:
            self.closed = False
            self.url = source_url

        def evaluate(self, _script):
            return True

        def goto(self, *_args, **_kwargs) -> None:
            raise RuntimeError("rendering failed")

        def close(self, **_kwargs) -> None:
            self.closed = True

    page = ValidationPage()

    class ExistingContext:
        def __init__(self):
            self.closed = False

        def cookies(self, urls):
            assert urls == [source_url]
            return []

    existing_context = ExistingContext()

    class ValidationContext:
        def __init__(self):
            self.closed = False
            self.pages = []
            self.routes = []
            self.init_script_installed = False

        def add_init_script(self, *, script):
            assert "RTCPeerConnection" in script
            self.init_script_installed = True

        def route(self, _pattern, handler):
            self.routes.append(handler)

        def unroute(self, _pattern, handler):
            self.routes.remove(handler)

        def route_web_socket(self, _pattern, _handler):
            pass

        def new_page(self):
            assert self.init_script_installed is True
            assert self.routes
            self.pages.append(page)
            return page

        def close(self):
            page.close()
            self.closed = True

    validation_context = ValidationContext()

    class Browser:
        contexts = [existing_context]

        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            assert kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
            return validation_context

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

    class PlaywrightContextManager:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=1,
        retries=0,
        playwright_factory=PlaywrightContextManager,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="Acme",
            title="Software Developer Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert page.closed is True
    assert validation_context.closed is True
    assert existing_context.closed is False


def test_scheduled_cdp_uses_target_close_when_page_close_fails():
    source_url = "https://www.linkedin.com/jobs/view/4425912315/"

    class StubbornPage:
        def __init__(self) -> None:
            self.closed = False
            self.url = source_url

        def evaluate(self, _script):
            return True

        def goto(self, *_args, **_kwargs) -> None:
            raise RuntimeError("rendering failed")

        def close(self, **_kwargs) -> None:
            raise RuntimeError("playwright close failed")

    page = StubbornPage()
    commands: list[tuple[str, object]] = []

    class Session:
        def send(self, command: str, params=None):
            commands.append((command, params))
            if command == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": "owned-target"}}
            assert command == "Target.closeTarget"
            assert params == {"targetId": "owned-target"}
            page.closed = True
            return {"success": True}

        def detach(self):
            commands.append(("detach", None))

    class ExistingContext:
        def cookies(self, urls):
            assert urls == [source_url]
            return []

    class ValidationContext:
        def __init__(self):
            self.pages = []
            self.routes = []
            self.close_attempts = 0
            self.init_script_installed = False
            self.page_handlers = []

        def add_init_script(self, *, script):
            assert "webkitRTCPeerConnection" in script
            self.init_script_installed = True

        def route(self, _pattern, handler):
            self.routes.append(handler)

        def unroute(self, _pattern, handler):
            self.routes.remove(handler)

        def route_web_socket(self, _pattern, _handler):
            pass

        def new_page(self):
            assert self.init_script_installed is True
            self.pages.append(page)
            for handler in list(self.page_handlers):
                handler(page)
            return page

        def on(self, event, handler):
            if event == "page":
                self.page_handlers.append(handler)

        def remove_listener(self, event, handler):
            if event == "page" and handler in self.page_handlers:
                self.page_handlers.remove(handler)

        def close(self):
            self.close_attempts += 1
            raise RuntimeError("context close failed")

        def new_cdp_session(self, target_page):
            assert target_page is page
            return Session()

    validation_context = ValidationContext()

    class Browser:
        contexts = [ExistingContext()]

        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            assert kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
            return validation_context

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

    class PlaywrightContextManager:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=1,
        retries=0,
        playwright_factory=PlaywrightContextManager,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="Acme",
            title="Software Developer Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: owned_context_cleanup_failed"
    assert page.closed is True
    assert validation_context.close_attempts == 3
    assert ("Target.closeTarget", {"targetId": "owned-target"}) in commands
