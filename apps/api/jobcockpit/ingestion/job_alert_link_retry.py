from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    EmailIngestionRun,
    JobAlertEmail,
    JobAlertEmailProcessingState,
    JobAlertLink,
    JobAlertSource,
)
from jobcockpit.ingestion.posting_links import (
    PostingLinkContext,
    SKIPPED_POSTING_LINK_INGESTION_STATUSES,
    is_successful_posting_link_import,
    durable_posting_link_error_message,
    process_posting_link,
)
from jobcockpit.storage.artifacts import ArtifactStorageService


def retry_unresolved_job_alert_link(
    db: Session,
    *,
    link_id: str,
    storage: ArtifactStorageService,
    settings: Settings,
) -> dict[str, Any]:
    link = db.get(JobAlertLink, link_id)
    if not link:
        raise ValueError(f"Job alert link not found: {link_id}")
    if link.ingestion_status != "needs_external_resolution":
        raise ValueError(f"Job alert link is not unresolved: {link_id}")

    email = db.get(JobAlertEmail, link.job_alert_email_id)
    run = db.get(EmailIngestionRun, email.email_ingestion_run_id) if email else None
    source = db.get(JobAlertSource, run.job_alert_source_id) if run else None
    profile = db.get(CandidateProfile, source.candidate_id) if source else None
    result = process_posting_link(
        PostingLinkContext(
            source_type="gmail_alert",
            source_url=link.canonical_url,
            raw_text=_retry_raw_text(link, email),
            link_text=link.link_text,
            surrounding_text=link.surrounding_text or (email.subject if email else ""),
            fetch_source_archive=True,
        ),
        db=db,
        storage=storage,
        settings=settings,
        language_filter_profile_data=profile.profile_data if profile else None,
        skip_language_filter=source.skip_language_filter if source else False,
    )

    link.resolved_url = result.resolved_url
    link.canonical_url = result.canonical_url
    link.classification = result.classification.classification
    link.confidence = int(result.classification.confidence * 100)
    link.ingestion_status = result.ingestion_status
    link.linked_job_id = result.linked_job_id
    link.error_message = durable_posting_link_error_message(result) or None
    link.updated_at = datetime.now(UTC)
    db.add(
        AuditLogEntry.create(
            "job_alert_link.retry",
            "job_alert_link",
            link.job_alert_link_id,
            {
                "ingestion_status": result.ingestion_status,
                "linked_job_id": result.linked_job_id,
                "canonical_url": result.canonical_url,
            },
        )
    )
    if email:
        _refresh_email_processing_state(db, email)
    db.flush()
    return _retry_result_to_api(link, result.created)



def record_unresolved_job_alert_link_retry_failure(
    db: Session,
    *,
    link_id: str,
    error: Exception,
) -> dict[str, Any]:
    error_message = "retry_failed"
    link = db.get(JobAlertLink, link_id)
    if link and link.ingestion_status == "needs_external_resolution":
        link.error_message = error_message
        link.updated_at = datetime.now(UTC)
        db.flush()
        return _retry_result_to_api(link, created=False)
    return _retry_failure_to_api(link_id, error_message)


def retry_failure_to_api(link_id: str, error: Exception) -> dict[str, Any]:
    return _retry_failure_to_api(link_id, "retry_failed")


def _retry_raw_text(link: JobAlertLink, email: JobAlertEmail | None) -> str:
    if link.raw_text.strip():
        return link.raw_text
    fields = [
        link.link_text,
        f"Email subject: {email.subject}" if email else "",
        f"Original URL: {link.original_url}",
        f"Resolved URL: {link.resolved_url}",
        f"Posting URL: {link.canonical_url}",
    ]
    return "\n".join(field for field in fields if field)


def _refresh_email_processing_state(db: Session, email: JobAlertEmail) -> None:
    run = db.get(EmailIngestionRun, email.email_ingestion_run_id)
    if not run:
        return
    state = db.execute(
        select(JobAlertEmailProcessingState).where(
            JobAlertEmailProcessingState.job_alert_source_id == run.job_alert_source_id,
            JobAlertEmailProcessingState.provider_message_id_hash == email.provider_message_id_hash,
        )
    ).scalar_one_or_none()
    if not state:
        return

    links = list(
        db.execute(
            select(JobAlertLink).where(JobAlertLink.job_alert_email_id == email.job_alert_email_id)
        ).scalars()
    )
    linked_jobs = sum(1 for item in links if is_successful_posting_link_import(item.ingestion_status))
    skipped_links = sum(
        1
        for item in links
        if item.ingestion_status in SKIPPED_POSTING_LINK_INGESTION_STATUSES
    )
    needs_review = sum(
        1
        for item in links
        if item.ingestion_status in {"needs_external_resolution", "ready_for_review"}
    )
    state.linked_job_count = linked_jobs
    state.skipped_link_count = skipped_links
    state.needs_review_count = needs_review
    state.last_seen_at = datetime.now(UTC)
    if needs_review:
        state.processing_status = "needs_review"
        state.processed_at = None
    else:
        state.processing_status = "processed"
        state.processed_at = datetime.now(UTC)


def _retry_result_to_api(link: JobAlertLink, created: bool) -> dict[str, Any]:
    return {
        "job_alert_link_id": link.job_alert_link_id,
        "ingestion_status": link.ingestion_status,
        "linked_job_id": link.linked_job_id,
        "created": created,
        "original_url": link.original_url,
        "resolved_url": link.resolved_url,
        "canonical_url": link.canonical_url,
        "classification": link.classification,
        "error_message": link.error_message or "",
        "updated_at": link.updated_at.isoformat(),
    }


def _retry_failure_to_api(link_id: str, error_message: str) -> dict[str, Any]:
    return {
        "job_alert_link_id": link_id,
        "ingestion_status": "retry_failed",
        "linked_job_id": None,
        "created": False,
        "original_url": "",
        "resolved_url": "",
        "canonical_url": "",
        "classification": "",
        "error_message": error_message,
        "updated_at": datetime.now(UTC).isoformat(),
    }
