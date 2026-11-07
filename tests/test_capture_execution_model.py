from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from jobcockpit.api import page_capture_runs as capture_queue
from jobcockpit.db.models import (
    JobPosting,
    PageCaptureRun,
    PageCaptureSnapshotScope,
    PageCaptureSnapshot,
    PageCaptureSubjectHead,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.browser_capture_adapters import CaptureSourceIdentity


def _payload(source_url: str, *, body: str = "Intern\nCompany: Acme") -> dict:
    return {
        "source_url": source_url,
        "page_title": "Intern | Acme",
        "visible_text": body,
    }


def test_enqueue_separates_private_snapshot_from_public_run_state(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    secret = "private-signed-capture-token"
    response = client.post(
        "/api/extension/page-captures",
        json=_payload(f"https://jobs.example.com/opening?jobCode=ABC&signature={secret}"),
    )

    assert response.status_code == 202
    assert secret not in response.text
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, response.json()["capture_id"])
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        assert snapshot.source_url.endswith(f"signature={secret}")
        assert run.public_source_url == "https://jobs.example.com/opening"
        assert not hasattr(run, "source_url")
        assert list(
            db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == run.snapshot_id
                )
            ).scalars()
        )


def test_unknown_provider_identity_keeps_distinct_private_query_values(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    first = client.post(
        "/api/extension/page-captures",
        json=_payload("https://jobs.example.com/opening?jobCode=A"),
    ).json()
    second = client.post(
        "/api/extension/page-captures",
        json=_payload("https://jobs.example.com/opening?jobCode=B"),
    ).json()

    assert first["capture_id"] != second["capture_id"]
    with session_scope(settings) as db:
        first_run = db.get(PageCaptureRun, first["capture_id"])
        second_run = db.get(PageCaptureRun, second["capture_id"])
        first_scopes = {
            row.scope_hash
            for row in db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == first_run.snapshot_id
                )
            ).scalars()
        }
        second_scopes = {
            row.scope_hash
            for row in db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == second_run.snapshot_id
                )
            ).scalars()
        }
    assert first_scopes.isdisjoint(second_scopes)


def test_enqueue_has_one_pending_head_per_subject_and_allows_running_plus_pending(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = _payload("https://www.linkedin.com/jobs/view/1234567890")

    first = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": "First\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        assert capture_queue.claim_page_capture_run(db, first["capture_id"], worker_id="worker_one")

    second = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": "Second\nCompany: Acme"},
    ).json()
    third = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": "Third\nCompany: Acme"},
    ).json()

    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, first["capture_id"]).status == "running"
        assert db.get(PageCaptureRun, second["capture_id"]).status == "superseded"
        assert db.get(PageCaptureRun, third["capture_id"]).status == "queued"
        heads = list(db.execute(select(PageCaptureSubjectHead)).scalars())
        assert heads
        third_run = db.get(PageCaptureRun, third["capture_id"])
        assert {head.snapshot_id for head in heads} == {third_run.snapshot_id}


