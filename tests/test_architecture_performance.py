"""Read-path and scheduling contracts; these fixtures do not exercise parsing."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.orm import Session

import pytest

from jobcockpit.db.models import JobPosting, JobReprocessRun, StoredArtifact
from jobcockpit.db.session import get_engine, session_scope


def seed_jobs(settings, count=3):
    with session_scope(settings) as db:
        for i in range(count):
            db.add(
                StoredArtifact(
                    artifact_id=f"artifact-{i}",
                    artifact_type="job_snapshot",
                    storage_backend="local",
                    storage_uri=f"local://job-snapshots/{i}.txt",
                    original_file_name="posting.txt",
                    content_type="text/plain",
                    size_bytes=0,
                    sha256=str(i),
                    privacy_class="private",
                )
            )
        db.flush()
        for i in range(count):
            db.add(
                JobPosting(
                    id=f"job-{i}",
                    raw_content_artifact_id=f"artifact-{i}",
                    posting_snapshot_hash=f"snapshot-{i}",
                    source_type="paste",
                    source_url=f"https://example.test/jobs/position-{i}",
                    status="active",
                    company_name="Example",
                    normalized_title=f"Engineer {i}",
                    role={"job_title": f"Engineer {i}", "evidence": ["large evidence" * 1000]},
                    employer={"company_name": "Example"},
                    fit_analysis={"fit_score": i},
                    employment_timing={"timing_type": "fixed_term", "start_date": "2027-05-01"},
                    created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
                )
            )


def test_dashboard_summary_is_small_paginated_and_filtered(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    seed_jobs(settings)
    response = client.get("/api/job-summaries", params={"page_size": 2, "sort": "fit"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [job["id"] for job in body["items"]] == ["job-2", "job-1"]
    assert "evidence" not in body["items"][0]["role"]
    assert len(response.content) < 12000
    assert client.get("/api/job-summaries", params={"q": "Engineer 0"}).json()["total"] == 1
    assert client.get("/api/job-summaries", params={"term": "summer 2027"}).json()["total"] == 3
    assert (
        client.get("/api/job-summaries", params={"page": 2, "page_size": 2}).json()["items"][0][
            "id"
        ]
        == "job-0"
    )
    assert client.get("/api/job-summaries", params={"page_size": 501}).status_code == 422
    assert "evidence" in client.get("/api/jobs/job-0").json()["role"]


def test_job_title_entities_display_decoded_without_changing_stored_evidence(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    seed_jobs(settings, count=1)
    # Observed employer JSON-LD retained numeric entities in the job title.
    encoded = "Data Science &#038; Data Engineering Intern &#8211; Quote Intelligence"
    displayed = "Data Science & Data Engineering Intern – Quote Intelligence"
    with session_scope(settings) as db:
        job = db.get(JobPosting, "job-0")
        job.role = {**job.role, "job_title": encoded, "description_raw": encoded}

    detail = client.get("/api/jobs/job-0").json()
    assert detail["role"]["job_title"] == displayed
    assert detail["role"]["description_raw"] == encoded
    summary = client.get("/api/job-summaries", params={"q": "Data Science &"}).json()
    assert summary["total"] == 1
    assert summary["items"][0]["role"]["job_title"] == displayed
    with session_scope(settings) as db:
        job = db.get(JobPosting, "job-0")
        assert job.role["job_title"] == encoded
        assert job.role["description_raw"] == encoded


def test_ats_identity_index_does_not_materialize_postings(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from jobcockpit.services.workflows import previously_imported_jobs_by_source_url_identity

    settings = jobcockpit_settings_factory()
    jobcockpit_client_factory(settings)
    seed_jobs(settings)
    with Session(get_engine(settings)) as db:
        result = previously_imported_jobs_by_source_url_identity(db)
        assert set(result.values()) == {"job-0", "job-1", "job-2"}
        assert not db.identity_map


def test_latest_reprocess_materializes_one_run_per_job(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from jobcockpit.api.job_reprocess_runs import latest_job_reprocess_runs_by_job

    settings = jobcockpit_settings_factory()
    jobcockpit_client_factory(settings)
    seed_jobs(settings)
    now = datetime.now(UTC)
    with session_scope(settings) as db:
        for i in range(12):
            db.add(
                JobReprocessRun(
                    reprocess_run_id=f"run-{i:02}",
                    job_id="job-0",
                    status="succeeded",
                    created_at=now + timedelta(seconds=i),
                )
            )
    loaded = []
    with Session(get_engine(settings)) as db:
        event.listen(db, "loaded_as_persistent", lambda session, row: loaded.append(row))
        runs = latest_job_reprocess_runs_by_job(db, ["job-0", "job-1"])
        assert runs["job-0"].reprocess_run_id == "run-11"
        assert len(loaded) == 1


def test_source_sweep_bounds_examined_rows_and_advances_cursor(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from jobcockpit.ingestion.source_reconciliation import sweep_source_reconciliations

    settings = jobcockpit_settings_factory()
    jobcockpit_client_factory(settings)
    seed_jobs(settings)
    with session_scope(settings) as db:
        db.get(JobPosting, "job-2").status_source_repair_required = True
    first = sweep_source_reconciliations(settings, batch_size=2)
    assert first.examined == 2
    assert first.enqueued == 0
    second = sweep_source_reconciliations(settings, batch_size=2, after=first.cursor)
    assert second.examined == 1
    assert second.enqueued == 1


def test_workflow_import_does_not_construct_http_application():
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; from jobcockpit.services import workflows; "
            "assert 'jobcockpit.api.main' not in sys.modules; "
            "assert not hasattr(workflows, 'app')",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_summary_statistics_cover_all_pages_and_exclude_merged_jobs(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from jobcockpit.db.models import Application

    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    seed_jobs(settings)
    with session_scope(settings) as db:
        db.get(JobPosting, "job-2").source = {"merged_into_job_id": "job-0"}
        db.get(JobPosting, "job-1").data_quality = {"needs_manual_review": True}
        db.add(
            Application(
                application_id="applied",
                candidate_id="candidate_local",
                job_id="job-0",
                status="applied",
                applied_at=datetime.now(UTC),
            )
        )
    response = client.get("/api/job-summaries", params={"page_size": 1, "hide_applied": True})
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == "job-1"
    assert data["stats"]["total"] == 2
    assert data["stats"]["tracked"] == 1
    assert data["stats"]["needs_review"] == 1
    assert client.get("/api/job-summaries", params={"needs_review": True}).json()["total"] == 1


def test_idle_source_claim_does_not_scan_postings(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from jobcockpit.ingestion.source_reconciliation import process_next_source_reconciliation

    settings = jobcockpit_settings_factory()
    jobcockpit_client_factory(settings)
    seed_jobs(settings)
    statements = []
    engine = get_engine(settings)

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert process_next_source_reconciliation(settings, sweep=False) is None
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("FROM job_postings" in statement for statement in statements)


@pytest.mark.parametrize("indexed", [False, True])
def test_source_collision_check_uses_only_url_projections(
    jobcockpit_settings_factory, jobcockpit_client_factory, indexed,
):
    from jobcockpit.ingestion.source_reconciliation import _candidate_already_saved
    from jobcockpit.api.page_capture_runs import sync_job_source_identities

    settings = jobcockpit_settings_factory()
    jobcockpit_client_factory(settings)
    seed_jobs(settings)
    candidate = "https://boards.greenhouse.io/example/jobs/123456"
    with session_scope(settings) as db:
        other = db.get(JobPosting, "job-1")
        other.application = {"external_apply_url": candidate, "evidence": ["large" * 10000]}
        if indexed:
            sync_job_source_identities(db, other)
    with Session(get_engine(settings)) as db:
        job = db.get(JobPosting, "job-0")
        loaded = []
        event.listen(db, "loaded_as_persistent", lambda session, obj: loaded.append(obj))
        assert _candidate_already_saved(db, job, candidate)
        assert not _candidate_already_saved(db, job, "https://example.test/jobs/missing")
        assert loaded == []
    with session_scope(settings) as db:
        # Indexed historical identities must not revive a URL removed from the posting.
        db.get(JobPosting, "job-1").application = {}
    with Session(get_engine(settings)) as db:
        assert not _candidate_already_saved(db, db.get(JobPosting, "job-0"), candidate)
