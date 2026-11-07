from __future__ import annotations

from conftest import finish_generation

from datetime import UTC, datetime
from io import BytesIO

from docx import Document
from sqlalchemy import delete, select

from jobcockpit.services import workflows as api_main
from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
from jobcockpit.api.cover_letter_generation_runs import (
    COVER_LETTER_BACKGROUND_WORKER_LEASE,
    CoverLetterGenerationProcessingError,
    bootstrap_cover_letter_generation_worker,
    claim_next_cover_letter_generation,
    enqueue_cover_letter_generation,
    record_cover_letter_generation_failure,
)
from jobcockpit.db.models import (
    Application,
    ApplicationPackage,
    ApplicationPackageDocument,
    CoverLetterGenerationRun,
    GeneratedDocument,
    JobPosting,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.status_reconciliation import (
    StatusCheckDecision,
    run_status_reconciliation,
)


def _profile_payload() -> dict:
    return {
        "first_name": "Jane",
        "last_name": "Candidate",
        "email": "jane.candidate@example.test",
        "phone": "",
        "links": [],
        "summary": "Backend engineer focused on local-first systems.",
        "skills": ["Python", "FastAPI"],
    }


def _required_cover_letter_posting() -> dict:
    return {
        "source_type": "paste",
        "raw_text": (
            "Backend Developer\n"
            "Company: Example Corp\n"
            "Please submit your resume and a cover letter."
        ),
    }


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT\n/F1 18 Tf\n72 720 Td\n({text}) Tj\nET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        ),
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_offset = sum(len(part) for part in parts)
    parts.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    parts.append(("0" * 10 + " 65535 f \n").encode("ascii"))
    for offset in offsets[1:]:
        parts.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    parts.append(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("ascii")
    )
    return b"".join(parts)


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_ingestion_queues_one_required_cover_letter_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    duplicate = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    assert duplicate["id"] == job["id"]
    with session_scope(settings) as db:
        runs = list(db.execute(select(CoverLetterGenerationRun)).scalars())
        assert len(runs) == 1
        assert runs[0].job_id == job["id"]
        assert runs[0].posting_snapshot_hash == db.get(JobPosting, job["id"]).posting_snapshot_hash
        assert runs[0].status == "queued"
        generation_run_id = runs[0].generation_run_id
    status = client.get(f"/api/jobs/{job['id']}/cover-letter-generation").json()
    assert status["run"]["generation_run_id"] == generation_run_id


def test_duplicate_ingestion_repairs_missing_required_cover_letter_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        db.execute(delete(CoverLetterGenerationRun))

    duplicate = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    assert duplicate["id"] == job["id"]
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.job_id == job["id"]
        assert run.status == "queued"


def test_duplicate_ingestion_does_not_reset_failed_snapshot_retry_budget(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        run.status = "failed"
        run.attempts = 3
        run.finished_at = datetime.now(UTC)
        run.error_code = "cover_letter_generation_rejected"

    duplicate = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    assert duplicate["id"] == job["id"]
    with session_scope(settings) as db:
        runs = list(db.execute(select(CoverLetterGenerationRun)).scalars())
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].attempts == 3


def test_ingestion_does_not_queue_optional_cover_letter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nA cover letter is optional and is not required.",
        },
    )

    with session_scope(settings) as db:
        assert db.execute(select(CoverLetterGenerationRun)).scalar_one_or_none() is None


def test_explicit_user_request_queues_and_generates_optional_cover_letter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nA cover letter is optional and is not required.",
        },
    ).json()

    with session_scope(settings) as db:
        run, queued = enqueue_cover_letter_generation(
            db.get(JobPosting, job["id"]),
            db,
            requested_by_user=True,
        )
        assert queued is True
        assert run.status == "queued"
        assert run.result == {"requested_by_user": True}

    processed = api_main.process_next_cover_letter_generation(
        settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["job_id"] == job["id"]


def test_explicit_user_request_retries_failed_current_snapshot(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        failed = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        failed.status = "failed"
        failed.attempts = 3
        failed.finished_at = datetime.now(UTC)
        db.flush()
        run, queued = enqueue_cover_letter_generation(
            db.get(JobPosting, job["id"]),
            db,
            requested_by_user=True,
        )
        assert queued is True
        assert run.generation_run_id != failed.generation_run_id
        assert run.status == "queued"
        assert run.result == {"requested_by_user": True}


def test_reconcile_preserves_explicit_optional_cover_letter_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nA cover letter is optional and is not required.",
        },
    ).json()

    with session_scope(settings) as db:
        job_row = db.get(JobPosting, job["id"])
        queued, was_queued = enqueue_cover_letter_generation(
            job_row,
            db,
            requested_by_user=True,
        )
        reconciled, was_requeued = api_main.reconcile_job_cover_letter_generation(
            job_row,
            db,
        )
        assert was_queued is True
        assert was_requeued is False
        assert reconciled.generation_run_id == queued.generation_run_id
        assert reconciled.status == "queued"


