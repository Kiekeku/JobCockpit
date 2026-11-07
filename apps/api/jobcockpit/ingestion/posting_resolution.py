from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from jobcockpit.ingestion.ats_adapters.base import SourceArchive
from jobcockpit.ingestion.source_resolution import (
    OfficialDiscoveryOutcome,
    canonical_official_discovery_reason,
    normalized_official_discovery_reason,
    official_discovery_outcome,
)


RenderedUrlIdentity = Literal["origin", "path", "path_and_query", "source_identity"]
RenderedQueryIdentityGroup = tuple[str, ...]
RenderedEvidenceOrigin = Literal["browser_dom", "model_output", "unknown"]

_RENDERED_QUERY_ALIAS_GROUPS: tuple[RenderedQueryIdentityGroup, ...] = (
    ("job_id", "gh_jid"),
)
_RENDERED_QUERY_TRACKING_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "gh_src",
        "mc_cid",
        "mc_eid",
    }
)


def rendered_query_identity_groups(source_url: str) -> tuple[RenderedQueryIdentityGroup, ...]:
    """Return query identities that must survive a rendered-page redirect."""
    query = parse_qs(urlparse(source_url).query, keep_blank_values=True)
    groups: list[RenderedQueryIdentityGroup] = []
    processed_alias_groups: set[RenderedQueryIdentityGroup] = set()
    for key in query:
        if _is_rendered_query_tracking_key(key):
            continue
        alias_group = next(
            (group for group in _RENDERED_QUERY_ALIAS_GROUPS if key in group),
            None,
        )
        if alias_group is None:
            groups.append((key,))
            continue
        if alias_group in processed_alias_groups:
            continue
        processed_alias_groups.add(alias_group)
        present_aliases = tuple(alias for alias in alias_group if alias in query)
        expected_values = [query[alias] for alias in present_aliases]
        if any(values != expected_values[0] for values in expected_values[1:]):
            groups.extend((alias,) for alias in present_aliases)
        else:
            groups.append(alias_group)
    return tuple(groups)


def _is_rendered_query_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in _RENDERED_QUERY_TRACKING_KEYS


