from __future__ import annotations

from contextlib import contextmanager

import httpx

from jobcockpit.ingestion.url_policy import UrlPolicyResult
from jobcockpit.integrations.public_web import PublicPageFetcher


class AllowPublicPolicy:
    def check(self, url: str) -> UrlPolicyResult:
        if "127.0.0.1" in url:
            return UrlPolicyResult(False, "private_network_blocked")
        return UrlPolicyResult(True, "public_hostname")


class FakeStreamingClient:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    @contextmanager
    def stream(self, _method: str, url: str, **_kwargs):
        self.requests.append(url)
        yield self.responses[url]

    def close(self) -> None:
        return None


def _response(
    status_code: int,
    body: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body,
        headers=headers,
        request=httpx.Request("GET", "https://www.example.test/"),
    )


def test_public_page_fetcher_validates_redirects_and_extracts_visible_html() -> None:
    client = FakeStreamingClient(
        {
            "https://example.test/about": _response(
                302,
                headers={"Location": "https://www.example.test/about-us"},
            ),
            "https://www.example.test/about-us": _response(
                200,
                b"<html><head><title>Example About</title><script>ignore()</script></head>"
                b"<body><h1>Example Systems</h1><p>Builds industrial software.</p></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        }
    )
    fetcher = PublicPageFetcher(
        http_client=client,
        url_policy=AllowPublicPolicy(),
    )

    page = fetcher.fetch("https://example.test/about")

    assert page is not None
    assert page.url == "https://www.example.test/about-us"
    assert page.title == "Example About"
    assert "Example Systems" in page.text
    assert "Builds industrial software." in page.text
    assert "ignore()" not in page.text
    assert client.requests == [
        "https://example.test/about",
        "https://www.example.test/about-us",
    ]


def test_public_page_fetcher_blocks_private_redirect_before_request() -> None:
    client = FakeStreamingClient(
        {
            "https://example.test/about": _response(
                302,
                headers={"Location": "http://127.0.0.1/private"},
            )
        }
    )
    fetcher = PublicPageFetcher(http_client=client, url_policy=AllowPublicPolicy())

    assert fetcher.fetch("https://example.test/about") is None
    assert fetcher.last_error == "public_page_private_network_blocked"
    assert client.requests == ["https://example.test/about"]


def test_public_page_fetcher_rejects_oversized_or_unsupported_content() -> None:
    oversized_client = FakeStreamingClient(
        {
            "https://example.test/about": _response(
                200,
                b"too large",
                headers={"Content-Type": "text/html", "Content-Length": "1001"},
            )
        }
    )
    oversized = PublicPageFetcher(
        http_client=oversized_client,
        url_policy=AllowPublicPolicy(),
        max_response_bytes=1000,
    )
    unsupported_client = FakeStreamingClient(
        {
            "https://example.test/report": _response(
                200,
                b"%PDF",
                headers={"Content-Type": "application/pdf"},
            )
        }
    )
    unsupported = PublicPageFetcher(
        http_client=unsupported_client,
        url_policy=AllowPublicPolicy(),
    )

    assert oversized.fetch("https://example.test/about") is None
    assert oversized.last_error == "public_page_too_large"
    assert unsupported.fetch("https://example.test/report") is None
    assert unsupported.last_error == "public_page_unsupported_content_type"


def test_public_page_fetcher_rejects_stream_that_exceeds_limit() -> None:
    client = FakeStreamingClient(
        {
            "https://example.test/about": _response(
                200,
                b"x" * 1001,
                headers={"Content-Type": "text/plain"},
            )
        }
    )
    fetcher = PublicPageFetcher(
        http_client=client,
        url_policy=AllowPublicPolicy(),
        max_response_bytes=1000,
    )

    assert fetcher.fetch("https://example.test/about") is None
    assert fetcher.last_error == "public_page_too_large"
