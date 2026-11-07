import sys
import json
from contextlib import contextmanager

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.eluta import ElutaAdapter
from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
from jobcockpit.ingestion.posting_discovery import ExternalPostingHint, TracedRenderedPageResolver
from jobcockpit.ingestion.posting_links import default_rendered_page_resolver
from test_posting_discovery import FakeBrowserContext, FakePage


SOURCE = "https://ca.indeed.com/viewjob?jk=0000000000000001"
BODY = "Platform Engineer\nCompany: Acme\nLocation: Remote Canada\nBuild internal platforms."


def test_firefox_waits_for_workday_body_after_indeed_redirect(tmp_path):
    # The failed Genesys/BCI Apply redirects returned empty DOM captures.
    destination = "https://acme.wd1.myworkdayjobs.com/Careers/job/City/Platform-Engineer_R123"
    class HydratingPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = destination
        def wait_for_timeout(self, delay):
            self._body_text = BODY
    page = HydratingPage(destination, "Careers", "")
    resolver, _, _ = browser_resolver(tmp_path, page)
    posting = resolver._render(ExternalPostingHint(
        source_url=SOURCE.replace("viewjob", "applystart"), source_kind="indeed",
        title="Platform Engineer", company="Acme",
    ))
    assert posting.source_url == destination
    assert "Build internal platforms" in posting.raw_text


@pytest.mark.parametrize("source,assets", [
    ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?jobId=42",
     ("https://static.workforcenow.adp.com/mas/mdf/runtime.js",
      "https://web-components-assets.synerg.dit.paascloud.oneadp.com/config/synerg-config.js")),
    ("https://careers.bird.ca/ca/en/job/JR-42/Platform-Engineer",
     ("https://cdn.phenompeople.com/CareerConnectResources/common/js/vendor/ph-basic.js",)),
])
def test_scheduled_browser_loads_observed_adp_and_bird_assets(
    tmp_path, allow_public_dns, source, assets,
):
    # Saved ADP and live Bird failures had empty bodies because these required
    # bootstrap scripts were blocked by the scheduled origin policy.
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy
    from urllib.parse import urlsplit

    allow_public_dns({urlsplit(url).hostname for url in (source, *assets)})

    class AssetPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            assert self.dispatch_request(url).continued
            requests = [self.dispatch_request(asset, navigation=False) for asset in assets]
            if all(request.continued for request in requests):
                self._body_text = BODY

    page = AssetPage(source, "Careers", "")
    resolver, context, _ = browser_resolver(tmp_path, page)
    resolver.url_policy = ScheduledRenderedUrlPolicy()
    result = resolver.load_page(ExternalPostingHint(source_url=source, source_kind="url"))
    assert result.status == "resolved", result.error
    assert "Build internal platforms" in result.posting.raw_text
    assert context.closed


@pytest.mark.parametrize("asset", [
    "https://new-cdn.example/runtime.js",
    "https://new-cdn.example/loader-without-a-js-suffix",
    "https://www.linkedin.com/assets/runtime.js",
    "https://new-api.example/data.json",
    "http://new-cdn.example/styles.css",
    "https://new-cdn.example:8443/font.woff2",
])
def test_scheduled_resources_need_no_domain_whitelist(allow_public_dns, asset):
    from urllib.parse import urlsplit
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    source = "https://jobs.example/posting/42"
    allow_public_dns({urlsplit(asset).hostname})
    policy = ScheduledRenderedUrlPolicy()
    assert policy.check(asset).allowed
    assert not policy.check_browser_final_url(source, asset).allowed


@pytest.mark.parametrize("asset,ip", [
    ("https://new-cdn.example/runtime.js", "127.0.0.1"),
    ("https://new-cdn.example/runtime.js", "10.0.0.1"),
    ("https://new-cdn.example/runtime.js", "169.254.169.254"),
    ("https://new-cdn.example/runtime.js", "::1"),
])
def test_scheduled_resources_still_require_public_dns(allow_public_dns, asset, ip):
    from urllib.parse import urlsplit
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    policy = ScheduledRenderedUrlPolicy()
    allow_public_dns({urlsplit(asset).hostname}, ip=ip)
    result = policy.check(asset)
    assert not result.allowed
    assert result.reason == "private_network_blocked"


@pytest.mark.parametrize("asset", [
    "http://localhost/script.js", "http://127.0.0.1/script.js",
    "http://169.254.169.254/latest/meta-data", "https://[::1]/script.js",
    "file:///C:/Users/private/script.js", "ftp://public.example/script.js",
    "https://public.example:0/script.js", "https://public.example:bad/script.js",
])
def test_scheduled_resources_reject_local_or_invalid_targets(asset):
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    assert not ScheduledRenderedUrlPolicy().check(asset).allowed


def test_scheduled_preparation_allows_public_connections_but_scopes_final_page(allow_public_dns):
    from jobcockpit.ingestion.invisible_session import PreparationUrlPolicy
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    resource = "https://new-cdn.example/widget.js"
    source = SOURCE
    allow_public_dns({"new-cdn.example"})
    policy = PreparationUrlPolicy(source, ScheduledRenderedUrlPolicy())
    assert policy.check(resource).allowed
    assert not policy.check_browser_final_url(source, resource).allowed


