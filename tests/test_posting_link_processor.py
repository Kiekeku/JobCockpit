import time
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion import posting_links as posting_links_module
from jobcockpit.ingestion.ats_adapters.base import AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_links import (
    PostingLinkContext,
    canonicalize_posting_url,
    default_rendered_page_resolver,
    posting_url_identity,
    process_posting_link,
    process_posting_links,
)
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    CodexCliRenderedPageResolver,
    ExternalPostingHint,
    PostingDiscoveryResolver,
    PrioritizedRenderedPageResolver,
    PostingResolution,
    PostingResolutionAttempt,
    ResolvedExternalPosting,
    TracedRenderedPageResolver,
)


def _settings(**overrides):
    values = {"posting_rendered_fallback_provider": "disabled"}
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize('rendered', [True, False])
def test_direct_metadata_preview_requires_full_retrieval(monkeypatch, rendered):
    from jobcockpit.ingestion.ats_handlers.apply import MetadataOnlyPostingError

    class PreviewFetcher:
        def resolve(self, *args, **kwargs):
            raise MetadataOnlyPostingError(502, 'metadata preview')

    context = PostingLinkContext(
        source_type='url', source_url='https://jobs.example.com/careers/123',
        raw_text='Old metadata preview that must not be imported',
    )
    body = AtsJob(source_url=context.source_url, raw_text='Retrieved full posting body')
    calls = []
    def fetch_rendered(*args):
        calls.append(args)
        return body if rendered else None
    monkeypatch.setattr(posting_links_module, '_fetch_rendered_direct_posting', fetch_rendered)
    if rendered:
        assert posting_links_module._fetch_direct_posting(context, _settings(), PreviewFetcher()) == body
    else:
        with pytest.raises(MetadataOnlyPostingError):
            posting_links_module._fetch_direct_posting(context, _settings(), PreviewFetcher())
    assert len(calls) == 1


def test_canonicalize_posting_url_only_unwraps_absolute_redirect_targets():
    tracked_absolute = (
        "https://email.example/click?"
        "target=https%3A%2F%2Fjob-boards.greenhouse.io%2Fmaintainx%2Fjobs%2Fgh5146022007x"
    )
    tracked_marker = "https://click.email.amazon.com/track?target=workday"

    assert canonicalize_posting_url(tracked_absolute) == (
        "https://job-boards.greenhouse.io/maintainx/jobs/gh5146022007x"
    )
    assert canonicalize_posting_url(tracked_marker) == tracked_marker


def test_canonicalize_posting_url_unwraps_linkedin_authwall_session_redirect():
    authwall_url = (
        "https://www.linkedin.com/authwall?trk=bf&original_referer=&"
        "sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4423162969"
    )

    assert canonicalize_posting_url(authwall_url) == "https://www.linkedin.com/jobs/view/4423162969"


def test_canonicalize_posting_url_normalizes_linkedin_job_variants_by_job_id():
    assert canonicalize_posting_url(
        "https://ca.linkedin.com/jobs/view/platform-engineer-at-acme-4438093173/?trk=public_jobs"
    ) == "https://www.linkedin.com/jobs/view/4438093173"
    assert canonicalize_posting_url(
        "https://www.linkedin.com/comm/jobs/view/4438093173/"
    ) == "https://www.linkedin.com/jobs/view/4438093173"


def test_canonicalize_posting_url_normalizes_indeed_job_variants_by_job_key():
    canonical_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    assert canonicalize_posting_url(
        "https://www.indeed.com/rc/clk/dl?jk=3CAB9868C348DD54&from=ja&tk=private"
    ) == canonical_url
    assert canonicalize_posting_url(
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54&utm_source=alert"
    ) == canonical_url
    assert canonicalize_posting_url(
        "https://ca.indeed.com/jobs?q=software&vjk=3cab9868c348dd54"
    ) == canonical_url


def test_canonicalize_posting_url_normalizes_workday_job_variants():
    canonical_url = (
        "https://interac.wd3.myworkdayjobs.com/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272"
    )
    assert canonicalize_posting_url(
        "https://interac.wd3.myworkdayjobs.com/en-US/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272?source=LinkedIn"
    ) == canonical_url


def test_posting_url_identity_normalizes_oracle_without_rewriting_fetch_url():
    job_url = (
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/"
        "sites/CX_1/job/11154"
    )
    preview_url = (
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/fr/"
        "sites/CX/requisitions/preview/11154"
    )

    assert posting_url_identity(job_url) == posting_url_identity(preview_url)
    assert canonicalize_posting_url(job_url) == job_url
    assert canonicalize_posting_url(preview_url) == preview_url


def test_posting_link_processor_ignores_non_posting_source_links():
    def fake_importer(request, db, storage, settings):
        raise AssertionError("Ignored links should not be imported")

    results = process_posting_links(
        [
            PostingLinkContext(
                source_type="gmail_alert",
                source_url="https://mapleradar.com/preferences/manage?email=user@example.invalid",
                raw_text="Manage preferences",
                link_text="Manage preferences",
            ),
            PostingLinkContext(
                source_type="outlook_alert",
                source_url="mailto:support@mapleradar.com",
                raw_text="support@mapleradar.com",
                link_text="support@mapleradar.com",
            ),
            PostingLinkContext(
                source_type="job_portal",
                source_url="https://instagram.com/acme",
                raw_text="Follow Acme",
                link_text="Instagram",
            ),
        ],
        db=object(),
        storage=object(),
        settings=_settings(),
        job_importer=fake_importer,
    )

    assert [result.ingestion_status for result in results] == ["ignored", "ignored", "ignored"]
    assert [result.classification.classification for result in results] == [
        "unsubscribe_or_preferences",
        "contact_or_support",
        "social_or_marketing",
    ]


