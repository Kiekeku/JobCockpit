from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from jobcockpit.db.models import AuditLogEntry, JobAlertSource, JobPosting, JobReprocessRun, StoredArtifact
from jobcockpit.db.session import get_engine, init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import SourceArchive
from jobcockpit.ingestion.inputs import JobIngestPayload
from jobcockpit.ingestion.item_execution import execute_posting_items
from jobcockpit.ingestion.posting_links import PostingLinkClassification, PostingLinkProcessingResult
from jobcockpit.integrations.gmail_processing import process_gmail_source
from jobcockpit.services import workflows
from jobcockpit.storage.artifacts import ArtifactStorageService, ArtifactStorageWriteTracker


def test_reprocess_computation_does_not_write_database(jobcockpit_settings_factory):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            JobIngestPayload(source_type="paste", raw_text="Developer Intern\nCompany: Example"),
            db, storage, settings, run_fit_analysis=False,
        )
        job_id = result.payload["id"]
    with session_scope(settings) as db:
        original = db.get(JobPosting, job_id).to_api()
        artifacts_before = list(db.scalars(select(StoredArtifact.artifact_id)))
        audit_before = list(db.scalars(select(AuditLogEntry.audit_log_entry_id)))

    writes = []

    def capture_write(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)

    engine = get_engine(settings)
    event.listen(engine, "before_cursor_execute", capture_write)
    try:
        computation = workflows.compute_job_reprocess(
            job_id, workflows.JOB_REPROCESS_TYPE_FULL, settings, ArtifactStorageWriteTracker(storage),
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_write)
    assert writes == []
    assert computation.result["status"] == "snapshot_reparsed"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).to_api() == original
        assert list(db.scalars(select(StoredArtifact.artifact_id))) == artifacts_before
        assert list(db.scalars(select(AuditLogEntry.audit_log_entry_id))) == audit_before


@pytest.mark.parametrize("change_during_preparation", [False, True])
def test_reprocess_prepared_artifacts_commit_or_are_removed_after_job_changes(
    jobcockpit_settings_factory, monkeypatch, change_during_preparation,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            JobIngestPayload(source_type="paste", raw_text="Developer Intern\nCompany: Example"),
            db, storage, settings,
        )
        job_id = result.payload["id"]
        run, _ = workflows.enqueue_job_reprocess(db.get(JobPosting, job_id), db)
        run_id = run.reprocess_run_id
    original_files = set(storage.root.rglob("*.txt"))
    url = "http://localhost:8001/ats/generic/example/jobs/123"
    request = JobIngestPayload(
        source_type="url", source_url=url, raw_text="Developer Intern\nCompany: Example\nPython SQL",
        source_archive=SourceArchive(
            source_url=url, final_url=url, status_code=200, content_type="text/html", body=b"<p>posting evidence</p>",
        ),
    )
    monkeypatch.setattr(workflows, "prepare_reprocess_source", lambda *_: PostingLinkProcessingResult(
        classification=PostingLinkClassification("direct_posting", 1, "test"),
        ingestion_status="imported", original_url=url, resolved_url=url, canonical_url=url,
        ingest_request=request,
    ))
    original_prepare = workflows.prepare_existing_job_update
    prepared_uris = []

    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared_uris.extend(row.storage_uri for row in result.artifacts.rows)
        # A second connection can write while preparation is in progress.
        # No artifact row, identity, audit, or job update is persisted yet.
        with session_scope(settings) as db:
            assert db.get(JobPosting, job_id).source_url is None
            assert len(list(db.scalars(select(StoredArtifact)))) == 1
            if change_during_preparation:
                saved = db.get(JobPosting, job_id)
                saved.company_name = "User edited company"
                saved.updated_at = datetime.now(UTC) + timedelta(seconds=1)
        return result

    monkeypatch.setattr(workflows, "prepare_existing_job_update", prepare)
    processed = workflows.process_job_reprocess_run(run_id, settings, storage)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        if change_during_preparation:
            assert processed["error_code"] == "job_changed_during_reprocess"
            assert job.company_name == "User edited company"
            assert len(list(db.scalars(select(StoredArtifact)))) == 1
            for uri in prepared_uris:
                with pytest.raises(FileNotFoundError):
                    storage.read_bytes(uri)
            assert set(storage.root.rglob("*.txt")) == original_files
        else:
            assert processed["status"] == "succeeded"
            assert job.source_url == url
            assert len(list(db.scalars(select(StoredArtifact)))) == 3
            snapshot = db.get(StoredArtifact, job.raw_content_artifact_id)
            archive = db.get(StoredArtifact, job.source["source_archive_artifact_id"])
            assert snapshot.job_id == archive.job_id == job_id
            assert storage.read_bytes(snapshot.storage_uri).decode("utf-8") == request.raw_text
            assert storage.read_bytes(archive.storage_uri) == request.source_archive.body
            events = list(db.scalars(select(AuditLogEntry.event_type).where(AuditLogEntry.entity_id == job_id)))
            assert events.count("job.reprocessed") == 1


