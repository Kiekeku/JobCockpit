import ssl
from urllib.parse import parse_qs

import httpx
import pytest

from jobcockpit.services.workflows import legacy_portal_source_url
from jobcockpit.db.models import JobPosting, StoredArtifact
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters import eluta as eluta_module
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.eluta import ElutaAdapter, _eluta_ssl_context
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    ExternalPostingHint,
    PostingResolution,
    ResolvedExternalPosting,
)
from jobcockpit.ingestion.role_interest import match_title_interest


def listing_html(*, page: int, next_page: int | None = None) -> str:
    jobs = {
        1: [
            (
                "software-developer-co-op-a1",
                "Software Developer Co-op",
                "ExampleCo",
                "Toronto ON",
                "a1",
            ),
            ("data-analyst-co-op-b2", "Data Analyst Co-op", "DataCo", "Calgary AB", "b2"),
        ],
        2: [
            (
                "platform-engineering-co-op-c3",
                "Platform Engineering Co-op",
                "PlatformCo",
                "Vancouver BC",
                "c3",
            ),
        ],
    }[page]
    cards = "\n".join(
        f"""
        <div class="organic-job" data-url="spl/{slug}?imo=12">
          <a class="lk-job-title" href="#!">{title}</a>
          <a class="employer lk-employer" href="#!">{company}</a>
          <span class="location">{location}</span>
          <a class="lk lastseen" href="#!">{page} hours ago</a>
          <span class="description">Build useful software for co-op students.</span>
        </div>
        """
        for slug, title, company, location, _direct_id in jobs
    )
    next_link = (
        f'<a href="/search?q=software+co-op&pg={next_page}">›</a>' if next_page is not None else ""
    )
    return f"""
    <html>
      <body>
        <div id="job-cards">{cards}</div>
        <nav class="pagination">{next_link}</nav>
      </body>
    </html>
    """


def listing_with_untrusted_detail_link_html() -> str:
    return """
    <html>
      <body>
        <div class="organic-job" data-url="http://169.254.169.254/latest/meta-data/">
          <a class="lk-job-title" href="#!">Software Developer Co-op</a>
          <a class="employer lk-employer" href="#!">UnsafeCo</a>
        </div>
        <div class="organic-job" data-url="spl/software-platform-co-op-safe1?imo=12">
          <a class="lk-job-title" href="#!">Software Platform Co-op</a>
          <a class="employer lk-employer" href="#!">SafeCo</a>
        </div>
      </body>
    </html>
    """


def detail_html(
    *,
    title: str = "Software Developer Co-op",
    company: str = "ExampleCo",
    location: str = "Toronto ON",
    direct_id: str = "a1",
) -> str:
    return f"""
    <html>
      <body>
        <main>
          <div class="description" itemscope itemtype="http://schema.org/JobPosting">
            <meta itemprop="datePosted" content="2026-06-03T14:00:00" />
            <h1 class="job-title" itemprop="title">
              <a href="#" onclick="enavOpenNew('/direct/i?i={direct_id}&amp;imo=12')">
                <span>{title}</span>
              </a>
            </h1>
            <h5 class="employer-name" itemprop="hiringOrganization">
              <span itemprop="name">{company}</span>
            </h5>
            <div class="summary">
              <span itemprop="jobLocation">{location}</span>
              <span>full-time</span>
            </div>
            <div class="short-text" itemprop="description">
              Build APIs, improve web platforms, and test production software.
            </div>
            <a class="apply-btn" href="#" onclick="enavOpenNew('/direct/i?i={direct_id}&amp;imo=12')">
              Apply Now
            </a>
          </div>
        </main>
      </body>
    </html>
    """