class RenderedIdentityVerdict(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"


class RenderedAccessVerdict(str, Enum):
    USABLE = "usable"
    CHALLENGE = "challenge"
    BLOCKED = "blocked"


class RenderedStatusSignal(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExternalPostingHint:
    source_url: str
    source_kind: str
    company: str = ""
    title: str = ""
    location: str = ""
    term: str = ""
    link_text: str = ""
    evidence_text: str = ""
    url_identity: RenderedUrlIdentity = "origin"
    identity_query_groups: tuple[RenderedQueryIdentityGroup, ...] = ()
    source_text: str = ""
    # Untrusted saved links: fetched and verified before accepting any identity.
    candidate_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class ResolvedExternalPosting:
    source_url: str
    raw_text: str
    confidence: float = 0.0
    evidence: str = ""
    evidence_origin: RenderedEvidenceOrigin = "unknown"
    source_archive: SourceArchive | None = None
    content_truncated: bool = False
    complete_body_text: str = ""
    rendered_text: str = field(default="", repr=False)
    rendered_html: str = field(default="", repr=False)


RenderedStatusClassifier = Callable[[str], RenderedStatusSignal]


@dataclass(frozen=True)
class RenderedPageObservation:
    posting: ResolvedExternalPosting
    identity: RenderedIdentityVerdict
    access: RenderedAccessVerdict
    status: RenderedStatusSignal = RenderedStatusSignal.UNKNOWN

    @property
    def has_trusted_status_signal(self) -> bool:
        return (
            self.identity is RenderedIdentityVerdict.MATCH
            and self.access is RenderedAccessVerdict.USABLE
            and self.status is not RenderedStatusSignal.UNKNOWN
        )

    @property
    def permits_status_action(self) -> bool:
        return self.has_trusted_status_signal and self.status in {
            RenderedStatusSignal.OPEN,
            RenderedStatusSignal.CLOSED,
            RenderedStatusSignal.EXPIRED,
        }


@dataclass(frozen=True)
class PostingResolutionAttempt:
    stage: str
    provider: str
    status: str
    reason_code: str
    candidate_url: str = ""
    confidence: float | None = None
    candidate_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "stage": self.stage,
            "provider": self.provider,
            "status": self.status,
            "reason_code": canonical_official_discovery_reason(self.reason_code),
        }
        candidate_url = _sanitized_attempt_candidate_url(self.candidate_url)
        if candidate_url:
            payload["candidate_url"] = candidate_url
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.candidate_count is not None:
            payload["candidate_count"] = self.candidate_count
        return payload


MAX_POSTING_RESOLUTION_ATTEMPTS = 32
POSTING_RESOLUTION_ATTEMPT_STAGES = frozenset(
    {
        "source_render",
        "official_search",
        "alternative_search",
        "official_candidate_render",
        "structured_fallback",
    }
)
POSTING_RESOLUTION_ATTEMPT_STATUSES = frozenset(
    {"resolved", "not_found", "needs_review"}
)


@dataclass(frozen=True)
class PostingResolution:
    status: str
    posting: ResolvedExternalPosting | None = None
    candidates: tuple[ResolvedExternalPosting, ...] = field(default_factory=tuple)
    error: str = ""
    source_path: str = ""
    official_discovery_status: str = ""
    reason_code: str = ""
    official_search_outcome: OfficialDiscoveryOutcome | None = None
    attempts: tuple[PostingResolutionAttempt, ...] = field(default_factory=tuple)
    trace_truncated: bool = False


_ATTEMPT_CANDIDATE_IDENTITY_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "jid",
        "jk",
        "job",
        "job_id",
        "job_number",
        "job_req_id",
        "job_seq_no",
        "jobid",
        "jobnumber",
        "jobreqid",
        "jobseqno",
        "opening_id",
        "openingid",
        "opportunity_id",
        "opportunityid",
        "position_id",
        "positionid",
        "posting",
        "posting_id",
        "postingid",
        "req_id",
        "reqid",
        "requisition_id",
        "requisitionid",
        "rid",
        "vacancy",
        "vacancy_id",
        "vacancyid",
    }
)
_ATTEMPT_CANDIDATE_IDENTITY_VALUE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._~:/-]{0,159}"
)


