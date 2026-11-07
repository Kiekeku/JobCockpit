from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.db.models import JobIngestionItem, JobIngestionRun
from jobcockpit.ingestion.audit import (
    IngestionAuditService,
    ats_ingestion_counts,
    empty_ingestion_counts,
)
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest
from jobcockpit.ingestion.posting_batch import PostingBatchItem
from jobcockpit.ingestion.posting_links import (
    PostingLinkProcessingResult,
    canonicalize_posting_url,
    durable_posting_link_error_message,
)
from jobcockpit.schemas.requests import AtsIngestRequest
from jobcockpit.utils.ids import new_id


logger = logging.getLogger(__name__)
_ingestion_audit = IngestionAuditService()


def exception_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)
    return str(error) or error.__class__.__name__


def create_job_ingestion_item(
    run_id: str,
    source_url: str,
    db: Session,
    *,
    status: str = "pending",
    error_message: str | None = None,
    retryable: bool = False,
) -> JobIngestionItem:
    item = JobIngestionItem(
        ingestion_item_id=new_id("ingestitem"),
        ingestion_run_id=run_id,
        source_url=source_url,
        resolved_url=source_url,
        canonical_url=canonicalize_posting_url(source_url),
        status=status,
        error_message=error_message,
        retryable=retryable,
    )
    db.add(item)
    return item


def create_discovered_job_ingestion_items(
    run_id: str,
    batch_items: list[PostingBatchItem],
    fetch_request: AtsFetchRequest,
    db: Session,
) -> list[JobIngestionItem]:
    items = [create_job_ingestion_item(run_id, item.source_url, db) for item in batch_items]
    for skip in fetch_request.stats.interest_skips:
        create_job_ingestion_item(
            run_id,
            skip.source_url,
            db,
            status="skipped_by_interest_filter",
            error_message=skip.reason,
        )
    for failure in fetch_request.stats.link_failures:
        create_job_ingestion_item(
            run_id,
            failure.source_url,
            db,
            status=(
                "expired"
                if failure.category == "permanent" and failure.upstream_status_code in {404, 410}
                else "failed"
            ),
            error_message=failure.reason,
            retryable=failure.category != "permanent",
        )
    db.flush()
    return items


def update_job_ingestion_item(
    item: JobIngestionItem,
    result: PostingLinkProcessingResult,
) -> None:
    item.resolved_url = result.resolved_url
    item.canonical_url = result.canonical_url
    item.status = result.ingestion_status
    item.linked_job_id = result.linked_job_id
    item.error_message = durable_posting_link_error_message(result) or None
    item.retryable = result.ingestion_status in {
        "needs_external_resolution",
        "ready_for_review",
    }


def fail_job_ingestion_item(item: JobIngestionItem, error: Exception) -> None:
    item.status = "failed"
    item.linked_job_id = None
    item.error_message = exception_message(error)
    item.retryable = True


def create_job_ingestion_run(request: AtsIngestRequest, db: Session) -> JobIngestionRun:
    request_payload = {
        "provider": request.provider,
        "source_url": request.source_url,
        "limit": request.limit,
        "search_text": request.search_text,
        "applied_facets": request.applied_facets,
        "skip_interest_filter": request.skip_interest_filter,
        "skip_language_filter": request.skip_language_filter,
    }
    run = JobIngestionRun(
        ingestion_run_id=new_id("ingestrun"),
        provider=request.provider,
        source_url=request.source_url,
        search_text=request.search_text,
        status="running",
        alert_level="info",
        started_at=datetime.now(UTC),
        request_payload=request_payload,
    )
    db.add(run)
    _ingestion_audit.record(
        db,
        lifecycle="started",
        provider=request.provider,
        run_id=run.ingestion_run_id,
        source_url=request.source_url,
        status="running",
        counts=empty_ingestion_counts(),
    )
    db.flush()
    logger.info("Started ATS ingestion run %s for %s", run.ingestion_run_id, request.source_url)
    return run


def finish_job_ingestion_run(
    run: JobIngestionRun,
    db: Session,
    *,
    status: str,
    jobs_seen_count: int,
    jobs_created_count: int,
    jobs_duplicate_count: int,
    jobs_skipped_count: int = 0,
    jobs_language_skipped_count: int = 0,
    error_message: str | None = None,
    item_error_count: int = 0,
) -> JobIngestionRun:
    run.status = status
    run.jobs_seen_count = jobs_seen_count
    run.jobs_created_count = jobs_created_count
    run.jobs_duplicate_count = jobs_duplicate_count
    run.jobs_skipped_count = jobs_skipped_count
    run.jobs_language_skipped_count = jobs_language_skipped_count
    run.error_message = error_message
    run.error_count = item_error_count + (1 if error_message else 0)
    run.finished_at = datetime.now(UTC)
    if status == "failed":
        run.alert_level = "error"
        logger.error("Failed ATS ingestion run %s: %s", run.ingestion_run_id, error_message)
    else:
        has_link_failures = bool(run.request_payload.get("link_failures"))
        run.alert_level = (
            "warning"
            if (
                jobs_duplicate_count
                or jobs_skipped_count
                or jobs_language_skipped_count
                or has_link_failures
                or item_error_count
            )
            else "info"
        )
        logger.info(
            "Completed ATS ingestion run %s: seen=%s created=%s duplicates=%s skipped=%s language_skipped=%s",
            run.ingestion_run_id,
            jobs_seen_count,
            jobs_created_count,
            jobs_duplicate_count,
            jobs_skipped_count,
            jobs_language_skipped_count,
        )
    _ingestion_audit.record(
        db,
        lifecycle="failed" if status == "failed" else "completed",
        provider=run.provider,
        run_id=run.ingestion_run_id,
        source_url=run.source_url,
        status=status,
        counts=ats_ingestion_counts(
            items=[
                item.to_api()
                for item in db.execute(
                    select(JobIngestionItem).where(
                        JobIngestionItem.ingestion_run_id == run.ingestion_run_id
                    )
                ).scalars()
            ],
            jobs_created_count=run.jobs_created_count,
            jobs_duplicate_count=run.jobs_duplicate_count,
            failed=status == "failed",
        ),
        error_code="run_failed" if status == "failed" else None,
    )
    db.flush()
    return run


def ingestion_run_alerts(run: JobIngestionRun) -> list[str]:
    alerts: list[str] = []
    if run.status == "failed":
        alerts.append(f"{run.provider} ingestion failed: {run.error_message or 'unknown error'}")
    if run.status == "completed_with_warnings" and run.error_count:
        noun = "link" if run.error_count == 1 else "links"
        alerts.append(f"{run.error_count} posting {noun} failed and can be reviewed individually.")
    if run.jobs_duplicate_count:
        noun = "posting" if run.jobs_duplicate_count == 1 else "postings"
        alerts.append(f"{run.jobs_duplicate_count} duplicate {noun} skipped.")
    if run.jobs_skipped_count:
        noun = "posting" if run.jobs_skipped_count == 1 else "postings"
        alerts.append(f"{run.jobs_skipped_count} {noun} skipped by role-interest filter.")
    if run.jobs_language_skipped_count:
        noun = "posting" if run.jobs_language_skipped_count == 1 else "postings"
        alerts.append(f"{run.jobs_language_skipped_count} {noun} skipped by language filter.")
    return alerts
