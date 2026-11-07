from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from bs4 import BeautifulSoup
from pydantic import BaseModel

from jobcockpit.ingestion.posting_links import classify_posting_link


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


SYNTHETIC_GMAIL_MESSAGES = [
    {
        "id": "synthetic-message-001",
        "id_hash": _hash("synthetic-message-001"),
        "thread_hash": _hash("synthetic-thread-001"),
        "subject": "New jobs matching Python",
        "sender_hash": _hash("alerts.invalid"),
        "sender_domain": "alerts.invalid",
        "received_at": "2026-06-02T12:00:00Z",
        "body_hash": _hash("Acme Data Engineer Python SQL FastAPI"),
        "plain_text": "Acme Data Engineer\nPython SQL FastAPI\nRemote Canada",
        "html": """
        <html><body>
        <a href="https://email.example/click?u=http%3A%2F%2Flocalhost%3A8001%2Fats%2Fgeneric%2Facme%2Fjobs%2F123">
        Acme Data Engineer
        </a>
        <a href="https://example.com/unsubscribe">unsubscribe</a>
        </body></html>
        """,
    }
]


class ExtractedEmailLink(BaseModel):
    href: str
    link_text: str
    surrounding_text: str = ""
    link_position: int


class EmailLinkClassification(BaseModel):
    classification: str
    confidence: float
    reason: str


class EmailLinkExtractor:
    def __init__(self, href_normalizer: Callable[[str], str] | None = None) -> None:
        self._href_normalizer = href_normalizer or _identity

    def extract(self, html_or_text: str) -> list[ExtractedEmailLink]:
        soup = BeautifulSoup(html_or_text, "html.parser")
        links: list[ExtractedEmailLink] = []
        for index, anchor in enumerate(soup.find_all("a")):
            href = anchor.get("href") or ""
            if not href:
                continue
            links.append(
                ExtractedEmailLink(
                    href=self._href_normalizer(href),
                    link_text=(anchor.get_text(" ", strip=True) or href),
                    surrounding_text=(anchor.parent.get_text(" ", strip=True) if anchor.parent else ""),
                    link_position=index,
                )
            )
        if links:
            return links
        for index, match in enumerate(re.finditer(r"https?://\S+", html_or_text)):
            href = match.group(0)
            links.append(
                ExtractedEmailLink(
                    href=self._href_normalizer(href),
                    link_text=href,
                    link_position=index,
                )
            )
        return links


def _identity(value: str) -> str:
    return value


def classify_email_link(link: ExtractedEmailLink) -> EmailLinkClassification:
    classification = classify_posting_link(link.href, link.link_text)
    return EmailLinkClassification(
        classification=classification.classification,
        confidence=classification.confidence,
        reason=classification.reason,
    )
