from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.pinned_browser_proxy import PinnedBrowserProxy
from jobcockpit.ingestion.posting_discovery import _PlaywrightUrlPolicyGuard
from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy
from test_posting_discovery import FakeRoute


SOURCE = "https://20.12.34.56/job"
PUBLIC_HOP = "https://20.12.34.57/redirect"


def request(url, *, navigation=True):
    return SimpleNamespace(
        url=url, headers={"sec-fetch-dest": "document" if navigation else "script"},
        resource_type="other", is_navigation_request=lambda: False,
        frame=SimpleNamespace(parent_frame=None),
    )


def test_public_navigation_hops_are_allowed_before_final_source_validation():
    guard = _PlaywrightUrlPolicyGuard(source_url=SOURCE, url_policy=ScheduledRenderedUrlPolicy())
    for url in (SOURCE, PUBLIC_HOP, SOURCE + "/final"):
        route = FakeRoute()
        guard._handle_request(route, request(url))
        assert route.continued
        assert not route.aborted
    guard.ensure_final_url_allowed(SOURCE + "/final")
    with pytest.raises(RuntimeError, match="rendered_cross_origin_blocked"):
        guard.ensure_final_url_allowed(PUBLIC_HOP)


def test_public_connection_does_not_need_a_document_or_navigation_callback():
    connected = []
    stream = object()

    def connect(host, port, timeout):
        connected.append((host, port))
        return stream

    proxy = PinnedBrowserProxy(source_url=SOURCE, url_policy=ScheduledRenderedUrlPolicy(), connector=connect)
    try:
        assert proxy._connect_upstream(PUBLIC_HOP) is stream
        assert connected == [("20.12.34.57", 443)]
        proxy.raise_if_blocked()
    finally:
        proxy.close()


@pytest.mark.parametrize("navigation", [True, False])
def test_private_requests_remain_blocked(navigation):
    guard = _PlaywrightUrlPolicyGuard(source_url=SOURCE, url_policy=ScheduledRenderedUrlPolicy())
    route = FakeRoute()
    guard._handle_request(route, request("http://10.0.0.1/private", navigation=navigation))
    assert route.aborted
    assert not route.continued
    if navigation:
        with pytest.raises(RuntimeError, match="private_network_blocked"):
            guard.raise_if_blocked()
    else:
        guard.raise_if_blocked()
        assert "private_network_blocked" in guard.blocked_subresource_diagnostic()


@pytest.mark.parametrize("engine", ["profile", "cdp", "firefox"])
def test_final_page_is_checked_again_after_capture(tmp_path, engine):
    from jobcockpit.ingestion.posting_discovery import (
        ChromeCdpRenderedPageResolver, ExternalPostingHint, PlaywrightProfileRenderedPageResolver,
    )
    from test_invisible_browser import browser_resolver
    from test_posting_discovery import FakePage, FakePlaywright, FakePlaywrightContextManager

    page = FakePage(SOURCE, "Platform Engineer - Acme", "Platform Engineer\nCompany: Acme\nBuild services.")
    if engine == "firefox":
        resolver, _, _ = browser_resolver(tmp_path, page)
    else:
        kwargs = {"playwright_factory": lambda: FakePlaywrightContextManager(FakePlaywright(page)),
                  "auth_wall_wait_seconds": 0}
        resolver = (
            ChromeCdpRenderedPageResolver(endpoint_url="http://127.0.0.1:9222", **kwargs)
            if engine == "cdp" else
            PlaywrightProfileRenderedPageResolver(user_data_dir=tmp_path / "profile", **kwargs)
        )
    resolver.capture_html = True
    resolver.url_policy = ScheduledRenderedUrlPolicy()

    def redirected_content():
        # A public navigation during capture must not inherit the previous URL's approval.
        page.url = PUBLIC_HOP
        return "<body>Unapproved destination</body>"

    page.content = redirected_content
    result = resolver.load_page(ExternalPostingHint(source_url=SOURCE, source_kind="generic"))
    assert result.posting is None
    assert "rendered_cross_origin_blocked" in result.error
    assert page.closed
