import threading
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace

import httpx
import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver
from jobcockpit.ingestion import posting_discovery as posting_discovery_module
from jobcockpit.ingestion.browser_cleanup import BrowserCleanupResult, BrowserCleanupState
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text
from jobcockpit.ingestion.posting_links import source_resolution_metadata_for_resolution

from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    CodexCliRenderedPageResolver,
    ExternalAggregatorFallbackUrlPolicy,
    ExternalPostingHint,
    ExternalPostingUrlPolicy,
    PlaywrightProfileRenderedPageResolver,
    PostingDiscoveryResolver,
    PostingResolution,
    PostingResolutionAttempt,
    ResolvedExternalPosting,
    SearchResult,
    TracedRenderedPageResolver,
    build_external_posting_hint,
    external_aggregator_kind,
    external_job_board_terminal_status,
    external_source_kind,
    normalize_posting_resolution,
    resolved_external_posting_terminal_status,
)
from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy


@pytest.mark.parametrize(
    "page_text",
    [
        "Job not found\nThis job may have been removed or the job ID is incorrect.",
        "This job has been removed or the job ID is invalid.",
    ],
)
def test_linkedin_adapter_classifies_removed_or_invalid_job_page(page_text: str):
    signal = external_job_board_terminal_status(
        "https://www.linkedin.com/jobs/view/9999999977",
        page_text,
    )

    assert signal is not None
    assert signal.status == "closed"
    assert signal.evidence_code == "linkedin_job_removed_or_invalid"


def test_linkedin_adapter_does_not_apply_removed_marker_to_other_hosts():
    signal = external_job_board_terminal_status(
        "https://careers.example.com/jobs/9999999977",
        "This job may have been removed or the job ID is incorrect.",
    )

    assert signal is None


def test_model_output_cannot_supply_linkedin_terminal_status():
    posting = ResolvedExternalPosting(
        source_url="https://www.linkedin.com/jobs/view/9999999977",
        raw_text=("Job not found\nThis job may have been removed or the job ID is incorrect."),
        confidence=1.0,
        evidence_origin="model_output",
    )

    assert resolved_external_posting_terminal_status(posting) is None


class FakePlaywrightContextManager:
    def __init__(self, fake_playwright):
        self.fake_playwright = fake_playwright

    def __enter__(self):
        return self.fake_playwright

    def __exit__(self, exc_type, exc, traceback):
        return False


def _install_inline_cleanup_supervisor(monkeypatch):
    requested = []

    def request_cleanup(leases):
        queued = tuple(leases)
        requested.extend(queued)
        for lease in queued:
            lease.close(timeout_seconds=0.5)
        return len(queued)

    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        lambda _lease: "test-cleanup-lease",
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "request_browser_cleanup",
        request_cleanup,
    )
    monkeypatch.setattr(posting_discovery_module, "confirm_browser_cleanup", lambda _lease: True)
    return requested


class FakePlaywright:
    def __init__(self, page, *, existing_cookies=None, existing_page=None):
        self.chromium = FakeChromium(
            page,
            existing_cookies=existing_cookies,
            existing_page=existing_page,
        )


class FakeChromium:
    def __init__(self, page, *, existing_cookies=None, existing_page=None):
        self.page = page
        self.existing_cookies = list(existing_cookies or [])
        self.existing_page = existing_page
        self.launch_kwargs = None
        self.cdp_endpoint_url = None
        self.cdp_kwargs = None
        self.persistent_context = None
        self.cdp_browser = None

    def launch_persistent_context(self, **kwargs):
        self.launch_kwargs = kwargs
        self.persistent_context = FakeBrowserContext(self.page)
        return self.persistent_context

    def connect_over_cdp(self, endpoint_url, **kwargs):
        self.cdp_endpoint_url = endpoint_url
        self.cdp_kwargs = kwargs
        self.cdp_browser = FakeCdpBrowser(
            self.page,
            existing_cookies=self.existing_cookies,
            existing_page=self.existing_page,
        )
        return self.cdp_browser


class FakeCdpBrowser:
    def __init__(self, page, *, existing_cookies=None, existing_page=None):
        self.page = page
        source_context = FakeBrowserContext(cookies=list(existing_cookies or []))
        if existing_page is not None:
            source_context.pages.append(existing_page)
            existing_page._context = source_context
        self.contexts = [source_context]
        self.new_context_kwargs = []
        self.validation_contexts = []

    def new_context(self, **kwargs):
        self.new_context_kwargs.append(kwargs)
        context = FakeBrowserContext(
            self.page,
            close_failures=getattr(self.page, "context_close_failures", 0),
        )
        self.validation_contexts.append(context)
        return context


class FakeBrowserContext:
    def __init__(self, page=None, *, cookies=None, close_failures=0):
        self.page = page
        self.pages = []
        self.closed = False
        self.close_failures = close_failures
        self.close_attempts = 0
        self.route_handlers = []
        self.web_socket_route_handlers = []
        self.page_handlers = []
        self.event_handlers = {}
        self.cookies_to_return = list(cookies or [])
        self.cookie_requests = []
        self.added_cookies = []
        self.route_installed_before_new_page = False
        self.init_scripts = []
        self.hardening_installed_before_new_page = False

    def new_page(self):
        self.route_installed_before_new_page = bool(
            self.route_handlers and self.web_socket_route_handlers
        )
        self.hardening_installed_before_new_page = bool(self.init_scripts)
        if self.page is None:
            raise AssertionError("no validation page configured")
        if getattr(self.page, "new_page_error", None):
            raise RuntimeError(self.page.new_page_error)
        self.page._context = self
        if self.page not in self.pages:
            self.pages.append(self.page)
        for handler in list(self.page_handlers):
            handler(self.page)
        return self.page

    def new_cdp_session(self, page):
        context = self

        class Session:
            def send(self, command, params=None):
                if command == "Target.getTargetInfo":
                    return {"targetInfo": {"targetId": f"fake-target-{id(page)}"}}
                if command == "Target.closeTarget":
                    assert params == {"targetId": f"fake-target-{id(page)}"}
                    page.closed = True
                    return {"success": True}
                raise AssertionError(command)

            def detach(self):
                return None

        assert page in context.pages
        return Session()

    def close(self):
        self.close_attempts += 1
        if self.close_attempts <= self.close_failures:
            raise RuntimeError("context close failed")
        for page in list(self.pages):
            page.close()
        self.closed = True

    def on(self, event, handler):
        if event == "page":
            self.page_handlers.append(handler)
        else:
            self.event_handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if event == "page" and handler in self.page_handlers:
            self.page_handlers.remove(handler)
        elif handler in self.event_handlers.get(event, []):
            self.event_handlers[event].remove(handler)

    def route(self, _pattern, handler):
        self.route_handlers.append(handler)

    def unroute(self, _pattern, handler):
        if handler in self.route_handlers:
            self.route_handlers.remove(handler)

    def route_web_socket(self, _pattern, handler):
        self.web_socket_route_handlers.append(handler)

    def dispatch_request(self, url, page=None, *, navigation=True):
        route = FakeRoute()
        frame = SimpleNamespace(page=page, parent_frame=None)
        request = SimpleNamespace(
            url=url,
            frame=frame,
            method="GET",
            post_data_buffer=None,
            all_headers=lambda: {"sec-fetch-dest": "document" if navigation else "script"},
            resource_type="document" if navigation else "script",
            is_navigation_request=lambda: navigation,
        )
        for handler in reversed(self.route_handlers):
            handler(route, request)
            if route.aborted or route.continued:
                break
        if navigation and route.continued and not route.aborted:
            for handler in self.event_handlers.get("response", []):
                handler(SimpleNamespace(request=request, status=200))
        return route

    def dispatch_web_socket(self, url):
        route = FakeWebSocketRoute(url)
        for handler in reversed(self.web_socket_route_handlers):
            handler(route)
            if route.closed or route.connected:
                break
        return route

    def cookies(self, urls=None):
        self.cookie_requests.append(urls)
        return list(self.cookies_to_return)

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def add_init_script(self, *, script):
        self.init_scripts.append(script)


class FakePage:
    def __init__(self, url, title, body_text):
        self.url = url
        self._title = title
        self._body_text = body_text
        self.goto_calls = []
        self.closed = False
        self.popup_handlers = []
        self.route_handlers = []
        self.web_socket_route_handlers = []
        self.body_evaluate_limits = []
        self.body_evaluate_expressions = []
        self.title_evaluate_limits = []
        self.context_close_failures = 0
        self._context = None
        self.hardening_verified = False

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def title(self):
        return self._title

    def locator(self, selector):
        assert selector == "body"
        return SimpleNamespace(
            evaluate=self._evaluate_body,
            inner_text=lambda timeout=None: self._body_text,
        )

    def evaluate(self, _expression, limit=None):
        if limit is None:
            self.hardening_verified = True
            return bool(self._context and self._context.init_scripts)
        self.title_evaluate_limits.append(limit)
        return {
            "text": self._title[:limit],
            "truncated": len(self._title) > limit,
        }

    def _evaluate_body(self, expression, options):
        limit = options["charLimit"] if isinstance(options, dict) else options
        self.body_evaluate_limits.append(limit)
        self.body_evaluate_expressions.append(expression)
        truncated = len(self._body_text) > limit
        text = self._body_text[:limit]
        if truncated:
            text = text.rsplit("\n", 1)[0] if "\n" in text else ""
        return {
            "text": text,
            "truncated": truncated,
        }

    def close(self, **_kwargs):
        self.closed = True

    def on(self, event, handler):
        if event == "popup":
            self.popup_handlers.append(handler)

    def remove_listener(self, event, handler):
        if event == "popup" and handler in self.popup_handlers:
            self.popup_handlers.remove(handler)

    def route(self, _pattern, handler):
        self.route_handlers.append(handler)

    def unroute(self, _pattern, handler):
        if handler in self.route_handlers:
            self.route_handlers.remove(handler)

    def route_web_socket(self, _pattern, handler):
        self.web_socket_route_handlers.append(handler)

    def dispatch_request(self, url, *, navigation=True):
        if self._context is None:
            return FakeRoute()
        return self._context.dispatch_request(url, self, navigation=navigation)

    def dispatch_web_socket(self, url):
        if self._context is None:
            return FakeWebSocketRoute(url)
        return self._context.dispatch_web_socket(url)


class FakeRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False

    def abort(self, *_args, **_kwargs):
        self.aborted = True

    def continue_(self):
        self.continued = True

    def fulfill(self, **_kwargs):
        self.continued = True


class FakeWebSocketRoute:
    def __init__(self, url):
        self.url = url
        self.closed = False
        self.connected = False

    def close(self, **_kwargs):
        self.closed = True

    def connect_to_server(self):
        self.connected = True


class AuthWallThenVisibleFakePage(FakePage):
    def __init__(self, url, title, body_text, visible_url, visible_title, visible_body_text):
        super().__init__(url, title, body_text)
        self.visible_url = visible_url
        self.visible_title = visible_title
        self.visible_body_text = visible_body_text
        self.wait_for_timeout_calls = []

    def wait_for_timeout(self, timeout_ms):
        self.wait_for_timeout_calls.append(timeout_ms)
        self.url = self.visible_url
        self._title = self.visible_title
        self._body_text = self.visible_body_text


def test_external_posting_hint_reads_mapleradar_company_only_digest_row():
    hint = build_external_posting_hint(
        source_url="https://www.linkedin.com/jobs/view/4430485849",
        link_text="Security Engineer Intern, Application Security - Fall 2026",
        surrounding_text="Security Engineer Intern, Application Security - Fall 2026 · 1Password ·",
        raw_text="Security Engineer Intern, Application Security - Fall 2026",
    )

    assert hint.title == "Security Engineer Intern, Application Security - Fall 2026"
    assert hint.company == "1Password"
    assert hint.location == ""


def test_external_posting_hint_ignores_email_row_when_location_missing():
    hint = build_external_posting_hint(
        source_url="https://www.linkedin.com/jobs/view/4427321105",
        link_text="Junior Data Scientist",
        surrounding_text="Junior Data Scientist · Why Hiring ·",
        raw_text=(
            "Junior Data Scientist\n"
            "Company: Why Hiring\n"
            "Email Row: Junior Data Scientist · Why Hiring"
        ),
    )

    assert hint.title == "Junior Data Scientist"
    assert hint.company == "Why Hiring"
    assert hint.location == ""


def test_linkedin_job_url_is_external_source():
    source_url = "https://www.linkedin.com/jobs/view/424242"

    assert external_source_kind(source_url) == "linkedin"

    hint = build_external_posting_hint(
        source_url=source_url,
        link_text="Platform Engineer at Acme",
        surrounding_text="Platform Engineer at Acme - Remote Canada",
        raw_text="",
    )

    assert hint.source_kind == "linkedin"
    assert hint.company == "Acme"
    assert hint.title == "Platform Engineer"
    assert hint.source_url == source_url


def test_linkedin_nested_job_url_is_external_source():
    source_url = "https://www.linkedin.com/comm/jobs/view/424242"

    assert external_source_kind(source_url) == "linkedin"


def test_indeed_viewjob_url_is_external_source():
    source_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    assert external_source_kind(source_url) == "indeed"
    assert external_aggregator_kind(source_url) == "indeed"

    hint = build_external_posting_hint(
        source_url=source_url,
        link_text="Platform Engineer at Acme on Indeed",
        surrounding_text="",
        raw_text="Location: Remote Canada",
    )

    assert hint.source_kind == "indeed"
    assert hint.company == "Acme"
    assert hint.title == "Platform Engineer"
    assert hint.source_url == source_url


def test_indeed_vjk_jobs_url_is_external_source():
    source_url = "https://ca.indeed.com/jobs?q=platform+engineer&vjk=3cab9868c348dd54"

    assert external_source_kind(source_url) == "indeed"
    assert external_aggregator_kind(source_url) == "indeed"


def test_normalize_posting_resolution_preserves_canonical_source_resolution_fields():
    resolution = normalize_posting_resolution(
        {
            "status": "resolved",
            "posting": {
                "source_url": "https://www.linkedin.com/jobs/view/4425912315",
                "raw_text": "Title: Intern, Digital Ship NLP\nCompany: Seaspan ULC",
            },
            "source_resolution": {
                "kind": "aggregator_fallback",
                "path": "aggregator_rendered_fallback",
                "official_discovery_status": "not_found",
                "reason_code": "official_posting_not_found",
                "official_discovery_error": "legacy_error_alias",
                "fallback_url": "https://www.linkedin.com/jobs/view/4425912315",
            },
        }
    )

    assert resolution.source_path == "aggregator_rendered_fallback"
    assert resolution.reason_code == "official_posting_not_found"
    assert resolution.official_discovery_status == "not_found"


