from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from jobcockpit.services import workflows as api_main
from jobcockpit.api import job_reprocess_runs as reprocess_queue
from jobcockpit.api import page_capture_processing as capture_processing
from jobcockpit.api import page_capture_runs as capture_queue
from jobcockpit.api.background_worker_leases import (
    acquire_background_worker_lease,
    release_background_worker_lease,
)
from jobcockpit.api.job_merge import merge_jobs_for_authoritative_identity
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    JobPosting,
    JobReprocessRun,
    JobSourceIdentity,
    PageCaptureRun,
    PageCaptureSnapshotScope,
    PageCaptureSnapshot,
    PageCaptureSubjectHead,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.schemas.requests import ExtensionPageCaptureRequest
from jobcockpit.utils.public_urls import sanitize_public_source_url


def _capture_request(*, visible_text: str = "Intern\nCompany: Acme"):
    return ExtensionPageCaptureRequest(
        source_url="https://jobs.example.com/intern?id=123",
        page_title="Intern | Acme",
        visible_text=visible_text,
    )


@pytest.mark.parametrize("queue_kind", ["page_capture", "job_reprocess"])
def test_new_worker_recovers_recent_run_owned_by_previous_supervisor(
    queue_kind,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    if queue_kind == "page_capture":
        run_id = client.post(
            "/api/extension/page-captures",
            json=_capture_request().model_dump(),
        ).json()["capture_id"]
        with session_scope(settings) as db:
            assert capture_queue.claim_page_capture_run(db, run_id, worker_id="worker_previous")
        recovered = capture_queue.recover_stale_page_capture_runs(
            settings,
            active_worker_id="worker_replacement",
            recover_orphaned=True,
        )
        model = PageCaptureRun
        key = "capture_id"
    else:
        job = client.post(
            "/api/jobs/ingest",
            json={"source_type": "paste", "raw_text": "Intern\nCompany: Acme"},
        ).json()
        with session_scope(settings) as db:
            run, _ = reprocess_queue.enqueue_job_reprocess(db.get(JobPosting, job["id"]), db)
            run_id = run.reprocess_run_id
            assert reprocess_queue.claim_job_reprocess_run(db, run_id, worker_id="worker_previous")
        recovered = reprocess_queue.recover_stale_job_reprocess_runs(
            settings,
            active_worker_id="worker_replacement",
            recover_orphaned=True,
        )
        model = JobReprocessRun
        key = "reprocess_run_id"

    assert recovered == 1
    with session_scope(settings) as db:
        run = db.execute(select(model).where(getattr(model, key) == run_id)).scalar_one()
        assert run.status == "retry_scheduled"
        assert run.next_attempt_at is not None
        assert run.worker_id is None


def test_stale_final_capture_failure_closes_standard_ingestion_audit(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    capture_id = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()["capture_id"]
    with session_scope(settings) as db:
        run = capture_queue.claim_page_capture_run(
            db,
            capture_id,
            worker_id="stale_worker",
        )
        run.attempts = capture_queue.PAGE_CAPTURE_MAX_ATTEMPTS
        run.started_at = (
            datetime.now(UTC) - capture_queue.PAGE_CAPTURE_STALE_AFTER - timedelta(seconds=1)
        )

    assert capture_queue.recover_stale_page_capture_runs(settings) == 1

    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, capture_id)
        audit = db.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.event_type == "ingestion.run_failed",
                AuditLogEntry.entity_id == capture_id,
            )
        ).scalar_one()
        assert run.status == "failed"
        assert audit.payload["provider"] == "browser_capture"
        assert audit.payload["status"] == "failed"
        assert audit.payload["error_code"] == capture_queue.PAGE_CAPTURE_STALE_ERROR
        assert audit.payload["counts"]["runs_failed_count"] == 1


