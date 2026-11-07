from __future__ import annotations

from typing import Literal


STATUS_EVIDENCE_LABELS = {
    "linkedin_no_longer_accepting": "LinkedIn no longer accepting applications",
    "linkedin_job_removed_or_invalid": "LinkedIn job removed or job ID invalid",
    "indeed_job_expired": "Indeed job expired",
    "ats_no_longer_available": "ATS posting no longer available",
    "http_410_gone": "Posting returned HTTP 410 Gone",
    "recognized_ats_detail_404": "ATS job detail returned HTTP 404",
    "generic_http_404_confirmed": "Posting returned HTTP 404 twice",
    "generic_http_404_pending_confirmation": "Posting returned HTTP 404 once",
    "application_deadline_passed": "Application deadline has passed",
    "workday_structured_job_match": "Matching Workday job detail",
    "rendered_posting_identity_match": "Matching rendered posting",
    "posting_identity_match": "Matching posting detail",
    "ashby_public_feed_match": "Matching published Ashby posting",
    "greenhouse_public_feed_match": "Matching published Greenhouse posting",
    "javascript_shell_render_required": "Posting needs browser rendering",
    "response_too_large_render_required": "Large posting needs browser rendering",
    "pdf_text_unavailable": "PDF posting text could not be read",
    "provider_feed_incomplete": "Provider job list is incomplete",
    "recruitee_public_feed_match": "Matching published Recruitee posting",
    "oracle_structured_job_match": "Matching Oracle job detail",
    "provider_feed_identity_missing": "Provider feed omitted job identifiers",
    "rendered_resolver_unavailable": "Rendered status checking is unavailable",
    "authentication_wall": "Posting requires authentication",
    "http_403_forbidden": "Posting returned HTTP 403 Forbidden",
    "http_429_rate_limited": "Posting source rate limited the check",
    "http_5xx": "Posting source returned a server error",
    "direct_fetch_failed": "Posting check could not reach the source",
    "rendered_check_failed": "Rendered posting check failed",
    "rendered_url_policy_blocked": "Browser request blocked by URL safety policy",
    "response_too_large": "Posting response exceeded the safety limit",
    "response_content_encoding_unsupported": "Posting response compression was unsupported",
    "rendered_content_truncated": "Rendered posting exceeded the safety limit",
    "url_policy_dns_resolution_failed": "Posting host could not be resolved",
    "indeed_selected_result_unbound": "Indeed selected result was not bound to its job detail",
    "terminal_evidence_insecure_transport": "Terminal evidence used an insecure connection",
}

StatusEvidenceCategory = Literal[
    "identity",
    "access",
    "response_limit",
    "network",
    "unsupported",
    "stale",
    "confirmed",
    "terminal",
    "unknown",
]

STATUS_EVIDENCE_CATEGORY_LABELS: dict[StatusEvidenceCategory, str] = {
    "identity": "Identity mismatch",
    "access": "Access blocked",
    "response_limit": "Response limit",
    "network": "Network / HTTP",
    "unsupported": "Unsupported URL",
    "stale": "Open confirmation is stale",
    "confirmed": "Confirmed",
    "terminal": "Closed or expired",
    "unknown": "Other",
}

_IDENTITY_EVIDENCE = frozenset(
    {
        "job_identity_mismatch",
        "redirect_identity_mismatch",
        "workday_job_identity_mismatch",
        "rendered_identity_inconclusive",
        "indeed_selected_result_unbound",
    }
)
_ACCESS_EVIDENCE = frozenset(
    {
        "authentication_required",
        "authentication_wall",
        "http_403_forbidden",
        "http_429_rate_limited",
    }
)
_RESPONSE_LIMIT_EVIDENCE = frozenset(
    {
        "response_too_large",
        "response_content_encoding_unsupported",
        "rendered_content_truncated",
    }
)
_UNSUPPORTED_EVIDENCE = frozenset(
    {
        "stored_url_not_job_detail",
        "posting_url_not_detail",
        "no_usable_stored_url",
    }
)


def status_evidence_label(evidence_code: str | None) -> str | None:
    if not evidence_code:
        return None
    return STATUS_EVIDENCE_LABELS.get(
        evidence_code,
        evidence_code.replace("_", " ").capitalize(),
    )


def status_evidence_category(
    result: str | None,
    evidence_code: str | None,
) -> StatusEvidenceCategory:
    if result == "open":
        return "confirmed"
    if result in {"closed", "expired"}:
        return "terminal"
    if result == "unsupported" or evidence_code in _UNSUPPORTED_EVIDENCE:
        return "unsupported"
    if evidence_code in _IDENTITY_EVIDENCE:
        return "identity"
    if evidence_code in _ACCESS_EVIDENCE:
        return "access"
    if evidence_code in _RESPONSE_LIMIT_EVIDENCE:
        return "response_limit"
    if evidence_code:
        return "network"
    return "unknown"


def status_evidence_category_label(category: StatusEvidenceCategory) -> str:
    return STATUS_EVIDENCE_CATEGORY_LABELS[category]
