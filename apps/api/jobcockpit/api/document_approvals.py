from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.db.models import AuditLogEntry


MANUAL_UPLOAD_APPROVAL_EVENT = "document.manual_upload_approved"


def has_manual_upload_approval(
    db: Session,
    *,
    package_id: str,
    document_id: str,
    artifact_id: str,
) -> bool:
    payloads = db.execute(
        select(AuditLogEntry.payload).where(
            AuditLogEntry.event_type == MANUAL_UPLOAD_APPROVAL_EVENT,
            AuditLogEntry.entity_type == "generated_document",
            AuditLogEntry.entity_id == document_id,
        )
    ).scalars()
    return any(
        isinstance(payload, dict)
        and payload.get("application_package_id") == package_id
        and payload.get("artifact_id") == artifact_id
        and payload.get("approval_source") == "manual_review"
        for payload in payloads
    )


def record_manual_upload_approval(
    db: Session,
    *,
    package_id: str,
    document_id: str,
    artifact_id: str,
) -> None:
    if has_manual_upload_approval(
        db,
        package_id=package_id,
        document_id=document_id,
        artifact_id=artifact_id,
    ):
        return
    db.add(
        AuditLogEntry.create(
            MANUAL_UPLOAD_APPROVAL_EVENT,
            "generated_document",
            document_id,
            {
                "application_package_id": package_id,
                "artifact_id": artifact_id,
                "approval_source": "manual_review",
            },
        )
    )