def apply_redirect_http_client(
    requests_seen: list[httpx.Request], target_url: str = "https://apply.example.com/jobs/a1"
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": target_url})
        raise AssertionError(f"Unexpected Eluta apply redirect fetch: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_eluta_apply_redirect_translates_url_policy_failures():
    class PolicyBlockedClient:
        def get(self, url: str, **_kwargs) -> httpx.Response:
            raise eluta_module.UrlPolicyViolation(url, "dns_resolution_failed")

    adapter = ElutaAdapter(
        rendered_page_loader=object(),
        apply_http_client=PolicyBlockedClient(),
    )

    with pytest.raises(AtsProviderError, match="apply redirect target was not allowed"):
        adapter.resolve_apply_url("https://www.eluta.ca/direct/i?i=a1&imo=12")


def test_eluta_uses_common_rendered_loader_without_owning_browser_state():
    class SharedRenderedLoader:
        def __init__(self) -> None:
            self.hints: list[ExternalPostingHint] = []

        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            self.hints.append(hint)
            html = listing_html(page=1) if "/search" in hint.source_url else detail_html()
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=html,
                ),
            )

        def cancel_active_pages(self) -> int:
            return 0

        def pending_cleanup_leases(self) -> tuple[object, ...]:
            return ()

        def release_pending_cleanup_leases(self, leases=()):
            return tuple(leases)

    loader = SharedRenderedLoader()
    requests_seen: list[httpx.Request] = []
    adapter = ElutaAdapter(
        rendered_page_loader=loader,
        apply_http_client=apply_redirect_http_client(requests_seen),
    )
    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software&pg=1", limit=1)
    )
    adapter.close()

    assert jobs[0].source_url == "https://apply.example.com/jobs/a1"
    assert [hint.source_kind for hint in loader.hints] == ["eluta", "eluta"]
    assert [hint.url_identity for hint in loader.hints] == ["path_and_query", "path"]
    assert [hint.identity_query_groups for hint in loader.hints] == [
        (("q",), ("pg",)),
        (),
    ]
    assert not hasattr(adapter, "_browser_context")
    assert not hasattr(adapter, "_owned_pages")


def test_eluta_search_identity_requires_only_query_keys_present_in_request():
    hints: list[ExternalPostingHint] = []

    class SharedRenderedLoader:
        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=listing_html(page=1),
                ),
            )

    adapter = ElutaAdapter(rendered_page_loader=SharedRenderedLoader())

    html = adapter._get_html_with_playwright(
        "https://www.eluta.ca/search?pg=1",
        request_kind="search",
    )

    assert html
    assert hints[0].identity_query_groups == (("pg",),)


def test_eluta_requests_cleanup_without_owning_shared_loader_leases():
    class SharedRenderedLoader:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_active_pages(self) -> int:
            self.cancel_calls += 1
            return 0

    loader = SharedRenderedLoader()
    adapter = ElutaAdapter(rendered_page_loader=loader)

    adapter.close()

    assert loader.cancel_calls == 1
    assert not hasattr(adapter, "pending_cleanup_leases")
    assert not hasattr(adapter, "release_pending_cleanup_leases")


def test_eluta_explicit_chrome_loader_uses_shared_session(monkeypatch):
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "chrome_cdp")
    adapter = ElutaAdapter()

    loader = adapter._shared_rendered_page_loader()

    assert isinstance(loader, ChromeCdpRenderedPageResolver)
    assert loader.persistent_source_session is True
    assert adapter.rendered_page_loader is loader
    assert not hasattr(adapter, "_browser_context")


def test_eluta_default_loader_uses_adapter_shared_request_interval(monkeypatch):
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "invisible_playwright")
    monkeypatch.setenv(
        "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS",
        "17.5",
    )
    adapter = ElutaAdapter()

    loader = adapter._shared_rendered_page_loader()

    assert adapter.request_min_interval_seconds == 17.5
    assert loader.min_start_interval_seconds == 0.0


def test_eluta_request_interval_is_shared_across_adapter_instances(monkeypatch):
    monkeypatch.setenv(
        "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS",
        "15",
    )
    monkeypatch.setattr(ElutaAdapter, "_next_request_at", 0.0)
    ticks = iter((100.0, 100.0))
    waits: list[float] = []
    monkeypatch.setattr(eluta_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(eluta_module.time, "sleep", waits.append)
    first = ElutaAdapter()
    second = ElutaAdapter()

    first._wait_for_request_slot()
    second._wait_for_request_slot()

    assert waits == [15.0]


def test_eluta_default_path_uses_pinned_http_without_opening_browser(monkeypatch):
    monkeypatch.setenv(
        "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS",
        "0",
    )
    requests_seen: list[httpx.Request] = []
    configured: dict[str, object] = {}

    class FakePinnedHttpClient:
        def __init__(self, **kwargs) -> None:
            configured.update(kwargs)

        def get(self, url: str, **_kwargs) -> httpx.Response:
            request = httpx.Request("GET", url)
            requests_seen.append(request)
            if request.url.path == "/search":
                return httpx.Response(200, text=listing_html(page=1), request=request)
            if request.url.path.startswith("/spl/"):
                return httpx.Response(200, text=detail_html(), request=request)
            if request.url.path == "/direct/i":
                return httpx.Response(
                    302,
                    headers={"Location": "https://apply.example.com/jobs/a1"},
                    request=request,
                )
            raise AssertionError(f"Unexpected Eluta fetch: {url}")

        def close(self) -> None:
            return None

    class BrowserMustNotOpen:
        def load_page(self, _hint: ExternalPostingHint) -> PostingResolution:
            raise AssertionError("browser fallback should not run for usable Eluta HTML")

    monkeypatch.setattr(eluta_module, "PinnedHttpClient", FakePinnedHttpClient)
    adapter = ElutaAdapter()
    adapter.rendered_page_loader = BrowserMustNotOpen()

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software&pg=1", limit=1)
    )

    assert jobs[0].source_url == "https://apply.example.com/jobs/a1"
    assert [request.url.path for request in requests_seen] == [
        "/search",
        "/spl/software-developer-co-op-a1",
        "/direct/i",
    ]
    assert configured["follow_redirects"] is False
    assert configured["ssl_context"].verify_mode == eluta_module.ssl.CERT_REQUIRED


