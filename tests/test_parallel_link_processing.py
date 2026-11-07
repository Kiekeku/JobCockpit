from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
from threading import Barrier, BoundedSemaphore, Event, Lock, get_ident
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from jobcockpit.db.models import (
    JobAlertEmail, JobAlertEmailProcessingState, JobAlertLink, JobAlertSource,
    JobIngestionItem, JobPosting,
)
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob
from jobcockpit.ingestion.ats_adapters.csjobs import CsJobsAdapter
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.concurrency import link_request_slot, prepare_links
from jobcockpit.ingestion import item_execution, concurrency, posting_links
from jobcockpit.integrations import gmail_processing
from jobcockpit.schemas.requests import AtsIngestRequest
from jobcockpit.services import workflows
from jobcockpit.storage.artifacts import ArtifactStorageService


def test_completed_link_is_delivered_while_another_is_waiting():
    slow_started, release_slow = Event(), Event()

    def prepare(item):
        if item == 0:
            slow_started.set()
            assert release_slow.wait(5)
        else:
            assert slow_started.wait(5)
        return item

    with closing(prepare_links(range(3), prepare, concurrency=2)) as results:
        try:
            assert next(results) == (1, 1)
        finally:
            release_slow.set()
        assert dict(results) == {0: 0, 2: 2}


@pytest.mark.parametrize("concurrency", [1, 4])
def test_adapter_link_budget_and_failures_are_accounted_independently(concurrency):
    request = AtsFetchRequest(source_url="https://board.invalid", limit=3,
                              link_concurrency=concurrency)
    called = []

    def fetch(index):
        called.append(index)
        if index == 1:
            raise RuntimeError("one bad link")
        return AtsJob(source_url=f"https://jobs.invalid/{index}", raw_text="Developer")

    jobs = fetch_details(request, (
        (f"https://jobs.invalid/{index}", lambda index=index: fetch(index)) for index in range(5)
    ))
    assert sorted(called) == [0, 1, 2]
    assert request.stats.links_seen_count == 3
    assert [job.source_url for job in jobs] == ["https://jobs.invalid/0", "https://jobs.invalid/2"]
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].reason == "one bad link"


def test_host_limits_are_shared_between_concurrent_runs():
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    active = peak = 0

    def prepare(_):
        nonlocal active, peak
        with link_request_slot("https://same.invalid/jobs"):
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
            finally:
                with lock:
                    active -= 1

    def run():
        results = list(prepare_links(range(2), prepare, concurrency=2))
        assert all(result is None for _, result in results)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert peak == 2


