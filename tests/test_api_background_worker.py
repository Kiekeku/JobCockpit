from __future__ import annotations

import sqlite3
import time
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from jobcockpit.api import background_worker_leases
from jobcockpit.services import workflows as api_main
from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    acquire_background_worker_lease,
    release_background_worker_lease,
)
from jobcockpit.api.main import create_app
from jobcockpit.db.session import init_database


def _sqlite_locked_error() -> OperationalError:
    return OperationalError(
        "DELETE FROM background_worker_leases",
        {},
        sqlite3.OperationalError("database is locked"),
    )


def test_background_worker_lease_guard_defers_transient_locked_acquisition(
    monkeypatch,
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory()

    def locked_acquisition(*_args, **_kwargs):
        raise _sqlite_locked_error()

    monkeypatch.setattr(
        background_worker_leases,
        "acquire_background_worker_lease",
        locked_acquisition,
    )

    with BackgroundWorkerLeaseGuard(settings, "locked_worker") as lease:
        assert lease.acquired is False
        assert lease.lease_token is None


def test_background_worker_lease_guard_does_not_raise_when_locked_during_release(
    monkeypatch,
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory()
    monkeypatch.setattr(
        background_worker_leases,
        "acquire_background_worker_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        background_worker_leases,
        "current_background_worker_lease_token",
        lambda *_args, **_kwargs: "lease_locked_release",
    )

    def locked_release(*_args, **_kwargs):
        raise _sqlite_locked_error()

    monkeypatch.setattr(
        background_worker_leases,
        "release_background_worker_lease",
        locked_release,
    )

    guard = BackgroundWorkerLeaseGuard(settings, "locked_worker")
    with guard as lease:
        assert lease.acquired is True

    assert guard.acquired is False
    assert guard.lost is True


def test_api_lifespan_worker_processes_capture(jobcockpit_settings_factory):
    settings = jobcockpit_settings_factory(
        background_worker_poll_seconds=0.1,
        background_worker_shutdown_timeout_seconds=2,
    )
    init_database(settings)

    with TestClient(create_app(settings, start_background_workers=True)) as client:
        queued = client.post(
            "/api/extension/page-captures",
            json={
                "source_url": "https://jobs.example.com/api-worker-intern",
                "page_title": "API Worker Intern | Acme",
                "visible_text": "API Worker Intern\nCompany: Acme\nPython",
            },
        ).json()
        deadline = time.monotonic() + 3
        status = queued
        while status["status"] in {"queued", "running", "retry_scheduled"}:
            assert time.monotonic() < deadline
            time.sleep(0.05)
            status = client.get(queued["status_url"]).json()

    assert status["status"] == "succeeded"
    assert status["job_id"].startswith("job_")


def test_cover_letter_worker_does_not_block_api_background_queue(
    monkeypatch,
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory(
        background_worker_poll_seconds=0.1,
        background_worker_shutdown_timeout_seconds=2,
    )
    init_database(settings)
    cover_worker_started = Event()
    release_cover_worker = Event()

    def blocking_cover_iteration(*_args, **_kwargs):
        cover_worker_started.set()
        release_cover_worker.wait(2)
        return None

    monkeypatch.setattr(
        api_main,
        "process_next_cover_letter_generation",
        blocking_cover_iteration,
    )
    try:
        with TestClient(create_app(settings, start_background_workers=True)) as client:
            assert cover_worker_started.wait(1)
            queued = client.post(
                "/api/extension/page-captures",
                json={
                    "source_url": "https://jobs.example.com/nonblocking-intern",
                    "page_title": "Nonblocking Intern | Acme",
                    "visible_text": "Nonblocking Intern\nCompany: Acme\nPython",
                },
            ).json()
            deadline = time.monotonic() + 3
            status = queued
            while status["status"] in {"queued", "running", "retry_scheduled"}:
                assert time.monotonic() < deadline
                time.sleep(0.05)
                status = client.get(queued["status_url"]).json()
            assert status["status"] == "succeeded"
            release_cover_worker.set()
    finally:
        release_cover_worker.set()


def test_background_worker_prioritizes_capture_over_job_reprocess(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Old Role\nCompany: Old Co"},
    ).json()
    client.post(f"/api/jobs/{job['id']}/reprocess")
    capture = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/priority-intern",
            "page_title": "Priority Intern | Acme",
            "visible_text": "Priority Intern\nCompany: Acme\nPython",
        },
    ).json()

    processed = api_main.process_next_background_job(settings, client.app.state.storage)

    assert processed["kind"] == "page_capture"
    assert processed["run"]["capture_id"] == capture["capture_id"]
    assert processed["run"]["status"] == "succeeded"


def test_background_worker_can_force_fair_reprocess_after_capture_streak(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Old Role\nCompany: Old Co"},
    ).json()
    client.post(f"/api/jobs/{job['id']}/reprocess")
    client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/still-queued",
            "page_title": "Still queued",
            "visible_text": "Intern\nCompany: Acme",
        },
    )

    processed = api_main.process_next_background_job(
        settings,
        client.app.state.storage,
        prefer_reprocess=True,
    )

    assert processed["kind"] == "job_reprocess"


def test_supervisor_survives_initial_stale_recovery_failure(
    monkeypatch,
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory(
        background_worker_poll_seconds=0.1,
        background_worker_shutdown_timeout_seconds=2,
    )
    init_database(settings)
    original_recovery = api_main.recover_stale_page_capture_runs
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic recovery failure")
        return original_recovery(*args, **kwargs)

    monkeypatch.setattr(api_main, "recover_stale_page_capture_runs", fail_once)
    with TestClient(create_app(settings, start_background_workers=True)) as client:
        queued = client.post(
            "/api/extension/page-captures",
            json={
                "source_url": "https://jobs.example.com/recovery-survivor",
                "page_title": "Recovery survivor",
                "visible_text": "Intern\nCompany: Acme",
            },
        ).json()
        deadline = time.monotonic() + 3
        status = queued
        while status["status"] in {"queued", "running", "retry_scheduled"}:
            assert time.monotonic() < deadline
            time.sleep(0.05)
            status = client.get(queued["status_url"]).json()

    assert calls >= 1
    assert status["status"] == "succeeded"


def test_worker_shutdown_relinquishes_lease_while_provider_call_unwinds(
    monkeypatch,
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory(
        background_worker_poll_seconds=0.1,
        background_worker_shutdown_timeout_seconds=0.01,
    )
    init_database(settings)
    entered = Event()
    release_provider = Event()

    def blocking_iteration(*_args, **_kwargs):
        entered.set()
        release_provider.wait(2)
        return None

    monkeypatch.setattr(api_main, "process_next_background_job", blocking_iteration)
    worker = api_main.ApiBackgroundWorker(
        settings,
        api_main.ArtifactStorageService(settings.artifact_root),
    )
    recovery_called = Event()

    def record_unexpected_recovery(*_args, **_kwargs):
        recovery_called.set()
        return 0, 0

    monkeypatch.setattr(worker, "_recover_stale_work", record_unexpected_recovery)
    worker.start()
    assert entered.wait(2)

    try:
        worker.stop()
        assert acquire_background_worker_lease(settings, "replacement_worker") is True
    finally:
        release_background_worker_lease(settings, "replacement_worker")
        release_provider.set()
        worker.thread.join(2)

    assert not worker.thread.is_alive()
    assert not recovery_called.is_set()