def test_normalize_posting_resolution_bounds_and_sanitizes_attempt_history():
    candidate_url = "https://example.com/careers/platform-engineer"
    attempts = [
        {
            "stage": "official_candidate_render",
            "provider": "chrome_cdp",
            "status": "needs_review",
            "reason_code": "navigation failed",
            "candidate_url": candidate_url,
            "confidence": 0.72,
            "candidate_count": 1,
            "prompt": "must not persist",
            "raw_text": "must not persist",
        }
        for _ in range(40)
    ]

    resolution = normalize_posting_resolution({"status": "needs_review", "attempts": attempts})

    assert len(resolution.attempts) == 32
    assert resolution.trace_truncated is True
    assert resolution.attempts[0].to_dict() == {
        "stage": "official_candidate_render",
        "provider": "chrome_cdp",
        "status": "needs_review",
        "reason_code": "navigation_failed",
        "candidate_url": candidate_url,
        "confidence": 0.72,
        "candidate_count": 1,
    }


def test_normalize_posting_resolution_bounds_and_sanitizes_resolution_instances():
    attempts = tuple(
        PostingResolutionAttempt(
            stage="official_candidate_render",
            provider="chrome_cdp",
            status="needs_review",
            reason_code="navigation failed",
            candidate_url="javascript:alert(1)",
        )
        for _ in range(40)
    )

    resolution = normalize_posting_resolution(
        PostingResolution(status="needs_review", attempts=attempts)
    )

    assert len(resolution.attempts) == 32
    assert resolution.trace_truncated is True
    assert resolution.attempts[0].reason_code == "navigation_failed"
    assert resolution.attempts[0].candidate_url == ""


def test_attempt_history_preserves_identical_real_executions():
    attempt = PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="chrome_cdp",
        status="needs_review",
        reason_code="official_candidate_render_needs_review",
        candidate_url="https://careers.example/jobs/1",
    )

    attempts, trace_truncated = posting_discovery_module._bounded_resolution_attempts(
        PostingResolution(status="needs_review", attempts=(attempt,)),
        PostingResolution(status="needs_review", attempts=(attempt,)),
    )

    assert attempts == (attempt, attempt)
    assert trace_truncated is False


def test_posting_discovery_tries_rendered_fallback_when_search_fails_for_aggregator():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    class FakeSearchClient:
        last_error = "codex_cli_failed: model unavailable"

        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "Apply page request failed")

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=indeed_url,
                    raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                    confidence=0.91,
                    evidence="Rendered Indeed fallback",
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == indeed_url
    assert resolution.posting.evidence == "Rendered Indeed fallback"
    assert resolution.error == "codex_cli_failed"
    assert resolution.source_path == "aggregator_rendered_fallback"
    assert resolution.official_discovery_status == "failed"
    assert resolution.reason_code == "codex_cli_failed"


def test_posting_discovery_uses_rendered_fallback_for_aggregator_without_direct_fetch(monkeypatch):
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    rendered_hints = []

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FailingApplyPageResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, apply_url: str, context=None):
            raise AssertionError("Indeed fallback must not use direct apply-page fetching")

        def close(self):
            pass

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=indeed_url,
                    raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                    confidence=0.91,
                    evidence="Rendered Indeed fallback",
                ),
            )

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_discovery.AtsApplyPageResolver", FailingApplyPageResolver
    )
    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "resolved"
    assert rendered_hints[0].source_url == indeed_url
    assert resolution.posting is not None
    assert resolution.posting.evidence == "Rendered Indeed fallback"
    assert resolution.source_path == "aggregator_rendered_fallback"
    assert resolution.official_discovery_status == "not_found"
    assert resolution.reason_code == "official_posting_not_found"


def test_posting_discovery_persists_structured_fallback_when_indeed_render_is_gated():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    class EmptySearchClient:
        last_error = ""

        def search(self, query: str, limit: int):
            return []

    class GatedRenderedFallbackResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_failed: blocked_rendered_page",
            )

    resolver = PostingDiscoveryResolver(
        search_client=EmptySearchClient(),
        rendered_fallback_resolver=GatedRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
            source_text=("Title: Platform Engineer\nCompany: Acme\nLocation: Remote Canada"),
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.source_path == "aggregator_structured_fallback"
    assert resolution.official_discovery_status == "not_found"
    assert resolution.error == "official_posting_not_found"
    assert "Title: Platform Engineer" in resolution.posting.raw_text
    assert "blocked_rendered_page" not in resolution.posting.raw_text


def test_posting_discovery_uses_rendered_aggregator_to_enrich_missing_hint_before_official_search():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    official_url = "https://careers.acme.test/jobs/platform-engineer"
    rendered_hints = []
    searched_queries = []
    resolved_contexts = []

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            searched_queries.append(query)
            return [
                SearchResult(title="Platform Engineer", url=official_url, snippet="Acme careers")
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved_contexts.append(context)
            return AtsJob(
                source_url=apply_url,
                raw_text="Title: Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
            )

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=indeed_url,
                    raw_text="Platform Engineer\nCompany: Acme\nLocation: Remote Canada",
                    confidence=0.91,
                    evidence="Rendered Indeed fallback",
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeApplyResolver(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="",
            title="",
            location="",
        )
    )

    assert resolution.status == "resolved"
    assert rendered_hints[0].source_url == indeed_url
    assert searched_queries == ['"Acme" "Platform Engineer" careers job Remote Canada']
    assert resolved_contexts[0].company == ""
    assert resolved_contexts[0].role == ""
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url


def test_posting_discovery_skips_linkedin_search_results_without_fetching():
    searched_queries = []
    resolved_urls = []

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            searched_queries.append((query, limit))
            return [
                SearchResult(
                    title="Acme jobs on LinkedIn",
                    url="https://www.linkedin.com/company/acme/jobs",
                    snippet="Platform Engineer",
                ),
                SearchResult(
                    title="Platform Engineer - Acme Careers",
                    url="https://careers.acme.example/jobs/platform-engineer",
                    snippet="Acme is hiring a Platform Engineer",
                ),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved_urls.append(apply_url)
            return AtsJob(
                source_url=apply_url,
                raw_text="Platform Engineer\nCompany: Acme\nBuild internal platforms.",
                source_archive=SourceArchive(
                    source_url=apply_url,
                    final_url=apply_url,
                    status_code=200,
                    content_type="text/html",
                    body=b"<h1>Platform Engineer</h1>",
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeApplyResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/424242",
            source_kind="linkedin",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert resolved_urls == ["https://careers.acme.example/jobs/platform-engineer"]
    assert resolution.posting is not None
    assert resolution.posting.source_archive is not None
    assert resolution.posting.source_archive.body == b"<h1>Platform Engineer</h1>"


def test_posting_discovery_skips_indeed_search_results_without_fetching():
    resolved_urls = []

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return [
                SearchResult(
                    title="Platform Engineer - Acme on Indeed",
                    url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
                    snippet="Platform Engineer at Acme",
                ),
                SearchResult(
                    title="Platform Engineer - Acme Careers",
                    url="https://careers.acme.example/jobs/platform-engineer",
                    snippet="Acme is hiring a Platform Engineer",
                ),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved_urls.append(apply_url)
            return AtsJob(
                source_url=apply_url,
                raw_text="Platform Engineer\nCompany: Acme\nBuild internal platforms.",
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeApplyResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert resolved_urls == ["https://careers.acme.example/jobs/platform-engineer"]


def test_posting_discovery_falls_back_to_original_indeed_posting_when_official_not_found():
    official_urls = []
    fallback_urls = []
    fallback_archive = SourceArchive(
        source_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
        final_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
        status_code=200,
        content_type="text/html",
        body=b"<h1>Platform Engineer</h1><p>Acme</p>",
    )

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return [
                SearchResult(
                    title="Platform Engineer - Acme on Indeed",
                    url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
                    snippet="Platform Engineer at Acme",
                )
            ]

    class FakeOfficialApplyResolver:
        def resolve(self, apply_url: str, context=None):
            official_urls.append(apply_url)
            raise AssertionError(
                "Aggregator search results should not be fetched as official candidates"
            )

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            fallback_urls.append((apply_url, context.source_label if context else ""))
            return AtsJob(
                source_url=apply_url,
                raw_text="Platform Engineer\nCompany: Acme\nBuild internal platforms.",
                source_archive=fallback_archive,
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeOfficialApplyResolver(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert official_urls == []
    assert fallback_urls == [
        ("https://ca.indeed.com/viewjob?jk=3cab9868c348dd54", "Indeed Fallback")
    ]
    assert resolution.posting is not None
    assert resolution.posting.source_url == "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    assert (
        resolution.posting.evidence
        == "Imported from public Indeed posting after official posting search found no match"
    )
    assert resolution.posting.source_archive == fallback_archive


def test_posting_discovery_uses_codex_rendered_fallback_when_aggregator_fetch_fails():
    calls = []
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"source_url":"https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",'
                '"raw_text":"Platform Engineer\\nCompany: Acme\\nLocation: Remote Canada",'
                '"confidence":0.92,'
                '"evidence":"Extracted from rendered page with Playwright"}'
            ),
            stderr="",
        )

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "Apply page request failed")

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
        rendered_fallback_resolver=CodexCliRenderedPageResolver(
            command="codex",
            runner=fake_runner,
            ignore_user_config=False,
        ),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == indeed_url
    assert resolution.posting.evidence == "Extracted from rendered page with Playwright"
    args, kwargs = calls[0]
    assert args[:3] == ["codex", "-a", "never"]
    assert "--ignore-user-config" not in args
    assert "--sandbox" in args
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == 60
    assert "reuse the user's already-running Chrome process" in args[-1]
    assert "Treat this as an explicit request for Chrome" in args[-1]
    assert "Do not use standalone Playwright" in args[-1]
    assert "Do not launch any browser process" in args[-1]
    assert indeed_url in args[-1]


def test_codex_rendered_fallback_trims_linkedin_raw_text_payload():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315/"

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "source_url": linkedin_url,
                    "raw_text": (
                        "Intern, Digital Ship NLP (8 Months)\n"
                        "Company: Seaspan ULC\n"
                        "Location: North Vancouver, British Columbia, Canada\n"
                        "Job Description\n"
                        "Build NLP data pipelines for shipyard operations.\n"
                        "End Date of Internship: Friday, April 30th, 2027 (8-Months).\n"
                        "About the company Seaspan ULC 78,010 followers\n"
                        "Interested in working with us in the future? Members who share that they are interested\n"
                        "More jobs SR .NET Back End Developer Pacific Programming & Tech Inc.\n"
                    ),
                    "confidence": 0.92,
                    "evidence": "Extracted from rendered page with Playwright",
                }
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Seaspan ULC",
            title="Intern, Digital Ship NLP (8 Months)",
            location="North Vancouver, British Columbia, Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert "Build NLP data pipelines" in resolution.posting.raw_text
    assert "End Date of Internship" in resolution.posting.raw_text
    assert "About the company" not in resolution.posting.raw_text
    assert "Interested in working with us" not in resolution.posting.raw_text
    assert "More jobs" not in resolution.posting.raw_text


def test_codex_rendered_terminal_marker_requires_trusted_dom_evidence():
    linkedin_url = "https://www.linkedin.com/jobs/view/9999999977"

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "source_url": linkedin_url,
                    "raw_text": (
                        "Software Development Intern\n"
                        "Company: Example Systems\n"
                        "Location: Canada\n"
                        "Job not found\n"
                        "This job may have been removed or the job ID is incorrect."
                    ),
                    "confidence": 0.99,
                    "evidence": "Reported from browser inspection",
                }
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Example Systems",
            title="Software Development Intern",
            location="Canada",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_terminal_evidence_untrusted"
    assert resolution.posting is None
    assert resolution.candidates
    assert resolution.candidates[0].evidence_origin == "model_output"


def test_codex_rendered_fallback_rejects_unrelated_returned_url():
    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"source_url":"https://careers.evil.example/jobs/1",'
                '"raw_text":"Platform Engineer\\nCompany: Acme\\nLocation: Remote Canada",'
                '"confidence":0.95,'
                '"evidence":"Extracted from rendered page with Playwright"}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_url_mismatch"


def test_codex_rendered_fallback_enforces_url_policy_before_browser_navigation():
    def unexpected_runner(args, **kwargs):
        raise AssertionError("A policy-blocked URL must not reach browser control")

    resolver = CodexCliRenderedPageResolver(command="codex", runner=unexpected_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="http://127.0.0.1:9999/private-job",
            source_kind="direct",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_browser_url_policy_blocked: localhost_not_allowlisted"


def test_codex_rendered_fallback_rejects_cross_host_direct_posting_url(monkeypatch):
    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"source_url":"https://careers.other.example/jobs/1",'
                '"raw_text":"Platform Engineer\\nCompany: Acme\\nLocation: Remote Canada",'
                '"confidence":0.95,'
                '"evidence":"Extracted from rendered page with Playwright"}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://careers.acme.example/jobs/platform-engineer",
            source_kind="external_job_board",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_url_mismatch"


def test_codex_rendered_fallback_rejects_model_confidence_without_local_match():
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f'{{"source_url":"{indeed_url}",'
                '"raw_text":"Sign in to continue\\nNo matching job text is visible",'
                '"confidence":0.95,'
                '"evidence":"Extracted from rendered page with Playwright"}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_auth_wall"


def test_codex_rendered_fallback_rejects_challenge_even_with_expected_identity():
    source_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "source_url": source_url,
                    "raw_text": (
                        "Are you human?\nPlease verify that you are a human to continue.\n"
                        "Platform Engineer\nCompany: Acme\nLocation: Remote Canada"
                    ),
                    "confidence": 0.95,
                    "evidence": "Challenge page",
                }
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_auth_wall"


def test_clean_aggregator_posting_text_trims_linkedin_recommendation_tail():
    raw_text = (
        "About the job\n"
        "Job Description\n"
        "Build NLP data pipelines for shipyard operations.\n"
        "Start Date of Internship: Tuesday, September 8th, 2026.\n"
        "End Date of Internship: Friday, April 30th, 2027 (8-Months).\n"
        "Set alert for similar jobs Digital Specialist, North Vancouver, British Columbia, Canada Off\n"
        "About the company Seaspan ULC 78,010 followers\n"
        "Interested in working with us in the future? Members who share that they are interested\n"
        "More jobs SR .NET Back End Developer Pacific Programming & Tech Inc.\n"
        "LinkedIn Corporation \u00a9 2026\n"
    )

    cleaned = clean_aggregator_posting_text(
        raw_text,
        source_url="https://www.linkedin.com/jobs/view/4425912315/",
    )

    assert "Build NLP data pipelines" in cleaned
    assert "Start Date of Internship" in cleaned
    assert "End Date of Internship" in cleaned
    assert "Set alert for similar jobs" not in cleaned
    assert "About the company" not in cleaned
    assert "More jobs" not in cleaned
    assert "LinkedIn Corporation" not in cleaned


def test_chrome_cdp_persistent_source_session_keeps_proxy_and_hardening():
    source_url = "https://20.12.34.56/jobs/software-engineer"
    page = FakePage(source_url, "Software Engineer", "Build reliable software.")
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        persistent_source_session=True,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.load_page(
        ExternalPostingHint(source_url=source_url, source_kind="generic")
    )

    assert resolution.status == "resolved"
    browser = fake_playwright.chromium.cdp_browser
    assert browser.new_context_kwargs[0]["proxy"]["server"].startswith("http://")
    validation_context = browser.validation_contexts[0]
    assert validation_context.init_scripts
    assert page.hardening_verified is True
    assert validation_context.closed is True


def test_chrome_cdp_persistent_source_session_coordinator_is_shared_by_endpoint_and_origin():
    source_url = "https://www.eluta.ca/search?q=software"
    first = ChromeCdpRenderedPageResolver(persistent_source_session=True)
    second = ChromeCdpRenderedPageResolver(persistent_source_session=True)

    assert first._persistent_session_for(source_url) is second._persistent_session_for(source_url)


def test_persistent_source_session_reloads_cookies_and_local_storage_after_restart(tmp_path):
    source_url = "https://20.12.34.56/search?q=software"
    state_dir = tmp_path / "sessions"
    cookie = {
        "name": "verified",
        "value": "yes",
        "domain": "20.12.34.56",
        "path": "/",
    }
    origin = {
        "origin": "https://20.12.34.56",
        "localStorage": [{"name": "challenge", "value": "complete"}],
    }
    first = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=state_dir,
    )
    first_session = first._persistent_session_for(source_url)
    accepted_state = {"cookies": [cookie], "origins": [origin]}
    posting_discovery_module._persist_persistent_storage_state(
        first_session.state_path, accepted_state
    )

    with ChromeCdpRenderedPageResolver._persistent_sessions_lock:
        session_keys = [
            key
            for key, value in ChromeCdpRenderedPageResolver._persistent_sessions.items()
            if value is first_session
        ]
        for key in session_keys:
            ChromeCdpRenderedPageResolver._persistent_sessions.pop(key)

    restarted = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=state_dir,
    )
    restarted_session = restarted._persistent_session_for(source_url)

    assert restarted_session.storage_state == accepted_state


