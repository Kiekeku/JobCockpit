from __future__ import annotations

import re
from urllib.parse import urlparse

from jobcockpit.ingestion.browser_capture_adapters.base import (
    BrowserCaptureContext,
    BrowserCaptureExtractedFields,
)
from jobcockpit.ingestion.url_hosts import hostname_matches_indeed

CANADIAN_PROVINCE_CODES = {
    "AB",
    "BC",
    "MB",
    "NB",
    "NL",
    "NS",
    "NT",
    "NU",
    "ON",
    "PE",
    "QC",
    "SK",
    "YT",
}
CANADIAN_PROVINCE_NAMES = {
    "alberta",
    "british columbia",
    "manitoba",
    "new brunswick",
    "newfoundland and labrador",
    "nova scotia",
    "northwest territories",
    "nunavut",
    "ontario",
    "prince edward island",
    "quebec",
    "saskatchewan",
    "yukon",
}
FIELD_BOUNDARY_PATTERN = (
    "Page Title|Source URL|Source Evidence|Email Row|Title|Job Title|Role|Position|Company|Location|"
    "Apply URL|Indeed Source URL|Resolution Evidence"
)


class IndeedBrowserCaptureAdapter:
    provider = "indeed"

    def matches(self, context: BrowserCaptureContext) -> bool:
        page_title = _page_title(context.lines) or ""
        return _is_indeed_source(context.source_url, page_title)

    def extract(self, context: BrowserCaptureContext) -> BrowserCaptureExtractedFields:
        page_title = _page_title(context.lines) or ""
        if not self.matches(context):
            return BrowserCaptureExtractedFields()
        if _is_blocked_page_text(page_title) or any(
            _is_blocked_page_text(line) for line in context.lines
        ):
            return BrowserCaptureExtractedFields()

        page_title_role, page_title_location = _indeed_page_title_parts(page_title)
        if not page_title_role:
            return BrowserCaptureExtractedFields(location_label=page_title_location)

        title_index = _indeed_body_title_index(context.lines, page_title_role)
        if title_index is None:
            return BrowserCaptureExtractedFields(location_label=page_title_location)

        title = _clean_title_value(_dedupe_repeated_phrase(context.lines[title_index]))
        company = _clean_company_name(_next_indeed_company_line(context.lines, title_index))
        return BrowserCaptureExtractedFields(
            title=title, company=company, location_label=page_title_location
        )


def _page_title(lines: list[str]) -> str | None:
    return _field_value(lines, "Page Title")


def _is_indeed_source(source_url: str | None, page_title: str) -> bool:
    parsed_host = urlparse(source_url or "").hostname or ""
    return bool(
        hostname_matches_indeed(parsed_host)
        or re.search(r"\bIndeed(?:\.com)?\b", page_title, flags=re.IGNORECASE)
    )


def _indeed_page_title_parts(page_title: str) -> tuple[str | None, str | None]:
    cleaned = re.sub(r"\s+-\s+Indeed(?:\.com)?\s*$", "", page_title.strip(), flags=re.IGNORECASE)
    cleaned = _clean_field_value(cleaned)
    if not cleaned:
        return None, None
    parts = [part.strip() for part in re.split(r"\s+-\s+", cleaned) if part.strip()]
    if len(parts) >= 2 and _looks_like_indeed_location(parts[-1]):
        return _clean_field_value(" - ".join(parts[:-1])), _clean_field_value(parts[-1])
    return cleaned, None


def _indeed_body_title_index(lines: list[str], page_title_role: str) -> int | None:
    normalized_role = _clean_field_value(page_title_role).casefold()
    for index, line in enumerate(lines):
        if _field_label(line):
            continue
        if _clean_field_value(line).casefold() == normalized_role:
            return index
    return None


def _next_indeed_company_line(lines: list[str], title_index: int) -> str | None:
    for line in lines[title_index + 1 : title_index + 6]:
        if _is_indeed_company_candidate(line):
            return line
    return None


def _is_indeed_company_candidate(line: str) -> bool:
    candidate = _clean_field_value(line)
    if not candidate or len(candidate) > 120:
        return False
    lower = candidate.casefold()
    if lower in {
        "full job description",
        "job details",
        "location",
        "benefits",
        "pay",
        "job type",
        "report job",
    }:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", candidate):
        return False
    if "out of 5 stars" in lower:
        return False
    if candidate.startswith("$") or re.search(r"\b(?:an hour|per hour|a year|per year)\b", lower):
        return False
    return not _looks_like_indeed_location(candidate)


def _looks_like_indeed_location(value: str) -> bool:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) < 2:
        return False
    return bool(
        re.search(r"\b[A-Z]{2}\b", value)
        or _province_name(parts[1])
        or _country_name(parts[-1], allow_ambiguous_code=True)
    )


def _province_name(value: str) -> str | None:
    cleaned = value.strip().casefold()
    upper = value.strip().upper()
    if upper in CANADIAN_PROVINCE_CODES:
        return upper
    if cleaned in CANADIAN_PROVINCE_NAMES:
        return value.strip()
    return None


def _country_name(value: str, *, allow_ambiguous_code: bool = False) -> str | None:
    cleaned = value.strip().casefold().strip(".")
    if cleaned in {"canada", "united states", "usa"}:
        return value.strip()
    if allow_ambiguous_code and cleaned in {"ca", "us"}:
        return value.strip().upper()
    return None


def _clean_title_value(value: str) -> str:
    cleaned = value
    while True:
        stripped = re.sub(
            r"^(?:Title|Job Title|Role|Position):\s*", "", cleaned, flags=re.IGNORECASE
        )
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def _clean_company_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _clean_field_value(value)
    for prefix in ("LinkedIn", "Indeed"):
        if cleaned.casefold().startswith(f"{prefix.casefold()} ") and len(cleaned.split()) > 1:
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned or None


def _dedupe_repeated_phrase(value: str) -> str:
    tokens = value.split()
    if len(tokens) >= 2 and len(tokens) % 2 == 0:
        halfway = len(tokens) // 2
        if tokens[:halfway] == tokens[halfway:]:
            return " ".join(tokens[:halfway])
    return value


def _is_blocked_page_text(value: str) -> bool:
    lower = value.casefold()
    return any(
        marker in lower
        for marker in (
            "blocked - indeed.com",
            "request blocked",
            "you have been blocked",
            "troubleshooting cloudflare errors",
        )
    )


def _field_value(lines: list[str], *labels: str) -> str | None:
    for line in lines:
        line_label = _field_label(line)
        for label in labels:
            label_key = label.casefold()
            if (
                line_label
                and line_label != label_key
                and not re.search(rf"\s{re.escape(label)}:", line, flags=re.IGNORECASE)
            ):
                continue
            match = re.search(
                rf"(?:^|\s){re.escape(label)}:\s*(?P<value>.*?)(?=\s+(?:{FIELD_BOUNDARY_PATTERN}):|$)",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                value = _clean_field_value(match.group("value"))
                if value:
                    return value
    return None


def _field_label(line: str) -> str:
    if ":" not in line:
        return ""
    label = line.split(":", 1)[0].strip().casefold()
    return label if re.fullmatch(r"[a-z][a-z\s_-]{0,40}", label) else ""


def _clean_field_value(value: str) -> str:
    return " ".join(value.strip(" -|").split())