def test_eluta_default_path_falls_back_to_browser_when_http_is_blocked(monkeypatch):
    monkeypatch.setenv(
        "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS",
        "0",
    )
    http_requests: list[str] = []

    class BlockedPinnedHttpClient:
        def __init__(self, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs) -> httpx.Response:
            http_requests.append(url)
            request = httpx.Request("GET", url)
            return httpx.Response(503, text="temporarily blocked", request=request)

        def close(self) -> None:
            return None

    class BrowserFallback:
        def __init__(self) -> None:
            self.hints: list[ExternalPostingHint] = []

        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            self.hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=listing_html(page=1),
                ),
            )

    monkeypatch.setattr(eluta_module, "PinnedHttpClient", BlockedPinnedHttpClient)
    loader = BrowserFallback()
    adapter = ElutaAdapter()
    adapter.rendered_page_loader = loader

    first_html = adapter._get_html(
        "https://www.eluta.ca/search?q=software&pg=1",
        request_kind="search",
    )
    second_html = adapter._get_html(
        "https://www.eluta.ca/search?q=software&pg=2",
        request_kind="search",
    )

    assert "organic-job" in first_html
    assert "organic-job" in second_html
    assert http_requests == ["https://www.eluta.ca/search?q=software&pg=1"]
    assert [hint.source_url for hint in loader.hints] == [
        "https://www.eluta.ca/search?q=software&pg=1",
        "https://www.eluta.ca/search?q=software&pg=2",
    ]


def test_eluta_spaces_blocked_http_attempt_before_browser_fallback(monkeypatch):
    monkeypatch.setenv(
        "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_MIN_INTERVAL_SECONDS",
        "15",
    )
    monkeypatch.setattr(ElutaAdapter, "_next_request_at", 0.0)
    ticks = iter((100.0, 100.0))
    waits: list[float] = []
    monkeypatch.setattr(eluta_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(eluta_module.time, "sleep", waits.append)

    class BlockedPinnedHttpClient:
        def __init__(self, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(503, text="temporarily blocked", request=request)

        def close(self) -> None:
            return None

    class BrowserFallback:
        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=listing_html(page=1),
                ),
            )

    monkeypatch.setattr(eluta_module, "PinnedHttpClient", BlockedPinnedHttpClient)
    adapter = ElutaAdapter()
    adapter.rendered_page_loader = BrowserFallback()

    adapter._get_html(
        "https://www.eluta.ca/search?q=software&pg=1",
        request_kind="search",
    )

    assert waits == [15.0]


@pytest.mark.parametrize("error", [
    "chrome_cdp_failed: auth_wall_unresolved",
    "playwright_failed: invisible_session_preparation_cooldown",
    "playwright_failed: invisible_session_preparation_needs_review",
    "playwright_failed: invisible_session_preparation_failed",
])
def test_eluta_stops_after_first_detail_human_verification_gate(error):
    class GatedLoader:
        def __init__(self) -> None:
            self.hints: list[ExternalPostingHint] = []

        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            self.hints.append(hint)
            if "/search" in hint.source_url:
                return PostingResolution(
                    status="resolved",
                    posting=ResolvedExternalPosting(
                        source_url=hint.source_url,
                        raw_text="rendered",
                        rendered_html=listing_html(page=1),
                    ),
                )
            if len(self.hints) > 2:
                raise AssertionError("Eluta continued crawling after a human-verification gate")
            return PostingResolution(
                status="needs_review",
                error=error,
            )

    loader = GatedLoader()
    adapter = ElutaAdapter(rendered_page_loader=loader)
    request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=2,
    )

    jobs = adapter.fetch_jobs(request)

    assert jobs == []
    assert len(loader.hints) == 2
    assert [failure.reason for failure in request.stats.link_failures] == [
        "Eluta detail page required user verification"
    ]


