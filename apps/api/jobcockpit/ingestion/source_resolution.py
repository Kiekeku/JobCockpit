from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SourceResolutionAttempt = dict[str, object]
SourceResolution = dict[str, Any]

AGGREGATOR_FALLBACK_SOURCE_PATHS = frozenset(
    {
        "aggregator_direct_fallback",
        "aggregator_rendered_fallback",
        "aggregator_structured_fallback",
    }
)
OFFICIAL_DISCOVERY_NOT_FOUND_REASONS = frozenset(
    {
        "not_found",
        "official_posting_not_found",
        "rendered_aggregator_fallback_not_configured",
    }
)
OFFICIAL_DISCOVERY_FALLBACK_STATUSES = frozenset(
    {
        "candidates_found",
        "not_found",
        "failed",
        "timeout",
        "auth_blocked",
        "unsafe_url",
    }
)
OFFICIAL_DISCOVERY_REASON_CODES = frozenset(
    {
        "agentic_resolver_low_confidence",
        "agentic_resolver_returned_aggregator_candidate",
        "agentic_resolver_returned_unsafe_candidate",
        "aggregator_fallback_low_confidence",
        "blocked_by_policy",
        "chrome_cdp_content_truncated",
        "chrome_cdp_identity_ambiguous",
        "chrome_cdp_url_mismatch",
        "chrome_cdp_low_confidence",
        "chrome_cdp_timeout",
        "codex_agentic_resolver_invalid_json",
        "codex_agentic_resolver_timeout",
        "codex_agentic_resolver_unavailable",
        "codex_browser_url_policy_blocked",
        "codex_cli_timeout",
        "codex_cli_unavailable",
        "codex_playwright_invalid_json",
        "codex_playwright_auth_wall",
        "codex_playwright_blocked_page",
        "codex_playwright_identity_ambiguous",
        "codex_playwright_url_mismatch",
        "codex_playwright_low_confidence",
        "codex_playwright_timeout",
        "codex_playwright_unavailable",
        "invalid_posting_resolution",
        "manual_auth_wall",
        "linkedin_search_completed",
        "missing_company_or_title",
        "navigation_failed",
        "needs_review",
        "official_candidate_render_needs_review",
        "official_candidate_render_not_found",
        "official_candidate_render_resolved",
        "official_candidate_not_job_detail",
        "official_candidate_not_employer_authoritative",
        "official_candidate_title_mismatch",
        "official_candidate_url_policy_blocked",
        "official_candidates_found",
        "official_candidates_low_confidence",
        "official_discovery_failed",
        "official_posting_resolved",
        "official_site_invalid_response",
        "official_site_request_failed",
        "official_search_needs_review",
        "playwright_content_truncated",
        "playwright_low_confidence",
        "playwright_timeout",
        "playwright_url_mismatch",
        "posting_search_not_configured",
        "serper_http_error",
        "serper_invalid_response",
        "serper_missing_api_key",
        "serper_rate_limited",
        "serper_request_failed",
        "serper_timeout",
        "serper_unauthorized",
        "serper_unavailable",
        "posting_resolver_failed",
        "posting_resolver_not_configured",
        "rendered_aggregator_fallback_not_configured",
        "rendered_content_truncated",
        "rendered_navigation_required",
        "source_render_needs_review",
        "source_render_not_found",
        "source_render_resolved",
        "structured_fallback_resolved",
        "structured_metadata_imported",
        "unknown",
    }
)
OFFICIAL_DISCOVERY_DYNAMIC_REASON_PREFIXES = (
    "aggregator_fallback_failed",
    "chrome_cdp_failed",
    "chrome_cdp_url_policy_blocked",
    "codex_cli_failed",
    "codex_playwright_failed",
    "playwright_failed",
    "playwright_url_policy_blocked",
)

OFFICIAL_DISCOVERY_OUTCOME_STATUSES = frozenset(
    {
        "resolved",
        "candidates_found",
        "not_found",
        "failed",
        "timeout",
        "auth_blocked",
        "unsafe_url",
    }
)


@dataclass(frozen=True)
class OfficialDiscoveryOutcome:
    status: str
    reason_code: str

    def __post_init__(self) -> None:
        raw_reason = self.reason_code
        reason_code = canonical_official_discovery_reason(raw_reason)
        status = normalized_official_discovery_reason(self.status)
        if status not in OFFICIAL_DISCOVERY_OUTCOME_STATUSES:
            status = official_discovery_status_from_reason(raw_reason)
        if status == "resolved":
            reason_code = "official_posting_resolved"
        elif status == "candidates_found":
            reason_code = "official_candidates_found"
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", reason_code)


def normalized_official_discovery_reason(reason: str) -> str:
    return reason.strip().casefold().replace("-", "_").replace(" ", "_")


def canonical_official_discovery_reason(reason: str) -> str:
    headline = re.split(r"[:|=\r\n]", reason, maxsplit=1)[0]
    normalized = normalized_official_discovery_reason(headline)
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")[:160]
    if not normalized:
        return "unknown"
    if normalized in OFFICIAL_DISCOVERY_NOT_FOUND_REASONS:
        return "official_posting_not_found"
    if normalized in OFFICIAL_DISCOVERY_REASON_CODES:
        return normalized
    for prefix in OFFICIAL_DISCOVERY_DYNAMIC_REASON_PREFIXES:
        if normalized.startswith(prefix):
            return prefix
    return "official_discovery_failed"