def test_capture_claim_commits_started_audit_before_snapshot_load_failure(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    capture_id = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()["capture_id"]

    def fail_snapshot_load(*_args, **_kwargs):
        # Read through another session before injecting the failure. Timestamp
        # ties and random audit IDs cannot establish commit order afterward.
        with session_scope(settings) as db:
            committed_events = list(db.scalars(
                select(AuditLogEntry.event_type)
                .where(AuditLogEntry.entity_id == capture_id)
                .where(AuditLogEntry.event_type.in_({"ingestion.run_started", "ingestion.run_failed"}))
            ))
            assert committed_events == ["ingestion.run_started"]
        raise capture_queue.PageCaptureProcessingError(
            "capture_artifact_missing",
            "Captured page artifacts are unavailable",
            retryable=False,
        )

    monkeypatch.setattr(
        capture_processing,
        "page_capture_request_from_run",
        fail_snapshot_load,
    )
    result = api_main.process_page_capture_run(capture_id, settings, client.app.state.storage)

    assert result["status"] == "failed"
    with session_scope(settings) as db:
        events = list(
            db.execute(
                select(AuditLogEntry.event_type)
                .where(AuditLogEntry.entity_id == capture_id)
                .where(
                    AuditLogEntry.event_type.in_({"ingestion.run_started", "ingestion.run_failed"})
                )
            ).scalars()
        )
    assert sorted(events) == ["ingestion.run_failed", "ingestion.run_started"]


def test_only_one_api_worker_supervisor_can_hold_the_database_lease(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(background_worker_lease_seconds=10)
    jobcockpit_client_factory(settings)

    assert acquire_background_worker_lease(settings, "worker_one") is True
    assert acquire_background_worker_lease(settings, "worker_two") is False
    assert release_background_worker_lease(settings, "worker_one") is True
    assert acquire_background_worker_lease(settings, "worker_two") is True


@pytest.mark.parametrize("requested_type", ["fit_analysis", "full_reprocess"])
def test_reprocess_dedup_conflict_does_not_rollback_outer_job_update(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    requested_type,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job_payload = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    original_lookup = reprocess_queue.active_job_reprocess_run
    lookup_count = 0

    def create_racing_winner(db, job_id):
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 1:
            winner = JobReprocessRun(
                reprocess_run_id="reprocess_race_winner",
                job_id=job_id,
                run_type=reprocess_queue.JOB_REPROCESS_TYPE_FIT_ANALYSIS,
                status="queued",
                result={},
            )
            db.add(winner)
            db.flush()
            return None
        return original_lookup(db, job_id)

    monkeypatch.setattr(reprocess_queue, "active_job_reprocess_run", create_racing_winner)
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_payload["id"])
        job.normalized_title = "Updated inside capture transaction"
        winner, queued = reprocess_queue.enqueue_job_reprocess(
            job,
            db,
            run_type=requested_type,
        )
        assert queued is False
        assert winner.reprocess_run_id == "reprocess_race_winner"
        assert winner.run_type == requested_type

    with session_scope(settings) as db:
        assert db.get(JobPosting, job_payload["id"]).normalized_title == (
            "Updated inside capture transaction"
        )


def test_capture_unique_conflict_returns_winner_without_orphan_artifacts(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    storage = client.app.state.storage
    request = _capture_request()
    with session_scope(settings) as db:
        winner, queued = capture_queue.enqueue_page_capture(
            request, request.visible_text, db, storage
        )
        assert queued is True
        winner_id = winner.capture_id

    with session_scope(settings) as db:
        before_artifacts = len(list(db.execute(select(StoredArtifact)).scalars()))
        duplicate, queued = capture_queue.enqueue_page_capture(
            request, request.visible_text, db, storage
        )
        assert queued is False
        assert duplicate.capture_id == winner_id

    with session_scope(settings) as db:
        assert len(list(db.execute(select(StoredArtifact)).scalars())) == before_artifacts


def test_plain_text_manual_retry_preserves_capture_identity_and_artifacts(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            **_capture_request().model_dump(),
            "visible_text": "Visible browser page",
            "raw_text": "Parser-oriented snapshot\nCompany: Acme",
        },
    ).json()
    with session_scope(settings) as db:
        original = db.get(PageCaptureRun, queued["capture_id"])
        original.status = "failed"
        original.retryable = True
        original.finished_at = datetime.now(UTC)
        original_snapshot = db.get(PageCaptureSnapshot, original.snapshot_id)
        original_hash = original_snapshot.content_hash
        artifact_ids = (
            original_snapshot.raw_content_artifact_id,
            original_snapshot.source_archive_artifact_id,
        )

    retried = client.post(f"{queued['status_url']}/retry")

    assert retried.status_code == 202
    with session_scope(settings) as db:
        retry_run = db.get(PageCaptureRun, retried.json()["capture_id"])
        retry_snapshot = db.get(PageCaptureSnapshot, retry_run.snapshot_id)
        assert retry_snapshot.content_hash == original_hash
        assert (
            retry_snapshot.raw_content_artifact_id,
            retry_snapshot.source_archive_artifact_id,
        ) == artifact_ids


def test_manual_retry_enters_serialized_transaction_before_loading_run(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        run.status = "failed"
        run.retryable = True
        run.finished_at = datetime.now(UTC)

    transaction_states: list[bool] = []
    original_begin = capture_queue._begin_immediate

    def observe_begin(db):
        transaction_states.append(db.in_transaction())
        return original_begin(db)

    monkeypatch.setattr(capture_queue, "_begin_immediate", observe_begin)

    response = client.post(f"{queued['status_url']}/retry")

    assert response.status_code == 202
    assert transaction_states[0] is False


def test_failed_targeted_capture_does_not_establish_source_identity(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/unaccepted-target-identity",
            "page_title": "Unaccepted target identity",
            "visible_text": "Updated\nCompany: Other",
            "target_job_id": job["id"],
        },
    ).json()

    def fail_parse(*_args, **_kwargs):
        raise TimeoutError("synthetic parser timeout")

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", fail_parse)
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "retry_scheduled"
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        assert (
            list(
                db.execute(
                    select(JobSourceIdentity).where(
                        JobSourceIdentity.job_id == job["id"],
                        JobSourceIdentity.provider == run.provider,
                        JobSourceIdentity.identity_hash == run.identity_hash,
                    )
                ).scalars()
            )
            == []
        )
        assert capture_queue.backfill_job_source_identities(db) == 0
        db.flush()
        assert (
            list(
                db.execute(
                    select(JobSourceIdentity).where(
                        JobSourceIdentity.job_id == job["id"],
                        JobSourceIdentity.provider == run.provider,
                        JobSourceIdentity.identity_hash == run.identity_hash,
                    )
                ).scalars()
            )
            == []
        )


def test_job_merge_rekeys_capture_scopes_and_supersedes_older_retry(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    canonical = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Canonical\nCompany: Acme"},
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Duplicate\nCompany: Acme"},
    ).json()
    older = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/older-duplicate-capture",
            "page_title": "Older duplicate capture",
            "visible_text": "Older\nCompany: Acme",
            "target_job_id": duplicate["id"],
        },
    ).json()
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, older["capture_id"])
        run.status = "retry_scheduled"
        run.next_attempt_at = datetime.now(UTC)
    newer = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/newer-canonical-capture",
            "page_title": "Newer canonical capture",
            "visible_text": "Newer\nCompany: Acme",
            "target_job_id": canonical["id"],
        },
    ).json()

    with session_scope(settings) as db:
        merge_jobs_for_authoritative_identity(
            db.get(JobPosting, canonical["id"]),
            {duplicate["id"]},
            db,
            provider="linkedin",
            identity_hash="authoritative_merge_identity",
            identity_strength="authoritative",
        )

    with session_scope(settings) as db:
        older_run = db.get(PageCaptureRun, older["capture_id"])
        newer_run = db.get(PageCaptureRun, newer["capture_id"])
        assert older_run.target_job_id == canonical["id"]
        assert older_run.status == "superseded"
        assert newer_run.status == "queued"
        job_scope_hashes = {
            capture_id: {
                scope.scope_hash
                for scope in db.execute(
                    select(PageCaptureSnapshotScope).where(
                        PageCaptureSnapshotScope.snapshot_id
                        == db.get(PageCaptureRun, capture_id).snapshot_id,
                        PageCaptureSnapshotScope.scope_kind == "job",
                    )
                ).scalars()
            }
            for capture_id in (older["capture_id"], newer["capture_id"])
        }
        assert job_scope_hashes[older["capture_id"]] == job_scope_hashes[newer["capture_id"]]
        canonical_job_head = db.get(
            PageCaptureSubjectHead,
            capture_queue._scope_hash("job", canonical["id"]),
        )
        assert canonical_job_head.snapshot_id == newer_run.snapshot_id