@pytest.mark.parametrize(
    ("error", "expected_detail"),
    (
        ("chrome_cdp_timeout", "Eluta detail request timed out"),
        (
            "chrome_cdp_failed: auth_wall_unresolved",
            "Eluta detail page required user verification",
        ),
        (
            "chrome_cdp_url_policy_blocked: blocked_host",
            "Eluta detail redirect target was not allowed",
        ),
        ("chrome_cdp_failed: navigation failed", "Eluta detail request failed"),
    ),
)
def test_eluta_maps_common_loader_failures(error: str, expected_detail: str):
    class FailedLoader:
        def load_page(self, _hint: ExternalPostingHint) -> PostingResolution:
            return PostingResolution(status="needs_review", error=error)

    adapter = ElutaAdapter(rendered_page_loader=FailedLoader())

    with pytest.raises(AtsProviderError, match=expected_detail):
        adapter._get_html(
            "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12",
            request_kind="detail",
        )


def test_eluta_rejects_cross_origin_result_from_common_loader():
    class RedirectedLoader:
        def load_page(self, _hint: ExternalPostingHint) -> PostingResolution:
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url="https://example.com/job",
                    raw_text="rendered",
                    rendered_html=detail_html(),
                ),
            )

    adapter = ElutaAdapter(rendered_page_loader=RedirectedLoader())

    with pytest.raises(
        AtsProviderError,
        match="Eluta detail redirect target was not allowed",
    ):
        adapter._get_html(
            "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12",
            request_kind="detail",
        )


def test_eluta_rejects_invalid_common_cdp_configuration(monkeypatch):
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "chrome_cdp")
    adapter = ElutaAdapter(chrome_cdp_endpoint_url="http://example.com:9222")

    with pytest.raises(AtsProviderError, match="endpoint must be localhost"):
        adapter._shared_rendered_page_loader()


def test_eluta_adapter_pages_search_results_and_fetches_detail_pages():
    requests_seen: list[httpx.Request] = []
    detail_by_slug = {
        "/spl/software-developer-co-op-a1": (
            "Software Developer Co-op",
            "ExampleCo",
            "Toronto ON",
            "a1",
        ),
        "/spl/data-analyst-co-op-b2": ("Data Analyst Co-op", "DataCo", "Calgary AB", "b2"),
        "/spl/platform-engineering-co-op-c3": (
            "Platform Engineering Co-op",
            "PlatformCo",
            "Vancouver BC",
            "c3",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/search":
            assert query["q"] == ["software co-op"]
            page = int(query.get("pg", ["1"])[0])
            return httpx.Response(
                200, text=listing_html(page=page, next_page=2 if page == 1 else None)
            )
        if request.url.path in detail_by_slug:
            title, company, location, direct_id = detail_by_slug[request.url.path]
            return httpx.Response(
                200,
                text=detail_html(
                    title=title, company=company, location=location, direct_id=direct_id
                ),
            )
        if request.url.path == "/direct/i":
            direct_id = query["i"][0]
            return httpx.Response(
                302, headers={"Location": f"https://apply.example.com/jobs/{direct_id}"}
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://www.eluta.ca/search?q=ignored&pg=1",
            limit=3,
            search_text="software co-op",
        )
    )

    assert [job.source_url for job in jobs] == [
        "https://apply.example.com/jobs/a1",
        "https://apply.example.com/jobs/b2",
        "https://apply.example.com/jobs/c3",
    ]
    assert "Software Developer Co-op" in jobs[0].raw_text
    assert "Company: ExampleCo" in jobs[0].raw_text
    assert "Location: Toronto ON" in jobs[0].raw_text
    assert "Posted: 2026-06-03T14:00:00" in jobs[0].raw_text
    assert (
        "Eluta URL: https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12" in jobs[0].raw_text
    )
    assert "Direct URL: https://www.eluta.ca/direct/i?i=a1&imo=12" in jobs[0].raw_text
    assert "Apply URL: https://apply.example.com/jobs/a1" in jobs[0].raw_text
    assert "Build APIs, improve web platforms" in jobs[0].raw_text
    assert [request.url.path for request in requests_seen] == [
        "/search",
        "/spl/software-developer-co-op-a1",
        "/direct/i",
        "/spl/data-analyst-co-op-b2",
        "/direct/i",
        "/search",
        "/spl/platform-engineering-co-op-c3",
        "/direct/i",
    ]


def test_eluta_adapter_applies_shared_interest_filter_before_detail_fetch():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_html(page=1))
        if request.url.path == "/spl/software-developer-co-op-a1":
            raise AssertionError("interest-filtered Eluta detail page should not be fetched")
        if request.url.path == "/spl/data-analyst-co-op-b2":
            return httpx.Response(
                200,
                text=detail_html(
                    title="Data Analyst Co-op",
                    company="DataCo",
                    location="Calgary AB",
                    direct_id="b2",
                ),
            )
        if request.url.path == "/direct/i":
            return httpx.Response(
                302,
                headers={"Location": f"https://apply.example.com/jobs/{query['i'][0]}"},
            )
        return httpx.Response(404)

    adapter = ElutaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    fetch_request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=1,
        listing_interest_matcher=lambda title: match_title_interest(
            title,
            {"target_role_include_keywords": ["data"]},
        ),
    )

    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.source_url for job in jobs] == ["https://apply.example.com/jobs/b2"]
    assert [request.url.path for request in requests_seen] == [
        "/search",
        "/spl/data-analyst-co-op-b2",
        "/direct/i",
    ]
    assert fetch_request.stats.interest_skips[0].title == "Software Developer Co-op"