def test_targetless_enqueue_does_not_scan_or_associate_jobs_before_worker(
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
    source_url = "https://www.linkedin.com/jobs/view/9988776655"
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = source_url
        stored.source = {**stored.source, "source_url": source_url}

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("enqueue must not perform job matching")

    monkeypatch.setattr("jobcockpit.api.page_capture_runs.resolve_job_by_source_identity", fail_if_called)
    queued = client.post(
        "/api/extension/page-captures",
        json=_payload(source_url),
    )

    assert queued.status_code == 202
    assert queued.json()["job_id"] is None


def test_manual_retry_reuses_immutable_snapshot(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json=_payload("https://jobs.example.com/opening?jobCode=ABC"),
    ).json()
    with session_scope(settings) as db:
        original = db.get(PageCaptureRun, queued["capture_id"])
        original.status = "failed"
        original.retryable = True
        snapshot_id = original.snapshot_id

    retried = client.post(f"{queued['status_url']}/retry")

    assert retried.status_code == 202
    with session_scope(settings) as db:
        retry_run = db.get(PageCaptureRun, retried.json()["capture_id"])
        assert retry_run.snapshot_id == snapshot_id
        assert retry_run.retry_of_capture_id == queued["capture_id"]


def test_manual_retry_preserves_original_identity_when_resolver_changes(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json=_payload("https://jobs.example.com/opening?jobCode=ABC"),
    ).json()
    with session_scope(settings) as db:
        original = db.get(PageCaptureRun, queued["capture_id"])
        original.status = "failed"
        original.retryable = True
        original_identity = (
            original.provider,
            original.identity_hash,
            original.identity_strength,
            original.public_source_url,
        )
        snapshot_id = original.snapshot_id
        original_scopes = {
            row.scope_hash
            for row in db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == snapshot_id
                )
            ).scalars()
        }

    monkeypatch.setattr(
        capture_queue,
        "capture_source_identity",
        lambda _source_url: CaptureSourceIdentity(
            provider="changed_provider",
            identity_hash="f" * 64,
            public_source_url="https://changed.example/jobs/ABC",
            canonical_source_url="https://changed.example/jobs/ABC",
            strength="authoritative",
            source_url_dedup_safe=True,
        ),
    )

    retried = client.post(f"{queued['status_url']}/retry")

    assert retried.status_code == 202
    with session_scope(settings) as db:
        retry_run = db.get(PageCaptureRun, retried.json()["capture_id"])
        retry_scopes = {
            row.scope_hash
            for row in db.execute(
                select(PageCaptureSnapshotScope).where(
                    PageCaptureSnapshotScope.snapshot_id == retry_run.snapshot_id
                )
            ).scalars()
        }
        assert retry_run.snapshot_id == snapshot_id
        assert (
            retry_run.provider,
            retry_run.identity_hash,
            retry_run.identity_strength,
            retry_run.public_source_url,
        ) == original_identity
        assert retry_scopes == original_scopes


def test_snapshot_sequence_is_the_only_evidence_freshness_order(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = _payload("https://www.linkedin.com/jobs/view/1234567890")

    first = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": "First\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        original = db.get(PageCaptureRun, first["capture_id"])
        first_snapshot = db.get(PageCaptureSnapshot, original.snapshot_id)
        first_snapshot_id = first_snapshot.snapshot_id
        first_evidence_seq = first_snapshot.evidence_seq
        first_created_at = first_snapshot.created_at
        original.status = "failed"
        original.retryable = True

    second = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": "Second\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        second_run = db.get(PageCaptureRun, second["capture_id"])
        second_snapshot = db.get(PageCaptureSnapshot, second_run.snapshot_id)
        second_snapshot.created_at = first_created_at
        assert second_snapshot.evidence_seq > first_evidence_seq

    replay = client.post(first["retry_url"]).json()

    assert replay["status"] == "superseded"
    with session_scope(settings) as db:
        replay_run = db.get(PageCaptureRun, replay["capture_id"])
        assert replay_run.snapshot_id == first_snapshot_id
        assert (
            db.get(PageCaptureSnapshot, replay_run.snapshot_id).evidence_seq == first_evidence_seq
        )


def test_subject_head_points_to_snapshot_and_survives_run_claim(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json=_payload("https://www.linkedin.com/jobs/view/1234567890"),
    ).json()

    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        snapshot_id = run.snapshot_id
        assert capture_queue.claim_page_capture_run(
            db, queued["capture_id"], worker_id="head_test_worker"
        )

    with session_scope(settings) as db:
        heads = list(db.execute(select(PageCaptureSubjectHead)).scalars())
        assert heads
        assert {head.snapshot_id for head in heads} == {snapshot_id}


def test_async_capture_migrations_are_collapsed_to_one_revision():
    versions = Path("apps/api/alembic/versions")
    async_revisions = sorted(versions.glob("20260803_00*_capture*.py"))

    assert [path.name for path in async_revisions] == ["20260803_0021_async_capture_execution.py"]
    migration = async_revisions[0].read_text(encoding="utf-8")
    assert 'revision = "20260803_0021"' in migration
    assert 'down_revision = "20260803_0017"' in migration