def test_posting_link_processor_does_not_ignore_job_query_terms():
    requests = []

    def fake_importer(request, db, storage, settings):
        requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_search_terms"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="job_portal",
            source_url="http://localhost:8001/ats/generic/acme/jobs/search?terms=software",
            raw_text="Software Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Software Engineer",
            trusted_posting=True,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
    )

    assert result.ingestion_status == "imported"
    assert (
        requests[0].source_url
        == "http://localhost:8001/ats/generic/acme/jobs/search?terms=software"
    )


def test_posting_link_processor_does_not_ignore_privacy_engineer_posting():
    requests = []

    def fake_importer(request, db, storage, settings):
        requests.append(request)
        return SimpleNamespace(payload={"id": "job_privacy_engineer"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="job_portal",
            source_url="http://localhost:8001/ats/generic/acme/jobs/privacy-engineer",
            raw_text="Privacy Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Privacy Engineer",
            trusted_posting=True,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
    )

    assert result.ingestion_status == "imported"
    assert requests[0].source_url == "http://localhost:8001/ats/generic/acme/jobs/privacy-engineer"


def test_posting_link_processor_imports_direct_links_for_any_source_type():
    requests = []

    def fake_importer(request, db, storage, settings):
        requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_outlook"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="outlook_alert",
            source_url="http://localhost:8001/ats/generic/acme/jobs/platform-engineer",
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Platform Engineer",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
    )

    assert result.ingestion_status == "imported"
    assert result.linked_job_id == "job_from_outlook"
    assert result.resolved_url == "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    assert requests[0].source_type == "outlook_alert"
    assert requests[0].source_url == "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    assert requests[0].raw_text.startswith("Platform Engineer")


def test_posting_link_processor_skips_unmet_required_language_before_importing():
    requests = []

    def fake_importer(request, db, storage, settings):
        requests.append(request)
        return SimpleNamespace(payload={"id": "job_language_required"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="job_portal",
            source_url="http://localhost:8001/ats/generic/acme/jobs/platform-engineer",
            raw_text="Platform Engineer\nCompany: Acme\nFrench required for this role.",
            link_text="Platform Engineer",
            trusted_posting=True,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        language_filter_profile_data={
            "language_filter_enabled": True,
            "languages": [
                {"language": "English", "proficiency": "fluent"},
                {"language": "French", "proficiency": "none"},
            ],
        },
    )

    assert result.ingestion_status == "skipped_by_language_filter"
    assert result.error_message == "unmet_required_language: French"
    assert requests == []


def test_posting_link_processor_fetches_direct_link_archive_before_importing():
    imported_requests = []
    fetched_urls = []
    source_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"
    final_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer?resolved=1"
    archive = SourceArchive(
        source_url=source_url,
        final_url=final_url,
        status_code=200,
        content_type="text/html; charset=utf-8",
        body=b"<html><h1>Platform Engineer</h1><p>Company: Acme</p></html>",
        headers={"etag": '"posting-v1"'},
    )

    class FakePostingFetcher:
        def resolve(self, apply_url, context=None):
            fetched_urls.append(apply_url)
            return AtsJob(
                source_url=final_url,
                raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nPython APIs",
                source_archive=archive,
            )

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_fetch"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="outlook_alert",
            source_url=source_url,
            raw_text="Email alert teaser that should not be archived as the posting",
            link_text="Platform Engineer job",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_fetcher=FakePostingFetcher(),
    )

    assert result.ingestion_status == "imported"
    assert result.linked_job_id == "job_from_fetch"
    assert result.resolved_url == final_url
    assert fetched_urls == [source_url]
    assert imported_requests[0].source_url == final_url
    assert imported_requests[0].raw_text.startswith("Platform Engineer")
    assert imported_requests[0].source_archive == archive


def test_posting_link_processor_falls_back_when_direct_link_fetch_fails():
    imported_requests = []
    source_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"

    class FakePostingFetcher:
        def resolve(self, apply_url, context=None):
            raise AtsProviderError(502, "request failed")

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_email_text"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="outlook_alert",
            source_url=source_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Platform Engineer job",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_fetcher=FakePostingFetcher(),
    )

    assert result.ingestion_status == "imported"
    assert result.resolved_url == source_url
    assert (
        imported_requests[0].raw_text == "Platform Engineer\nCompany: Acme\nLocation: Remote Canada"
    )
    assert imported_requests[0].source_archive is None


def test_posting_link_processor_uses_rendered_fallback_for_direct_link_fetch_failures(monkeypatch):
    imported_requests = []
    rendered_hints = []
    source_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"

    class FakePostingFetcher:
        def resolve(self, apply_url, context=None):
            raise AtsProviderError(502, "request failed")

    class FakeRenderedResolver:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": "Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nRendered ATS page",
                    "confidence": 0.91,
                    "evidence": "Extracted from rendered page with Playwright",
                },
            }

    monkeypatch.setattr(
        posting_links_module,
        "default_rendered_page_resolver",
        lambda settings: FakeRenderedResolver(),
    )

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_rendered"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="ats_custom_portal",
            source_url=source_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Platform Engineer job",
        ),
        db=object(),
        storage=object(),
        settings=Settings(
            ats_mock_port=8001,
            posting_rendered_fallback_provider="codex_cli",
        ),
        job_importer=fake_importer,
        posting_fetcher=FakePostingFetcher(),
    )

    assert result.ingestion_status == "imported"
    assert result.linked_job_id == "job_from_rendered"
    assert result.resolved_url == source_url
    assert rendered_hints[0].source_url == source_url
    assert imported_requests[0].raw_text.startswith("Platform Engineer")
    assert "Rendered ATS page" in imported_requests[0].raw_text


