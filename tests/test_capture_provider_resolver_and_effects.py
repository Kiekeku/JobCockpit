from __future__ import annotations

import pytest
from sqlalchemy import select

from jobcockpit.services import workflows as api_main
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    JobPosting,
    PageCaptureEffect,
    PageCaptureRun,
    PageCaptureSnapshotScope,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.browser_capture_adapters import (
    BrowserCaptureContext,
    CaptureProcessingAction,
    extract_browser_capture_fields,
    resolve_browser_capture_provider,
)
from jobcockpit.ingestion.posting_links import canonicalize_indeed_job_url
from jobcockpit.ingestion.posting_discovery import (
    assess_page_capture,
    capture_processing_action,
    capture_source_identity,
)
from jobcockpit.ingestion.status_reconciliation import status_urls_match
from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy


@pytest.mark.parametrize(
    ("source_url", "provider"),
    [
        ("https://acme.myworkdaysite.com/not-a-job-route", "workday"),
        ("https://www.linkedin.com/jobs/view/1234567890", "linkedin"),
        ("https://jobs.linkedin.com/jobs/view/1234567890", "linkedin"),
        ("https://ca.indeed.com/viewjob?jk=abc123", "indeed"),
        ("https://indeed.ca/viewjob?jk=abc123", "indeed"),
        ("https://www.indeed.ca/viewjob?jk=abc123", "indeed"),
        (
            "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/City/Role_R123",
            "workday",
        ),
        ("https://jobs.example.com/opening/42", "generic"),
        ("https://linkedin.com.evil.example/jobs/view/1234567890", "generic"),
        ("https://evil-linkedin.com/jobs/view/1234567890", "generic"),
    ],
)
def test_provider_factory_matches_dns_boundaries(source_url: str, provider: str):
    adapter = resolve_browser_capture_provider(source_url)

    assert adapter is not None
    assert adapter.provider == provider


@pytest.mark.parametrize(
    "source_url",
    [
        "https://jobs.example.com:notaport/opening/42",
        "https://jobs.example.com:0/opening/42",
        "https://jobs.example.com:65536/opening/42",
    ],
)
def test_provider_factory_rejects_invalid_explicit_ports(source_url: str):
    assert resolve_browser_capture_provider(source_url) is None
    assert capture_source_identity(source_url) is None


def test_indeed_ca_domain_uses_the_complete_indeed_provider_contract():
    source_url = "https://www.indeed.ca/viewjob?jk=3cab9868c348dd54"
    canonical_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    assert canonicalize_indeed_job_url(source_url) == canonical_url
    assert status_urls_match(source_url, canonical_url)

    browser_request = ScheduledRenderedUrlPolicy().check_browser_final_url(
        source_url, "https://assets.indeedcdn.com/runtime.js"
    )
    assert browser_request.allowed
    assert browser_request.reason == "rendered_provider_origin"

    fields = extract_browser_capture_fields(
        BrowserCaptureContext(
            source_url=source_url,
            raw_text="Platform Engineer\nAcme",
            lines=[
                "Page Title: Platform Engineer - Toronto, ON",
                "Platform Engineer",
                "Acme",
            ],
        )
    )
    assert fields.title == "Platform Engineer"
    assert fields.company == "Acme"


def test_workday_render_policy_allows_real_application_asset_origins():
    source_url = (
        "https://example.wd5.myworkdayjobs.com/en-US/Example/job/"
        "Example-Role_JR12345"
    )
    policy = ScheduledRenderedUrlPolicy()

    for asset_url in (
        "https://wd5.myworkday.com/wday/asset/runtime.js",
        "https://wd5.myworkdaycdn.com/wday/asset/application.js",
    ):
        browser_request = policy.check_browser_final_url(source_url, asset_url)
        assert browser_request.allowed
        assert browser_request.reason == "rendered_provider_origin"

    unrelated = policy.check_browser_final_url(
        source_url,
        "https://assets.example.net/runtime.js",
    )
    assert not unrelated.allowed
    assert unrelated.reason == "rendered_cross_origin_blocked"


def test_scheduled_render_policy_accepts_additive_domain_groups_from_env(monkeypatch):
    monkeypatch.setenv(
        "JOBCOCKPIT_SCHEDULED_RENDERED_EXTRA_DOMAIN_GROUPS",
        '{"jobs.vendor.example":["assets.vendor-cdn.example"]}',
    )
    settings = Settings(_env_file=None)
    policy = ScheduledRenderedUrlPolicy(
        extra_domain_groups=settings.scheduled_rendered_extra_domain_groups,
    )

    allowed = policy.check_browser_final_url(
        "https://jobs.vendor.example/openings/123",
        "https://assets.vendor-cdn.example/runtime.js",
    )
    assert allowed.allowed
    assert allowed.reason == "rendered_provider_origin"

    unrelated = policy.check_browser_final_url(
        "https://jobs.other.example/openings/456",
        "https://assets.vendor-cdn.example/runtime.js",
    )
    assert not unrelated.allowed
    assert unrelated.reason == "rendered_cross_origin_blocked"