@pytest.mark.parametrize("routed", [True, False])
def test_public_navigation_does_not_allow_an_unapproved_final_url(tmp_path, routed):
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    source = "https://20.12.34.56/posting/42"
    resource = "https://20.12.34.57/runtime.js"

    class RedirectPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            assert self.dispatch_request(url).continued
            assert self.dispatch_request(resource, navigation=False).continued
            if routed:
                assert self.dispatch_request(resource).continued
            # Validate the final URL whether or not a redirect was routed.
            self.url = resource

    resolver, context, _ = browser_resolver(tmp_path, RedirectPage(source, "Job", BODY))
    resolver.url_policy = ScheduledRenderedUrlPolicy()
    result = resolver.load_page(ExternalPostingHint(source_url=source, source_kind="url"))
    assert result.posting is None
    assert "rendered_cross_origin_blocked" in result.error
    assert context.closed


def test_scheduled_guard_handles_unclassified_resources_and_revalidates_redirect_dns(
    allow_public_dns,
):
    from types import SimpleNamespace
    from jobcockpit.ingestion.posting_discovery import _PlaywrightUrlPolicyGuard
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy
    from test_posting_discovery import FakeRoute

    resource = "https://new-cdn.example/runtime.js"
    guard = _PlaywrightUrlPolicyGuard(
        source_url="https://20.12.34.56/posting/42", url_policy=ScheduledRenderedUrlPolicy(),
    )
    # Invisible Playwright reports even bootstrap scripts as "other".
    request = SimpleNamespace(
        url=resource, resource_type="other", is_navigation_request=lambda: False,
        method="GET", post_data_buffer=None,
        all_headers=lambda: {"sec-fetch-dest": "script"},
    )
    allow_public_dns({"new-cdn.example"}, ip="93.184.216.34")
    first = FakeRoute()
    guard._handle_request(first, request)
    assert first.continued

    # A later request (including a redirect) must not reuse the public answer.
    allow_public_dns({"new-cdn.example"}, ip="10.0.0.1")
    second = FakeRoute()
    guard._handle_request(second, request)
    assert second.aborted
    assert "private_network_blocked" in guard.blocked_subresource_diagnostic()


@pytest.mark.parametrize("ip,allowed", [("93.184.216.34", True), ("10.0.0.1", False)])
def test_scheduled_resource_websockets_retain_public_dns_requirement(allow_public_dns, ip, allowed):
    from jobcockpit.ingestion.posting_discovery import _PlaywrightUrlPolicyGuard
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    allow_public_dns({"new-api.example"}, ip=ip)
    context = FakeBrowserContext()
    guard = _PlaywrightUrlPolicyGuard(
        source_url="https://20.12.34.56/posting/42", url_policy=ScheduledRenderedUrlPolicy(),
    )
    guard.install(context)
    route = context.dispatch_web_socket("wss://new-api.example/socket")
    assert route.connected is allowed
    assert route.closed is not allowed
    guard.close()


@pytest.mark.parametrize("destination,top_level", [("document", True), ("script", False), ("iframe", False)])
def test_firefox_navigation_classification_uses_browser_fetch_destination(destination, top_level):
    from types import SimpleNamespace
    from jobcockpit.ingestion.posting_discovery import _is_top_level_navigation_request

    # The installed wrapper reports TYPE_DOCUMENT as "other" with a false flag.
    request = SimpleNamespace(
        resource_type="other", is_navigation_request=lambda: False,
        headers={"sec-fetch-dest": destination},
        frame=SimpleNamespace(parent_frame=None),
    )
    assert _is_top_level_navigation_request(request) is top_level


@pytest.mark.parametrize("with_hints", [False, True])
@pytest.mark.parametrize("allow_fallback", [False, True])
@pytest.mark.parametrize("empty_first", [False, True])
@pytest.mark.parametrize("title,structured_title", [
    ("Acme - Commercial Optimization Co-op Student", "Acme - Commercial Optimization Co-op Student"),
    # Observed Workday JSON-LD encodes the ampersand, unlike the Indeed title.
    ("Software Developer Intern, Predictions Data & Decision Science",
     "Software Developer Intern, Predictions Data &amp; Decision Science"),
])
def test_indeed_company_apply_precedes_search(
    tmp_path, monkeypatch, with_hints, allow_fallback, empty_first, title, structured_title,
):
    from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver
    from jobcockpit.ingestion.posting_resolution import ResolvedExternalPosting
    body = f"Search\n{title}\nAcme Ltd.\nCalgary, AB\nFull job description\n" + "Analyze operations and support data analysis and reporting. " * 10
    page = FakePage(SOURCE, title + " - Calgary, AB - Indeed.com", body)
    resolver, _, _ = browser_resolver(tmp_path, page)
    resolver.retries = int(empty_first)
    page.content = lambda: '<a data-testid="viewjob-apply" href="/applystart?jk=0000000000000001">Apply on company site</a>'
    original_render = resolver._render
    visited = []
    destination = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Calgary/Commercial-Co-op_R123"
    def render(hint):
        visited.append(hint.source_url)
        if "/applystart?" not in hint.source_url:
            return original_render(hint)
        if empty_first and len(visited) == 2:
            from jobcockpit.ingestion.posting_discovery import _PlaywrightRenderedError
            raise _PlaywrightRenderedError('empty_rendered_page')
        payload = {"@context": "https://schema.org", "@type": "JobPosting", "title": structured_title,
                   "hiringOrganization": {"@type": "Organization", "name": "Acme Ltd."},
                   "description": "Analyze operations and support data analysis and reporting. " * 10,
                   "jobLocation": {"@type": "Place", "address": {"addressLocality": "Calgary", "addressRegion": "AB"}}}
        return ResolvedExternalPosting(source_url=destination, raw_text=body,
            evidence_origin="browser_dom", rendered_html='<script type="application/ld+json">' + json.dumps(payload) + '</script>')
    monkeypatch.setattr(resolver, "_render", render)
    class NoSearch:
        def search(self, *args, **kwargs):
            pytest.fail("The observed official link should be tried before search")
    result = PostingDiscoveryResolver(
        search_client=NoSearch(), allow_aggregator_fallback=allow_fallback,
        rendered_fallback_resolver=TracedRenderedPageResolver(
            inner_resolver=resolver, provider="invisible_playwright",
        ),
    ).resolve(
        ExternalPostingHint(source_url=SOURCE, source_kind="indeed",
                            title=title if with_hints else "", company="Acme Ltd." if with_hints else ""))
    assert result.status == "resolved", result.error
    assert result.posting.source_url == destination
    assert len(visited) == 2 + int(empty_first)
    assert any(attempt.reason_code == "official_posting_resolved" for attempt in result.attempts)


