import asyncio
from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.posting_discovery import (
    _is_top_level_navigation_request,
    _PlaywrightUrlPolicyGuard,
)
from jobcockpit.ingestion.url_policy import UrlPolicyResult


@pytest.mark.parametrize(
    ("headers", "resource_type", "expected"),
    [
        ({"sec-fetch-dest": "document"}, "other", True),
        ({"accept": "text/html"}, "other", True),
        ({"sec-fetch-dest": "script"}, "script", False),
    ],
)
def test_navigation_classification_does_not_wait_for_browser_headers(
    headers, resource_type, expected,
):
    def cancelled_headers():
        raise asyncio.CancelledError()

    request = SimpleNamespace(
        headers=headers, all_headers=cancelled_headers,
        resource_type=resource_type, is_navigation_request=lambda: False,
        frame=SimpleNamespace(parent_frame=None),
    )
    assert _is_top_level_navigation_request(request) is expected


@pytest.mark.parametrize(
    "message",
    [
        "Route.continue_: Network.resumeInterceptedRequest: NS_BINDING_ABORTED",
        "Route.continue_: Target page, context or browser has been closed",
        "unexpected protocol failure",
    ],
)
def test_route_continuation_only_ignores_cancelled_or_closed_requests(message):
    guard = _PlaywrightUrlPolicyGuard(
        source_url="https://careers.example.com/job",
        url_policy=SimpleNamespace(check=lambda url: UrlPolicyResult(True, "allowed")),
    )
    calls = []

    def continue_request():
        calls.append("continue")
        raise RuntimeError(message)

    route = SimpleNamespace(continue_=continue_request)
    request = SimpleNamespace(
        url="https://careers.example.com/job", headers={},
        resource_type="document", is_navigation_request=lambda: True,
        frame=SimpleNamespace(parent_frame=None),
    )
    if message == "unexpected protocol failure":
        with pytest.raises(RuntimeError, match=message):
            guard._handle_request(route, request)
    else:
        guard._handle_request(route, request)
        guard.raise_if_blocked()
    assert calls == ["continue"]


def test_cancelled_header_lookup_does_not_bypass_navigation_policy():
    def cancelled_headers():
        raise asyncio.CancelledError()

    guard = _PlaywrightUrlPolicyGuard(
        source_url="https://careers.example.com/job",
        url_policy=SimpleNamespace(check=lambda url: UrlPolicyResult(False, "private_ip")),
    )
    calls = []
    route = SimpleNamespace(
        continue_=lambda: calls.append("continue"),
        abort=lambda reason: calls.append(reason),
    )
    request = SimpleNamespace(
        url="https://careers.example.com/job",
        headers={"sec-fetch-dest": "document"}, all_headers=cancelled_headers,
        resource_type="other", is_navigation_request=lambda: False,
        frame=SimpleNamespace(parent_frame=None),
    )
    guard._handle_request(route, request)
    assert calls == ["blockedbyclient"]
    with pytest.raises(RuntimeError, match="private_ip"):
        guard.raise_if_blocked()