def test_default_rendered_page_resolver_uses_rendered_codex_config():
    resolver = default_rendered_page_resolver(
        _settings(
            posting_rendered_fallback_provider="codex_cli",
            posting_rendered_fallback_timeout_seconds=240,
            posting_rendered_fallback_retries=2,
            posting_rendered_fallback_min_interval_seconds=12.5,
            codex_cli_ignore_user_config=True,
            codex_cli_rendered_ignore_user_config=False,
        )
    )

    assert resolver is not None
    assert isinstance(resolver, TracedRenderedPageResolver)
    assert resolver.provider == "codex_rendered"
    assert resolver.timeout_seconds == 240
    assert resolver.retries == 2
    assert resolver.min_start_interval_seconds == 12.5
    assert resolver.ignore_user_config is False


def test_default_rendered_page_resolver_uses_playwright_profile_config(tmp_path):
    resolver = default_rendered_page_resolver(
        _settings(
            posting_rendered_fallback_provider="playwright_profile",
            posting_rendered_fallback_timeout_seconds=240,
            posting_rendered_fallback_retries=2,
            posting_rendered_fallback_min_interval_seconds=12.5,
            playwright_user_data_dir=tmp_path / "profile",
            playwright_profile_directory="Default",
            playwright_channel="chrome",
            playwright_headless=False,
        )
    )

    assert resolver is not None
    assert isinstance(resolver, TracedRenderedPageResolver)
    assert resolver.provider == "playwright_profile"
    assert resolver.user_data_dir == tmp_path / "profile"
    assert resolver.profile_directory == "Default"
    assert resolver.channel == "chrome"
    assert resolver.headless is False
    assert resolver.timeout_seconds == 240
    assert resolver.retries == 2
    assert resolver.min_start_interval_seconds == 12.5


def test_default_rendered_page_resolver_uses_chrome_cdp_config(tmp_path):
    executable_path = tmp_path / "chrome.exe"
    user_data_dir = tmp_path / "cdp-profile"
    resolver = default_rendered_page_resolver(
        _settings(
            posting_rendered_fallback_provider="chrome_cdp",
            posting_rendered_fallback_timeout_seconds=240,
            posting_rendered_fallback_retries=2,
            posting_rendered_fallback_min_interval_seconds=12.5,
            chrome_cdp_endpoint_url="http://localhost:9222",
            chrome_cdp_auto_start=True,
            chrome_cdp_executable_path=executable_path,
            chrome_cdp_user_data_dir=user_data_dir,
            chrome_cdp_profile_directory="Profile 2",
            playwright_headless=False,
        )
    )

    assert isinstance(resolver, TracedRenderedPageResolver)
    assert resolver.provider == "chrome_cdp"
    assert isinstance(resolver.inner_resolver, ChromeCdpRenderedPageResolver)
    assert resolver.endpoint_url == "http://localhost:9222"
    assert resolver.timeout_seconds == 240
    assert resolver.retries == 2
    assert resolver.min_start_interval_seconds == 12.5
    assert resolver.auto_start is True
    assert resolver.executable_path == executable_path
    assert resolver.user_data_dir == user_data_dir
    assert resolver.profile_directory == "Profile 2"
    assert resolver.headless is False


def test_traced_rendered_resolver_records_single_provider_attempt():
    class FakeRenderedResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="codex_playwright_low_confidence",
            )

    resolver = TracedRenderedPageResolver(
        inner_resolver=FakeRenderedResolver(),
        provider="codex_rendered",
    )
    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "source_render",
            "provider": "codex_rendered",
            "status": "needs_review",
            "reason_code": "codex_playwright_low_confidence",
        }
    ]


def test_default_rendered_page_resolver_uses_cdp_first_config(tmp_path):
    executable_path = tmp_path / "chrome.exe"
    user_data_dir = tmp_path / "cdp-profile"
    resolver = default_rendered_page_resolver(
        _settings(
            posting_rendered_fallback_provider="chrome_cdp_first",
            posting_rendered_fallback_timeout_seconds=240,
            posting_rendered_fallback_retries=2,
            posting_rendered_fallback_min_interval_seconds=12.5,
            chrome_cdp_endpoint_url="http://localhost:9222",
            chrome_cdp_auto_start=True,
            chrome_cdp_executable_path=executable_path,
            chrome_cdp_user_data_dir=user_data_dir,
            chrome_cdp_profile_directory="Profile 3",
            playwright_headless=False,
            codex_cli_rendered_ignore_user_config=False,
        )
    )

    assert isinstance(resolver, PrioritizedRenderedPageResolver)
    assert isinstance(resolver.primary_resolver, ChromeCdpRenderedPageResolver)
    assert resolver.primary_resolver.endpoint_url == "http://localhost:9222"
    assert resolver.primary_resolver.timeout_seconds == 240
    assert resolver.primary_resolver.auto_start is True
    assert resolver.primary_resolver.executable_path == executable_path
    assert resolver.primary_resolver.user_data_dir == user_data_dir
    assert resolver.primary_resolver.profile_directory == "Profile 3"
    assert resolver.primary_resolver.headless is False
    assert isinstance(resolver.fallback_resolver, CodexCliRenderedPageResolver)
    assert resolver.fallback_resolver.ignore_user_config is False


