from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from jobcockpit.api.job_access import require_active_package_job
from jobcockpit.api.profile_routes import (
    CANDIDATE_ID,
    PROFILE_RESUME_SOURCE_KIND,
    artifact_to_api,
    docx_artifact_bytes,
    is_profile_pdf_resume_artifact,
    latest_cover_letter_sample_artifact,
    normalize_profile_data,
    parse_artifact_text,
    resolve_cover_letter_resume_artifact,
)
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    ApplicationPackage,
    ApplicationPackageDocument,
    AuditLogEntry,
    CandidateProfile,
    EvidenceItem,
    GeneratedDocument,
    JobPosting,
    StoredArtifact,
)
from jobcockpit.documents.cover_letters import (
    CoverLetterDraftContext,
    CoverLetterGenerationError,
    CoverLetterVerificationError,
    build_cover_letter_docx_bytes,
    build_cover_letter_pdf_bytes,
    cover_letter_docx_filename,
    cover_letter_pdf_filename,
    docx_cover_letter_to_markdown,
    extract_pdf_text,
    generate_cover_letter_markdown,
    render_verified_cover_letter_pdf,
)
from jobcockpit.documents.company_profiles import (
    cached_company_profile,
    company_profile_identity_hints,
    get_or_research_company_profile,
)
from jobcockpit.documents.generator import select_evidence_for_job
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


logger = logging.getLogger(__name__)
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_COVER_LETTER_DOCX_UPLOAD_BYTES = 5 * 1024 * 1024


def package_document_to_api(
    package_document: ApplicationPackageDocument, document: GeneratedDocument
) -> dict:
    payload = document.to_api()
    payload.update(
        {
            "application_package_document_id": package_document.application_package_document_id,
            "approved_for_upload": package_document.approved_for_upload,
            "required": package_document.required,
        }
    )
    return payload


def cover_letter_document_to_api(
    package_document: ApplicationPackageDocument,
    document: GeneratedDocument,
    artifact: StoredArtifact,
    editable_artifact: StoredArtifact | None = None,
) -> dict:
    if (
        editable_artifact is not None
        and (
            editable_artifact.deleted_at is not None
            or editable_artifact.artifact_type != "generated_document_editable"
            or editable_artifact.content_type != DOCX_CONTENT_TYPE
        )
    ):
        editable_artifact = None
    payload = package_document_to_api(package_document, document)
    base_url = (
        f"/api/application-packages/{document.application_package_id}"
        f"/documents/{document.document_id}/download"
    )
    payload.update(
        {
            "original_file_name": artifact.original_file_name,
            "content_type": artifact.content_type,
            "size_bytes": artifact.size_bytes,
            "preview_url": base_url,
            "download_url": f"{base_url}?download=true",
            "word_original_file_name": (
                editable_artifact.original_file_name if editable_artifact else None
            ),
            "word_size_bytes": editable_artifact.size_bytes if editable_artifact else None,
            "word_download_url": (
                f"/api/application-packages/{document.application_package_id}"
                f"/documents/{document.document_id}/word"
                if editable_artifact
                else None
            ),
        }
    )
    return payload


def attach_generated_document_to_package(
    db: Session,
    package_id: str,
    document: GeneratedDocument,
    *,
    required: bool,
) -> ApplicationPackageDocument:
    package_document = ApplicationPackageDocument(
        application_package_document_id=new_id("pkgdoc"),
        application_package_id=package_id,
        generated_document_id=document.document_id,
        artifact_id=document.artifact_id,
        document_type=document.document_type,
        required=required,
        approved_for_upload=False,
    )
    db.add(package_document)
    return package_document


def job_requires_cover_letter(job: JobPosting) -> bool:
    application = job.application or {}
    required_documents = {
        str(document).strip().lower().replace("-", "_").replace(" ", "_")
        for document in application.get("documents_required") or []
    }
    return bool(application.get("cover_letter_required")) or "cover_letter" in required_documents


def begin_serialized_package_write(db: Session) -> None:
    """End the read phase and serialize the short SQLite package write phase."""
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def next_application_package_version(db: Session, application_id: str) -> int:
    current_version = db.execute(
        select(func.max(ApplicationPackage.package_version)).where(
            ApplicationPackage.application_id == application_id
        )
    ).scalar_one()
    return int(current_version or 0) + 1


@dataclass(frozen=True)
class PreparedCoverLetterDocument:
    content: str
    pdf_bytes: bytes
    docx_bytes: bytes
    profile_data: dict[str, Any] | None
    claim_mapping_count: int
    generation_provider: str | None = None


