from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobcockpit.ingestion.pinned_http import PinnedHttpClient
from jobcockpit.ingestion.url_policy import UrlFetchPolicy


_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_ALLOWED_CONTENT_TYPES = {"text/html", "text/plain"}


class StreamingHttpClient(Protocol):
    def stream(self, method: str, url: str, **kwargs: Any): ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PublicPageEvidence:
    url: str
    title: str
    text: str


class PublicPageFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_redirects: int = 5,
        max_response_bytes: int = 1_000_000,
        max_text_chars: int = 12_000,
        url_policy: UrlFetchPolicy | None = None,
        http_client: StreamingHttpClient | None = None,
    ) -> None:
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.max_text_chars = max_text_chars
        self.url_policy = url_policy or UrlFetchPolicy()
        self.http_client = http_client or PinnedHttpClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "Accept": "text/html,text/plain;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": "JobCockpit-Company-Research/1.0",
            },
        )
        self._owns_client = http_client is None
        self.last_error = ""

    def fetch(self, source_url: str) -> PublicPageEvidence | None:
        self.last_error = ""
        current_url = source_url.strip()
        try:
            for redirect_count in range(self.max_redirects + 1):
                policy_result = self.url_policy.check(current_url)
                if not policy_result.allowed:
                    self.last_error = f"public_page_{policy_result.reason}"
                    return None
                with self.http_client.stream(
                    "GET",
                    current_url,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    status_code = int(response.status_code)
                    if status_code in _REDIRECT_STATUS_CODES:
                        location = str(response.headers.get("Location") or "").strip()
                        if not location:
                            self.last_error = "public_page_redirect_missing_location"
                            return None
                        if redirect_count >= self.max_redirects:
                            self.last_error = "public_page_redirect_limit"
                            return None
                        current_url = urljoin(current_url, location)
                        continue
                    if not 200 <= status_code < 300:
                        self.last_error = "public_page_http_error"
                        return None
                    if str(
                        response.headers.get("Content-Encoding") or ""
                    ).strip().casefold() not in {
                        "",
                        "identity",
                    }:
                        self.last_error = "public_page_unsupported_encoding"
                        return None
                    content_type = (
                        str(response.headers.get("Content-Type") or "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if content_type not in _ALLOWED_CONTENT_TYPES:
                        self.last_error = "public_page_unsupported_content_type"
                        return None
                    declared_length = _content_length(response.headers.get("Content-Length"))
                    if declared_length is not None and declared_length > self.max_response_bytes:
                        self.last_error = "public_page_too_large"
                        return None
                    body = _read_bounded(response, self.max_response_bytes)
                    if body is None:
                        self.last_error = "public_page_too_large"
                        return None
                    text, title = _page_text(body, content_type, response)
                    if not text:
                        self.last_error = "public_page_empty"
                        return None
                    return PublicPageEvidence(
                        url=current_url,
                        title=title[:500],
                        text=text[: self.max_text_chars],
                    )
            self.last_error = "public_page_redirect_limit"
            return None
        except (httpx.HTTPError, OSError, ValueError):
            self.last_error = "public_page_request_failed"
            return None

    def close(self) -> None:
        if self._owns_client:
            with suppress(Exception):
                self.http_client.close()


def _content_length(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _read_bounded(response: Any, max_bytes: int) -> bytes | None:
    body = bytearray()
    if bool(getattr(response, "is_stream_consumed", False)):
        content = bytes(response.content)
        return content if len(content) <= max_bytes else None
    for chunk in response.iter_raw():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


def _page_text(body: bytes, content_type: str, response: Any) -> tuple[str, str]:
    encoding = str(getattr(response, "encoding", None) or "utf-8")
    value = body.decode(encoding, errors="replace")
    if content_type == "text/plain":
        return _clean_text(value), ""
    soup = BeautifulSoup(value, "html.parser")
    for element in soup.select("script, style, noscript, template, svg"):
        element.decompose()
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    return _clean_text(soup.get_text("\n", strip=True)), title


def _clean_text(value: str) -> str:
    return "\n".join(
        line for line in (" ".join(part.split()) for part in value.splitlines()) if line
    )
