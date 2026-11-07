from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.document_approvals import has_manual_upload_approval
from jobcockpit.api.file_responses import attachment_headers
from jobcockpit.api.job_access import require_active_package_job
from jobcockpit.api.page_capture_runs import retry_page_capture
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    ApplicationPackage,
    ApplicationPackageDocument,
    AuditLogEntry,
    GeneratedDocument,
    PageCaptureRun,
    StoredArtifact,
    TruthfulnessCheck,
)
from jobcockpit.extension.tokens import (
    create_extension_download_token,
    verify_extension_download_token,
)
from jobcockpit.schemas.requests import (
    ExtensionPackageSessionRequest,
    ExtensionPageCaptureRequest,
)
from jobcockpit.storage.artifacts import ArtifactStorageService


ExtensionPageCaptureHandler = Callable[
    [ExtensionPageCaptureRequest, Session, ArtifactStorageService],
    dict,
]


def get_package_documents_for_requested_ids(
    package_id: str,
    document_ids: set[str],
    db: Session,
) -> list[tuple[ApplicationPackageDocument, GeneratedDocument]]:
    package_rows = list(
        db.execute(
            select(ApplicationPackageDocument, GeneratedDocument)
            .join(
                GeneratedDocument,
                ApplicationPackageDocument.generated_document_id == GeneratedDocument.document_id,
            )
            .where(ApplicationPackageDocument.application_package_id == package_id)
        ).all()
    )
    rows_by_document_id = {
        document.document_id: (package_document, document)
        for package_document, document in package_rows
    }
    return [
        rows_by_document_id[document_id]
        for document_id in document_ids
        if document_id in rows_by_document_id
    ]


def get_package_document_for_generated_document(
    db: Session,
    package_id: str,
    document_id: str,
) -> ApplicationPackageDocument | None:
    return db.execute(
        select(ApplicationPackageDocument).where(
            ApplicationPackageDocument.application_package_id == package_id,
            ApplicationPackageDocument.generated_document_id == document_id,
        )
    ).scalar_one_or_none()


def create_extension_router(
    *,
    get_db: Callable[..., Session],
    get_storage: Callable[..., ArtifactStorageService],
    settings: Settings,
    capture_page: ExtensionPageCaptureHandler,
) -> APIRouter:
    router = APIRouter(prefix="/api/extension", tags=["extension"])

    @router.post("/page-captures", status_code=202)
    def capture_extension_page(
        request: ExtensionPageCaptureRequest,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        return capture_page(request, db, storage)

    @router.get("/page-captures")
    def list_page_captures(
        limit: int = 50,
        db: Session = Depends(get_db),
    ) -> list[dict]:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        runs = db.execute(
            select(PageCaptureRun)
            .order_by(PageCaptureRun.created_at.desc(), PageCaptureRun.capture_id.desc())
            .limit(limit)
        ).scalars()
        return [run.to_api() for run in runs]

    @router.get("/page-captures/{capture_id}")
    def get_page_capture(
        capture_id: str,
        db: Session = Depends(get_db),
    ) -> dict:
        run = db.get(PageCaptureRun, capture_id)
        if not run:
            raise HTTPException(status_code=404, detail="Page capture not found")
        return run.to_api()

    @router.post("/page-captures/{capture_id}/retry", status_code=202)
    def retry_failed_page_capture(
        capture_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        retried, queued = retry_page_capture(capture_id, db, storage)
        return {**retried.to_api(), "queued": queued}

    @router.post("/packages/{package_id}/session")
    def create_extension_package_session(
        package_id: str,
        request: ExtensionPackageSessionRequest,
        db: Session = Depends(get_db),
    ) -> dict:
        package, _application, _job = require_active_package_job(db, package_id)

        if not request.document_ids:
            raise HTTPException(status_code=400, detail="document_ids cannot be empty")
        requested_ids = set(request.document_ids)
        package_documents = get_package_documents_for_requested_ids(package_id, requested_ids, db)
        if len(package_documents) != len(requested_ids):
            raise HTTPException(status_code=404, detail="Package document not found")
        if not package_documents:
            raise HTTPException(status_code=400, detail="Package has no generated documents")

        documents = [document for _, document in package_documents]
        passed_check_ids = set(
            db.execute(
                select(TruthfulnessCheck.truthfulness_check_id).where(
                    TruthfulnessCheck.application_package_id == package_id,
                    TruthfulnessCheck.status == "passed",
                )
            ).scalars()
        )
        if any(
            document.truthfulness_check_id not in passed_check_ids
            and not has_manual_upload_approval(
                db,
                package_id=package_id,
                document_id=document.document_id,
                artifact_id=document.artifact_id,
            )
            for document in documents
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Documents require a passed truthfulness check or an audited "
                    "manual upload approval"
                ),
            )

        package.approved_for_extension = True
        package.approved_at = datetime.now(UTC)
        for package_document, document in package_documents:
            package_document.approved_for_upload = True
            document.approved_by_user = True
        db.add(
            AuditLogEntry.create(
                "extension.session.created",
                "application_package",
                package_id,
            )
        )
        db.flush()

        session_documents = []
        for document in documents:
            token = create_extension_download_token(
                settings,
                package_id=package_id,
                document_id=document.document_id,
                expires_in_seconds=request.expires_in_seconds,
            )
            session_documents.append(
                {
                    "document_id": document.document_id,
                    "token": token,
                    "download_url": (
                        f"/api/extension/documents/{document.document_id}/download?token={token}"
                    ),
                }
            )
        return {
            "package_id": package_id,
            "expires_in_seconds": request.expires_in_seconds,
            "documents": session_documents,
        }

    @router.get("/documents/{document_id}/download")
    def download_extension_document(
        document_id: str,
        token: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        token_payload = verify_extension_download_token(settings, token)
        if not token_payload or token_payload.document_id != document_id:
            raise HTTPException(status_code=403, detail="Invalid extension token")
        document = db.get(GeneratedDocument, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        package = db.get(ApplicationPackage, document.application_package_id)
        if package:
            require_active_package_job(db, package.application_package_id)
        package_document = get_package_document_for_generated_document(
            db,
            token_payload.package_id,
            document_id,
        )
        if (
            not package
            or not package.approved_for_extension
            or not package_document
            or not package_document.approved_for_upload
            or not document.approved_by_user
            or package_document.artifact_id != document.artifact_id
            or token_payload.package_id != document.application_package_id
        ):
            raise HTTPException(
                status_code=403,
                detail="Document is not approved for extension download",
            )
        truthfulness = (
            db.get(TruthfulnessCheck, document.truthfulness_check_id)
            if document.truthfulness_check_id
            else None
        )
        has_passed_truthfulness = bool(
            truthfulness
            and truthfulness.application_package_id == document.application_package_id
            and truthfulness.status == "passed"
        )
        has_manual_approval = has_manual_upload_approval(
            db,
            package_id=document.application_package_id,
            document_id=document.document_id,
            artifact_id=document.artifact_id,
        )
        if not has_passed_truthfulness and not has_manual_approval:
            raise HTTPException(
                status_code=403,
                detail="Document is not cleared for extension download",
            )
        artifact = db.get(StoredArtifact, document.artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return Response(
            content=storage.read_bytes(artifact.storage_uri),
            media_type=artifact.content_type,
            headers=attachment_headers(artifact.original_file_name),
        )

    return router