def test_eluta_adapter_skips_excluded_detail_urls_before_detail_fetch():
    requests_seen: list[httpx.Request] = []
    excluded_url = "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_html(page=1))
        if str(request.url) == excluded_url:
            raise AssertionError("excluded Eluta detail page should not be fetched")
        if request.url.path == "/spl/data-analyst-co-op-b2":
            return httpx.Response(200, text=detail_html(title="Data Analyst Co-op", direct_id="b2"))
        if request.url.path == "/direct/i":
            return httpx.Response(
                302, headers={"Location": f"https://apply.example.com/jobs/{query['i'][0]}"}
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://www.eluta.ca/search?q=software&pg=1",
            limit=1,
            excluded_source_urls=frozenset({excluded_url}),
        )
    )

    assert [job.source_url for job in jobs] == ["https://apply.example.com/jobs/b2"]
    assert [str(request.url) for request in requests_seen] == [
        "https://www.eluta.ca/search?q=software&pg=1",
        "https://www.eluta.ca/spl/data-analyst-co-op-b2?imo=12",
        "https://www.eluta.ca/direct/i?i=b2&imo=12",
    ]


def test_eluta_adapter_skips_non_eluta_listing_links():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_with_untrusted_detail_link_html())
        if request.url.path == "/spl/software-platform-co-op-safe1":
            return httpx.Response(
                200, text=detail_html(title="Software Platform Co-op", direct_id="safe1")
            )
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": "https://apply.example.com/jobs/safe1"})
        raise AssertionError(f"Unexpected Eluta adapter fetch: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://www.eluta.ca/search?q=software&pg=1",
            limit=2,
        )
    )

    assert len(jobs) == 1
    assert jobs[0].raw_text.startswith("Software Platform Co-op")
    assert [str(request.url) for request in requests_seen] == [
        "https://www.eluta.ca/search?q=software&pg=1",
        "https://www.eluta.ca/spl/software-platform-co-op-safe1?imo=12",
        "https://www.eluta.ca/direct/i?i=safe1&imo=12",
    ]


def test_eluta_adapter_follows_same_origin_search_and_detail_redirects():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/search":
            if "redirected" not in query:
                return httpx.Response(
                    302, headers={"Location": "/search?q=software&pg=1&redirected=1"}
                )
            return httpx.Response(200, text=listing_html(page=1))
        if request.url.path == "/spl/software-developer-co-op-a1":
            if "canonical" not in query:
                return httpx.Response(
                    302, headers={"Location": "/spl/software-developer-co-op-a1?imo=12&canonical=1"}
                )
            return httpx.Response(200, text=detail_html())
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": "https://apply.example.com/jobs/a1"})
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software&pg=1", limit=1)
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://apply.example.com/jobs/a1"
    assert jobs[0].portal_source_url == (
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"
    )
    assert [str(request.url) for request in requests_seen] == [
        "https://www.eluta.ca/search?q=software&pg=1",
        "https://www.eluta.ca/search?q=software&pg=1&redirected=1",
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12",
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12&canonical=1",
        "https://www.eluta.ca/direct/i?i=a1&imo=12",
    ]