def prepare_cover_letter_document(
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
    candidate_id: str,
    job: JobPosting,
    operation_id: str,
    resume_artifact_id: str | None = None,
    use_latest_resume: bool = True,
) -> PreparedCoverLetterDocument:
    profile = db.get(CandidateProfile, candidate_id)
    profile_data = profile.profile_data if profile else None
    evidence_items = list(
        db.execute(
            select(EvidenceItem).where(
                EvidenceItem.candidate_id == candidate_id,
                EvidenceItem.allowed_for_cover_letter.is_(True),
            )
        ).scalars()
    )
    selected_evidence_items = select_evidence_for_job(job, evidence_items, 3)
    resume_artifact = resolve_cover_letter_resume_artifact(
        db,
        candidate_id,
        resume_artifact_id,
    ) if resume_artifact_id or use_latest_resume else None
    sample_artifact = latest_cover_letter_sample_artifact(db, candidate_id)
    template_docx_bytes = docx_artifact_bytes(sample_artifact, storage)
    company_name = str(job.employer.get("company_name") or job.company_name or "").strip()
    company_profile = None
    if company_name and settings.cover_letter_generator_provider == "codex_cli":
        company_profile = get_or_research_company_profile(
            settings,
            company_name,
            job_id=job.id,
            identity_hints=company_profile_identity_hints(job),
        )
    context = CoverLetterDraftContext(
        job=job,
        profile_data=profile_data,
        evidence_items=selected_evidence_items,
        resume_text=parse_artifact_text(resume_artifact, storage),
        sample_cover_letter_text=parse_artifact_text(sample_artifact, storage),
        custom_instructions=(profile_data or {}).get("cover_letter_custom_instructions"),
        company_profile=company_profile.to_context() if company_profile else None,
    )
    try:
        content = generate_cover_letter_markdown(settings, context)
    except CoverLetterGenerationError as error:
        logger.warning(
            "Cover letter generation failed for %s with provider %s: %s",
            operation_id,
            error.provider,
            error.reason,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Cover letter generation failed with provider {error.provider}; "
                "no fallback draft was created."
            ),
        ) from error
    try:
        content, pdf_bytes = render_verified_cover_letter_pdf(
            settings,
            context,
            content,
            template_docx_bytes=template_docx_bytes,
        )
    except CoverLetterVerificationError as error:
        logger.warning(
            "Cover letter verification failed for %s with provider %s: %s",
            operation_id,
            error.provider,
            error.reason,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Cover letter PDF verification failed with provider {error.provider}; "
                "no draft was created."
            ),
        ) from error
    return PreparedCoverLetterDocument(
        content=content,
        pdf_bytes=pdf_bytes,
        docx_bytes=build_cover_letter_docx_bytes(
            content,
            template_docx_bytes=template_docx_bytes,
        ),
        profile_data=profile_data,
        claim_mapping_count=len(selected_evidence_items),
    )


def prepare_cover_letter_word_revision(
    content: bytes,
    *,
    profile_data: dict[str, Any] | None,
) -> PreparedCoverLetterDocument:
    markdown = docx_cover_letter_to_markdown(content)
    pdf_bytes = build_cover_letter_pdf_bytes(markdown, template_docx_bytes=content)
    if not extract_pdf_text(pdf_bytes).strip():
        raise ValueError("Word cover letter could not be rendered")
    return PreparedCoverLetterDocument(
        content=markdown,
        pdf_bytes=pdf_bytes,
        docx_bytes=content,
        profile_data=profile_data,
        claim_mapping_count=0,
        generation_provider="manual_docx_edit",
    )


def persist_cover_letter_package_document(
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
    package_id: str,
    application: Application,
    job: JobPosting,
    resume_artifact_id: str | None = None,
    prepared: PreparedCoverLetterDocument | None = None,
) -> dict:
    if prepared is None:
        prepared = prepare_cover_letter_document(
            db=db,
            storage=storage,
            settings=settings,
            candidate_id=application.candidate_id,
            job=job,
            operation_id=f"package {package_id}",
            resume_artifact_id=resume_artifact_id,
        )
    artifact = storage.write_bytes(
        artifact_type="generated_document",
        content=prepared.pdf_bytes,
        original_file_name=cover_letter_pdf_filename(prepared.profile_data, job),
        content_type="application/pdf",
        candidate_id=application.candidate_id,
        application_id=application.application_id,
        job_id=job.id,
        privacy_class="private",
    )
    artifact_row = StoredArtifact.from_record(artifact)
    db.add(artifact_row)
    db.flush()
    editable_artifact = storage.write_bytes(
        artifact_type="generated_document_editable",
        content=prepared.docx_bytes,
        original_file_name=cover_letter_docx_filename(prepared.profile_data, job),
        content_type=DOCX_CONTENT_TYPE,
        candidate_id=application.candidate_id,
        application_id=application.application_id,
        job_id=job.id,
        privacy_class="private",
        source_kind="cover_letter_editable",
    )
    editable_artifact_row = StoredArtifact.from_record(editable_artifact)
    db.add(editable_artifact_row)
    db.flush()
    document = GeneratedDocument(
        document_id=new_id("doc"),
        application_package_id=package_id,
        document_type="cover_letter",
        artifact_id=artifact_row.artifact_id,
        editable_artifact_id=editable_artifact_row.artifact_id,
        generation_provider=(
            prepared.generation_provider or settings.cover_letter_generator_provider
        ),
        source_format="markdown",
        rendered_format="pdf",
        sha256=artifact.sha256,
        content_preview=prepared.content,
        claim_mapping_count=prepared.claim_mapping_count,
        approved_by_user=False,
    )
    db.add(document)
    db.flush()
    package_document = attach_generated_document_to_package(
        db,
        package_id,
        document,
        required=job_requires_cover_letter(job),
    )
    db.add(AuditLogEntry.create("document.generated", "generated_document", document.document_id))
    db.flush()
    return cover_letter_document_to_api(
        package_document,
        document,
        artifact_row,
        editable_artifact_row,
    )