@pytest.mark.parametrize('retries,error,expected_attempts', [
    (0, 'empty_rendered_page', 1),
    (3, 'empty_rendered_page', 2),
    (3, 'owned_context_cleanup_failed', 1),
    (3, 'url_policy_blocked', 1),
])
def test_company_apply_empty_retry_is_bounded_and_preserves_failures(tmp_path, monkeypatch, retries, error, expected_attempts):
    from jobcockpit.ingestion.posting_discovery import _PlaywrightRenderedError
    from jobcockpit.ingestion.posting_resolution import ResolvedExternalPosting

    resolver, _, _ = browser_resolver(tmp_path, FakePage(SOURCE, 'Platform Engineer', BODY))
    resolver.retries = retries
    calls = []
    def render(hint):
        calls.append(hint.source_url)
        raise _PlaywrightRenderedError(error)
    monkeypatch.setattr(resolver, '_render', render)
    hint = ExternalPostingHint(source_url=SOURCE, source_kind='indeed', title='Platform Engineer', company='Acme')
    posting = ResolvedExternalPosting(source_url=SOURCE, raw_text=BODY,
        rendered_html='<a data-testid="viewjob-apply" href="/applystart?jk=0000000000000001">Apply on company site</a>')
    result = resolver.resolve_company_apply(hint, posting)
    assert len(calls) == expected_attempts
    assert result.status == 'needs_review'
    assert result.error == error


@pytest.mark.parametrize("href,label", [
    ("/applystart?jk=0000000000000002", "Apply on company site"),
    ("/applystart?jk=0000000000000001", "Apply now"),
    ("javascript:alert(1)", "Apply on company site"),
])
def test_indeed_apply_link_rejects_unrelated_or_native_controls(href, label):
    from jobcockpit.ingestion.invisible_browser import _indeed_company_apply_url
    from jobcockpit.ingestion.posting_resolution import ResolvedExternalPosting
    hint = ExternalPostingHint(source_url=SOURCE, source_kind="indeed")
    posting = ResolvedExternalPosting(source_url=SOURCE, raw_text=BODY,
        rendered_html=f'<a data-testid="viewjob-apply" href="{href}">{label}</a>')
    assert not _indeed_company_apply_url(hint, posting)


def test_url_only_identity_does_not_accept_different_indeed_job(tmp_path):
    from jobcockpit.ingestion.invisible_browser import _with_observed_indeed_identity
    from jobcockpit.ingestion.posting_resolution import ResolvedExternalPosting
    hint = ExternalPostingHint(source_url=SOURCE, source_kind="indeed")
    posting = ResolvedExternalPosting(source_url=SOURCE.replace("0001", "0002"), raw_text=BODY,
        evidence_origin="browser_dom", complete_body_text="Full job description " + BODY * 50)
    assert _with_observed_indeed_identity(hint, posting).confidence == 0


@pytest.mark.parametrize("destination", ["https://127.0.0.1/private", "https://other.example/jobs/123"])
def test_company_apply_rejects_private_or_wrong_employer(tmp_path, monkeypatch, allow_public_dns, destination):
    from jobcockpit.ingestion.posting_resolution import ResolvedExternalPosting
    allow_public_dns({"other.example"})
    hint = ExternalPostingHint(source_url=SOURCE, source_kind="indeed", title="Platform Engineer", company="Acme")
    posting = ResolvedExternalPosting(source_url=SOURCE, raw_text=BODY,
        rendered_html=f'<a data-testid="viewjob-apply" href="{destination}">Apply on company site</a>')
    resolver, _, _ = browser_resolver(tmp_path, FakePage(SOURCE, "Platform Engineer", BODY))
    calls = []
    def render(hint):
        calls.append(hint.source_url)
        return ResolvedExternalPosting(source_url=destination, raw_text=BODY, evidence_origin="browser_dom")
    monkeypatch.setattr(resolver, "_render", render)
    result = resolver.resolve_company_apply(hint, posting)
    assert result.status == "needs_review"
    if "127.0.0.1" in destination:
        assert not calls
    else:
        assert calls == [destination]
        assert result.error == "official_candidate_identity_mismatch"