@pytest.mark.parametrize(
    "malformed_origin",
    ["https://www.eluta.ca:not-a-port", "https://[broken"],
)
def test_persistent_source_session_ignores_malformed_origin(tmp_path, malformed_origin):
    source_url = "https://www.eluta.ca/search?q=software"
    state_path = tmp_path / "sessions" / "source.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": malformed_origin,
                        "localStorage": [{"name": "challenge", "value": "complete"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state = posting_discovery_module._load_persistent_storage_state(state_path, source_url)

    assert state == {"cookies": [], "origins": []}


def test_auth_wall_detection_does_not_match_normal_job_description_phrases():
    text = (
        "Software Security Engineer\n"
        "Improve security verification and human verification workflows. "
        "Teach systems to verify you are human without adding friction."
    )

    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://jobs.example.com/software-security-engineer",
            text,
        )
        is False
    )
    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://www.eluta.ca/spl/human-verification-specialist-a1",
            "Human Verification Specialist\nDesign fraud-prevention workflows.",
        )
        is False
    )
    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://jobs.example.com/software-security-engineer",
            "Just a moment...\nChecking your browser before accessing the posting.",
        )
        is True
    )
    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://jobs.example.com/software-security-engineer",
            "Security Verification\nPlease complete the challenge to continue.",
        )
        is True
    )
    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://www.eluta.ca/search?q=software",
            "Are you human? | Eluta\nPlease complete the challenge to continue.",
        )
        is True
    )
    assert (
        posting_discovery_module._is_auth_wall_page(
            "https://www.eluta.ca/spl/human-verification-specialist-a1",
            "Human Verification - Eluta\nDesign fraud-prevention workflows.",
        )
        is False
    )


def test_chrome_cdp_persistent_source_session_reuses_storage_and_closes_only_owned_tabs(
    tmp_path,
):
    first_url = "https://www.eluta.ca/search?q=software&pg=1"
    second_url = "https://www.eluta.ca/spl/software-developer-co-op-a1?imo=12"
    session_cookie = {
        "name": "eluta_verified",
        "value": "yes",
        "domain": ".eluta.ca",
        "path": "/",
        "secure": True,
        "expires": -1,
    }
    search_cookie = {
        "name": "search_preferences",
        "value": "internships",
        "domain": ".eluta.ca",
        "path": "/search",
    }

    class SessionPage(FakePage):
        def __init__(self, url, title, body_text, *, establishes_session=False):
            super().__init__(url, title, body_text)
            self.establishes_session = establishes_session
            self.observed_session = False

        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.observed_session = any(
                cookie.get("name") == "eluta_verified" for cookie in self._context.cookies_to_return
            )
            if self.establishes_session:
                self._context.cookies_to_return = [session_cookie, search_cookie]

    first_page = SessionPage(
        first_url,
        "Eluta search",
        "Software internships and co-op jobs",
        establishes_session=True,
    )
    second_page = SessionPage(
        second_url,
        "Software Developer Co-op",
        "Software Developer Co-op\nExampleCo\nBuild reliable software.",
    )
    existing_page = FakePage("https://www.eluta.ca/", "Eluta", "Existing user tab")
    source_context = FakeBrowserContext()
    source_context.pages.append(existing_page)
    existing_page._context = source_context

    class Browser:
        contexts = [source_context]

        def __init__(self):
            self.pages_to_open = [first_page, second_page]
            self.new_context_kwargs = []
            self.validation_contexts = []

        def new_context(self, **kwargs):
            self.new_context_kwargs.append(kwargs)
            stored = kwargs.get("storage_state") or {}
            context = FakeBrowserContext(
                self.pages_to_open.pop(0),
                cookies=list(stored.get("cookies", [])),
            )
            self.validation_contexts.append(context)
            return context

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return browser

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(
            SimpleNamespace(chromium=Chromium())
        ),
    )

    first = resolver.load_page(ExternalPostingHint(source_url=first_url, source_kind="eluta"))
    second = resolver.load_page(ExternalPostingHint(source_url=second_url, source_kind="eluta"))

    assert first.status == "resolved"
    assert second.status == "resolved"
    assert first_page.observed_session is False
    assert second_page.observed_session is True
    assert "storage_state" not in browser.new_context_kwargs[0]
    assert browser.new_context_kwargs[1]["storage_state"]["cookies"] == [
        session_cookie,
        search_cookie,
    ]
    assert first_page.closed is True
    assert second_page.closed is True
    assert existing_page.closed is False
    assert all(context.closed for context in browser.validation_contexts)
    assert source_context.closed is False
    session = resolver._persistent_session_for(second_url)
    assert session.storage_state["cookies"] == [session_cookie, search_cookie]
    assert source_context.added_cookies == []
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_persistent_source_session_bootstraps_origin_storage_from_profile(
    tmp_path,
):
    source_url = "https://www.eluta.ca/search?q=software"
    profile_cookie = {
        "name": "eluta_verified",
        "value": "yes",
        "domain": ".eluta.ca",
        "path": "/",
    }
    profile_origin = {
        "origin": "https://www.eluta.ca",
        "localStorage": [{"name": "challenge", "value": "complete"}],
    }
    page = FakePage(source_url, "Eluta search", "Software internships and co-op jobs")
    source_context = FakeBrowserContext(cookies=[profile_cookie])
    source_context.storage_state = lambda: {
        "cookies": [profile_cookie],
        "origins": [profile_origin],
    }

    class Browser:
        contexts = [source_context]

        def __init__(self):
            self.new_context_kwargs = []
            self.validation_contexts = []

        def new_context(self, **kwargs):
            self.new_context_kwargs.append(kwargs)
            stored = kwargs.get("storage_state") or {}
            context = FakeBrowserContext(
                page,
                cookies=list(stored.get("cookies", [])),
            )
            context.storage_state = lambda: stored
            self.validation_contexts.append(context)
            return context

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return browser

    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(
            SimpleNamespace(chromium=Chromium())
        ),
    )

    resolution = resolver.load_page(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="eluta",
            url_identity="path_and_query",
        )
    )

    assert resolution.status == "resolved"
    assert browser.new_context_kwargs[0]["storage_state"] == {
        "cookies": [profile_cookie],
        "origins": [profile_origin],
    }
    assert browser.validation_contexts[0].closed is True
    assert source_context.closed is False


def test_chrome_cdp_durable_source_session_does_not_read_shared_profile(tmp_path):
    source_url = "https://www.eluta.ca/search?q=software&pg=1"
    durable_cookie = {
        "name": "eluta_verified",
        "value": "yes",
        "domain": ".eluta.ca",
        "path": "/",
    }
    durable_state = {"cookies": [durable_cookie], "origins": []}
    page = FakePage(source_url, "Eluta search", "Software internships and co-op jobs")

    class UnreadableSourceContext(FakeBrowserContext):
        def cookies(self, *_args, **_kwargs):
            raise RuntimeError("shared profile is unavailable")

        def storage_state(self, *_args, **_kwargs):
            raise RuntimeError("shared profile is unavailable")

    source_context = UnreadableSourceContext()

    class Browser:
        contexts = [source_context]

        def __init__(self):
            self.new_context_kwargs = []
            self.validation_contexts = []

        def new_context(self, **kwargs):
            self.new_context_kwargs.append(kwargs)
            context = FakeBrowserContext(page, cookies=kwargs["storage_state"]["cookies"])
            context.storage_state = lambda: kwargs["storage_state"]
            self.validation_contexts.append(context)
            return context

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return browser

    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(
            SimpleNamespace(chromium=Chromium())
        ),
    )
    session = resolver._persistent_session_for(source_url)
    posting_discovery_module._persist_persistent_storage_state(session.state_path, durable_state)

    resolution = resolver.load_page(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="eluta",
            url_identity="path_and_query",
        )
    )

    assert resolution.status == "resolved"
    assert browser.new_context_kwargs[0]["storage_state"] == durable_state
    assert source_context.cookie_requests == []
    assert source_context.closed is False
    assert browser.validation_contexts[0].closed is True


def test_chrome_cdp_persistent_source_session_reloads_durable_state_before_render(
    tmp_path,
    monkeypatch,
):
    source_url = "https://www.eluta.ca/search?q=software"
    stale_state = {
        "cookies": [
            {
                "name": "challenge",
                "value": "stale",
                "domain": ".eluta.ca",
                "path": "/",
            }
        ],
        "origins": [],
    }
    current_state = {
        "cookies": [
            {
                "name": "challenge",
                "value": "current",
                "domain": ".eluta.ca",
                "path": "/",
            }
        ],
        "origins": [],
    }
    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
    )
    session = resolver._persistent_session_for(source_url)
    session.storage_state = stale_state
    posting_discovery_module._persist_persistent_storage_state(
        session.state_path,
        current_state,
    )
    observed_states = []

    def render(_hint, *, persistent_session, require_resolved_posting):
        assert require_resolved_posting is False
        observed_states.append(persistent_session.storage_state)
        return ResolvedExternalPosting(
            source_url=source_url,
            raw_text="Current Eluta search",
            confidence=0.5,
        )

    monkeypatch.setattr(resolver, "_render", render)

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "resolved"
    assert observed_states == [current_state]


def test_persistent_storage_state_lock_rejects_a_second_writer(tmp_path):
    state_path = tmp_path / "sessions" / "source.json"

    with posting_discovery_module._persistent_storage_state_lock(state_path, timeout_seconds=1.0):
        with pytest.raises(
            posting_discovery_module._PlaywrightRenderedError,
            match="persistent_storage_state_lock_timeout",
        ):
            with posting_discovery_module._persistent_storage_state_lock(
                state_path, timeout_seconds=0.01
            ):
                raise AssertionError("the second writer unexpectedly acquired the lock")


def test_chrome_cdp_persistent_source_session_waits_for_eluta_human_gate():
    source_url = "https://www.eluta.ca/search?q=software"
    page = AuthWallThenVisibleFakePage(
        source_url,
        "Are you human? | Eluta",
        "Are you human? | Eluta\nPlease verify that you are a human to continue.",
        source_url,
        "Software co-op jobs | Eluta.ca",
        "Software internships and co-op jobs in Canada",
    )
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        auth_wall_wait_seconds=120,
        auth_wall_settle_seconds=5,
        persistent_source_session=True,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "resolved"
    assert page.wait_for_timeout_calls == [1000, 5000]
    assert page.closed is True
    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert validation_context.closed is True
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_rejects_title_only_challenge_without_persisting_state(tmp_path):
    source_url = "https://www.eluta.ca/search?q=software"
    page = FakePage(
        source_url,
        "Just a moment",
        "Software internships and co-op jobs in Canada",
    )
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        auth_wall_wait_seconds=0,
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )
    hint = ExternalPostingHint(
        source_url=source_url,
        source_kind="eluta",
        url_identity="path_and_query",
        identity_query_groups=(("q",),),
    )

    resolution = resolver.load_page(hint)

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: auth_wall_unresolved"
    assert page.closed is True
    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert validation_context.closed is True
    session = resolver._persistent_session_for(source_url)
    assert session.state_path.exists() is False
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_waits_for_human_gate_even_when_it_contains_expected_identity():
    source_url = "https://20.12.34.56/jobs/software-engineer"
    page = AuthWallThenVisibleFakePage(
        source_url,
        "Are you human?",
        (
            "Are you human?\nPlease verify that you are a human to continue.\n"
            "Software Engineer\nExampleCo"
        ),
        source_url,
        "Software Engineer | ExampleCo",
        "Software Engineer\nExampleCo\nBuild reliable software.",
    )
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        timeout_seconds=15,
        auth_wall_wait_seconds=120,
        auth_wall_settle_seconds=5,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="ExampleCo",
            title="Software Engineer",
            url_identity="path",
        )
    )

    assert resolution.status == "resolved"
    assert page.wait_for_timeout_calls == [1000, 5000]
    assert "Build reliable software." in resolution.posting.raw_text
    assert page.closed is True


