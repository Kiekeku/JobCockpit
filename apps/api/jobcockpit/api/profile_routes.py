from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jobcockpit.api.cover_letter_generation_runs import (
    wake_cover_letter_generations_waiting_for_profile,
)
from jobcockpit.api.file_responses import attachment_headers
from jobcockpit.api.transcripts import create_transcript_router, transcript_state
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    EvidenceItem,
    StoredArtifact,
)
from jobcockpit.profiles import parse_resume_bytes
from jobcockpit.schemas.requests import (
    EvidencePatchRequest,
    GRADUATION_MONTHS,
    ProfileRequest,
    normalize_graduation_month,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.services.profile_uploads import (
    PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND as PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND,
    PROFILE_RESUME_SOURCE_KIND as PROFILE_RESUME_SOURCE_KIND,
    ProfileDocumentUploadResult,
    ProfileUploadKind,
    upload_profile_document,
)


CANDIDATE_ID = "candidate_local"
BACKEND_PROFILE_DATA_KEYS = {
    "latest_resume_artifact_id",
    "latest_resume_parse_status",
    "latest_cover_letter_sample_artifact_id",
    "latest_cover_letter_sample_parse_status",
}
PROFILE_DATA_DEFAULTS = {
    "street_address": "",
    "address_line_2": "",
    "city": "",
    "province_state": "",
    "postal_code": "",
    "country": "",
    "linkedin_url": "",
    "github_url": "",
    "current_year": "",
    "graduation_date": "",
    "graduation_month": "",
    "graduation_year": "",
    "gpa": "",
    "co_op_program": None,
    "willing_to_relocate": None,
    "work_authorized_countries": [],
    "requires_sponsorship": None,
    "cover_letter_custom_instructions": "",
    "target_role_include_keywords": [],
    "target_role_exclude_keywords": [],
    "language_filter_enabled": False,
    "languages": [
        {"language": "English", "proficiency": "fluent"},
        {"language": "French", "proficiency": "none"},
    ],
}


def artifact_to_api(artifact: StoredArtifact) -> dict:
    return {
        "artifact_id": artifact.artifact_id,
        "candidate_id": artifact.candidate_id,
        "artifact_type": artifact.artifact_type,
        "storage_backend": artifact.storage_backend,
        "original_file_name": artifact.original_file_name,
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "privacy_class": artifact.privacy_class,
        "source_kind": artifact.source_kind,
    }


def evidence_to_api(evidence: EvidenceItem) -> dict:
    return {
        "evidence_id": evidence.evidence_id,
        "candidate_id": evidence.candidate_id,
        "source_type": evidence.source_type,
        "source_artifact_id": evidence.source_artifact_id,
        "claim": evidence.claim,
        "skills": evidence.skills,
        "private": evidence.private,
        "allowed_for_resume": evidence.allowed_for_resume,
        "allowed_for_cover_letter": evidence.allowed_for_cover_letter,
    }


def profile_upload_to_api(result: ProfileDocumentUploadResult) -> dict:
    response = {
        "artifact": artifact_to_api(result.artifact),
        "parse_status": result.parsed.parse_status,
        "warnings": result.parsed.warnings,
    }
    if result.evidence_items is not None:
        response["evidence_created_count"] = len(result.evidence_items)
        response["evidence_items"] = [evidence_to_api(item) for item in result.evidence_items]
    return response


def profile_to_api(profile: CandidateProfile | None) -> dict | None:
    if not profile:
        return None
    return {
        "candidate_id": profile.candidate_id,
        **normalize_profile_data(profile.profile_data, profile.profile_name),
    }


def profile_request_data(
    request: ProfileRequest, existing_profile_data: dict | None = None
) -> dict:
    data = request.model_dump()
    data["full_name"] = request.full_name
    normalize_graduation_profile_fields(
        data,
        structured_fields_supplied=bool(
            request.model_fields_set & {"graduation_month", "graduation_year"}
        ),
    )
    for key in BACKEND_PROFILE_DATA_KEYS:
        if existing_profile_data and key in existing_profile_data:
            data[key] = existing_profile_data[key]
    return data


def normalize_profile_data(profile_data: dict, profile_name: str | None = None) -> dict:
    data = dict(profile_data)
    for key, value in PROFILE_DATA_DEFAULTS.items():
        data.setdefault(key, value)
    normalize_graduation_profile_fields(data)
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    full_name = str(data.get("full_name") or profile_name or "").strip()
    if (not first_name or not last_name) and full_name:
        legacy_first_name, legacy_last_name = split_full_name(full_name)
        first_name = first_name or legacy_first_name
        last_name = last_name or legacy_last_name
    data["first_name"] = first_name
    data["last_name"] = last_name
    data["full_name"] = f"{first_name} {last_name}".strip() or full_name
    data.pop("location", None)
    return data


def normalize_graduation_profile_fields(
    data: dict, *, structured_fields_supplied: bool = False
) -> None:
    month = str(data.get("graduation_month") or "").strip()
    year = str(data.get("graduation_year") or "").strip()
    date = str(data.get("graduation_date") or "").strip()
    if month:
        try:
            month = normalize_graduation_month(month)
        except ValueError:
            month = ""
    if year and not re.fullmatch(r"\d{4}", year):
        year = ""
    if not month and not year and date:
        month, year = split_graduation_date(date)
    data["graduation_month"] = month
    data["graduation_year"] = year
    structured_date = " ".join(part for part in (month, year) if part)
    if month or not date:
        data["graduation_date"] = structured_date
    elif structured_fields_supplied and date == structured_date:
        data["graduation_date"] = structured_date
    else:
        data["graduation_date"] = date


def split_graduation_date(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    if not cleaned:
        return "", ""
    month = ""
    year = ""
    for candidate in GRADUATION_MONTHS:
        if re.search(rf"\b{re.escape(candidate)}\b", cleaned, re.IGNORECASE):
            month = candidate
            break
    if not month:
        for candidate in GRADUATION_MONTHS:
            if re.search(rf"\b{re.escape(candidate[:3])}\b", cleaned, re.IGNORECASE):
                month = candidate
                break
    year_match = re.search(r"\b(19|20)\d{2}\b", cleaned)
    if year_match:
        year = year_match.group(0)
    return month, year


def split_full_name(value: str) -> tuple[str, str]:
    parts = value.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def profile_state(db: Session) -> dict:
    profile = db.get(CandidateProfile, CANDIDATE_ID)
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
    cover_letter_sample_artifacts = list(
        db.execute(
            select(StoredArtifact)
            .where(
                StoredArtifact.candidate_id == CANDIDATE_ID,
                StoredArtifact.artifact_type == "candidate_document",
                StoredArtifact.source_kind == PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND,
                StoredArtifact.deleted_at.is_(None),
            )
            .order_by(StoredArtifact.created_at.desc())
        ).scalars()
    )
    evidence_items = list(
        db.execute(
            select(EvidenceItem)
            .where(EvidenceItem.candidate_id == CANDIDATE_ID)
            .order_by(EvidenceItem.created_at.asc())
        ).scalars()
    )
    return {
        "profile": profile_to_api(profile),
        **transcript_state(db, CANDIDATE_ID),
        "resume_artifacts": [artifact_to_api(artifact) for artifact in resume_artifacts],
        "cover_letter_sample_artifacts": [
            artifact_to_api(artifact) for artifact in cover_letter_sample_artifacts
        ],
        "evidence_items": [evidence_to_api(evidence) for evidence in evidence_items],
    }


def is_profile_pdf_resume_artifact(artifact: StoredArtifact | None, candidate_id: str) -> bool:
    if not artifact or artifact.deleted_at is not None:
        return False
    content_type = artifact.content_type.split(";", 1)[0].strip().lower()
    return (
        artifact.candidate_id == candidate_id
        and artifact.artifact_type == "candidate_document"
        and artifact.source_kind == PROFILE_RESUME_SOURCE_KIND
        and (
            content_type == "application/pdf"
            or artifact.original_file_name.lower().endswith(".pdf")
        )
    )


def latest_profile_pdf_resume_artifact(db: Session, candidate_id: str) -> StoredArtifact | None:
    artifacts = db.execute(
        select(StoredArtifact)
        .where(
            StoredArtifact.candidate_id == candidate_id,
            StoredArtifact.artifact_type == "candidate_document",
            StoredArtifact.source_kind == PROFILE_RESUME_SOURCE_KIND,
            StoredArtifact.deleted_at.is_(None),
        )
        .order_by(StoredArtifact.created_at.desc())
    ).scalars()
    return next(
        (
            artifact
            for artifact in artifacts
            if is_profile_pdf_resume_artifact(artifact, candidate_id)
        ),
        None,
    )


def latest_cover_letter_sample_artifact(db: Session, candidate_id: str) -> StoredArtifact | None:
    return (
        db.execute(
            select(StoredArtifact)
            .where(
                StoredArtifact.candidate_id == candidate_id,
                StoredArtifact.artifact_type == "candidate_document",
                StoredArtifact.source_kind == PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND,
                StoredArtifact.deleted_at.is_(None),
            )
            .order_by(StoredArtifact.created_at.desc())
        )
        .scalars()
        .first()
    )


def parse_artifact_text(
    artifact: StoredArtifact | None, storage: ArtifactStorageService
) -> str | None:
    if not artifact:
        return None
    try:
        parsed = parse_resume_bytes(
            artifact.original_file_name,
            storage.read_bytes(artifact.storage_uri),
        )
    except Exception:
        return None
    return parsed.text if parsed.parse_status == "parsed" else None


def profile_document_parse_status(artifact: StoredArtifact, storage: ArtifactStorageService) -> str:
    try:
        parsed = parse_resume_bytes(
            artifact.original_file_name,
            storage.read_bytes(artifact.storage_uri),
        )
    except Exception:
        return "stored_unparsed"
    return parsed.parse_status


def docx_artifact_bytes(
    artifact: StoredArtifact | None, storage: ArtifactStorageService
) -> bytes | None:
    if not artifact or not artifact.original_file_name.lower().endswith(".docx"):
        return None
    try:
        return storage.read_bytes(artifact.storage_uri)
    except Exception:
        return None


def delete_profile_document_artifact(
    *,
    db: Session,
    storage: ArtifactStorageService,
    artifact_id: str,
    source_kind: str,
    latest_artifact_key: str,
    latest_parse_status_key: str,
    not_found_detail: str,
    audit_action: str,
) -> dict:
    profile = db.get(CandidateProfile, CANDIDATE_ID)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    artifact = db.get(StoredArtifact, artifact_id)
    if (
        not artifact
        or artifact.candidate_id != CANDIDATE_ID
        or artifact.artifact_type != "candidate_document"
        or artifact.source_kind != source_kind
        or artifact.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail=not_found_detail)

    artifact.deleted_at = datetime.now(UTC)
    was_latest_artifact = profile.profile_data.get(latest_artifact_key) == artifact.artifact_id
    if source_kind == PROFILE_RESUME_SOURCE_KIND:
        linked_resume_evidence = db.execute(
            delete(EvidenceItem).where(
                EvidenceItem.candidate_id == CANDIDATE_ID,
                EvidenceItem.source_type == "resume_upload",
                EvidenceItem.source_artifact_id == artifact.artifact_id,
            )
        )
        if linked_resume_evidence.rowcount == 0:
            db.execute(
                delete(EvidenceItem).where(
                    EvidenceItem.candidate_id == CANDIDATE_ID,
                    EvidenceItem.source_type == "resume_upload",
                    EvidenceItem.source_artifact_id.is_(None),
                )
            )
    if was_latest_artifact:
        promoted_artifact = (
            db.execute(
                select(StoredArtifact)
                .where(
                    StoredArtifact.candidate_id == CANDIDATE_ID,
                    StoredArtifact.artifact_type == "candidate_document",
                    StoredArtifact.source_kind == source_kind,
                    StoredArtifact.artifact_id != artifact.artifact_id,
                    StoredArtifact.deleted_at.is_(None),
                )
                .order_by(StoredArtifact.created_at.desc())
            )
            .scalars()
            .first()
        )
        profile.profile_data = {
            **profile.profile_data,
            latest_artifact_key: promoted_artifact.artifact_id if promoted_artifact else None,
            latest_parse_status_key: profile_document_parse_status(promoted_artifact, storage)
            if promoted_artifact
            else "",
        }
    storage.delete(artifact.storage_uri)
    db.add(AuditLogEntry.create(audit_action, "stored_artifact", artifact.artifact_id))
    db.flush()
    return profile_state(db)


def resolve_cover_letter_resume_artifact(
    db: Session,
    candidate_id: str,
    resume_artifact_id: str | None,
) -> StoredArtifact | None:
    if not resume_artifact_id:
        return latest_profile_pdf_resume_artifact(db, candidate_id)
    artifact = db.get(StoredArtifact, resume_artifact_id)
    if not is_profile_pdf_resume_artifact(artifact, candidate_id):
        raise HTTPException(status_code=400, detail="Resume artifact must be a profile PDF resume")
    return artifact


def create_profile_router(
    *,
    get_db: Callable[..., Session],
    get_storage: Callable[..., ArtifactStorageService],
) -> APIRouter:
    router = APIRouter(prefix="/api/profile", tags=["profile"])
    router.include_router(create_transcript_router(
        candidate_id=CANDIDATE_ID, get_db=get_db, get_storage=get_storage,
    ))

    @router.get("")
    def get_profile(db: Session = Depends(get_db)) -> dict:
        return profile_state(db)

    @router.post("", status_code=201)
    def create_profile(request: ProfileRequest, db: Session = Depends(get_db)) -> dict:
        if db.get(CandidateProfile, CANDIDATE_ID):
            raise HTTPException(status_code=409, detail="Profile already exists")
        profile = CandidateProfile(
            candidate_id=CANDIDATE_ID,
            profile_name=request.full_name,
            profile_data=profile_request_data(request),
        )
        db.add(profile)
        db.add(AuditLogEntry.create("profile.created", "candidate_profile", CANDIDATE_ID))
        db.flush()
        wake_cover_letter_generations_waiting_for_profile(db)
        return profile_state(db)

    @router.put("")
    def update_profile(request: ProfileRequest, db: Session = Depends(get_db)) -> dict:
        profile = db.get(CandidateProfile, CANDIDATE_ID)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        profile.profile_name = request.full_name
        profile.profile_data = profile_request_data(request, profile.profile_data)
        profile.updated_at = datetime.now(UTC)
        db.add(AuditLogEntry.create("profile.updated", "candidate_profile", CANDIDATE_ID))
        db.flush()
        return profile_state(db)

    @router.post("/resume", status_code=201)
    async def upload_profile_resume(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        result = await upload_profile_document(
            file, db, storage, candidate_id=CANDIDATE_ID, kind=ProfileUploadKind.RESUME,
        )
        return profile_upload_to_api(result)

    @router.post("/cover-letter-sample", status_code=201)
    async def upload_profile_cover_letter_sample(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        result = await upload_profile_document(
            file, db, storage, candidate_id=CANDIDATE_ID, kind=ProfileUploadKind.COVER_LETTER_SAMPLE,
        )
        return profile_upload_to_api(result)

    @router.delete("/resume/{artifact_id}")
    def delete_profile_resume(
        artifact_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        return delete_profile_document_artifact(
            db=db,
            storage=storage,
            artifact_id=artifact_id,
            source_kind=PROFILE_RESUME_SOURCE_KIND,
            latest_artifact_key="latest_resume_artifact_id",
            latest_parse_status_key="latest_resume_parse_status",
            not_found_detail="Resume artifact not found",
            audit_action="profile.resume_deleted",
        )

    @router.delete("/cover-letter-sample/{artifact_id}")
    def delete_profile_cover_letter_sample(
        artifact_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        return delete_profile_document_artifact(
            db=db,
            storage=storage,
            artifact_id=artifact_id,
            source_kind=PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND,
            latest_artifact_key="latest_cover_letter_sample_artifact_id",
            latest_parse_status_key="latest_cover_letter_sample_parse_status",
            not_found_detail="Cover letter sample artifact not found",
            audit_action="profile.cover_letter_sample_deleted",
        )

    @router.get("/resume/{artifact_id}/download")
    def download_profile_resume(
        artifact_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        artifact = db.get(StoredArtifact, artifact_id)
        if (
            not artifact
            or artifact.candidate_id != CANDIDATE_ID
            or artifact.artifact_type != "candidate_document"
            or artifact.source_kind != PROFILE_RESUME_SOURCE_KIND
            or artifact.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Resume artifact not found")
        return Response(
            content=storage.read_bytes(artifact.storage_uri),
            media_type=artifact.content_type,
            headers=attachment_headers(artifact.original_file_name),
        )

    @router.patch("/evidence/{evidence_id}")
    def update_profile_evidence(
        evidence_id: str,
        request: EvidencePatchRequest,
        db: Session = Depends(get_db),
    ) -> dict:
        evidence = db.get(EvidenceItem, evidence_id)
        if not evidence or evidence.candidate_id != CANDIDATE_ID:
            raise HTTPException(status_code=404, detail="Evidence item not found")
        for field, value in request.model_dump(exclude_unset=True).items():
            setattr(evidence, field, value)
        evidence.updated_at = datetime.now(UTC)
        db.flush()
        return evidence_to_api(evidence)

    @router.delete("/evidence/{evidence_id}", status_code=204)
    def delete_profile_evidence(evidence_id: str, db: Session = Depends(get_db)) -> Response:
        evidence = db.get(EvidenceItem, evidence_id)
        if not evidence or evidence.candidate_id != CANDIDATE_ID:
            raise HTTPException(status_code=404, detail="Evidence item not found")
        db.delete(evidence)
        db.flush()
        return Response(status_code=204)

    return router
