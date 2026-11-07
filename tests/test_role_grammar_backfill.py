from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from jobcockpit.api.main import create_app
from jobcockpit.cli import main as cli_main
from jobcockpit.core.config import Settings
from jobcockpit.db.models import Application, AuditLogEntry, JobPosting, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.role_grammar_backfill import (
    _semantic_delta,
    backfill_role_grammar,
)
from jobcockpit.storage.artifacts import ArtifactStorageService


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        posting_parser_provider="codex_cli",
        fit_analysis_provider="codex_cli",
    )


def _ingest(client: TestClient, text: str) -> dict:
    response = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": text, "source_url": None},
    )
    assert response.status_code == 201
    return response.json()


def test_semantic_delta_reports_only_true_requirement_moves() -> None:
    unchanged_overlap = {
        "must_have_requirements": ["Python"],
        "nice_to_have_requirements": ["Python"],
    }

    unchanged_delta = _semantic_delta(unchanged_overlap, unchanged_overlap)
    assert unchanged_delta["requirements_moved_to_required"] == []
    assert unchanged_delta["requirements_moved_to_preferred"] == []

    moved_delta = _semantic_delta(
        {"must_have_requirements": [], "nice_to_have_requirements": ["Python"]},
        {"must_have_requirements": ["Python"], "nice_to_have_requirements": []},
    )
    assert moved_delta["requirements_moved_to_required"] == ["Python"]
    assert moved_delta["requirements_moved_to_preferred"] == []


def test_semantic_delta_preserves_coextensive_claim_occurrences() -> None:
    def role(claims: list[tuple[str, str]]) -> dict:
        return {
            "must_have_requirements": ["Python experience."],
            "nice_to_have_requirements": [],
            "capabilities": ["Python"],
            "evidence": [
                {
                    "claims": [
                        {
                            "text": "Python experience.",
                            "source_start": 10,
                            "source_end": 28,
                            "kind": kind,
                            "modality": modality,
                        }
                        for kind, modality in claims
                    ]
                }
            ],
        }

    reordered = _semantic_delta(
        role(
            [
                ("qualification", "mandatory"),
                ("qualification", "preferred"),
            ]
        ),
        role(
            [
                ("qualification", "preferred"),
                ("qualification", "mandatory"),
            ]
        ),
    )
    assert reordered["claims_with_changed_modality"] == []

    changed = _semantic_delta(
        role(
            [
                ("qualification", "mandatory"),
                ("qualification", "preferred"),
            ]
        ),
        role(
            [
                ("qualification", "expected"),
                ("qualification", "preferred"),
            ]
        ),
    )
    assert [
        (item["previous"], item["current"])
        for item in changed["claims_with_changed_modality"]
    ] == [("mandatory", "expected")]

    changed_kind = _semantic_delta(
        role(
            [
                ("qualification", "mandatory"),
                ("responsibility", "preferred"),
            ]
        ),
        role(
            [
                ("eligibility", "mandatory"),
                ("responsibility", "preferred"),
            ]
        ),
    )
    assert [
        (item["previous"], item["current"])
        for item in changed_kind["claims_with_changed_kind"]
    ] == [("qualification", "eligibility")]


def test_semantic_delta_uses_a_global_minimum_cost_claim_alignment() -> None:
    def role(claims: list[tuple[str, str]]) -> dict:
        return {
            "evidence": [
                {
                    "claims": [
                        {
                            "text": "Same grounded clause.",
                            "source_start": 10,
                            "source_end": 31,
                            "kind": kind,
                            "modality": modality,
                        }
                        for kind, modality in claims
                    ]
                }
            ]
        }

    delta = _semantic_delta(
        role(
            [
                ("eligibility", "mandatory"),
                ("responsibility", "preferred"),
            ]
        ),
        role(
            [
                ("eligibility", "preferred"),
                ("qualification", "mandatory"),
            ]
        ),
    )

    assert len(delta["claims_with_changed_kind"]) == 2
    assert delta["claims_with_changed_modality"] == []


def test_semantic_delta_scales_to_large_coextensive_claim_groups() -> None:
    def role(kind: str) -> dict:
        return {
            "evidence": [
                {
                    "claims": [
                        {
                            "text": "Same coordinated source clause.",
                            "source_start": 10,
                            "source_end": 42,
                            "kind": kind,
                            "modality": "mandatory",
                        }
                        for _ in range(64)
                    ]
                }
            ]
        }

    delta = _semantic_delta(role("qualification"), role("responsibility"))

    assert len(delta["claims_with_changed_kind"]) == 64
    assert delta["claims_with_changed_modality"] == []


