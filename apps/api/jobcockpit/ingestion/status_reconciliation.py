from __future__ import annotations

import ipaddress
import json
import queue
import re
import threading
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import Enum
from io import BytesIO
from typing import Callable, Generic, Literal, Sequence, TypeVar
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment
from pypdf import PdfReader
from sqlalchemy import and_, case, func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from jobcockpit.api.cover_letter_generation_runs import (
    cancel_active_cover_letter_generations,
)
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    AuditLogEntry,
    JobPosting,
    JobStatusCheck,
    JobStatusScanRun,
)
from jobcockpit.db.session import get_engine, session_scope

from jobcockpit.ingestion.ats_adapters.base import (
    AtsResponseTooLargeError,
    classify_link_failure,
)
from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
    oracle_candidate_experience_detail_urls,
)
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver
from jobcockpit.ingestion.browser_cleanup import retain_pending_browser_cleanup
from jobcockpit.ingestion.pinned_http import (
    PinnedHttpClient,
    UrlPolicyViolation,
)
from jobcockpit.ingestion.posting_discovery import (
    ExternalPostingHint,
    external_job_board_terminal_status,
    normalize_posting_resolution,
)
from jobcockpit.ingestion.posting_links import default_rendered_page_resolver
from jobcockpit.ingestion.source_authority import job_portal_kind
from jobcockpit.ingestion.url_hosts import hostname_matches_indeed
from jobcockpit.ingestion.status_scan_lock import (
    StatusScanProcessLock,
    StatusScanProcessLockUnavailable,
)
from jobcockpit.ingestion.status_identity import (
    ATS_CLOSED_SOURCE_KINDS as _ATS_CLOSED_SOURCE_KINDS,
    INDEED_POSTING_ID as _INDEED_POSTING_ID,
    RESERVED_ROUTE_SEGMENTS as _RESERVED_ROUTE_SEGMENTS,
    path_parts_for_url as _path_parts,
    posting_identity,
    posting_url_supports_open_evidence,
    source_kind as _source_kind,
    supports_definitive_ats_404 as _supports_definitive_ats_404,
)
from jobcockpit.ingestion.url_policy import (
    ScheduledRenderedUrlPolicy,
    UrlFetchPolicy,
)
from jobcockpit.utils.ids import new_id

CheckResult = Literal["open", "closed", "expired", "inconclusive", "failed", "unsupported"]
DetectedStatus = Literal["closed", "expired"]
EvidenceScope = Literal["source_page", "ingestion_snapshot", "rendered_page"]


@dataclass(frozen=True)
class StatusUrlCandidate:
    url: str
    kind: str
    source_kind: str


@dataclass(frozen=True)
class JobStatusTarget:
    job_id: str
    status: str
    source_type: str
    source_url: str | None
    company: str
    title: str
    location: str
    candidates: tuple[StatusUrlCandidate, ...]
    failure_count: int
    not_found_count: int
    last_checked_at: datetime | None
    last_check_source_url: str | None = None
    selected_updated_at: datetime | None = None
    application_deadline: str | None = None
    source_repair_required: bool = False


@dataclass(frozen=True)
class StatusCheckDecision:
    result: CheckResult
    evidence_code: str
    source_url: str | None = None
    source_kind: str | None = None
    detected_status: DetectedStatus | None = None
    http_status: int | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None
    error_message: str | None = None
    requested_url: str | None = None
    final_url: str | None = None
    candidate_kind: str | None = None
    diagnostics: dict[str, object] | None = None

    def __post_init__(self) -> None:
        expected = self.result if self.result in {"closed", "expired"} else None
        if self.detected_status != expected:
            raise ValueError(
                "detected_status must match a closed or expired result and must "
                "be absent for non-terminal results"
            )


@dataclass(frozen=True)
class RenderRequired:
    evidence_code: str
    source_url: str
    source_kind: str
    retry_after_seconds: int | None = None
    http_status: int | None = None
    prior_decision: StatusCheckDecision | None = None
    resume_candidate_index: int = 0


@dataclass(frozen=True)
class DeferredRequired:
    evidence_code: str
    source_url: str | None = None
    retry_after_seconds: int | None = None
    prior_decision: StatusCheckDecision | None = None


StatusResolution = StatusCheckDecision | RenderRequired | DeferredRequired


class WorkerOutcomeState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


_WorkItem = TypeVar("_WorkItem")


@dataclass(frozen=True)
class WorkerOutcome(Generic[_WorkItem]):
    item: _WorkItem
    state: WorkerOutcomeState
    resolution: StatusResolution | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        if self.state is WorkerOutcomeState.COMPLETED:
            if self.resolution is None or self.error is not None:
                raise ValueError("completed worker outcomes require only a resolution")
        elif self.state is WorkerOutcomeState.FAILED:
            if self.error is None or self.resolution is not None:
                raise ValueError("failed worker outcomes require only an error")
        elif self.resolution is not None or self.error is not None:
            raise ValueError("abandoned worker outcomes cannot contain a result")