@pytest.mark.parametrize("allow_fallback", [False, True])
@pytest.mark.parametrize("with_search", [False, True])
def test_failed_company_apply_respects_fallback_policy(
    tmp_path, monkeypatch, allow_fallback, with_search,
):
    from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver, _PlaywrightRenderedError
    page = FakePage(SOURCE, "Platform Engineer", BODY)
    resolver, _, _ = browser_resolver(tmp_path, page)
    page.content = lambda: '<a data-testid="viewjob-apply" href="/applystart?jk=0000000000000001">Apply on company site</a>'
    original = resolver._render
    def render(hint):
        if "/applystart?" in hint.source_url:
            raise _PlaywrightRenderedError("blocked_rendered_page")
        return original(hint)
    monkeypatch.setattr(resolver, "_render", render)
    class EmptySearch:
        def search(self, *args, **kwargs):
            return []
    result = PostingDiscoveryResolver(
        rendered_fallback_resolver=resolver, allow_aggregator_fallback=allow_fallback,
        search_client=EmptySearch() if with_search else None,
    ).resolve(
        ExternalPostingHint(source_url=SOURCE, source_kind="indeed", company="Acme", title="Platform Engineer"))
    if allow_fallback:
        assert result.status == "resolved"
        assert result.posting.source_url == SOURCE
    else:
        assert result.status != "resolved"
        assert result.posting is None
    assert any(a.reason_code == "blocked_rendered_page" for a in result.attempts)


@pytest.mark.parametrize("allow_fallback", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_company_discovery_early_returns_respect_fallback_policy(allow_fallback, terminal):
    from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver
    from jobcockpit.ingestion.posting_resolution import PostingResolution, ResolvedExternalPosting

    class Render:
        def resolve(self, hint):
            return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
                source_url=SOURCE,
                raw_text="This job has expired on Indeed." if terminal else "",
                evidence_origin="browser_dom",
            ))

        def resolve_company_apply(self, hint, posting):
            assert not terminal
            return PostingResolution(status="not_found")

    result = PostingDiscoveryResolver(
        rendered_fallback_resolver=Render(), allow_aggregator_fallback=allow_fallback,
    ).resolve(ExternalPostingHint(source_url=SOURCE, source_kind="indeed"))
    if allow_fallback:
        assert result.status == "resolved"
        assert result.posting.source_url == SOURCE
    else:
        assert result.status != "resolved"
        assert result.posting is None


def test_url_only_indeed_posting_uses_observed_identity(tmp_path):
    # Minimized from the supplied AltaGas posting: matching document/body title,
    # employer on the next line, then a substantial full-description section.
    body = "Search\nAcme - Commercial Optimization Co-op Student\nAcme Ltd.\nCalgary, AB\nFull job description\n" + (
        "Analyze commercial operations and support the team with data analysis and reporting. " * 6
    )
    page = FakePage(SOURCE, "Acme - Commercial Optimization Co-op Student - Calgary, AB - Indeed.com", body)
    resolver, _, _ = browser_resolver(tmp_path, page)
    result = resolver.resolve(ExternalPostingHint(source_url=SOURCE, source_kind="indeed"))
    assert result.status == "resolved", result.error
    assert result.posting.confidence >= .8


def test_url_only_indeed_navigation_without_description_needs_review(tmp_path):
    page = FakePage(SOURCE, "Acme - Commercial Optimization Co-op Student - Calgary, AB - Indeed.com",
                    "Search\nAcme - Commercial Optimization Co-op Student\nAcme Ltd.\nCalgary, AB\nApply on company site")
    resolver, _, _ = browser_resolver(tmp_path, page)
    assert resolver.resolve(ExternalPostingHint(source_url=SOURCE, source_kind="indeed")).status == "needs_review"


@pytest.mark.parametrize("scheduled", [False, True])
def test_greenhouse_embed_capture_survives_www_redirect(tmp_path, allow_public_dns, scheduled):
    allow_public_dns({"www.example.test", "example.test", "job-boards.greenhouse.io"})
    # Minimized from the user-supplied Tower Summer 2027 posting: the outer
    # careers page contains no description; a job_app frame owns the job text.
    source = "https://www.example.test/open-positions/?gh_jid=42"
    class RedirectPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = url.replace("www.", "")
        def evaluate(self, expression, limit=None):
            if isinstance(limit, dict) and "height" in limit:
                self.resize_request = limit
                return None
            return super().evaluate(expression, limit)
    page = RedirectPage("https://example.test/open-positions/?gh_jid=42", "Open positions", "Build your career")
    class EmbeddedFrame(FakePage):
        parent_frame = None  # Observed for Invisible Playwright cross-origin frames.
        def evaluate(self, expression, limit=None):
            if "document.body, limits" in expression:
                return self._evaluate_body(expression, limit)
            if "document.body.scrollHeight" in expression:
                return 6307
            return super().evaluate(expression, limit)
        def locator(self, selector):
            from types import SimpleNamespace
            def broken_handle(*args):
                raise RuntimeError("Cannot find object with id")
            return SimpleNamespace(evaluate=broken_handle)
    frame = EmbeddedFrame("https://job-boards.greenhouse.io/embed/job_app?for=example&token=42&validityToken=private", "Software Developer Intern", "Software Developer Intern (Summer 2027)\nMontreal\nBuild trading software.")
    frame.content = lambda: "<main>Software Developer Intern (Summer 2027)</main>"
    page.frames = [page, frame]
    page.main_frame = page
    resolver, _, _ = browser_resolver(tmp_path, page)
    if scheduled:
        from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy
        resolver.url_policy = ScheduledRenderedUrlPolicy()
    resolver.health_root = tmp_path / "health"
    result = resolver.load_page(ExternalPostingHint(source_url=source, source_kind="generic"))
    assert result.status == "resolved", result.error
    assert "Build trading software" in result.posting.complete_body_text
    assert "Build your career" not in result.posting.raw_text
    assert "validityToken" not in result.posting.raw_text
    assert result.posting.rendered_html == frame.content()
    assert page.resize_request == {"jobId": "42", "board": "example", "height": 6307}
    from jobcockpit.ingestion.browser_health import summarize
    assert summarize(resolver.health_root)["counts"]["succeeded"] == 1