def test_prioritized_rendered_resolver_uses_fallback_only_after_primary_failure():
    calls = []
    posting = ResolvedExternalPosting(
        source_url="https://ca.indeed.com/viewjob?jk=abc123",
        raw_text="Platform Engineer\nCompany: Acme",
        confidence=0.95,
    )

    class FakeResolver:
        def __init__(self, label, resolution):
            self.label = label
            self.resolution = resolution

        def resolve(self, hint):
            calls.append(self.label)
            return self.resolution

    resolver = PrioritizedRenderedPageResolver(
        primary_resolver=FakeResolver(
            "cdp", PostingResolution(status="needs_review", error="cdp_down")
        ),
        fallback_resolver=FakeResolver(
            "codex", PostingResolution(status="resolved", posting=posting)
        ),
    )

    resolution = resolver.resolve(object())

    assert resolution.status == "resolved"
    assert calls == ["cdp", "codex"]

    calls.clear()
    resolver = PrioritizedRenderedPageResolver(
        primary_resolver=FakeResolver("cdp", PostingResolution(status="resolved", posting=posting)),
        fallback_resolver=FakeResolver(
            "codex", PostingResolution(status="needs_review", error="unused")
        ),
    )

    resolution = resolver.resolve(object())

    assert resolution.status == "resolved"


def test_prioritized_rendered_resolver_does_not_bypass_primary_url_policy_block():
    fallback_calls = []

    class BlockedPrimaryResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_url_policy_blocked: private_network_blocked",
            )

    class UnexpectedFallbackResolver:
        def resolve(self, hint):
            fallback_calls.append(hint)
            return PostingResolution(status="not_found")

    resolver = PrioritizedRenderedPageResolver(
        primary_resolver=BlockedPrimaryResolver(),
        fallback_resolver=UnexpectedFallbackResolver(),
    )

    resolution = resolver.resolve(object())

    assert resolution.error == "chrome_cdp_url_policy_blocked: private_network_blocked"
    assert fallback_calls == []


def test_posting_link_processor_blocks_direct_link_when_fetch_hits_url_policy():
    source_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"

    class FakePostingFetcher:
        def resolve(self, apply_url, context=None):
            raise AtsProviderError(400, "Apply URL is blocked by policy: private_network_blocked")

    def fake_importer(request, db, storage, settings):
        raise AssertionError(
            "URL policy blocks discovered during fetch must not import fallback text"
        )

    result = process_posting_link(
        PostingLinkContext(
            source_type="outlook_alert",
            source_url=source_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="Platform Engineer job",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_fetcher=FakePostingFetcher(),
    )

    assert result.ingestion_status == "blocked_by_url_policy"
    assert result.error_message == "private_network_blocked"
    assert result.resolved_url == source_url


def test_posting_link_processor_imports_trusted_portal_posting_without_job_terms():
    requests = []

    def fake_importer(request, db, storage, settings):
        requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_portal"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="ats_custom_portal",
            source_url="http://localhost:8001/openings/12345",
            raw_text="Backend Developer\nCompany: Acme\nLocation: Toronto, ON",
            trusted_posting=True,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
    )

    assert result.ingestion_status == "imported"
    assert result.linked_job_id == "job_from_portal"
    assert requests[0].source_type == "ats_custom_portal"
    assert requests[0].source_url == "http://localhost:8001/openings/12345"


def test_posting_link_processor_does_not_resolve_external_apply_url_for_trusted_snapshot(
    allow_public_dns,
):
    imported_requests = []
    linkedin_url = "https://www.linkedin.com/jobs/view/424242"
    allow_public_dns({"www.linkedin.com"})

    class UnexpectedPostingResolver:
        def resolve(self, hint):
            raise AssertionError("trusted provider snapshot should bypass external resolution")

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_from_trusted_snapshot"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="ats_hanzilla",
            source_url=linkedin_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            portal_source_url="https://jobs.hanzilla.co/jobs/platform-engineer-acme-123/",
            trusted_posting=True,
            fetch_source_archive=False,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=UnexpectedPostingResolver(),
    )

    assert result.ingestion_status == "imported"
    assert result.classification.classification == "probable_job_posting"
    assert result.resolved_url == linkedin_url
    assert imported_requests[0].source_url == linkedin_url
    assert imported_requests[0].source_resolution["kind"] == "aggregator_fallback"
    assert imported_requests[0].source_resolution["official_discovery_status"] == "not_attempted"
    assert imported_requests[0].source_resolution["reason_code"] == "source_discovery_pending"
    assert result.source_resolution == imported_requests[0].source_resolution
    assert imported_requests[0].portal_source_url == (
        "https://jobs.hanzilla.co/jobs/platform-engineer-acme-123/"
    )


@pytest.mark.parametrize("portal_url", [None, "https://jobs.example.test/platform-engineer-123"])
def test_posting_link_processor_resolves_external_job_board_links_before_importing(portal_url):
    imported_requests = []
    resolved_hints = []
    linkedin_url = "https://www.linkedin.com/jobs/view/424242"
    official_url = "http://localhost:8001/ats/generic/acme/jobs/platform-engineer"

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": "Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                    "confidence": 0.91,
                    "evidence": "Matched official careers page",
                },
            }

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_resolved"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="outlook_alert",
            source_url=linkedin_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nLinkedIn source",
            link_text="View job",
            surrounding_text="View job - Platform Engineer - Acme - Remote Canada",
            portal_source_url=portal_url,
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "imported"
    assert result.linked_job_id == "job_resolved"
    assert result.original_url == linkedin_url
    assert result.resolved_url == official_url
    assert resolved_hints[0].source_kind == "linkedin"
    assert resolved_hints[0].company == "Acme"
    assert resolved_hints[0].title == "Platform Engineer"
    assert imported_requests[0].source_type == "outlook_alert"
    assert imported_requests[0].source_url == official_url
    assert imported_requests[0].portal_source_url == (portal_url or linkedin_url)
    assert (
        "Linkedin Source URL: https://www.linkedin.com/jobs/view/424242"
        in imported_requests[0].raw_text
    )


