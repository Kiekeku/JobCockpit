from jobcockpit.db.models import CoverLetterGenerationRun
from jobcockpit.db.session import session_scope

import pytest


@pytest.mark.parametrize("required", [False, True])
def test_snapshot_replacement_preserves_manual_resume_and_package(
    jobcockpit_settings_factory, jobcockpit_client_factory, required,
):
    from sqlalchemy import select
    from jobcockpit.db.models import JobPosting
    from jobcockpit.services.workflows import reconcile_job_cover_letter_generation
    from test_architecture_performance import seed_jobs
    from test_cover_letter_queue import _pdf_bytes, _profile_payload

    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    seed_jobs(settings, 1)
    selected = client.post("/api/profile/resume", files={
        "file": ("selected.pdf", _pdf_bytes("Selected Resume"), "application/pdf"),
    }).json()["artifact"]["artifact_id"]
    application = client.post("/api/jobs/job-0/applications").json()
    package = client.post(f"/api/applications/{application['application_id']}/packages").json()
    package_id = package["application_package_id"]
    queued = client.post(f"/api/application-packages/{package_id}/cover-letter-draft", json={
        "request_id": "manual-original", "resume_artifact_id": selected,
    }).json()["run"]
    client.post("/api/profile/resume", files={
        "file": ("latest.pdf", _pdf_bytes("Latest Resume"), "application/pdf"),
    })
    with session_scope(settings) as db:
        job = db.get(JobPosting, "job-0")
        job.posting_snapshot_hash = "refreshed"
        job.application = {"documents_required": ["cover_letter"] if required else ["resume"]}
        replacement, created = reconcile_job_cover_letter_generation(job, db)
        assert created
        assert replacement.request_payload["resume_artifact_id"] == selected
        assert replacement.request_payload["resume_sha256"] == queued["request"]["resume_sha256"]
        assert replacement.request_payload["target_package_id"] == package_id
        assert replacement.request_payload["request_id"] != "manual-original"
        run_id = replacement.generation_run_id
        assert db.get(CoverLetterGenerationRun, queued["generation_run_id"]).status == "cancelled"
    from jobcockpit.services.workflows import process_next_cover_letter_generation
    process_next_cover_letter_generation(settings, client.app.state.storage)
    result = client.get(f"/api/cover-letter-generation-runs/{run_id}/result")
    assert result.status_code == 200
    assert result.json()["package"]["application_package_id"] == package_id
    assert result.json()["package"]["approved_for_extension"] is False
    with session_scope(settings) as db:
        assert len(list(db.scalars(select(CoverLetterGenerationRun)))) == 2


def test_manual_generation_returns_durable_request_without_generating(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    monkeypatch,
):
    from jobcockpit.services import workflows as api_main

    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post(
        "/api/profile",
        json={"first_name": "Jane", "last_name": "Candidate", "email": "jane@example.test"},
    )
    job = client.post(
        "/api/jobs/ingest", json={"source_type": "paste", "raw_text": "Engineer\nCompany: Example"}
    ).json()

    def unexpected(**kwargs):
        raise AssertionError("Request handler must not generate documents")

    monkeypatch.setattr(api_main, "prepare_cover_letter_document", unexpected)
    response = client.post(
        f"/api/jobs/{job['id']}/cover-letter-draft", json={"request_id": "manual-request-1"}
    )
    assert response.status_code == 202
    run = response.json()["run"]
    assert run["status"] == "queued"
    again = client.post(
        f"/api/jobs/{job['id']}/cover-letter-draft", json={"request_id": "manual-request-1"}
    )
    assert again.json()["run"]["generation_run_id"] == run["generation_run_id"]
    with session_scope(settings) as db:
        saved = db.get(CoverLetterGenerationRun, run["generation_run_id"])
        assert saved.request_payload["request_id"] == "manual-request-1"
        assert saved.request_payload["requested_by_user"] is True


def test_manual_request_rejects_unknown_resume_before_queueing(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post(
        "/api/profile",
        json={"first_name": "Jane", "last_name": "Candidate", "email": "jane@example.test"},
    )
    job = client.post(
        "/api/jobs/ingest", json={"source_type": "paste", "raw_text": "Engineer\nCompany: Example"}
    ).json()
    response = client.post(
        f"/api/jobs/{job['id']}/cover-letter-draft", json={"resume_artifact_id": "missing"}
    )
    assert response.status_code in {400, 404}


def test_completed_request_is_idempotent_and_new_request_creates_unapproved_revision(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    from conftest import finish_generation

    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post(
        "/api/profile",
        json={"first_name": "Jane", "last_name": "Candidate", "email": "jane@example.test"},
    )
    job = client.post(
        "/api/jobs/ingest", json={"source_type": "paste", "raw_text": "Engineer\nCompany: Example"}
    ).json()
    url = f"/api/jobs/{job['id']}/cover-letter-draft"
    queued = client.post(url, json={"request_id": "revision-1"})
    first = finish_generation(client, queued)
    assert first.status_code == 200
    assert first.json()["package"]["approved_for_extension"] is False
    again = client.post(url, json={"request_id": "revision-1"})
    assert again.status_code == 202
    assert again.json()["run"]["generation_run_id"] == queued.json()["run"]["generation_run_id"]
    second = finish_generation(client, client.post(url, json={"request_id": "revision-2"}))
    assert second.status_code == 200
    assert second.json()["package"]["package_version"] == 2
    assert second.json()["package"]["approved_for_extension"] is False
