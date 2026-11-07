from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobcockpit.api.main import create_app
from jobcockpit.cli import main as cli_main
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, JobPosting, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.application_document_backfill import backfill_required_documents
from jobcockpit.storage.artifacts import ArtifactStorageService


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        posting_parser_provider="heuristic",
        fit_analysis_provider="disabled",
    )


def _ingest(client: TestClient, text: str) -> dict:
    response = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": text, "source_url": None},
    )
    assert response.status_code == 201
    return response.json()


def _restore_legacy_defaults(settings: Settings, job_ids: list[str]) -> None:
    with session_scope(settings) as db:
        jobs = list(db.execute(select(JobPosting).where(JobPosting.id.in_(job_ids))).scalars())
        for job in jobs:
            job.application = {
                **job.application,
                "documents_required": ["resume"],
                "resume_required": True,
                "cover_letter_required": False,
            }


def test_required_document_backfill_is_dry_run_first_and_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    combined = _ingest(
        client,
        "Data Intern\nSubmit your resume, cover letter, and unofficial transcript.",
    )
    unknown = _ingest(client, "Backend Intern\nBuild APIs and test services.")
    _restore_legacy_defaults(settings, [combined["id"], unknown["id"]])
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        original = db.get(JobPosting, combined["id"])
        preserved_fields = {
            "company_name": original.company_name,
            "normalized_title": original.normalized_title,
            "status": original.status,
            "source": original.source,
            "fit_analysis": original.fit_analysis,
            "application_method": original.application["application_method"],
            "extraction_confidence": original.data_quality["extraction_confidence"],
        }

    with session_scope(settings) as db:
        dry_run = backfill_required_documents(db, storage)
    assert dry_run["applied"] is False
    assert dry_run["changed_count"] == 2
    assert dry_run["distribution"] == {
        "not_stated": 1,
        "resume+cover_letter+transcript": 1,
    }
    with session_scope(settings) as db:
        assert db.get(JobPosting, unknown["id"]).application["documents_required"] == ["resume"]

    with session_scope(settings) as db:
        applied = backfill_required_documents(db, storage, apply=True)
    assert applied["applied"] is True
    assert applied["changed_count"] == 2

    with session_scope(settings) as db:
        combined_job = db.get(JobPosting, combined["id"])
        assert combined_job.application["documents_required"] == [
            "resume",
            "cover_letter",
            "transcript",
        ]
        assert combined_job.company_name == preserved_fields["company_name"]
        assert combined_job.normalized_title == preserved_fields["normalized_title"]
        assert combined_job.status == preserved_fields["status"]
        assert combined_job.source == preserved_fields["source"]
        assert combined_job.fit_analysis == preserved_fields["fit_analysis"]
        assert combined_job.application["application_method"] == preserved_fields["application_method"]
        assert (
            combined_job.data_quality["extraction_confidence"]
            == preserved_fields["extraction_confidence"]
        )
        assert db.get(JobPosting, unknown["id"]).application["documents_required"] is None
        assert (
            db.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.event_type == "job.required_documents_backfilled"
                )
            ).scalars().all()
        )

    with session_scope(settings) as db:
        second_run = backfill_required_documents(db, storage, apply=True)
    assert second_run["changed_count"] == 0


def test_required_document_backfill_does_not_partially_apply_with_missing_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    good = _ingest(client, "Data Intern\nSubmit a resume and cover letter.")
    missing = _ingest(client, "Design Intern\nSubmit a resume and portfolio.")
    _restore_legacy_defaults(settings, [good["id"], missing["id"]])
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        missing_job = db.get(JobPosting, missing["id"])
        artifact = db.get(StoredArtifact, missing_job.raw_content_artifact_id)
        storage.delete(artifact.storage_uri)

    with session_scope(settings) as db:
        result = backfill_required_documents(db, storage, apply=True)

    assert result["applied"] is False
    assert result["error_count"] == 1
    with session_scope(settings) as db:
        assert db.get(JobPosting, good["id"]).application["documents_required"] == ["resume"]


def test_required_document_backfill_cli_defaults_to_dry_run(
    tmp_path: Path,
    capsys,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)

    exit_code = cli_main(
        ["jobs", "backfill-required-documents", "--json"],
        settings=settings,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["applied"] is False


@pytest.mark.parametrize(
    ("other_ambiguities", "inferred_fields", "expected_review"),
    [
        ([], [], False),
        (["employment_timing.start_date"], [], True),
        ([], ["location.work_mode"], True),
    ],
)
def test_backfill_clears_only_document_specific_review_reason(
    tmp_path: Path,
    other_ambiguities: list[str],
    inferred_fields: list[str],
    expected_review: bool,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = _ingest(client, "Backend Intern\nBuild APIs and test services.")
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        persisted = db.get(JobPosting, job["id"])
        persisted.data_quality = {
            **persisted.data_quality,
            "missing_fields": [],
            "ambiguous_fields": [
                "application.documents_required",
                *other_ambiguities,
            ],
            "inferred_fields": inferred_fields,
            "needs_manual_review": True,
        }

    with session_scope(settings) as db:
        result = backfill_required_documents(db, storage, apply=True)

    assert result["applied"] is True
    with session_scope(settings) as db:
        updated = db.get(JobPosting, job["id"])
        assert updated.data_quality["ambiguous_fields"] == other_ambiguities
        assert updated.data_quality["needs_manual_review"] is expected_review


def test_required_document_backfill_apply_creates_recoverable_backup(
    tmp_path: Path,
    capsys,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = _ingest(client, "Data Intern\nSubmit a resume and cover letter.")
    _restore_legacy_defaults(settings, [job["id"]])

    exit_code = cli_main(
        ["jobs", "backfill-required-documents", "--apply", "--json"],
        settings=settings,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    backup_path = Path(payload["backup_path"])
    assert backup_path.parent == tmp_path / "backups"
    assert backup_path.is_file()

    with sqlite3.connect(backup_path) as backup:
        saved_application = json.loads(
            backup.execute(
                "SELECT application FROM job_postings WHERE id = ?",
                (job["id"],),
            ).fetchone()[0]
        )
    assert saved_application["documents_required"] == ["resume"]

    with session_scope(settings) as db:
        updated = db.get(JobPosting, job["id"])
        assert updated.application["documents_required"] == ["resume", "cover_letter"]


    second_exit_code = cli_main(
        ["jobs", "backfill-required-documents", "--apply", "--json"],
        settings=settings,
    )

    assert second_exit_code == 0
    second_payload = json.loads(capsys.readouterr().out)
    assert second_payload["mode"] == "apply"
    assert second_payload["applied"] is True
    assert second_payload["changed_count"] == 0
    assert "backup_path" not in second_payload
    assert list((tmp_path / "backups").glob("*.db")) == [backup_path]
