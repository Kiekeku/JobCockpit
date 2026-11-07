from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from jobcockpit.ingestion.ats_adapters.base import AtsProviderError, SourceArchive
from jobcockpit.ingestion.url_policy import UrlFetchPolicy

ARCHIVED_HEADER_NAMES = {"etag", "last-modified", "content-language"}
DEFAULT_USER_AGENT = "Mozilla/5.0 JobCockpit/0.1"


def request_headers(accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "User-Agent": DEFAULT_USER_AGENT,
    }


def archived_headers(headers: httpx.Headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in ARCHIVED_HEADER_NAMES}


def source_archive_from_response(
    source_url: str,
    response: httpx.Response,
    *,
    final_url: str | None = None,
    default_content_type: str = "application/json",
) -> SourceArchive:
    return SourceArchive(
        source_url=source_url,
        final_url=final_url or str(response.url),
        status_code=response.status_code,
        content_type=response.headers.get("content-type", default_content_type),
        body=response.content,
        headers=archived_headers(response.headers),
    )


def enforce_url_policy(policy: UrlFetchPolicy, url: str, *, label: str) -> None:
    result = policy.check(url)
    if not result.allowed:
        raise AtsProviderError(400, f"{label} URL is blocked by policy: {result.reason}")


def clean_text(text: str) -> str:
    return " ".join(str(text or "").split())


def html_to_text(html: str, *, separator: str = "\n", normalize: bool = False) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(separator, strip=True)
    return clean_text(text) if normalize else text


def select_text(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    if not element:
        return ""
    return clean_text(element.get_text(" ", strip=True))


def posting_html_text(html: str) -> str:
    """Preserve block boundaries while keeping inline emphasis in its sentence."""
    from jobcockpit.ingestion.posting_text_cleaning import _clean_embedded_html_posting_text

    return _clean_embedded_html_posting_text(f'<div>{html}</div>')


def select_posting_text(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    return posting_html_text(str(element)) if element is not None else ""


def first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def line(label: str, value: object | None) -> str:
    text = str(value or "").strip()
    return f"{label}: {text}" if text else ""