@pytest.mark.parametrize("token", ["43", "42&token=43"])
def test_greenhouse_embed_does_not_capture_another_job(tmp_path, token, allow_public_dns):
    allow_public_dns({"example.test", "job-boards.greenhouse.io"})
    source = "https://example.test/open-positions/?gh_jid=42"
    page = FakePage(source, "Open positions", "Build your career")
    frame = FakePage(f"https://job-boards.greenhouse.io/embed/job_app?for=example&token={token}", "Other job", "Wrong job description")
    page.frames = [page, frame]
    page.main_frame = page
    resolver, _, _ = browser_resolver(tmp_path, page)
    result = resolver.load_page(ExternalPostingHint(source_url=source, source_kind="generic"))
    assert result.status == "resolved", result.error
    assert "Wrong job description" not in result.posting.raw_text


@pytest.mark.parametrize("url,allowed", [
    ("https://example.test/open-positions/?gh_jid=42", True),
    ("https://example.test/open-positions/?gh_jid=43", False),
    ("https://example.test/open-positions/", False),
    ("https://example.test/other/?gh_jid=42", False),
    ("https://example.test:444/open-positions/?gh_jid=42", False),
    ("http://example.test/open-positions/?gh_jid=42", False),
    ("https://other.example.test/open-positions/?gh_jid=42", False),
])
def test_canonical_www_redirect_preserves_posting_identity(url, allowed):
    from jobcockpit.ingestion.posting_discovery import _rendered_page_url_matches_hint
    assert _rendered_page_url_matches_hint(url, "https://www.example.test/open-positions/?gh_jid=42") is allowed


def test_scheduled_embed_admission_retains_private_network_guard(allow_public_dns):
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy
    from jobcockpit.ingestion.posting_discovery import _PlaywrightUrlPolicyGuard
    policy = ScheduledRenderedUrlPolicy()
    source = "https://www.example.test/open-positions/?gh_jid=42"
    frame = "https://job-boards.greenhouse.io/embed/job_app?token=42"
    assert policy.check_browser_final_url(source, frame).allowed
    assert policy.check_browser_final_url(source, "https://job-boards.cdn.greenhouse.io/assets/app.js").allowed
    assert not policy.check_browser_final_url(source, "https://greenhouse.io.attacker.test/frame").allowed
    assert not policy.check_browser_final_url("https://example.test/jobs/", frame).allowed
    allow_public_dns("job-boards.greenhouse.io", ip="127.0.0.1")
    guard = _PlaywrightUrlPolicyGuard(source_url=source, url_policy=policy)
    assert not guard._check_network(frame, allow_browser_local=False).allowed


def test_scheduled_indeed_can_load_observed_verification_widget(tmp_path, allow_public_dns):
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    # A September 29 failed posting remained on "Just a moment..." because
    # the scheduled policy blocked its Cloudflare widget and follow-up host.
    # Both are needed for passive completion, without preparation or clicks.
    widget = "https://challenges.cloudflare.com/turnstile/v0/api.js"
    attestation = "https://hagen.challenges.cloudflare.com/attestation"
    allow_public_dns({"ca.indeed.com", "challenges.cloudflare.com", "hagen.challenges.cloudflare.com"})

    class VerificationPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.widget_request = self.dispatch_request(widget, navigation=False)
            self.attestation_request = self.dispatch_request(attestation, navigation=False)
            if self.widget_request.continued and self.attestation_request.continued:
                self._title = "Platform Engineer - Acme"
                self._body_text = BODY

    page = VerificationPage(SOURCE, "Just a moment...", "Verify you are human")
    resolver, context, _ = browser_resolver(tmp_path, page)
    resolver.url_policy = ScheduledRenderedUrlPolicy()

    result = resolver.resolve(hint())

    assert result.status == "resolved", result.error
    assert page.widget_request.continued
    assert page.attestation_request.continued
    assert len(page.goto_calls) == 1
    assert context.closed


@pytest.mark.parametrize("source,widget,allowed", [
    (SOURCE, "https://challenges.cloudflare.com/widget", True),
    (SOURCE, "https://hagen.challenges.cloudflare.com/attestation", True),
    (SOURCE.replace("ca.indeed.com", "www.indeed.ca"),
     "https://challenges.cloudflare.com:443/widget", True),
    (SOURCE, "http://challenges.cloudflare.com/widget", False),
    (SOURCE, "https://challenges.cloudflare.com:8443/widget", False),
    (SOURCE, "https://challenges.cloudflare.com.evil.example/widget", False),
    (SOURCE, "https://unrelated.cloudflare.com/widget", False),
    (SOURCE, "https://unobserved.challenges.cloudflare.com/widget", False),
    (SOURCE.replace("ca.indeed.com", "indeed.com.evil.example"),
     "https://challenges.cloudflare.com/widget", False),
    ("https://www.linkedin.com/jobs/view/1234567890",
     "https://challenges.cloudflare.com/widget", False),
])
def test_scheduled_verification_allowance_is_limited_to_indeed(source, widget, allowed):
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    assert ScheduledRenderedUrlPolicy().check_browser_final_url(source, widget).allowed is allowed


@pytest.mark.parametrize("host", ["challenges.cloudflare.com", "hagen.challenges.cloudflare.com"])
def test_scheduled_verification_widget_still_rejects_private_dns(allow_public_dns, host):
    from jobcockpit.ingestion.posting_discovery import _PlaywrightUrlPolicyGuard
    from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

    widget = f"https://{host}/widget"
    policy = ScheduledRenderedUrlPolicy()
    assert policy.check_browser_final_url(SOURCE, widget).allowed
    allow_public_dns({host}, ip="127.0.0.1")
    guard = _PlaywrightUrlPolicyGuard(source_url=SOURCE, url_policy=policy)
    assert not guard._check_network(widget, allow_browser_local=False).allowed