def test_capture_status_and_audit_strip_sensitive_url_query_values(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    secret = "do-not-expose-this-token"
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": (
                "https://jobs.example.com/intern?id=123"
                f"&access_token={secret}&signature=signed-secret"
            ),
            "page_title": "Intern | Acme",
            "visible_text": "Intern\nCompany: Acme",
        },
    ).json()

    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )
    assert processed["status"] == "succeeded"
    status_body = client.get(queued["status_url"]).text
    job_body = client.get(f"/api/jobs/{processed['job_id']}").text
    with session_scope(settings) as db:
        audit_body = json.dumps(
            [entry.payload for entry in db.execute(select(AuditLogEntry)).scalars()]
        )

    assert secret not in status_body
    assert "signed-secret" not in status_body
    assert secret not in job_body
    assert "signed-secret" not in job_body
    assert secret not in audit_body
    assert "signed-secret" not in audit_body
    assert "id=123" not in status_body
    assert sanitize_public_source_url(
        "https://jobs.example.com/opening?vacancy=123&utm_source=alert"
    ).endswith("?vacancy=123")


def test_targetless_url_recapture_is_associated_by_worker(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://www.linkedin.com/jobs/view/1234567890"
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = source_url
        stored.source = {**stored.source, "source_url": source_url}
        capture_queue.sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Updated | Acme",
            "visible_text": "Updated\nCompany: Acme",
        },
    ).json()

    assert queued["job_id"] is None
    queued_detail = client.get(f"/api/jobs/{job['id']}").json()
    assert queued_detail["capture_processing"]["capture_id"] == queued["capture_id"]
    assert queued_detail["capture_processing"]["status"] == "queued"

    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["job_id"] == job["id"]
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["capture_processing"]["capture_id"] == queued["capture_id"]
    assert detail["capture_processing"]["status"] == "succeeded"