def test_eluta_adapter_rejects_redirect_to_different_detail_path():
    source_url = "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == source_url:
            return httpx.Response(302, headers={"Location": "/spl/different-job-b2?imo=12"})
        return httpx.Response(200, text=detail_html(title="Different Job"))

    adapter = ElutaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(AtsProviderError, match="redirect target was not allowed"):
        adapter._get_html(source_url, request_kind="detail")


@pytest.mark.parametrize(
    "location",
    (
        "/search?q=data&pg=1",
        "/search?q=software&pg=2",
    ),
)
def test_eluta_adapter_rejects_redirect_that_changes_search_identity(location: str):
    source_url = "https://www.eluta.ca/search?q=software&pg=1"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == source_url:
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, text=listing_html(page=1))

    adapter = ElutaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(AtsProviderError, match="redirect target was not allowed"):
        adapter._get_html(source_url, request_kind="search")


def test_eluta_adapter_stops_after_detail_page_redirects_to_user_verification():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_html(page=1))
        if request.url.path == "/spl/software-developer-co-op-a1":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://www.eluta.ca/sandbox?destination="
                        "https%3A%2F%2Fwww.eluta.ca%2Fspl%2Fsoftware-developer-co-op-a1%3Fimo%3D12"
                    )
                },
            )
        if request.url.path == "/spl/data-analyst-co-op-b2":
            return httpx.Response(200, text=detail_html(title="Data Analyst Co-op", direct_id="b2"))
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": "https://apply.example.com/jobs/b2"})
        raise AssertionError(f"Unexpected Eluta adapter fetch: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software&pg=1", limit=2)
    )

    assert jobs == []
    assert [request.url.path for request in requests_seen] == [
        "/search",
        "/spl/software-developer-co-op-a1",
    ]


def test_eluta_adapter_classifies_expired_detail_and_continues():
    requests_seen: list[httpx.Request] = []
    expired_url = "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_html(page=1))
        if str(request.url) == expired_url:
            return httpx.Response(410)
        if request.url.path == "/spl/data-analyst-co-op-b2":
            return httpx.Response(
                200,
                text=detail_html(title="Data Analyst Co-op", direct_id="b2"),
            )
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": "https://apply.example.com/jobs/b2"})
        raise AssertionError(f"Unexpected Eluta adapter fetch: {request.url}")

    adapter = ElutaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=2,
    )

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == ["https://apply.example.com/jobs/b2"]
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].source_url == expired_url
    assert request.stats.link_failures[0].category == "permanent"
    assert request.stats.link_failures[0].upstream_status_code == 410


def test_eluta_adapter_bounds_detail_attempts_during_outage():
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, text=listing_html(page=1))
        detail_requests.append(str(request.url))
        return httpx.Response(503)

    adapter = ElutaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=1,
    )

    assert adapter.fetch_jobs(request) == []
    assert request.stats.links_seen_count == 1
    assert len(request.stats.link_failures) == 1
    assert detail_requests == ["https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"]


def test_eluta_adapter_stops_when_later_search_page_requires_user_verification():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/search":
            page = int(query.get("pg", ["1"])[0])
            if page == 2:
                return httpx.Response(
                    302,
                    headers={
                        "Location": (
                            "https://www.eluta.ca/sandbox?destination="
                            "https%3A%2F%2Fwww.eluta.ca%2Fsearch%3Fq%3Dsoftware%26pg%3D2"
                        )
                    },
                )
            return httpx.Response(200, text=listing_html(page=1, next_page=2))
        if request.url.path == "/spl/software-developer-co-op-a1":
            return httpx.Response(200, text=detail_html(direct_id="a1"))
        if request.url.path == "/spl/data-analyst-co-op-b2":
            return httpx.Response(200, text=detail_html(title="Data Analyst Co-op", direct_id="b2"))
        if request.url.path == "/direct/i":
            direct_id = query["i"][0]
            return httpx.Response(
                302, headers={"Location": f"https://apply.example.com/jobs/{direct_id}"}
            )
        raise AssertionError(f"Unexpected Eluta adapter fetch: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    adapter_request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=3,
    )
    jobs = adapter.fetch_jobs(adapter_request)

    assert [job.source_url for job in jobs] == [
        "https://apply.example.com/jobs/a1",
        "https://apply.example.com/jobs/b2",
    ]
    assert [str(request.url) for request in requests_seen] == [
        "https://www.eluta.ca/search?q=software&pg=1",
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12",
        "https://www.eluta.ca/direct/i?i=a1&imo=12",
        "https://www.eluta.ca/spl/data-analyst-co-op-b2?imo=12",
        "https://www.eluta.ca/direct/i?i=b2&imo=12",
        "https://www.eluta.ca/search?q=software&pg=2",
    ]

    assert len(adapter_request.stats.link_failures) == 1
    assert adapter_request.stats.link_failures[0].source_url == (
        "https://www.eluta.ca/search?q=software&pg=2"
    )