def _make_role_legacy(settings: Settings, job_id: str) -> None:
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        role = dict(job.role)
        role.update(
            {
                "responsibilities": [],
                "must_have_requirements": [],
                "nice_to_have_requirements": [],
                "evidence": [],
                "required_capabilities": [],
                "preferred_capabilities": [],
                "responsibility_capabilities": [],
                "environment_capabilities": [],
                "capabilities": [],
                "capability_domains": [],
                "tech_stack": [],
            }
        )
        job.role = role
        job.data_quality = {
            **job.data_quality,
            "missing_fields": ["role.tech_stack"],
            "ambiguous_fields": [],
            "inferred_fields": [],
            "needs_manual_review": True,
            "custom_quality_marker": "preserve-me",
        }


def test_role_grammar_backfill_is_dry_run_first_idempotent_and_preserving(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    heuristic_settings = settings.model_copy(
        update={"posting_parser_provider": "heuristic", "fit_analysis_provider": "disabled"}
    )
    init_database(settings)
    client = TestClient(create_app(heuristic_settings))
    created = _ingest(
        client,
        "Title: Platform Engineer\n"
        "Responsibilities: Build Python services.\n"
        "Qualifications: Kubernetes experience is required.\n"
        "Tech Stack: Python, Kubernetes, and AWS.",
    )
    _make_role_legacy(settings, created["id"])
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.fit_analysis = {"fit_score": 91, "summary": "preserve fit"}
        db.add(
            Application(
                application_id="app_role_grammar_backfill",
                candidate_id="candidate_local",
                job_id=job.id,
                status="interested",
            )
        )
        preserved = {
            "status": job.status,
            "status_last_checked_at": job.status_last_checked_at,
            "source": job.source,
            "employer": job.employer,
            "application": job.application,
            "fit_analysis": job.fit_analysis,
            "normalized_title": job.normalized_title,
            "role_identity": {
                field: job.role[field]
                for field in ("job_title", "normalized_title", "role_type", "description_clean")
            },
        }

    with session_scope(settings) as db:
        dry_run = backfill_role_grammar(db, storage)
    assert dry_run["mode"] == "dry_run"
    assert dry_run["scanned_count"] == 1
    assert dry_run["changed_count"] == 1
    assert dry_run["error_count"] == 0
    assert dry_run["gained_claims_count"] == 1
    assert "role.evidence" in dry_run["changed_fields"]
    semantic_delta = dry_run["samples"][0]["semantic_delta"]
    assert semantic_delta["requirements_added"] == [
        "Kubernetes experience is required."
    ]
    assert semantic_delta["capabilities_added"] == ["Kubernetes", "Python", "AWS"]
    assert semantic_delta["requirements_moved_to_required"] == []
    assert semantic_delta["requirements_moved_to_preferred"] == []
    with session_scope(settings) as db:
        assert db.get(JobPosting, created["id"]).role["evidence"] == []

    with session_scope(settings) as db:
        applied = backfill_role_grammar(db, storage, apply=True)
    assert applied["applied"] is True
    assert applied["changed_count"] == 1

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.role["responsibilities"] == ["Build Python services."]
        assert job.role["must_have_requirements"] == [
            "Kubernetes experience is required."
        ]
        assert job.role["environment_capabilities"] == ["Python", "Kubernetes", "AWS"]
        assert job.role["evidence"]
        assert all(item["claims"] for item in job.role["evidence"])
        assert "role.tech_stack" not in job.data_quality["missing_fields"]
        assert job.data_quality["needs_manual_review"] is False
        assert job.data_quality["custom_quality_marker"] == "preserve-me"
        assert job.status == preserved["status"]
        assert job.status_last_checked_at == preserved["status_last_checked_at"]
        assert job.source == preserved["source"]
        assert job.employer == preserved["employer"]
        assert job.application == preserved["application"]
        assert job.fit_analysis == preserved["fit_analysis"]
        assert job.normalized_title == preserved["normalized_title"]
        assert {
            field: job.role[field]
            for field in ("job_title", "normalized_title", "role_type", "description_clean")
        } == preserved["role_identity"]
        assert db.execute(select(Application).where(Application.job_id == job.id)).scalar_one()
        assert db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "job.role_grammar_backfilled"
            )
        ).scalar_one()

    with session_scope(settings) as db:
        second = backfill_role_grammar(db, storage, apply=True)
    assert second["changed_count"] == 0
    assert second["applied"] is True