def test_posting_link_processor_resolves_linkedin_authwall_redirect_before_direct_fetch():
    imported_requests = []
    resolved_hints = []
    authwall_url = (
        "https://www.linkedin.com/authwall?trk=bf&original_referer=&"
        "sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4423162969"
    )
    linkedin_url = "https://www.linkedin.com/jobs/view/4423162969"
    official_url = "http://localhost:8001/ats/generic/acme/jobs/analytics-engineer"

    class FailingPostingFetcher:
        def resolve(self, apply_url, context=None):
            raise AssertionError(
                "LinkedIn authwall links should use external resolution, not direct fetch"
            )

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": "Junior Analytics Engineer\nCompany: Lightspeed Commerce\nLocation: Montreal, QC",
                    "confidence": 0.91,
                    "evidence": "Matched official careers page",
                },
            }

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_resolved_from_authwall"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=authwall_url,
            raw_text="Junior Analytics Engineer\nCompany: Lightspeed Commerce\nLocation: Montreal, Quebec",
            link_text="View job",
            surrounding_text="Junior Analytics Engineer - Lightspeed Commerce - Montreal, Quebec",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
        posting_fetcher=FailingPostingFetcher(),
    )

    assert result.ingestion_status == "imported"
    assert result.original_url == linkedin_url
    assert result.resolved_url == official_url
    assert resolved_hints[0].source_kind == "linkedin"
    assert resolved_hints[0].source_url == linkedin_url
    assert imported_requests[0].source_url == official_url


def test_posting_link_batch_resolves_external_links_in_parallel_for_any_source(monkeypatch):
    imported_requests = []
    resolver_barrier = Barrier(2, timeout=2)
    barrier_passed = []
    linkedin_urls = [
        "https://www.linkedin.com/jobs/view/424242",
        "https://www.linkedin.com/jobs/view/525252",
    ]

    class FakePostingResolver:
        def resolve(self, hint):
            resolver_barrier.wait()
            barrier_passed.append(hint.source_url)
            role_slug = hint.title.lower().replace(" ", "-")
            return {
                "status": "resolved",
                "posting": {
                    "source_url": f"http://localhost:8001/ats/generic/acme/jobs/{role_slug}",
                    "raw_text": f"{hint.title}\nCompany: Acme\nLocation: Remote Canada",
                    "confidence": 0.91,
                    "evidence": "Matched official careers page",
                },
            }

    monkeypatch.setattr(
        posting_links_module,
        "default_posting_resolver",
        lambda settings: FakePostingResolver(),
    )

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": f"job_{len(imported_requests)}"}, created=True)

    results = process_posting_links(
        [
            PostingLinkContext(
                source_type="outlook_alert",
                source_url=linkedin_urls[0],
                raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                link_text="Platform Engineer",
                surrounding_text="Platform Engineer - Acme - Remote Canada",
            ),
            PostingLinkContext(
                source_type="ats_custom_portal",
                source_url=linkedin_urls[1],
                raw_text="Data Engineer\nCompany: Acme\nLocation: Remote Canada",
                link_text="Data Engineer",
                surrounding_text="Data Engineer - Acme - Remote Canada",
            ),
        ],
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001, posting_external_resolution_concurrency=2),
        job_importer=fake_importer,
    )

    assert [result.ingestion_status for result in results] == ["imported", "imported"]
    assert [request.source_type for request in imported_requests] == [
        "outlook_alert",
        "ats_custom_portal",
    ]
    assert sorted(barrier_passed) == sorted(linkedin_urls)


def test_posting_link_batch_caps_parallelism_when_rendered_fallback_is_enabled(monkeypatch):
    active_count = 0
    max_active_count = 0
    lock = Lock()
    linkedin_urls = [
        "https://www.linkedin.com/jobs/view/111",
        "https://www.linkedin.com/jobs/view/222",
        "https://www.linkedin.com/jobs/view/333",
    ]

    class FakePostingResolver:
        def resolve(self, hint):
            nonlocal active_count, max_active_count
            with lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
            try:
                time.sleep(0.05)
                return {"status": "not_found"}
            finally:
                with lock:
                    active_count -= 1

    monkeypatch.setattr(
        posting_links_module,
        "default_posting_resolver",
        lambda settings: FakePostingResolver(),
    )

    results = process_posting_links(
        [
            PostingLinkContext(
                source_type="gmail_alert",
                source_url=url,
                raw_text=f"Platform Engineer at Acme\nLocation: Remote Canada\n{url}",
                link_text="Platform Engineer",
                surrounding_text="Platform Engineer at Acme - Remote Canada",
            )
            for url in linkedin_urls
        ],
        db=object(),
        storage=object(),
        settings=_settings(
            ats_mock_port=8001,
            posting_external_resolution_concurrency=4,
            posting_rendered_fallback_provider="codex_cli",
            posting_rendered_fallback_concurrency=2,
        ),
        job_importer=lambda request, db, storage, settings: SimpleNamespace(
            payload={"id": "unused"}, created=False
        ),
    )

    assert [result.ingestion_status for result in results] == [
        "needs_external_resolution",
        "needs_external_resolution",
        "needs_external_resolution",
    ]
    assert max_active_count == 2


def test_posting_link_processor_keeps_indeed_only_links_for_review():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {"status": "not_found"}

    def fake_importer(request, db, storage, settings):
        raise AssertionError("Indeed-only links should not be imported directly")

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=indeed_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="View on Indeed",
            surrounding_text="Platform Engineer at Acme - Remote Canada",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "needs_external_resolution"
    assert result.error_message == "official_posting_not_found"
    assert result.original_url == indeed_url
    assert result.resolved_url == indeed_url
    assert resolved_hints[0].source_kind == "indeed"
    assert resolved_hints[0].company == "Acme"
    assert resolved_hints[0].title == "Platform Engineer"


