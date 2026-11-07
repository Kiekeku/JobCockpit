from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from jobcockpit.db.models import JobPosting, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.schemas.requests import JobIngestRequest
from jobcockpit.services import workflows
from jobcockpit.storage.artifacts import ArtifactStorageService


@pytest.mark.parametrize("mode, expected", [
    ("same_url", 1), ("same_snapshot", 1), ("different_sources", 1),
    ("canonical_identity", 1), ("distinct_urls", 2), ("unsafe_shared_url", 2),
    ("cross_source", 1),
])
def test_parallel_imports_recheck_url_after_preparation(
    jobcockpit_settings_factory, monkeypatch, allow_public_dns, mode, expected
):
    from types import SimpleNamespace

    allow_public_dns("example.wd1.myworkdayjobs.com")
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    barrier = Barrier(2, timeout=10)
    original_parse = workflows.parse_job_text_with_settings
    original_write = storage.write_bytes

    def parse(*args, **kwargs):
        parsed = original_parse(*args, **kwargs)
        if mode == "cross_source":
            parsed.location.city = "Toronto"
            parsed.location.country = "Canada"
            parsed.employment_timing.start_date = "2027-05-01"
            parsed.employment_timing.end_date = "2027-08-31"
            parsed.employment_timing.duration_months = 4
        barrier.wait()
        return parsed

    def write(**kwargs):
        time.sleep(0.1)
        return original_write(**kwargs)

    monkeypatch.setattr(workflows, "parse_job_text_with_settings", parse)
    monkeypatch.setattr(storage, "write_bytes", write)

    def ingest(index):
        url = "http://localhost:8001/ats/generic/example/jobs/123"
        if mode == "canonical_identity":
            locale = "en-US/" if index else ""
            url = f"https://example.wd1.myworkdayjobs.com/{locale}Careers/job/Toronto/Engineer_R123"
        if mode == "distinct_urls":
            url += str(index)
        if mode == "cross_source" and index:
            url = "https://example.wd1.myworkdayjobs.com/Careers/job/Toronto/Engineer_R123"
        request = SimpleNamespace(
            source_type="gmail_alert" if mode == "different_sources" and index else "url",
            source_url=url,
            source_url_dedup_safe=mode != "unsafe_shared_url", skip_language_filter=False, source_archive=None,
            raw_text=f"Backend Engineer\nCompany: Example\nBuild software. Revision {0 if mode == 'same_snapshot' else index}.",
        )
        with session_scope(settings) as db:
            return workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, range(2)))
    assert sum(result.created for result in results) == expected
    assert len({result.payload["id"] for result in results}) == expected
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == expected
        artifact_count = 2 if mode == "cross_source" else expected
        assert db.scalar(select(func.count()).select_from(StoredArtifact)) == artifact_count
    assert len([path for path in storage.root.rglob("*") if path.is_file()]) == artifact_count


def test_lock_retry_does_not_repeat_parsing(jobcockpit_settings_factory, monkeypatch):
    from threading import Event
    from jobcockpit.ingestion import concurrency

    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    lock_held, release_lock = Event(), Event()
    parse_calls = []
    original_parse = workflows.parse_job_text_with_settings

    def parse(*args, **kwargs):
        parse_calls.append(1)
        return original_parse(*args, **kwargs)

    def hold_writer():
        with session_scope(settings) as db:
            concurrency.begin_ingestion_write(db)
            lock_held.set()
            assert release_lock.wait(10)

    monkeypatch.setattr(workflows, "parse_job_text_with_settings", parse)
    sleeps = []
    original_sleep = time.sleep

    def backoff(seconds):
        sleeps.append(seconds)
        release_lock.set()
        original_sleep(seconds)

    monkeypatch.setattr(concurrency.time, "sleep", backoff)
    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_writer)
        assert lock_held.wait(10)
        try:
            with session_scope(settings) as db:
                db.connection().exec_driver_sql("PRAGMA busy_timeout=1")
                result = workflows.persist_job_ingest_result(
                    JobIngestRequest(source_type="paste", raw_text="Backend Engineer\nBuild software."),
                    db, storage, settings, run_fit_analysis=False,
                )
        finally:
            release_lock.set()
        holder.result()
    assert result.created
    assert parse_calls == [1]
    assert sleeps