def test_chrome_cdp_persistent_source_session_blocks_private_redirect_and_closes_tab():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "http://127.0.0.1:8080/admin"

    class PrivateRedirectPage(FakePage):
        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            assert self.dispatch_request(url).continued is True
            blocked_route = self.dispatch_request(private_url)
            assert blocked_route.aborted is True
            raise RuntimeError("navigation aborted")

    page = PrivateRedirectPage(source_url, "Platform Engineer", "Build reliable software.")
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        persistent_source_session=True,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.load_page(
        ExternalPostingHint(source_url=source_url, source_kind="generic")
    )

    assert resolution.status == "needs_review"
    assert "rendered_url_policy_blocked" in resolution.error
    assert page.closed is True
    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert validation_context.closed is True
    assert validation_context.route_handlers == []
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_persistent_source_session_closes_owned_tab_when_navigation_fails():
    source_url = "https://www.eluta.ca/search?q=software"
    existing_page = FakePage("https://www.eluta.ca/", "Eluta", "Existing user tab")

    class FailingPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            raise RuntimeError("navigation failed")

    page = FailingPage(source_url, "", "")
    fake_playwright = FakePlaywright(page, existing_page=existing_page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        persistent_source_session=True,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "needs_review"
    assert "navigation failed" in resolution.error
    assert page.closed is True
    assert existing_page.closed is False
    source_context = fake_playwright.chromium.cdp_browser.contexts[0]
    assert source_context.closed is False
    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert validation_context.closed is True
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_failed_navigation_does_not_replace_persistent_source_state(tmp_path):
    source_url = "https://www.eluta.ca/search?q=software"
    verified_cookie = {
        "name": "challenge",
        "value": "verified",
        "domain": ".eluta.ca",
        "path": "/",
    }
    blocked_cookie = {**verified_cookie, "value": "blocked"}

    class FailingPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self._context.cookies_to_return = [blocked_cookie]
            raise RuntimeError("navigation failed")

    page = FailingPage(source_url, "", "")
    fake_playwright = FakePlaywright(page, existing_cookies=[verified_cookie])
    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )
    session = resolver._persistent_session_for(source_url)
    original_state = {"cookies": [verified_cookie], "origins": []}
    session.storage_state = original_state
    posting_discovery_module._persist_persistent_storage_state(session.state_path, original_state)

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "needs_review"
    assert session.storage_state == original_state
    source_context = fake_playwright.chromium.cdp_browser.contexts[0]
    assert source_context.added_cookies == []


def test_chrome_cdp_rejected_identity_does_not_replace_persistent_source_state(tmp_path):
    source_url = "https://20.12.34.56/software-engineer"
    verified_cookie = {
        "name": "session",
        "value": "verified",
        "domain": "20.12.34.56",
        "path": "/",
    }
    unrelated_cookie = {**verified_cookie, "value": "unrelated"}

    class UnrelatedPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self._context.cookies_to_return = [unrelated_cookie]

    page = UnrelatedPage(source_url, "Search results", "Other jobs at Example")
    fake_playwright = FakePlaywright(page, existing_cookies=[verified_cookie])
    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )
    session = resolver._persistent_session_for(source_url)
    original_state = {"cookies": [verified_cookie], "origins": []}
    session.storage_state = original_state
    posting_discovery_module._persist_persistent_storage_state(session.state_path, original_state)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="ExpectedCo",
            title="Software Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_low_confidence"
    assert session.storage_state == original_state
    source_context = fake_playwright.chromium.cdp_browser.contexts[0]
    assert source_context.added_cookies == []


def test_chrome_cdp_persistent_source_session_persists_cookie_deletions_without_profile_mutation(
    tmp_path,
):
    source_url = "https://www.eluta.ca/search?q=software"
    stale_cookie = {
        "name": "stale_challenge",
        "value": "bad",
        "domain": ".eluta.ca",
        "path": "/",
    }
    retained_cookie = {
        "name": "verified",
        "value": "yes",
        "domain": ".eluta.ca",
        "path": "/",
    }

    class SourceContext(FakeBrowserContext):
        def __init__(self):
            super().__init__(cookies=[stale_cookie, retained_cookie])
            self.cleared_cookie_scopes = []

        def clear_cookies(self, *, name=None, domain=None, path=None):
            self.cleared_cookie_scopes.append((name, domain, path))
            self.cookies_to_return = [
                cookie
                for cookie in self.cookies_to_return
                if not (
                    cookie.get("name") == name
                    and cookie.get("domain") == domain
                    and cookie.get("path") == path
                )
            ]

        def add_cookies(self, cookies):
            super().add_cookies(cookies)
            for cookie in cookies:
                self.cookies_to_return = [
                    current
                    for current in self.cookies_to_return
                    if not (
                        current.get("name") == cookie.get("name")
                        and current.get("domain") == cookie.get("domain")
                        and current.get("path") == cookie.get("path")
                    )
                ]
                self.cookies_to_return.append(cookie)

    page = FakePage(source_url, "Eluta search", "Software internships and co-op jobs")
    source_context = SourceContext()

    class Browser:
        contexts = [source_context]

        def __init__(self):
            self.validation_contexts = []

        def new_context(self, **kwargs):
            stored = kwargs.get("storage_state") or {}
            context = FakeBrowserContext(page, cookies=list(stored.get("cookies", [])))
            context.cookies_to_return = [retained_cookie]
            self.validation_contexts.append(context)
            return context

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return browser

    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(
            SimpleNamespace(chromium=Chromium())
        ),
    )

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "resolved"
    assert source_context.cleared_cookie_scopes == []
    assert source_context.added_cookies == []
    assert source_context.cookies_to_return == [stale_cookie, retained_cookie]
    session = resolver._persistent_session_for(source_url)
    assert session.storage_state == {"cookies": [retained_cookie], "origins": []}

    persisted = json.loads(session.state_path.read_text(encoding="utf-8"))
    assert persisted == session.storage_state


def test_chrome_cdp_storage_persistence_failure_still_closes_owned_tab(monkeypatch, tmp_path):
    source_url = "https://www.eluta.ca/search?q=software"
    stale_cookie = {
        "name": "stale_challenge",
        "value": "bad",
        "domain": ".eluta.ca",
        "path": "/",
    }

    class FailingSourceContext(FakeBrowserContext):
        def clear_cookies(self, **_kwargs):
            raise RuntimeError("cookie store unavailable")

    page = FakePage(source_url, "Eluta search", "Software internships and co-op jobs")
    source_context = FailingSourceContext(cookies=[stale_cookie])

    class Browser:
        contexts = [source_context]

        def __init__(self):
            self.validation_contexts = []

        def new_context(self, **_kwargs):
            context = FakeBrowserContext(page, cookies=[])
            self.validation_contexts.append(context)
            return context

    browser = Browser()

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return browser

    def fail_persistence(*_args, **_kwargs):
        raise posting_discovery_module._PlaywrightRenderedError(
            "persistent_storage_state_write_failed"
        )

    monkeypatch.setattr(
        posting_discovery_module, "_persist_persistent_storage_state", fail_persistence
    )

    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(
            SimpleNamespace(chromium=Chromium())
        ),
    )
    session = resolver._persistent_session_for(source_url)
    original_state = {"cookies": [stale_cookie], "origins": []}
    session.storage_state = original_state

    resolution = resolver.load_page(ExternalPostingHint(source_url=source_url, source_kind="eluta"))

    assert resolution.status == "needs_review"
    assert "persistent_storage_state_write_failed" in resolution.error
    assert session.storage_state == original_state
    assert page.closed is True
    assert browser.validation_contexts[0].closed is True
    assert resolver.pending_cleanup_leases() == ()


