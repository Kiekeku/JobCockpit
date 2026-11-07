from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.file_responses import attachment_headers, safe_download_filename
from jobcockpit.db.models import AuditLogEntry, CandidateProfile, StoredArtifact
from jobcockpit.storage.artifacts import ArtifactStorageService


TRANSCRIPT_SOURCE_KIND = "profile_transcript_upload"
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024


def transcript_artifacts(db: Session, candidate_id: str) -> list[StoredArtifact]:
    return list(db.scalars(
        select(StoredArtifact).where(
            StoredArtifact.candidate_id == candidate_id,
            StoredArtifact.artifact_type == "candidate_document",
            StoredArtifact.source_kind == TRANSCRIPT_SOURCE_KIND,
            StoredArtifact.deleted_at.is_(None),
        ).order_by(StoredArtifact.created_at.desc(), StoredArtifact.artifact_id.desc())
    ))


def latest_transcript_artifact(db: Session, candidate_id: str) -> StoredArtifact | None:
    return next(iter(transcript_artifacts(db, candidate_id)), None)


def transcript_state(db: Session, candidate_id: str) -> dict:
    artifacts = transcript_artifacts(db, candidate_id)
    return {
        "transcript_artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "original_file_name": artifact.original_file_name,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "created_at": artifact.created_at.replace(tzinfo=UTC).isoformat(),
                "is_latest": index == 0,
                "download_url": f"/api/profile/transcript/{artifact.artifact_id}/download",
            }
            for index, artifact in enumerate(artifacts)
        ],
        "latest_transcript_artifact_id": artifacts[0].artifact_id if artifacts else None,
    }


def create_transcript_router(
    *, candidate_id: str, get_db: Callable, get_storage: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/transcript", tags=["profile"])

    @router.get("")
    def get_transcripts(db: Session = Depends(get_db)) -> dict:
        return transcript_state(db, candidate_id)

    @router.post("", status_code=201)
    async def upload_transcript(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        if not db.get(CandidateProfile, candidate_id):
            raise HTTPException(404, "Create a profile before uploading a transcript")
        filename = safe_download_filename(file.filename or "transcript")
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(400, "Transcript must be a PDF")
        if len(filename) > 180:
            raise HTTPException(400, "Transcript filename is too long")
        content = await file.read(MAX_TRANSCRIPT_BYTES + 1)
        if len(content) > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(413, "Transcript upload exceeds 5 MB")
        try:
            reader = PdfReader(BytesIO(content))
            # Issuers often restrict editing while allowing password-free viewing.
            # Validate readability without rewriting the original signed PDF.
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF requires an opening password")
            if not len(reader.pages):
                raise ValueError("Empty PDF")
        except Exception as error:
            raise HTTPException(
                400, "Upload a valid PDF with at least one page that opens without a password",
            ) from error
        record = storage.write_bytes(
            artifact_type="candidate_document", content=content,
            original_file_name=filename, content_type="application/pdf",
            candidate_id=candidate_id, privacy_class="private",
            source_kind=TRANSCRIPT_SOURCE_KIND,
        )
        try:
            db.add(StoredArtifact.from_record(record))
            db.add(AuditLogEntry.create("profile.transcript_uploaded", "stored_artifact", record.artifact_id))
            db.flush()
            return transcript_state(db, candidate_id)
        except Exception:
            storage.delete(record.storage_uri)
            raise

    @router.get("/{artifact_id}/download")
    def download_transcript(
        artifact_id: str,
        db: Session = Depends(get_db),
        storage: ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        # Resolve latest at request time, never from a cached client selection.
        artifact = (
            latest_transcript_artifact(db, candidate_id) if artifact_id == "latest"
            else db.get(StoredArtifact, artifact_id)
        )
        if (
            not artifact or artifact.candidate_id != candidate_id
            or artifact.artifact_type != "candidate_document"
            or artifact.source_kind != TRANSCRIPT_SOURCE_KIND
            or artifact.deleted_at is not None
        ):
            raise HTTPException(404, "Transcript not found")
        try:
            content = storage.read_bytes(artifact.storage_uri)
        except FileNotFoundError as error:
            raise HTTPException(404, "Stored transcript file is missing") from error
        return Response(
            content=content, media_type="application/pdf",
            headers={**attachment_headers(artifact.original_file_name), "Cache-Control": "no-store"},
        )

    return router