def hint(url=SOURCE, **overrides):
    values = dict(source_url=url, source_kind="indeed", company="Acme", title="Platform Engineer")
    values.update(overrides)
    return ExternalPostingHint(**values)


def browser_resolver(tmp_path, page):
    class FirefoxContext(FakeBrowserContext):
        def new_page(self):
            self.firefox_route_installed_before_new_page = bool(self.route_handlers)
            return super().new_page()

        def new_cdp_session(self, page):
            raise RuntimeError("Firefox does not support CDP")

        def route_web_socket(self, *_args):
            raise RuntimeError("WebSocket routing is not implemented")

    context = FirefoxContext(page)
    context.set_default_timeout = lambda value: setattr(context, "timeout", value)
    context.set_default_navigation_timeout = lambda value: setattr(context, "navigation_timeout", value)
    calls = []

    @contextmanager
    def factory(**kwargs):
        calls.append(kwargs)
        try:
            yield context
        finally:
            calls.append("exited")

    page.content = lambda: "<html><body>" + page._body_text + "</body></html>"
    resolver = InvisiblePlaywrightRenderedPageResolver(
        user_data_dir=tmp_path / "firefox", invisible_factory=factory,
        timeout_seconds=15, auth_wall_wait_seconds=0,
        auto_prepare=False,
    )
    return resolver, context, calls


def test_provider_captures_html_and_preserves_guarded_lifecycle(tmp_path):
    page = FakePage(SOURCE, "Platform Engineer - Acme", BODY)
    resolver, context, calls = browser_resolver(tmp_path, page)
    result = TracedRenderedPageResolver(
        inner_resolver=resolver, provider="invisible_playwright",
    ).resolve(hint())

    assert result.status == "resolved"
    assert result.posting.evidence_origin == "browser_dom"
    assert "invisible_playwright" in result.posting.evidence
    assert result.posting.rendered_html == page.content()
    assert result.attempts[-1].provider == "invisible_playwright"
    assert calls[0]["profile_dir"] == str(tmp_path / "firefox" / "indeed" / "seed-42")
    assert calls[0]["seed"] == 42
    assert calls[0]["proxy"]["server"].startswith("http://127.0.0.1:")
    assert calls[0]["extra_prefs"]["dom.serviceWorkers.enabled"] is True
    assert any('"serviceWorker"' in script for script in context.init_scripts)
    assert calls[0]["extra_prefs"]["media.peerconnection.enabled"] is False
    assert calls[0]["extra_prefs"]["network.proxy.allow_hijacking_localhost"] is True
    assert calls[0]["extra_prefs"]["network.proxy.no_proxies_on"] == ""
    assert context.timeout == context.navigation_timeout == 15000
    assert context.firefox_route_installed_before_new_page
    assert not any("SharedWorker" in str(script) for script in context.init_scripts)
    assert context.hardening_installed_before_new_page
    assert page.hardening_verified
    assert context.closed and page.closed and calls[-1] == "exited"


@pytest.mark.parametrize("separator", ["\n", " "])
def test_interview_contact_phrase_does_not_reject_a_visible_job(tmp_path, separator):
    # Minimized from the failed LinkedIn postings' actual interview-process text.
    source = "https://www.linkedin.com/jobs/view/9999999977"
    body = separator.join([
        "Platform Engineer", "Company: Acme", "About the job", "Build internal platforms.",
        "Interview Process",
        "We do our best to communicate outcomes to all applicants by email or phone call.",
    ])
    resolver, context, _ = browser_resolver(tmp_path, FakePage(source, "Platform Engineer - Acme", body))

    result = resolver.resolve(hint(source, source_kind="linkedin"))

    assert result.status == "resolved"
    assert "email or phone call" in result.posting.raw_text
    assert context.closed


@pytest.mark.parametrize("separator", ["\n", " "])
def test_login_credential_fields_still_reject_the_page(tmp_path, separator):
    source = "https://www.linkedin.com/jobs/view/9999999977"
    body = separator.join(["Platform Engineer", "Company: Acme", "Email or phone", "Password"])
    resolver, context, _ = browser_resolver(tmp_path, FakePage(source, "Platform Engineer - Acme", body))

    result = resolver.resolve(hint(source, source_kind="linkedin"))

    assert result.status == "needs_review"
    assert result.posting is None
    assert result.error == "playwright_failed: auth_wall_unresolved"
    assert context.closed


@pytest.mark.parametrize("body", ["Just a moment... Verify you are human", "Access denied"])
def test_challenge_is_not_imported_and_browser_is_closed(tmp_path, body):
    resolver, context, calls = browser_resolver(tmp_path, FakePage(SOURCE, "Blocked", body))
    result = resolver.resolve(hint())
    assert result.status == "needs_review"
    assert result.posting is None
    assert context.closed and calls[-1] == "exited"