def test_scope_lease_excludes_same_source_but_allows_other_sources(jobcockpit_settings_factory):
    from threading import Event
    from jobcockpit.ingestion.concurrency import coordinated_ingestion

    settings = jobcockpit_settings_factory()
    init_database(settings)
    entered, release = Event(), Event()

    @coordinated_ingestion(lambda arguments: arguments["source"], HTTPException)
    def ingest(*, source, db, settings):
        if source == "first":
            entered.set()
            assert release.wait(10)
        return source

    def call(source):
        with session_scope(settings) as db:
            return ingest(source=source, db=db, settings=settings)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(call, "first")
        assert entered.wait(10)
        try:
            with pytest.raises(HTTPException) as error:
                call("first")
            assert error.value.status_code == 409
            assert call("second") == "second"
        finally:
            release.set()
        assert pending.result() == "first"
    assert call("first") == "first"


def test_expired_scope_cannot_commit_progress(jobcockpit_settings_factory):
    from datetime import UTC, datetime, timedelta
    from jobcockpit.db.models import BackgroundWorkerLease, JobAlertSource
    from jobcockpit.ingestion.concurrency import coordinated_ingestion

    settings = jobcockpit_settings_factory()
    init_database(settings)

    @coordinated_ingestion(lambda arguments: "expiring", HTTPException)
    def ingest(*, db, settings):
        # Simulate expiry without changing the owner in an unrelated process.
        db.execute(select(BackgroundWorkerLease)).scalar_one().expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.flush()
        db.add(JobAlertSource(job_alert_source_id="stale", candidate_id="test",
                              display_name="Test", gmail_query="test"))

    with pytest.raises(HTTPException, match="ownership expired"):
        with session_scope(settings) as db:
            ingest(db=db, settings=settings)
    with session_scope(settings) as db:
        assert db.get(JobAlertSource, "stale") is None


def test_ingest_rollback_removes_artifacts_and_job(jobcockpit_settings_factory):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with pytest.raises(RuntimeError, match="follow-up failed"):
        with session_scope(settings) as db:
            workflows.persist_job_ingest_result(
                JobIngestRequest(source_type="paste", raw_text="Backend Engineer\nBuild software."),
                db, storage, settings, run_fit_analysis=False,
            )
            raise RuntimeError("follow-up failed")
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 0
        assert db.scalar(select(func.count()).select_from(StoredArtifact)) == 0
    assert not [path for path in storage.root.rglob("*") if path.is_file()]


def _child_ingest(settings_data, index, barrier, results):
    from jobcockpit.core.config import Settings

    settings = Settings(**settings_data)
    storage = ArtifactStorageService(settings.artifact_root)
    parse = workflows.parse_job_text_with_settings

    def prepare(*args, **kwargs):
        parsed = parse(*args, **kwargs)
        barrier.wait(timeout=15)
        return parsed

    workflows.parse_job_text_with_settings = prepare
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            JobIngestRequest(source_type="url",
                             source_url="http://localhost:8001/ats/generic/example/jobs/123",
                             raw_text=f"Backend Engineer\nCompany: Example\nRevision {index}."),
            db, storage, settings, run_fit_analysis=False,
        )
    results.put((result.created, result.payload["id"]))


def test_separate_processes_share_the_database_writer(jobcockpit_settings_factory):
    import multiprocessing

    settings = jobcockpit_settings_factory(ats_mock_port=8001, posting_parser_provider="heuristic")
    init_database(settings)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_child_ingest,
                                 args=(settings.model_dump(), index, barrier, results))
                 for index in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=25)
            assert process.exitcode == 0
        received = [results.get(timeout=5) for _ in processes]
        assert sorted(created for created, _ in received) == [False, True]
        assert len({job_id for _, job_id in received}) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