def test_chrome_cdp_path_rejection_does_not_return_or_commit_persistent_state(tmp_path):
    source_url = "https://20.12.34.56/spl/software-engineer-a1"
    unexpected_url = "https://20.12.34.56/search?q=software"
    verified_cookie = {
        "name": "session",
        "value": "verified",
        "domain": "20.12.34.56",
        "path": "/",
    }
    rejected_cookie = {**verified_cookie, "value": "rejected"}

    class UnexpectedPathPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = unexpected_url
            self._context.cookies_to_return = [rejected_cookie]

    page = UnexpectedPathPage(unexpected_url, "Search", "Other available jobs")
    fake_playwright = FakePlaywright(page, existing_cookies=[verified_cookie])
    resolver = ChromeCdpRenderedPageResolver(
        persistent_source_session=True,
        persistent_session_state_dir=tmp_path / "sessions",
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )
    session = resolver._persistent_session_for(source_url)
    original_state = {"cookies": [verified_cookie], "origins": []}
    session.storage_state = original_state

    resolution = resolver.load_page(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            url_identity="path",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.candidates[0].source_url == unexpected_url
    assert resolution.error == "chrome_cdp_url_mismatch"
    assert session.storage_state == original_state
    assert not session.state_path.exists()
    assert page.closed is True


@pytest.mark.parametrize(
    ("rendered_url", "expected_status"),
    [
        ("https://www.eluta.ca/search?q=software&pg=1&redirected=1", "resolved"),
        ("https://www.eluta.ca/search?q=software", "needs_review"),
        ("https://www.eluta.ca/search?q=software&pg=2", "needs_review"),
        ("https://www.eluta.ca/search?q=data&pg=1", "needs_review"),
    ],
)
def test_chrome_cdp_path_and_query_identity_preserves_expected_query_values(
    rendered_url,
    expected_status,
):
    source_url = "https://www.eluta.ca/search?q=software&pg=1&utm_source=github"

    class RedirectedPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = rendered_url

    page = RedirectedPage(rendered_url, "Eluta search", "Software internships and co-op jobs")
    resolver = ChromeCdpRenderedPageResolver(
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.load_page(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="eluta",
            url_identity="path_and_query",
            identity_query_groups=(("q",), ("pg",)),
        )
    )

    assert resolution.status == expected_status
    assert page.closed is True


@pytest.mark.parametrize(
    ("source_url", "expected_groups"),
    [
        (
            "https://www.d2l.com/careers/jobs/?job_id=789&gh_jid=789&utm_source=list",
            (("job_id", "gh_jid"),),
        ),
        (
            "https://jobs.example.com/opening?jobId=123&source=LinkedIn",
            (("jobId",), ("source",)),
        ),
        (
            "https://jobs.example.com/opening?job_id=123&gh_jid=456",
            (("job_id",), ("gh_jid",)),
        ),
        (
            "https://jobs.example.com/opening?vacancy=123&utm_campaign=summer",
            (("vacancy",),),
        ),
        (
            "https://jobs.example.com/opening?ref=R123&source=LinkedIn",
            (("ref",), ("source",)),
        ),
        (
            "https://jobs.example.com/opening?utm_source=list&gclid=campaign",
            (),
        ),
    ],
)
def test_rendered_query_identity_groups_classifies_identity_and_tracking_parameters(
    source_url,
    expected_groups,
):
    assert posting_discovery_module.rendered_query_identity_groups(source_url) == expected_groups


@pytest.mark.parametrize(
    ("rendered_url", "expected"),
    [
        ("https://www.d2l.com/careers/jobs/?job_id=789", True),
        ("https://www.d2l.com/careers/jobs/?gh_jid=789", True),
        ("https://www.d2l.com/careers/jobs/?job_id=789&gh_jid=789", True),
        ("https://www.d2l.com/careers/jobs/?job_id=999", False),
        ("https://www.d2l.com/careers/jobs/?job_id=789&gh_jid=999", False),
        ("https://www.d2l.com/careers/jobs/", False),
    ],
)
def test_rendered_query_identity_alias_group_accepts_equivalent_redirect_forms(
    rendered_url,
    expected,
):
    source_url = "https://www.d2l.com/careers/jobs/?job_id=789&gh_jid=789&utm_source=github"
    hint = ExternalPostingHint(
        source_url=source_url,
        source_kind="generic",
        url_identity="path_and_query",
        identity_query_groups=(("job_id", "gh_jid"),),
    )

    assert (
        posting_discovery_module._rendered_posting_matches_hint(
            ResolvedExternalPosting(source_url=rendered_url, raw_text="job"),
            hint,
        )
        is expected
    )


def test_rendered_query_identity_preserves_unknown_identity_parameters():
    source_url = "https://jobs.example.com/opening?vacancy=123&utm_source=list"
    hint = ExternalPostingHint(
        source_url=source_url,
        source_kind="generic",
        url_identity="path_and_query",
        identity_query_groups=posting_discovery_module.rendered_query_identity_groups(source_url),
    )

    assert not posting_discovery_module._rendered_posting_matches_hint(
        ResolvedExternalPosting(
            source_url="https://jobs.example.com/opening?vacancy=456",
            raw_text="other job",
        ),
        hint,
    )


@pytest.mark.parametrize(
    (
        "rendered_url",
        "raw_text",
        "expected_identity",
        "expected_access",
        "expected_status",
        "permits_action",
    ),
    [
        (
            "https://jobs.example.com/opening?vacancy=123",
            "Position is no longer available",
            "match",
            "usable",
            "closed",
            True,
        ),
        (
            "https://jobs.example.com/opening?vacancy=456",
            "Position is no longer available",
            "mismatch",
            "usable",
            "unknown",
            False,
        ),
        (
            "https://jobs.example.com/opening",
            "Position is no longer available",
            "ambiguous",
            "usable",
            "unknown",
            False,
        ),
        (
            "https://jobs.example.com/opening?vacancy=123",
            "Are you human?\nPosition is no longer available",
            "match",
            "challenge",
            "unknown",
            False,
        ),
        (
            "https://jobs.example.com/opening?vacancy=123",
            "403 Forbidden\nPosition is no longer available",
            "match",
            "blocked",
            "unknown",
            False,
        ),
    ],
)
def test_rendered_page_observation_gates_status_on_identity_and_access(
    rendered_url,
    raw_text,
    expected_identity,
    expected_access,
    expected_status,
    permits_action,
):
    hint = ExternalPostingHint(
        source_url="https://jobs.example.com/opening?vacancy=123",
        source_kind="generic",
        url_identity="path_and_query",
        identity_query_groups=(("vacancy",),),
    )

    observation = posting_discovery_module.observe_rendered_posting(
        ResolvedExternalPosting(source_url=rendered_url, raw_text=raw_text),
        hint,
        status_classifier=lambda _text: posting_discovery_module.RenderedStatusSignal.CLOSED,
    )

    assert observation.identity.value == expected_identity
    assert observation.access.value == expected_access
    assert observation.status.value == expected_status
    assert observation.permits_status_action is permits_action


def test_rendered_page_observation_preserves_title_only_challenge_evidence():
    source_url = "https://jobs.example.com/opening?vacancy=123"
    hint = ExternalPostingHint(
        source_url=source_url,
        source_kind="generic",
        url_identity="path_and_query",
        identity_query_groups=(("vacancy",),),
    )
    posting = ResolvedExternalPosting(
        source_url=source_url,
        raw_text=(
            "Page Title: Just a moment\n"
            f"Source URL: {source_url}\n"
            "Platform Engineer\nAcme\nPosition is no longer available"
        ),
        complete_body_text=("Platform Engineer\nAcme\nPosition is no longer available"),
        rendered_text=("Just a moment\nPlatform Engineer\nAcme\nPosition is no longer available"),
    )

    observation = posting_discovery_module.observe_rendered_posting(
        posting,
        hint,
        status_classifier=lambda _text: posting_discovery_module.RenderedStatusSignal.CLOSED,
    )

    assert observation.identity.value == "match"
    assert observation.access.value == "challenge"
    assert observation.status.value == "unknown"
    assert observation.permits_status_action is False


def test_chrome_cdp_rendered_fallback_attaches_to_existing_chrome():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315/"
    page = FakePage(
        linkedin_url,
        "Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn",
        (
            "Intern, Digital Ship NLP (8 Months)\n"
            "Company: Seaspan ULC\n"
            "Location: North Vancouver, British Columbia, Canada\n"
            "Build NLP data pipelines for shipyard operations."
        ),
    )
    existing_user_page = FakePage(
        "https://www.linkedin.com/feed/",
        "LinkedIn",
        "Existing user tab",
    )
    fake_playwright = FakePlaywright(
        page,
        existing_page=existing_user_page,
        existing_cookies=[
            {
                "name": "li_at",
                "value": "applicable",
                "domain": ".linkedin.com",
                "path": "/",
                "secure": True,
                "expires": 4_102_444_800,
            },
            {
                "name": "wrong_domain",
                "value": "private",
                "domain": ".example.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "wrong_path",
                "value": "private",
                "domain": ".linkedin.com",
                "path": "/feed",
                "secure": True,
            },
            {
                "name": "expired",
                "value": "private",
                "domain": ".linkedin.com",
                "path": "/",
                "secure": True,
                "expires": 1,
            },
        ],
    )

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Seaspan ULC",
            title="Intern, Digital Ship NLP (8 Months)",
            location="North Vancouver, British Columbia, Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == linkedin_url
    assert "Chrome CDP" in resolution.posting.evidence
    assert fake_playwright.chromium.cdp_endpoint_url == "http://127.0.0.1:9222"
    assert fake_playwright.chromium.cdp_kwargs == {"timeout": 15000}
    assert page.goto_calls == [(linkedin_url, {"wait_until": "domcontentloaded", "timeout": 15000})]
    assert page.closed is True
    browser = fake_playwright.chromium.cdp_browser
    assert browser is not None
    assert len(browser.new_context_kwargs) == 1
    assert browser.new_context_kwargs[0]["service_workers"] == "block"
    proxy = browser.new_context_kwargs[0]["proxy"]
    assert proxy["server"].startswith("http://127.0.0.1:")
    assert proxy["username"] == "jobcockpit"
    assert proxy["password"]
    source_context = browser.contexts[0]
    validation_context = browser.validation_contexts[0]
    assert source_context.cookie_requests == [[linkedin_url]]
    assert source_context.closed is False
    assert source_context.pages == [existing_user_page]
    assert existing_user_page.closed is False
    assert validation_context.added_cookies == [
        next(
            cookie
            for cookie in fake_playwright.chromium.existing_cookies
            if cookie["name"] == "li_at"
        )
    ]
    assert validation_context.route_installed_before_new_page is True
    assert validation_context.hardening_installed_before_new_page is True
    assert "RTCPeerConnection" in validation_context.init_scripts[0]
    assert "webkitRTCPeerConnection" in validation_context.init_scripts[0]
    assert page.hardening_verified is True
    assert validation_context.closed is True
    assert validation_context.route_handlers == []


def test_playwright_profile_blocks_private_redirect_before_target_is_reached(tmp_path):
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "http://127.0.0.1:8080/admin"

    class PrivateRedirectPage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.private_target_reached = False
            self.redirect_route = None

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            initial_route = self.dispatch_request(url)
            assert initial_route.continued is True
            self.redirect_route = self.dispatch_request(private_url)
            if self.redirect_route.aborted:
                raise RuntimeError("navigation aborted")
            self.private_target_reached = True
            self.url = private_url

    page = PrivateRedirectPage()
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == (
        "playwright_failed: rendered_url_policy_blocked: localhost_not_allowlisted"
    )
    assert page.redirect_route is not None
    assert page.redirect_route.aborted is True
    assert page.private_target_reached is False
    assert page.route_handlers == []


def test_chrome_cdp_blocks_private_subresource_and_closes_owned_tab():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "http://169.254.169.254/latest/meta-data/"

    class PrivateSubresourcePage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.private_subresource_reached = False
            self.subresource_route = None

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            initial_route = self.dispatch_request(url)
            assert initial_route.continued is True
            self.url = url
            self.subresource_route = self.dispatch_request(private_url, navigation=False)
            self.private_subresource_reached = not self.subresource_route.aborted

    page = PrivateSubresourcePage()
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert page.subresource_route is not None
    assert page.subresource_route.aborted is True
    assert page.private_subresource_reached is False
    assert page.closed is True
    assert page.route_handlers == []


def test_chrome_cdp_blocks_popup_first_private_request_before_popup_event():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "http://127.0.0.1:8080/admin"
    popup = FakePage(private_url, "Admin", "private")

    class PopupFirstRequestPage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.popup_route = None
            self.private_target_reached = False

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            assert self.dispatch_request(url).continued is True
            self.url = url
            assert self._context is not None
            popup._context = self._context
            self._context.pages.append(popup)
            self.popup_route = self._context.dispatch_request(private_url, popup)
            self.private_target_reached = not self.popup_route.aborted

    page = PopupFirstRequestPage()
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == (
        "chrome_cdp_failed: rendered_url_policy_blocked: localhost_not_allowlisted"
    )
    assert page.popup_route is not None and page.popup_route.aborted is True
    assert page.private_target_reached is False
    assert page.closed is True
    assert popup.closed is True


def test_chrome_cdp_blocks_private_web_socket_before_connecting():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "ws://127.0.0.1:9222/devtools"

    class PrivateWebSocketPage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.web_socket_route = None

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            initial_route = self.dispatch_request(url)
            assert initial_route.continued is True
            self.url = url
            self.web_socket_route = self.dispatch_web_socket(private_url)

    page = PrivateWebSocketPage()
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert page.web_socket_route is not None
    assert page.web_socket_route.closed is True
    assert page.web_socket_route.connected is False
    assert page.closed is True


def test_playwright_profile_allows_public_subresources(tmp_path):
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    public_script_url = "https://20.12.34.57/assets/app.js"

    class PublicSubresourcePage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.subresource_route = None

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            initial_route = self.dispatch_request(url)
            assert initial_route.continued is True
            self.url = url
            self.subresource_route = self.dispatch_request(public_script_url, navigation=False)

    page = PublicSubresourcePage()
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert page.subresource_route is not None
    assert page.subresource_route.continued is True
    assert page.subresource_route.aborted is False
    assert page.route_handlers == []


def test_scheduled_browser_allows_public_cross_origin_subresource():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    unrelated_public_url = "https://20.12.34.57/assets/app.js"

    class CrossOriginSubresourcePage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.subresource_route = None
            self.subresource_reached = False

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            assert self.dispatch_request(url).continued is True
            self.url = url
            self.subresource_route = self.dispatch_request(unrelated_public_url, navigation=False)
            self.subresource_reached = not self.subresource_route.aborted

    page = CrossOriginSubresourcePage()
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        url_policy=ScheduledRenderedUrlPolicy(),
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert page.subresource_route is not None
    assert page.subresource_route.continued is True
    assert page.subresource_reached is True
    assert page.closed is True


def test_empty_render_reports_blocked_subresource_host_and_policy_reason():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    blocked_script_url = "https://10.0.0.1/assets/application.js"

    class EmptyPageWithBlockedScript(FakePage):
        def __init__(self):
            super().__init__(source_url, "", "")

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            assert self.dispatch_request(url).continued is True
            self.url = url
            blocked_route = self.dispatch_request(blocked_script_url, navigation=False)
            assert blocked_route.aborted is True

    page = EmptyPageWithBlockedScript()
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        url_policy=ScheduledRenderedUrlPolicy(),
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == (
        "chrome_cdp_failed: rendered_subresource_blocked: "
        "private_network_blocked host=10.0.0.1"
    )


def test_chrome_cdp_truncated_browser_snapshot_never_resolves_open_page():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\n" + ("x" * 25_000),
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_content_truncated"
    assert len(resolution.candidates) == 1
    assert resolution.candidates[0].content_truncated is True
    assert len(resolution.candidates[0].raw_text) <= 22_000
    assert page.body_evaluate_limits == [20_000, 20_000]
    assert page.title_evaluate_limits == [1_000, 1_000]
    assert all(
        "createTreeWalker" in expression
        and "innerText" not in expression
        and "textContent" not in expression
        for expression in page.body_evaluate_expressions
    )
    assert page.closed is True


def test_rendered_snapshot_excludes_control_values_and_closed_containers():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    expression = page.body_evaluate_expressions[0]
    assert resolution.status == "resolved"
    assert all(
        f'"{tag}"' in expression for tag in ("SELECT", "OPTION", "OPTGROUP", "DATALIST", "TEXTAREA")
    )
    assert 'node.tagName === "DIALOG" && !node.open' in expression
    assert 'node.tagName === "DETAILS" && !node.open' in expression
    assert 'node.matches(":popover-open")' in expression
    assert '"BUTTON"' not in expression


def test_chrome_cdp_does_not_resolve_empty_page_from_source_evidence():
    workday_url = "https://cat.wd5.myworkdayjobs.com/Careers/job/Laval/Role_R123"
    page = FakePage(workday_url, "", "")
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=workday_url,
            source_kind="workday",
            company="Caterpillar",
            title="Software Designer Intern",
            location="Laval, QC",
            evidence_text="Caterpillar Software Designer Intern Laval, QC",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: empty_rendered_page"
    assert page.closed is True


def test_chrome_cdp_waits_for_delayed_workday_body():
    workday_url = "https://cat.wd5.myworkdayjobs.com/Careers/job/Laval/Role_R123"

    class DelayedWorkdayPage(FakePage):
        def __init__(self):
            super().__init__(workday_url, "Careers", "")
            self.wait_for_timeout_calls = []

        def wait_for_timeout(self, timeout_ms):
            self.wait_for_timeout_calls.append(timeout_ms)
            self._body_text = "The page you are looking for doesn't exist."

    page = DelayedWorkdayPage()
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.load_page(
        ExternalPostingHint(
            source_url=workday_url,
            source_kind="workday",
            company="Caterpillar",
            title="Software Designer Intern",
            location="Laval, QC",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert "page you are looking for" in resolution.posting.complete_body_text
    assert page.wait_for_timeout_calls == [250]
    assert page.closed is True


def test_chrome_cdp_closes_popup_tabs_owned_by_the_validation():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315/"
    popup = FakePage(
        "https://www.linkedin.com/authwall",
        "LinkedIn sign in",
        "Sign in to continue",
    )

    class PopupOpeningPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            popup._context = self._context
            assert self._context is not None
            self._context.pages.append(popup)
            for handler in list(self.popup_handlers):
                handler(popup)

    page = PopupOpeningPage(
        linkedin_url,
        "Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn",
        (
            "Intern, Digital Ship NLP (8 Months)\n"
            "Company: Seaspan ULC\n"
            "Location: North Vancouver, British Columbia, Canada\n"
            "Build NLP data pipelines for shipyard operations."
        ),
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Seaspan ULC",
            title="Intern, Digital Ship NLP (8 Months)",
            location="North Vancouver, British Columbia, Canada",
        )
    )

    assert resolution.status == "resolved"
    assert page.closed is True
    assert popup.closed is True


def test_chrome_cdp_registers_the_primary_target_from_the_context_page_event(
    monkeypatch,
):
    source_url = "https://www.linkedin.com/jobs/view/4425912315/"
    page = FakePage(
        source_url,
        "Software Intern | Example | LinkedIn",
        "Software Intern\nCompany: Example\nBuild reliable services.",
    )
    capture_during_page_creation = []

    class Lease:
        target_id = "primary-event-target"
        endpoint_url = "http://127.0.0.1:9222"

        def close(self, *, timeout_seconds):
            assert timeout_seconds == 0.5
            return True

    lease = Lease()

    class EventContext(FakeBrowserContext):
        def __init__(self, owned_page):
            super().__init__(owned_page)
            self.creating_page = False

        def new_page(self):
            self.creating_page = True
            try:
                return super().new_page()
            finally:
                self.creating_page = False

    validation_context = EventContext(page)

    class Browser:
        contexts = [FakeBrowserContext(cookies=[])]

        def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            return validation_context

    class Chromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return Browser()

    fake_playwright = SimpleNamespace(chromium=Chromium())

    def capture(context, owned_page, **_kwargs):
        assert owned_page is page
        capture_during_page_creation.append(context.creating_page)
        return lease

    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        capture,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        lambda registered: "owned" if registered is lease else pytest.fail("wrong lease"),
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "confirm_browser_cleanup",
        lambda registered: registered is lease,
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="Example",
            title="Software Intern",
        )
    )

    assert resolution.status == "resolved"
    assert capture_during_page_creation == [True]


