from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobcockpit.ingestion.posting_links import (
    SKIPPED_POSTING_LINK_INGESTION_STATUSES,
    SUCCESSFUL_POSTING_LINK_IMPORT_STATUSES,
)

OUTPUT_SCHEMA_VERSION = 1
INGESTION_COUNT_KEYS = (
    "items_seen_count",
    "jobs_created_count",
    "jobs_reused_count",
    "items_skipped_count",
    "items_needing_review_count",
    "items_failed_count",
    "runs_failed_count",
)
_SUCCESS_STATUSES = SUCCESSFUL_POSTING_LINK_IMPORT_STATUSES
_SKIPPED_STATUSES = SKIPPED_POSTING_LINK_INGESTION_STATUSES
_NEEDS_REVIEW_STATUSES = {"needs_external_resolution", "ready_for_review"}


def normalize_ats_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
    has_items = "items" in payload
    items = [_normalize_ats_item(item) for item in payload.get("items") or []]
    item_counts = _counts_from_items(items)
    counts = empty_ingestion_counts()
    counts.update(
        {
            "items_seen_count": (
                len(items) if has_items else int(payload.get("jobs_seen_count") or 0)
            ),
            "jobs_created_count": int(payload.get("jobs_created_count") or 0),
            "jobs_reused_count": int(payload.get("jobs_duplicate_count") or 0),
            "items_skipped_count": (
                item_counts["items_skipped_count"]
                if items
                else int(payload.get("jobs_skipped_count") or 0)
                + int(payload.get("jobs_language_skipped_count") or 0)
            ),
            "items_needing_review_count": item_counts["items_needing_review_count"],
            "items_failed_count": (
                item_counts["items_failed_count"]
                if items
                else len(payload.get("link_failures") or [])
            ),
        }
    )
    ingestion_run = payload.get("ingestion_run") or {}
    alerts = [str(alert) for alert in payload.get("alerts") or []]
    run = _run(
        run_id=ingestion_run.get("ingestion_run_id"),
        provider=str(payload.get("provider") or ingestion_run.get("provider") or "ats"),
        source_id=None,
        source_url=payload.get("source_url"),
        status=_run_status(ingestion_run.get("status"), counts),
        counts=counts,
        items=items,
        alerts=alerts,
        metadata={
            "jobs": payload.get("jobs") or [],
            "skipped_jobs": payload.get("skipped_jobs") or [],
            "language_skipped_jobs": payload.get("language_skipped_jobs") or [],
            "link_failures": payload.get("link_failures") or [],
            "provider_jobs_seen_count": int(payload.get("jobs_seen_count") or 0),
        },
    )
    return _output(provider=run["provider"], runs=[run], alerts=alerts)