@pytest.mark.parametrize("url,title,error", [
    (SOURCE, "Authenticating...", "auth_wall_unresolved"),
    ("https://secure.indeed.com/auth;jsessionid=fixture", "Error 400 Bad Request", "rendered_http_error"),
])
def test_authentication_intermediates_never_resolve_as_job_content(tmp_path, url, title, error):
    class AuthenticationPage(FakePage):
        def goto(self, source, **kwargs):
            super().goto(source, **kwargs)
            self.url = url
    resolver, context, _ = browser_resolver(tmp_path, AuthenticationPage(url, title, "HTTP ERROR 400 Bad Request"))
    resolver.health_root = tmp_path / "health"
    result = resolver.load_page(hint())
    assert result.posting is None
    assert error in result.error
    assert context.closed
    if error == "rendered_http_error":
        from jobcockpit.ingestion.browser_health import summarize

        issue = summarize(resolver.health_root)["recent_issues"][0]
        assert issue["error_code"] == "rendered_http_error"
        assert issue["signals"] == ["HTTP 400 at secure.indeed.com/auth"]
        assert "jsessionid" not in str(issue)


def test_private_redirect_is_blocked_and_browser_is_closed(tmp_path):
    class RedirectPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            route = self.dispatch_request("http://127.0.0.1/private")
            assert route.aborted
            raise RuntimeError("navigation blocked")

    resolver, context, _ = browser_resolver(tmp_path, RedirectPage(SOURCE, "Job", BODY))
    result = resolver.resolve(hint())
    assert result.status == "needs_review"
    assert "localhost_not_allowlisted" in result.error
    assert context.closed


def test_search_page_load_preserves_query_identity_without_job_confidence_gate(tmp_path):
    url = "https://www.eluta.ca/search?q=co-op&pg=2"
    resolver, context, _ = browser_resolver(tmp_path, FakePage(url, "Search results", "Available jobs"))
    search_hint = hint(
        url, source_kind="eluta", title="", company="", url_identity="path_and_query",
        identity_query_groups=(("q",), ("pg",)),
    )
    result = resolver.load_page(search_hint)
    assert result.status == "resolved"
    assert "Available jobs" in result.posting.rendered_html
    assert context.closed

    class WrongPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = url.replace("pg=2", "pg=1")

    resolver, _, _ = browser_resolver(tmp_path, WrongPage(url, "Search", "Available jobs"))
    assert resolver.load_page(search_hint).error == "playwright_url_mismatch"


def test_missing_optional_dependency_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "invisible_playwright", None)
    resolver = InvisiblePlaywrightRenderedPageResolver(user_data_dir=tmp_path / "firefox")
    result = resolver.resolve(hint())
    assert result.status == "needs_review"
    assert "uv sync --extra invisible-browser" in result.error


def test_missing_firefox_hardening_prevents_navigation_and_closes_context(tmp_path):
    class UnhardenedPage(FakePage):
        def evaluate(self, expression, limit=None):
            if "RTCPeerConnection" in expression:
                return False
            return super().evaluate(expression, limit)

    page = UnhardenedPage(SOURCE, "Job", BODY)
    resolver, context, calls = browser_resolver(tmp_path, page)
    result = resolver.resolve(hint())
    assert "rendered_context_hardening_verification_failed" in result.error
    assert page.goto_calls == []
    assert context.closed and calls[-1] == "exited"


def test_selection_configures_main_and_eluta_without_changing_chrome_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "invisible_playwright")
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_USER_DATA_DIR", str(tmp_path / "firefox"))
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_SEED", "123")
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_TIMEZONE", "America/Toronto")
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_AUTO_PREPARE", "false")
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("JOBCOCKPIT_INVISIBLE_PLAYWRIGHT_PREPARE_COOLDOWN_SECONDS", "120")
    settings = Settings()
    traced = default_rendered_page_resolver(settings)
    assert traced.provider == "invisible_playwright"
    assert isinstance(traced.inner_resolver, InvisiblePlaywrightRenderedPageResolver)
    assert traced.inner_resolver.seed == 123
    assert traced.inner_resolver.timezone == "America/Toronto"
    assert traced.inner_resolver.auto_prepare is False
    assert traced.inner_resolver.prepare_timeout_seconds == 45
    assert traced.inner_resolver.prepare_cooldown_seconds == 120
    adapter = ElutaAdapter()
    try:
        loader = adapter._shared_rendered_page_loader()
        assert isinstance(loader, InvisiblePlaywrightRenderedPageResolver)
        assert loader.user_data_dir == tmp_path / "firefox"
        assert loader.seed == 123
        assert loader.auto_prepare is False
        assert loader.prepare_timeout_seconds == 45
        assert loader.prepare_cooldown_seconds == 120
        assert loader.min_start_interval_seconds == 0
        assert loader is adapter._shared_rendered_page_loader()
    finally:
        adapter.close()

    chrome = default_rendered_page_resolver(settings.model_copy(update={
        "posting_rendered_fallback_provider": "chrome_cdp",
    }))
    assert chrome.provider == "chrome_cdp"


@pytest.mark.parametrize("method", ["resolve", "load_page"])
@pytest.mark.parametrize("outcome", ["success", "failure", "policy"])
def test_firefox_first_preserves_fallback_traces_and_policy(method, outcome):
    from unittest.mock import Mock
    from jobcockpit.ingestion.posting_discovery import (
        PostingResolution, ResolvedExternalPosting,
    )
    settings = Settings(posting_rendered_fallback_provider="invisible_playwright_first")
    resolver = default_rendered_page_resolver(settings)
    source_hint = hint("https://careers.example/jobs/platform-engineer", source_kind="official_candidate")
    posting = ResolvedExternalPosting(source_url=source_hint.source_url, raw_text=BODY, confidence=.9)
    success = PostingResolution(status="resolved", posting=posting)
    error = "playwright_failed: rendered_url_policy_blocked: private_network_blocked" if outcome == "policy" else "playwright_timeout"
    primary_result = success if outcome == "success" else PostingResolution(status="needs_review", error=error)
    primary = Mock(return_value=primary_result)
    fallback = Mock(return_value=success)
    setattr(resolver.primary_resolver, method, primary)
    setattr(resolver.fallback_resolver, method, fallback)
    result = getattr(resolver, method)(source_hint)
    primary.assert_called_once_with(source_hint)
    if outcome == "failure":
        fallback.assert_called_once_with(source_hint)
        assert result.posting == posting
        assert [a.provider for a in result.attempts] == ["invisible_playwright", "chrome_cdp"]
    else:
        fallback.assert_not_called()
        assert result.status == ("resolved" if outcome == "success" else "needs_review")
        assert [a.provider for a in result.attempts] == ["invisible_playwright"]