class _RenderedWorkerPhase(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    CANCEL_REQUESTED = "cancel_requested"
    TERMINAL = "terminal"


class _RenderedWorkerLifecycle:
    """Single owner for resolver attachment, cancellation, and cleanup transfer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = _RenderedWorkerPhase.PENDING
        self._resolver: object | None = None
        self._cleanup_error: Exception | None = None
        self._handed_off_count = 0
        self._cleanup_owned = False

    def attach(self, resolver: object) -> bool:
        """Attach exactly once and report whether the worker may start rendering."""

        with self._lock:
            if self._resolver is not None or self._phase is _RenderedWorkerPhase.TERMINAL:
                raise RuntimeError("rendered worker resolver was attached more than once")
            self._resolver = resolver
            if self._phase is _RenderedWorkerPhase.CANCEL_REQUESTED:
                return False
            self._phase = _RenderedWorkerPhase.ACTIVE
            return True

    def cancel(self) -> int:
        """Make cancellation terminal for new work and transfer known cleanup."""

        with self._lock:
            if self._phase is not _RenderedWorkerPhase.TERMINAL:
                self._phase = _RenderedWorkerPhase.CANCEL_REQUESTED
        return self.ensure_cleanup_owned()

    def finalize_worker(self) -> int:
        """Finalize from the worker, recording terminal state even on failure."""

        with self._lock:
            resolver = self._resolver
        try:
            return self._finalize(resolver)
        finally:
            with self._lock:
                self._phase = _RenderedWorkerPhase.TERMINAL

    def ensure_cleanup_owned(self) -> int:
        """Retry cleanup transfer from the coordinator before completing a run."""

        with self._lock:
            resolver = self._resolver
            if resolver is not None and self._cleanup_owned and self._cleanup_error is None:
                return 0
        return self._finalize(resolver)

    def _finalize(self, resolver: object | None) -> int:
        if resolver is None:
            return 0
        try:
            retained_count = _finalize_rendered_resolver(resolver)
        except Exception as error:
            with self._lock:
                self._cleanup_error = error
            raise
        with self._lock:
            self._cleanup_error = None
            self._cleanup_owned = True
            self._handed_off_count += max(0, retained_count)
        return retained_count

    @property
    def phase(self) -> _RenderedWorkerPhase:
        with self._lock:
            return self._phase

    @property
    def cleanup_error(self) -> Exception | None:
        with self._lock:
            return self._cleanup_error

    @property
    def handed_off_count(self) -> int:
        with self._lock:
            return self._handed_off_count


@dataclass(frozen=True)
class StatusObservation:
    decision: StatusCheckDecision
    observed_at: datetime
    retry_not_before: datetime | None = None


@dataclass(frozen=True)
class RenderRequest:
    target: JobStatusTarget
    candidate: StatusUrlCandidate
    retry_not_before: datetime | None = None
    prior_decision: StatusCheckDecision | None = None
    resume_candidate_index: int = 0


ObservationClock = Callable[[], datetime]


class StatusScanAlreadyRunningError(RuntimeError):
    pass


class StatusScanLeaseLostError(RuntimeError):
    pass


class StatusReconciliationRunFailed(RuntimeError):
    """A started durable scan failed after retaining its partial outcome."""

    def __init__(self, summary: dict) -> None:
        self.summary = dict(summary)
        super().__init__(str(self.summary.get("error") or "status reconciliation run failed"))

    @property
    def run_id(self) -> str:
        return str(self.summary["run_id"])


class StatusRuntimeBudgetExhausted(TimeoutError):
    pass


class StatusUnsupportedContentEncodingError(AtsResponseTooLargeError):
    pass


StatusChecker = Callable[[JobStatusTarget, StatusUrlCandidate], StatusCheckDecision]


class _ProviderFeedCache:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, dict[str, str]] = {}
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, feed_url: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(feed_url, threading.Lock())

    def get(self, feed_url: str) -> dict[str, str] | None:
        with self._guard:
            return self._entries.get(feed_url)

    def put(self, feed_url: str, entries: dict[str, str]) -> None:
        with self._guard:
            self._entries[feed_url] = entries


_INDEED_EXPIRED = "this job has expired on indeed"
_ATS_EXPLICIT_CLOSED_MARKERS = (
    "the job you are looking for is no longer open",
    "job is no longer available",
    "the job is no longer available",
    "this job is no longer available",
    "job posting is no longer available",
    "the job posting is no longer available",
    "position is no longer available",
    "the position is no longer available",
    "this position is no longer available",
)
_ATS_DETAIL_MISSING_MARKERS = (
    "the page you are looking for does not exist",
    "the page you are looking for doesn't exist",
)
_AUTH_MARKERS = (
    "sign in to continue",
    "log in to continue",
    "authentication required",
    "access denied",
    "request blocked",
    "you have been blocked",
    "authwall",
)
_AUTH_OR_SEARCH_ROUTE_SEGMENTS = frozenset(
    {
        "authwall",
        "checkpoint",
        "login",
        "result",
        "results",
        "search",
        "search-results",
        "signin",
        "sign-in",
    }
)
_URL_IN_ERROR = re.compile(r"https?://\S+", re.IGNORECASE)
_MAX_REDIRECTS = 5
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_PROVIDER_FEED_RESPONSE_BYTES = 5_000_000
_RESPONSE_TOO_LARGE_EXTENSION = "jobcockpit_response_too_large"
_RESPONSE_UNSUPPORTED_ENCODING_EXTENSION = "jobcockpit_unsupported_content_encoding"
_DIRECT_CONCURRENCY = 4
_MAX_SCAN_RUNTIME_SECONDS = 7200
_GENERIC_404_CONFIRMATION_DELAY = timedelta(hours=6)
_BUDGET_DEFERRAL_DELAY = timedelta(hours=2)
_APPLICATION_DEADLINE_SAFE_CLOSE_HOUR_UTC = 12
_MAX_RETRY_AFTER_SECONDS = 365 * 24 * 60 * 60
_STATUS_SCHEMA_TABLES = frozenset({"job_status_scan_runs", "job_status_checks"})
_STATUS_SCHEMA_JOB_COLUMNS = frozenset(
    {
        "status_last_checked_at",
        "status_next_check_at",
        "status_check_result",
        "status_check_evidence_code",
        "status_check_failure_count",
        "status_check_not_found_count",
        "status_last_open_at",
        "status_last_open_evidence_code",
        "status_source_repair_required",
    }
)
_STATUS_SCHEMA_CHECK_COLUMNS = frozenset(
    {
        "requested_url",
        "final_url",
        "candidate_kind",
        "diagnostics",
    }
)
SOURCE_REPAIR_EVIDENCE_CODES = frozenset(
    {
        "job_identity_mismatch",
        "redirect_identity_mismatch",
        "workday_job_identity_mismatch",
    }
)


def infer_status_from_page(
    source_url: str,
    page_text: str,
    *,
    evidence_scope: EvidenceScope = "source_page",
    expected_company: str | None = None,
    expected_title: str | None = None,
) -> StatusCheckDecision | None:
    html_visibility_unconfirmed = evidence_scope != "rendered_page" and _looks_like_html_page(
        page_text
    )
    visible_text = _visible_page_text(page_text)
    identity = posting_identity(source_url)
    if identity.is_detail and _looks_like_auth_wall(source_url, visible_text):
        return StatusCheckDecision(
            "inconclusive",
            "authentication_wall",
            source_url,
            identity.source_kind,
            http_status=200,
            retryable=True,
        )
    if html_visibility_unconfirmed:
        return None
    decision: StatusCheckDecision | None = None
    external_terminal_status = (
        external_job_board_terminal_status(source_url, visible_text) if identity.is_detail else None
    )
    if external_terminal_status is not None:
        decision = StatusCheckDecision(
            external_terminal_status.status,
            external_terminal_status.evidence_code,
            source_url,
            identity.source_kind,
            external_terminal_status.status,
            200,
        )
    elif identity.source_kind == "indeed" and _is_indeed_selected_result_url(source_url):
        decision = _indeed_selected_result_expired_decision(
            source_url,
            visible_text,
            expected_company=expected_company,
            expected_title=expected_title,
        )
    elif (
        identity.source_kind == "indeed"
        and identity.is_detail
        and _contains_standalone_terminal_marker(visible_text, (_INDEED_EXPIRED,))
    ):
        decision = StatusCheckDecision(
            "expired", "indeed_job_expired", source_url, "indeed", "expired", 200
        )
    else:
        ats_closed = _contains_standalone_terminal_marker(
            visible_text, _ATS_EXPLICIT_CLOSED_MARKERS
        )
        ats_missing = _contains_standalone_terminal_marker(
            visible_text, _ATS_DETAIL_MISSING_MARKERS
        )
        if (
            identity.is_trusted_provider
            and identity.is_detail
            and identity.source_kind in _ATS_CLOSED_SOURCE_KINDS
            and evidence_scope != "ingestion_snapshot"
            and (ats_closed or (ats_missing and _supports_definitive_ats_404(identity)))
        ):
            decision = StatusCheckDecision(
                "closed",
                "ats_no_longer_available",
                source_url,
                identity.source_kind,
                "closed",
                200,
            )
    return _guard_terminal_evidence_transport(decision) if decision else None


def classify_http_status(
    status_code: int,
    *,
    source_kind: str,
    generic_404_count: int,
    source_url: str | None = None,
    retry_after_seconds: int | None = None,
) -> StatusCheckDecision:
    if status_code == 410:
        return _guard_terminal_evidence_transport(
            StatusCheckDecision(
                "closed", "http_410_gone", source_url, source_kind, "closed", status_code
            )
        )
    if status_code == 404:
        identity = posting_identity(source_url) if source_url else None
        if identity is not None and _supports_definitive_ats_404(identity):
            return _guard_terminal_evidence_transport(
                StatusCheckDecision(
                    "closed",
                    "recognized_ats_detail_404",
                    source_url,
                    identity.source_kind,
                    "closed",
                    status_code,
                )
            )
        if generic_404_count >= 1:
            return _guard_terminal_evidence_transport(
                StatusCheckDecision(
                    "closed",
                    "generic_http_404_confirmed",
                    source_url,
                    source_kind,
                    "closed",
                    status_code,
                )
            )
        return StatusCheckDecision(
            "inconclusive",
            "generic_http_404_pending_confirmation",
            source_url,
            source_kind,
            http_status=status_code,
            retryable=True,
            retry_after_seconds=retry_after_seconds,
        )
    evidence = {
        401: "authentication_required",
        403: "http_403_forbidden",
        408: "http_408_timeout",
        425: "http_425_too_early",
        429: "http_429_rate_limited",
    }.get(status_code)
    if evidence or status_code >= 500:
        return StatusCheckDecision(
            "inconclusive",
            evidence or "http_5xx",
            source_url,
            source_kind,
            http_status=status_code,
            retryable=True,
            retry_after_seconds=retry_after_seconds,
        )
    return StatusCheckDecision(
        "inconclusive",
        "unexpected_http_status",
        source_url,
        source_kind,
        http_status=status_code,
    )


def retry_delay(result: str, failure_count: int) -> timedelta | None:
    if result == "open":
        return timedelta(hours=24)
    if result in {"closed", "expired"}:
        return None
    if result == "unsupported":
        return timedelta(days=7)
    return (timedelta(hours=6), timedelta(hours=12), timedelta(hours=24))[
        min(max(failure_count, 0), 2)
    ]


def _passed_application_deadline_decision(
    target: JobStatusTarget,
    observed_at: datetime,
) -> StatusCheckDecision | None:
    raw_deadline = str(target.application_deadline or "").strip()
    if len(raw_deadline) != 10:
        return None
    try:
        deadline = datetime.strptime(raw_deadline, "%Y-%m-%d").date()
    except ValueError:
        return None
    safe_terminal_at = datetime(
        deadline.year,
        deadline.month,
        deadline.day,
        tzinfo=UTC,
    ) + timedelta(days=1, hours=_APPLICATION_DEADLINE_SAFE_CLOSE_HOUR_UTC)
    if _aware_utc(observed_at) < safe_terminal_at:
        return None
    candidate = target.candidates[0] if target.candidates else None
    return StatusCheckDecision(
        "expired",
        "application_deadline_passed",
        candidate.url if candidate else target.source_url,
        candidate.source_kind if candidate else None,
        detected_status="expired",
    )


def _status_url_comparison_key(
    source_url: str,
) -> tuple[str, str, int | None, str, str, str, str, str]:
    """Normalize only URL components whose comparison is case-insensitive."""
    canonical = source_url.strip()
    parsed = urlparse(canonical)
    if not parsed.scheme or not parsed.hostname:
        return ("", "", None, "", canonical, "", "", "")
    scheme = parsed.scheme.casefold()
    try:
        port = parsed.port
    except ValueError:
        return ("", "", None, "", canonical, "", "", "")
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    if port is None:
        port = default_port
    userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    return (
        scheme,
        parsed.hostname.rstrip(".").casefold(),
        port,
        userinfo,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    )


def ordered_status_urls(job: JobPosting) -> tuple[StatusUrlCandidate, ...]:
    source = job.source if isinstance(job.source, dict) else {}
    resolution = source.get("source_resolution")
    if not isinstance(resolution, dict):
        resolution = {}
    application = job.application if isinstance(job.application, dict) else {}
    values = (
        (resolution.get("official_url"), "official_ats"),
        (application.get("external_apply_url"), "external_apply"),
        (job.source_url or source.get("source_url"), "canonical_source"),
        (resolution.get("fallback_url"), "aggregator_fallback"),
    )
    candidates: list[StatusUrlCandidate] = []
    for value, kind in values:
        raw_url = str(value or "").strip()
        if not raw_url:
            continue
        if any(status_urls_match(candidate.url, raw_url) for candidate in candidates):
            continue
        candidates.append(StatusUrlCandidate(raw_url, kind, _source_kind(raw_url)))
    return tuple(candidates)


def status_report(
    settings: Settings,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> dict:
    _require_status_reconciliation_schema(settings)
    with session_scope(settings) as db:
        run_query = select(JobStatusScanRun).order_by(JobStatusScanRun.started_at.desc())
        check_query = select(JobStatusCheck).order_by(
            JobStatusCheck.checked_at.desc(),
            JobStatusCheck.created_at.desc(),
            JobStatusCheck.status_check_id.desc(),
        )
        if run_id:
            run_query = run_query.where(JobStatusScanRun.status_scan_run_id == run_id)
            check_query = check_query.where(JobStatusCheck.status_scan_run_id == run_id)
        if job_id:
            check_query = check_query.where(JobStatusCheck.job_id == job_id)
            run_query = run_query.where(
                JobStatusScanRun.status_scan_run_id.in_(
                    select(JobStatusCheck.status_scan_run_id).where(JobStatusCheck.job_id == job_id)
                )
            )
        runs = db.execute(run_query.limit(limit)).scalars().all()
        checks = db.execute(check_query.limit(limit)).scalars().all()
        return {
            "runs": [run.to_api() for run in runs],
            "checks": [check.to_api() for check in checks],
        }


def run_status_reconciliation(
    settings: Settings,
    *,
    limit: int = 100,
    rendered_limit: int = 30,
    max_runtime_seconds: int = 5400,
    job_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    checker: StatusChecker | None = None,
    now: datetime | None = None,
    observation_clock: ObservationClock | None = None,
) -> dict:
    try:
        with StatusScanProcessLock(settings):
            return _run_status_reconciliation_under_lock(
                settings,
                limit=limit,
                rendered_limit=rendered_limit,
                max_runtime_seconds=max_runtime_seconds,
                job_id=job_id,
                force=force,
                dry_run=dry_run,
                checker=checker,
                now=now,
                observation_clock=observation_clock,
            )
    except StatusScanProcessLockUnavailable as error:
        raise StatusScanAlreadyRunningError(str(error)) from error


def _run_status_reconciliation_under_lock(
    settings: Settings,
    *,
    limit: int = 100,
    rendered_limit: int = 30,
    max_runtime_seconds: int = 5400,
    job_id: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    checker: StatusChecker | None = None,
    now: datetime | None = None,
    observation_clock: ObservationClock | None = None,
) -> dict:
    if checker is None and settings.posting_rendered_fallback_provider in {
        "playwright_profile", "invisible_playwright_first",
    }:
        raise ValueError(
            "scheduled status reconciliation requires a cancellable "
            f"rendered resolver; {settings.posting_rendered_fallback_provider} is not supported"
        )
    if (
        limit < 1
        or max_runtime_seconds < 1
        or max_runtime_seconds > _MAX_SCAN_RUNTIME_SECONDS
        or rendered_limit < 0
    ):
        raise ValueError("invalid reconciliation limits")
    _require_status_reconciliation_schema(settings)
    run_started_at = _aware_utc(now or datetime.now(UTC))

    def observed_now() -> datetime:
        if observation_clock is not None:
            return _aware_utc(observation_clock())
        if now is not None:
            return run_started_at
        return datetime.now(UTC)

    deadline = time.monotonic() + max_runtime_seconds
    request = {
        "limit": limit,
        "rendered_limit": rendered_limit,
        "max_runtime_seconds": max_runtime_seconds,
        "job_id": job_id,
        "force": force,
        "dry_run": dry_run,
        "direct_concurrency": 4,
        "per_host_concurrency": 1,
        "rendered_concurrency": 2,
    }
    if dry_run:
        _assert_no_persisted_running_scan(settings)
        run_id = None
    else:
        run_id = _start_run(settings, request, run_started_at, max_runtime_seconds)
    counts = {
        key: 0
        for key in (
            "selected",
            "checked",
            "open",
            "closed",
            "expired",
            "inconclusive",
            "failed",
            "deferred",
            "skipped",
        )
    }
    try:
        targets = _select_targets(settings, run_started_at, limit, job_id, force)
        counts["selected"] = len(targets)
        if run_id:
            _record_run_selection(settings, run_id, len(targets))
        deferred_job_ids: set[str] = set()
        completed_job_ids: set[str] = set()

        def defer_target(
            target: JobStatusTarget,
            retry_not_before: datetime | None = None,
        ) -> None:
            if target.job_id in completed_job_ids or target.job_id in deferred_job_ids:
                return
            deferred_at = observed_now()
            target_is_current = (
                _persist_retry_deferral(
                    settings,
                    run_id,
                    target,
                    deferred_at,
                    retry_not_before=retry_not_before,
                )
                if run_id
                else _dry_run_target_is_current(settings, target)
            )
            if not target_is_current:
                counts["skipped"] += 1
                deferred_job_ids.add(target.job_id)
                return
            deferred_job_ids.add(target.job_id)
            counts["deferred"] += 1

        def record_observation(
            target: JobStatusTarget,
            observation: StatusObservation,
            *,
            deferred: bool = False,
        ) -> None:
            if target.job_id in completed_job_ids or target.job_id in deferred_job_ids:
                return
            decision = observation.decision
            bucket = "skipped" if decision.result == "unsupported" else decision.result
            counts["checked"] += 1
            counts[bucket] += 1
            if deferred:
                counts["deferred"] += 1
            target_is_current = (
                _persist_decision(
                    settings,
                    run_id,
                    target,
                    observation,
                    deferred=deferred,
                )
                if run_id
                else _dry_run_target_is_current(settings, target)
            )
            if not target_is_current:
                counts[bucket] -= 1
                if deferred:
                    counts["deferred"] -= 1
                counts["skipped"] += 1
            completed_job_ids.add(target.job_id)

        render_queue: list[RenderRequest] = []

        def defer_with_prior(
            target: JobStatusTarget,
            prior: StatusCheckDecision | None,
            retry_not_before: datetime | None,
        ) -> None:
            if prior is not None and _decision_precedence(prior) >= 2:
                record_observation(
                    target,
                    StatusObservation(
                        prior,
                        observed_now(),
                        retry_not_before,
                    ),
                    deferred=True,
                )
                return
            defer_target(target, retry_not_before)

        def queue_or_record_resolution(
            target: JobStatusTarget,
            resolution: StatusResolution,
            *,
            inherited_retry_not_before: datetime | None = None,
        ) -> None:
            observed_at = observed_now()
            retry_not_before = _latest_datetime(
                inherited_retry_not_before,
                _retry_not_before(resolution.retry_after_seconds, observed_at),
            )
            if isinstance(resolution, DeferredRequired):
                defer_with_prior(target, resolution.prior_decision, retry_not_before)
                return
            if not isinstance(resolution, RenderRequired):
                record_observation(
                    target,
                    StatusObservation(resolution, observed_at, retry_not_before),
                )
                return
            candidate_index, candidate = _render_candidate(target, resolution)
            render_queue.append(
                RenderRequest(
                    target,
                    candidate,
                    retry_not_before,
                    resolution.prior_decision,
                    max(resolution.resume_candidate_index, candidate_index + 1),
                )
            )

        def defer_render_request(request_item: RenderRequest) -> None:
            defer_with_prior(
                request_item.target,
                request_item.prior_decision,
                request_item.retry_not_before,
            )

        host_locks: dict[str, threading.Lock] = {}
        guard = threading.Lock()
        provider_feed_cache = _ProviderFeedCache()
        from jobcockpit.ingestion.scheduled_browser import (
            ScheduledInvisiblePlaywrightResolver, ScheduledSessionRecovery,
        )

        session_recovery = ScheduledSessionRecovery(
            cooldown_seconds=settings.invisible_playwright_prepare_cooldown_seconds,
        )
        def worker(target: JobStatusTarget) -> StatusResolution:
            deadline_decision = _passed_application_deadline_decision(
                target,
                observed_now(),
            )
            if deadline_decision is not None:
                return deadline_decision
            if checker is not None:
                return _run_injected_checker(checker, target)
            return _direct_target_check(
                target,
                settings,
                host_locks,
                guard,
                deadline=deadline,
                observation_clock=observed_now,
                provider_feed_cache=provider_feed_cache,
            )
        for offset in range(0, len(targets), _DIRECT_CONCURRENCY):
            batch = targets[offset : offset + _DIRECT_CONCURRENCY]
            if time.monotonic() >= deadline:
                for target in targets[offset:]:
                    defer_target(target)
                break
            outcomes = _run_worker_batch_until_deadline(
                batch,
                worker,
                deadline=deadline,
            )
            abandoned = False
            for outcome in outcomes:
                target = outcome.item
                if outcome.state is WorkerOutcomeState.ABANDONED:
                    abandoned = True
                    defer_target(target)
                    continue
                resolution = _worker_outcome_resolution(outcome, target)
                queue_or_record_resolution(target, resolution)
            if abandoned:
                for target in targets[offset + len(batch) :]:
                    defer_target(target)
                break

        if not checker:
            target_order = {target.job_id: index for index, target in enumerate(targets)}
            render_queue.sort(key=lambda item: target_order[item.target.job_id])
            render_cursor = 0
            rendered_used = 0
            while render_cursor < len(render_queue):
                if rendered_used >= rendered_limit:
                    for request_item in render_queue[render_cursor:]:
                        defer_render_request(request_item)
                    break
                if time.monotonic() >= deadline:
                    for request_item in render_queue[render_cursor:]:
                        defer_render_request(request_item)
                    break
                available_requests = rendered_limit - rendered_used
                batch_size = _scheduled_render_batch_size(
                    render_queue, render_cursor, available_requests
                )
                batch = render_queue[render_cursor : render_cursor + batch_size]
                render_cursor += batch_size
                rendered_used += batch_size
                lifecycles = {
                    request_item.target.job_id: _RenderedWorkerLifecycle() for request_item in batch
                }

                def render_worker(request_item: RenderRequest) -> StatusResolution:
                    lifecycle = lifecycles[request_item.target.job_id]
                    resolver = _rendered_resolver_for_remaining_budget(settings, deadline)
                    if isinstance(resolver, ScheduledInvisiblePlaywrightResolver):
                        resolver.session_recovery = session_recovery
                    may_render = lifecycle.attach(resolver)
                    try:
                        if not may_render:
                            return DeferredRequired(
                                "runtime_budget_exhausted",
                                request_item.candidate.url,
                            )
                        return _rendered_check_with_host_lock(
                            request_item.target,
                            request_item.candidate,
                            resolver,
                            host_locks,
                            guard,
                            deadline=deadline,
                            minimum_start_seconds=_minimum_rendered_start_seconds(settings),
                        )
                    finally:
                        with suppress(Exception):
                            lifecycle.finalize_worker()

                outcomes = _run_worker_batch_until_deadline(
                    batch,
                    render_worker,
                    deadline=deadline,
                )
                abandoned = False
                for outcome in outcomes:
                    if outcome.state is WorkerOutcomeState.ABANDONED:
                        abandoned = True
                        lifecycles[outcome.item.target.job_id].cancel()

                for lifecycle in lifecycles.values():
                    lifecycle.ensure_cleanup_owned()

                for outcome in outcomes:
                    request_item = outcome.item
                    if outcome.state is WorkerOutcomeState.ABANDONED:
                        defer_render_request(request_item)
                        continue
                    resolution = _worker_outcome_resolution(
                        outcome,
                        request_item.target,
                    )
                    observed_at = observed_now()
                    if isinstance(resolution, DeferredRequired):
                        defer_with_prior(
                            request_item.target,
                            request_item.prior_decision,
                            _latest_datetime(
                                request_item.retry_not_before,
                                _retry_not_before(
                                    resolution.retry_after_seconds,
                                    observed_at,
                                ),
                            ),
                        )
                        continue
                    if isinstance(resolution, RenderRequired):
                        resolution = StatusCheckDecision(
                            "failed",
                            "unexpected_nested_render_request",
                            request_item.candidate.url,
                            request_item.candidate.source_kind,
                            retryable=True,
                        )
                    resolution = _prefer_confirmable_decision(
                        resolution, request_item.prior_decision
                    )
                    _continue_after_render(
                        request_item,
                        resolution,
                        settings=settings,
                        deadline=deadline,
                        observation_clock=lambda: observed_at,
                        host_locks=host_locks,
                        host_locks_guard=guard,
                        provider_feed_cache=provider_feed_cache,
                        queue_or_record=queue_or_record_resolution,
                        record_observation=record_observation,
                        defer_with_prior=defer_with_prior,
                    )
                if abandoned:
                    for request_item in render_queue[render_cursor:]:
                        defer_render_request(request_item)
                    break
        status = (
            "dry_run"
            if dry_run
            else ("completed_with_warnings" if counts["failed"] else "completed")
        )
        summary = {"run_id": run_id, "status": status, **counts}
        if run_id:
            _finish_run(settings, run_id, summary, _completion_time(run_started_at))
        return summary
    except Exception as error:
        if not run_id:
            raise
        try:
            failure_summary = _fail_run(
                settings,
                run_id,
                error,
                _completion_time(run_started_at),
            )
        except Exception as persistence_error:
            failure_summary = {
                "run_id": run_id,
                "status": "failed",
                "selected": 0,
                "checked": 0,
                "open": 0,
                "closed": 0,
                "expired": 0,
                "inconclusive": 0,
                "failed": 0,
                "deferred": 0,
                "skipped": 0,
                "error": (
                    f"{sanitize_status_reconciliation_error(error)}; "
                    "failure_record_error: "
                    f"{sanitize_status_reconciliation_error(persistence_error)}"
                )[:400],
            }
        raise StatusReconciliationRunFailed(failure_summary) from error


def _scheduled_render_batch_size(
    render_queue: Sequence[RenderRequest],
    render_cursor: int,
    available_requests: int,
) -> int:
    batch_size = min(
        2,
        available_requests,
        len(render_queue) - render_cursor,
    )
    pending_batch = render_queue[render_cursor : render_cursor + batch_size]
    if any(
        request.candidate.source_kind == "workday"
        or posting_identity(request.candidate.url).source_kind == "workday"
        for request in pending_batch
    ):
        return 1
    return batch_size


def _select_targets(
    settings: Settings,
    now: datetime,
    limit: int,
    job_id: str | None,
    force: bool,
) -> list[JobStatusTarget]:
    with session_scope(settings) as db:
        latest_check_source_url = (
            select(JobStatusCheck.source_url)
            .where(JobStatusCheck.job_id == JobPosting.id)
            .order_by(
                JobStatusCheck.checked_at.desc(),
                JobStatusCheck.created_at.desc(),
                JobStatusCheck.status_check_id.desc(),
            )
            .limit(1)
            .correlate(JobPosting)
            .scalar_subquery()
        )
        applied_application_exists = (
            select(Application.application_id)
            .where(
                Application.job_id == JobPosting.id,
                or_(
                    Application.status == "applied",
                    Application.applied_at.is_not(None),
                ),
            )
            .correlate(JobPosting)
            .exists()
        )
        needs_publication_check = False
        if settings.publication_enabled:
            from jobcockpit.db.models import JobPublication
            from jobcockpit.services.publication_policy import load_policy

            publication_policy = load_policy(settings)
            if publication_policy is not None:
                needs_publication_check = select(JobPublication.publication_id).where(
                    JobPublication.job_id == JobPosting.id,
                    JobPublication.destination == publication_policy.endpoint,
                    or_(JobPublication.published_at.is_not(None), JobPublication.payload != {}),
                ).correlate(JobPosting).exists()
        query = select(JobPosting, latest_check_source_url).where(
            JobPosting.status == "active",
            or_(~applied_application_exists, needs_publication_check),
            text("json_extract(job_postings.source, '$.merged_into_job_id') IS NULL"),
        )
        if job_id:
            query = query.where(JobPosting.id == job_id)
        if not force:
            query = query.where(
                JobPosting.status_source_repair_required.is_(False),
                or_(
                    JobPosting.status_next_check_at.is_(None),
                    JobPosting.status_next_check_at <= now,
                )
            )
        passed_deadline_priority = case(
            (
                and_(
                    JobPosting.application_deadline.is_not(None),
                    func.length(JobPosting.application_deadline) == 10,
                    JobPosting.application_deadline < now.date().isoformat(),
                ),
                0,
            ),
            else_=1,
        )
        validation_priority = case(
            (
                or_(
                    JobPosting.status_check_result.is_(None),
                    JobPosting.status_check_result != "open",
                ),
                0,
            ),
            (
                or_(
                    JobPosting.status_last_checked_at.is_(None),
                    JobPosting.status_last_checked_at < now - timedelta(days=7),
                ),
                1,
            ),
            else_=2,
        )
        rows = db.execute(
            query.order_by(
                passed_deadline_priority,
                validation_priority,
                JobPosting.status_next_check_at.asc().nullsfirst(),
                JobPosting.created_at.asc(),
                JobPosting.id.asc(),
            ).limit(limit)
        ).all()
        return [_target_from_job(job, latest_source_url) for job, latest_source_url in rows]


def _target_from_job(job: JobPosting, last_check_source_url: str | None = None) -> JobStatusTarget:
    location = job.location if isinstance(job.location, dict) else {}
    role = job.role if isinstance(job.role, dict) else {}
    display_title = str(role.get("job_title") or "").strip()
    if not display_title:
        display_title = str(job.normalized_title or "").replace("_", " ").strip()
    return JobStatusTarget(
        job.id,
        job.status,
        job.source_type,
        job.source_url,
        str(job.company_name or "").strip(),
        display_title,
        ", ".join(
            str(location.get(key) or "").strip()
            for key in ("city", "province_state", "country")
            if str(location.get(key) or "").strip()
        ),
        ordered_status_urls(job),
        job.status_check_failure_count or 0,
        job.status_check_not_found_count or 0,
        _aware_utc(job.status_last_checked_at) if job.status_last_checked_at else None,
        last_check_source_url,
        _aware_utc(job.updated_at),
        application_deadline=getattr(job, "application_deadline", None),
        source_repair_required=bool(getattr(job, "status_source_repair_required", False)),
    )


def _run_injected_checker(
    checker: StatusChecker | None, target: JobStatusTarget
) -> StatusCheckDecision:
    if not target.candidates:
        return StatusCheckDecision("unsupported", "no_usable_stored_url")
    if checker is None:
        raise RuntimeError("missing checker")
    return checker(target, target.candidates[0])


def _run_worker_batch_until_deadline(
    items: list[_WorkItem],
    worker: Callable[[_WorkItem], StatusResolution],
    *,
    deadline: float,
) -> tuple[WorkerOutcome[_WorkItem], ...]:
    """Return terminal outcomes while abandoning non-cooperative workers safely.

    Workers never persist state. Once the deadline passes, their daemon thread
    may finish in the background, but its private result queue is discarded and
    the main thread records the item only as deferred.
    """

    if not items:
        return ()
    completed: queue.Queue[tuple[int, StatusResolution | Exception]] = queue.Queue()

    def invoke(index: int, item: _WorkItem) -> None:
        try:
            value: StatusResolution | Exception = worker(item)
        except Exception as error:
            value = error
        completed.put((index, value))

    for index, item in enumerate(items):
        threading.Thread(
            target=invoke,
            args=(index, item),
            name="jobcockpit-status-worker",
            daemon=True,
        ).start()

    outcomes: list[WorkerOutcome[_WorkItem]] = []
    finished_indexes: set[int] = set()
    while len(finished_indexes) < len(items):
        try:
            index, value = completed.get_nowait()
        except queue.Empty:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                index, value = completed.get(timeout=remaining)
            except queue.Empty:
                break
        finished_indexes.add(index)
        if isinstance(value, Exception):
            outcomes.append(WorkerOutcome(items[index], WorkerOutcomeState.FAILED, error=value))
        else:
            outcomes.append(
                WorkerOutcome(
                    items[index],
                    WorkerOutcomeState.COMPLETED,
                    resolution=value,
                )
            )

    outcomes.extend(
        WorkerOutcome(item, WorkerOutcomeState.ABANDONED)
        for index, item in enumerate(items)
        if index not in finished_indexes
    )
    return tuple(outcomes)


def _worker_outcome_resolution(
    outcome: WorkerOutcome[object],
    target: JobStatusTarget,
) -> StatusResolution:
    if outcome.state is WorkerOutcomeState.COMPLETED:
        if outcome.resolution is None:
            raise AssertionError("completed worker outcome lost its resolution")
        return outcome.resolution
    if outcome.state is WorkerOutcomeState.FAILED:
        if outcome.error is None:
            raise AssertionError("failed worker outcome lost its error")
        return StatusCheckDecision(
            "failed",
            "status_check_exception",
            target.source_url,
            error_message=_sanitize_error(outcome.error),
            retryable=True,
        )
    raise AssertionError("abandoned worker outcomes must be deferred")


def _direct_target_check(
    target: JobStatusTarget,
    settings: Settings,
    host_locks: dict[str, threading.Lock],
    guard: threading.Lock,
    *,
    deadline: float | None = None,
    observation_clock: ObservationClock | None = None,
    start_candidate_index: int = 0,
    prior_decision: StatusCheckDecision | None = None,
    prior_retry_after_seconds: int | None = None,
    provider_feed_cache: _ProviderFeedCache | None = None,
) -> StatusResolution:
    if not target.candidates:
        return StatusCheckDecision("unsupported", "no_usable_stored_url")
    best_nonterminal = prior_decision
    longest_retry_after = prior_retry_after_seconds
    for candidate_index, candidate in enumerate(
        target.candidates[start_candidate_index:],
        start=start_candidate_index,
    ):
        if deadline is not None and time.monotonic() >= deadline:
            return _with_retry_after(
                DeferredRequired(
                    "runtime_budget_exhausted",
                    candidate.url,
                    prior_decision=best_nonterminal,
                ),
                longest_retry_after,
            )
        candidate_identity = posting_identity(candidate.url)
        if candidate_identity.is_trusted_provider and not candidate_identity.is_detail:
            best_nonterminal = _prefer_confirmable_decision(
                StatusCheckDecision(
                    "unsupported",
                    "stored_url_not_job_detail",
                    candidate.url,
                    candidate.source_kind,
                ),
                best_nonterminal,
            )
            continue
        if candidate.source_kind in {"linkedin", "indeed"}:
            return _with_retry_after(
                RenderRequired(
                    "aggregator_render_required",
                    candidate.url,
                    candidate.source_kind,
                    prior_decision=best_nonterminal,
                    resume_candidate_index=candidate_index + 1,
                ),
                longest_retry_after,
            )
        decision = _direct_check(
            target,
            candidate,
            settings,
            deadline=deadline,
            observation_clock=observation_clock,
            host_locks=host_locks,
            host_locks_guard=guard,
            provider_feed_cache=provider_feed_cache,
        )
        longest_retry_after = _longest_retry_after(
            longest_retry_after,
            decision.retry_after_seconds,
        )
        if isinstance(decision, DeferredRequired):
            return _with_retry_after(
                replace(
                    decision,
                    prior_decision=best_nonterminal,
                ),
                longest_retry_after,
            )
        if isinstance(decision, RenderRequired):
            return _with_retry_after(
                replace(
                    decision,
                    prior_decision=(
                        _prefer_confirmable_decision(decision.prior_decision, best_nonterminal)
                        if decision.prior_decision is not None else best_nonterminal
                    ),
                    resume_candidate_index=candidate_index + 1,
                ),
                longest_retry_after,
            )
        if decision.result in {"open", "closed", "expired"}:
            return _with_retry_after(decision, longest_retry_after)
        best_nonterminal = _prefer_confirmable_decision(
            decision,
            best_nonterminal,
        )
    return _with_retry_after(
        best_nonterminal or StatusCheckDecision("unsupported", "no_usable_stored_url"),
        longest_retry_after,
    )


def _direct_check(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    settings: Settings,
    *,
    deadline: float | None = None,
    observation_clock: ObservationClock | None = None,
    host_locks: dict[str, threading.Lock] | None = None,
    host_locks_guard: threading.Lock | None = None,
    provider_feed_cache: _ProviderFeedCache | None = None,
) -> StatusResolution:
    candidate_identity = posting_identity(candidate.url)
    if candidate_identity.is_trusted_provider and not candidate_identity.is_detail:
        return StatusCheckDecision(
            "unsupported",
            "stored_url_not_job_detail",
            candidate.url,
            candidate.source_kind,
        )
    if candidate.source_kind in {"ashby", "recruitee"}:
        feed_decision = _public_provider_feed_check(
            target,
            candidate,
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
            feed_cache=provider_feed_cache,
        )
        if feed_decision is not None:
            return feed_decision
    if candidate.source_kind == "oracle":
        oracle_decision = _oracle_public_detail_check(
            target,
            candidate,
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
        if oracle_decision is not None:
            return oracle_decision
    if candidate.source_kind in {"oracle", "workday"}:
        with _host_request_context(candidate.url, host_locks, host_locks_guard):
            if deadline is not None and time.monotonic() >= deadline:
                return DeferredRequired(
                    "runtime_budget_exhausted",
                    candidate.url,
                )
            return _workday_check(
                target,
                candidate,
                settings,
                None,
                deadline=deadline,
                observation_clock=observation_clock,
            )
    try:
        response, final_url = _get_with_safe_redirects(
            candidate.url,
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
    except StatusRuntimeBudgetExhausted:
        return DeferredRequired(
            "runtime_budget_exhausted",
            candidate.url,
        )
    except UrlPolicyViolation as error:
        return _url_policy_decision(candidate, error.reason)
    except Exception as error:
        return StatusCheckDecision(
            "inconclusive",
            "direct_fetch_failed",
            candidate.url,
            candidate.source_kind,
            retryable=True,
            error_message=_sanitize_error(error),
        )
    body_is_inspectable = not response.extensions.get(_RESPONSE_UNSUPPORTED_ENCODING_EXTENSION)
    returned_url_matches = status_urls_match(candidate.url, final_url)
    if response.status_code != 200:
        terminal_url_allowed = returned_url_matches or _is_exact_http_to_https_upgrade(
            candidate.url, final_url
        )
        if not terminal_url_allowed:
            return _inconclusive(
                candidate,
                "redirect_identity_mismatch",
                final_url=final_url,
                diagnostics={"requested_identity_matches_final": False},
            )
        if not body_is_inspectable and response.status_code == 404:
            return _inconclusive(candidate, "response_content_encoding_unsupported")
        if body_is_inspectable:
            error_page_text = _visible_page_text(
                response.text,
                response.headers.get("Content-Type"),
            )
            if _looks_like_auth_wall(final_url, error_page_text):
                return _inconclusive(candidate, "authentication_wall")
        observed_at = _aware_utc(observation_clock()) if observation_clock else datetime.now(UTC)
        decision = classify_http_status(
            response.status_code,
            source_kind=candidate.source_kind,
            generic_404_count=(
                _eligible_generic_404_count(target, final_url, observed_at=observed_at)
                if response.status_code == 404
                else 0
            ),
            source_url=final_url,
            retry_after_seconds=_retry_after_seconds(
                response.headers.get("Retry-After"),
                observed_at=observed_at,
            ),
        )
        if decision.detected_status and _is_indeed_selected_result_url(final_url):
            return _inconclusive(candidate, "indeed_selected_result_unbound")
        return _guard_terminal_evidence_transport(decision)
    if response.extensions.get(_RESPONSE_UNSUPPORTED_ENCODING_EXTENSION):
        return _inconclusive(candidate, "response_content_encoding_unsupported")
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
    if response.extensions.get(_RESPONSE_TOO_LARGE_EXTENSION):
        fallback = _inconclusive(candidate, "response_too_large")
        if returned_url_matches and content_type in {"text/html", "application/xhtml+xml"}:
            return RenderRequired(
                "response_too_large_render_required", candidate.url, candidate.source_kind,
                http_status=200, prior_decision=fallback,
            )
        return fallback
    if content_type == "application/pdf":
        page_source = _pdf_posting_text(response.content, deadline=deadline)
        if page_source is None:
            return _inconclusive(candidate, "pdf_text_unavailable")
    else:
        page_source = response.text
    page_text = _visible_page_text(
        page_source,
        content_type,
    )
    if _looks_like_auth_wall(final_url, page_text):
        return _inconclusive(candidate, "authentication_wall")
    if _greenhouse_board_error_redirect(candidate.url, final_url):
        feed_decision = _public_provider_feed_check(
            target, candidate, settings, deadline=deadline,
            host_locks=host_locks, host_locks_guard=host_locks_guard,
            feed_cache=provider_feed_cache,
        )
        if feed_decision is not None:
            return feed_decision
    if _bamboohr_detail_redirect_indicates_removal(candidate.url, final_url):
        return StatusCheckDecision(
            "closed",
            "ats_no_longer_available",
            final_url,
            "bamboohr",
            detected_status="closed",
            http_status=200,
        )
    returned_page_identity_allowed = returned_url_matches or _can_use_redirected_page_identity(
        candidate.url, final_url
    )
    response_is_html = _looks_like_html_page(
        page_source,
        content_type,
    )
    explicit = (
        None
        if response_is_html
        else infer_status_from_page(
            final_url,
            page_source,
            expected_company=target.company,
            expected_title=target.title,
        )
    )
    if explicit:
        if returned_page_identity_allowed:
            return explicit
        return _inconclusive(
            candidate,
            "redirect_identity_mismatch",
            final_url=final_url,
            diagnostics={"requested_identity_matches_final": False},
        )
    if response_is_html:
        visible_terminal = infer_status_from_page(
            final_url,
            page_text,
            evidence_scope="rendered_page",
            expected_company=target.company,
            expected_title=target.title,
        )
        if visible_terminal:
            if not returned_page_identity_allowed:
                return _inconclusive(
                    candidate,
                    "redirect_identity_mismatch",
                    final_url=final_url,
                    diagnostics={"requested_identity_matches_final": False},
                )
            if visible_terminal.detected_status is None:
                return visible_terminal
            return RenderRequired(
                "html_terminal_marker_render_required",
                candidate.url,
                candidate.source_kind,
                http_status=200,
            )
    if not returned_page_identity_allowed:
        return _inconclusive(
            candidate,
            "redirect_identity_mismatch",
            final_url=final_url,
            diagnostics={"requested_identity_matches_final": False},
        )
    if not _posting_url_supports_open_evidence(final_url):
        return _inconclusive(candidate, "posting_url_not_detail")
    structured_identities = (
        _json_ld_job_posting_identities(response.text) if response_is_html else ()
    )
    open_graph_identity = (
        _open_graph_job_identity(response.text)
        if response_is_html and candidate.source_kind == "bamboohr"
        else None
    )
    if structured_identities:
        identity_matches = any(
            _structured_identity_values_match(
                target.company,
                target.title,
                returned_company=company,
                returned_title=title,
            )
            for title, company in structured_identities
        )
    elif open_graph_identity:
        title, company = open_graph_identity
        identity_matches = _bamboohr_open_graph_identity_matches(
            target.company,
            target.title,
            returned_company=company,
            returned_title=title,
        )
    else:
        identity_matches = _identity_matches(target, page_text)
    if not identity_matches:
        # Observed ADP, UltiPro and Cornerstone responses contain only a script
        # shell. Missing content needs rendering, not a source-repair mismatch.
        if (
            response_is_html
            and not structured_identities
            and re.search(r"<script\b", page_source, re.IGNORECASE)
            and page_text.strip().casefold() in {"", "recruitment"}
        ):
            return RenderRequired(
                "javascript_shell_render_required", candidate.url, candidate.source_kind,
                http_status=200,
            )
        return _inconclusive(
            candidate,
            "job_identity_mismatch" if returned_url_matches else "redirect_identity_mismatch",
            final_url=final_url,
            diagnostics={
                "requested_identity_matches_final": returned_url_matches,
                "posting_content_identity_matches": False,
            },
        )
    return StatusCheckDecision(
        "open",
        "posting_identity_match",
        final_url,
        candidate.source_kind,
        http_status=200,
        requested_url=candidate.url,
        final_url=final_url,
        candidate_kind=candidate.kind,
        diagnostics={"posting_content_identity_matches": True},
    )


def _pdf_posting_text(content: bytes, *, deadline: float | None) -> str | None:
    if len(content) > _MAX_RESPONSE_BYTES:
        return None
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted or len(reader.pages) > 20:
            return None
        parts: list[str] = []
        extracted_length = 0
        for page in reader.pages:
            if deadline is not None and time.monotonic() >= deadline:
                return None
            text = page.extract_text() or ""
            extracted_length += len(text)
            if extracted_length > 100_000:
                return None
            parts.append(text)
        result = "\n".join(parts).strip()
        return result or None
    except Exception:
        return None


def _greenhouse_board_error_redirect(expected: str, returned: str) -> bool:
    identity = posting_identity(expected)
    original, final = urlparse(expected), urlparse(returned)
    return bool(
        identity.source_kind == "greenhouse" and identity.is_detail
        and original.scheme == final.scheme == "https"
        and original.netloc == final.netloc
        and original.username is None and original.password is None
        and _path_parts(final.path) == (identity.posting_scope,)
        and httpx.QueryParams(final.query).get_list("error") == ["true"]
    )


def _public_provider_feed_check(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    settings: Settings,
    *,
    deadline: float | None = None,
    host_locks: dict[str, threading.Lock] | None = None,
    host_locks_guard: threading.Lock | None = None,
    feed_cache: _ProviderFeedCache | None = None,
) -> StatusResolution | None:
    identity = posting_identity(candidate.url)
    if not identity.is_detail or not identity.posting_id:
        return None
    parsed = urlparse(candidate.url)
    path_parts = _path_parts(parsed.path)
    if candidate.source_kind == "greenhouse":
        if not identity.is_detail or not identity.posting_scope or parsed.scheme != "https":
            return None
        feed_url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(identity.posting_scope, safe='')}/jobs"
        )
        collection_key = "jobs"
        identity_key = "id"
        evidence_code = "greenhouse_public_feed_match"
    elif candidate.source_kind == "ashby":
        if len(path_parts) < 2:
            return None
        feed_url = (
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{quote(path_parts[0], safe='')}"
        )
        collection_key = "jobs"
        identity_key = "id"
        evidence_code = "ashby_public_feed_match"
    elif candidate.source_kind == "recruitee":
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        if not hostname or parsed.scheme.casefold() != "https":
            return None
        feed_url = f"https://{hostname}/api/offers"
        collection_key = "offers"
        identity_key = "slug"
        evidence_code = "recruitee_public_feed_match"
    else:
        return None
    feed_lock = feed_cache.lock_for(feed_url) if feed_cache is not None else nullcontext()
    with feed_lock:
        feed_index = feed_cache.get(feed_url) if feed_cache is not None else None
        if feed_index is None:
            try:
                response, final_url = _get_with_safe_redirects(
                    feed_url,
                    settings,
                    deadline=deadline,
                    host_locks=host_locks,
                    host_locks_guard=host_locks_guard,
                    max_response_bytes=_MAX_PROVIDER_FEED_RESPONSE_BYTES,
                )
            except StatusRuntimeBudgetExhausted:
                return DeferredRequired("runtime_budget_exhausted", candidate.url)
            except (UrlPolicyViolation, httpx.HTTPError):
                return None
            if response.status_code != 200 or final_url != feed_url:
                return None
            if response.extensions.get(_RESPONSE_TOO_LARGE_EXTENSION):
                return _inconclusive(candidate, "response_too_large")
            if response.extensions.get(_RESPONSE_UNSUPPORTED_ENCODING_EXTENSION):
                return _inconclusive(candidate, "response_content_encoding_unsupported")
            try:
                payload = response.json()
            except ValueError:
                return None
            payload_entries = payload.get(collection_key) if isinstance(payload, dict) else None
            if not isinstance(payload_entries, list) or not all(
                isinstance(entry, dict) for entry in payload_entries
            ):
                return None
            entry_identities = [
                str(entry.get(identity_key) or "").strip() for entry in payload_entries
            ]
            if candidate.source_kind == "greenhouse":
                meta = payload.get("meta")
                total = meta.get("total") if isinstance(meta, dict) else None
                if (
                    type(total) is not int or total != len(payload_entries)
                    or len(set(entry_identities)) != len(entry_identities)
                    or any(not re.fullmatch(r"[0-9]+", value) for value in entry_identities)
                ):
                    return _inconclusive(candidate, "provider_feed_incomplete")
            if any(not identifier for identifier in entry_identities):
                return _inconclusive(candidate, "provider_feed_identity_missing")
            feed_index = {
                identifier: str(entry.get("title") or "").strip()
                for identifier, entry in zip(
                    entry_identities,
                    payload_entries,
                    strict=True,
                )
            }
            if feed_cache is not None:
                feed_cache.put(feed_url, feed_index)
    if identity.posting_id not in feed_index:
        return StatusCheckDecision(
            "closed",
            "ats_no_longer_available",
            candidate.url,
            candidate.source_kind,
            detected_status="closed",
            http_status=200,
        )
    returned_title = feed_index[identity.posting_id]
    if returned_title and not _title_values_match(target.title, returned_title):
        return _inconclusive(candidate, "job_identity_mismatch")
    return StatusCheckDecision(
        "open",
        evidence_code,
        candidate.url,
        candidate.source_kind,
        http_status=200,
    )


def _oracle_public_detail_check(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    settings: Settings,
    *,
    deadline: float | None = None,
    host_locks: dict[str, threading.Lock] | None = None,
    host_locks_guard: threading.Lock | None = None,
) -> StatusResolution | None:
    try:
        detail_urls = oracle_candidate_experience_detail_urls(candidate.url)
    except Exception:
        return None
    if not detail_urls:
        return None
    valid_empty_payload_count = 0
    for detail_url in detail_urls:
        try:
            response, final_url = _get_with_safe_redirects(
                detail_url,
                settings,
                deadline=deadline,
                host_locks=host_locks,
                host_locks_guard=host_locks_guard,
            )
        except StatusRuntimeBudgetExhausted:
            return DeferredRequired("runtime_budget_exhausted", candidate.url)
        except (UrlPolicyViolation, httpx.HTTPError):
            return None
        if response.status_code != 200 or final_url != detail_url:
            continue
        if response.extensions.get(_RESPONSE_TOO_LARGE_EXTENSION):
            return _inconclusive(candidate, "response_too_large")
        if response.extensions.get(_RESPONSE_UNSUPPORTED_ENCODING_EXTENSION):
            return _inconclusive(candidate, "response_content_encoding_unsupported")
        try:
            payload = response.json()
        except ValueError:
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            continue
        if not items:
            valid_empty_payload_count += 1
            continue
        item = items[0]
        returned_id = str(
            item.get("RequisitionNumber") or item.get("Id") or item.get("RequisitionId") or ""
        ).strip()
        returned_title = str(item.get("Title") or "").strip()
        expected_id = posting_identity(candidate.url).posting_id or ""
        if not _structured_provider_job_ids_match("oracle", expected_id, returned_id):
            return _inconclusive(candidate, "job_identity_mismatch")
        if not _title_values_match(target.title, returned_title):
            return _inconclusive(candidate, "job_identity_mismatch")
        return StatusCheckDecision(
            "open",
            "oracle_structured_job_match",
            candidate.url,
            "oracle",
            http_status=200,
        )
    if valid_empty_payload_count == len(detail_urls):
        return StatusCheckDecision(
            "closed",
            "ats_no_longer_available",
            candidate.url,
            "oracle",
            detected_status="closed",
            http_status=200,
        )
    return None


def _bamboohr_detail_redirect_indicates_removal(expected: str, returned: str) -> bool:
    expected_identity = posting_identity(expected)
    if expected_identity.source_kind != "bamboohr" or not expected_identity.is_detail:
        return False
    expected_url = urlparse(expected)
    returned_url = urlparse(returned)
    if not _provider_identity_redirect_transport_is_safe(expected, returned):
        return False
    if (expected_url.hostname or "").casefold() != (returned_url.hostname or "").casefold():
        return False
    return tuple(part.casefold() for part in _path_parts(returned_url.path)) == ("careers",)


def _workday_check(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    settings: Settings,
    policy: UrlFetchPolicy | None,
    *,
    deadline: float | None = None,
    observation_clock: ObservationClock | None = None,
) -> StatusResolution:
    structured_source_kind = candidate.source_kind
    mismatch_evidence = (
        "workday_job_identity_mismatch"
        if structured_source_kind == "workday"
        else "job_identity_mismatch"
    )
    open_evidence = (
        "workday_structured_job_match"
        if structured_source_kind == "workday"
        else "oracle_structured_job_match"
    )
    timeout_seconds = _remaining_timeout_seconds(
        settings.posting_direct_fetch_timeout_seconds,
        deadline,
    )
    timeout_was_deadline_limited = deadline is not None and timeout_seconds < float(
        settings.posting_direct_fetch_timeout_seconds
    )
    http_client = _BoundedStatusHttpClient(
        timeout_seconds,
        deadline=deadline,
        allowed_localhosts=settings.allowed_localhost_urls(),
    )
    resolver_policy = getattr(http_client, "url_policy", None) or policy
    if resolver_policy is None:
        resolver_policy = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls())
    resolver = AtsApplyPageResolver(
        http_client=http_client,
        timeout=timeout_seconds,
        url_policy=resolver_policy,
    )
    try:
        job = resolver.resolve(candidate.url)
        archive = job.source_archive
        resolved_url = job.source_url
        if _looks_like_auth_wall(resolved_url, job.raw_text):
            return _inconclusive(candidate, "authentication_wall")
        if not _structured_workday_identity_matches(target, candidate, job):
            return _inconclusive(candidate, mismatch_evidence)
        return StatusCheckDecision(
            "open",
            open_evidence,
            resolved_url,
            structured_source_kind,
            http_status=archive.status_code if archive else 200,
        )
    except StatusRuntimeBudgetExhausted:
        return DeferredRequired(
            "runtime_budget_exhausted",
            candidate.url,
        )
    except StatusUnsupportedContentEncodingError:
        return StatusCheckDecision(
            "inconclusive",
            "response_content_encoding_unsupported",
            candidate.url,
            structured_source_kind,
            retryable=True,
        )
    except Exception as error:
        if policy_reason := _url_policy_reason_from_error(error):
            return _url_policy_decision(candidate, policy_reason)
        failure = classify_link_failure(candidate.url, error)
        if timeout_was_deadline_limited and failure.upstream_timed_out:
            return DeferredRequired(
                "runtime_budget_exhausted",
                candidate.url,
            )
        if failure.upstream_response_too_large:
            return StatusCheckDecision(
                "inconclusive",
                "response_too_large",
                candidate.url,
                structured_source_kind,
                retryable=True,
            )
        if failure.upstream_status_code == 403 and structured_source_kind == "workday":
            return RenderRequired(
                "workday_403_render_required",
                candidate.url,
                "workday",
                retry_after_seconds=failure.retry_after_seconds,
                http_status=403,
            )
        if failure.upstream_status_code is not None:
            failure_text = _visible_page_text(failure.upstream_response_text or "")
            if _looks_like_auth_wall(candidate.url, failure_text):
                return StatusCheckDecision(
                    "inconclusive",
                    "authentication_wall",
                    candidate.url,
                    structured_source_kind,
                    http_status=failure.upstream_status_code,
                    retryable=True,
                    retry_after_seconds=failure.retry_after_seconds,
                )
            observed_at = (
                _aware_utc(observation_clock()) if observation_clock else datetime.now(UTC)
            )

            return _guard_terminal_evidence_transport(
                classify_http_status(
                    failure.upstream_status_code,
                    source_kind=structured_source_kind,
                    generic_404_count=(
                        _eligible_generic_404_count(
                            target,
                            candidate.url,
                            observed_at=observed_at,
                        )
                        if failure.upstream_status_code == 404
                        else 0
                    ),
                    source_url=candidate.url,
                    retry_after_seconds=failure.retry_after_seconds,
                )
            )
        return StatusCheckDecision(
            "inconclusive",
            "direct_fetch_failed",
            candidate.url,
            structured_source_kind,
            retryable=True,
            error_message=_sanitize_error(error),
        )
    finally:
        resolver.close()
        http_client.close()


def _get_with_safe_redirects(
    source_url: str,
    settings: Settings,
    *,
    deadline: float | None = None,
    host_locks: dict[str, threading.Lock] | None = None,
    host_locks_guard: threading.Lock | None = None,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
) -> tuple[httpx.Response, str]:
    current_url = source_url
    timeout_seconds = _remaining_timeout_seconds(
        settings.posting_direct_fetch_timeout_seconds,
        deadline,
    )
    client = PinnedHttpClient(
        timeout=timeout_seconds,
        allowed_localhosts=settings.allowed_localhost_urls(),
        follow_redirects=False,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "JobCockpit-Status-Reconciliation/1.0",
        },
    )
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if deadline is not None and time.monotonic() >= deadline:
                raise StatusRuntimeBudgetExhausted("status reconciliation runtime budget exhausted")
            policy_result = client.url_policy.check(current_url)
            if not policy_result.allowed:
                raise UrlPolicyViolation(current_url, policy_result.reason)
            with _host_request_context(current_url, host_locks, host_locks_guard):
                if deadline is not None and time.monotonic() >= deadline:
                    raise StatusRuntimeBudgetExhausted(
                        "status reconciliation runtime budget exhausted"
                    )
                request_timeout = _remaining_timeout_seconds(
                    settings.posting_direct_fetch_timeout_seconds,
                    deadline,
                )
                timeout_was_deadline_limited = deadline is not None and request_timeout < float(
                    settings.posting_direct_fetch_timeout_seconds
                )
                client.timeout = request_timeout
                try:
                    response = _bounded_http_get(
                        client,
                        current_url,
                        deadline=deadline,
                        max_response_bytes=max_response_bytes,
                    )
                except httpx.TimeoutException as error:
                    if timeout_was_deadline_limited:
                        raise StatusRuntimeBudgetExhausted(str(error)) from error
                    raise
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response, current_url
            location = response.headers.get("Location")
            if not location:
                return response, current_url
            current_url = urljoin(current_url, location)
        raise RuntimeError("redirect_limit_exceeded")
    finally:
        client.close()


def _bounded_http_get(
    client,
    source_url: str,
    *,
    deadline: float | None = None,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
    **request_kwargs,
) -> httpx.Response:
    request_headers = httpx.Headers(request_kwargs.get("headers"))
    request_headers["Accept-Encoding"] = "identity"
    request_kwargs["headers"] = request_headers
    _raise_if_runtime_deadline_expired(deadline)
    stream = getattr(client, "stream", None)
    if not callable(stream):
        response = client.get(source_url, **request_kwargs)
        _raise_if_runtime_deadline_expired(deadline)
        return _bounded_buffered_response(response, max_response_bytes=max_response_bytes)
    with stream("GET", source_url, **request_kwargs) as response:
        if response.status_code in {301, 302, 303, 307, 308}:
            return _copy_http_response(response, b"", too_large=False)
        if _unsupported_content_encoding(response.headers.get("Content-Encoding")):
            return _copy_http_response(
                response,
                b"",
                too_large=False,
                unsupported_encoding=True,
            )
        content_length = _content_length(response.headers.get("Content-Length"))
        declared_too_large = content_length is not None and content_length > max_response_bytes
        if declared_too_large and response.status_code < 400:
            return _copy_http_response(response, b"", too_large=True)
        content = bytearray()
        deadline_reached = threading.Event()
        deadline_timer: threading.Timer | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StatusRuntimeBudgetExhausted("status reconciliation runtime budget exhausted")

            def close_at_deadline() -> None:
                deadline_reached.set()
                with suppress(Exception):
                    response.close()

            deadline_timer = threading.Timer(remaining, close_at_deadline)
            deadline_timer.daemon = True
            deadline_timer.start()
        too_large = declared_too_large
        chunks = iter(response.iter_raw())
        try:
            while True:
                _raise_if_runtime_deadline_expired(deadline, deadline_reached)
                try:
                    chunk = next(chunks)
                except StopIteration:
                    _raise_if_runtime_deadline_expired(deadline, deadline_reached)
                    break
                except Exception as error:
                    if deadline_reached.is_set() or (
                        deadline is not None and time.monotonic() >= deadline
                    ):
                        raise StatusRuntimeBudgetExhausted(
                            "status reconciliation runtime budget exhausted"
                        ) from error
                    raise
                _raise_if_runtime_deadline_expired(deadline, deadline_reached)
                remaining = max_response_bytes - len(content)
                if len(chunk) > remaining:
                    content.extend(chunk[:remaining])
                    too_large = True
                    break
                content.extend(chunk)
            return _copy_http_response(response, bytes(content), too_large=too_large)
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()


def _raise_if_runtime_deadline_expired(
    deadline: float | None,
    deadline_reached: threading.Event | None = None,
) -> None:
    if (deadline_reached is not None and deadline_reached.is_set()) or (
        deadline is not None and time.monotonic() >= deadline
    ):
        raise StatusRuntimeBudgetExhausted("status reconciliation runtime budget exhausted")


def _bounded_buffered_response(
    response: httpx.Response,
    *,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
) -> httpx.Response:
    if _unsupported_content_encoding(response.headers.get("Content-Encoding")):
        return _copy_http_response(
            response,
            b"",
            too_large=False,
            unsupported_encoding=True,
        )
    content = response.content
    if len(content) <= max_response_bytes:
        return response
    return _copy_http_response(
        response,
        content[:max_response_bytes],
        too_large=True,
    )


def _copy_http_response(
    response: httpx.Response,
    content: bytes,
    *,
    too_large: bool,
    unsupported_encoding: bool = False,
) -> httpx.Response:
    extensions = {
        _RESPONSE_TOO_LARGE_EXTENSION: too_large,
        _RESPONSE_UNSUPPORTED_ENCODING_EXTENSION: unsupported_encoding,
    }
    headers = httpx.Headers(response.headers)
    for header_name in ("Content-Encoding", "Content-Length", "Transfer-Encoding"):
        if header_name in headers:
            del headers[header_name]
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=content,
        request=response.request,
        extensions=extensions,
    )


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _unsupported_content_encoding(value: str | None) -> bool:
    return bool(value and value.strip().casefold() not in {"identity"})


class _BoundedStatusHttpClient:
    def __init__(
        self,
        timeout_seconds: float,
        *,
        deadline: float | None = None,
        allowed_localhosts: set[str] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._deadline = deadline
        self._client = PinnedHttpClient(
            timeout=timeout_seconds,
            allowed_localhosts=allowed_localhosts,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )
        self.url_policy = self._client.url_policy

    def get(self, source_url: str, **request_kwargs) -> httpx.Response:
        request_kwargs.setdefault(
            "timeout",
            _remaining_timeout_seconds(self._timeout_seconds, self._deadline),
        )
        response = _bounded_http_get(
            self._client,
            source_url,
            deadline=self._deadline,
            **request_kwargs,
        )
        if response.is_success and response.extensions.get(_RESPONSE_TOO_LARGE_EXTENSION):
            request = response.request or httpx.Request("GET", source_url)
            raise AtsResponseTooLargeError(
                "status response exceeded the configured byte limit",
                request=request,
            )
        if response.is_success and response.extensions.get(
            _RESPONSE_UNSUPPORTED_ENCODING_EXTENSION
        ):
            request = response.request or httpx.Request("GET", source_url)
            raise StatusUnsupportedContentEncodingError(
                "status response used an unsupported content encoding",
                request=request,
            )
        return response

    def close(self) -> None:
        self._client.close()


def _url_policy_decision(
    candidate: StatusUrlCandidate,
    reason: str,
) -> StatusCheckDecision:
    if reason == "dns_resolution_failed":
        return StatusCheckDecision(
            "inconclusive",
            "url_policy_dns_resolution_failed",
            candidate.url,
            candidate.source_kind,
            retryable=True,
        )
    return StatusCheckDecision(
        "unsupported",
        f"url_policy_{reason}",
        candidate.url,
        candidate.source_kind,
    )


def _url_policy_reason_from_error(error: BaseException) -> str | None:
    marker = "url is blocked by policy:"
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        marker_index = message.casefold().find(marker)
        if marker_index >= 0:
            return message[marker_index + len(marker) :].strip().casefold()
        current = current.__cause__ or current.__context__
    return None


def _finalize_rendered_resolver(resolver: object | None) -> int:
    finalization_lock = getattr(resolver, "cleanup_finalization_lock", None)
    lock_context = finalization_lock if hasattr(finalization_lock, "__enter__") else nullcontext()
    with lock_context:
        cancel_active_pages = getattr(resolver, "cancel_active_pages", None)
        if callable(cancel_active_pages):
            with suppress(Exception):
                cancel_active_pages()
        return retain_pending_browser_cleanup(resolver)


def _rendered_check_with_host_lock(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    resolver,
    host_locks: dict[str, threading.Lock],
    host_locks_guard: threading.Lock,
    *,
    deadline: float | None = None,
    resolver_factory=None,
    minimum_start_seconds: float = 0,
) -> StatusCheckDecision | DeferredRequired:
    hostname = (urlparse(candidate.url).hostname or "").casefold()
    with host_locks_guard:
        host_lock = host_locks.setdefault(hostname, threading.Lock())
    if deadline is None:
        acquired = host_lock.acquire()
    else:
        remaining = deadline - time.monotonic()
        acquired = remaining > 0 and host_lock.acquire(timeout=remaining)
    if not acquired:
        return DeferredRequired(
            "runtime_budget_exhausted",
            candidate.url,
        )
    try:
        if deadline is not None and (
            deadline - time.monotonic() <= 0 or deadline - time.monotonic() < minimum_start_seconds
        ):
            return DeferredRequired(
                "runtime_budget_exhausted",
                candidate.url,
            )
        if resolver_factory is not None:
            resolver = resolver_factory()
        if resolver is None:
            return StatusCheckDecision(
                "unsupported",
                "rendered_resolver_unavailable",
                candidate.url,
                candidate.source_kind,
            )
        return _rendered_check(target, candidate, resolver)
    finally:
        host_lock.release()


def _rendered_check(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    resolver,
) -> StatusCheckDecision:
    hint = ExternalPostingHint(
        source_url=candidate.url,
        source_kind=candidate.source_kind,
        company=target.company,
        title=target.title,
        location=target.location,
    )
    try:
        # Availability owns the identity/status decision below. Load the guarded
        # page without imposing ingestion's literal-title confidence threshold.
        load_page = getattr(resolver, "load_page", None)
        resolution = normalize_posting_resolution(
            load_page(hint) if callable(load_page) else resolver.resolve(hint)
        )
    except Exception as error:
        return StatusCheckDecision(
            "inconclusive",
            "rendered_check_failed",
            candidate.url,
            candidate.source_kind,
            retryable=True,
            error_message=_sanitize_error(error),
        )
    # Classify resolver codes, not words in an embedded URL or provider message.
    error = _URL_IN_ERROR.sub("", resolution.error).casefold()
    if resolution.status != "resolved" and re.search(
        r"\b(?:(?:(?:rendered|playwright|chrome_cdp|codex_browser)_)?url_policy_blocked"
        r"|rendered_subresource_blocked)\b",
        error,
    ):
        return StatusCheckDecision(
            "failed",
            "rendered_url_policy_blocked",
            candidate.url,
            candidate.source_kind,
            retryable=True,
            error_message=_sanitize_error(resolution.error),
            requested_url=candidate.url,
            candidate_kind=candidate.kind,
        )
    authentication_detected = resolution.status != "resolved" and _rendered_error_is_auth_wall(
        error
    )
    postings = ([resolution.posting] if resolution.posting else []) + list(resolution.candidates)
    terminal_decision: StatusCheckDecision | None = None
    open_decision: StatusCheckDecision | None = None
    truncated_decision: StatusCheckDecision | None = None
    untrusted_decision: StatusCheckDecision | None = None
    terminal_redirect_mismatch = False
    for posting in postings:
        if posting is None:
            continue
        returned_url_matches = status_urls_match(candidate.url, posting.source_url)
        redirect_identity_fallback_allowed = (
            returned_url_matches
            or _can_use_redirected_page_identity(
                candidate.url,
                posting.source_url,
            )
        )
        if posting.evidence_origin != "browser_dom":
            untrusted_decision = untrusted_decision or StatusCheckDecision(
                "inconclusive",
                "rendered_evidence_untrusted",
                posting.source_url,
                candidate.source_kind,
                retryable=True,
            )
            continue
        if not redirect_identity_fallback_allowed:
            terminal_redirect_mismatch = True
            continue
        posting_matches_identity = _identity_matches(target, posting.raw_text) or (
            returned_url_matches
            and _linkedin_rendered_title_matches(target, posting.source_url, posting.raw_text)
        )
        if _looks_like_auth_wall(posting.source_url, posting.raw_text):
            authentication_detected = True
            continue
        status_text = posting.complete_body_text if posting.content_truncated else posting.raw_text
        explicit = infer_status_from_page(
            posting.source_url,
            status_text,
            evidence_scope="rendered_page",
            expected_company=target.company,
            expected_title=target.title,
        )
        if explicit:
            if not returned_url_matches:
                terminal_redirect_mismatch = True
                continue
            terminal_decision = terminal_decision or explicit
            continue
        if posting.content_truncated:
            truncated_decision = truncated_decision or StatusCheckDecision(
                "inconclusive",
                "rendered_content_truncated",
                posting.source_url,
                candidate.source_kind,
                retryable=True,
            )
            continue
        if (
            resolution.status == "resolved"
            and _posting_url_supports_open_evidence(posting.source_url)
            and posting_matches_identity
        ):
            open_decision = open_decision or StatusCheckDecision(
                "open",
                "rendered_posting_identity_match",
                posting.source_url,
                candidate.source_kind,
                http_status=200,
            )
    if authentication_detected:
        return replace(
            _inconclusive(candidate, "authentication_wall"),
            error_message=_sanitize_error(resolution.error) if resolution.error else None,
        )
    if terminal_decision is not None:
        return terminal_decision
    if open_decision is not None:
        return open_decision
    if terminal_redirect_mismatch:
        return _inconclusive(candidate, "redirect_identity_mismatch")
    if truncated_decision is not None:
        return truncated_decision
    if untrusted_decision is not None:
        return untrusted_decision
    return StatusCheckDecision(
        "inconclusive",
        "rendered_identity_inconclusive",
        candidate.url,
        candidate.source_kind,
        retryable=True,
        error_message=_sanitize_error(resolution.error) if resolution.error else None,
    )


def _persist_decision(
    settings: Settings,
    run_id: str,
    target: JobStatusTarget,
    observation: StatusObservation,
    *,
    deferred: bool = False,
) -> bool:
    decision = observation.decision
    observed_at = _aware_utc(observation.observed_at)
    before = "active"
    after = decision.detected_status or before
    failure_count = (
        0
        if decision.result in {"open", "closed", "expired", "unsupported"}
        else target.failure_count + 1
    )
    identity_repair_evidence = decision.evidence_code in SOURCE_REPAIR_EVIDENCE_CODES
    not_found_count = target.not_found_count
    if decision.evidence_code == "generic_http_404_pending_confirmation":
        same_source_as_previous = bool(
            target.last_check_source_url
            and decision.source_url
            and status_urls_match(
                target.last_check_source_url,
                decision.source_url,
            )
        )
        not_found_count = target.not_found_count + 1 if same_source_as_previous else 1
    elif decision.evidence_code != "generic_http_404_confirmed":
        not_found_count = 0
    delay = retry_delay(decision.result, target.failure_count)
    retry_not_before = (
        observation.retry_not_before if decision.result in {"inconclusive", "failed"} else None
    )
    next_check_at = _latest_datetime(
        observed_at + delay if delay is not None else None,
        retry_not_before,
    )
    with session_scope(settings) as db:
        run = _require_running_scan(db, run_id)
        previous_identity_mismatches = 0
        if identity_repair_evidence and decision.source_url:
            recent_checks = db.execute(
                select(JobStatusCheck.evidence_code, JobStatusCheck.source_url)
                .where(JobStatusCheck.job_id == target.job_id)
                .order_by(
                    JobStatusCheck.checked_at.desc(),
                    JobStatusCheck.created_at.desc(),
                    JobStatusCheck.status_check_id.desc(),
                )
                .limit(2)
            ).all()
            for evidence_code, source_url in recent_checks:
                if (
                    evidence_code not in SOURCE_REPAIR_EVIDENCE_CODES
                    or not source_url
                    or not status_urls_match(decision.source_url, source_url)
                ):
                    break
                previous_identity_mismatches += 1
        source_repair_required = (
            False
            if decision.result in {"open", "closed", "expired"}
            else True
            if (
                decision.evidence_code == "stored_url_not_job_detail"
                and bool(decision.source_url)
                and job_portal_kind(decision.source_url or "")
                not in {None, "linkedin", "indeed"}
            )
            or (
                identity_repair_evidence
                and target.failure_count >= 2
                and previous_identity_mismatches >= 2
            )
            else target.source_repair_required
        )
        if source_repair_required:
            next_check_at = None
        posting_values: dict[str, object] = {
            "status": after,
            "status_last_checked_at": observed_at,
            "status_next_check_at": next_check_at,
            "status_check_result": decision.result,
            "status_check_evidence_code": decision.evidence_code,
            "status_check_failure_count": failure_count,
            "status_check_not_found_count": not_found_count,
            "status_source_repair_required": source_repair_required,
            "updated_at": observed_at,
        }
        if decision.result == "open":
            posting_values.update(
                status_last_open_at=observed_at,
                status_last_open_evidence_code=decision.evidence_code,
            )
        update_result = db.execute(
            update(JobPosting)
            .where(*_target_update_predicates(target))
            .values(**posting_values)
        )
        if update_result.rowcount != 1:
            run.checked_count += 1
            run.skipped_count += 1
            return False
        if after != "active":
            job = db.get(JobPosting, target.job_id)
            if job is not None:
                cancel_active_cover_letter_generations(
                    job,
                    db,
                    error_code="job_not_active",
                    error_message="The job is no longer active",
                )
        db.add(
            JobStatusCheck(
                status_check_id=new_id("status_check"),
                status_scan_run_id=run_id,
                job_id=target.job_id,
                source_url=decision.source_url,
                source_kind=decision.source_kind,
                requested_url=decision.requested_url,
                final_url=decision.final_url,
                candidate_kind=decision.candidate_kind,
                diagnostics=decision.diagnostics,
                result=decision.result,
                detected_status=decision.detected_status,
                evidence_code=decision.evidence_code,
                http_status=decision.http_status,
                retryable=decision.retryable,
                status_before=before,
                status_after=after,
                error_message=(
                    _sanitize_error(decision.error_message) if decision.error_message else None
                ),
                checked_at=observed_at,
            )
        )
        if after != before:
            db.add(
                AuditLogEntry.create(
                    "job.opportunity_status_updated",
                    "job_posting",
                    target.job_id,
                    payload={
                        "previous_status": before,
                        "status": after,
                        "source": "scheduled_status_check",
                        "status_scan_run_id": run_id,
                        "evidence_code": decision.evidence_code,
                    },
                )
            )
        run.checked_count += 1
        bucket = "skipped" if decision.result == "unsupported" else decision.result
        setattr(run, f"{bucket}_count", getattr(run, f"{bucket}_count") + 1)
        if deferred:
            run.deferred_count += 1
        return True


def _persist_retry_deferral(
    settings: Settings,
    run_id: str,
    target: JobStatusTarget,
    deferred_at: datetime,
    *,
    retry_not_before: datetime | None = None,
) -> bool:
    retry_at = _latest_datetime(
        _aware_utc(deferred_at) + _BUDGET_DEFERRAL_DELAY,
        retry_not_before,
    )
    with session_scope(settings) as db:
        run = _require_running_scan(db, run_id)
        update_result = db.execute(
            update(JobPosting)
            .where(*_target_update_predicates(target))
            .values(
                status_next_check_at=case(
                    (
                        or_(
                            JobPosting.status_next_check_at.is_(None),
                            JobPosting.status_next_check_at < retry_at,
                        ),
                        retry_at,
                    ),
                    else_=JobPosting.status_next_check_at,
                )
            )
        )
        if update_result.rowcount != 1:
            run.skipped_count += 1
            return False
        run.deferred_count += 1
        return True


def _target_update_predicates(target: JobStatusTarget) -> tuple:
    return (
        JobPosting.id == target.job_id,
        JobPosting.status == "active",
        text("json_extract(job_postings.source, '$.merged_into_job_id') IS NULL"),
        (
            JobPosting.updated_at == target.selected_updated_at
            if target.selected_updated_at is not None
            else JobPosting.updated_at.is_(None)
        ),
    )


def _dry_run_target_is_current(
    settings: Settings,
    target: JobStatusTarget,
) -> bool:
    with session_scope(settings) as db:
        current_job_id = db.execute(
            select(JobPosting.id).where(*_target_update_predicates(target))
        ).scalar_one_or_none()
        return current_job_id is not None


def _require_running_scan(db, run_id: str) -> JobStatusScanRun:
    run = db.get(JobStatusScanRun, run_id)
    if run is None or run.status != "running":
        raise StatusScanLeaseLostError(
            f"status reconciliation run {run_id} no longer holds the singleton lease"
        )
    return run


def _record_run_selection(
    settings: Settings,
    run_id: str,
    selected_count: int,
) -> None:
    with session_scope(settings) as db:
        run = _require_running_scan(db, run_id)
        run.selected_count = selected_count


def _assert_no_persisted_running_scan(settings: Settings) -> None:
    with session_scope(settings) as db:
        running_id = db.execute(
            select(JobStatusScanRun.status_scan_run_id).where(JobStatusScanRun.status == "running")
        ).scalar_one_or_none()
        if running_id:
            raise StatusScanAlreadyRunningError(
                f"status reconciliation run {running_id} is already running"
            )


def _start_run(
    settings: Settings,
    request: dict,
    now: datetime,
    max_runtime_seconds: int,
) -> str:
    with session_scope(settings) as db:
        running = db.execute(
            select(JobStatusScanRun).where(JobStatusScanRun.status == "running")
        ).scalar_one_or_none()
        if running:
            running_request = (
                running.request_payload if isinstance(running.request_payload, dict) else {}
            )
            running_max_runtime = _positive_int(
                running_request.get("max_runtime_seconds"),
                max_runtime_seconds,
            )
            running_max_runtime = min(running_max_runtime, _MAX_SCAN_RUNTIME_SECONDS)
            stale_after = timedelta(seconds=running_max_runtime + 600)
            if _aware_utc(running.started_at) + stale_after > now:
                raise StatusScanAlreadyRunningError(
                    f"status reconciliation run {running.status_scan_run_id} is already running"
                )
            running.status = "abandoned"
            running.finished_at = now
            running.error_message = "stale_run_recovered"
            running.updated_at = now
            db.flush()
        run_id = new_id("status_scan")
        db.add(
            JobStatusScanRun(
                status_scan_run_id=run_id,
                status="running",
                started_at=now,
                request_payload=request,
            )
        )
        try:
            db.flush()
        except IntegrityError as error:
            raise StatusScanAlreadyRunningError(
                "another status reconciliation run is already running"
            ) from error
        return run_id


def _finish_run(settings: Settings, run_id: str, summary: dict, now: datetime) -> None:
    with session_scope(settings) as db:
        run = _require_running_scan(db, run_id)
        run.status = summary["status"]
        run.finished_at = now
        for key in (
            "selected",
            "checked",
            "open",
            "closed",
            "expired",
            "inconclusive",
            "failed",
            "deferred",
            "skipped",
        ):
            setattr(run, f"{key}_count", summary[key])
        run.updated_at = now


def _fail_run(
    settings: Settings,
    run_id: str,
    error: Exception,
    now: datetime,
) -> dict:
    with session_scope(settings) as db:
        run = db.get(JobStatusScanRun, run_id)
        if run is None:
            raise RuntimeError("status reconciliation run disappeared")
        if run.status == "running":
            run.status = "failed"
            run.finished_at = now
            run.error_message = sanitize_status_reconciliation_error(error)
            run.updated_at = now
            db.flush()
        return {
            "run_id": run.status_scan_run_id,
            "status": run.status,
            "selected": run.selected_count,
            "checked": run.checked_count,
            "open": run.open_count,
            "closed": run.closed_count,
            "expired": run.expired_count,
            "inconclusive": run.inconclusive_count,
            "failed": run.failed_count,
            "deferred": run.deferred_count,
            "skipped": run.skipped_count,
            "error": (run.error_message or sanitize_status_reconciliation_error(error)),
        }


def _require_status_reconciliation_schema(settings: Settings) -> None:
    inspector = inspect(get_engine(settings))
    table_names = set(inspector.get_table_names())
    missing_tables = sorted(_STATUS_SCHEMA_TABLES - table_names)
    missing_job_columns: list[str] = []
    missing_check_columns: list[str] = []
    if "job_postings" in table_names:
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        missing_job_columns = sorted(_STATUS_SCHEMA_JOB_COLUMNS - job_columns)
    else:
        missing_tables.append("job_postings")
    if "job_status_checks" in table_names:
        check_columns = {
            column["name"] for column in inspector.get_columns("job_status_checks")
        }
        missing_check_columns = sorted(_STATUS_SCHEMA_CHECK_COLUMNS - check_columns)
    if not missing_tables and not missing_job_columns and not missing_check_columns:
        return
    details = []
    if missing_tables:
        details.append(f"missing tables: {', '.join(sorted(set(missing_tables)))}")
    if missing_job_columns:
        details.append(f"missing job columns: {', '.join(missing_job_columns)}")
    if missing_check_columns:
        details.append(f"missing status-check columns: {', '.join(missing_check_columns)}")
    raise RuntimeError(
        "Job status reconciliation schema is not installed; run "
        "`pnpm --silent python scripts/migrate.py` before reconciliation"
        f" ({'; '.join(details)})"
    )


def _structured_workday_identity_matches(
    target: JobStatusTarget,
    candidate: StatusUrlCandidate,
    job: object,
) -> bool:
    expected_job_id = posting_identity(candidate.url).posting_id
    returned_job_id = str(getattr(job, "provider_job_id", "") or "").strip()
    return bool(
        expected_job_id
        and returned_job_id
        and _structured_provider_job_ids_match(
            candidate.source_kind,
            expected_job_id,
            returned_job_id,
        )
        and _title_values_match(
            target.title,
            str(getattr(job, "title", "") or ""),
        )
    )


def _structured_provider_job_ids_match(
    source_kind: str,
    expected: str,
    returned: str,
) -> bool:
    expected_folded = expected.casefold()
    returned_folded = returned.casefold()
    if expected_folded == returned_folded:
        return True
    if source_kind == "workday" and re.fullmatch(
        rf"{re.escape(returned_folded)}-\d+",
        expected_folded,
    ):
        return True
    if source_kind == "oracle":
        def numeric_oracle_id(value: str) -> str:
            if value.startswith("j") and value[1:].isascii() and value[1:].isdigit():
                return value[1:]
            return value

        return numeric_oracle_id(expected_folded) == numeric_oracle_id(returned_folded)
    return False


def _identity_matches(target: JobStatusTarget, page_text: str) -> bool:
    return _identity_values_match(target.company, target.title, page_text)


def _linkedin_rendered_title_matches(
    target: JobStatusTarget, source_url: str, page_text: str,
) -> bool:
    identity = posting_identity(source_url)
    if identity.source_kind != "linkedin" or not identity.is_detail or not identity.posting_id:
        return False
    # This first line is the browser's document title, not a title supplied by
    # the hint or one found in recommendations. The caller also requires the
    # same posting URL. Exact token equality permits the observed Intern/Co-op
    # order and invisible spacing while retaining every role and year token.
    title = page_text.partition("\n")[0]
    match = re.fullmatch(r"Page Title: (.+?)\s+\|\s+(.+?)\s+\|\s+LinkedIn", title)
    if not match:
        return False
    observed_title, observed_company = match.groups()
    expected_tokens = _identity_tokens(target.title)
    expected_company = _normalized_company_identity(target.company)
    return bool(
        expected_tokens and expected_tokens == _identity_tokens(observed_title)
        and expected_company and expected_company == _normalized_company_identity(observed_company)
    )


def _identity_values_match(
    company_value: str | None,
    title_value: str | None,
    page_text: str,
) -> bool:
    normalized = _normalize_title_identity_text(page_text)
    title = _normalize_title_identity_text(title_value or "")
    companies = _company_identity_alternatives(company_value or "")
    normalized_company_text = _normalized_company_identity(page_text)
    company_matches = any(
        f" {company} " in f" {normalized_company_text} " for company in companies
    )
    # The observed saved title omits '&' between Learning and Development.
    # Preserve every substantive word and their order when comparing the phrase.
    title_without_conjunction = _without_title_conjunction(title)
    title_matches = bool(title and (
        f" {title} " in f" {normalized} "
        or (title_without_conjunction and f" {title_without_conjunction} "
            in f" {_without_title_conjunction(normalized)} ")
    ))
    if title and companies:
        return title_matches and company_matches
    return bool(
        title_matches or company_matches
    )


def _without_title_conjunction(value: str) -> str:
    return " ".join(token for token in value.split() if token != "and")


def _company_identity_alternatives(value: str) -> tuple[str, ...]:
    parts = re.split(r"\s+/\s+", value)
    if len(parts) == 1:
        normalized = _normalized_company_identity(value)
        return (normalized,) if normalized else ()
    return tuple(
        normalized for part in parts
        if len(normalized := _normalized_company_identity(part)) >= 3
    )


def _structured_identity_values_match(
    company_value: str | None,
    title_value: str | None,
    *,
    returned_company: str,
    returned_title: str,
) -> bool:
    comparisons: list[bool] = []
    if title_value:
        comparisons.append(
            not _title_values_have_conflicting_numeric_tokens(
                title_value,
                returned_title,
            )
            and _title_values_match(title_value, returned_title)
        )
    if company_value:
        expected_company = _normalized_company_identity(company_value)
        actual_company = _normalized_company_identity(returned_company)
        comparisons.append(
            bool(expected_company and actual_company and expected_company == actual_company)
        )
    return bool(comparisons) and all(comparisons)


def _bamboohr_open_graph_identity_matches(
    company_value: str | None,
    title_value: str | None,
    *,
    returned_company: str,
    returned_title: str,
) -> bool:
    comparisons: list[bool] = []
    if title_value:
        comparisons.append(
            not _title_values_have_conflicting_numeric_tokens(
                title_value,
                returned_title,
            )
            and _title_values_match(title_value, returned_title)
        )
    if company_value:
        expected_tokens = set(_normalized_company_identity(company_value).split())
        returned_tokens = set(_normalized_company_identity(returned_company).split())
        comparisons.append(
            bool(
                expected_tokens
                and returned_tokens
                and (
                    expected_tokens <= returned_tokens
                    or returned_tokens <= expected_tokens
                )
            )
        )
    return bool(comparisons) and all(comparisons)


def _title_values_have_conflicting_numeric_tokens(
    expected: str | None,
    returned: str | None,
) -> bool:
    expected_numbers = {
        token for token in _identity_tokens(expected or "") if token.isdigit()
    }
    returned_numbers = {
        token for token in _identity_tokens(returned or "") if token.isdigit()
    }
    return bool(
        expected_numbers - returned_numbers and returned_numbers - expected_numbers
    )


def _title_values_match(expected: str | None, returned: str | None) -> bool:
    expected_tokens = _identity_tokens(expected or "")
    returned_tokens = _identity_tokens(returned or "")
    if not expected_tokens or not returned_tokens:
        return False
    if expected_tokens == returned_tokens:
        return True
    shared = expected_tokens & returned_tokens
    shorter = min(len(expected_tokens), len(returned_tokens))
    return len(shared) >= 3 and len(shared) / shorter >= 0.75


def _identity_tokens(value: str) -> set[str]:
    return set(_normalize_title_identity_text(value).split())


def _normalized_company_identity(value: str) -> str:
    suffixes = {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "plc",
        "ulc",
    }
    dotted_suffixes = {"llc", "llp", "lp", "plc", "ulc"}
    tokens = _normalize_identity_text(value).split()
    while tokens:
        if tokens[-1] in suffixes:
            tokens.pop()
            continue

        removed_dotted_suffix = False
        for suffix_length in (3, 2):
            trailing_tokens = tokens[-suffix_length:]
            if (
                len(trailing_tokens) == suffix_length
                and all(len(token) == 1 for token in trailing_tokens)
                and "".join(trailing_tokens) in dotted_suffixes
            ):
                del tokens[-suffix_length:]
                removed_dotted_suffix = True
                break
        if not removed_dotted_suffix:
            break

    return " ".join(tokens)


def _normalize_identity_text(value: str) -> str:
    normalized = value.casefold().replace("_", " ").replace("&", " and ")
    return " ".join(re.sub(r"[^\w+#]+", " ", normalized).split())


def _normalize_title_identity_text(value: str) -> str:
    normalized = _normalize_identity_text(value)
    normalized = re.sub(r"\bco\s+op\b", "coop", normalized)
    normalized = re.sub(r"\binternships?\b", "intern", normalized)
    return normalized


def _posting_url_supports_open_evidence(source_url: str) -> bool:
    identity = posting_identity(source_url)
    if identity.source_kind == "indeed" and _is_indeed_selected_result_url(source_url):
        return False
    return posting_url_supports_open_evidence(source_url)


def _is_indeed_selected_result_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    path_parts = tuple(part.casefold() for part in _path_parts(parsed.path))
    query_params = httpx.QueryParams(parsed.query)
    query_id = str(query_params.get("jk") or query_params.get("vjk") or "").strip()
    if not hostname_matches_indeed(parsed.hostname) or not _INDEED_POSTING_ID.fullmatch(query_id):
        return False
    if path_parts and path_parts[-1] == "viewjob":
        return False
    if path_parts in {
        ("rc", "clk"),
        ("rc", "clk", "dl"),
        ("pagead", "clk"),
        ("pagead", "clk", "dl"),
    }:
        return False
    if len(path_parts) == 2 and path_parts[0] == "job":
        return False
    return (
        path_parts == ("jobs",)
        or (
            len(path_parts) == 1
            and path_parts[0].startswith("q-")
            and path_parts[0].endswith("-jobs.html")
        )
        or any(part in {"job", "jobs"} for part in path_parts)
    )


def _indeed_selected_result_expired_decision(
    source_url: str,
    page_text: str,
    *,
    expected_company: str | None,
    expected_title: str | None,
) -> StatusCheckDecision | None:
    if not _is_indeed_selected_result_url(source_url):
        return None
    selected_detail = _indeed_selected_detail_section(page_text)
    if (
        selected_detail is None
        or not _indeed_selected_header_identity_matches(
            expected_company, expected_title, selected_detail
        )
        or not _contains_standalone_terminal_marker(
            selected_detail,
            (_INDEED_EXPIRED,),
        )
    ):
        return None
    return StatusCheckDecision(
        "expired",
        "indeed_job_expired",
        source_url,
        "indeed",
        "expired",
        200,
    )


_INDEED_SELECTED_HEADER_BOUNDARIES = frozenset(
    {"profile insights", "job details", "benefits", "full job description"}
)
_INDEED_SELECTED_HEADER_MAX_LINES = 24
_INDEED_SELECTED_HEADER_MAX_CHARS = 4_000


def _indeed_selected_header_identity_matches(
    expected_company: str | None,
    expected_title: str | None,
    selected_header: str,
) -> bool:
    company_tokens = _identity_phrase_tokens(expected_company or "")
    title_tokens = _identity_phrase_tokens(expected_title or "")
    if not company_tokens or not title_tokens:
        return False
    header_lines = [
        _identity_phrase_tokens(line) for line in selected_header.splitlines() if line.strip()
    ]
    expired_tokens = _identity_phrase_tokens(_INDEED_EXPIRED)
    try:
        expired_index = header_lines.index(expired_tokens)
    except ValueError:
        return False
    remaining_lines = header_lines[expired_index + 1 :]
    while remaining_lines and _is_indeed_expired_reason_line(remaining_lines[0]):
        remaining_lines.pop(0)
    if not remaining_lines:
        return False
    title_line = remaining_lines.pop(0)
    if title_line[-2:] != ("job", "post") or title_line[:-2] != title_tokens:
        return False
    while remaining_lines and remaining_lines[0] in _INDEED_SELECTED_IMAGE_ALT_TOKEN_LINES:
        remaining_lines.pop(0)
    return bool(remaining_lines and remaining_lines[0] == company_tokens)


def _identity_phrase_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w#+]+", value.casefold()))


def _is_indeed_expired_reason_line(line_tokens: tuple[str, ...]) -> bool:
    return line_tokens[:3] == ("reasons", "could", "include")


_INDEED_SELECTED_IMAGE_ALT_TOKEN_LINES = frozenset(
    {
        ("image",),
        ("company", "logo"),
        ("image", "company", "logo"),
    }
)


def _indeed_selected_detail_section(page_text: str) -> str | None:
    lines = [
        " ".join(line.split())
        for line in _visible_page_text(page_text).splitlines()
        if line.strip()
    ]
    for start_index, line in enumerate(lines):
        if line.casefold().strip(" .:-") != "job post details":
            continue
        header_lines: list[str] = []
        header_chars = 0
        for candidate_line in lines[start_index + 1 :]:
            folded = candidate_line.casefold().strip(" .:-")
            if (
                folded.startswith("return to search")
                or folded in _INDEED_SELECTED_HEADER_BOUNDARIES
            ):
                return "\n".join(header_lines) if header_lines else None
            if len(header_lines) >= _INDEED_SELECTED_HEADER_MAX_LINES:
                return None
            next_chars = header_chars + len(candidate_line)
            if header_lines:
                next_chars += 1
            if next_chars > _INDEED_SELECTED_HEADER_MAX_CHARS:
                return None
            header_lines.append(candidate_line)
            header_chars = next_chars
        return None
    return None


def _contains_standalone_terminal_marker(
    page_text: str,
    markers: tuple[str, ...],
) -> bool:
    normalized_markers = {
        " ".join(marker.casefold().split()).strip(" \t\r\n.!:;,-|") for marker in markers
    }
    for line in page_text.splitlines():
        normalized_line = " ".join(line.casefold().split()).strip(" \t\r\n.!:;,-|")
        if normalized_line in normalized_markers:
            return True
    return False


def _looks_like_html_page(
    raw_text: str,
    content_type: str | None = None,
) -> bool:
    return "html" in str(content_type or "").casefold() or bool(
        re.search(
            r"</?(?:!doctype|html|head|body|script|style|main|article|section|"
            r"div|span|p|h[1-6]|dialog|details|select|option)\b",
            raw_text[:1_000_000],
            re.I,
        )
    )


def _visible_page_text(raw_text: str, content_type: str | None = None) -> str:
    value = raw_text[:1_000_000]
    looks_like_html = _looks_like_html_page(value, content_type)
    if not looks_like_html:
        return value
    soup = BeautifulSoup(value, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for element in soup.find_all(["script", "style", "template", "noscript", "svg", "head"]):
        element.decompose()
    for element in soup.find_all(["button", "datalist", "option", "select", "textarea"]):
        element.decompose()
    for element in soup.find_all(
        lambda tag: (
            (tag.name == "dialog" and not tag.has_attr("open"))
            or (tag.name == "details" and not tag.has_attr("open"))
            or tag.has_attr("popover")
        )
    ):
        element.decompose()
    for element in soup.find_all(
        lambda tag: (
            tag.has_attr("hidden")
            or str(tag.get("aria-hidden") or "").casefold() == "true"
            or bool(
                re.search(
                    r"(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)"
                    r"|opacity\s*:\s*0(?:\.0+)?(?:\s*[;}])?)",
                    str(tag.get("style") or ""),
                    re.I,
                )
            )
            or bool(
                {str(class_name).casefold() for class_name in tag.get("class", [])}
                & {"hidden", "d-none", "sr-only", "visually-hidden"}
            )
        )
    ):
        element.decompose()
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for element in soup.find_all("br"):
        element.replace_with("\n")
    for element in soup.find_all(
        [
            "address",
            "article",
            "aside",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "p",
            "section",
            "td",
            "th",
            "tr",
        ]
    ):
        element.insert_before("\n")
        element.insert_after("\n")
    body = "\n".join(
        normalized
        for line in soup.get_text(" ", strip=False).splitlines()
        if (normalized := " ".join(line.split()))
    )
    return "\n".join(part for part in (title, body) if part)[:1_000_000]


def _json_ld_job_posting_identities(raw_text: str) -> tuple[tuple[str, str], ...]:
    soup = BeautifulSoup(raw_text[:1_000_000], "html.parser")
    identities: list[tuple[str, str]] = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        content = script.string or script.get_text() or ""
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        items: list[object]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            items = payload["@graph"]
        else:
            items = [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_types = item.get("@type")
            if isinstance(item_types, list):
                is_job_posting = any(str(value) == "JobPosting" for value in item_types)
            else:
                is_job_posting = str(item_types) == "JobPosting"
            if not is_job_posting:
                continue
            organization = item.get("hiringOrganization")
            company = (
                str(organization.get("name") or "").strip()
                if isinstance(organization, dict)
                else str(organization or "").strip()
            )
            title = str(item.get("title") or "").strip()
            if title and company:
                identities.append((title, company))
    return tuple(identities)


def _open_graph_job_identity(raw_text: str) -> tuple[str, str] | None:
    soup = BeautifulSoup(raw_text[:1_000_000], "html.parser")
    values: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        property_name = str(meta.get("property") or "").strip().casefold()
        if property_name not in {"og:title", "og:site_name"}:
            continue
        content = str(meta.get("content") or "").strip()
        if content:
            values[property_name] = content
    title = values.get("og:title", "")
    company = values.get("og:site_name", "")
    return (title, company) if title and company else None


def status_urls_match(expected: str, returned: str) -> bool:
    if _status_url_comparison_key(expected) == _status_url_comparison_key(returned):
        return True
    expected_identity = posting_identity(expected)
    returned_identity = posting_identity(returned)
    return bool(
        _provider_identity_redirect_transport_is_safe(expected, returned)
        and expected_identity.is_trusted_provider
        and expected_identity.is_detail
        and returned_identity.is_trusted_provider
        and returned_identity.is_detail
        and expected_identity.source_kind == returned_identity.source_kind
        and expected_identity.posting_id
        and expected_identity.posting_id == returned_identity.posting_id
        and expected_identity.posting_scope
        and expected_identity.posting_scope == returned_identity.posting_scope
    )


def _provider_identity_redirect_transport_is_safe(
    expected: str,
    returned: str,
) -> bool:
    expected_url = urlparse(expected)
    returned_url = urlparse(returned)
    if (
        expected_url.username is not None
        or expected_url.password is not None
        or returned_url.username is not None
        or returned_url.password is not None
    ):
        return False
    expected_scheme = expected_url.scheme.casefold()
    returned_scheme = returned_url.scheme.casefold()
    if not (
        expected_scheme == returned_scheme
        or (expected_scheme == "http" and returned_scheme == "https")
    ):
        return False
    try:
        expected_port = expected_url.port
        returned_port = returned_url.port
    except ValueError:
        return False
    expected_default = 443 if expected_scheme == "https" else 80
    returned_default = 443 if returned_scheme == "https" else 80
    return expected_port in {None, expected_default} and returned_port in {None, returned_default}


def _can_use_redirected_page_identity(expected: str, returned: str) -> bool:
    expected_identity = posting_identity(expected)
    returned_identity = posting_identity(returned)
    if expected_identity.is_trusted_provider or returned_identity.is_trusted_provider:
        return False

    expected_url = urlparse(expected)
    returned_url = urlparse(returned)
    expected_host = (expected_url.hostname or "").rstrip(".").casefold()
    returned_host = (returned_url.hostname or "").rstrip(".").casefold()
    if not expected_host or expected_host != returned_host:
        return False
    if expected_url.username is not None or returned_url.username is not None:
        return False
    expected_scheme = expected_url.scheme.casefold()
    returned_scheme = returned_url.scheme.casefold()
    if not (
        expected_scheme == returned_scheme
        or (expected_scheme == "http" and returned_scheme == "https")
    ):
        return False
    try:
        expected_port = expected_url.port
        returned_port = returned_url.port
    except ValueError:
        return False
    expected_default_port = 443 if expected_scheme == "https" else 80
    returned_default_port = 443 if returned_scheme == "https" else 80
    if expected_port not in {None, expected_default_port} or returned_port not in {
        None,
        returned_default_port,
    }:
        return False

    expected_parts = _path_parts(expected_url.path)
    returned_parts = _path_parts(returned_url.path)
    if len(expected_parts) < 2 or len(returned_parts) < 2:
        return False
    if any(part.casefold() in _AUTH_OR_SEARCH_ROUTE_SEGMENTS for part in returned_parts):
        return False
    detail_token = expected_parts[-1]
    return (
        detail_token == returned_parts[-1]
        and detail_token.casefold() not in _RESERVED_ROUTE_SEGMENTS
        and detail_token.casefold() not in _AUTH_OR_SEARCH_ROUTE_SEGMENTS
    )


def _is_exact_http_to_https_upgrade(expected: str, returned: str) -> bool:
    expected_url = urlparse(expected)
    returned_url = urlparse(returned)
    if (
        expected_url.username is not None
        or expected_url.password is not None
        or returned_url.username is not None
        or returned_url.password is not None
    ):
        return False
    if expected_url.scheme.casefold() != "http" or returned_url.scheme.casefold() != "https":
        return False
    expected_host = (expected_url.hostname or "").rstrip(".").casefold()
    returned_host = (returned_url.hostname or "").rstrip(".").casefold()
    if not expected_host or expected_host != returned_host:
        return False
    try:
        expected_port = expected_url.port
        returned_port = returned_url.port
    except ValueError:
        return False
    return bool(
        expected_port in {None, 80}
        and returned_port in {None, 443}
        and expected_url.path == returned_url.path
        and expected_url.params == returned_url.params
        and expected_url.query == returned_url.query
    )


def _terminal_evidence_transport_is_safe(source_url: str) -> bool:
    parsed = urlparse(source_url)
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        return False
    scheme = parsed.scheme.casefold()
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _guard_terminal_evidence_transport(
    decision: StatusCheckDecision,
) -> StatusCheckDecision:
    if (
        decision.detected_status is None
        or not decision.source_url
        or _terminal_evidence_transport_is_safe(decision.source_url)
    ):
        return decision
    return StatusCheckDecision(
        "inconclusive",
        "terminal_evidence_insecure_transport",
        decision.source_url,
        decision.source_kind,
        http_status=decision.http_status,
        retryable=True,
        retry_after_seconds=decision.retry_after_seconds,
    )


def _looks_like_auth_wall(source_url: str, page_text: str) -> bool:
    normalized = " ".join(page_text.casefold().split())
    identity = posting_identity(source_url)
    route_is_wall = not identity.is_detail and any(
        part.casefold() in _AUTH_OR_SEARCH_ROUTE_SEGMENTS
        for part in _path_parts(urlparse(source_url).path)
    )
    return route_is_wall or any(marker in normalized for marker in _AUTH_MARKERS)


def _rendered_error_is_auth_wall(error: str) -> bool:
    # "blocked" also occurs in our own URL/network policy failures. Only
    # explicit site-access codes establish an authentication/verification wall.
    return bool(re.search(
        r"\b(?:auth_wall_unresolved|blocked_rendered_page|invisible_session_sign_in_required"
        r"|codex_playwright_auth_wall)\b",
        error,
    ))


def _inconclusive(
    candidate: StatusUrlCandidate,
    evidence_code: str,
    *,
    final_url: str | None = None,
    diagnostics: dict[str, object] | None = None,
) -> StatusCheckDecision:
    return StatusCheckDecision(
        "inconclusive",
        evidence_code,
        candidate.url,
        candidate.source_kind,
        retryable=True,
        requested_url=candidate.url,
        final_url=final_url,
        candidate_kind=candidate.kind,
        diagnostics=diagnostics,
    )


def _eligible_generic_404_count(
    target: JobStatusTarget,
    source_url: str,
    *,
    observed_at: datetime | None = None,
) -> int:
    if not target.last_checked_at:
        return 0
    current = _aware_utc(observed_at or datetime.now(UTC))
    if current - target.last_checked_at < _GENERIC_404_CONFIRMATION_DELAY:
        return 0
    if not target.last_check_source_url or not status_urls_match(
        target.last_check_source_url, source_url
    ):
        return 0
    return target.not_found_count


def _retry_after_seconds(
    value: str | None,
    *,
    observed_at: datetime | None = None,
) -> int | None:
    if not value:
        return None
    try:
        return _bounded_retry_after_seconds(int(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (OverflowError, TypeError, ValueError):
            return None
        current = _aware_utc(observed_at or datetime.now(UTC))
        return _bounded_retry_after_seconds(int((_aware_utc(retry_at) - current).total_seconds()))


def sanitize_status_reconciliation_error(value: object) -> str:
    return _sanitize_error(value)


def _sanitize_error(value: object) -> str:
    return _URL_IN_ERROR.sub("[url]", " ".join(str(value).split()))[:400]


def _target_version_matches(job: JobPosting, target: JobStatusTarget) -> bool:
    if target.selected_updated_at is None:
        return True
    return _aware_utc(job.updated_at) == _aware_utc(target.selected_updated_at)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _completion_time(started_at: datetime) -> datetime:
    return max(datetime.now(UTC), _aware_utc(started_at))


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _bounded_retry_after_seconds(value: int) -> int:
    return min(max(0, value), _MAX_RETRY_AFTER_SECONDS)


def _longest_retry_after(
    current: int | None,
    candidate: int | None,
) -> int | None:
    values = [
        _bounded_retry_after_seconds(value) for value in (current, candidate) if value is not None
    ]
    return max(values) if values else None


def _retry_not_before(
    retry_after_seconds: int | None,
    observed_at: datetime,
) -> datetime | None:
    if retry_after_seconds is None:
        return None
    return observed_at + timedelta(seconds=_bounded_retry_after_seconds(retry_after_seconds))


def _latest_datetime(
    first: datetime | None,
    second: datetime | None,
) -> datetime | None:
    values = [_aware_utc(value) for value in (first, second) if value is not None]
    return max(values) if values else None


def _with_retry_after(
    decision: StatusResolution,
    retry_after_seconds: int | None,
) -> StatusResolution:
    longest = _longest_retry_after(
        decision.retry_after_seconds,
        retry_after_seconds,
    )
    if longest == decision.retry_after_seconds:
        return decision
    return replace(decision, retry_after_seconds=longest)


def _render_candidate(
    target: JobStatusTarget,
    resolution: RenderRequired,
) -> tuple[int, StatusUrlCandidate]:
    for index, candidate in enumerate(target.candidates):
        if candidate.url == resolution.source_url:
            return index, candidate
    raise ValueError("render request source URL is not a stored job URL candidate")


def _continue_after_render(
    request_item: RenderRequest,
    rendered_decision: StatusCheckDecision,
    *,
    settings: Settings,
    deadline: float,
    observation_clock: ObservationClock,
    host_locks: dict[str, threading.Lock],
    host_locks_guard: threading.Lock,
    queue_or_record,
    record_observation,
    defer_with_prior,
    provider_feed_cache: _ProviderFeedCache | None = None,
) -> None:
    observed_at = _aware_utc(observation_clock())
    retry_not_before = _latest_datetime(
        request_item.retry_not_before,
        _retry_not_before(rendered_decision.retry_after_seconds, observed_at),
    )
    if rendered_decision.result in {
        "open",
        "closed",
        "expired",
    } or request_item.resume_candidate_index >= len(request_item.target.candidates):
        record_observation(
            request_item.target,
            StatusObservation(rendered_decision, observed_at, retry_not_before),
        )
        return
    if time.monotonic() >= deadline:
        defer_with_prior(
            request_item.target,
            rendered_decision,
            retry_not_before,
        )
        return
    continuation = _direct_target_check(
        request_item.target,
        settings,
        host_locks,
        host_locks_guard,
        deadline=deadline,
        observation_clock=observation_clock,
        start_candidate_index=request_item.resume_candidate_index,
        prior_decision=rendered_decision,
        prior_retry_after_seconds=rendered_decision.retry_after_seconds,
        provider_feed_cache=provider_feed_cache,
    )
    queue_or_record(
        request_item.target,
        continuation,
        inherited_retry_not_before=retry_not_before,
    )


def _prefer_confirmable_decision(
    current: StatusCheckDecision,
    prior: StatusCheckDecision | None,
) -> StatusCheckDecision:
    if prior is None:
        return current
    preferred = current if _decision_precedence(current) >= _decision_precedence(prior) else prior
    preferred = _with_retry_after(
        preferred,
        _longest_retry_after(current.retry_after_seconds, prior.retry_after_seconds),
    )
    if isinstance(preferred, RenderRequired):
        raise TypeError("preferred decision cannot require rendering")
    return preferred


def _decision_precedence(decision: StatusCheckDecision) -> int:
    if decision.result in {"open", "closed", "expired"}:
        return 4
    if decision.evidence_code == "generic_http_404_pending_confirmation":
        return 3
    if decision.result in {"inconclusive", "failed"}:
        return 2
    if decision.result == "unsupported":
        return 1
    return 0


def _remaining_timeout_seconds(
    configured_timeout: float,
    deadline: float | None,
) -> float:
    if deadline is None:
        return configured_timeout
    remaining = max(0.001, deadline - time.monotonic())
    return min(float(configured_timeout), remaining)


def _rendered_budget_units(settings: Settings) -> int:
    provider = settings.posting_rendered_fallback_provider
    if provider in {"chrome_cdp_first", "chrome_cdp", "playwright_profile"}:
        return 5
    return 1


def _minimum_rendered_start_seconds(settings: Settings) -> int:
    # Observed Firefox checks take ~15s even when successful. A four-second
    # start at the October 1 scan deadline produced a preventable timeout.
    # Keep startup headroom separate from the navigation budget divisor.
    if settings.posting_rendered_fallback_provider == "invisible_playwright":
        return 30
    return _rendered_budget_units(settings)


def _rendered_resolver_for_remaining_budget(settings: Settings, deadline: float):
    provider = settings.posting_rendered_fallback_provider
    # Scheduled workers may be abandoned at the runtime deadline, so they may
    # only start independently cancellable resolvers: an owned Firefox process
    # tree or explicit Chrome CDP sessions. In-process profile and model-driven
    # resolvers do not provide that cancellation contract.
    if provider not in {"invisible_playwright", "chrome_cdp_first", "chrome_cdp"}:
        return None
    scheduled_settings = settings.model_copy(
        update={
            "posting_rendered_fallback_provider": provider,
            # No per-posting retries. Indeed recovery uses bounded grants
            # shared across the scan, with a cooldown and the same hard deadline.
            "posting_rendered_fallback_retries": 0,
            # Scheduled concurrency and per-host locks provide pacing without
            # an unbounded class-global throttle sleep outside the run deadline.
            "posting_rendered_fallback_min_interval_seconds": 0.0,
        }
    )
    remaining = max(1, int(deadline - time.monotonic()))
    budget_units = _rendered_budget_units(scheduled_settings)
    per_attempt_timeout = max(1, remaining // budget_units)
    bounded_settings = scheduled_settings.model_copy(
        update={
            "posting_rendered_fallback_timeout_seconds": min(
                scheduled_settings.posting_rendered_fallback_timeout_seconds,
                per_attempt_timeout,
            ),
            "playwright_auth_wall_wait_seconds": min(
                scheduled_settings.playwright_auth_wall_wait_seconds,
                per_attempt_timeout,
            ),
            "playwright_auth_wall_settle_seconds": min(
                scheduled_settings.playwright_auth_wall_settle_seconds,
                per_attempt_timeout,
            ),
        }
    )
    if provider == "invisible_playwright":
        from jobcockpit.ingestion.scheduled_browser import ScheduledInvisiblePlaywrightResolver

        return ScheduledInvisiblePlaywrightResolver(bounded_settings, deadline)
    return default_rendered_page_resolver(
        bounded_settings,
        url_policy=ScheduledRenderedUrlPolicy(
            extra_domain_groups=(
                bounded_settings.scheduled_rendered_extra_domain_groups
            ),
        ),
        trusted_dom_only=True,
    )


def _host_request_context(
    source_url: str,
    host_locks: dict[str, threading.Lock] | None,
    host_locks_guard: threading.Lock | None,
):
    if host_locks is None or host_locks_guard is None:
        return nullcontext()
    hostname = (urlparse(source_url).hostname or "").casefold()
    with host_locks_guard:
        lock = host_locks.setdefault(hostname, threading.Lock())
    return lock