def test_manual_retry_creates_linked_run_when_identical_capture_is_active(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = _capture_request().model_dump()
    failed = client.post("/api/extension/page-captures", json=payload).json()
    with session_scope(settings) as db:
        original = db.get(PageCaptureRun, failed["capture_id"])
        original.status = "failed"
        original.retryable = True
        original.finished_at = datetime.now(UTC)

    active = client.post("/api/extension/page-captures", json=payload).json()
    retried = client.post(f"{failed['status_url']}/retry")

    assert retried.status_code == 202
    assert retried.json()["capture_id"] not in {
        failed["capture_id"],
        active["capture_id"],
    }
    assert retried.json()["retry_of_capture_id"] == failed["capture_id"]
    assert retried.json()["status"] == "superseded"
    assert retried.json()["queued"] is False
    assert client.get(active["status_url"]).json()["status"] == "queued"


def test_replay_is_suppressed_after_same_snapshot_already_succeeded(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    original = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()
    with session_scope(settings) as db:
        run = capture_queue.claim_page_capture_run(
            db,
            original["capture_id"],
            worker_id="worker_original",
        )
        capture_queue.ensure_page_capture_transition(run.status, "failed")
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.worker_id = None
        run.claim_token = None

    successful_replay = client.post(original["retry_url"]).json()
    with session_scope(settings) as db:
        run = capture_queue.claim_page_capture_run(
            db,
            successful_replay["capture_id"],
            worker_id="worker_replay",
        )
        capture_queue.ensure_page_capture_transition(run.status, "succeeded")
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        run.worker_id = None
        run.claim_token = None

    duplicate_replay = client.post(original["retry_url"])

    assert duplicate_replay.status_code == 202
    assert duplicate_replay.json()["status"] == "superseded"
    assert duplicate_replay.json()["queued"] is False
    assert duplicate_replay.json()["error_code"] == "stale_capture_replay"


@pytest.mark.parametrize("newer_status", ["queued", "running", "succeeded"])
def test_retry_of_old_snapshot_never_outranks_newer_capture(
    newer_status,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Tracked job\nCompany: Acme"},
    ).json()
    original_payload = {
        **_capture_request(
            visible_text="Original snapshot\nCompany: Acme"
        ).model_dump(),
        "target_job_id": job["id"],
    }
    original = client.post(
        "/api/extension/page-captures",
        json=original_payload,
    ).json()
    with session_scope(settings) as db:
        run = capture_queue.claim_page_capture_run(
            db,
            original["capture_id"],
            worker_id="worker_original",
        )
        capture_queue.ensure_page_capture_transition(run.status, "failed")
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.worker_id = None
        run.claim_token = None

    newer = client.post(
        "/api/extension/page-captures",
        json={
            **original_payload,
            "visible_text": "Corrected snapshot\nCompany: Acme",
        },
    ).json()
    if newer_status == "queued":
        # SQLite compatibility imports may preserve only second-level capture
        # timestamps. Equal timestamps must still keep runnable evidence ahead
        # of a suppressed stale replay in the job status projection.
        with session_scope(settings) as db:
            original_run = db.get(PageCaptureRun, original["capture_id"])
            newer_run = db.get(PageCaptureRun, newer["capture_id"])
            original_snapshot = db.get(PageCaptureSnapshot, original_run.snapshot_id)
            newer_snapshot = db.get(PageCaptureSnapshot, newer_run.snapshot_id)
            newer_snapshot.created_at = original_snapshot.created_at
    if newer_status != "queued":
        with session_scope(settings) as db:
            run = capture_queue.claim_page_capture_run(
                db,
                newer["capture_id"],
                worker_id="worker_newer",
            )
            if newer_status == "succeeded":
                capture_queue.ensure_page_capture_transition(run.status, "succeeded")
                run.status = "succeeded"
                run.finished_at = datetime.now(UTC)
                run.worker_id = None
                run.claim_token = None

    replay = client.post(original["retry_url"])

    assert replay.status_code == 202
    assert replay.json()["status"] == "superseded"
    assert replay.json()["queued"] is False
    assert replay.json()["retry_of_capture_id"] == original["capture_id"]
    assert replay.json()["error_code"] == "stale_capture_replay"
    assert client.get(newer["status_url"]).json()["status"] == newer_status
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["capture_processing"]["capture_id"] == newer["capture_id"]
    assert detail["capture_processing"]["status"] == newer_status


def test_capture_target_is_normalized_to_active_canonical_job_before_scoping(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    canonical = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Canonical\nCompany: Acme"},
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Duplicate\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        merge_jobs_for_authoritative_identity(
            db.get(JobPosting, canonical["id"]),
            {duplicate["id"]},
            db,
            provider="linkedin",
            identity_hash="canonical_target_test",
            identity_strength="authoritative",
        )

    first = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/first-target",
            "page_title": "First target",
            "visible_text": "First snapshot\nCompany: Acme",
            "target_job_id": duplicate["id"],
        },
    )
    assert first.status_code == 202
    assert first.json()["target_job_id"] == canonical["id"]

    second = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/second-target",
            "page_title": "Second target",
            "visible_text": "Second snapshot\nCompany: Acme",
            "target_job_id": canonical["id"],
        },
    )

    assert second.status_code == 202
    assert client.get(first.json()["status_url"]).json()["status"] == "superseded"
    assert second.json()["status"] == "queued"
    with session_scope(settings) as db:
        scopes = set(
            db.execute(
                select(PageCaptureSnapshotScope.scope_hash).where(
                    PageCaptureSnapshotScope.snapshot_id
                    == db.get(PageCaptureRun, first.json()["capture_id"]).snapshot_id
                )
            ).scalars()
        )
    assert capture_queue._scope_hash("job", canonical["id"]) in scopes
    assert capture_queue._scope_hash("job", duplicate["id"]) not in scopes


