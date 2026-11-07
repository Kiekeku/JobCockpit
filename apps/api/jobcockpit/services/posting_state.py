"""Shared posting field mapping and artifact preparation, without database writes."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobcockpit.api.file_responses import source_archive_file_name, source_archive_metadata
from jobcockpit.db.models import StoredArtifact
from jobcockpit.ingestion.concurrency import track_ingestion_artifact
from jobcockpit.ingestion.inputs import JobIngestPayload
from jobcockpit.ingestion.parser import ParsedJob
from jobcockpit.storage.artifacts import ArtifactStorageService


def parsed_posting_values(parsed: ParsedJob, *, include_fit: bool = True) -> dict:
    values = {
        "company_name": parsed.employer.company_name,
        "normalized_title": parsed.role.normalized_title,
        "role_type": parsed.role.role_type,
        "application_deadline": parsed.application.application_deadline,
        "duration_months": parsed.employment_timing.duration_months,
        **{name: getattr(parsed, name).model_dump() for name in (
            "employer", "role", "employment_timing", "location", "eligibility",
            "compensation", "application", "data_quality",
        )},
    }
    if include_fit:
        values["fit_analysis"] = parsed.fit_analysis.model_dump()
    return values


@dataclass(frozen=True)
class PreparedPostingArtifacts:
    snapshot: StoredArtifact
    archive: StoredArtifact | None
    created: bool

    @property
    def rows(self) -> tuple[StoredArtifact, ...]:
        return (self.snapshot, self.archive) if self.archive else (self.snapshot,)

    def source_metadata(self, source: dict, request: JobIngestPayload) -> dict:
        if self.archive is None:
            return source
        return {
            **source,
            "source_archive_artifact_id": self.archive.artifact_id,
            "source_archive": source_archive_metadata(request.source_archive, self.archive),
        }

    def track(self, db: Session, storage: ArtifactStorageService) -> None:
        if self.created:
            for row in self.rows:
                track_ingestion_artifact(db, storage, row.storage_uri)

    def attach(self, db: Session, *, job_id: str, candidate_id: str, source_type: str) -> None:
        if not self.created:
            for row in self.rows:
                row.candidate_id = candidate_id
                row.job_id = job_id
            self.snapshot.source_kind = source_type
            if self.archive:
                self.archive.source_kind = f"{source_type}_source_archive"
        db.add_all(self.rows)

    def discard(self, storage: ArtifactStorageService) -> None:
        if self.created:
            for row in self.rows:
                storage.delete(row.storage_uri)


def prepare_posting_artifacts(
    request: JobIngestPayload, storage: ArtifactStorageService, *, job_id: str,
    candidate_id: str, existing: tuple[StoredArtifact, StoredArtifact] | None = None,
) -> PreparedPostingArtifacts:
    if existing:
        return PreparedPostingArtifacts(existing[0], existing[1] if request.source_archive else None, False)
    rows = []
    try:
        record = storage.write_bytes(
            artifact_type="job_snapshot", content=request.raw_text.encode("utf-8"),
            original_file_name="job-posting.txt", content_type="text/plain",
            candidate_id=candidate_id, job_id=job_id, privacy_class="private",
        )
        rows.append(StoredArtifact.from_record(record))
        if request.source_archive:
            archive = request.source_archive
            record = storage.write_bytes(
                artifact_type="job_source_archive", content=archive.body,
                original_file_name=source_archive_file_name(archive), content_type=archive.content_type,
                candidate_id=candidate_id, job_id=job_id, privacy_class="private",
                source_kind=f"{request.source_type}_source_archive",
            )
            rows.append(StoredArtifact.from_record(record))
    except BaseException:
        for row in rows:
            storage.delete(row.storage_uri)
        raise
    return PreparedPostingArtifacts(rows[0], rows[1] if len(rows) > 1 else None, True)