def test_background_worker_generates_unapproved_required_cover_letter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    processed = api_main.process_next_cover_letter_generation(
        settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["job_id"] == job["id"]
    with session_scope(settings) as db:
        application = db.execute(
            select(Application).where(Application.job_id == job["id"])
        ).scalar_one()
        package = db.execute(
            select(ApplicationPackage).where(
                ApplicationPackage.application_id == application.application_id
            )
        ).scalar_one()
        document = db.execute(
            select(GeneratedDocument).where(
                GeneratedDocument.application_package_id == package.application_package_id
            )
        ).scalar_one()
        package_document = db.execute(
            select(ApplicationPackageDocument).where(
                ApplicationPackageDocument.generated_document_id == document.document_id
            )
        ).scalar_one()
        assert package.status == "draft"
        assert package.approved_for_extension is False
        assert document.document_type == "cover_letter"
        assert document.approved_by_user is False
        assert package_document.required is True
        assert package_document.approved_for_upload is False


def test_manual_cover_letter_satisfies_pending_automatic_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    manual = finish_generation(client, client.post(f"/api/jobs/{job['id']}/cover-letter-draft"))

    assert manual.status_code == 200
    payload = manual.json()
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.status == "succeeded"
        assert run.result["application_package_id"] == payload["package"][
            "application_package_id"
        ]
        assert run.result["document_id"] == payload["document"]["document_id"]
        assert len(list(db.execute(select(ApplicationPackage)).scalars())) == 1
        assert len(list(db.execute(select(GeneratedDocument)).scalars())) == 1
    assert (
        api_main.process_next_cover_letter_generation(settings, client.app.state.storage) is None
    )


def test_application_package_creation_satisfies_pending_automatic_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()

    package_response = finish_generation(client, client.post(
        f"/api/applications/{application['application_id']}/packages"
    ))

    assert package_response.status_code == 200
    package = package_response.json()
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        documents = list(db.execute(select(GeneratedDocument)).scalars())
        assert run.status == "succeeded"
        assert run.result["application_package_id"] == package["application_package_id"]
        assert run.result["document_id"] == documents[0].document_id
        assert len(list(db.execute(select(ApplicationPackage)).scalars())) == 1
        assert len(documents) == 1
    assert (
        api_main.process_next_cover_letter_generation(settings, client.app.state.storage) is None
    )


def test_package_cover_letter_draft_satisfies_pending_automatic_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Developer\nPython APIs"},
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = client.post(
        f"/api/applications/{application['application_id']}/packages"
    ).json()
    with session_scope(settings) as db:
        job_row = db.get(JobPosting, job["id"])
        job_row.application = {
            **(job_row.application or {}),
            "cover_letter_required": True,
            "documents_required": ["resume", "cover_letter"],
        }
        job_row.posting_snapshot_hash = "manual-required-snapshot"
        api_main.reconcile_job_cover_letter_generation(job_row, db)

    document_response = finish_generation(client, client.post(
        f"/api/application-packages/{package['application_package_id']}/cover-letter-draft"
    ))

    assert document_response.status_code == 200
    document = document_response.json()
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.status == "succeeded"
        assert run.result["application_package_id"] == package["application_package_id"]
        assert run.result["document_id"] == document["document_id"]
        assert len(list(db.execute(select(ApplicationPackage)).scalars())) == 1
        assert len(list(db.execute(select(GeneratedDocument)).scalars())) == 1
    assert (
        api_main.process_next_cover_letter_generation(settings, client.app.state.storage) is None
    )


def test_manual_cover_letter_resolves_failed_current_snapshot_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        run.status = "failed"
        run.attempts = 3
        run.finished_at = datetime.now(UTC)
        run.error_code = "cover_letter_generation_rejected"

    manual = finish_generation(client, client.post(f"/api/jobs/{job['id']}/cover-letter-draft"))

    assert manual.status_code == 200
    with session_scope(settings) as db:
        runs = list(db.scalars(select(CoverLetterGenerationRun)))
        assert len(runs) == 2
        assert sum(run.status == "failed" for run in runs) == 1
        run = next(run for run in runs if run.status == "succeeded")
        assert run.status == "succeeded"
        assert run.result["document_id"] == manual.json()["document"]["document_id"]


def test_manual_cover_letter_reuses_pending_automatic_work_without_generating(
    monkeypatch, jobcockpit_settings_factory, jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    def unexpected(*args, **kwargs):
        raise AssertionError("HTTP must not compute a competing draft")
    with monkeypatch.context() as patch:
        patch.setattr(api_main, "prepare_cover_letter_document", unexpected)
        queued = client.post(f"/api/jobs/{job['id']}/cover-letter-draft")
        assert queued.status_code == 202
        assert queued.json()["queued"] is False
    completed = finish_generation(client, queued)
    assert completed.status_code == 200
    with session_scope(settings) as db:
        assert len(list(db.scalars(select(ApplicationPackage)))) == 1
        assert len(list(db.scalars(select(GeneratedDocument)))) == 1


def test_manual_cover_letter_rejects_automatic_race_for_selected_resume(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    selected_resume = client.post(
        "/api/profile/resume",
        files={
            "file": (
                "selected.pdf",
                _pdf_bytes("Selected Resume"),
                "application/pdf",
            )
        },
    ).json()
    client.post(
        "/api/profile/resume",
        files={
            "file": (
                "latest.pdf",
                _pdf_bytes("Latest Resume"),
                "application/pdf",
            )
        },
    )
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        db.scalar(select(CoverLetterGenerationRun)).status = "running"
    def unexpected(*args, **kwargs):
        raise AssertionError("A conflicting request must not generate documents")
    monkeypatch.setattr(api_main, "prepare_cover_letter_document", unexpected)

    manual = client.post(
        f"/api/jobs/{job['id']}/cover-letter-draft",
        json={"resume_artifact_id": selected_resume["artifact"]["artifact_id"]},
    )

    assert manual.status_code == 409
    assert "already in progress" in manual.json()["detail"].lower()
    with session_scope(settings) as db:
        assert len(list(db.execute(select(ApplicationPackage)).scalars())) == 0
        assert len(list(db.execute(select(GeneratedDocument)).scalars())) == 0


def test_word_revision_rejects_automatic_completion_race(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    original_prepare = api_main.prepare_cover_letter_word_revision
    automatic_result = None

    def interleaved_prepare(*args, **kwargs):
        nonlocal automatic_result
        prepared = original_prepare(*args, **kwargs)
        if automatic_result is None:
            automatic_result = api_main.process_next_cover_letter_generation(
                settings,
                client.app.state.storage,
            )
        return prepared

    monkeypatch.setattr(api_main, "prepare_cover_letter_word_revision", interleaved_prepare)

    manual = client.post(
        f"/api/jobs/{job['id']}/cover-letter-word-revision",
        files={
            "file": (
                "revision.docx",
                _docx_bytes("Selected manual revision"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert automatic_result["status"] == "succeeded"
    assert manual.status_code == 409
    assert "word revision" in manual.json()["detail"].lower()
    with session_scope(settings) as db:
        assert len(list(db.execute(select(ApplicationPackage)).scalars())) == 1
        assert len(list(db.execute(select(GeneratedDocument)).scalars())) == 1


def test_cover_letter_queue_waits_for_profile_then_wakes_and_generates(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    processed = api_main.process_next_cover_letter_generation(
        settings, client.app.state.storage
    )

    assert processed["status"] == "waiting_for_profile"
    assert processed["error_code"] == "candidate_profile_missing"
    assert processed["attempts"] == 0
    with session_scope(settings) as db:
        assert (
            db.execute(select(Application).where(Application.job_id == job["id"]))
            .scalar_one_or_none()
            is None
        )
    client.post("/api/profile", json=_profile_payload())
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.status == "queued"
        assert run.error_code is None

    processed = api_main.process_next_cover_letter_generation(
        settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"


def test_profile_created_before_missing_profile_failure_is_recorded_requeues_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/jobs/ingest", json=_required_cover_letter_posting())

    with BackgroundWorkerLeaseGuard(
        settings,
        "cover_worker_profile_race",
        lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
    ) as lease:
        assert lease.lease_token is not None
        run_id = claim_next_cover_letter_generation(
            settings,
            worker_id="cover_worker_profile_race",
            supervisor_lease_token=lease.lease_token,
        )
        assert run_id is not None
        with session_scope(settings) as db:
            run = db.get(CoverLetterGenerationRun, run_id)
            expected_attempt = run.attempts
            expected_claim_token = run.claim_token
        assert expected_claim_token is not None

        client.post("/api/profile", json=_profile_payload())
        result = record_cover_letter_generation_failure(
            run_id,
            settings,
            CoverLetterGenerationProcessingError(
                "candidate_profile_missing",
                "Create a candidate profile before generating a cover letter",
                retryable=False,
            ),
            worker_id="cover_worker_profile_race",
            expected_attempt=expected_attempt,
            expected_claim_token=expected_claim_token,
            expected_supervisor_lease_token=lease.lease_token,
            candidate_id="candidate_local",
        )

    assert result["status"] == "queued"
    assert result["attempts"] == 0
    assert result["error_code"] is None
    assert api_main.process_next_cover_letter_generation(
        settings, client.app.state.storage
    )["status"] == "succeeded"


def test_api_background_dispatcher_does_not_process_cover_letter_queue(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    client.post("/api/jobs/ingest", json=_required_cover_letter_posting())
    processed = api_main.process_next_background_job(settings, client.app.state.storage)

    assert processed is None
    with session_scope(settings) as db:
        assert db.execute(select(CoverLetterGenerationRun)).scalar_one().status == "queued"


def test_existing_job_refresh_queues_newly_required_cover_letter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Developer\nCompany: Acme"},
    ).json()

    with session_scope(settings) as db:
        row = db.get(JobPosting, job["id"])
        api_main.update_existing_job_from_ingest_payload(
            row,
            api_main.JobIngestPayload(
                source_type="browser_capture",
                raw_text=(
                    "Backend Developer\nCompany: Acme\n"
                    "Please submit your resume and a cover letter."
                ),
            ),
            db,
            client.app.state.storage,
            settings,
        )

    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        refreshed_job = db.get(JobPosting, job["id"])
        assert run.status == "queued"
        assert run.posting_snapshot_hash == refreshed_job.posting_snapshot_hash


def test_new_cover_letter_worker_recovers_previous_lease_claim_immediately(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    client.post("/api/profile", json=_profile_payload())
    client.post("/api/jobs/ingest", json=_required_cover_letter_posting())

    with BackgroundWorkerLeaseGuard(
        settings,
        "old_cover_worker",
        lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
    ) as old_lease:
        run_id = claim_next_cover_letter_generation(
            settings,
            worker_id="old_cover_worker",
            supervisor_lease_token=old_lease.lease_token,
        )
        assert run_id is not None

    with BackgroundWorkerLeaseGuard(
        settings,
        "new_cover_worker",
        lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
    ) as new_lease:
        recovered = bootstrap_cover_letter_generation_worker(
            settings,
            worker_id="new_cover_worker",
            lease_token=new_lease.lease_token,
        )

    assert recovered == 1
    with session_scope(settings) as db:
        run = db.get(CoverLetterGenerationRun, run_id)
        assert run.status == "retry_scheduled"
        assert run.next_attempt_at is not None


def test_refreshed_required_posting_supersedes_older_snapshot_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()
    with session_scope(settings) as db:
        initial_run_id = db.execute(
            select(CoverLetterGenerationRun.generation_run_id)
        ).scalar_one()

    with session_scope(settings) as db:
        row = db.get(JobPosting, job["id"])
        api_main.update_existing_job_from_ingest_payload(
            row,
            api_main.JobIngestPayload(
                source_type="browser_capture",
                raw_text=(
                    "Backend Developer\nCompany: Example Corp\nPython APIs\n"
                    "A tailored cover letter is required."
                ),
            ),
            db,
            client.app.state.storage,
            settings,
        )

    with session_scope(settings) as db:
        runs = list(db.execute(select(CoverLetterGenerationRun)).scalars())
        assert len(runs) == 2
        initial = next(run for run in runs if run.generation_run_id == initial_run_id)
        replacement = next(run for run in runs if run.generation_run_id != initial_run_id)
        assert initial.status == "cancelled"
        assert initial.error_code == "posting_snapshot_superseded"
        assert replacement.status == "queued"
        assert replacement.posting_snapshot_hash == db.get(
            JobPosting, job["id"]
        ).posting_snapshot_hash


def test_required_posting_snapshot_reversion_creates_current_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    initial_payload = _required_cover_letter_posting()
    job = client.post("/api/jobs/ingest", json=initial_payload).json()

    with session_scope(settings) as db:
        row = db.get(JobPosting, job["id"])
        api_main.update_existing_job_from_ingest_payload(
            row,
            api_main.JobIngestPayload(
                source_type="browser_capture",
                raw_text=(
                    "Backend Developer\nCompany: Example Corp\nPython APIs\n"
                    "A tailored cover letter is required."
                ),
            ),
            db,
            client.app.state.storage,
            settings,
        )
    with session_scope(settings) as db:
        row = db.get(JobPosting, job["id"])
        api_main.update_existing_job_from_ingest_payload(
            row,
            api_main.JobIngestPayload(
                source_type="browser_capture",
                raw_text=initial_payload["raw_text"],
            ),
            db,
            client.app.state.storage,
            settings,
        )

    with session_scope(settings) as db:
        runs = list(db.execute(select(CoverLetterGenerationRun)).scalars())
        current_hash = db.get(JobPosting, job["id"]).posting_snapshot_hash
        current_runs = [run for run in runs if run.posting_snapshot_hash == current_hash]
        assert len(runs) == 3
        assert sum(run.status == "queued" for run in runs) == 1
        assert sum(run.status == "queued" for run in current_runs) == 1
        assert sum(run.status == "cancelled" for run in current_runs) == 1


def test_refresh_that_removes_requirement_cancels_pending_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    with session_scope(settings) as db:
        row = db.get(JobPosting, job["id"])
        api_main.update_existing_job_from_ingest_payload(
            row,
            api_main.JobIngestPayload(
                source_type="browser_capture",
                raw_text="Backend Developer\nCompany: Example Corp\nResume required.",
            ),
            db,
            client.app.state.storage,
            settings,
        )

    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.status == "cancelled"
        assert run.error_code == "cover_letter_not_required"


def test_job_status_transition_cancels_then_requeues_required_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post("/api/jobs/ingest", json=_required_cover_letter_posting()).json()

    assert client.patch(
        f"/api/jobs/{job['id']}/status", json={"status": "closed"}
    ).status_code == 200
    with session_scope(settings) as db:
        first = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert first.status == "cancelled"
        assert first.error_code == "job_not_active"

    assert client.patch(
        f"/api/jobs/{job['id']}/status", json={"status": "active"}
    ).status_code == 200
    with session_scope(settings) as db:
        runs = list(db.execute(select(CoverLetterGenerationRun)).scalars())
        assert len(runs) == 2
        assert sum(run.status == "queued" for run in runs) == 1


def test_scheduled_status_closure_cancels_pending_required_request(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    payload = {
        **_required_cover_letter_posting(),
        "source_type": "gmail_alert",
        "source_url": "https://www.linkedin.com/jobs/view/1234567890",
    }
    job = client.post("/api/jobs/ingest", json=payload).json()

    def closed_probe(_target, candidate):
        return StatusCheckDecision(
            result="closed",
            detected_status="closed",
            evidence_code="linkedin_no_longer_accepting",
            source_url=candidate.url,
            source_kind="linkedin",
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        job_id=job["id"],
        checker=closed_probe,
        now=datetime.now(UTC),
    )

    assert summary["closed"] == 1
    with session_scope(settings) as db:
        run = db.execute(select(CoverLetterGenerationRun)).scalar_one()
        assert run.status == "cancelled"
        assert run.error_code == "job_not_active"