def create_cover_letter_package_document(
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
    package_id: str,
    resume_artifact_id: str | None = None,
    prepared: PreparedCoverLetterDocument | None = None,
) -> dict:
    _package, application, job = require_active_package_job(db, package_id)
    return persist_cover_letter_package_document(
        db=db,
        storage=storage,
        settings=settings,
        package_id=package_id,
        application=application,
        job=job,
        resume_artifact_id=resume_artifact_id,
        prepared=prepared,
    )

def cover_letter_generator_to_api(settings: Settings) -> dict:
    provider = str(settings.cover_letter_generator_provider or "heuristic").lower()
    return {
        "provider": provider,
        "uses_codex": provider == "codex_cli",
        "company_profile_cache_enabled": (
            provider == "codex_cli"
            and settings.company_profile_research_provider != "disabled"
        ),
        "strict": provider not in {"", "heuristic", "disabled"},
    }


def cover_letter_workspace_to_api(
    db: Session,
    settings: Settings,
    job: JobPosting,
) -> dict:
    application = db.execute(
        select(Application).where(Application.job_id == job.id)
    ).scalar_one_or_none()
    profile = db.get(CandidateProfile, CANDIDATE_ID)
    profile_data = (
        normalize_profile_data(profile.profile_data, profile.profile_name) if profile else {}
    )
    company_name = str(job.employer.get("company_name") or job.company_name or "").strip()
    company_profile = (
        cached_company_profile(
            db,
            company_name,
            job_id=job.id,
            identity_hints=company_profile_identity_hints(job),
        )
        if company_name
        else None
    )
    resume_artifacts = list(
        db.execute(
            select(StoredArtifact)
            .where(
                StoredArtifact.candidate_id == CANDIDATE_ID,
                StoredArtifact.artifact_type == "candidate_document",
                StoredArtifact.source_kind == PROFILE_RESUME_SOURCE_KIND,
                StoredArtifact.deleted_at.is_(None),
            )
            .order_by(StoredArtifact.created_at.desc())
        ).scalars()
    )
    pdf_resumes = [
        artifact
        for artifact in resume_artifacts
        if is_profile_pdf_resume_artifact(artifact, CANDIDATE_ID)
    ]
    latest_package = None
    latest_document = None
    if application:
        latest_row = db.execute(
            select(
                ApplicationPackage,
                ApplicationPackageDocument,
                GeneratedDocument,
                StoredArtifact,
            )
            .join(
                ApplicationPackageDocument,
                ApplicationPackageDocument.application_package_id
                == ApplicationPackage.application_package_id,
            )
            .join(
                GeneratedDocument,
                GeneratedDocument.document_id == ApplicationPackageDocument.generated_document_id,
            )
            .join(StoredArtifact, StoredArtifact.artifact_id == GeneratedDocument.artifact_id)
            .where(
                ApplicationPackage.application_id == application.application_id,
                GeneratedDocument.document_type == "cover_letter",
                StoredArtifact.deleted_at.is_(None),
            )
            .order_by(
                ApplicationPackage.package_version.desc(),
                GeneratedDocument.created_at.desc(),
            )
        ).first()
        if latest_row:
            package, package_document, document, artifact = latest_row
            latest_package = package.to_api()
            latest_document = cover_letter_document_to_api(
                package_document,
                document,
                artifact,
                db.get(StoredArtifact, document.editable_artifact_id)
                if document.editable_artifact_id
                else None,
            )

    resume_payloads = []
    for artifact in pdf_resumes:
        payload = artifact_to_api(artifact)
        payload["download_url"] = f"/api/profile/resume/{artifact.artifact_id}/download"
        resume_payloads.append(payload)
    return {
        "generator": cover_letter_generator_to_api(settings),
        "profile": {
            "exists": profile is not None,
            "full_name": profile_data.get("full_name") or "",
            "resume_artifacts": resume_payloads,
            "cover_letter_sample_configured": latest_cover_letter_sample_artifact(db, CANDIDATE_ID)
            is not None,
            "custom_instructions_configured": bool(
                str(profile_data.get("cover_letter_custom_instructions") or "").strip()
            ),
        },
        "company_profile": company_profile.to_context() if company_profile else None,
        "application": application.to_api() if application else None,
        "latest_package": latest_package,
        "latest_document": latest_document,
    }