def test_eluta_adapter_preserves_jobs_when_later_rendered_search_page_fails(monkeypatch):
    class PaginatedRenderedLoader:
        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            if hint.source_url == "https://www.eluta.ca/search?q=software&pg=1":
                return PostingResolution(
                    status="resolved",
                    posting=ResolvedExternalPosting(
                        source_url=hint.source_url,
                        raw_text="rendered",
                        rendered_html=listing_html(page=1, next_page=2),
                    ),
                )
            if hint.source_url == "https://www.eluta.ca/search?q=software&pg=2":
                return PostingResolution(
                    status="needs_review",
                    error="chrome_cdp_failed: browser disconnected",
                )
            direct_id = "a1" if "software-developer" in hint.source_url else "b2"
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=detail_html(direct_id=direct_id),
                ),
            )

    logged_warnings: list[tuple] = []
    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.eluta.logger.warning",
        lambda *args: logged_warnings.append(args),
    )
    adapter_request = AtsFetchRequest(
        source_url="https://www.eluta.ca/search?q=software&pg=1",
        limit=3,
    )
    adapter = ElutaAdapter(
        rendered_page_loader=PaginatedRenderedLoader(),
        apply_http_client=apply_redirect_http_client([]),
    )

    jobs = adapter.fetch_jobs(adapter_request)

    assert [job.portal_source_url for job in jobs] == [
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12",
        "https://www.eluta.ca/spl/data-analyst-co-op-b2?imo=12",
    ]
    assert len(adapter_request.stats.link_failures) == 1
    failure = adapter_request.stats.link_failures[0]
    assert failure.source_url == "https://www.eluta.ca/search?q=software&pg=2"
    assert failure.category == "technical"
    assert failure.reason == "Eluta search request failed"
    assert any(
        "chrome_cdp_failed: browser disconnected" in str(argument)
        for warning in logged_warnings
        for argument in warning
    )


def test_eluta_adapter_does_not_hide_later_search_url_policy_failure():
    class UnsafeSecondPageLoader:
        def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
            if hint.source_url == "https://www.eluta.ca/search?q=software&pg=1":
                return PostingResolution(
                    status="resolved",
                    posting=ResolvedExternalPosting(
                        source_url=hint.source_url,
                        raw_text="rendered",
                        rendered_html=listing_html(page=1, next_page=2),
                    ),
                )
            if hint.source_url == "https://www.eluta.ca/search?q=software&pg=2":
                return PostingResolution(
                    status="needs_review",
                    error="chrome_cdp_url_policy_blocked: private_network",
                )
            direct_id = "a1" if "software-developer" in hint.source_url else "b2"
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text="rendered",
                    rendered_html=detail_html(direct_id=direct_id),
                ),
            )

    adapter = ElutaAdapter(
        rendered_page_loader=UnsafeSecondPageLoader(),
        apply_http_client=apply_redirect_http_client([]),
    )

    with pytest.raises(AtsProviderError, match="redirect target was not allowed"):
        adapter.fetch_jobs(
            AtsFetchRequest(
                source_url="https://www.eluta.ca/search?q=software&pg=1",
                limit=3,
            )
        )


def test_eluta_adapter_rejects_external_search_redirect_before_fetch():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.host == "www.eluta.ca" and request.url.path == "/search":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        raise AssertionError(f"Unexpected Eluta adapter fetch: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    try:
        adapter.fetch_jobs(
            AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software&pg=1", limit=1)
        )
    except AtsProviderError as error:
        assert error.status_code == 502
        assert error.detail == "Eluta search redirect target was not allowed"
    else:
        raise AssertionError("Expected external Eluta search redirect to raise AtsProviderError")

    assert [str(request.url) for request in requests_seen] == [
        "https://www.eluta.ca/search?q=software&pg=1"
    ]


def test_eluta_adapter_imports_single_detail_url():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/spl/software-developer-co-op-a1":
            return httpx.Response(200, text=detail_html())
        if request.url.path == "/direct/i":
            return httpx.Response(302, headers={"Location": "https://apply.example.com/jobs/a1"})
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://www.eluta.ca/spl/software-developer-co-op-a1",
            limit=10,
        )
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://apply.example.com/jobs/a1"
    assert [request.url.path for request in requests_seen] == [
        "/spl/software-developer-co-op-a1",
        "/direct/i",
    ]