def test_csjobs_adapter_actually_overlaps_apply_links():
    barrier = Barrier(2, timeout=5)
    def resolve(url, context):
        barrier.wait()
        return AtsJob(source_url=url, raw_text=context.role)

    payload = {"jobs": [
        {"id": index + 1, "job_type": "internship", "title": "Developer Intern",
         "company": "Example", "location": "Canada", "link": f"https://jobs.invalid/{index}"}
        for index in range(2)
    ], "meta": {"total": 2}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        adapter = CsJobsAdapter(http_client=client, apply_resolver=SimpleNamespace(resolve=resolve))
        jobs = adapter.fetch_jobs(AtsFetchRequest(source_url="https://csjobs.ca/jobs", limit=2))
    assert [job.source_url for job in jobs] == ["https://jobs.invalid/0", "https://jobs.invalid/1"]


def _message(index, url):
    digest = hashlib.sha256(str(index).encode()).hexdigest()
    return {"id": str(index), "id_hash": digest, "thread_hash": digest,
            "subject": "Developer jobs", "sender_hash": digest, "sender_domain": "alerts.invalid",
            "received_at": "2026-06-02T12:00:00Z", "body_hash": digest,
            "plain_text": "Software Developer Intern\nCompany: Example",
            "html": f'<a href="{url}">Software Developer Job</a>'}


@pytest.mark.parametrize("source_kind", ["board", "gmail"])
@pytest.mark.parametrize("failure_phase", ["prepare", "write", "duplicate"])
def test_ingestion_overlaps_preparation_and_isolates_each_failure(
    jobcockpit_settings_factory, monkeypatch, source_kind, failure_phase,
):
    settings = jobcockpit_settings_factory(
        ats_mock_port=8001, ingestion_link_concurrency=2,
        posting_rendered_fallback_provider="disabled", posting_parser_provider="heuristic",
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    base = "http://localhost:8001/ats/generic/example/jobs"
    jobs = [AtsJob(source_url=f"{base}/{index}", raw_text=f"Developer {index}\nCompany: Example")
            for index in range(3)]
    module = workflows if source_kind == "board" else gmail_processing
    original_prepare = module.prepare_posting_batch_item
    original_import = workflows.persist_job_ingest_result
    barrier = Barrier(2, timeout=5)
    caller = get_ident()
    prepared_threads, writer_threads = [], []

    def prepare(item, **kwargs):
        prepared_threads.append(get_ident())
        assert "db" not in kwargs and "storage" not in kwargs
        if item.source_url.endswith(("/0", "/1")):
            barrier.wait()
        if failure_phase == "prepare" and item.source_url.endswith("/1"):
            raise RuntimeError("isolated preparation failure")
        result = original_prepare(item, **kwargs)
        if failure_phase == "duplicate" and item.source_url.endswith("/1"):
            result = replace(result, processing_result=replace(
                result.processing_result, ingest_request=replace(
                    result.processing_result.ingest_request,
                    source_url=jobs[0].source_url, raw_text=jobs[0].raw_text,
                ),
            ))
        return result

    def importer(request, db, storage, settings):
        writer_threads.append(get_ident())
        result = original_import(request, db, storage, settings, run_fit_analysis=False)
        if failure_phase == "write" and request.source_url.endswith("/1"):
            raise RuntimeError("isolated write failure after artifacts")
        return result

    monkeypatch.setattr(module, "prepare_posting_batch_item", prepare)
    monkeypatch.setattr(posting_links, "default_posting_fetcher", lambda _: SimpleNamespace(
        resolve=lambda url, context: jobs[int(url.rsplit("/", 1)[1])],
    ))
    if source_kind == "board":
        monkeypatch.setattr(workflows, "get_ats_adapter", lambda _: SimpleNamespace(
            provider="workday", validate_source_url=lambda _: None, fetch_jobs=lambda _: jobs,
        ))
        monkeypatch.setattr(workflows, "persist_job_ingest_result", importer)
        with session_scope(settings) as db:
            result = workflows.ingest_ats_request(AtsIngestRequest(
                provider="workday", source_url=base, skip_interest_filter=True,
                skip_language_filter=True,
            ), db, storage, settings)
    else:
        with session_scope(settings) as db:
            source = JobAlertSource(job_alert_source_id="parallel", candidate_id="candidate_local",
                                    display_name="Test", gmail_query="test", skip_interest_filter=True,
                                    skip_language_filter=True)
            db.add(source)
            db.commit()
            result = gmail_processing.process_gmail_source(
                db=db, storage=storage, settings=settings, source=source, job_importer=importer,
                messages=[_message(index, job.source_url) for index, job in enumerate(jobs)],
            )
        assert result["mailbox_mutations"] == []

    assert result["jobs_created_count"] == 2
    if failure_phase == "duplicate":
        key = "jobs_duplicate_count" if source_kind == "board" else "jobs_reused_count"
        assert result[key] == 1
    assert set(writer_threads) == {caller}
    assert len(set(prepared_threads)) == 2 and caller not in prepared_threads
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 2
        if source_kind == "board":
            assert sorted(row.status for row in db.scalars(select(JobIngestionItem))) == [
                "imported" if failure_phase == "duplicate" else "failed", "imported", "imported",
            ]
        else:
            assert sorted(row.ingestion_status for row in db.scalars(select(JobAlertLink))) == [
                "imported", "imported", "imported" if failure_phase == "duplicate" else "ready_for_review",
            ]
    assert len(list((storage.root / "job-snapshots").iterdir())) == 2


def test_board_runs_and_direct_links_do_not_invert_resource_locks(
    jobcockpit_settings_factory, monkeypatch, allow_public_dns,
):
    settings = jobcockpit_settings_factory(posting_rendered_fallback_provider="disabled")
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    boards_started = Barrier(8, timeout=10)
    links_acquired = Barrier(8, timeout=10)
    timeouts = []
    allow_public_dns([f"board{index}.invalid" for index in range(4)])

    class TimedFetchSlots:
        # Bound a failed regression instead of hanging the test process forever.
        def __init__(self):
            self.slots = BoundedSemaphore(4)

        def __enter__(self):
            if not self.slots.acquire(timeout=2):
                timeouts.append("fetch")
                raise TimeoutError("fetch/link lock inversion")

        def __exit__(self, *_):
            self.slots.release()

    def fetch_board(request):
        boards_started.wait()
        links_acquired.wait()
        # Exercise the real adapter worker path without any network or job writes.
        fetch_details(request, [(request.source_url, lambda: None)])
        return []

    monkeypatch.setitem(concurrency._resource_slots, "fetch", TimedFetchSlots())
    monkeypatch.setattr(workflows, "get_ats_adapter", lambda _: SimpleNamespace(
        provider="workday", validate_source_url=lambda _: None, fetch_jobs=fetch_board,
    ))

    def board(index):
        with session_scope(settings) as db:
            return workflows.ingest_ats_request(AtsIngestRequest(
                provider="workday", source_url=f"https://board{index}.invalid/jobs",
                skip_interest_filter=True, skip_language_filter=True,
            ), db, storage, settings)

    def direct(index):
        boards_started.wait()
        with link_request_slot(f"https://direct{index}.invalid/jobs"):
            links_acquired.wait()
            try:
                return concurrency.bounded_call("fetch", lambda: "fetched")
            except TimeoutError:
                return "deadlock"

    with ThreadPoolExecutor(max_workers=8) as pool:
        boards = [pool.submit(board, index) for index in range(4)]
        links = [pool.submit(direct, index) for index in range(4)]
        outcomes = [future.result(timeout=20) for future in links]
        for future in boards:
            future.result(timeout=20)
    assert timeouts == []
    assert outcomes == ["fetched"] * 4


@pytest.mark.parametrize("empty_message, link_failure", [(False, False), (False, True), (True, False)])
def test_gmail_checkpoints_completed_message_before_later_interruption(
    jobcockpit_settings_factory, monkeypatch, empty_message, link_failure,
):
    settings = jobcockpit_settings_factory(posting_rendered_fallback_provider="disabled")
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    messages = [_message(index, f"https://jobs.invalid/jobs/{index}") for index in range(2)]
    if empty_message:
        messages[0]["html"] = ""
    prepared_urls = []

    def prepare(item, **_):
        prepared_urls.append(item.source_url)
        if link_failure and item.source_url.endswith("/0") and prepared_urls.count(item.source_url) == 1:
            raise RuntimeError("isolated link failure")
        from jobcockpit.ingestion.posting_batch import PreparedPostingBatchItem
        return PreparedPostingBatchItem(item, posting_links.PostingLinkProcessingResult(
            classification=posting_links.PostingLinkClassification("probable_job_posting", 1, "test"),
            ingestion_status="terminal_status_detected", original_url=item.source_url,
            resolved_url=item.source_url, canonical_url=item.source_url,
        ))

    real_prepare_links = item_execution.prepare_links

    def interrupted(items, prepare, **_):
        if not empty_message:
            item = next(iter(items))
            try:
                result = prepare(item)
            except Exception as error:
                result = error
            yield item, result
        raise RuntimeError("interrupted before remaining messages finish")

    monkeypatch.setattr(gmail_processing, "prepare_posting_batch_item", prepare)
    with session_scope(settings) as db:
        db.add(JobAlertSource(
            job_alert_source_id="checkpoint", candidate_id="candidate_local", display_name="Test",
            gmail_query="test", skip_interest_filter=True, skip_language_filter=True,
        ))

    def run():
        with session_scope(settings) as db:
            return gmail_processing.process_gmail_source(
                db=db, storage=storage, settings=settings, source=db.get(JobAlertSource, "checkpoint"),
                messages=messages, posting_resolver=SimpleNamespace(resolve=lambda _: None),
            )

    monkeypatch.setattr(item_execution, "prepare_links", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        run()
    with session_scope(settings) as db:
        state = db.scalar(select(JobAlertEmailProcessingState).where(
            JobAlertEmailProcessingState.provider_message_id_hash == messages[0]["id_hash"],
        ))
        assert state.processing_status == ("needs_review" if link_failure else "processed")
    monkeypatch.setattr(item_execution, "prepare_links", real_prepare_links)
    run()
    assert prepared_urls.count("https://jobs.invalid/jobs/0") == (
        0 if empty_message else 2 if link_failure else 1
    )
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobAlertEmail).where(
            JobAlertEmail.provider_message_id_hash == messages[0]["id_hash"],
        )) == 1