def normalize_failed_ingestion(
    *,
    provider: str,
    error: str,
    source_id: str | None = None,
    source_url: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    counts = empty_ingestion_counts()
    counts["runs_failed_count"] = 1
    run = _run(
        run_id=run_id,
        provider=provider,
        source_id=source_id,
        source_url=source_url,
        status="failed",
        counts=counts,
        items=[],
        alerts=[error],
        metadata={"error": error},
    )
    return _output(provider=provider, runs=[run], alerts=[error])


def normalize_gmail_ingestion(
    source_results: list[dict[str, Any]],
    failed_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runs = [_normalize_gmail_run(result) for result in source_results]
    for failure in failed_sources or []:
        error = str(failure.get("error") or "Gmail ingestion failed")
        failed_output = normalize_failed_ingestion(
            provider="gmail",
            source_id=failure.get("source_id"),
            error=error,
            run_id=failure.get("run_id"),
        )
        failed_run = failed_output["runs"][0]
        failed_run["metadata"]["display_name"] = failure.get("display_name")
        failed_run["metadata"]["provider_diagnostics"] = (
            failure.get("provider_diagnostics") or {}
        )
        runs.append(failed_run)
    alerts = [alert for run in runs for alert in run["alerts"]]
    return _output(provider="gmail", runs=runs, alerts=alerts)


def _normalize_gmail_run(result: dict[str, Any]) -> dict[str, Any]:
    items = [_normalize_gmail_item(item) for item in result.get("items") or []]
    item_counts = _counts_from_items(items)
    counts = empty_ingestion_counts()
    counts.update(
        {
            "items_seen_count": (
                len(items)
                if items
                else int(result.get("jobs_discovered_count") or 0)
                + int(result.get("unresolved_links_count") or 0)
            ),
            "jobs_created_count": int(result.get("jobs_created_count") or 0),
            "jobs_reused_count": int(result.get("jobs_reused_count") or 0),
            "items_skipped_count": item_counts["items_skipped_count"],
            "items_needing_review_count": (
                item_counts["items_needing_review_count"]
                if items
                else int(result.get("unresolved_links_count") or 0)
            ),
            "items_failed_count": item_counts["items_failed_count"],
        }
    )
    alerts = [str(alert) for alert in result.get("alerts") or []]
    return _run(
        run_id=result.get("email_ingestion_run_id"),
        provider="gmail",
        source_id=result.get("source_id"),
        source_url=None,
        status=_status_from_counts(counts),
        counts=counts,
        items=items,
        alerts=alerts,
        metadata={
            "display_name": result.get("display_name"),
            "processed_messages": result.get("processed_messages") or [],
            "messages_needing_review_count": int(result.get("messages_needing_review_count") or 0),
            "mailbox_mutations": result.get("mailbox_mutations") or [],
        },
    )


def _normalize_ats_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "item_id": item.get("ingestion_item_id"),
        "source_url": item.get("source_url"),
        "resolved_url": item.get("resolved_url"),
        "canonical_url": item.get("canonical_url"),
        "status": item.get("status"),
        "linked_job_id": item.get("linked_job_id"),
        "error_message": item.get("error_message"),
        "retryable": bool(item.get("retryable")),
        "metadata": {
            key: value for key, value in item.items() if key in {"created_at", "updated_at"}
        },
    }
    return normalized


def _normalize_gmail_item(item: dict[str, Any]) -> dict[str, Any]:
    # Gmail links may contain addresses, tracking parameters, or bearer-style tokens.
    # Keep only local identifiers and classification outcomes in scheduler output.
    return {
        "item_id": item.get("item_id"),
        "source_url": None,
        "resolved_url": None,
        "canonical_url": None,
        "status": item.get("status"),
        "linked_job_id": item.get("linked_job_id"),
        "error_message": None,
        "retryable": bool(item.get("retryable")),
        "metadata": {},
    }


def _counts_from_items(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = empty_ingestion_counts()
    for item in items:
        status = str(item.get("status") or "")
        counts["items_seen_count"] += 1
        if status in _SKIPPED_STATUSES:
            counts["items_skipped_count"] += 1
        elif status in _NEEDS_REVIEW_STATUSES:
            counts["items_needing_review_count"] += 1
        elif status not in _SUCCESS_STATUSES:
            counts["items_failed_count"] += 1
    return counts


def empty_ingestion_counts() -> dict[str, int]:
    return {key: 0 for key in INGESTION_COUNT_KEYS}


def _status_from_counts(counts: dict[str, int]) -> str:
    if counts["items_failed_count"] or counts["items_needing_review_count"]:
        return "completed_with_warnings"
    return "completed"


def _run_status(status: object, counts: dict[str, int]) -> str:
    persisted_status = str(status or "")
    if persisted_status == "failed":
        return "failed"
    if persisted_status == "completed_with_warnings":
        return "completed_with_warnings"
    return _status_from_counts(counts)


def _run(
    *,
    run_id: str | None,
    provider: str,
    source_id: str | None,
    source_url: str | None,
    status: str,
    counts: dict[str, int],
    items: list[dict[str, Any]],
    alerts: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "provider": provider,
        "source_id": source_id,
        "source_url": source_url,
        "status": status,
        "counts": counts,
        "items": items,
        "alerts": alerts,
        "metadata": metadata,
    }


def _output(*, provider: str, runs: list[dict[str, Any]], alerts: list[str]) -> dict[str, Any]:
    totals = empty_ingestion_counts()
    for run in runs:
        for key in totals:
            totals[key] += int(run["counts"].get(key) or 0)
    statuses = {run["status"] for run in runs}
    if statuses == {"failed"}:
        status = "failed"
    elif "failed" in statuses or "completed_with_warnings" in statuses:
        status = "completed_with_warnings"
    else:
        status = "completed"
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "operation": "job_ingestion",
        "status": status,
        "provider": provider,
        "runs": runs,
        "totals": totals,
        "alerts": alerts,
    }