def test_blank_capture_target_is_normalized_to_no_target(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/extension/page-captures",
        json={**_capture_request().model_dump(), "target_job_id": "   "},
    )

    assert response.status_code == 202
    assert response.json()["target_job_id"] is None


def test_capture_completion_is_fenced_after_worker_takeover(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Original Co"},
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/fenced-capture",
            "page_title": "Updated | New Co",
            "visible_text": "Updated\nCompany: New Co",
            "target_job_id": job["id"],
        },
    ).json()

    def take_over_during_parse(raw_text, source_url, *_args, **_kwargs):
        assert (
            capture_queue.recover_stale_page_capture_runs(
                settings,
                active_worker_id="worker_replacement",
                recover_orphaned=True,
            )
            == 1
        )
        with session_scope(settings) as db:
            replacement = capture_queue.claim_page_capture_run(
                db,
                queued["capture_id"],
                worker_id="worker_replacement",
            )
            assert replacement is not None
        return parse_job_text(raw_text, source_url)

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", take_over_during_parse)

    result = api_main.process_page_capture_run(
        queued["capture_id"],
        settings,
        client.app.state.storage,
        worker_id="worker_original",
    )

    assert result["status"] == "running"
    assert result["attempts"] == 2
    with session_scope(settings) as db:
        assert db.get(JobPosting, job["id"]).company_name == "Original Co"
        run = db.get(PageCaptureRun, queued["capture_id"])
        assert run.worker_id == "worker_replacement"


