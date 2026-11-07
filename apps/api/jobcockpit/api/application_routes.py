from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.application_documents import (
    DOCX_CONTENT_TYPE,
    attach_generated_document_to_package,
    begin_serialized_package_write,
    job_requires_cover_letter,
    next_application_package_version,
    package_document_to_api,
)
from jobcockpit.api.document_approvals import record_manual_upload_approval
from jobcockpit.api.file_responses import attachment_headers, inline_headers
from jobcockpit.api.job_access import (
    active_applications,
    require_active_application_job,
    require_active_job,
    require_active_package_job,
)
from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    ApplicationPackage,
    ApplicationPackageDocument,
    AuditLogEntry,
    CandidateProfile,
    EvidenceItem,
    GeneratedDocument,
    StoredArtifact,
    TruthfulnessCheck,
)
from jobcockpit.documents.generator import build_resume_markdown, select_evidence_for_job
from jobcockpit.documents.truthfulness import run_truthfulness_check
from jobcockpit.schemas.requests import ApplicationDecisionRequest, CoverLetterDraftRequest
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


def create_application_router(
    *,
    get_db: Callable[..., Session],
    get_storage: Callable[..., ArtifactStorageService],
    settings: Settings,
) -> APIRouter:
    router = APIRouter(tags=["applications"])

    @router.post("/api/jobs/{job_id}/applications", status_code=201)
    def create_application(job_id: str, db: Session = Depends(get_db)) -> dict:
        require_active_job(db, job_id)
        existing = db.execute(
            select(Application).where(Application.job_id == job_id)
        ).scalar_one_or_none()
        if existing:
            return existing.to_api()
        application = Application(
            application_id=new_id("app"),
            candidate_id=CANDIDATE_ID,
            job_id=job_id,
            status="draft",
            decision="undecided",
        )
        db.add(application)
        db.add(
            AuditLogEntry.create("application.created", "application", application.application_id)
        )
        db.flush()
        return application.to_api()

    @router.post("/api/jobs/{job_id}/applications/applied")
    def mark_job_applied(job_id: str, db: Session = Depends(get_db)) -> dict:
        require_active_job(db, job_id)
        application = db.execute(
            select(Application).where(Application.job_id == job_id)
        ).scalar_one_or_none()
        if application is not None and application.status == "applied" and application.applied_at:
            return application.to_api()

        previous_status = application.status if application else None
        now = datetime.now(UTC)
        if application is None:
            application = Application(
                application_id=new_id("app"),
                candidate_id=CANDIDATE_ID,
                job_id=job_id,
                status="applied",
                decision="undecided",
                applied_at=now,
            )
            db.add(application)
            db.add(
                AuditLogEntry.create(
                    "application.created", "application", application.application_id
                )
            )
        else:
            application.status = "applied"
            application.applied_at = now
            application.updated_at = now

        db.add(
            AuditLogEntry.create(
                "application.applied",
                "application",
                application.application_id,
                payload={
                    "job_id": job_id,
                    "previous_status": previous_status,
                    "applied_at": now.isoformat(),
                },
            )
        )
        db.flush()
        return application.to_api()

    @router.get("/api/applications")
    def list_applications(db: Session = Depends(get_db)) -> list[dict]:
        return [application.to_api() for application in active_applications(db)]

    @router.get("/api/applications/{application_id}")
    def get_application(application_id: str, db: Session = Depends(get_db)) -> dict:
        application, _job = require_active_application_job(db, application_id)
        return application.to_api()

    @router.post("/api/applications/{application_id}/decision")
    def update_decision(
        application_id: str,
        request: ApplicationDecisionRequest,
        db: Session = Depends(get_db),
    ) -> dict:
        application, _job = require_active_application_job(db, application_id)
        application.decision = request.decision
        application.would_accept_if_offered = request.would_accept_if_offered
        application.updated_at = datetime.now(UTC)
        return application.to_api()

    @router.post("/api/applications/{application_id}/packages", status_code=201)
    def create_package(
        application_id: str, response: Response, db: Session = Depends(get_db),
    ) -> dict:
        from jobcockpit.api.cover_letter_generation_runs import queue_manual_cover_letter
        application, job = require_active_application_job(db, application_id)
        if job_requires_cover_letter(job):
            result = queue_manual_cover_letter(db, job.id, None, candidate_id=application.candidate_id, response_view="package")
            response.status_code = 202
            return result
        begin_serialized_package_write(db)
        application, job = require_active_application_job(db, application_id)
        package = ApplicationPackage(
            application_package_id=new_id("pkg"), application_id=application_id,
            package_version=next_application_package_version(db, application_id),
            status="draft", approved_for_extension=False,
        )
        db.add(package)
        db.flush()
        return package.to_api()

    @router.get("/api/application-packages/{package_id}")
    def get_package(package_id: str, db: Session = Depends(get_db)) -> dict:
        package, _application, _job = require_active_package_job(db, package_id)
        return package.to_api()

    @router.post("/api/application-packages/{package_id}/resume-draft", status_code=201)
    def create_resume_draft(
        package_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        _package, application, job = require_active_package_job(db, package_id)
        profile = db.get(CandidateProfile, application.candidate_id)
        evidence_items = list(
            db.execute(
                select(EvidenceItem).where(
                    EvidenceItem.candidate_id == application.candidate_id,
                    EvidenceItem.allowed_for_resume.is_(True),
                )
            ).scalars()
        )
        selected_evidence_items = select_evidence_for_job(job, evidence_items, 5)
        markdown = build_resume_markdown(
            job, profile.profile_data if profile else None, selected_evidence_items
        )
        artifact = storage.write_bytes(
            artifact_type="generated_document",
            content=markdown.encode("utf-8"),
            original_file_name=f"{job.normalized_title or 'resume'}-resume.md",
            content_type="text/markdown",
            candidate_id=application.candidate_id,
            application_id=application.application_id,
            job_id=job.id,
            privacy_class="private",
        )
        artifact_row = StoredArtifact.from_record(artifact)
        db.add(artifact_row)
        db.flush()
        document = GeneratedDocument(
            document_id=new_id("doc"),
            application_package_id=package_id,
            document_type="resume",
            artifact_id=artifact_row.artifact_id,
            source_format="markdown",
            rendered_format="markdown",
            sha256=artifact.sha256,
            content_preview=markdown,
            claim_mapping_count=len(selected_evidence_items),
            approved_by_user=False,
        )
        db.add(document)
        db.flush()
        package_document = attach_generated_document_to_package(
            db, package_id, document, required=True
        )
        db.add(
            AuditLogEntry.create("document.generated", "generated_document", document.document_id)
        )
        db.flush()
        return package_document_to_api(package_document, document)

    @router.post("/api/application-packages/{package_id}/cover-letter-draft", status_code=202)
    def create_cover_letter_draft(
        package_id: str, request: CoverLetterDraftRequest | None = None,
        db: Session = Depends(get_db),
    ) -> dict:
        from jobcockpit.api.cover_letter_generation_runs import queue_manual_cover_letter
        _package, application, job = require_active_package_job(db, package_id)
        return queue_manual_cover_letter(
            db, job.id, request, candidate_id=application.candidate_id, target_package_id=package_id, response_view="document",
        )

    @router.post("/api/application-packages/{package_id}/truthfulness-check")
    def truthfulness_check(package_id: str, db: Session = Depends(get_db)) -> dict:
        _package, application, _job = require_active_package_job(db, package_id)
        documents = list(
            db.execute(
                select(GeneratedDocument).where(
                    GeneratedDocument.application_package_id == package_id
                )
            ).scalars()
        )
        profile = db.get(CandidateProfile, application.candidate_id)
        result = run_truthfulness_check(documents, profile.profile_data if profile else None)
        row = TruthfulnessCheck(
            truthfulness_check_id=new_id("truth"),
            application_package_id=package_id,
            status=result["status"],
            blocking_errors=result["blocking_errors"],
            warnings=result["warnings"],
        )
        db.add(row)
        db.flush()
        for document in documents:
            document.truthfulness_check_id = row.truthfulness_check_id
        return result

    @router.post(
        "/api/application-packages/{package_id}/documents/{document_id}/approve-for-upload"
    )
    def approve_package_document_for_upload(
        package_id: str,
        document_id: str,
        db: Session = Depends(get_db),
    ) -> dict:
        package, application, job = require_active_package_job(db, package_id)
        row = db.execute(
            select(ApplicationPackageDocument, GeneratedDocument)
            .join(
                GeneratedDocument,
                ApplicationPackageDocument.generated_document_id == GeneratedDocument.document_id,
            )
            .where(
                ApplicationPackageDocument.application_package_id == package_id,
                ApplicationPackageDocument.generated_document_id == document_id,
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Package document not found")
        package_document, document = row
        artifact = db.get(StoredArtifact, document.artifact_id)
        if (
            artifact is None
            or artifact.deleted_at is not None
            or artifact.artifact_type != "generated_document"
            or artifact.application_id != application.application_id
            or artifact.job_id != job.id
            or artifact.candidate_id != application.candidate_id
            or package_document.artifact_id != document.artifact_id
        ):
            raise HTTPException(status_code=404, detail="Package document not found")

        document.approved_by_user = True
        package_document.approved_for_upload = True
        package.approved_for_extension = True
        package.approved_at = datetime.now(UTC)
        record_manual_upload_approval(
            db,
            package_id=package_id,
            document_id=document_id,
            artifact_id=document.artifact_id,
        )
        db.flush()
        return package_document_to_api(package_document, document)

    @router.get("/api/application-packages/{package_id}/documents")
    def list_package_documents(package_id: str, db: Session = Depends(get_db)) -> list[dict]:
        require_active_package_job(db, package_id)
        return [
            package_document_to_api(package_document, document)
            for package_document, document in db.execute(
                select(ApplicationPackageDocument, GeneratedDocument)
                .join(
                    GeneratedDocument,
                    ApplicationPackageDocument.generated_document_id
                    == GeneratedDocument.document_id,
                )
                .where(ApplicationPackageDocument.application_package_id == package_id)
            ).all()
        ]

    @router.get("/api/application-packages/{package_id}/documents/{document_id}/download")
    def download_local_package_document(
        package_id: str,
        document_id: str,
        download: bool = False,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        _package, application, job = require_active_package_job(db, package_id)
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id == package_id,
                ApplicationPackageDocument.generated_document_id == document_id,
            )
        ).scalar_one_or_none()
        document = db.get(GeneratedDocument, document_id)
        artifact = db.get(StoredArtifact, document.artifact_id) if document else None
        if (
            not package_document
            or not document
            or not artifact
            or document.application_package_id != package_id
            or package_document.artifact_id != document.artifact_id
            or artifact.artifact_type != "generated_document"
            or artifact.application_id != application.application_id
            or artifact.job_id != job.id
            or artifact.candidate_id != application.candidate_id
            or artifact.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Package document not found")
        headers = (
            attachment_headers(artifact.original_file_name)
            if download
            else inline_headers(artifact.original_file_name)
        )
        return Response(
            content=storage.read_bytes(artifact.storage_uri),
            media_type=artifact.content_type,
            headers=headers,
        )

    @router.get("/api/application-packages/{package_id}/documents/{document_id}/word")
    def download_editable_package_document(
        package_id: str,
        document_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        _package, application, job = require_active_package_job(db, package_id)
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.application_package_id == package_id,
                ApplicationPackageDocument.generated_document_id == document_id,
            )
        ).scalar_one_or_none()
        document = db.get(GeneratedDocument, document_id)
        artifact = (
            db.get(StoredArtifact, document.editable_artifact_id)
            if document and document.editable_artifact_id
            else None
        )
        if (
            not package_document
            or not document
            or not artifact
            or document.application_package_id != package_id
            or document.document_type != "cover_letter"
            or package_document.artifact_id != document.artifact_id
            or artifact.artifact_type != "generated_document_editable"
            or artifact.content_type != DOCX_CONTENT_TYPE
            or artifact.application_id != application.application_id
            or artifact.job_id != job.id
            or artifact.candidate_id != application.candidate_id
            or artifact.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Editable package document not found")
        return Response(
            content=storage.read_bytes(artifact.storage_uri),
            media_type=DOCX_CONTENT_TYPE,
            headers=attachment_headers(artifact.original_file_name),
        )

    return router
