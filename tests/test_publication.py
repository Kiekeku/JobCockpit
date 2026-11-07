"""Publication contracts use explicitly supplied fields, not parser behavior."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from jobcockpit.db.models import (
    JobPosting, JobPublication, JobStatusCheck, JobStatusScanRun, StoredArtifact,
)
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.services.publication import excluded_publication_ids, process_next_publication
from jobcockpit.storage.artifacts import ArtifactStorageService


ASSESSMENT = {"summary": "Build and test backend services with an engineering team. Eligibility must be confirmed.",
              "verdict": "Consider after checking eligibility", "tags": ["Python"]}


@pytest.fixture
def publication_settings(jobcockpit_settings_factory, tmp_path, allow_public_dns):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "endpoint": "https://board.example/api/jobs",
        "rules": {"all": [
            {"field": "fit_analysis.fit_score", "op": "gte", "value": 60},
            {"field": "employment_timing.start_date", "op": "between",
             "value": ["2031-05-01", "2031-08-31"]},
        ]},
        "excluded_providers": ["ats_partner"], "source_hosts": ["board.example"],
        "audience_brief": "Students seeking technical placements.",
        "display_term": "Summer 2031", "standard_duration_months": 4,
        "country_names": {"Canada": "Canada"},
    }), encoding="utf-8")
    settings = jobcockpit_settings_factory(publication_enabled=True,
        publication_policy_path=path, publication_write_token="test-write-only")
    init_database(settings)
    allow_public_dns("board.example")
    return settings


def add_job(db, settings, *, job_id="job_test", **overrides):
    artifact = ArtifactStorageService(settings.artifact_root).write_bytes(
        artifact_type="job_snapshot", content=b"Public posting evidence", job_id=job_id,
        original_file_name="posting.txt", content_type="text/plain", privacy_class="private",
    )
    db.add(StoredArtifact.from_record(artifact))
    db.flush()
    values = dict(id=job_id, source_type="ats_example", source_url="https://employer.example/jobs/1",
        posting_snapshot_hash=job_id, raw_content_artifact_id=artifact.artifact_id,
        company_name="Example", normalized_title="Engineering Intern", role_type="internship",
        status="active", source={}, role={"job_title": "Engineering Intern",
        "description_clean": "Build backend services", "tech_stack": ["Python"]},
        employment_timing={"start_date": "2031-05-01", "raw_label": "Summer 2031"},
        duration_months=8, fit_analysis={"fit_score": 60, "reasons": ["PRIVATE_CANDIDATE"]},
        location={"country": "Canada", "city": "Toronto"},
        status_check_result="open", status_last_checked_at=datetime.now(UTC),
        status_check_evidence_code="workday_structured_job_match")
    values.update(overrides)
    job = JobPosting(**values)
    db.add(job)
    return job


def rows(settings):
    with session_scope(settings) as db:
        result = list(db.scalars(select(JobPublication)))
        db.expunge_all()
        return result


def send(settings, *, assessor=None, handler=None, **kwargs):
    def success(request):
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-write-only"
        assert "PRIVATE_CANDIDATE" not in request.content.decode()
        return httpx.Response(201, json={"data": payload,
                                       "publicationRevision": payload["publicationRevision"]})
    return process_next_publication(settings, assessor=assessor or (lambda *args: ASSESSMENT),
                                    transport=httpx.MockTransport(handler or success), **kwargs)


@pytest.mark.parametrize("score,expected", [(None, 0), (59, 0), (60, 1), (99, 1)])
def test_selection_boundary(publication_settings, score, expected):
    with session_scope(publication_settings) as db:
        add_job(db, publication_settings, fit_analysis={"fit_score": score})
    assert len(rows(publication_settings)) == expected


@pytest.mark.parametrize("overrides", [
    {"employment_timing": {"start_date": "2031-09-01"}},
    {"source_type": "ats_partner"},
    {"source": {"cross_source_matches": [{"source_type": "ats_partner"}]}},
    {"source": {"portal_source_urls": ["https://board.example/api/jobs"]}},
    {"source_url": "https://board.example/jobs/1"},
    {"status": "closed"},
])
def test_exclusions(publication_settings, overrides):
    with session_scope(publication_settings) as db:
        add_job(db, publication_settings, **overrides)
    assert not rows(publication_settings)


def test_transaction_rollback_and_deferred_fit(publication_settings):
    settings = publication_settings
    with pytest.raises(RuntimeError), session_scope(settings) as db:
        add_job(db, settings)
        db.flush()
        raise RuntimeError("rollback")
    assert not rows(settings)
    with session_scope(settings) as db:
        add_job(db, settings, fit_analysis={})
    assert not rows(settings)
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").fit_analysis = {"fit_score": 60}
    assert len(rows(settings)) == 1
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").application = {"notes": "private"}
    assert len(rows(settings)) == 1
    assert rows(settings)[0].attempts == 0


def test_delivery_and_bulk_status_update_reuse_assessment(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    generated = []
    def assess(data, *args):
        assert "PRIVATE_CANDIDATE" not in json.dumps(data)
        generated.append(data)
        return ASSESSMENT
    assert send(settings, assessor=assess)["status"] == "succeeded"
    first = rows(settings)[0]
    assert "8 months" in first.payload["summary"]
    with session_scope(settings) as db:
        db.add(JobStatusScanRun(status_scan_run_id="scan"))
        db.flush()
        db.execute(update(JobPosting).where(JobPosting.id == "job_test").values(
            status="closed", status_check_result="closed", status_last_checked_at=datetime.now(UTC)))
        db.add(JobStatusCheck(status_check_id="check", status_scan_run_id="scan", job_id="job_test",
            result="closed", evidence_code="posting_explicitly_closed", status_before="active",
            status_after="closed"))
    assert rows(settings)[0].status == "pending"
    assert send(settings, assessor=assess)["status"] == "succeeded"
    last = rows(settings)[0]
    assert len(generated) == 1
    assert last.remote_id == first.remote_id
    assert last.payload["group"] == "closed"
    assert last.payload["summary"] == first.payload["summary"]


def test_http_retry_and_identity_exclusion_survive_disabled_sender(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    assert send(settings, handler=lambda r: httpx.Response(503))["status"] == "retry_scheduled"
    row = rows(settings)[0]
    assert row.payload and not row.published_at
    settings.publication_enabled = False
    with session_scope(settings) as db:
        assert excluded_publication_ids(db, settings, "https://board.example/api/jobs") == {row.remote_id}
        assert not excluded_publication_ids(db, settings, "https://different.example/api/jobs")
    assert send(settings) is None
    settings.publication_enabled = True
    with session_scope(settings) as db:
        db.get(JobPublication, row.publication_id).next_attempt_at = datetime.now(UTC)
    assert send(settings, assessor=lambda *args: pytest.fail("must use cached assessment"))["status"] == "succeeded"


@pytest.mark.parametrize("code,expected", [(401, "failed"), (422, "failed"), (429, "retry_scheduled")])
def test_http_failure_policy(publication_settings, code, expected):
    with session_scope(publication_settings) as db:
        add_job(db, publication_settings)
    assert send(publication_settings, handler=lambda r: httpx.Response(code))["status"] == expected


def test_newer_change_during_assessment_is_not_published(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    def changed(*args):
        with session_scope(settings) as db:
            db.get(JobPosting, "job_test").company_name = "Updated Example"
        return ASSESSMENT
    assert send(settings, assessor=changed, handler=lambda r: pytest.fail("obsolete payload")) is None
    assert rows(settings)[0].status == "pending"
    assert send(settings)["status"] == "succeeded"
    assert rows(settings)[0].payload["company"] == "Updated Example"


def test_newer_change_during_send_stays_queued(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    def changed(request):
        with session_scope(settings) as db:
            db.get(JobPosting, "job_test").status = "closed"
        payload = json.loads(request.content)
        return httpx.Response(201, json={"data": payload,
                                       "publicationRevision": payload["publicationRevision"]})
    assert send(settings, handler=changed)["status"] == "pending"
    assert send(settings)["status"] == "succeeded"
    assert rows(settings)[0].payload["group"] == "closed"


def test_unverified_and_invalid_assessment_never_post(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings, status_last_checked_at=None)
    result = send(settings, assessor=lambda *args: pytest.fail("must validate before LLM"),
                  handler=lambda r: pytest.fail("unverified posting"))
    assert result["status"] == "blocked"
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").status_last_checked_at = datetime.now(UTC)
    result = send(settings, assessor=lambda *args: {}, handler=lambda r: pytest.fail("invalid assessment"))
    assert result["status"] == "retry_scheduled"


def test_default_off_routes_and_automatic_queue(publication_settings, jobcockpit_client_factory):
    settings = publication_settings
    settings.publication_enabled = False
    with session_scope(settings) as db:
        add_job(db, settings)
    assert not rows(settings)
    assert send(settings) is None
    with jobcockpit_client_factory(settings) as client:
        assert client.get("/api/publications").status_code == 404


def test_explicit_backfill_preview(publication_settings, jobcockpit_client_factory):
    settings = publication_settings
    settings.publication_enabled = False
    with session_scope(settings) as db:
        add_job(db, settings)
    settings.publication_enabled = True
    with jobcockpit_client_factory(settings) as client:
        preview = client.post("/api/publications/enqueue-existing").json()
        assert preview["eligible_job_ids"] == ["job_test"]
        assert not rows(settings)
        assert client.post("/api/publications/enqueue-existing?dry_run=false").status_code == 200
        assert len(rows(settings)) == 1
        assert client.post("/api/publications/enqueue-existing").json()["eligible_job_ids"] == []


def test_lost_lease_during_assessment_fences_http(publication_settings):
    from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
    from jobcockpit.services.publication import PUBLICATION_LEASE

    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    with BackgroundWorkerLeaseGuard(settings, "test-worker", lease_name=PUBLICATION_LEASE) as lease:
        def lost(*args):
            lease.relinquish()
            return ASSESSMENT
        assert send(settings, assessor=lost, worker_id="test-worker",
            supervisor_lease_token=lease.lease_token,
            handler=lambda r: pytest.fail("lost lease cannot send")) is None
    assert rows(settings)[0].status == "running"
    assert send(settings)["status"] == "succeeded"


def test_retry_limit_and_unexpected_provider_failure(publication_settings):
    settings = publication_settings
    settings.publication_max_attempts = 1
    with session_scope(settings) as db:
        add_job(db, settings)
    def failure(*args):
        raise RuntimeError("PRIVATE_SECRET")
    result = send(settings, assessor=failure, handler=lambda r: pytest.fail("no valid assessment"))
    assert result["status"] == "failed"
    assert "PRIVATE_SECRET" not in json.dumps(result)
    assert send(settings) is None


def test_lower_score_cancels_unsent_but_not_published_lifecycle(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").fit_analysis = {"fit_score": 59}
    assert rows(settings)[0].status == "cancelled"
    assert send(settings) is None
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").fit_analysis = {"fit_score": 60}
    assert send(settings)["status"] == "succeeded"
    with session_scope(settings) as db:
        job = db.get(JobPosting, "job_test")
        job.fit_analysis = {"fit_score": 59}
        job.status = "closed"
    assert send(settings)["status"] == "succeeded"
    assert rows(settings)[0].payload["group"] == "closed"


def test_public_url_strips_credentials_and_tracking(publication_settings):
    with session_scope(publication_settings) as db:
        add_job(db, publication_settings,
            source_url="https://employer.example/jobs?id=123&token=PRIVATE_SECRET#session")
    assert send(publication_settings)["status"] == "succeeded"
    assert rows(publication_settings)[0].payload["url"] == "https://employer.example/jobs?id=123"


def test_merged_publication_is_withdrawn_without_changing_identity(publication_settings):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    assert send(settings)["status"] == "succeeded"
    remote_id = rows(settings)[0].remote_id
    with session_scope(settings) as db:
        job = db.get(JobPosting, "job_test")
        job.source = {"merged_into_job_id": "canonical"}
        job.source_url = None
    assert send(settings)["status"] == "succeeded"
    row = rows(settings)[0]
    assert row.remote_id == remote_id
    assert row.payload["status"] == "Withdrawn"
    assert "does not confirm employer closure" in row.payload["statusEvidence"]


def test_data_only_assessment_runner(publication_settings):
    from subprocess import CompletedProcess
    from jobcockpit.services.publication_assessment import generate_assessment
    from jobcockpit.services.publication_policy import load_policy

    def runner(args, **kwargs):
        assert "--ignore-user-config" in args and "--ephemeral" in args
        assert "features.shell_tool=false" in args and "project_doc_max_bytes=0" in args
        assert "features.memories=false" in args and 'web_search="disabled"' in args
        assert args[args.index("--cd") + 1] != str(publication_settings.codex_cli_workspace)
        assert "Posting evidence" in kwargs["input"]
        return CompletedProcess(args, 0, stdout=json.dumps(ASSESSMENT), stderr="")
    assert generate_assessment({"company": "Example"}, load_policy(publication_settings),
                               publication_settings, runner=runner) == ASSESSMENT


def test_publication_migration_matches_model(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from sqlalchemy import create_engine
    from jobcockpit.db.models import Base

    monkeypatch.delenv("JOBCOCKPIT_DATABASE_URL", raising=False)
    config = Config("alembic.ini")
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20260906_0031")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            changes = compare_metadata(MigrationContext.configure(connection, opts={
                "include_object": lambda obj, name, typ, reflected, compare_to:
                    name == "job_publications" if typ == "table" else True,
            }), Base.metadata)
            assert changes == []
    finally:
        engine.dispose()


def test_publication_created_during_feed_fetch_cannot_be_reingested(publication_settings, monkeypatch):
    from jobcockpit.ingestion.ats_adapters.base import AtsJob
    from jobcockpit.schemas.requests import AtsIngestRequest
    from jobcockpit.services import workflows

    settings = publication_settings
    class Adapter:
        provider = "example"
        def validate_source_url(self, source_url):
            pass
        def fetch_jobs(self, request):
            assert not request.excluded_provider_job_ids
            with session_scope(settings) as db:
                add_job(db, settings)
            remote_id = rows(settings)[0].remote_id
            return [AtsJob(source_url="https://changed.example/jobs/2", raw_text="Public posting",
                           title="Engineering Intern", provider_job_id=remote_id)]

    monkeypatch.setattr(workflows, "get_ats_adapter", lambda _: Adapter())
    with session_scope(settings) as db:
        result = workflows.ingest_ats_request(AtsIngestRequest(provider="example",
            source_url="https://board.example/api/jobs", skip_interest_filter=True),
            db, ArtifactStorageService(settings.artifact_root), settings)
        assert result["jobs_created_count"] == 0
        assert db.query(JobPosting).count() == 1


@pytest.mark.parametrize("status,remote", [("filled", "Filled"), ("withdrawn", "Withdrawn"),
                                         ("expired", "Expired")])
def test_explicit_terminal_status_wins_over_old_open_evidence(publication_settings, status, remote):
    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    assert send(settings)["status"] == "succeeded"
    with session_scope(settings) as db:
        db.get(JobPosting, "job_test").status = status
    assert send(settings)["status"] == "succeeded"
    payload = rows(settings)[0].payload
    assert payload["status"] == remote
    assert payload["verdict"] != "Duplicate listing removed"
    assert status in payload["statusEvidence"]


def test_late_write_after_takeover_cannot_reopen_remote(publication_settings):
    from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
    from jobcockpit.services.publication import PUBLICATION_LEASE

    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    remote = {}
    def receiver(request):
        payload = json.loads(request.content)
        if payload["publicationRevision"] < remote.get("publicationRevision", 0):
            return httpx.Response(409)
        remote.update(payload)
        return httpx.Response(200, json={"data": payload,
                                        "publicationRevision": payload["publicationRevision"]})
    with BackgroundWorkerLeaseGuard(settings, "old", lease_name=PUBLICATION_LEASE) as lease:
        def delayed(request):
            lease.relinquish()
            with session_scope(settings) as db:
                db.get(JobPosting, "job_test").status = "closed"
            assert send(settings, handler=receiver)["status"] == "succeeded"
            return receiver(request)
        assert send(settings, handler=delayed, worker_id="old",
                    supervisor_lease_token=lease.lease_token) is None
    assert remote["status"] == "Expired"
    assert rows(settings)[0].status == "succeeded"
    assert send(settings) is None


def test_receiver_must_acknowledge_revision(publication_settings):
    with session_scope(publication_settings) as db:
        add_job(db, publication_settings)
    result = send(publication_settings, handler=lambda request:
        httpx.Response(201, json={"data": json.loads(request.content)}))
    assert result["status"] == "retry_scheduled"


def test_applied_publication_remains_in_due_status_checks(publication_settings):
    from jobcockpit.db.models import Application
    from jobcockpit.ingestion.status_reconciliation import _select_targets

    settings = publication_settings
    with session_scope(settings) as db:
        job = add_job(db, settings)
        db.flush()
        db.add(Application(application_id="applied_publication", candidate_id="candidate",
                           job_id=job.id, status="applied", applied_at=datetime.now(UTC)))
    assert not _select_targets(settings, datetime.now(UTC), 10, "job_test", False)
    assert send(settings)["status"] == "succeeded"
    assert [target.job_id for target in _select_targets(
        settings, datetime.now(UTC), 10, "job_test", False)] == ["job_test"]
    settings.publication_enabled = False
    assert not _select_targets(settings, datetime.now(UTC), 10, "job_test", True)


def test_concurrent_enqueues_allocate_distinct_revisions(publication_settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from jobcockpit.services.publication import enqueue_job

    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    assert send(settings)["status"] == "succeeded"
    staged, competing = Event(), Event()
    def first():
        with session_scope(settings) as db:
            job = db.get(JobPosting, "job_test")
            job.status = "filled"
            row = enqueue_job(db, job, settings)
            revision = row.input_data["revision"]
            staged.set()
            assert competing.wait(5)
        return revision
    def second():
        assert staged.wait(5)
        with session_scope(settings) as db:
            job = db.get(JobPosting, "job_test")
            # Cache the old outbox before the competing transaction commits.
            db.get(JobPublication, rows(settings)[0].publication_id)
            job.status = "withdrawn"
            competing.set()
            row = enqueue_job(db, job, settings)
            revision = row.input_data["revision"]
        return revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        assert (a.result(), b.result()) == (2, 3)
    assert send(settings)["status"] == "succeeded"
    assert rows(settings)[0].payload["status"] == "Withdrawn"


@pytest.mark.parametrize("private_first", [False, True])
def test_production_post_pins_dns_and_ignores_proxy(publication_settings, monkeypatch, private_first):
    import socket
    import httpcore
    from test_pinned_status_http import _CannedHttpStream, _RecordingBackend

    settings = publication_settings
    with session_scope(settings) as db:
        add_job(db, settings)
    remote_id = rows(settings)[0].remote_id
    body = json.dumps({"data": {"id": remote_id}, "publicationRevision": 1}).encode()
    stream = _CannedHttpStream(b"HTTP/1.1 201 Created\r\nContent-Length: " +
                              str(len(body)).encode() + b"\r\n\r\n" + body)
    backend = _RecordingBackend(stream)
    monkeypatch.setattr(httpcore, "SyncBackend", lambda: backend)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    calls = []
    def dns(host, port, *args, **kwargs):
        calls.append(host)
        address = "127.0.0.1" if private_first or len(calls) > 1 else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    result = process_next_publication(settings, assessor=lambda *args: ASSESSMENT)
    assert calls == ["board.example"]
    if private_first:
        assert result["status"] == "retry_scheduled"
        assert not backend.connects and not stream.writes
    else:
        assert result["status"] == "succeeded"
        assert backend.connects == [("93.184.216.34", 443)]
        assert stream.server_hostname == "board.example"
        assert b"Host: board.example" in b"".join(stream.writes)
        assert b"Bearer test-write-only" in b"".join(stream.writes)