def test_posting_link_processor_preserves_unresolved_candidate_evidence():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    class FakePostingResolver:
        def resolve(self, hint):
            return {
                "status": "needs_review",
                "error": "codex_playwright_low_confidence",
                "candidates": [
                    {
                        "source_url": indeed_url,
                        "raw_text": "Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                        "confidence": 0.72,
                        "evidence": "Rendered page had title and company but confidence was below import threshold.",
                    }
                ],
            }

    def fake_importer(request, db, storage, settings):
        raise AssertionError("Low-confidence candidates should remain unresolved")

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=indeed_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="View on Indeed",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "needs_external_resolution"
    assert result.error_message == "codex_playwright_low_confidence"
    assert "candidate_url=" not in result.error_message
    assert "confidence=" not in result.error_message
    assert "Rendered page had title and company" not in result.error_message


def test_unresolved_resolution_cannot_claim_resolved_official_outcome():
    outcome = posting_links_module.official_discovery_outcome_for_resolution(
        PostingResolution(
            status="needs_review",
            error="codex_cli_failed: private provider output",
            official_discovery_status="resolved",
        )
    )

    assert outcome.status == "failed"
    assert outcome.reason_code == "codex_cli_failed"






def test_unresolved_resolution_carries_sanitized_source_trace():
    source_url = "https://ca.indeed.com/viewjob?jk=unresolved-trace"

    class UnresolvedResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="codex_cli_failed: private provider output",
                attempts=(
                    PostingResolutionAttempt(
                        stage="official_search",
                        provider="codex_cli",
                        status="needs_review",
                        reason_code="codex_cli_failed: private provider output",
                        candidate_count=0,
                    ),
                ),
            )

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=source_url,
            raw_text="Title: Platform Engineer\nCompany: Acme",
        ),
        db=object(),
        storage=object(),
        settings=_settings(),
        posting_resolver=UnresolvedResolver(),
    )

    assert result.ingestion_status == "needs_external_resolution"
    assert result.source_resolution == {
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
def test_candidates_found_remains_a_concise_durable_link_summary():
    result = SimpleNamespace(
        official_discovery_outcome=posting_links_module.official_discovery_outcome(
            "official_candidates_found",
            status="candidates_found",
        ),
        ingestion_status="fallback_imported",
        error_message="",
    )

    assert (
        posting_links_module.durable_posting_link_error_message(result)
        == "official_candidates_found"
    )
def test_successful_official_import_has_no_durable_link_error():
    result = SimpleNamespace(
        official_discovery_outcome=posting_links_module.official_discovery_outcome(
            "official_candidates_found",
            status="candidates_found",
        ),
        ingestion_status="imported",
        error_message="",
    )

    assert posting_links_module.durable_posting_link_error_message(result) == ""




def test_terminal_link_error_takes_precedence_over_discovery_summary():
    for ingestion_status, error_message in (
        ("blocked_by_url_policy", "private_network_blocked"),
        ("skipped_by_language_filter", "unmet_required_language: French"),
    ):
        result = SimpleNamespace(
            official_discovery_outcome=posting_links_module.official_discovery_outcome(
                "official_candidates_found",
                status="candidates_found",
            ),
            ingestion_status=ingestion_status,
            error_message=error_message,
        )

        assert posting_links_module.durable_posting_link_error_message(result) == error_message


def test_typed_official_search_outcome_is_not_rederived_from_final_error():
    outcome = posting_links_module.official_discovery_outcome_for_resolution(
        PostingResolution(
            status="resolved",
            error="official_candidate_render_needs_review",
            official_search_outcome=posting_links_module.official_discovery_outcome(
                "official_posting_resolved",
                status="resolved",
            ),
        )
    )

    assert outcome.status == "resolved"
    assert outcome.reason_code == "official_posting_resolved"


def test_successful_official_import_persists_typed_search_outcome():
    official_url = "https://careers.acme.example/jobs/platform-engineer"
    resolution = PostingResolution(
        status="resolved",
        posting=ResolvedExternalPosting(
            source_url=official_url,
            raw_text="Platform Engineer at Acme",
            confidence=0.95,
            evidence="Validated official posting",
        ),
        official_search_outcome=posting_links_module.official_discovery_outcome(
            "official_candidates_found",
            status="candidates_found",
        ),
    )

    source_resolution = posting_links_module.source_resolution_for_resolution(
        "https://ca.indeed.com/viewjob?jk=123",
        resolution,
    )

    assert source_resolution == {
        "kind": "official_discovery",
        "path": "discovered_ats",
        "official_discovery_status": "candidates_found",
        "reason_code": "official_candidates_found",
        "official_url": official_url,
    }


def test_posting_link_processor_imports_same_aggregator_fallback_with_status_metadata():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"
    imported_requests = []

    class FakePostingResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=linkedin_url,
                    raw_text="Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC",
                    confidence=0.91,
                    evidence="Rendered aggregator fallback after official posting search found no match",
                ),
                error="codex_cli_failed: private provider output",
            )

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_linkedin_fallback"}, created=False)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=linkedin_url,
            raw_text="Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            link_text="Intern, Digital Ship NLP",
            surrounding_text="Intern, Digital Ship NLP - Seaspan ULC - North Vancouver",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.linked_job_id == "job_linkedin_fallback"
    assert result.resolved_url == linkedin_url
    assert result.error_message == "codex_cli_failed"
    assert result.official_discovery_outcome is not None
    assert result.official_discovery_outcome.status == "failed"
    assert result.official_discovery_outcome.reason_code == "codex_cli_failed"
    assert result.payload["source_resolution"]["kind"] == "aggregator_fallback"
    assert result.payload["source_resolution"]["official_discovery_status"] == "failed"
    assert (
        result.payload["source_resolution"]["official_discovery_error"]
        == "codex_cli_failed"
    )
    assert imported_requests[0].source_url == linkedin_url
    assert "Source Resolution Kind: aggregator_fallback" in imported_requests[0].raw_text
    assert "Official Discovery Status: failed" in imported_requests[0].raw_text
    assert "Official Discovery Error: codex_cli_failed" in imported_requests[0].raw_text