def test_chrome_cdp_supervises_each_popup_when_context_cleanup_fails(monkeypatch):
    source_url = "https://www.linkedin.com/jobs/view/4425912315/"
    popup = FakePage("https://www.linkedin.com/authwall", "Sign in", "Sign in")

    class PopupOpeningPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            popup._context = self._context
            assert self._context is not None
            self._context.pages.append(popup)
            for handler in list(self.popup_handlers):
                handler(popup)

    page = PopupOpeningPage(
        source_url,
        "Software Intern | Example | LinkedIn",
        "Software Intern\nCompany: Example\nBuild reliable services.",
    )

    class Lease:
        endpoint_url = "http://127.0.0.1:9222"

        def __init__(self, target_id, closes):
            self.target_id = target_id
            self.closes = closes

        def close(self, *, timeout_seconds):
            assert timeout_seconds == 0.5
            return self.closes

    main_lease = Lease("main-target", True)
    popup_lease = Lease("popup-target", False)
    requested = []
    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        lambda _context, owned_page, **_kwargs: popup_lease if owned_page is popup else main_lease,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        lambda _lease: "owned",
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "confirm_browser_cleanup",
        lambda _lease: True,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "request_browser_cleanup",
        lambda leases: requested.extend(leases) or len(tuple(leases)),
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "cleanup_owned_browser_context",
        lambda *_args, **_kwargs: BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED),
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="Example",
            title="Software Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: owned_context_cleanup_failed"
    assert requested == [popup_lease]
    assert resolver.pending_cleanup_leases() == (popup_lease,)


def test_chrome_cdp_does_not_resolve_rendered_403_from_source_evidence():
    workday_url = "https://cat.wd5.myworkdayjobs.com/Careers/job/Laval/Role_R123"
    page = FakePage(
        workday_url,
        "403 Forbidden",
        "Access denied. You do not have permission to access this page.",
    )
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=workday_url,
            source_kind="workday",
            company="Caterpillar",
            title="Software Designer Intern",
            location="Laval, QC",
            evidence_text="Caterpillar Software Designer Intern Laval, QC",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: blocked_rendered_page"
    assert page.closed is True


def test_chrome_cdp_closes_validation_tab_when_navigation_fails():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315/"

    class FailingNavigationPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            raise RuntimeError("navigation failed")

    page = FailingNavigationPage(linkedin_url, "", "")
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Seaspan ULC",
            title="Intern, Digital Ship NLP (8 Months)",
        )
    )

    assert resolution.status == "needs_review"
    assert "navigation failed" in resolution.error
    assert page.closed is True


def test_chrome_cdp_retries_isolated_context_close_once():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    page.context_close_failures = 1
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert resolution.status == "resolved"
    assert validation_context.close_attempts == 2
    assert validation_context.closed is True
    assert page.closed is True


def test_chrome_cdp_retries_context_after_page_fallback_cleanup():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    page.context_close_failures = 2
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert resolution.status == "resolved"
    assert validation_context.close_attempts == 3
    assert validation_context.closed is True
    assert page.closed is True


def test_chrome_cdp_permanent_context_close_failure_closes_targets_and_fails():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    private_url = "http://169.254.169.254/latest/meta-data/"

    class CleanupProbePage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.context_close_failures = 10
            self.cleanup_route = None

        def close(self, **_kwargs):
            self.cleanup_route = self.dispatch_request(private_url)
            super().close()

    page = CleanupProbePage()
    fake_playwright = FakePlaywright(page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    validation_context = fake_playwright.chromium.cdp_browser.validation_contexts[0]
    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: owned_context_cleanup_failed"
    assert validation_context.close_attempts == 3
    assert validation_context.closed is False
    assert page.closed is True
    assert page.cleanup_route is not None and page.cleanup_route.aborted is True
    assert validation_context.route_handlers == []


def test_chrome_cdp_closes_isolated_context_when_new_page_fails():
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(source_url, "", "")
    page.new_page_error = "new page failed"
    existing_page = FakePage("https://example.com/existing", "Existing", "Existing tab")
    fake_playwright = FakePlaywright(page, existing_page=existing_page)
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    browser = fake_playwright.chromium.cdp_browser
    validation_context = browser.validation_contexts[0]
    assert resolution.status == "needs_review"
    assert "new page failed" in resolution.error
    assert validation_context.route_installed_before_new_page is True
    assert validation_context.closed is True
    assert browser.contexts[0].closed is False
    assert existing_page.closed is False


def test_rendered_url_policy_rechecks_requests_and_final_source(tmp_path):
    source_url = "https://20.12.34.56/jobs/platform-engineer"

    class CountingPolicy:
        def __init__(self):
            self.urls = []

        def check(self, url):
            self.urls.append(url)
            return SimpleNamespace(allowed=True, reason="public")

    class MultipleAssetPage(FakePage):
        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            assert self.dispatch_request(url).continued is True
            assert (
                self.dispatch_request("https://20.12.34.56/a.js", navigation=False).continued
                is True
            )
            assert (
                self.dispatch_request("https://20.12.34.56/b.css", navigation=False).continued
                is True
            )
            self.url = url

    policy = CountingPolicy()
    page = MultipleAssetPage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        url_policy=policy,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert policy.urls.count(source_url) >= 3  # Initial request and final capture checks.
    assert "https://20.12.34.56/a.js" in policy.urls
    assert "https://20.12.34.56/b.css" in policy.urls


def test_rendered_url_policy_exception_aborts_request_and_surfaces(tmp_path):
    source_url = "https://20.12.34.56/jobs/platform-engineer"

    class ExplodingPolicy:
        def __init__(self):
            self.calls = 0

        def check(self, _url):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(allowed=True, reason="public")
            raise RuntimeError("dns resolver failed")

    class NavigationPage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\nBuild reliable services.",
            )
            self.navigation_route = None
            self.target_reached = False

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            self.navigation_route = self.dispatch_request(url)
            self.target_reached = not self.navigation_route.aborted

    policy = ExplodingPolicy()
    page = NavigationPage()
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        url_policy=policy,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == (
        "playwright_failed: rendered_url_policy_blocked: url_policy_check_failed"
    )
    assert page.navigation_route is not None
    assert page.navigation_route.aborted is True
    assert page.target_reached is False


def test_playwright_body_snapshot_failure_never_uses_unbounded_fallback(tmp_path):
    source_url = "https://20.12.34.56/jobs/platform-engineer"

    class BrokenBodyPage(FakePage):
        def __init__(self):
            super().__init__(
                source_url,
                "Platform Engineer | Example",
                "Platform Engineer\nCompany: Example\n" + ("x" * 100_000),
            )
            self.unbounded_fallback_called = False

        def locator(self, selector):
            assert selector == "body"

            def fail_evaluate(*_args):
                raise RuntimeError("detached body")

            def fail_if_unbounded(**_kwargs):
                self.unbounded_fallback_called = True
                raise AssertionError("unbounded fallback must not run")

            return SimpleNamespace(
                evaluate=fail_evaluate,
                inner_text=fail_if_unbounded,
            )

    page = BrokenBodyPage()
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "playwright_failed: rendered_body_snapshot_failed"
    assert page.unbounded_fallback_called is False
    assert page.closed is True


def test_chrome_cdp_rendered_fallback_auto_starts_when_endpoint_is_down():
    workday_url = "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Role_R123"
    page = FakePage(
        workday_url,
        "Software Developer Intern | Example Careers",
        (
            "Software Developer Intern\n"
            "Company: Example\n"
            "Location: Toronto, Ontario\n"
            "Build customer-facing software."
        ),
    )
    fake_playwright = FakePlaywright(page)
    connect_calls = []
    launches = []
    original_connect = fake_playwright.chromium.connect_over_cdp

    def connect_after_start(endpoint_url, **kwargs):
        connect_calls.append((endpoint_url, kwargs))
        if not launches:
            raise OSError("CDP endpoint refused the connection")
        return original_connect(endpoint_url, **kwargs)

    fake_playwright.chromium.connect_over_cdp = connect_after_start
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        auto_start=True,
        chrome_launcher=lambda: launches.append("started"),
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=workday_url,
            source_kind="workday",
            company="Example",
            title="Software Developer Intern",
            location="Toronto, Ontario",
        )
    )

    assert resolution.status == "resolved"
    assert launches == ["started"]
    assert connect_calls == [
        ("http://127.0.0.1:9222", {"timeout": 15000}),
        ("http://127.0.0.1:9222", {"timeout": 1000}),
        ("http://127.0.0.1:9222", {"timeout": 1000}),
    ]


def test_chrome_cdp_auto_start_does_not_launch_after_timeout_is_exhausted():
    class FakeTime:
        def __init__(self):
            self.now_ms = 100_000
            self.sleeps = []

        def monotonic(self):
            return self.now_ms / 1000

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now_ms += round(seconds * 1000)

    fake_time = FakeTime()
    connect_timeouts = []
    launches = []

    class FakeChromium:
        def connect_over_cdp(self, endpoint_url, **kwargs):
            assert endpoint_url == "http://127.0.0.1:9222"
            connect_timeouts.append(kwargs["timeout"])
            if len(connect_timeouts) == 1:
                fake_time.now_ms += 800
            else:
                fake_time.now_ms += kwargs["timeout"]
            raise OSError("CDP endpoint refused the connection")

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        auto_start=True,
        chrome_launcher=lambda: launches.append("started"),
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
    )

    try:
        resolver._connect_browser(SimpleNamespace(chromium=FakeChromium()), 1000)
    except OSError as error:
        assert str(error) == "CDP endpoint refused the connection"
    else:
        raise AssertionError("an unavailable endpoint should fail")

    assert connect_timeouts[0] == 1000
    assert 0 < connect_timeouts[1] <= 200
    assert fake_time.now_ms <= 101_000
    assert fake_time.sleeps == []
    assert launches == []


def test_chrome_cdp_auto_start_caps_retry_and_sleep_to_remaining_timeout():
    class FakeTime:
        def __init__(self):
            self.now_ms = 200_000
            self.sleeps = []

        def monotonic(self):
            return self.now_ms / 1000

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now_ms += round(seconds * 1000)

    fake_time = FakeTime()
    connect_timeouts = []
    launches = []

    class FakeChromium:
        def connect_over_cdp(self, endpoint_url, **kwargs):
            assert endpoint_url == "http://127.0.0.1:9222"
            connect_timeouts.append(kwargs["timeout"])
            call_number = len(connect_timeouts)
            if call_number == 1:
                fake_time.now_ms += 700
            elif call_number == 2:
                fake_time.now_ms += 100
            else:
                fake_time.now_ms += 150
            raise OSError(f"connect attempt {call_number} failed")

    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        auto_start=True,
        chrome_launcher=lambda: launches.append("started"),
        sleeper=fake_time.sleep,
        clock=fake_time.monotonic,
    )

    try:
        resolver._connect_browser(SimpleNamespace(chromium=FakeChromium()), 1000)
    except OSError as error:
        assert str(error) == "connect attempt 3 failed"
    else:
        raise AssertionError("an unavailable endpoint should fail")

    assert connect_timeouts[0] == 1000
    assert 0 < connect_timeouts[1] <= 300
    assert 0 < connect_timeouts[2] <= 200
    assert launches == ["started"]
    assert len(fake_time.sleeps) == 1
    assert 0 < fake_time.sleeps[0] <= 0.051
    assert fake_time.now_ms <= 201_000