def official_discovery_status_from_reason(reason: str) -> str:
    canonical_reason = canonical_official_discovery_reason(reason)
    normalized = normalized_official_discovery_reason(reason)
    if canonical_reason == "official_posting_resolved":
        return "resolved"
    if canonical_reason == "official_candidates_found":
        return "candidates_found"
    if canonical_reason == "official_posting_not_found":
        return "not_found"
    if "timeout" in normalized:
        return "timeout"
    if any(
        marker in normalized
        for marker in ("unsafe", "policy_blocked", "blocked_by_policy", "url_policy")
    ):
        return "unsafe_url"
    if any(
        marker in normalized
        for marker in (
            "auth",
            "blocked_rendered_page",
            "forbidden",
            "gated",
            "request_blocked",
            "cloudflare",
        )
    ):
        return "auth_blocked"
    return "failed"


def official_discovery_outcome(
    reason: str,
    *,
    status: str = "",
) -> OfficialDiscoveryOutcome:
    reason_code = canonical_official_discovery_reason(reason)
    normalized_status = normalized_official_discovery_reason(status)
    if normalized_status not in OFFICIAL_DISCOVERY_OUTCOME_STATUSES:
        normalized_status = official_discovery_status_from_reason(reason)
    if normalized_status == "resolved":
        reason_code = "official_posting_resolved"
    elif normalized_status == "candidates_found":
        reason_code = "official_candidates_found"
    return OfficialDiscoveryOutcome(status=normalized_status, reason_code=reason_code)


def official_discovery_trace_source_resolution(
    *,
    outcome: OfficialDiscoveryOutcome,
    attempts: Sequence[SourceResolutionAttempt] = (),
    trace_truncated: bool = False,
) -> SourceResolution:
    metadata: SourceResolution = {
        "official_discovery_status": outcome.status,
        "reason_code": outcome.reason_code,
        "official_discovery_error": outcome.reason_code,
    }
    if attempts:
        metadata["attempts"] = [dict(attempt) for attempt in attempts]
    if trace_truncated:
        metadata["trace_truncated"] = True
    return metadata


def aggregator_fallback_source_resolution(
    *,
    path: str,
    reason: str,
    fallback_url: str | None = None,
    official_discovery_status: str = "",
    outcome: OfficialDiscoveryOutcome | None = None,
    attempts: Sequence[SourceResolutionAttempt] = (),
    trace_truncated: bool = False,
) -> SourceResolution:
    normalized_status = normalized_official_discovery_reason(official_discovery_status)
    outcome = outcome or official_discovery_outcome(
        reason,
        status=normalized_status
        if normalized_status in OFFICIAL_DISCOVERY_FALLBACK_STATUSES
        else "",
    )
    metadata = {
        "kind": "aggregator_fallback",
        "path": path,
        "official_discovery_status": outcome.status,
        "reason_code": outcome.reason_code,
        "official_discovery_error": outcome.reason_code,
    }
    if fallback_url:
        metadata["fallback_url"] = fallback_url
    if attempts:
        metadata["attempts"] = [dict(attempt) for attempt in attempts]
    if trace_truncated:
        metadata["trace_truncated"] = True
    return metadata


def official_discovery_source_resolution(
    official_url: str,
    *,
    outcome: OfficialDiscoveryOutcome | None = None,
    attempts: Sequence[SourceResolutionAttempt] = (),
    trace_truncated: bool = False,
) -> SourceResolution:
    outcome = outcome or official_discovery_outcome(
        "official_posting_resolved",
        status="resolved",
    )
    metadata: SourceResolution = {
        "kind": "official_discovery",
        "path": "discovered_ats",
        "official_discovery_status": outcome.status,
        "reason_code": outcome.reason_code,
        "official_url": official_url,
    }
    if attempts:
        metadata["attempts"] = [dict(attempt) for attempt in attempts]
    if trace_truncated:
        metadata["trace_truncated"] = True
    return metadata


def direct_posting_source_resolution(official_url: str) -> SourceResolution:
    return {
        "kind": "direct",
        "path": "direct_ats",
        "official_discovery_status": "not_attempted",
        "reason_code": "direct_posting",
        "official_url": official_url,
    }


def is_aggregator_fallback_source_resolution(source_resolution: SourceResolution | None) -> bool:
    return bool(source_resolution and source_resolution.get("kind") == "aggregator_fallback")


def official_discovery_outcome_from_source_resolution(
    source_resolution: SourceResolution | None,
) -> OfficialDiscoveryOutcome | None:
    if not source_resolution:
        return None
    status = str(source_resolution.get("official_discovery_status") or "")
    reason = str(
        source_resolution.get("reason_code")
        or source_resolution.get("official_discovery_error")
        or ""
    )
    if not status or not reason:
        return None
    return official_discovery_outcome(reason, status=status)
