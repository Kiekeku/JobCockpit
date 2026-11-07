from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

AGGREGATOR_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
}


def clean_aggregator_posting_text(raw_text: str, *, source_url: str | None = None) -> str:
    raw_text = _clean_embedded_html_posting_text(raw_text)
    source_kind = aggregator_source_kind(source_url or "")
    if source_kind == "linkedin" or _looks_like_linkedin_shell(raw_text):
        return _clean_linkedin_posting_text(raw_text)
    return raw_text.strip()


def _looks_like_linkedin_shell(raw_text: str) -> bool:
    marker = re.search(r"\bUse AI to assess how you fit\b", raw_text, re.IGNORECASE)
    about_job = re.search(r"\bAbout the job\b", raw_text, re.IGNORECASE)
    return bool(marker is not None and about_job is not None and marker.end() < about_job.start())


_EMBEDDED_HTML_BLOCK_PATTERN = re.compile(
    r"</?(?:article|blockquote|br|div|h[1-6]|li|ol|p|section|table|td|th|tr|ul)\b",
    re.IGNORECASE,
)
_EMBEDDED_HTML_BLOCK_TAGS = (
    "article",
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ol",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
)


def _clean_embedded_html_posting_text(raw_text: str) -> str:
    if _EMBEDDED_HTML_BLOCK_PATTERN.search(raw_text) is None:
        return raw_text

    soup = BeautifulSoup(raw_text, "html.parser")
    for line_break in soup.find_all("br"):
        line_break.replace_with("\n")
    for block in soup.find_all(_EMBEDDED_HTML_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")

    lines = [" ".join(line.split()) for line in soup.get_text("", strip=False).splitlines()]
    return "\n".join(line for line in lines if line)


def aggregator_source_kind(source_url: str) -> str:
    hostname = (urlparse(source_url).hostname or "").casefold()
    for source_kind, host_suffixes in AGGREGATOR_HOST_SUFFIXES.items():
        if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in host_suffixes):
            return source_kind
    return ""


def _clean_linkedin_posting_text(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if not cleaned:
        return ""

    shell_marker = re.search(
        r"\bUse AI to assess how you fit\b",
        cleaned,
        re.IGNORECASE,
    )
    about_job_matches = list(re.finditer(r"\bAbout the job\b", cleaned, re.IGNORECASE))
    if shell_marker is not None:
        actual_body = next(
            (match for match in about_job_matches if match.start() > shell_marker.end()),
            None,
        )
        if actual_body is not None:
            cleaned = cleaned[actual_body.end() :].lstrip(" :-\r\n")

    cutoff_patterns = [
        r"\bSet alert for similar jobs\b",
        r"\bAbout the company\b",
        r"\bInterested in working with us in the future\?",
        r"\bCompany photos\b",
        r"\bMore jobs\b",
        r"\bSee more jobs like this\b",
        r"\bCan['\u2019]t find top talent\?",
        r"\bAbout Accessibility Talent Solutions\b",
        r"\bLinkedIn Corporation\s+\u00a9\b",
        r"\bSelect language\b",
    ]
    cutoff = len(cleaned)
    for pattern in cutoff_patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            cutoff = min(cutoff, match.start())

    return cleaned[:cutoff].strip()