def test_chrome_cdp_auto_start_is_serialized_across_resolvers():
    initial_connects = Barrier(2)
    state_lock = Lock()
    browser = object()
    launched = False
    launch_count = 0
    initial_call_count = 0

    class FakeChromium:
        def connect_over_cdp(self, endpoint_url, **kwargs):
            nonlocal initial_call_count
            assert endpoint_url == "http://127.0.0.1:9222"
            with state_lock:
                endpoint_started = launched
                initial_call_count += 1
                is_initial_connect = initial_call_count <= 2
            if not endpoint_started:
                if is_initial_connect:
                    initial_connects.wait(timeout=2)
                raise OSError("CDP endpoint refused the connection")
            return browser

    def launch_chrome():
        nonlocal launched, launch_count
        with state_lock:
            launched = True
            launch_count += 1

    playwright = SimpleNamespace(chromium=FakeChromium())
    resolvers = [
        ChromeCdpRenderedPageResolver(
            endpoint_url="http://127.0.0.1:9222",
            timeout_seconds=15,
            auto_start=True,
            chrome_launcher=launch_chrome,
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        connected = list(
            executor.map(
                lambda resolver: resolver._connect_browser(playwright, 15000),
                resolvers,
            )
        )

    assert connected == [browser, browser]
    assert launch_count == 1


def test_chrome_cdp_rendered_fallback_rejects_nonlocal_endpoint():
    try:
        ChromeCdpRenderedPageResolver(endpoint_url="http://example.com:9222")
    except ValueError as error:
        assert "localhost" in str(error)
    else:
        raise AssertionError("remote CDP endpoints should be rejected")


def test_chrome_cdp_rendered_fallback_rejects_zero_port_before_auto_start():
    launches: list[str] = []
    try:
        ChromeCdpRenderedPageResolver(
            endpoint_url="http://127.0.0.1:0",
            auto_start=True,
            chrome_launcher=lambda: launches.append("started"),
        )
    except ValueError as error:
        assert "nonzero port" in str(error)
    else:
        raise AssertionError("port zero must be rejected before Chrome auto-start")
    assert launches == []


def test_playwright_profile_rendered_fallback_uses_default_profile_dir(tmp_path):
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    page = FakePage(
        indeed_url,
        "Platform Engineer - Acme",
        "Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nBuild internal platforms.",
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "playwright-profile",
        headless=True,
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Remote Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert "direct Playwright" in resolution.posting.evidence
    launch_kwargs = fake_playwright.chromium.launch_kwargs
    assert launch_kwargs["user_data_dir"] == str(tmp_path / "playwright-profile")
    assert launch_kwargs["headless"] is True
    assert launch_kwargs["timeout"] == 15000
    assert launch_kwargs["service_workers"] == "block"
    assert launch_kwargs["args"] == ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    assert launch_kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
    context = fake_playwright.chromium.persistent_context
    assert context.hardening_installed_before_new_page is True
    assert page.hardening_verified is True
    assert page.goto_calls == [(indeed_url, {"wait_until": "domcontentloaded", "timeout": 15000})]


def test_rendered_context_hardening_fails_closed_when_init_script_does_not_apply(
    tmp_path,
):
    source_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"

    class UnhardenedPage(FakePage):
        def evaluate(self, expression, limit=None):
            if limit is None:
                self.hardening_verified = True
                return False
            return super().evaluate(expression, limit)

    page = UnhardenedPage(
        source_url,
        "Platform Engineer - Acme",
        "Platform Engineer\nCompany: Acme\nBuild internal platforms.",
    )
    fake_playwright = FakePlaywright(page)
    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "profile",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert "rendered_context_hardening_verification_failed" in resolution.error
    assert page.hardening_verified is True
    assert page.goto_calls == []
    assert fake_playwright.chromium.persistent_context.closed is True


def test_playwright_profile_rendered_fallback_trims_linkedin_recommendation_tail(tmp_path):
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315/"
    page = FakePage(
        linkedin_url,
        "Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn",
        (
            "Seaspan ULC\n"
            "Intern, Digital Ship NLP (8 Months)\n"
            "North Vancouver, BC\n"
            "About the job\n"
            "Job Description\n"
            "Build NLP data pipelines for shipyard operations.\n"
            "Start Date of Internship: Tuesday, September 8th, 2026.\n"
            "End Date of Internship: Friday, April 30th, 2027 (8-Months).\n"
            "About the company Seaspan ULC 78,010 followers\n"
            "Interested in working with us in the future? Members who share that they are interested\n"
            "More jobs SR .NET Back End Developer Pacific Programming & Tech Inc.\n"
            "Location: North Vancouver, British Columbia, Canada Linkedin"
        ),
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "playwright-profile",
        headless=True,
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="Seaspan ULC",
            title="Intern, Digital Ship NLP (8 Months)",
            location="North Vancouver, BC",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert "Build NLP data pipelines" in resolution.posting.raw_text
    assert "End Date of Internship" in resolution.posting.raw_text
    assert "About the company" not in resolution.posting.raw_text
    assert "Interested in working with us" not in resolution.posting.raw_text
    assert "More jobs" not in resolution.posting.raw_text


def test_playwright_profile_rendered_fallback_rejects_blocked_page_with_matching_source_evidence(
    tmp_path,
):
    indeed_url = "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926"
    page = FakePage(
        indeed_url,
        "Blocked - Indeed.com",
        "Request Blocked\nYou have been blocked.\nTroubleshooting Cloudflare Errors",
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "playwright-profile",
        headless=True,
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Birdseye Solutions",
            title="Software Development Intern",
            location="Mississauga, ON, CA",
            evidence_text="Software Development Intern - Birdseye Solutions - Mississauga, ON, CA",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "playwright_failed: blocked_rendered_page"


def test_playwright_profile_rendered_fallback_rejects_linkedin_checkpoint_challenge(tmp_path):
    checkpoint_url = "https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc"
    page = FakePage(
        checkpoint_url,
        "LinkedIn App Challenge",
        "Check your LinkedIn app\nVerify using authenticator app",
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "playwright-profile",
        headless=False,
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4425694183",
            source_kind="linkedin",
            company="TrendAI",
            title="Software Developer Co-Op - Fall 2026",
            evidence_text="TrendAI hiring Software Developer Co-Op - Fall 2026",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "playwright_failed: auth_wall_unresolved"


def test_playwright_profile_rendered_fallback_waits_for_auth_wall_completion(tmp_path):
    source_url = "https://www.linkedin.com/jobs/view/4425694183"
    page = AuthWallThenVisibleFakePage(
        source_url,
        "Sign Up | LinkedIn",
        "Sign in to continue\nEmail or phone",
        source_url,
        "TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn",
        "Software Developer Co-Op - Fall 2026\nCompany: TrendAI\nLocation: Ottawa, Ontario, Canada",
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "playwright-profile",
        headless=False,
        auth_wall_wait_seconds=180,
        auth_wall_settle_seconds=3,
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="TrendAI",
            title="Software Developer Co-Op - Fall 2026",
            location="Ottawa, Ontario, Canada",
            evidence_text="Software Developer Co-Op - Fall 2026 - TrendAI - Ottawa, Ontario, Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == source_url
    assert "Company: TrendAI" in resolution.posting.raw_text
    assert page.wait_for_timeout_calls == [1000, 3000]


def test_playwright_profile_rendered_fallback_accepts_chrome_profile_directory(tmp_path):
    indeed_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    page = FakePage(
        indeed_url,
        "Junior Software Engineer - CAE",
        "Junior Software Engineer\nCompany: CAE Inc.\nLocation: Montreal, QC",
    )
    fake_playwright = FakePlaywright(page)

    resolver = PlaywrightProfileRenderedPageResolver(
        user_data_dir=tmp_path / "Chrome" / "User Data",
        profile_directory="Default",
        channel="chrome",
        headless=False,
        timeout_seconds=20,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="CAE",
            title="Junior Software Engineer",
            location="Montreal, QC",
        )
    )

    assert resolution.status == "resolved"
    launch_kwargs = fake_playwright.chromium.launch_kwargs
    assert launch_kwargs["user_data_dir"] == str(tmp_path / "Chrome" / "User Data")
    assert launch_kwargs["headless"] is False
    assert launch_kwargs["timeout"] == 20000
    assert launch_kwargs["service_workers"] == "block"
    assert launch_kwargs["channel"] == "chrome"
    assert launch_kwargs["args"] == [
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--profile-directory=Default",
    ]
    assert launch_kwargs["proxy"]["server"].startswith("http://127.0.0.1:")


def test_codex_rendered_fallback_retries_timeout_before_returning_result():
    indeed_url = "https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3"
    calls = []

    def fake_runner(args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, timeout=kwargs["timeout"])
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f'{{"source_url":"{indeed_url}",'
                '"raw_text":"Software Developer Co-Op - Fall 2026\\n'
                "Company: Trend Micro\\n"
                'Location: Ottawa, ON",'
                '"confidence":0.95,'
                '"evidence":"Rendered Indeed page showed title and company."}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(
        command="codex", runner=fake_runner, timeout_seconds=15, retries=1
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="",
            title="Software Developer Co-Op - Fall 2026",
        )
    )

    assert resolution.status == "resolved"
    assert calls == [15, 15]


def test_codex_rendered_fallback_throttles_start_between_attempts():
    indeed_url = "https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3"
    calls = []
    sleeps = []
    current_time = 100.0

    def fake_clock():
        return current_time

    def fake_sleep(seconds):
        sleeps.append(seconds)

    def fake_runner(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, timeout=kwargs["timeout"])
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f'{{"source_url":"{indeed_url}",'
                '"raw_text":"Software Developer Co-Op - Fall 2026\\n'
                "Company: Trend Micro\\n"
                'Location: Ottawa, ON",'
                '"confidence":0.95,'
                '"evidence":"Rendered Indeed page showed title and company."}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(
        command="codex",
        runner=fake_runner,
        timeout_seconds=15,
        retries=1,
        min_start_interval_seconds=7.5,
        sleeper=fake_sleep,
        clock=fake_clock,
    )
    resolver.__class__._next_start_at = 0.0

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="",
            title="Software Developer Co-Op - Fall 2026",
        )
    )

    assert resolution.status == "resolved"
    assert len(calls) == 2
    assert sleeps == [7.5]


def test_codex_rendered_fallback_accepts_visible_aggregator_company_when_hint_company_is_missing():
    indeed_url = "https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3"

    def fake_runner(args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f'{{"source_url":"{indeed_url}",'
                '"raw_text":"Software Developer Co-Op - Fall 2026\\n'
                "Company: Trend Micro\\n"
                "Location: Ottawa, ON K2K 2M5\\n"
                'Apply URL: https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3",'
                '"confidence":0.95,'
                '"evidence":"Rendered Indeed page showed title, hiring organization, and location."}'
            ),
            stderr="",
        )

    resolver = CodexCliRenderedPageResolver(command="codex", runner=fake_runner)

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="",
            title="Software Developer Co-Op - Fall 2026",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.confidence >= 0.85


def test_posting_discovery_keeps_low_confidence_aggregator_fallback_for_review():
    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text="Sign in to continue",
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/424242",
            source_kind="linkedin",
            company="",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "aggregator_fallback_low_confidence"
    assert resolution.candidates[0].source_url == "https://www.linkedin.com/jobs/view/424242"
    assert resolution.official_search_outcome is not None
    assert resolution.official_search_outcome.status == "failed"
    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "official_search",
            "provider": "search",
            "status": "needs_review",
            "reason_code": "missing_company_or_title",
            "candidate_count": 0,
        }
    ]


def test_aggregator_fallback_failure_preserves_official_search_state():
    class EmptySearchClient:
        def search(self, query: str, limit: int):
            return []

    class FailingAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "private provider detail")

    resolution = PostingDiscoveryResolver(
        search_client=EmptySearchClient(),
        aggregator_fallback_resolver=FailingAggregatorFallbackResolver(),
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/424242",
            source_kind="linkedin",
            company="",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.official_search_outcome is not None
    assert resolution.official_search_outcome.status == "failed"
    assert resolution.official_search_outcome.reason_code == "missing_company_or_title"
    assert [attempt.stage for attempt in resolution.attempts] == ["official_search"]


@pytest.mark.parametrize("rendered_status", ["resolved", "needs_review"])
def test_post_search_render_is_appended_to_cumulative_history(rendered_status):
    source_url = "https://www.linkedin.com/jobs/view/late-render"

    class EmptySearchClient:
        def search(self, query: str, limit: int):
            return []

    class FailingAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "direct fallback failed")

    class RenderedResolver:
        def resolve(self, hint):
            if rendered_status == "resolved":
                return PostingResolution(
                    status="resolved",
                    posting=ResolvedExternalPosting(
                        source_url=source_url,
                        raw_text="",
                        confidence=0.7,
                        evidence="Rendered aggregator page",
                    ),
                )
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_low_confidence",
            )

    resolution = PostingDiscoveryResolver(
        search_client=EmptySearchClient(),
        aggregator_fallback_resolver=FailingAggregatorFallbackResolver(),
        rendered_fallback_resolver=TracedRenderedPageResolver(
            inner_resolver=RenderedResolver(),
            provider="chrome_cdp",
        ),
    ).resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="linkedin",
            company="",
            title="Platform Engineer",
        )
    )

    assert [attempt.stage for attempt in resolution.attempts] == [
        "official_search",
        "source_render",
    ]
    assert resolution.attempts[0].reason_code == "missing_company_or_title"
    assert resolution.attempts[1].status == rendered_status


def test_posting_discovery_imports_structured_hint_when_aggregator_page_is_blocked():
    indeed_url = "https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "Request Blocked")

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="codex_playwright_low_confidence | evidence=Blocked - Indeed.com",
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="CAE",
            title="Software Developer Co-Op - Fall 2026",
            location="Saint-Laurent, Quebec, Canada",
            evidence_text="Software Developer Co-Op - Fall 2026 - CAE - Saint-Laurent, QC, CA",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == indeed_url
    assert "Company: CAE" in resolution.posting.raw_text
    assert "Location: Saint-Laurent, Quebec, Canada" in resolution.posting.raw_text
    assert "public posting page could not be resolved automatically" in resolution.posting.evidence


def test_posting_discovery_does_not_import_structured_hint_without_company():
    indeed_url = "https://ca.indeed.com/viewjob?jk=234c5366e5c1e2e3"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "Request Blocked")

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            return PostingResolution(status="needs_review", error="codex_playwright_low_confidence")

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="",
            title="Software Developer Co-Op - Fall 2026",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "codex_playwright_low_confidence"


def test_posting_discovery_accepts_aggregator_fallback_with_visible_company_when_hint_company_is_missing():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425618748"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Junior Software Engineer\n"
                    "Company: Finning Canada\n"
                    "Location: Edmonton, AB, Canada"
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="",
            title="Junior Software Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.confidence >= 0.85


def test_posting_discovery_accepts_partial_company_suffix_match_for_aggregator_fallback():
    indeed_url = "https://ca.indeed.com/viewjob?jk=4090531cbd659aec"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: C-GE-102 - Software Developer Intern - GUI Framework\n"
                    "Company: CAE Inc.\n"
                    "Location: Saint-Laurent, QC"
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="CAE",
            title="C-GE-102 - Software Developer Intern",
            location="Saint-Laurent, Quebec, Canada",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.confidence >= 0.85


def test_posting_discovery_tries_rendered_fallback_when_aggregator_fetch_is_low_confidence():
    linkedin_url = "https://www.linkedin.com/jobs/view/4425618748"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return []

    class FakeAggregatorFallbackResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text="Junior Software Engineer",
            )

    class FakeRenderedFallbackResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=linkedin_url,
                    raw_text=(
                        "Junior Software Engineer\n"
                        "Company: Finning Canada\n"
                        "Location: Edmonton, AB, Canada"
                    ),
                    confidence=0.85,
                    evidence="Rendered LinkedIn page showed title and company.",
                ),
            )

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        aggregator_fallback_resolver=FakeAggregatorFallbackResolver(),
        rendered_fallback_resolver=FakeRenderedFallbackResolver(),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=linkedin_url,
            source_kind="linkedin",
            company="",
            title="Junior Software Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.evidence == "Rendered LinkedIn page showed title and company."
    assert resolution.source_path == "aggregator_rendered_fallback"
    assert resolution.official_discovery_status == "failed"
    assert resolution.reason_code == "missing_company_or_title"
    assert resolution.error == "missing_company_or_title"


def test_posting_discovery_preserves_render_trace_when_search_is_unconfigured():
    indeed_url = "https://ca.indeed.com/viewjob?jk=trace-no-search"

    class FakeRenderedResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_low_confidence",
                attempts=(
                    PostingResolutionAttempt(
                        stage="source_render",
                        provider="chrome_cdp",
                        status="needs_review",
                        reason_code="chrome_cdp_low_confidence",
                    ),
                ),
            )

    resolver = PostingDiscoveryResolver(
        rendered_fallback_resolver=FakeRenderedResolver(),
        allow_aggregator_fallback=True,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.error == "posting_search_not_configured"
    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "source_render",
            "provider": "chrome_cdp",
            "status": "needs_review",
            "reason_code": "chrome_cdp_low_confidence",
        },
        {
            "stage": "official_search",
            "provider": "search",
            "status": "needs_review",
            "reason_code": "posting_search_not_configured",
            "candidate_count": 0,
        },
    ]


