"""The durable Gmail run lifecycle shared by HTTP and scheduled imports."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobAlertSource
from jobcockpit.ingestion.audit import (
    IngestionAuditService,
    empty_ingestion_counts,
    failed_ingestion_counts,
    gmail_ingestion_counts,
)
from jobcockpit.integrations.gmail_processing import (
    GmailProcessingError,
    mark_email_ingestion_run_failed,
    process_gmail_source,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


def run_gmail_source(
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
    source_id: str,
    approved_message_review_ids: Iterable[str] | None = None,
    require_preview: bool = False,
    job_importer: Callable | None = None,
    run_id: str | None = None,
    processor: Callable = process_gmail_source,
) -> dict:
    """Commit completion, or roll back and durably record the failed run.

    Item transactions and message checkpoints belong to the processor. A failed
    run must retain completed items and close the same audit run in both callers.
    """
    run_id = run_id or new_id("emailrun")
    audit = IngestionAuditService(settings)
    audit.record_durable(
        lifecycle="started", provider="gmail", run_id=run_id, source_id=source_id,
        status="running", counts=empty_ingestion_counts(),
    )
    try:
        source = db.get(JobAlertSource, source_id)
        if source is None:
            raise ValueError(f"Job alert source not found: {source_id}")
        result = processor(
            db=db, storage=storage, settings=settings, source=source,
            approved_message_review_ids=approved_message_review_ids,
            require_preview=require_preview, job_importer=job_importer,
            ingestion_run_id=run_id,
        )
        status, counts = gmail_ingestion_counts(result, source_id=source_id)
        audit.record(
            db, lifecycle="completed", provider="gmail", run_id=run_id,
            source_id=source_id, status=status, counts=counts,
        )
        db.commit()
        return result
    except Exception as error:
        db.rollback()
        mark_email_ingestion_run_failed(settings, run_id)
        error_code = (
            error.error_code if isinstance(error, GmailProcessingError)
            else "validation_error" if isinstance(error, ValueError)
            else "unexpected_error"
        )
        audit.record_durable(
            lifecycle="failed", provider="gmail", run_id=run_id, source_id=source_id,
            status="failed", counts=failed_ingestion_counts(), error_code=error_code,
        )
        raise