def test_reprocess_attempt_removes_files_when_transaction_fails(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        run, _ = reprocess_queue.enqueue_job_reprocess(db.get(JobPosting, job["id"]), db)
        run_id = run.reprocess_run_id

    def write_then_fail(_job, _db, storage, _settings, **_kwargs):
        storage.write_bytes(
            artifact_type="job_snapshot",
            content=b"failed attempt",
            original_file_name="failed-attempt.txt",
            content_type="text/plain",
            privacy_class="private",
        )
        raise TimeoutError("provider timed out after artifact write")

    monkeypatch.setattr(api_main, "prepare_job_reprocess", write_then_fail)
    before = _artifact_files(settings.artifact_root)

    result = api_main.process_job_reprocess_run(
        run_id,
        settings,
        client.app.state.storage,
        worker_id="worker_artifact_rollback",
    )

    assert result["status"] == "retry_scheduled"
    assert _artifact_files(settings.artifact_root) == before


def test_unexpected_programming_errors_are_not_automatically_retried():
    assert capture_queue.page_capture_error_is_retryable(TypeError("bad call")) is False
    assert capture_queue.page_capture_error_is_retryable(TimeoutError("slow provider")) is True
    assert reprocess_queue.job_reprocess_error_is_retryable(TypeError("bad call")) is False
    assert reprocess_queue.job_reprocess_error_is_retryable(TimeoutError("slow provider")) is True


def _artifact_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def test_reprocess_completion_is_fenced_after_worker_takeover(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Original Co"},
    ).json()
    with session_scope(settings) as db:
        run, _ = reprocess_queue.enqueue_job_reprocess(db.get(JobPosting, job["id"]), db)
        run_id = run.reprocess_run_id

    before = _artifact_files(settings.artifact_root)

    def take_over_during_reprocess(job_row, _db, storage, _settings, **_kwargs):
        storage.write_bytes(
            artifact_type="job_snapshot",
            content=b"stale worker artifact",
            original_file_name="stale-worker.txt",
            content_type="text/plain",
            privacy_class="private",
        )
        assert (
            reprocess_queue.recover_stale_job_reprocess_runs(
                settings,
                active_worker_id="worker_replacement",
                recover_orphaned=True,
            )
            == 1
        )
        with session_scope(settings) as db:
            replacement = reprocess_queue.claim_job_reprocess_run(
                db,
                run_id,
                worker_id="worker_replacement",
            )
            assert replacement is not None
        return api_main.JobReprocessComputation(
            run_type=api_main.JOB_REPROCESS_TYPE_FULL, job_updated_at=job_row.updated_at,
            status_before=job_row.status, job_values={"company_name": "Stale Worker Co"},
            posting_update=None, result={"status": "stale_worker_result"},
        )

    monkeypatch.setattr(api_main, "prepare_job_reprocess", take_over_during_reprocess)

    result = api_main.process_job_reprocess_run(
        run_id,
        settings,
        client.app.state.storage,
        worker_id="worker_original",
    )

    assert result["status"] == "running"
    assert result["attempts"] == 2
    with session_scope(settings) as db:
        assert db.get(JobPosting, job["id"]).company_name == "Original Co"
        run = db.get(JobReprocessRun, run_id)
        assert run.worker_id == "worker_replacement"
    assert _artifact_files(settings.artifact_root) == before


def test_capture_processing_uses_canonical_identity_without_exposing_private_url(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Intern\nCompany: Acme"},
    ).json()
    canonical_url = "https://www.linkedin.com/jobs/view/1234567890"
    private_url = (
        "https://www.linkedin.com/jobs/collections/recommended/"
        "?currentJobId=1234567890&trackingId=private-capture-token"
    )
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = canonical_url
        stored.source = {**stored.source, "source_url": canonical_url}

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": private_url,
            "page_title": "LinkedIn",
            "visible_text": "This job has been removed or the job ID is not valid.",
        },
    )

    assert queued.status_code == 202
    assert queued.json()["source_url"] == canonical_url
    assert queued.json()["job_id"] is None
    assert "private-capture-token" not in queued.text
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued.json()["capture_id"])
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        assert snapshot.source_url == private_url
        assert run.public_source_url == canonical_url

    processed = api_main.process_page_capture_run(
        queued.json()["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["result"]["status"] == "terminal_status_updated"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "closed"


def test_capture_deduplication_includes_source_metadata_and_processing_options(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    base = {
        "page_title": "Same visible page",
        "visible_text": "Same page body\nCompany: Acme",
        "target_job_id": job["id"],
    }

    first = client.post(
        "/api/extension/page-captures",
        json={**base, "source_url": "https://jobs.example.com/openings/one"},
    ).json()
    exact_duplicate = client.post(
        "/api/extension/page-captures",
        json={**base, "source_url": "https://jobs.example.com/openings/one"},
    ).json()
    different_url = client.post(
        "/api/extension/page-captures",
        json={**base, "source_url": "https://jobs.example.com/openings/two"},
    ).json()
    different_option = client.post(
        "/api/extension/page-captures",
        json={
            **base,
            "source_url": "https://jobs.example.com/openings/two",
            "skip_language_filter": True,
        },
    ).json()

    assert exact_duplicate["capture_id"] == first["capture_id"]
    assert different_url["capture_id"] != first["capture_id"]
    assert different_option["capture_id"] != different_url["capture_id"]
    assert client.get(first["status_url"]).json()["status"] == "superseded"
    assert client.get(different_url["status_url"]).json()["status"] == "superseded"
    assert client.get(different_option["status_url"]).json()["status"] == "queued"


def test_enqueue_compensates_private_artifacts_when_owned_commit_fails(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    request = _capture_request()
    before = _artifact_files(settings.artifact_root)

    with session_scope(settings) as db:
        original_commit = db.commit
        commit_calls = 0

        def fail_first_commit():
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                raise RuntimeError("forced outer commit failure")
            return original_commit()

        monkeypatch.setattr(db, "commit", fail_first_commit)
        with pytest.raises(RuntimeError, match="forced outer commit failure"):
            capture_queue.enqueue_page_capture(
                request,
                request.visible_text,
                db,
                client.app.state.storage,
            )

    assert _artifact_files(settings.artifact_root) == before
    with session_scope(settings) as db:
        assert list(db.execute(select(PageCaptureRun)).scalars()) == []
        assert list(db.execute(select(StoredArtifact)).scalars()) == []


def test_enqueue_does_not_delete_artifacts_after_commit_becomes_durable(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    request = _capture_request()
    before = _artifact_files(settings.artifact_root)

    with session_scope(settings) as db:

        def fail_post_commit_refresh(*_args, **_kwargs):
            raise RuntimeError("forced post-commit refresh failure")

        monkeypatch.setattr(db, "refresh", fail_post_commit_refresh)
        with pytest.raises(RuntimeError, match="forced post-commit refresh failure"):
            capture_queue.enqueue_page_capture(
                request,
                request.visible_text,
                db,
                client.app.state.storage,
            )

    with session_scope(settings) as db:
        runs = list(db.execute(select(PageCaptureRun)).scalars())
        artifacts = list(db.execute(select(StoredArtifact)).scalars())
        assert len(runs) == 1
        assert len(artifacts) == 2
    assert len(_artifact_files(settings.artifact_root) - before) == 2


def test_manual_capture_replay_is_available_after_non_retryable_failure(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()
    with session_scope(settings) as db:
        failed = db.get(PageCaptureRun, queued["capture_id"])
        failed.status = "failed"
        failed.retryable = False
        failed.error_code = "deterministic_parser_error"
        failed.error_message = "Captured page processing failed"
        failed.finished_at = datetime.now(UTC)

    retried = client.post(f"{queued['status_url']}/retry")

    assert retried.status_code == 202
    assert retried.json()["capture_id"] != queued["capture_id"]
    assert retried.json()["retry_of_capture_id"] == queued["capture_id"]


def test_capture_status_projection_never_exposes_language_evidence(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    private_phrase = "French required for Project Nightingale customer interviews"
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/private-language-evidence?id=42",
            "page_title": "Private posting",
            "visible_text": f"Backend Intern\nCompany: Acme\n{private_phrase}.",
        },
    ).json()

    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )
    detail_body = client.get(queued["status_url"]).text
    list_body = client.get("/api/extension/page-captures").text

    assert processed["status"] == "skipped"
    assert processed["result"] == {
        "status": "skipped_by_language_filter",
        "job_id": None,
        "reason": "unmet_required_language",
    }
    assert private_phrase not in detail_body
    assert private_phrase not in list_body
    assert '"evidence":' not in detail_body


def test_claim_tokens_change_for_every_attempt_even_when_worker_id_is_reused(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    capture_id = client.post(
        "/api/extension/page-captures",
        json=_capture_request().model_dump(),
    ).json()["capture_id"]
    with session_scope(settings) as db:
        first = capture_queue.claim_page_capture_run(db, capture_id, worker_id="worker_reused")
        first_token = first.claim_token
        first_attempt = first.attempts
    assert first_token
    assert (
        capture_queue.recover_stale_page_capture_runs(
            settings,
            active_worker_id="worker_replacement",
            recover_orphaned=True,
        )
        == 1
    )
    with session_scope(settings) as db:
        second = capture_queue.claim_page_capture_run(db, capture_id, worker_id="worker_reused")
        second_token = second.claim_token

    assert second_token
    assert second_token != first_token
    stale_result = capture_queue.record_page_capture_failure(
        capture_id,
        settings,
        TimeoutError("stale attempt"),
        retryable=True,
        worker_id="worker_reused",
        expected_attempt=first_attempt,
        expected_claim_token=first_token,
    )
    assert stale_result["status"] == "running"
    with session_scope(settings) as db:
        current = db.get(PageCaptureRun, capture_id)
        assert current.claim_token == second_token


def test_reprocess_claim_token_fences_a_reused_worker_id(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        run, _ = reprocess_queue.enqueue_job_reprocess(db.get(JobPosting, job["id"]), db)
        run_id = run.reprocess_run_id
        first = reprocess_queue.claim_job_reprocess_run(db, run_id, worker_id="worker_reused")
        first_token = first.claim_token
        first_attempt = first.attempts
    assert (
        reprocess_queue.recover_stale_job_reprocess_runs(
            settings,
            active_worker_id="worker_replacement",
            recover_orphaned=True,
        )
        == 1
    )
    with session_scope(settings) as db:
        second = reprocess_queue.claim_job_reprocess_run(db, run_id, worker_id="worker_reused")
        second_token = second.claim_token

    assert first_token
    assert second_token
    assert second_token != first_token
    stale_result = reprocess_queue.record_job_reprocess_failure(
        run_id,
        settings,
        TimeoutError("stale attempt"),
        retryable=True,
        worker_id="worker_reused",
        expected_attempt=first_attempt,
        expected_claim_token=first_token,
    )
    assert stale_result["status"] == "running"
    with session_scope(settings) as db:
        assert db.get(JobReprocessRun, run_id).claim_token == second_token


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        ("queued", "running"),
        ("queued", "superseded"),
        ("retry_scheduled", "running"),
        ("running", "retry_scheduled"),
        ("running", "succeeded"),
        ("running", "skipped"),
        ("running", "failed"),
    ],
)
def test_capture_state_machine_accepts_only_declared_transitions(current, next_status):
    capture_queue.ensure_page_capture_transition(current, next_status)


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        ("succeeded", "running"),
        ("failed", "retry_scheduled"),
        ("superseded", "running"),
        ("queued", "succeeded"),
    ],
)
def test_capture_state_machine_rejects_implicit_transitions(current, next_status):
    with pytest.raises(RuntimeError, match="Invalid page capture transition"):
        capture_queue.ensure_page_capture_transition(current, next_status)


def test_job_merge_chooses_newest_snapshot_not_newest_retry_run(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    canonical = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Canonical\nCompany: Acme"},
    ).json()
    duplicate = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Duplicate\nCompany: Acme"},
    ).json()
    older = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/older-merge-evidence",
            "page_title": "Older evidence",
            "visible_text": "Older\nCompany: Acme",
            "target_job_id": duplicate["id"],
        },
    ).json()
    with session_scope(settings) as db:
        older_run = db.get(PageCaptureRun, older["capture_id"])
        older_run.status = "failed"
        older_run.retryable = True

    newer = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/newer-merge-evidence",
            "page_title": "Newer evidence",
            "visible_text": "Newer\nCompany: Acme",
            "target_job_id": canonical["id"],
        },
    ).json()
    replay = client.post(older["retry_url"]).json()
    assert replay["status"] == "queued"

    with session_scope(settings) as db:
        merge_jobs_for_authoritative_identity(
            db.get(JobPosting, canonical["id"]),
            {duplicate["id"]},
            db,
            provider="linkedin",
            identity_hash="snapshot_order_merge_identity",
            identity_strength="authoritative",
        )

    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, replay["capture_id"]).status == "superseded"
        assert db.get(PageCaptureRun, newer["capture_id"]).status == "queued"


