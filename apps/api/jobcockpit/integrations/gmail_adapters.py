from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup

from jobcockpit.db.models import JobAlertSource
from jobcockpit.ingestion.posting_links import (
    canonicalize_indeed_job_url,
    canonicalize_linkedin_job_url,
    canonicalize_posting_url,
)
from jobcockpit.integrations.gmail import EmailLinkExtractor, ExtractedEmailLink


_DIGEST_FIELD_SEPARATOR = re.compile(r"\s*(?:\u00b7|•|\u2014|\ufffd)\s*")
_LINKEDIN_CARD_SEPARATOR = re.compile(r"\s*[\u00b7•]\s*")
_CANADIAN_PROVINCE_CODES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}
_COUNTRY_CODES = {"CA": "Canada", "CAN": "Canada"}


class GmailSourceAdapter(Protocol):
    def extract_links(self, message: dict) -> list[ExtractedEmailLink]:
        """Extract and normalize job-alert links for this Gmail source."""

    def raw_text_for_link(self, message: dict, link: ExtractedEmailLink) -> str:
        """Build the posting text passed to the shared job-link processor."""


@dataclass(frozen=True)
class MapleRadarDigestRow:
    title: str
    company: str
    location: str
    raw_row: str


@dataclass(frozen=True)
class LinkedInJobCard:
    title: str
    company: str
    location: str
    raw_card: str


@dataclass(frozen=True)
class IndeedJobCard:
    title: str
    company: str
    location: str
    raw_card: str


class GenericGmailAdapter:
    def __init__(self, extractor: EmailLinkExtractor | None = None) -> None:
        self.extractor = extractor or EmailLinkExtractor(href_normalizer=self.normalize_href)

    def extract_links(self, message: dict) -> list[ExtractedEmailLink]:
        return extract_message_links(self.extractor, message)

    def raw_text_for_link(self, message: dict, link: ExtractedEmailLink) -> str:
        return str(message.get("plain_text") or "")

    def normalize_href(self, href: str) -> str:
        return canonicalize_posting_url(href)


class MapleRadarGmailAdapter(GenericGmailAdapter):
    def raw_text_for_link(self, message: dict, link: ExtractedEmailLink) -> str:
        title = _clean_digest_field(str(link.link_text or ""))
        plain_text = str(message.get("plain_text") or "")
        row_raw_text = _raw_text_from_digest_row(str(link.surrounding_text or ""), title)
        if row_raw_text:
            return row_raw_text
        row_raw_text = _raw_text_from_unambiguous_plain_text_row(plain_text, title)
        if row_raw_text:
            return row_raw_text
        if title and not _looks_like_url(title) and _starts_with_digest_headline(plain_text):
            return title
        return plain_text


class IndeedGmailAdapter(GenericGmailAdapter):
    def extract_links(self, message: dict) -> list[ExtractedEmailLink]:
        cards_by_url = _indeed_cards_from_plain_text(str(message.get("plain_text") or ""))
        links_by_url: dict[str, ExtractedEmailLink] = {}
        position = 0
        html = str(message.get("html") or "")
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a"):
                canonical_url = canonicalize_indeed_job_url(str(anchor.get("href") or ""))
                if not canonical_url or canonical_url in links_by_url:
                    continue
                anchor_title = _clean_digest_field(anchor.get_text(" ", strip=True))
                card = cards_by_url.get(canonical_url) or IndeedJobCard(
                    title=anchor_title,
                    company="",
                    location="",
                    raw_card=anchor_title,
                )
                links_by_url[canonical_url] = ExtractedEmailLink(
                    href=canonical_url,
                    link_text=card.title or canonical_url,
                    surrounding_text=card.raw_card,
                    link_position=position,
                )
                position += 1

        for canonical_url, card in cards_by_url.items():
            if canonical_url in links_by_url:
                continue
            links_by_url[canonical_url] = ExtractedEmailLink(
                href=canonical_url,
                link_text=card.title or canonical_url,
                surrounding_text=card.raw_card,
                link_position=position,
            )
            position += 1
        return list(links_by_url.values())

    def raw_text_for_link(self, message: dict, link: ExtractedEmailLink) -> str:
        card = _indeed_cards_from_plain_text(str(message.get("plain_text") or "")).get(link.href)
        if card and card.title and card.company:
            fields = [
                card.title,
                f"Company: {card.company}",
                f"Location: {card.location}" if card.location else "",
                f"Email Card: {card.raw_card}" if card.raw_card else "",
            ]
            return "\n".join(field for field in fields if field)
        return str(message.get("plain_text") or message.get("subject") or link.link_text or "")


