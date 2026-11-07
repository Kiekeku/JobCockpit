from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol
from urllib.parse import unquote_plus, urlparse, urlunparse

import httpx

from jobcockpit.ingestion.role_interest import RoleInterestMatch

MAX_RETRY_AFTER_SECONDS = 365 * 24 * 60 * 60



def normalize_source_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    query_segments = parsed.query.split("&")
    filtered_segments = [
        segment
        for segment in query_segments
        if not unquote_plus(segment.partition("=")[0]).lower().startswith("utm_")
    ]
    if len(filtered_segments) == len(query_segments):
        return source_url
    return urlunparse(parsed._replace(query="&".join(filtered_segments)))


@dataclass(frozen=True)
class SourceArchive:
    source_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AtsJob:
    source_url: str
    raw_text: str
    portal_source_url: str | None = None
    source_archive: SourceArchive | None = None
    title: str | None = None
    provider_job_id: str | None = None
    # Page evidence before source hints are appended for ingestion provenance.
    observed_raw_text: str | None = None
    has_posting_body: bool | None = None
    # Origin of raw_text, which may differ from the discovered application URL.
    content_source_url: str | None = None

    def __post_init__(self) -> None:
        if not self.portal_source_url:
            object.__setattr__(self, "portal_source_url", self.source_url)


@dataclass(frozen=True)
class AtsInterestSkip:
    source_url: str
    title: str
    reason: str
    matched_include: str | None = None
    matched_exclude: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_url": self.source_url,
            "title": self.title,
            "reason": self.reason,
            "matched_exclude": self.matched_exclude,
            "matched_include": self.matched_include,
        }


@dataclass(frozen=True)
class AtsSourceExclusion:
    category: str
    count: int
    reason: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "category": self.category,
            "count": self.count,
            "reason": self.reason,
        }


ListingInterestMatcher = Callable[[str], RoleInterestMatch]


@dataclass
class AtsFetchStats:
    links_seen_count: int = 0
    link_failures: list[AtsLinkFailure] = field(default_factory=list)

    interest_skips: list[AtsInterestSkip] = field(default_factory=list)
    source_exclusions: list[AtsSourceExclusion] = field(default_factory=list)

    def record_link_failure(self, source_url: str, error: Exception) -> AtsLinkFailure:
        failure = classify_link_failure(source_url, error)
        self.link_failures.append(failure)
        return failure

    @property
    def jobs_seen_count(self) -> int | None:
        return self.links_seen_count or None


@dataclass(frozen=True)
class AtsFetchRequest:
    source_url: str
    limit: int = 10
    search_text: str = ""
    applied_facets: dict = field(default_factory=dict)
    excluded_source_urls: frozenset[str] = field(default_factory=frozenset)
    excluded_provider_job_ids: frozenset[str] = field(default_factory=frozenset)
    stats: AtsFetchStats = field(default_factory=AtsFetchStats)
    link_concurrency: int = field(default=4, kw_only=True)
    source_url_normalizer: Callable[[str], str] = field(
        default=normalize_source_url, repr=False, compare=False
    )

    listing_interest_matcher: ListingInterestMatcher | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "excluded_source_urls",
            frozenset(self.source_url_normalizer(url) for url in self.excluded_source_urls),
        )

    def excludes_source_url(self, source_url: str) -> bool:
        return self.source_url_normalizer(source_url) in self.excluded_source_urls

    def excludes_provider_job_id(self, provider_job_id: str | None) -> bool:
        return bool(provider_job_id and provider_job_id in self.excluded_provider_job_ids)

    def allows_listing_candidate(self, source_url: str, title: str) -> bool:
        if self.listing_interest_matcher is None:
            return True
        match = self.listing_interest_matcher(title)
        if match.matched:
            return True
        self.stats.interest_skips.append(
            AtsInterestSkip(
                source_url=source_url,
                title=match.title,
                reason=match.reason,
                matched_include=match.matched_include,
                matched_exclude=match.matched_exclude,
            )
        )
        return False

    @property
    def remaining_link_attempts(self) -> int:
        return max(0, self.limit - self.stats.links_seen_count)

    def try_begin_link_attempt(self) -> bool:
        if self.remaining_link_attempts == 0:
            return False
        self.stats.links_seen_count += 1
        return True