@pytest.mark.parametrize("provider", ["ats", "gmail"])
def test_run_entry_points_reject_overlap_before_fetch(
    jobcockpit_settings_factory, monkeypatch, provider
):
    from threading import Event
    from types import SimpleNamespace
    from jobcockpit.db.models import JobAlertSource
    from jobcockpit.integrations import gmail_processing
    from jobcockpit.schemas.requests import AtsIngestRequest

    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    with session_scope(settings) as db:
        db.add(JobAlertSource(job_alert_source_id="source", candidate_id="candidate_local",
                              display_name="Test", gmail_query="test"))
    entered, release = Event(), Event()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return []

    monkeypatch.setattr(gmail_processing, "_resolve_messages", fetch)
    monkeypatch.setattr(workflows, "get_ats_adapter", lambda provider: SimpleNamespace(
        provider="workday", validate_source_url=lambda url: None, fetch_jobs=fetch,
    ))

    def call():
        with session_scope(settings) as db:
            if provider == "gmail":
                return gmail_processing.process_gmail_source(
                    db=db, settings=settings, storage=storage, source=db.get(JobAlertSource, "source"),
                )
            return workflows.ingest_ats_request(
                AtsIngestRequest(provider="workday", source_url="http://localhost:8001/jobs"),
                db, storage, settings,
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(call)
        assert entered.wait(10)
        try:
            with pytest.raises((HTTPException, gmail_processing.GmailProcessingError)) as error:
                call()
            assert error.value.status_code == 409
            assert calls == [1]
        finally:
            release.set()
        pending.result()


@pytest.mark.parametrize("after_first_item", [False, True])
def test_ats_lease_loss_records_terminal_failure_without_touching_replacement(
    jobcockpit_settings_factory, monkeypatch, after_first_item
):
    from types import SimpleNamespace
    from jobcockpit.db.models import AuditLogEntry, BackgroundWorkerLease, JobIngestionItem, JobIngestionRun
    from jobcockpit.ingestion.ats_adapters.base import AtsJob
    from jobcockpit.ingestion.concurrency import _active_scope
    from jobcockpit.schemas.requests import AtsIngestRequest

    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    url = "http://localhost:8001/jobs"

    def replace_owner():
        guard, _ = _active_scope.get()
        # A separate process takes over the source. Temporarily clear this
        # thread's scope only to simulate that independent writer in the test.
        token = _active_scope.set(None)
        try:
            with session_scope(settings) as db:
                lease = db.get(BackgroundWorkerLease, guard.lease_name)
                lease.owner_id = "replacement"
                lease.lease_token = "replacement-token"
                db.add(JobIngestionRun(ingestion_run_id="replacement-run", provider="workday",
                                       source_url=url, search_text="", status="running", request_payload={}))
        finally:
            _active_scope.reset(token)

    def fetch(request):
        if not after_first_item:
            replace_owner()
            return []
        return [AtsJob(source_url=f"http://localhost:8001/ats/generic/example/jobs/{index}",
                       raw_text=f"Backend Engineer\nCompany: Example\nBuild software {index}.",
                       title="Backend Engineer") for index in range(2)]

    original_importer = workflows.persist_job_ingest_result
    calls = []

    def importer(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            replace_owner()
        return original_importer(*args, **kwargs)

    monkeypatch.setattr(workflows, "persist_job_ingest_result", importer)
    monkeypatch.setattr(workflows, "get_ats_adapter", lambda provider: SimpleNamespace(
        provider="workday", validate_source_url=lambda url: None, fetch_jobs=fetch,
    ))
    with pytest.raises(HTTPException) as error:
        with session_scope(settings) as db:
            workflows.ingest_ats_request(
                AtsIngestRequest(provider="workday", source_url=url, skip_interest_filter=True,
                                 skip_language_filter=True), db, storage, settings,
            )
    assert error.value.status_code == 409
    with session_scope(settings) as db:
        run = db.scalars(select(JobIngestionRun).where(JobIngestionRun.ingestion_run_id != "replacement-run")).one()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert "ownership expired" in run.error_message
        assert run.jobs_created_count == int(after_first_item)
        assert db.get(JobIngestionRun, "replacement-run").status == "running"
        assert db.scalars(select(BackgroundWorkerLease)).one().owner_id == "replacement"
        items = list(db.scalars(select(JobIngestionItem).where(JobIngestionItem.ingestion_run_id == run.ingestion_run_id)))
        assert sorted(item.status for item in items) == (["failed", "imported"] if after_first_item else [])
        assert all(item.retryable for item in items if item.status == "failed")
        assert db.scalar(select(func.count()).select_from(JobPosting)) == int(after_first_item)
        events = list(db.scalars(select(AuditLogEntry).where(AuditLogEntry.entity_id == run.ingestion_run_id)))
        assert [entry.event_type for entry in events].count("ingestion.run_failed") == 1