def test_posting_link_processor_prefers_source_hint_company_for_aggregator_fallback():
    linkedin_url = "https://www.linkedin.com/jobs/view/4427321105/"
    imported_requests = []

    class FakePostingResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=linkedin_url,
                    raw_text=(
                        "Junior Data Scientist\n"
                        "Company: Junior\n"
                        "Location: Remote Canada\n"
                        "Expired LinkedIn content"
                    ),
                    confidence=0.86,
                    evidence="Rendered expired LinkedIn fallback",
                ),
                source_path="aggregator_rendered_fallback",
                official_discovery_status="failed",
                error="official_posting_not_found",
            )

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_why_hiring_fallback"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=linkedin_url,
            raw_text=(
                "Junior Data Scientist\n"
                "Company: Why Hiring\n"
                "Email Row: Junior Data Scientist · Why Hiring"
            ),
            link_text="Junior Data Scientist",
            surrounding_text="Junior Data Scientist · Why Hiring ·",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "fallback_imported"
    assert imported_requests[0].raw_text.startswith(
        "Title: Junior Data Scientist\nCompany: Why Hiring\nJunior Data Scientist\nCompany: Junior"
    )


def test_posting_link_processor_marks_structured_source_hint_fallback_with_status_metadata():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"
    imported_requests = []

    class EmptySearchClient:
        last_error = ""

        def search(self, query, limit):
            return []

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_linkedin_structured_fallback"}, created=False)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=linkedin_url,
            raw_text="Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC\nLocation: North Vancouver, BC",
            link_text="Intern, Digital Ship NLP",
            surrounding_text="Intern, Digital Ship NLP - Seaspan ULC - North Vancouver",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=PostingDiscoveryResolver(search_client=EmptySearchClient()),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.linked_job_id == "job_linkedin_structured_fallback"
    assert result.resolved_url == linkedin_url
    assert result.payload["source_resolution"] == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": linkedin_url,
        "attempts": [
            {
                "stage": "official_search",
                "provider": "search",
                "status": "not_found",
                "reason_code": "official_posting_not_found",
                "candidate_count": 0,
            },
            {
                "stage": "official_search",
                "provider": "search",
                "status": "not_found",
                "reason_code": "official_posting_not_found",
                "candidate_count": 0,
            },
            {
                "stage": "official_search",
                "provider": "search",
                "status": "not_found",
                "reason_code": "official_posting_not_found",
                "candidate_count": 0,
            },
            {
                "stage": "structured_fallback",
                "provider": "structured_hint",
                "status": "resolved",
                "reason_code": "structured_metadata_imported",
                "candidate_url": linkedin_url,
                "confidence": 0.86,
            },
        ],
    }
    assert "Source Resolution Kind: aggregator_fallback" in imported_requests[0].raw_text
    assert "Official Discovery Status: not_found" in imported_requests[0].raw_text


def test_structured_aggregator_fallback_preserves_complete_source_body():
    linkedin_url = "https://www.linkedin.com/jobs/view/4426152384"
    imported_requests = []
    source_body = (
        "Title: Machine Learning Intern\n"
        "Company: Example Design\n"
        "Location: Toronto, ON\n"
        f"Description: {'platform engineering ' * 140}\n"
        "The 2026 Canada Internship program runs for 16 weeks "
        "(Sept 1\u2013 Dec 18 2026)."
    )

    class EmptySearchClient:
        last_error = ""

        def search(self, query, limit):
            return []

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_complete_fallback"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="ats_hanzilla",
            source_url=linkedin_url,
            raw_text=source_body,
            link_text="Machine Learning Intern",
            surrounding_text="Machine Learning Intern - Example Design - Toronto, ON",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=PostingDiscoveryResolver(search_client=EmptySearchClient()),
    )

    assert result.ingestion_status == "fallback_imported"
    assert source_body in imported_requests[0].raw_text
    assert "The 2026 Canada Internship program runs for 16 weeks" in imported_requests[0].raw_text
    parsed = parse_job_text(imported_requests[0].raw_text)
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-18"
    assert parsed.employment_timing.duration_months == 4


def test_posting_link_processor_preserves_search_failure_reason_for_structured_fallback():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"
    imported_requests = []

    class FailedSearchClient:
        last_error = "codex_cli_failed: model unavailable"

        def search(self, query, limit):
            return []

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(
            payload={"id": "job_linkedin_structured_failure_fallback"}, created=False
        )

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=linkedin_url,
            raw_text="Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC\nLocation: North Vancouver, BC",
            link_text="Intern, Digital Ship NLP",
            surrounding_text="Intern, Digital Ship NLP - Seaspan ULC - North Vancouver",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=PostingDiscoveryResolver(search_client=FailedSearchClient()),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.payload["source_resolution"] == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "failed",
        "reason_code": "codex_cli_failed",
        "official_discovery_error": "codex_cli_failed",
        "fallback_url": linkedin_url,
        "attempts": [
            {
                "stage": "official_search",
                "provider": "search",
                "status": "needs_review",
                "reason_code": "codex_cli_failed",
                "candidate_count": 0,
            },
            {
                "stage": "structured_fallback",
                "provider": "structured_hint",
                "status": "resolved",
                "reason_code": "structured_metadata_imported",
                "candidate_url": linkedin_url,
                "confidence": 0.86,
            },
        ],
    }
    assert imported_requests[0].source_resolution == result.payload["source_resolution"]
    assert "model unavailable" not in imported_requests[0].raw_text
    assert "Resolution Evidence:" in imported_requests[0].raw_text
    assert "codex_cli_failed" in imported_requests[0].raw_text


