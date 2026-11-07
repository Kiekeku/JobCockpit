"""Shared private-document upload handling for candidate profiles."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from jobcockpit.api.file_responses import content_type_for_extension
from jobcockpit.db.models import AuditLogEntry, CandidateProfile, EvidenceItem, StoredArtifact
from jobcockpit.ingestion.concurrency import track_ingestion_artifact
from jobcockpit.profiles import (
    MAX_RESUME_UPLOAD_BYTES,
    SUPPORTED_RESUME_EXTENSIONS,
    ParsedResume,
    extract_resume_evidence,
    parse_resume_bytes,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


PROFILE_RESUME_SOURCE_KIND = "profile_resume_upload"
PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND = "profile_cover_letter_sample_upload"


class ProfileUploadKind(str, Enum):
    RESUME = "resume"
    COVER_LETTER_SAMPLE = "cover_letter_sample"


@dataclass(frozen=True)
class _UploadPolicy:
    default_filename: str
    label: str
    source_kind: str
    extract_evidence: bool = False


_UPLOAD_POLICIES = {
    ProfileUploadKind.RESUME: _UploadPolicy(
        "resume", "resume", PROFILE_RESUME_SOURCE_KIND, extract_evidence=True,
    ),
    ProfileUploadKind.COVER_LETTER_SAMPLE: _UploadPolicy(
        "cover-letter-sample", "cover letter sample", PROFILE_COVER_LETTER_SAMPLE_SOURCE_KIND,
    ),
}


@dataclass(frozen=True)
class ProfileDocumentUploadResult:
    artifact: StoredArtifact
    parsed: ParsedResume
    evidence_items: tuple[EvidenceItem, ...] | None


async def upload_profile_document(
    file: UploadFile, db: Session, storage: ArtifactStorageService,
    *, candidate_id: str, kind: ProfileUploadKind,
) -> ProfileDocumentUploadResult:
    """Store one document and its metadata in the caller's transaction.

    Unreadable supported files remain available as private source documents.
    Only resumes create claim evidence. A failed transaction removes new bytes.
    """
    profile = db.get(CandidateProfile, candidate_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    policy = _UPLOAD_POLICIES[kind]
    filename = file.filename or policy.default_filename
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in SUPPORTED_RESUME_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported {policy.label} file type")
    content = await file.read(MAX_RESUME_UPLOAD_BYTES + 1)
    if len(content) > MAX_RESUME_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{policy.label.capitalize()} upload exceeds 5 MB")

    artifact = storage.write_bytes(
        artifact_type="candidate_document", content=content, original_file_name=filename,
        content_type=file.content_type or content_type_for_extension(extension),
        candidate_id=candidate_id, privacy_class="private", source_kind=policy.source_kind,
    )
    track_ingestion_artifact(db, storage, artifact.storage_uri)
    artifact_row = StoredArtifact.from_record(artifact)
    db.add(artifact_row)
    db.flush()

    parsed = parse_resume_bytes(filename, content)
    evidence_rows = []
    if policy.extract_evidence and parsed.parse_status == "parsed":
        for item in extract_resume_evidence(parsed.text, profile.profile_data.get("skills") or []):
            evidence = EvidenceItem(
                evidence_id=new_id("evidence"), candidate_id=candidate_id,
                source_type="resume_upload", source_artifact_id=artifact_row.artifact_id,
                claim=item["claim"], skills=item["skills"], private=True,
                allowed_for_resume=True, allowed_for_cover_letter=True,
            )
            db.add(evidence)
            evidence_rows.append(evidence)
    profile.profile_data = {
        **profile.profile_data,
        f"latest_{kind.value}_artifact_id": artifact_row.artifact_id,
        f"latest_{kind.value}_parse_status": parsed.parse_status,
    }
    db.add(AuditLogEntry.create(f"profile.{kind.value}_uploaded", "stored_artifact", artifact_row.artifact_id))
    db.flush()
    return ProfileDocumentUploadResult(
        artifact_row, parsed, tuple(evidence_rows) if policy.extract_evidence else None,
    )