@pytest.mark.parametrize(
    ("company", "title", "expected_reason"),
    [
        ("", "", "missing_company_or_title"),
        ("Acme", "Platform Engineer", "posting_search_not_configured"),
    ],
)
def test_rendered_aggregator_success_preserves_official_search_attempt(
    company,
    title,
    expected_reason,
):
    indeed_url = "https://ca.indeed.com/viewjob?jk=trace-rendered-success"

    class FakeRenderedResolver:
        def resolve(self, hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=indeed_url,
                    raw_text="",
                    confidence=0.7,
                    evidence="Rendered aggregator page",
                ),
            )

    resolution = PostingDiscoveryResolver(
        rendered_fallback_resolver=TracedRenderedPageResolver(
            inner_resolver=FakeRenderedResolver(),
            provider="chrome_cdp",
        ),
        allow_aggregator_fallback=True,
    ).resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company=company,
            title=title,
        )
    )

    assert resolution.status == "resolved"
    assert resolution.source_path == "aggregator_rendered_fallback"
    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "source_render",
            "provider": "chrome_cdp",
            "status": "resolved",
            "reason_code": "source_render_resolved",
            "confidence": 0.7,
        },
        {
            "stage": "official_search",
            "provider": "search",
            "status": "needs_review",
            "reason_code": expected_reason,
            "candidate_count": 0,
        },
    ]


def test_posting_discovery_enforces_max_results_before_fetching_candidates():
    resolved_urls = []

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return [
                SearchResult(title="First", url="https://careers.acme.example/jobs/1"),
                SearchResult(title="Second", url="https://careers.acme.example/jobs/2"),
                SearchResult(title="Third", url="https://careers.acme.example/jobs/3"),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved_urls.append(apply_url)
            raise AtsProviderError(502, "candidate fetch failed")

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeApplyResolver(),
        max_results=2,
        allow_aggregator_fallback=False,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/424242",
            source_kind="linkedin",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolved_urls == [
        "https://careers.acme.example/jobs/1",
        "https://careers.acme.example/jobs/2",
    ]
    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "official_search",
            "provider": "search",
            "status": "resolved",
            "reason_code": "official_candidates_found",
            "candidate_count": 2,
        },
        {
            "stage": "official_candidate_render",
            "provider": "ats_apply",
            "status": "needs_review",
            "reason_code": "official_candidate_render_needs_review",
            "candidate_url": "https://careers.acme.example/jobs/1",
        },
        {
            "stage": "official_candidate_render",
            "provider": "ats_apply",
            "status": "needs_review",
            "reason_code": "official_candidate_render_needs_review",
            "candidate_url": "https://careers.acme.example/jobs/2",
        },
        {
            "stage": "official_search",
            "provider": "search",
            "status": "resolved",
            "reason_code": "official_candidates_found",
            "candidate_count": 2,
        },
    ]
    assert resolution.error == "official_candidate_render_needs_review"


def test_candidate_failures_do_not_overwrite_official_search_outcome():
    indeed_url = "https://ca.indeed.com/viewjob?jk=trace-search-boundary"

    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return [
                SearchResult(
                    title="Platform Engineer",
                    url="https://careers.acme.example/jobs/platform-engineer",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(502, "private candidate failure")

    resolver = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=True,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
            location="Calgary, AB",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.source_path == "aggregator_structured_fallback"
    assert resolution.official_search_outcome is not None
    assert resolution.official_search_outcome.status == "candidates_found"
    assert resolution.official_search_outcome.reason_code == "official_candidates_found"
    assert resolution.error == "official_candidate_render_needs_review"
    assert [attempt.stage for attempt in resolution.attempts] == [
        "official_search",
        "official_candidate_render",
        "official_search",
        "official_search",
        "alternative_search",
        "structured_fallback",
    ]
    assert resolution.attempts[1].reason_code == "official_candidate_render_needs_review"
    source_resolution = source_resolution_metadata_for_resolution(resolution)
    assert source_resolution["official_discovery_status"] == "candidates_found"
    assert source_resolution["reason_code"] == "official_candidates_found"
    assert source_resolution["official_discovery_error"] == "official_candidates_found"
    assert source_resolution["attempts"][1]["reason_code"] == (
        "official_candidate_render_needs_review"
    )


def test_posting_discovery_records_unsafe_candidate_without_private_url():
    class FakeSearchClient:
        def search(self, query: str, limit: int):
            return [
                SearchResult(
                    title="Platform Engineer",
                    url="http://127.0.0.1/private-job?access_token=private",
                )
            ]

    class PolicyBlockedApplyResolver:
        def resolve(self, apply_url: str, context=None):
            raise AtsProviderError(
                400,
                "Apply URL is blocked by policy: private_network_blocked",
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSearchClient(),
        apply_resolver=PolicyBlockedApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://ca.indeed.com/viewjob?jk=unsafe-candidate",
            source_kind="indeed",
            company="Acme",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.official_search_outcome is not None
    assert resolution.official_search_outcome.status == "candidates_found"
    assert [attempt.to_dict() for attempt in resolution.attempts] == [
        {
            "stage": "official_search",
            "provider": "search",
            "status": "resolved",
            "reason_code": "official_candidates_found",
            "candidate_count": 1,
        },
        {
            "stage": "official_candidate_render",
            "provider": "ats_apply",
            "status": "needs_review",
            "reason_code": "official_candidate_url_policy_blocked",
        },
        {
            "stage": "official_search",
            "provider": "search",
            "status": "resolved",
            "reason_code": "official_candidates_found",
            "candidate_count": 1,
        },
    ]


def test_posting_resolution_attempt_drops_candidate_url_credentials():
    attempt = PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="ats_apply",
        status="needs_review",
        reason_code="official_candidate_render_needs_review: private provider output",
        candidate_url="https://user:private-secret" + "@careers.acme.example/jobs/123",
    )

    payload = attempt.to_dict()
    assert "candidate_url" not in payload
    assert payload["reason_code"] == "official_candidate_render_needs_review"
    malformed = PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="ats_apply",
        status="needs_review",
        reason_code="official_candidate_render_needs_review",
        candidate_url="https://[invalid",
    )
    assert "candidate_url" not in malformed.to_dict()
    signed = PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="ats_apply",
        status="needs_review",
        reason_code="official_candidate_render_needs_review",
        candidate_url=(
            "https://careers.acme.example/jobs/123?job_id=123&"
            "access_token=private&x-amz-signature=private#private"
        ),
    )
    assert signed.to_dict()["candidate_url"] == ("https://careers.acme.example/jobs/123?job_id=123")
    nested = PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="ats_apply",
        status="needs_review",
        reason_code="official_candidate_render_needs_review",
        candidate_url=(
            "https://careers.acme.example/jobs/123?job_id=123&"
            "redirect=https%3A%2F%2Fidentity.example%2Fcallback%3F"
            "access_token%3Dprivate&email=private%40example.com"
        ),
    )
    assert nested.to_dict()["candidate_url"] == ("https://careers.acme.example/jobs/123?job_id=123")


def test_external_posting_url_policy_blocks_linkedin_redirect_before_fetch(monkeypatch):
    requests_seen = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "careers.acme.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        if str(request.url) == "https://careers.acme.example/jobs/platform-engineer":
            return httpx.Response(
                302,
                headers={"Location": "https://www.linkedin.com/company/acme/jobs"},
            )
        raise AssertionError(f"Unexpected fetch: {request.url}")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        url_policy=ExternalPostingUrlPolicy(),
    )

    try:
        resolver.resolve("https://careers.acme.example/jobs/platform-engineer")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Apply URL is blocked by policy: external_aggregator_blocked"
    else:
        raise AssertionError("Expected LinkedIn redirect to be blocked")

    assert requests_seen == ["https://careers.acme.example/jobs/platform-engineer"]


def test_external_aggregator_fallback_url_policy_allows_same_aggregator_kind(monkeypatch):
    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    policy = ExternalAggregatorFallbackUrlPolicy(
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    )

    assert policy.check("https://ca.indeed.com/viewjob?jk=3cab9868c348dd54").allowed
    assert policy.check("https://www.indeed.com/jobs?q=engineer&jk=3cab9868c348dd54").allowed
    blocked = policy.check("https://www.linkedin.com/jobs/view/424242")
    assert not blocked.allowed
    assert blocked.reason == "external_aggregator_blocked"


def test_chrome_cdp_keeps_lease_owned_when_durable_registration_fails(
    monkeypatch,
) -> None:
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
    )

    class Lease:
        target_id = "registration-failed-target"
        endpoint_url = "http://127.0.0.1:9222"

        def close(self, *, timeout_seconds: float) -> bool:
            return False

    lease = Lease()

    def fail_registration(_lease):
        raise OSError("journal unavailable")

    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        fail_registration,
    )

    with pytest.raises(OSError, match="journal unavailable"):
        resolver._register_cleanup_lease(lease)

    assert resolver.pending_cleanup_leases() == (lease,)


def test_chrome_cdp_cancellation_closes_a_cleanup_lease_registered_after_cancel() -> None:
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
    )
    close_timeouts: list[float] = []

    class LateLease:
        target_id = "late-target"
        endpoint_url = "http://127.0.0.1:9222"

        def close(self, *, timeout_seconds: float) -> bool:
            close_timeouts.append(timeout_seconds)
            return True

    assert resolver.cancel_active_pages() == 0

    assert resolver._register_cleanup_lease(LateLease()) is False

    assert close_timeouts == [0.5]
    assert resolver._active_cleanup_leases == []


def test_chrome_cdp_cancellation_supervises_a_failed_late_lease_until_closed(monkeypatch) -> None:
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
    )
    closed = threading.Event()
    requested = _install_inline_cleanup_supervisor(monkeypatch)

    class FlakyLateLease:
        target_id = "flaky-late-target"
        endpoint_url = "http://127.0.0.1:9222"

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 0.5
            self.close_calls += 1
            if self.close_calls >= 2:
                closed.set()
                return True
            return False

    lease = FlakyLateLease()
    assert resolver.cancel_active_pages() == 0

    assert resolver._register_cleanup_lease(lease) is False

    assert closed.wait(timeout=1.0)
    assert requested == [lease]
    assert lease.close_calls >= 2
    assert resolver._active_cleanup_leases == [lease]
    assert resolver.release_pending_cleanup_leases((lease,)) == (lease,)
    assert resolver._active_cleanup_leases == []


def test_chrome_cdp_cancellation_during_registration_aborts_before_navigation(
    monkeypatch,
) -> None:
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    class Lease:
        target_id = "cancelled-before-navigation"
        endpoint_url = "http://127.0.0.1:9222"

        def close(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 0.5
            return False

    lease = Lease()

    def capture_after_cancellation(*_args, **_kwargs):
        resolver.cancel_active_pages()
        return lease

    requested = []
    monkeypatch.setattr(
        posting_discovery_module,
        "browser_page_target_ids",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        capture_after_cancellation,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        lambda _lease: "registered",
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "request_browser_cleanup",
        lambda leases: requested.extend(leases) or len(tuple(leases)),
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "confirm_browser_cleanup",
        lambda _lease: True,
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: browser_render_cancelled"
    assert page.goto_calls == []
    assert requested == [lease]


def test_chrome_cdp_navigation_claim_linearizes_with_cancellation() -> None:
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
    )

    resolver._begin_navigation()
    assert resolver._active_navigation_claims == 1

    assert resolver.cancel_active_pages() == 0
    resolver._end_navigation()

    assert resolver._active_navigation_claims == 0
    with pytest.raises(
        posting_discovery_module._PlaywrightRenderedError,
        match="browser_render_cancelled",
    ):
        resolver._begin_navigation()


def test_chrome_cdp_uses_unambiguous_target_inventory_fallback(monkeypatch) -> None:
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    lease = SimpleNamespace(
        target_id="new-validation-target",
        endpoint_url="http://127.0.0.1:9222",
        close=lambda **_kwargs: True,
    )
    captured_known_targets = []

    monkeypatch.setattr(
        posting_discovery_module,
        "browser_page_target_ids",
        lambda *_args, **_kwargs: frozenset({"existing-user-tab"}),
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        lambda *_args, **_kwargs: None,
    )

    def capture_fallback(_endpoint, known_targets, **_kwargs):
        captured_known_targets.append(set(known_targets))
        return lease

    monkeypatch.setattr(
        posting_discovery_module,
        "capture_new_browser_page_cleanup_lease",
        capture_fallback,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "register_browser_cleanup_lease",
        lambda _lease: "registered",
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "confirm_browser_cleanup",
        lambda _lease: True,
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "resolved"
    assert page.goto_calls
    assert captured_known_targets == [{"existing-user-tab"}]


def test_chrome_cdp_failed_context_cleanup_keeps_failed_target_lease_owned(monkeypatch) -> None:
    requested = _install_inline_cleanup_supervisor(monkeypatch)
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    fake_playwright = FakePlaywright(page)

    closed_on_retry = threading.Event()

    class FlakyLease:
        target_id = "failed-context-target"
        endpoint_url = "http://127.0.0.1:9222"

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 0.5
            self.close_calls += 1
            if self.close_calls >= 2:
                closed_on_retry.set()
            return closed_on_retry.is_set()

    lease = FlakyLease()
    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        lambda *_args, **_kwargs: lease,
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "cleanup_owned_browser_context",
        lambda *_args, **_kwargs: BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED),
        raising=False,
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(fake_playwright),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_failed: owned_context_cleanup_failed"
    assert closed_on_retry.wait(timeout=1.0)
    assert requested == [lease]
    assert lease.close_calls >= 2
    assert resolver._active_cleanup_leases == [lease]
    assert resolver.cancel_active_pages() == 1
    assert resolver._active_cleanup_leases == []


def test_chrome_cdp_refuses_navigation_without_supervisor_registration(monkeypatch) -> None:
    source_url = "https://20.12.34.56/jobs/platform-engineer"
    page = FakePage(
        source_url,
        "Platform Engineer | Example",
        "Platform Engineer\nCompany: Example\nBuild reliable services.",
    )
    monkeypatch.setattr(
        posting_discovery_module,
        "capture_browser_page_cleanup_lease",
        lambda *_args, **_kwargs: None,
    )
    resolver = ChromeCdpRenderedPageResolver(
        endpoint_url="http://127.0.0.1:9222",
        timeout_seconds=15,
        playwright_factory=lambda: FakePlaywrightContextManager(FakePlaywright(page)),
    )

    resolution = resolver.resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="generic",
            company="Example",
            title="Platform Engineer",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == ("chrome_cdp_failed: browser_target_registration_unavailable")
    assert page.goto_calls == []
    assert page.closed is True