def test_posting_link_processor_persists_bounded_resolution_attempts():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    imported_requests = []

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_traced_fallback"}, created=True)

    class TracedFallbackResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": indeed_url,
                    "raw_text": "Title: Platform Engineer\nCompany: Acme",
                    "confidence": 0.86,
                    "evidence": "Structured source metadata.",
                },
                "source_path": "aggregator_structured_fallback",
                "official_discovery_status": "not_found",
                "reason_code": "official_posting_not_found",
                "error": "official_posting_not_found",
                "attempts": [
                    {
                        "stage": "official_search",
                        "provider": "codex_cli",
                        "status": "not_found",
                        "reason_code": "official_posting_not_found",
                        "candidate_count": 0,
                    },
                    {
                        "stage": "structured_fallback",
                        "provider": "structured_hint",
                        "status": "resolved",
                        "reason_code": "structured_metadata_imported",
                        "candidate_url": indeed_url,
                        "confidence": 0.86,
                    },
                ],
            }

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=indeed_url,
            raw_text="Title: Platform Engineer\nCompany: Acme",
        ),
        db=object(),
        storage=object(),
        settings=_settings(),
        job_importer=fake_importer,
        posting_resolver=TracedFallbackResolver(),
    )

    source_resolution = result.payload["source_resolution"]
    assert source_resolution["reason_code"] == "official_posting_not_found"
    assert source_resolution["attempts"] == [
        {
            "stage": "official_search",
            "provider": "codex_cli",
            "status": "not_found",
            "reason_code": "official_posting_not_found",
            "candidate_count": 0,
        },
        {
            "stage": "structured_fallback",
            "provider": "structured_hint",
            "status": "resolved",
            "reason_code": "structured_metadata_imported",
            "candidate_url": indeed_url,
            "confidence": 0.86,
        },
    ]
    assert imported_requests[0].source_resolution == source_resolution


def test_posting_link_processor_preserves_normalized_source_resolution_fallback_metadata():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"
    imported_requests = []

    class FakePostingResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": linkedin_url,
                    "raw_text": "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC",
                    "confidence": 0.91,
                    "evidence": "Imported from source metadata after official posting search failed",
                },
                "source_resolution": {
                    "kind": "aggregator_fallback",
                    "official_discovery_status": "not_found",
                    "official_discovery_error": "official_posting_not_found",
                },
            }

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_linkedin_normalized_fallback"}, created=False)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=linkedin_url,
            raw_text="Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            link_text="Intern, Digital Ship NLP",
            surrounding_text="Intern, Digital Ship NLP - Seaspan ULC - North Vancouver",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.payload["source_resolution"] == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": linkedin_url,
    }
    assert "Source Resolution Kind: aggregator_fallback" in imported_requests[0].raw_text


def test_official_discovery_reason_variants_map_to_statuses():
    assert (
        posting_links_module.official_discovery_status_from_reason("official_posting_not_found")
        == "not_found"
    )
    assert (
        posting_links_module.official_discovery_status_from_reason("official posting not found")
        == "not_found"
    )
    assert posting_links_module.official_discovery_status_from_reason("not_found") == "not_found"
    assert (
        posting_links_module.official_discovery_status_from_reason("codex_agentic_resolver_timeout")
        == "timeout"
    )


def test_posting_link_processor_imports_indeed_fallback_resolution():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    imported_requests = []
    archive = SourceArchive(
        source_url=indeed_url,
        final_url=indeed_url,
        status_code=200,
        content_type="text/html",
        body=b"<h1>Platform Engineer</h1><p>Acme</p>",
    )

    class FakePostingResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": indeed_url,
                    "raw_text": "Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                    "confidence": 0.91,
                    "evidence": "Imported from public Indeed posting after official posting search found no match",
                    "source_archive": {
                        "source_url": archive.source_url,
                        "final_url": archive.final_url,
                        "status_code": archive.status_code,
                        "content_type": archive.content_type,
                        "body": archive.body,
                        "headers": archive.headers,
                    },
                },
            }

    def fake_importer(request, db, storage, settings):
        imported_requests.append(request)
        return SimpleNamespace(payload={"id": "job_indeed_fallback"}, created=True)

    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=indeed_url,
            raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            link_text="View on Indeed",
            surrounding_text="Platform Engineer at Acme - Remote Canada",
        ),
        db=object(),
        storage=object(),
        settings=_settings(ats_mock_port=8001),
        job_importer=fake_importer,
        posting_resolver=FakePostingResolver(),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.linked_job_id == "job_indeed_fallback"
    assert result.resolved_url == indeed_url
    assert result.payload["source_resolution"] == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": indeed_url,
    }
    assert imported_requests[0].source_url == indeed_url
    assert imported_requests[0].source_resolution == result.payload["source_resolution"]
    assert (
        "Indeed Source URL: https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
        in imported_requests[0].raw_text
    )
    assert imported_requests[0].source_archive.source_url == indeed_url