class AtsProviderError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AtsVerificationRequiredError(AtsProviderError):
    """The guarded browser already reached a verification/preparation gate."""

    def __init__(self, detail: str, *, reason_code: str = "authentication_wall") -> None:
        super().__init__(502, detail)
        self.reason_code = reason_code


class AtsResponseTooLargeError(httpx.RequestError):
    pass


@dataclass(frozen=True)
class AtsLinkFailure:
    source_url: str
    category: Literal["permanent", "retryable", "technical"]
    reason: str
    upstream_status_code: int | None = None
    retry_after_seconds: int | None = None
    upstream_response_text: str | None = field(default=None, repr=False, compare=False)
    upstream_timed_out: bool = field(default=False, repr=False, compare=False)
    upstream_response_too_large: bool = field(default=False, repr=False, compare=False)

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "source_url": self.source_url,
            "category": self.category,
            "reason": self.reason,
            "upstream_status_code": self.upstream_status_code,
        }


class AtsRecordedLinkFailureError(AtsProviderError):
    def __init__(self, failure: AtsLinkFailure) -> None:
        super().__init__(502, failure.reason)
        self.failure = failure


def classify_link_failure(source_url: str, error: Exception) -> AtsLinkFailure:
    recorded_failure = _recorded_link_failure(error)
    if recorded_failure is not None:
        return AtsLinkFailure(
            source_url=normalize_source_url(source_url),
            category=recorded_failure.category,
            reason=recorded_failure.reason,
            upstream_status_code=recorded_failure.upstream_status_code,
            retry_after_seconds=recorded_failure.retry_after_seconds,
            upstream_response_text=recorded_failure.upstream_response_text,
            upstream_timed_out=recorded_failure.upstream_timed_out,
            upstream_response_too_large=recorded_failure.upstream_response_too_large,
        )
    upstream_status_code = _upstream_status_code(error)
    if upstream_status_code in {404, 410}:
        category = "permanent"
    elif upstream_status_code in {408, 425, 429} or (
        upstream_status_code is not None and upstream_status_code >= 500
    ):
        category = "retryable"
    elif _has_httpx_transport_error(error):
        category = "retryable"
    else:
        category = "technical"
    return AtsLinkFailure(
        source_url=normalize_source_url(source_url),
        category=category,
        reason=str(error) or error.__class__.__name__,
        upstream_status_code=upstream_status_code,
        retry_after_seconds=_upstream_retry_after_seconds(error),
        upstream_response_text=_upstream_response_text(error),
        upstream_timed_out=_has_httpx_timeout_error(error),
        upstream_response_too_large=any(
            isinstance(current, AtsResponseTooLargeError)
            for current in _exception_chain(error)
        ),
    )


def _recorded_link_failure(error: BaseException) -> AtsLinkFailure | None:
    for current in _exception_chain(error):
        if isinstance(current, AtsRecordedLinkFailureError):
            return current.failure
    return None


def _upstream_status_code(error: BaseException) -> int | None:
    import httpx

    for current in _exception_chain(error):
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code
    return None


def _upstream_retry_after_seconds(error: BaseException) -> int | None:
    import httpx

    for current in _exception_chain(error):
        if not isinstance(current, httpx.HTTPStatusError):
            continue
        value = current.response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return _bounded_retry_after_seconds(int(value.strip()))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (OverflowError, TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return _bounded_retry_after_seconds(
                int((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            )
    return None


def _upstream_response_text(error: BaseException) -> str | None:
    import httpx

    for current in _exception_chain(error):
        if not isinstance(current, httpx.HTTPStatusError):
            continue
        try:
            return current.response.text[:1_000_000]
        except (httpx.ResponseNotRead, UnicodeDecodeError):
            return None
    return None


def _bounded_retry_after_seconds(value: int) -> int:
    return min(max(0, value), MAX_RETRY_AFTER_SECONDS)


def _has_httpx_transport_error(error: BaseException) -> bool:
    import httpx

    return any(isinstance(current, httpx.RequestError) for current in _exception_chain(error))


def _has_httpx_timeout_error(error: BaseException) -> bool:
    import httpx

    return any(
        isinstance(current, httpx.TimeoutException)
        for current in _exception_chain(error)
    )


def _exception_chain(error: BaseException):
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


class AtsProviderAdapter(Protocol):
    provider: str

    def validate_source_url(self, source_url: str) -> None:
        """Raise AtsProviderError if the URL is not valid for this provider."""

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        """Fetch provider jobs and return normalized raw-text snapshots."""