def test_firefox_first_is_wired_to_eluta_and_employer_link_adapters(monkeypatch):
    from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import configured_cdp_resolver
    from jobcockpit.ingestion.ats_adapters import csjobs
    from jobcockpit.ingestion.posting_discovery import ChromeCdpRenderedPageResolver
    from jobcockpit.ingestion.url_policy import UrlFetchPolicy
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "invisible_playwright_first")
    settings = Settings()
    policy = UrlFetchPolicy()
    for factory in (configured_cdp_resolver, csjobs.configured_cdp_resolver):
        resolver = factory(settings, policy)
        assert isinstance(resolver.primary_resolver, InvisiblePlaywrightRenderedPageResolver)
        assert isinstance(resolver.fallback_resolver, ChromeCdpRenderedPageResolver)
        assert resolver.primary_resolver.url_policy is policy
        assert resolver.fallback_resolver.url_policy is policy
    adapter = ElutaAdapter()
    try:
        loader = adapter._shared_rendered_page_loader()
        assert isinstance(loader.primary_resolver, InvisiblePlaywrightRenderedPageResolver)
        assert isinstance(loader.fallback_resolver, ChromeCdpRenderedPageResolver)
        assert loader.fallback_resolver.persistent_source_session
        assert loader is adapter._shared_rendered_page_loader()
    finally:
        adapter.close()


def test_missing_firefox_dependency_falls_back_to_chrome(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from jobcockpit.ingestion.posting_discovery import PostingResolution, ResolvedExternalPosting
    monkeypatch.setitem(sys.modules, "invisible_playwright", None)
    resolver = default_rendered_page_resolver(Settings(
        posting_rendered_fallback_provider="invisible_playwright_first",
        invisible_playwright_user_data_dir=tmp_path / "firefox",
        posting_rendered_fallback_min_interval_seconds=0,
        posting_rendered_fallback_retries=0,
    ))
    posting = ResolvedExternalPosting(source_url=SOURCE, raw_text=BODY, confidence=.9)
    resolver.fallback_resolver.resolve = Mock(return_value=PostingResolution(status="resolved", posting=posting))
    result = resolver.resolve(hint())
    assert result.status == "resolved"
    resolver.fallback_resolver.resolve.assert_called_once()
    assert [a.provider for a in result.attempts] == ["invisible_playwright", "chrome_cdp"]


def test_default_browser_entry_points_never_construct_chrome(monkeypatch):
    from jobcockpit.ingestion import posting_links
    from jobcockpit.ingestion.ats_adapters import canadian_tech_internships, csjobs, eluta
    from jobcockpit.ingestion.url_policy import UrlFetchPolicy

    monkeypatch.delenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", raising=False)
    settings = Settings(_env_file=None)
    assert settings.posting_rendered_fallback_provider == "invisible_playwright"

    def forbidden(*args, **kwargs):
        raise AssertionError("Default browser paths must never construct Chrome CDP")

    for module in (posting_links, canadian_tech_internships, eluta):
        monkeypatch.setattr(module, "ChromeCdpRenderedPageResolver", forbidden)
    for factory in (canadian_tech_internships.configured_cdp_resolver, csjobs.configured_cdp_resolver):
        resolver = factory(settings, UrlFetchPolicy())
        assert isinstance(resolver.inner_resolver, InvisiblePlaywrightRenderedPageResolver)
    for trusted_dom_only in (False, True):
        resolver = default_rendered_page_resolver(settings, trusted_dom_only=trusted_dom_only)
        assert isinstance(resolver.inner_resolver, InvisiblePlaywrightRenderedPageResolver)
        assert not hasattr(resolver, "fallback_resolver")
    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", "invisible_playwright")
    adapter = ElutaAdapter()
    try:
        assert isinstance(adapter._shared_rendered_page_loader(), InvisiblePlaywrightRenderedPageResolver)
    finally:
        adapter.close()


def test_firefox_only_failure_does_not_fall_back(monkeypatch):
    from unittest.mock import Mock
    from jobcockpit.ingestion.posting_discovery import PostingResolution

    resolver = default_rendered_page_resolver(Settings(posting_rendered_fallback_provider="invisible_playwright"))
    resolver.inner_resolver.resolve = Mock(return_value=PostingResolution(
        status="needs_review", error="playwright_timeout",
    ))
    result = resolver.resolve(hint())
    assert result.status == "needs_review"
    assert [attempt.provider for attempt in result.attempts] == ["invisible_playwright"]


@pytest.mark.parametrize("provider", ["disabled", "unknown", "codex_cli", "playwright_profile"])
def test_eluta_never_implicitly_selects_chrome(provider, monkeypatch):
    from jobcockpit.ingestion.ats_adapters.base import AtsProviderError

    monkeypatch.setenv("JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER", provider)
    adapter = ElutaAdapter()
    try:
        with pytest.raises(AtsProviderError, match="disabled or unsupported"):
            adapter._shared_rendered_page_loader()
    finally:
        adapter.close()