def test_newer_snapshot_fences_running_capture_before_job_mutation(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Original\nCompany: Original Co"},
    ).json()
    source_url = "https://jobs.example.com/publication-fence"
    older = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Older | Stale Co",
            "visible_text": "Older\nCompany: Stale Co",
            "target_job_id": job["id"],
        },
    ).json()
    original_parser = api_main.parse_job_text_with_settings
    queued_newer = None

    def enqueue_newer_during_parse(raw_text, parsed_source_url, *args, **kwargs):
        nonlocal queued_newer
        queued_newer = client.post(
            "/api/extension/page-captures",
            json={
                "source_url": source_url,
                "page_title": "Newer | Fresh Co",
                "visible_text": "Newer\nCompany: Fresh Co",
                "target_job_id": job["id"],
            },
        ).json()
        return original_parser(raw_text, parsed_source_url, *args, **kwargs)

    monkeypatch.setattr(
        api_main, "parse_job_text_with_settings", enqueue_newer_during_parse
    )

    processed = api_main.process_page_capture_run(
        older["capture_id"], settings, client.app.state.storage
    )

    assert queued_newer is not None
    assert processed["status"] == "superseded"
    assert processed["error_code"] == "stale_capture_evidence"
    with session_scope(settings) as db:
        assert db.get(JobPosting, job["id"]).company_name == "Original Co"
