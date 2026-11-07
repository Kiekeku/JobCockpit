from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    EmailIngestionRun,
    JobAlertEmail,
    JobAlertEmailProcessingState,
    JobAlertLink,
    JobAlertSource,
)
from jobcockpit.integrations.gmail_runs import run_gmail_source
from jobcockpit.ingestion.job_alert_link_retry import (
    record_unresolved_job_alert_link_retry_failure,
    retry_unresolved_job_alert_link,
)
from jobcockpit.integrations.gmail_processing import (
    GmailProcessingError,
    preview_gmail_source,
    process_gmail_source,
)
from jobcockpit.schemas.requests import (
    GmailImportRequest,
    GmailPreviewRequest,
    JobAlertSourceCreate,
    JobAlertSourceUpdate,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


def create_job_alert_router(
    *,
    get_db: Callable[..., Session],
    get_storage: Callable[..., ArtifactStorageService],
    settings: Settings,
    job_importer: Callable,
) -> APIRouter:
    router = APIRouter(tags=["job-alerts"])

    @router.post("/api/job-alert-sources", status_code=201)
    def create_job_alert_source(
        request: JobAlertSourceCreate, db: Session = Depends(get_db)
    ) -> dict:
        source = JobAlertSource(
            job_alert_source_id=new_id("alertsrc"),
            candidate_id=CANDIDATE_ID,
            provider="gmail",
            display_name=request.display_name,
            gmail_query=request.gmail_query,
            sender_allowlist=request.sender_allowlist,
            enabled=request.enabled,
            skip_interest_filter=request.skip_interest_filter,
            skip_language_filter=request.skip_language_filter,
        )
        db.add(source)
        db.flush()
        return source.to_api()

    @router.get("/api/job-alert-sources")
    def list_job_alert_sources(db: Session = Depends(get_db)) -> list[dict]:
        return [
            source.to_api() for source in db.execute(select(JobAlertSource)).scalars()
        ]

    @router.get("/api/job-alert-links/unresolved")
    def list_unresolved_job_alert_links(
        db: Session = Depends(get_db),
    ) -> list[dict]:
        rows = db.execute(
            select(JobAlertLink, JobAlertEmail, EmailIngestionRun, JobAlertSource)
            .join(
                JobAlertEmail,
                JobAlertLink.job_alert_email_id == JobAlertEmail.job_alert_email_id,
            )
            .join(
                EmailIngestionRun,
                JobAlertEmail.email_ingestion_run_id
                == EmailIngestionRun.email_ingestion_run_id,
            )
            .join(
                JobAlertSource,
                EmailIngestionRun.job_alert_source_id
                == JobAlertSource.job_alert_source_id,
            )
            .where(JobAlertLink.ingestion_status == "needs_external_resolution")
            .order_by(JobAlertLink.updated_at.desc())
        ).all()
        return [
            {
                "job_alert_link_id": link.job_alert_link_id,
                "job_alert_source_id": source.job_alert_source_id,
                "source_display_name": source.display_name,
                "email_subject": email.subject,
                "received_at": email.received_at,
                "link_text": link.link_text,
                "original_url": link.original_url,
                "resolved_url": link.resolved_url,
                "canonical_url": link.canonical_url,
                "classification": link.classification,
                "ingestion_status": link.ingestion_status,
                "error_message": link.error_message or "",
                "updated_at": link.updated_at.isoformat(),
            }
            for link, email, _run, source in rows
        ]

    @router.post("/api/job-alert-links/{link_id}/retry")
    def retry_job_alert_link(
        link_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        try:
            return retry_unresolved_job_alert_link(
                db,
                link_id=link_id,
                storage=storage,
                settings=settings,
            )
        except ValueError as error:
            detail = str(error)
            if "not found" in detail.casefold():
                raise HTTPException(status_code=404, detail=detail) from error
            raise HTTPException(status_code=409, detail=detail) from error
        except Exception as error:
            return record_unresolved_job_alert_link_retry_failure(
                db, link_id=link_id, error=error
            )

    @router.put("/api/job-alert-sources/{source_id}")
    def update_job_alert_source(
        source_id: str,
        request: JobAlertSourceUpdate,
        db: Session = Depends(get_db),
    ) -> dict:
        source = db.get(JobAlertSource, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Job alert source not found")
        source.display_name = request.display_name
        source.gmail_query = request.gmail_query
        source.sender_allowlist = request.sender_allowlist
        source.enabled = request.enabled
        source.skip_interest_filter = request.skip_interest_filter
        source.skip_language_filter = request.skip_language_filter
        source.updated_at = datetime.now(UTC)
        db.add(
            AuditLogEntry.create(
                "job_alert_source.updated", "job_alert_source", source_id
            )
        )
        db.flush()
        return source.to_api()

    @router.delete("/api/job-alert-sources/{source_id}", status_code=204)
    def delete_job_alert_source(
        source_id: str, db: Session = Depends(get_db)
    ) -> Response:
        source = db.get(JobAlertSource, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Job alert source not found")

        runs = list(
            db.execute(
                select(EmailIngestionRun).where(
                    EmailIngestionRun.job_alert_source_id == source_id
                )
            ).scalars()
        )
        run_ids = [run.email_ingestion_run_id for run in runs]
        emails = (
            list(
                db.execute(
                    select(JobAlertEmail).where(
                        JobAlertEmail.email_ingestion_run_id.in_(run_ids)
                    )
                ).scalars()
            )
            if run_ids
            else []
        )
        email_ids = [email.job_alert_email_id for email in emails]
        if email_ids:
            db.execute(
                delete(JobAlertLink).where(
                    JobAlertLink.job_alert_email_id.in_(email_ids)
                )
            )
            db.execute(
                delete(JobAlertEmail).where(
                    JobAlertEmail.job_alert_email_id.in_(email_ids)
                )
            )
        db.execute(
            delete(JobAlertEmailProcessingState).where(
                JobAlertEmailProcessingState.job_alert_source_id == source_id
            )
        )
        if run_ids:
            db.execute(
                delete(EmailIngestionRun).where(
                    EmailIngestionRun.email_ingestion_run_id.in_(run_ids)
                )
            )
        db.delete(source)
        db.add(
            AuditLogEntry.create(
                "job_alert_source.deleted", "job_alert_source", source_id
            )
        )
        db.flush()
        return Response(status_code=204)

    @router.post("/api/job-alerts/gmail/preview")
    def preview_gmail_alerts(
        request: GmailPreviewRequest, db: Session = Depends(get_db)
    ) -> dict:
        source = db.get(JobAlertSource, request.source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Job alert source not found")
        try:
            return preview_gmail_source(db=db, source=source, settings=settings)
        except GmailProcessingError as error:
            raise HTTPException(
                status_code=error.status_code, detail=error.detail
            ) from error

    @router.post("/api/job-alerts/gmail/import")
    def import_gmail_alerts(
        request: GmailImportRequest,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        source = db.get(JobAlertSource, request.source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Job alert source not found")
        try:
            return run_gmail_source(
                db=db, storage=storage, settings=settings, source_id=request.source_id,
                approved_message_review_ids=request.approved_message_review_ids,
                require_preview=True, job_importer=job_importer,
                processor=process_gmail_source,
            )
        except GmailProcessingError as error:
            raise HTTPException(
                status_code=error.status_code, detail=error.detail
            ) from error

    return router