def test_role_grammar_backfill_scopes_to_active_jobs_and_preserves_review_completion(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    heuristic_settings = settings.model_copy(
        update={"posting_parser_provider": "heuristic", "fit_analysis_provider": "disabled"}
    )
    init_database(settings)
    client = TestClient(create_app(heuristic_settings))
    active = _ingest(client, "Title: Data Engineer\nQualifications: Python is required.")
    closed = _ingest(client, "Title: Data Engineer\nQualifications: SQL is required.")
    _make_role_legacy(settings, active["id"])
    _make_role_legacy(settings, closed["id"])

    with session_scope(settings) as db:
        active_job = db.get(JobPosting, active["id"])
        active_job.data_quality = {
            **active_job.data_quality,
            "needs_manual_review": False,
            "manual_review_completed_at": "2026-08-20T12:00:00Z",
            "manual_review_completed_snapshot_hash": active_job.posting_snapshot_hash,
        }
        db.get(JobPosting, closed["id"]).status = "closed"

    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        result = backfill_role_grammar(db, storage, apply=True)

    assert result["scanned_count"] == 1
    with session_scope(settings) as db:
        active_job = db.get(JobPosting, active["id"])
        closed_job = db.get(JobPosting, closed["id"])
        assert active_job.role["required_capabilities"] == ["Python"]
        assert active_job.data_quality["needs_manual_review"] is False
        assert active_job.data_quality["manual_review_completed_at"] == (
            "2026-08-20T12:00:00Z"
        )
        assert closed_job.role["evidence"] == []


def test_role_grammar_backfill_does_not_partially_apply_with_missing_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    heuristic_settings = settings.model_copy(
        update={"posting_parser_provider": "heuristic", "fit_analysis_provider": "disabled"}
    )
    init_database(settings)
    client = TestClient(create_app(heuristic_settings))
    good = _ingest(client, "Title: Data Engineer\nQualifications: Python is required.")
    missing = _ingest(client, "Title: Data Engineer\nQualifications: SQL is required.")
    _make_role_legacy(settings, good["id"])
    _make_role_legacy(settings, missing["id"])
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        missing_job = db.get(JobPosting, missing["id"])
        artifact = db.get(StoredArtifact, missing_job.raw_content_artifact_id)
        storage.delete(artifact.storage_uri)

    with session_scope(settings) as db:
        result = backfill_role_grammar(db, storage, apply=True)

    assert result["applied"] is False
    assert result["error_count"] == 1
    with session_scope(settings) as db:
        assert db.get(JobPosting, good["id"]).role["evidence"] == []


def test_role_grammar_backfill_skips_oversized_snapshot_without_blocking_apply(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    heuristic_settings = settings.model_copy(
        update={"posting_parser_provider": "heuristic", "fit_analysis_provider": "disabled"}
    )
    init_database(settings)
    client = TestClient(create_app(heuristic_settings))
    ordinary = _ingest(client, "Title: Data Engineer\nQualifications: Python is required.")
    oversized = _ingest(
        client,
        "Title: Data Engineer\nQualifications: SQL is required.\n"
        + ("Company context sentence. " * 20),
    )
    _make_role_legacy(settings, ordinary["id"])
    _make_role_legacy(settings, oversized["id"])
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        result = backfill_role_grammar(
            db,
            storage,
            apply=True,
            max_snapshot_bytes=100,
        )

    assert result["applied"] is True
    assert result["skipped_count"] == 1
    assert result["changed_count"] == 1
    assert {item["reason"] for item in result["skips"]} == {"snapshot_too_large"}
    with session_scope(settings) as db:
        assert db.get(JobPosting, ordinary["id"]).role["required_capabilities"] == ["Python"]
        assert db.get(JobPosting, oversized["id"]).role["evidence"] == []


def test_role_grammar_backfill_cli_defaults_to_dry_run_and_apply_creates_backup(
    tmp_path: Path,
    capsys,
) -> None:
    settings = _settings(tmp_path)
    heuristic_settings = settings.model_copy(
        update={"posting_parser_provider": "heuristic", "fit_analysis_provider": "disabled"}
    )
    init_database(settings)
    client = TestClient(create_app(heuristic_settings))
    first = _ingest(client, "Title: Data Engineer\nQualifications: Python is required.")
    second = _ingest(client, "Title: Backend Engineer\nQualifications: SQL is required.")
    _make_role_legacy(settings, first["id"])
    _make_role_legacy(settings, second["id"])

    assert cli_main(["jobs", "backfill-role-grammar", "--json"], settings=settings) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry_run"
    assert dry_run["changed_count"] == 2
    assert dry_run["applied"] is False

    assert (
        cli_main(
            [
                "jobs",
                "backfill-role-grammar",
                "--apply",
                "--batch-size",
                "1",
                "--json",
            ],
            settings=settings,
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["applied"] is True
    assert applied["changed_count"] == 2
    assert applied["batch_count"] == 2
    backup_path = Path(applied["backup_path"])
    assert backup_path.is_file()

    with sqlite3.connect(backup_path) as backup:
        saved_role = json.loads(
            backup.execute(
                "SELECT role FROM job_postings WHERE id = ?",
                (first["id"],),
            ).fetchone()[0]
        )
    assert saved_role["evidence"] == []