@pytest.mark.parametrize("source_type", ["paste", "file", "url", "gmail_alert", "ats_workday", "browser_capture"])
def test_ingestion_defers_provider_fit_and_reuses_its_queue_run(
    jobcockpit_settings_factory, monkeypatch, source_type,
):
    settings = jobcockpit_settings_factory(fit_analysis_provider="unit_test_queued", ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)

    def fail_inline(*args, **kwargs):
        raise AssertionError("provider-backed analysis belongs in a worker")

    monkeypatch.setattr(workflows, "enrich_parsed_job_fit_analysis", fail_inline)
    request = JobIngestPayload(
        source_type=source_type, raw_text="Developer Intern\nCompany: Example\nPython",
        source_url="http://localhost:8001/ats/generic/example/jobs/123",
    )
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(request, db, storage, settings)
        second = workflows.persist_job_ingest_result(request, db, storage, settings)
        runs = list(db.scalars(select(JobReprocessRun)))
        assert len(runs) == 1
        assert runs[0].status == "queued" and runs[0].run_type == "fit_analysis"
        assert first.payload["reprocess"]["reprocess_run_id"] == second.payload["reprocess"]["reprocess_run_id"]


@pytest.mark.parametrize("status", ["queued", "retry_scheduled", "running"])
def test_full_refresh_promotes_only_unclaimed_fit_work(jobcockpit_settings_factory, status):
    settings = jobcockpit_settings_factory(fit_analysis_provider="unit_test_queued")
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        imported = workflows.persist_job_ingest_result(
            JobIngestPayload(source_type="paste", raw_text="Developer Intern\nCompany: Example"),
            db, storage, settings,
        )
        job = db.get(JobPosting, imported.payload["id"])
        run = db.scalars(select(JobReprocessRun)).one()
        run.status = status
        if status == "running":
            run.started_at = datetime.now(UTC)
            run.worker_id = "active_worker"
            run.claim_token = "active_claim"
            run.supervisor_lease_token = "active_supervisor"
        elif status == "retry_scheduled":
            run.attempts = 1
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
        db.flush()
        original_run = run.to_api()
        original_claim = (run.worker_id, run.claim_token, run.supervisor_lease_token)
        for _ in range(2):
            reused, queued = workflows.enqueue_job_reprocess(job, db)
            assert queued is False
            assert reused.reprocess_run_id == run.reprocess_run_id
            assert reused.status == status
            assert reused.run_type == ("fit_analysis" if status == "running" else "full_reprocess")
        assert (run.worker_id, run.claim_token, run.supervisor_lease_token) == original_claim
        assert run.to_api()["attempts"] == original_run["attempts"]
        assert run.to_api()["next_attempt_at"] == original_run["next_attempt_at"]
        db.flush()
        upgrades = list(db.scalars(select(AuditLogEntry).where(
            AuditLogEntry.event_type == "job.reprocess_upgraded",
        )))
        assert len(upgrades) == (0 if status == "running" else 1)


@pytest.mark.parametrize("provider, expected_runs", [("disabled", 0), ("unit_test_queued", 1)])
def test_gmail_ingestion_uses_shared_fit_policy(jobcockpit_settings_factory, monkeypatch, provider, expected_runs):
    settings = jobcockpit_settings_factory(fit_analysis_provider=provider, ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)

    def fail_inline(*args, **kwargs):
        raise AssertionError("Gmail must honor disabled or deferred fitting")

    monkeypatch.setattr(workflows, "enrich_parsed_job_fit_analysis", fail_inline)
    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="policy", candidate_id="candidate_local", display_name="Test alerts",
            gmail_query="test", sender_allowlist=["alerts.invalid"],
            skip_interest_filter=True, skip_language_filter=True,
        )
        db.add(source)
        db.flush()
        result = process_gmail_source(db=db, storage=storage, settings=settings, source=source)
        assert result["jobs_created_count"] == 1
        assert result["mailbox_mutations"] == []
        assert len(list(db.scalars(select(JobReprocessRun)))) == expected_runs


def test_item_checkpoint_failure_preserves_committed_artifacts(jobcockpit_settings_factory):
    settings = jobcockpit_settings_factory(ingestion_link_concurrency=1)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    failures = []

    def persist(work, prepared, db, tracked_storage):
        record = tracked_storage.write_bytes(
            artifact_type="job_snapshot", content=b"committed evidence",
            original_file_name="job-posting.txt", content_type="text/plain", privacy_class="private",
        )
        db.add(StoredArtifact.from_record(record))
        return record.artifact_id

    def failed_checkpoint(work, result):
        raise TimeoutError("checkpoint failed after item commit")

    with pytest.raises(TimeoutError, match="checkpoint"):
        execute_posting_items(
            ["item"], settings=settings, storage=storage, prepare=lambda _: None, persist=persist,
            record_failure=lambda work, error, db: failures.append(work), completed=failed_checkpoint,
        )
    assert failures == []
    with session_scope(settings) as db:
        artifact = db.scalars(select(StoredArtifact)).one()
        assert storage.read_bytes(artifact.storage_uri) == b"committed evidence"