class LinkedInGmailAdapter(GenericGmailAdapter):
    def extract_links(self, message: dict) -> list[ExtractedEmailLink]:
        candidates_by_url: dict[str, tuple[int, ExtractedEmailLink]] = {}
        position = 0
        html = str(message.get("html") or "")
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a"):
                raw_href = str(anchor.get("href") or "")
                canonical_url = canonicalize_linkedin_job_url(raw_href)
                if not canonical_url:
                    continue
                raw_card = _linkedin_anchor_text(anchor)
                card = _parse_linkedin_job_card(raw_card)
                link = ExtractedEmailLink(
                    href=canonical_url,
                    link_text=card.title or _clean_digest_field(raw_card) or canonical_url,
                    surrounding_text=card.raw_card,
                    link_position=position,
                )
                position += 1
                score = _linkedin_card_score(card)
                existing = candidates_by_url.get(canonical_url)
                if existing is None or score > existing[0]:
                    candidates_by_url[canonical_url] = (score, link)

        plain_text = str(message.get("plain_text") or "")
        if plain_text:
            for extracted in EmailLinkExtractor().extract(plain_text):
                canonical_url = canonicalize_linkedin_job_url(extracted.href)
                if not canonical_url or canonical_url in candidates_by_url:
                    continue
                link = ExtractedEmailLink(
                    href=canonical_url,
                    link_text=extracted.link_text,
                    surrounding_text=extracted.surrounding_text,
                    link_position=position,
                )
                position += 1
                candidates_by_url[canonical_url] = (0, link)

        return [entry[1] for entry in candidates_by_url.values()]

    def raw_text_for_link(self, message: dict, link: ExtractedEmailLink) -> str:
        card = _parse_linkedin_job_card(str(link.surrounding_text or ""))
        if card.title and card.company:
            display_card = " | ".join(card.raw_card.splitlines())
            fields = [
                card.title,
                f"Company: {card.company}",
                f"Location: {card.location}" if card.location else "",
                f"Email Card: {display_card}" if display_card else "",
            ]
            return "\n".join(field for field in fields if field)
        return str(message.get("plain_text") or message.get("subject") or link.link_text or "")


def gmail_adapter_for_source(source: JobAlertSource) -> GmailSourceAdapter:
    if _is_mapleradar_source(source):
        return MapleRadarGmailAdapter()
    if _is_indeed_source(source):
        return IndeedGmailAdapter()
    if _is_linkedin_source(source):
        return LinkedInGmailAdapter()
    return GenericGmailAdapter()


def extract_message_links(extractor: EmailLinkExtractor, message: dict) -> list[ExtractedEmailLink]:
    links = []
    seen_hrefs = set()
    for body in (message.get("html", ""), message.get("plain_text", "")):
        if not body:
            continue
        for link in extractor.extract(body):
            if link.href in seen_hrefs:
                continue
            seen_hrefs.add(link.href)
            links.append(link)
    return links


def _is_mapleradar_source(source: JobAlertSource) -> bool:
    query = str(source.gmail_query or "").casefold()
    display_name = str(source.display_name or "").casefold()
    sender_allowlist = {str(sender).casefold() for sender in source.sender_allowlist}
    return (
        "mapleradar" in display_name
        or "mapleradar.com" in query
        or "jobs@mapleradar.com" in query
        or "mapleradar.com" in sender_allowlist
    )


def _is_indeed_source(source: JobAlertSource) -> bool:
    query = str(source.gmail_query or "").casefold()
    display_name = str(source.display_name or "").casefold()
    sender_allowlist = {str(sender).casefold() for sender in source.sender_allowlist}
    return (
        "indeed" in display_name
        or "jobalert.indeed.com" in query
        or any(sender == "indeed.com" or sender.endswith(".indeed.com") for sender in sender_allowlist)
    )


def _is_linkedin_source(source: JobAlertSource) -> bool:
    query = str(source.gmail_query or "").casefold()
    display_name = str(source.display_name or "").casefold()
    sender_allowlist = {str(sender).casefold() for sender in source.sender_allowlist}
    return (
        "linkedin" in display_name
        or "linkedin.com" in query
        or any(sender == "linkedin.com" or sender.endswith(".linkedin.com") for sender in sender_allowlist)
    )


def _indeed_cards_from_plain_text(plain_text: str) -> dict[str, IndeedJobCard]:
    lines = [line for line in (_clean_digest_field(value) for value in plain_text.splitlines()) if line]
    cards: dict[str, IndeedJobCard] = {}
    for index, line in enumerate(lines):
        canonical_url = canonicalize_indeed_job_url(line)
        if not canonical_url:
            continue
        card = _indeed_card_before_url(lines, index)
        if card:
            cards[canonical_url] = card
    return cards


def _indeed_card_before_url(lines: list[str], url_index: int) -> IndeedJobCard | None:
    for company_index in range(url_index - 1, max(-1, url_index - 8), -1):
        company_location = _split_indeed_company_location(lines[company_index])
        if not company_location or company_index == 0:
            continue
        company, location = company_location
        title = lines[company_index - 1]
        if _looks_like_url(title):
            continue
        raw_card = f"{title} | {company} - {location}"
        return IndeedJobCard(title=title, company=company, location=location, raw_card=raw_card)
    return None