def test_eluta_adapter_skips_excluded_direct_detail_url_before_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("excluded Eluta direct detail URL should not be fetched")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)
    source_url = "https://www.eluta.ca/spl/software-developer-co-op-a1"

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url=source_url,
            limit=1,
            excluded_source_urls=frozenset({source_url}),
        )
    )

    assert jobs == []


def test_eluta_is_registered_as_ats_adapter():
    assert "eluta" in registered_ats_providers()
    assert isinstance(get_ats_adapter("ELUTA"), ElutaAdapter)


def test_eluta_tls_context_keeps_certificate_verification_enabled():
    context = _eluta_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_legacy_portal_source_url_reads_persisted_eluta_role_text():
    role = {
        "description_raw": (
            "Software co-op Company: ExampleCo Eluta URL: "
            "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"
        )
    }
    assert legacy_portal_source_url("ats_eluta", role) == (
        "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"
    )


def test_generic_ats_ingest_endpoint_persists_eluta_jobs(
    jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeElutaAdapter:
        provider = "eluta"
        fetch_count = 0
        close_count = 0

        def close(self) -> None:
            self.close_count += 1
            if self.close_count == 1:
                raise RuntimeError("transient cleanup failure")

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://www.eluta.ca/search?q=software&pg=1"
            assert request.limit == 2
            assert request.search_text == "software"
            if self.fetch_count:
                assert (
                    "https://www.eluta.ca/spl/software-developer-co-op-a1"
                    in request.excluded_source_urls
                )
                return []
            self.fetch_count += 1
            return [
                AtsJob(
                    source_url="https://apply.example.com/jobs/a1",
                    portal_source_url="https://www.eluta.ca/spl/software-developer-co-op-a1",
                    raw_text=(
                        "Software Developer Co-op\nCompany: ExampleCo\nLocation: Toronto ON\n"
                        "Eluta URL: https://www.eluta.ca/spl/software-developer-co-op-a1\n"
                        "Apply URL: https://apply.example.com/jobs/a1\nPython SQL"
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://www.eluta.ca/search?q=software&pg=1"

    fake_adapter = FakeElutaAdapter()

    def fake_get_ats_adapter(provider: str):
        assert provider == "eluta"
        return fake_adapter

    allow_public_dns({"www.eluta.ca", "apply.example.com"})
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)
    cleanup_handoffs: list[object] = []
    monkeypatch.setattr(
        "jobcockpit.services.workflows.retain_pending_browser_cleanup",
        lambda adapter: cleanup_handoffs.append(adapter) or 0,
    )

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "eluta",
            "source_url": "https://www.eluta.ca/search?q=software&pg=1",
            "search_text": "software",
            "limit": 2,
        },
    )

    assert response.status_code == 201
    assert fake_adapter.close_count == 2
    payload = response.json()
    assert payload["provider"] == "eluta"
    assert payload["jobs_created_count"] == 1
    assert payload["jobs"][0]["source"]["source_type"] == "ats_eluta"
    assert payload["jobs"][0]["source"]["source_url"] == "https://apply.example.com/jobs/a1"

    assert payload["jobs"][0]["source"]["portal_source_url"] == (
        "https://www.eluta.ca/spl/software-developer-co-op-a1"
    )

    second_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "eluta",
            "source_url": "https://www.eluta.ca/search?q=software&pg=1",
            "search_text": "software",
            "limit": 2,
        },
    )
    assert second_response.status_code == 201
    assert second_response.json()["jobs_created_count"] == 0
    assert cleanup_handoffs == [fake_adapter, fake_adapter]
    with session_scope(settings) as db:
        job = db.query(JobPosting).one()
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert job.source_type == "ats_eluta"
        assert artifact is not None
        assert artifact.artifact_type == "job_snapshot"


def test_eluta_adapter_rejects_unsupported_hosts():
    adapter = ElutaAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    try:
        adapter.validate_source_url("https://example.com/search?q=software")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Only eluta.ca search/detail URLs are supported"
    else:
        raise AssertionError("Expected unsupported Eluta host to raise AtsProviderError")


def test_eluta_adapter_reports_missing_search_cards_as_provider_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>No jobs here</body></html>")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ElutaAdapter(http_client=http_client)

    try:
        adapter.fetch_jobs(AtsFetchRequest(source_url="https://www.eluta.ca/search?q=software"))
    except AtsProviderError as error:
        assert error.status_code == 502
        assert error.detail == "Eluta search page did not include job results"
    else:
        raise AssertionError("Expected missing Eluta cards to raise AtsProviderError")
