from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from jobcockpit.api.page_capture_runs import claim_page_capture_run, enqueue_page_capture
from jobcockpit.db.models import PageCaptureRun, PageCaptureSubjectHead
from jobcockpit.db.session import session_scope
from jobcockpit.schemas.requests import ExtensionPageCaptureRequest


def test_identical_active_capture_is_deduplicated_and_newer_content_supersedes_pending(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = {
        "source_url": "https://jobs.example.com/deduplicated-intern",
        "page_title": "Intern | Acme",
        "visible_text": "Intern\nCompany: Acme\nPython",
    }

    first = client.post("/api/extension/page-captures", json=payload).json()
    duplicate = client.post("/api/extension/page-captures", json=payload).json()
    newer = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": f"{payload['visible_text']}\nSQL"},
    ).json()

    assert duplicate["capture_id"] == first["capture_id"]
    assert duplicate["queued"] is False
    assert newer["capture_id"] != first["capture_id"]
    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, first["capture_id"]).status == "superseded"
        assert db.get(PageCaptureRun, newer["capture_id"]).status == "queued"


def test_newer_capture_does_not_supersede_running_capture(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = {
        "source_url": "https://jobs.example.com/running-intern",
        "page_title": "Intern | Acme",
        "visible_text": "Intern\nCompany: Acme\nPython",
    }
    first = client.post("/api/extension/page-captures", json=payload).json()
    with session_scope(settings) as db:
        assert claim_page_capture_run(db, first["capture_id"]).status == "running"

    newer = client.post(
        "/api/extension/page-captures",
        json={**payload, "visible_text": f"{payload['visible_text']}\nSQL"},
    ).json()

    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, first["capture_id"]).status == "running"
        assert db.get(PageCaptureRun, newer["capture_id"]).status == "queued"


def test_concurrent_different_captures_leave_exactly_one_pending_head(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    storage = client.app.state.storage

    def enqueue(body: str) -> str:
        request = ExtensionPageCaptureRequest(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            page_title="Intern | Acme",
            visible_text=body,
        )
        with session_scope(settings) as db:
            run, _queued = enqueue_page_capture(
                request, request.visible_text, db, storage
            )
            return run.capture_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_ids = set(
            executor.map(
                enqueue,
                ("First\nCompany: Acme", "Second\nCompany: Acme"),
            )
        )

    assert len(capture_ids) == 2
    with session_scope(settings) as db:
        runs = [db.get(PageCaptureRun, capture_id) for capture_id in capture_ids]
        assert sorted(run.status for run in runs) == ["queued", "superseded"]
        heads = list(db.execute(select(PageCaptureSubjectHead)).scalars())
        assert len(heads) == 1
        assert heads[0].snapshot_id == next(
            run.snapshot_id for run in runs if run.status == "queued"
        )


def test_concurrent_identical_captures_reuse_one_run(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    storage = client.app.state.storage
    request = ExtensionPageCaptureRequest(
        source_url="https://www.linkedin.com/jobs/view/9988776655",
        page_title="Intern | Acme",
        visible_text="Intern\nCompany: Acme",
    )

    def enqueue() -> str:
        with session_scope(settings) as db:
            run, _queued = enqueue_page_capture(
                request, request.visible_text, db, storage
            )
            return run.capture_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_ids = list(executor.map(lambda _index: enqueue(), range(2)))

    assert capture_ids[0] == capture_ids[1]