def test_scheduled_render_policy_rejects_wildcard_extra_domains():
    with pytest.raises(ValueError, match="valid hostname"):
        ScheduledRenderedUrlPolicy(
            extra_domain_groups={"jobs.vendor.example": ("*",)},
        )


def test_workday_public_identity_drops_all_query_parameters():
    identity = capture_source_identity(
        "https://acme.wd1.myworkdayjobs.com/Careers/job/Role_R123?id=secret123&access_token=hidden"
    )

    assert identity is not None
    assert identity.provider == "workday"
    assert identity.public_source_url == (
        "https://acme.wd1.myworkdayjobs.com/Careers/job/Role_R123"
    )
    assert identity.canonical_source_url == identity.public_source_url


@pytest.mark.parametrize(
    ("source_url", "page_text", "provider", "strength", "action", "page_kind"),
    [
        (
            "https://www.linkedin.com/jobs/view/1234567890",
            "Engineer\nCompany: Acme",
            "linkedin",
            "authoritative",
            CaptureProcessingAction.PARSE,
            "posting",
        ),
        (
            "https://www.linkedin.com/jobs/search?keywords=engineer",
            "Search results",
            "linkedin",
            "weak",
            CaptureProcessingAction.REJECT,
            "non_job",
        ),
        (
            "https://www.linkedin.com/feed?currentJobId=1234567890",
            "Engineer\nCompany: Acme",
            "linkedin",
            "weak",
            CaptureProcessingAction.REJECT,
            "non_job",
        ),
        (
            "https://ca.indeed.com/jobs?q=engineer&vjk=abc123",
            "Search results",
            "indeed",
            "authoritative",
            CaptureProcessingAction.PARSE,
            "posting",
        ),
        (
            "https://www.indeed.com/q-Platform-Engineer-jobs.html?vjk=abc123",
            "Engineer\nCompany: Acme\nThis job has expired on Indeed",
            "indeed",
            "authoritative",
            CaptureProcessingAction.PARSE,
            "posting",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/en-US/careers",
            "Search jobs",
            "workday",
            "weak",
            CaptureProcessingAction.REJECT,
            "non_job",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/City/Role_R123",
            "This job requisition is no longer available.",
            "workday",
            "authoritative",
            CaptureProcessingAction.APPLY_TERMINAL,
            "terminal_posting",
        ),
        (
            "https://jobs.example.com/opening/42",
            "Engineer\nCompany: Acme",
            "generic",
            "weak",
            CaptureProcessingAction.PARSE,
            "unknown",
        ),
    ],
)
def test_provider_route_and_fallback_matrix(
    source_url: str,
    page_text: str,
    provider: str,
    strength: str,
    action: CaptureProcessingAction,
    page_kind: str,
):
    identity = capture_source_identity(source_url)
    assessment = assess_page_capture(source_url, page_text)

    assert identity is not None
    assert identity.provider == provider
    assert identity.strength == strength
    assert assessment.page_kind.value == page_kind
    assert capture_processing_action(assessment) is action


def test_rejected_authoritative_capture_remains_visible_on_associated_job(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://www.linkedin.com/jobs/view/9876543210"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "source_url": source_url,
            "raw_text": "Engineer\nCompany: Acme",
        },
    ).json()

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Sign in | LinkedIn",
            "visible_text": "Sign in to continue to LinkedIn",
        },
    ).json()
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "failed"
    assert processed["error_code"] == "capture_authentication_wall"
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["capture_processing"]["capture_id"] == queued["capture_id"]
    assert detail["capture_processing"]["status"] == "failed"
    assert detail["status"] == "active"
    assert detail["capture_processing"]["effect_outcome_code"] == "capture_authentication_wall"

    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        associations = list(
            db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == run.snapshot_id,
                    PageCaptureSnapshotScope.job_id == job["id"],
                )
            ).scalars()
        )
        effect = db.execute(
            select(PageCaptureEffect).where(
                PageCaptureEffect.capture_id == run.capture_id,
                PageCaptureEffect.job_id == job["id"],
            )
        ).scalar_one()
        assert associations
        assert effect.status == "failed"
        assert effect.error_code == "capture_authentication_wall"


def test_identical_weak_capture_deduplication_requires_exact_subject_set(
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
                "raw_text": f"Original {index}\nCompany: Acme",
            },
        ).json()
        for index in (1, 2)
    ]
    payload = {
        "source_url": "https://jobs.example.com/shared-board-route",
        "page_title": "Engineer | Acme",
        "visible_text": "Engineer\nCompany: Acme",
    }

    first = client.post(
        "/api/extension/page-captures",
        json={**payload, "target_job_id": jobs[0]["id"]},
    ).json()
    duplicate = client.post(
        "/api/extension/page-captures",
        json={**payload, "target_job_id": jobs[0]["id"]},
    ).json()
    other_subject = client.post(
        "/api/extension/page-captures",
        json={**payload, "target_job_id": jobs[1]["id"]},
    ).json()

    assert duplicate["capture_id"] == first["capture_id"]
    assert other_subject["capture_id"] != first["capture_id"]
    with session_scope(settings) as db:
        assert db.get(JobPosting, jobs[0]["id"]) is not None
        assert db.get(JobPosting, jobs[1]["id"]) is not None
