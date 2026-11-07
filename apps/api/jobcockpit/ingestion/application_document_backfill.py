from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from jobcockpit.db.models import AuditLogEntry, JobPosting, StoredArtifact
from jobcockpit.ingestion.application_documents import (
    APPLICATION_DOCUMENT_AMBIGUITY,
    RequiredDocumentResolution,
    parse_required_documents,
)
from jobcockpit.ingestion.posting_scope import _posting_content_scope
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text
from jobcockpit.storage.artifacts import ArtifactStorageService


@dataclass(frozen=True)
class RequiredDocumentChange:
    job: JobPosting
    resolution: RequiredDocumentResolution
    application: dict
    data_quality: dict
    previous_fields: dict


def backfill_required_documents(
    db: Session,
    storage: ArtifactStorageService,
    *,
    apply: bool = False,
) -> dict:
    jobs = list(db.execute(select(JobPosting).order_by(JobPosting.id)).scalars())
    changes: list[RequiredDocumentChange] = []
    errors: list[dict[str, str]] = []
    distribution: Counter[str] = Counter()

    for job in jobs:
        try:
            artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
            if artifact is None or artifact.deleted_at is not None:
                raise ValueError("raw snapshot artifact is unavailable")
            raw_text = storage.read_bytes(artifact.storage_uri).decode("utf-8")
            posting_text = _posting_content_scope(
                clean_aggregator_posting_text(raw_text, source_url=job.source_url)
            ).text
            resolution = parse_required_documents(posting_text)
            application, data_quality = _updated_payloads(job, resolution)
            distribution[_distribution_key(resolution)] += 1
            previous_fields = _document_fields(job.application or {})
            if application != (job.application or {}) or data_quality != (job.data_quality or {}):
                changes.append(
                    RequiredDocumentChange(
                        job=job,
                        resolution=resolution,
                        application=application,
                        data_quality=data_quality,
                        previous_fields=previous_fields,
                    )
                )
        except (OSError, UnicodeDecodeError, ValueError) as error:
            errors.append({"job_id": job.id, "error": type(error).__name__})

    if apply and not errors:
        _apply_changes(db, changes)

    return {
        "mode": "apply" if apply else "dry_run",
        "scanned_count": len(jobs),
        "changed_count": len(changes),
        "unchanged_count": len(jobs) - len(changes) - len(errors),
        "error_count": len(errors),
        "errors": errors,
        "distribution": dict(sorted(distribution.items())),
        "applied": apply and not errors,
    }


def _updated_payloads(
    job: JobPosting,
    resolution: RequiredDocumentResolution,
) -> tuple[dict, dict]:
    application = dict(job.application or {})
    application.update(
        {
            "documents_required": (
                list(resolution.documents_required)
                if resolution.documents_required is not None
                else None
            ),
            "resume_required": resolution.resume_required,
            "cover_letter_required": resolution.cover_letter_required,
        }
    )

    data_quality = dict(job.data_quality or {})
    previous_ambiguous_fields = [
        str(field) for field in data_quality.get("ambiguous_fields") or []
    ]
    had_document_ambiguity = (
        APPLICATION_DOCUMENT_AMBIGUITY in previous_ambiguous_fields
    )
    ambiguous_fields = [
        field
        for field in previous_ambiguous_fields
        if field != APPLICATION_DOCUMENT_AMBIGUITY
    ]
    if resolution.ambiguous:
        ambiguous_fields.append(APPLICATION_DOCUMENT_AMBIGUITY)
    data_quality["ambiguous_fields"] = ambiguous_fields
    if resolution.ambiguous:
        data_quality["needs_manual_review"] = True
    elif (
        had_document_ambiguity
        and not data_quality.get("missing_fields")
        and not ambiguous_fields
        and not data_quality.get("inferred_fields")
        and float(data_quality.get("extraction_confidence", 0.7)) >= 0.7
    ):
        data_quality["needs_manual_review"] = False
    return application, data_quality


def _apply_changes(db: Session, changes: list[RequiredDocumentChange]) -> None:
    changed_at = datetime.now(UTC)
    for change in changes:
        change.job.application = change.application
        change.job.data_quality = change.data_quality
        change.job.updated_at = changed_at
        db.add(
            AuditLogEntry.create(
                "job.required_documents_backfilled",
                "job_posting",
                change.job.id,
                payload={
                    "previous": change.previous_fields,
                    "current": _document_fields(change.application),
                    "ambiguous": change.resolution.ambiguous,
                },
            )
        )


def _document_fields(application: dict) -> dict:
    return {
        "documents_required": application.get("documents_required"),
        "resume_required": application.get("resume_required"),
        "cover_letter_required": application.get("cover_letter_required"),
    }


def _distribution_key(resolution: RequiredDocumentResolution) -> str:
    if resolution.ambiguous:
        return "ambiguous"
    if resolution.documents_required is None:
        return "not_stated"
    if not resolution.documents_required:
        return "none_required"
    return "+".join(resolution.documents_required)


def create_required_documents_sqlite_backup(database_url: str) -> Path:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise ValueError("required-document backfill backups require a file-backed SQLite database")
    if url.database == ":memory:":
        raise ValueError("cannot back up an in-memory SQLite database")

    source_path = Path(url.database).resolve()
    if not source_path.is_file():
        raise ValueError("SQLite database file is unavailable")

    backup_dir = source_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = source_path.suffix or ".db"
    backup_path = backup_dir / (
        f"{source_path.stem}-required-documents-{timestamp}{suffix}"
    )

    try:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source,
            sqlite3.connect(backup_path) as destination,
        ):
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("SQLite backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path
