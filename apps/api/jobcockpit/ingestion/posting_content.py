"""Classify recovery evidence without changing snapshot bytes or parser input.

Discovery provenance is not job content. A missing description must be established
before replacing a saved snapshot; uncertain content remains protected.
"""
from __future__ import annotations

import re
from enum import StrEnum

from jobcockpit.ingestion.posting_resolution import ExternalPostingHint


class PostingContentState(StrEnum):
    DESCRIPTION = "description"
    METADATA_ONLY = "metadata_only"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


_IDENTITY_LABELS = frozenset({
    "title", "job title", "role", "position", "company", "location", "locations",
    "primary location", "page title", "apply url", "duration", "work term", "term",
    "start date", "employment timing", "country", "status", "tags",
})
_PROVENANCE_LABEL = re.compile(
    r"(?:[a-z]+ )?source url|source(?: evidence| resolution(?: [a-z]+)*)?|"
    r"official discovery(?: [a-z]+)*|resolution evidence|(?:email|hint)(?: [a-z]+)*"
)


def _normalized(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def posting_content_state(raw_text: str, hint: ExternalPostingHint) -> PostingContentState:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return PostingContentState.UNKNOWN
    fields = []
    for line in lines:
        label, separator, value = line.partition(":")
        fields.append((line, _normalized(label) if separator else "", value.strip()))

    # Inspect the first actual page title, not nested titles in source evidence.
    page_title = next((value.casefold() for _, label, value in fields if label == "page title"), "")
    if page_title in {"blocked - indeed.com", "sign up | linkedin"}:
        return PostingContentState.BLOCKED

    identities = {_normalized(value) for value in (hint.title, hint.company, hint.location, hint.term)}
    identities.update(_normalized(value) for _, label, value in fields if label in _IDENTITY_LABELS)
    # Legacy email captures sometimes retain the Email Row/Card prefix in Title.
    # Recognize the repeated bare title without modifying the parsed job identity.
    for value in [hint.title, *(value for _, label, value in fields if label in {"title", "job title"})]:
        identities.add(_normalized(re.sub(r"^(?:email row|email card):\s*", "", value, flags=re.I)))
    body = []
    evidence_may_contain_body = False
    for line, label, value in fields:
        if _PROVENANCE_LABEL.fullmatch(label):
            # Some legacy captures retained a description only inside evidence.
            # It cannot verify a new candidate, but must not be erased as metadata.
            if label == "source evidence" and re.search(
                r"\b(?:about the (?:role|job)|description|responsibilities|qualifications)\b", value, re.I,
            ):
                evidence_may_contain_body = True
            continue
        if label in _IDENTITY_LABELS or _normalized(line) in identities:
            continue
        body.append(line)

    # Observed Indeed block screens can lack a document title. Requiring the
    # standalone heading avoids treating a job's prose about blocked requests as a wall.
    if any(line.casefold() == "request blocked" for line in body) and any(
        "you have been blocked" in line.casefold() for line in body
    ):
        return PostingContentState.BLOCKED
    if len(" ".join(body).split()) >= 5:
        return PostingContentState.DESCRIPTION
    if body or evidence_may_contain_body:
        return PostingContentState.UNKNOWN
    return PostingContentState.METADATA_ONLY