def _is_safe_attempt_identity_query(key: str, value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return (
        normalized in _ATTEMPT_CANDIDATE_IDENTITY_QUERY_KEYS
        and bool(_ATTEMPT_CANDIDATE_IDENTITY_VALUE.fullmatch(value))
    )


def _sanitized_attempt_candidate_url(value: str) -> str:
    candidate_url = value.strip()
    if not candidate_url or len(candidate_url) > 8192:
        return ""
    try:
        parsed_url = urlparse(candidate_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            return ""
        safe_query = urlencode(
            [
                (key, query_value)
                for key, query_value in parse_qsl(
                    parsed_url.query,
                    keep_blank_values=True,
                    max_num_fields=128,
                )
                if _is_safe_attempt_identity_query(key, query_value)
            ]
        )
        sanitized_url = parsed_url._replace(query=safe_query, fragment="").geturl()
        return sanitized_url if len(sanitized_url) <= 2048 else ""
    except ValueError:
        return ""


def _official_search_outcome_from_attempt(
    attempt: PostingResolutionAttempt,
) -> OfficialDiscoveryOutcome:
    if attempt.reason_code == "official_candidates_found":
        return official_discovery_outcome(
            attempt.reason_code,
            status="candidates_found",
        )
    status = attempt.status if attempt.status in {"resolved", "not_found"} else ""
    return official_discovery_outcome(attempt.reason_code, status=status)


def _with_official_search_outcome(
    resolution: PostingResolution,
    outcome: OfficialDiscoveryOutcome,
) -> PostingResolution:
    if (
        resolution.official_search_outcome is not None
        and resolution.official_search_outcome != outcome
    ):
        raise ValueError("official_search_outcome_is_immutable")
    return replace(
        resolution,
        official_search_outcome=outcome,
        official_discovery_status=outcome.status,
        reason_code=outcome.reason_code,
    )


def _official_search_outcome_for_resolution(
    resolution: PostingResolution,
) -> OfficialDiscoveryOutcome:
    if resolution.official_search_outcome is not None:
        return resolution.official_search_outcome
    status = resolution.official_discovery_status
    if (
        resolution.status != "resolved"
        and normalized_official_discovery_reason(status) == "resolved"
    ):
        status = ""
    reason = resolution.reason_code or resolution.error or resolution.status
    return official_discovery_outcome(reason, status=status)


def _bounded_resolution_attempts(
    *resolutions: PostingResolution | None,
    extra: Sequence[PostingResolutionAttempt] = (),
) -> tuple[tuple[PostingResolutionAttempt, ...], bool]:
    # Sources must represent disjoint chronological slices. Equal attempts are
    # distinct executions and must never be collapsed by value.
    attempts = [
        attempt
        for resolution in resolutions
        if resolution is not None
        for attempt in resolution.attempts
    ]
    attempts.extend(extra)
    truncated = any(
        resolution is not None and resolution.trace_truncated for resolution in resolutions
    ) or len(attempts) > MAX_POSTING_RESOLUTION_ATTEMPTS
    return tuple(attempts[:MAX_POSTING_RESOLUTION_ATTEMPTS]), truncated


def _with_resolution_attempts(
    resolution: PostingResolution,
    *sources: PostingResolution | None,
    extra: Sequence[PostingResolutionAttempt] = (),
) -> PostingResolution:
    attempts, trace_truncated = _bounded_resolution_attempts(
        *sources,
        resolution,
        extra=extra,
    )
    return replace(
        resolution,
        attempts=attempts,
        trace_truncated=trace_truncated,
    )


def _append_resolution_history(
    resolution: PostingResolution,
    *later_resolutions: PostingResolution,
) -> PostingResolution:
    return _with_resolution_attempts(
        replace(resolution, attempts=(), trace_truncated=False),
        resolution,
        *later_resolutions,
    )


def _resolution_attempt(
    resolution: PostingResolution,
    *,
    stage: str,
    provider: str,
    candidate_url: str = "",
) -> PostingResolutionAttempt:
    search_found_candidates = (
        stage == "official_search"
        and resolution.status != "resolved"
        and bool(resolution.candidates)
    )
    if search_found_candidates:
        reason = "official_candidates_found"
        attempt_status = "resolved"
    elif resolution.error or resolution.reason_code:
        reason = resolution.reason_code or resolution.error
        attempt_status = resolution.status
    elif stage == "official_search" and resolution.status == "resolved":
        reason = "official_posting_resolved"
        attempt_status = "resolved"
    elif stage == "official_search" and resolution.status == "not_found":
        reason = "official_posting_not_found"
        attempt_status = "not_found"
    else:
        reason = f"{stage}_{resolution.status}"
        attempt_status = resolution.status
    posting = resolution.posting
    return PostingResolutionAttempt(
        stage=stage,
        provider=provider,
        status=(
            attempt_status
            if attempt_status in POSTING_RESOLUTION_ATTEMPT_STATUSES
            else "needs_review"
        ),
        reason_code=canonical_official_discovery_reason(reason)[:160],
        candidate_url=candidate_url,
        confidence=posting.confidence if posting is not None else None,
        candidate_count=len(resolution.candidates) if stage == "official_search" else None,
    )


def _with_official_search_attempt(
    resolution: PostingResolution,
    *sources: PostingResolution | None,
    provider: str,
) -> PostingResolution:
    attempt = _resolution_attempt(
        resolution,
        stage="official_search",
        provider=provider,
    )
    resolution = _with_official_search_outcome(
        resolution,
        _official_search_outcome_from_attempt(attempt),
    )
    return _with_resolution_attempts(
        resolution,
        *sources,
        extra=(attempt,),
    )