def _split_indeed_company_location(value: str) -> tuple[str, str] | None:
    if " - " not in value:
        return None
    company, location = (_clean_digest_field(part) for part in value.rsplit(" - ", maxsplit=1))
    if not company or not location:
        return None
    if "," not in location and not location.casefold().startswith("remote"):
        return None
    return company, location


def _linkedin_anchor_text(anchor) -> str:
    return "\n".join(
        line
        for line in (_clean_digest_field(part) for part in anchor.get_text("\n", strip=True).splitlines())
        if line
    )


def _parse_linkedin_job_card(value: str) -> LinkedInJobCard:
    lines = [
        line
        for line in (_clean_digest_field(part) for part in str(value or "").splitlines())
        if line
    ]
    title = ""
    company = ""
    location = ""
    for index, line in enumerate(lines):
        parts = [_clean_digest_field(part) for part in _LINKEDIN_CARD_SEPARATOR.split(line, maxsplit=1)]
        if len(parts) != 2 or not all(parts):
            continue
        company, location = parts
        if index > 0:
            title_lines = lines[:index]
            if title_lines and _text_match_key(title_lines[0]) == _text_match_key(company):
                title_lines = title_lines[1:]
            title = _clean_digest_field(" ".join(title_lines))
        break
    return LinkedInJobCard(
        title=title,
        company=company,
        location=location,
        raw_card="\n".join(lines),
    )


def _linkedin_card_score(card: LinkedInJobCard) -> int:
    return (
        (100 if card.title else 0)
        + (50 if card.company else 0)
        + (25 if card.location else 0)
        + min(len(card.raw_card), 24)
    )


def _plain_text_rows_for_title(plain_text: str, title: str) -> list[str]:
    if not title:
        return []
    title_key = title.casefold()
    return [line for line in plain_text.splitlines() if title_key in line.casefold()]


def _raw_text_from_unambiguous_plain_text_row(plain_text: str, title: str) -> str:
    rows = _parseable_plain_text_rows_for_title(plain_text, title)
    if len(rows) != 1:
        return ""
    return _raw_text_from_digest_row(rows[0], title)


def _parseable_plain_text_rows_for_title(plain_text: str, title: str) -> list[str]:
    rows = []
    seen = set()
    for row_text in _plain_text_rows_for_title(plain_text, title):
        key = _clean_digest_field(row_text).casefold()
        if not key or key in seen:
            continue
        if not _parse_mapleradar_digest_row(row_text, title):
            continue
        seen.add(key)
        rows.append(row_text)
    return rows


def _raw_text_from_digest_row(row_text: str, title: str) -> str:
    parsed = _parse_mapleradar_digest_row(row_text, title)
    if not parsed:
        return ""
    fields = [
        parsed.title,
        f"Company: {parsed.company}",
        f"Location: {parsed.location}" if parsed.location else "",
        f"Email Row: {parsed.raw_row}",
    ]
    return "\n".join(field for field in fields if field)


def _parse_mapleradar_digest_row(row_text: str, title: str) -> MapleRadarDigestRow | None:
    row = _clean_digest_field(row_text)
    title = _clean_digest_field(title)
    if not row:
        return None
    if title and _text_match_key(title) not in _text_match_key(row):
        return None

    parts = [_clean_digest_field(part) for part in _DIGEST_FIELD_SEPARATOR.split(row)]
    if parts and not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    if len(parts) < 2:
        return None

    location = _normalize_mapleradar_location(parts[-1]) if len(parts) >= 3 else ""

    title_parts = parts[:-2] if location else parts[:-1]
    title_from_row = _clean_digest_field(" \u2014 ".join(part for part in title_parts if part))
    company = (parts[-2] if location else parts[-1]) or "Unknown"
    return MapleRadarDigestRow(
        title=title_from_row or title,
        company=company,
        location=location,
        raw_row=_trim_digest_separators(row),
    )


def _trim_digest_separators(value: str) -> str:
    return _clean_digest_field(value).strip(" \u00b7•\u2014\ufffd")

def _normalize_mapleradar_location(value: str) -> str:
    parts = [_clean_digest_field(part) for part in value.split(",")]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        parts[1] = _CANADIAN_PROVINCE_CODES.get(parts[1].upper(), parts[1])
    if len(parts) >= 3:
        parts[2] = _COUNTRY_CODES.get(parts[2].upper(), parts[2])
    return ", ".join(parts)


def _clean_digest_field(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()


def _text_match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _looks_like_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _starts_with_digest_headline(plain_text: str) -> bool:
    first_line = _clean_digest_field(plain_text.splitlines()[0] if plain_text.splitlines() else "")
    return "daily digest" in first_line.casefold()
