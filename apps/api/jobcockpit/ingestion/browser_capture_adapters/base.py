from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol
from urllib.parse import ParseResult


@dataclass(frozen=True)
class ExternalJobBoardTerminalStatus:
    status: Literal["closed", "expired"]
    evidence_code: str


class CaptureAccessVerdict(str, Enum):
    PENDING = "pending"
    USABLE = "usable"
    AUTHENTICATION_WALL = "authentication_wall"
    CHALLENGE = "challenge"
    INCOMPLETE = "incomplete"


class CapturePageKind(str, Enum):
    PENDING = "pending"
    POSTING = "posting"
    TERMINAL_POSTING = "terminal_posting"
    NON_JOB = "non_job"
    UNKNOWN = "unknown"


class CaptureProcessingAction(str, Enum):
    PARSE = "parse"
    APPLY_TERMINAL = "apply_terminal"
    REJECT = "reject"


@dataclass(frozen=True)
class CaptureSourceIdentity:
    provider: str
    identity_hash: str
    public_source_url: str
    canonical_source_url: str
    strength: Literal["authoritative", "weak"]
    source_url_dedup_safe: bool


@dataclass(frozen=True)
class PageCaptureAssessment:
    source_identity: CaptureSourceIdentity | None
    access_verdict: CaptureAccessVerdict
    page_kind: CapturePageKind
    evidence_code: str
    terminal_status: ExternalJobBoardTerminalStatus | None = None


class BrowserCaptureProviderAdapter(Protocol):
    """Provider-owned capture identity, route, and terminal-state rules."""

    provider: str

    def matches_provider_url(self, parsed: ParseResult) -> bool: ...

    def matches_posting_route(self, parsed: ParseResult) -> bool: ...
    def matches_source_route(self, parsed: ParseResult) -> bool: ...

    def matches_terminal_route(self, parsed: ParseResult) -> bool: ...

    def capture_identity(self, parsed: ParseResult) -> CaptureSourceIdentity: ...

    def terminal_status(self, page_text: str) -> ExternalJobBoardTerminalStatus | None: ...


@dataclass(frozen=True)
class BrowserCaptureContext:
    raw_text: str
    lines: list[str]
    source_url: str | None = None
    source_type: str | None = None


@dataclass(frozen=True)
class BrowserCaptureExtractedFields:
    title: str | None = None
    company: str | None = None
    location_label: str | None = None

    def has_values(self) -> bool:
        return bool(self.title or self.company or self.location_label)


class BrowserCaptureAdapter(Protocol):
    provider: str

    def matches(self, context: BrowserCaptureContext) -> bool:
        """Return true when this adapter can parse the captured page shape."""

    def extract(self, context: BrowserCaptureContext) -> BrowserCaptureExtractedFields:
        """Return deterministic fields parsed from already-captured browser text."""
