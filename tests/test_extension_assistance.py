from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.db.models import (
    CandidateProfile, EvidenceItem, ExtensionApplicationSession, JobPosting, StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.extension.answers import base_answer, exact_answer, generate_answers
from jobcockpit.schemas.extension_assistance import ApplicationQuestion


def question(**overrides):
    return ApplicationQuestion.model_validate({
        "id": "q1", "document_token": "document", "snapshot_token": "snapshot",
        "fingerprint": "a" * 64, "text": "First name", "kind": "text",
        "frame_url": "http://localhost:8001/apply/test", "fillable": True, **overrides,
    })


@pytest.fixture
def assistance(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(extension_answer_provider="codex_cli")
    client = jobcockpit_client_factory(settings)
    record = client.app.state.storage.write_bytes(
        artifact_type="job_snapshot", content=b"Synthetic software internship",
        original_file_name="posting.txt", content_type="text/plain", privacy_class="private",
    )
    with session_scope(settings) as db:
        db.add(StoredArtifact.from_record(record))
        db.flush()
        for job_id in ("job-one", "job-two"):
            db.add(JobPosting(id=job_id, raw_content_artifact_id=record.artifact_id,
                source_type="manual", posting_snapshot_hash=job_id.ljust(64, "b"),
                company_name="Example", normalized_title="Software intern"))
        db.add(CandidateProfile(candidate_id=CANDIDATE_ID, profile_name="Test applicant",
            profile_data={"first_name": "Example", "last_name": "Applicant", "country": "Canada"}))
        for evidence_id, private in (("permitted", False), ("private-note", True)):
            db.add(EvidenceItem(evidence_id=evidence_id, candidate_id=CANDIDATE_ID,
                source_type="manual", claim="Built a local Python service", skills=["Python"], private=private))
    return client, settings


def create_review(client, questions=None, job_id="job-one"):
    response = client.post("/api/extension/application-sessions", json={
        "job_id": job_id, "page_url": "http://localhost:8001/apply/test", "job_confirmed": True,
        "questions": [q.model_dump() for q in questions or [question()]],
    })
    assert response.status_code == 201, response.text
    return response.json()


def path(review, suffix=""):
    return f"/api/extension/application-sessions/{review['session_id']}{suffix}"


def approve(client, review, **overrides):
    return client.post(path(review, "/review"), json={"revision": review["revision"],
        "reviews": [{"question_id": "q1", "answer": "Example", "decision": "approve", **overrides}]})


@pytest.mark.parametrize("text,section,kind,profile", [
    ("Why are you interested in this opportunity?", "", "textarea", {"address_line_2": "Unit 2"}),
    ("Email", "References", "email", {"email": "applicant" + "@example.invalid"}),
    ("Country", "", "select", {"country": "Canada"}),
    ("Graduation date", "", "date", {"graduation_date": "May 2027"}),
])
def test_audit_mapping_regressions(text, section, kind, profile):
    q = question(text=text, section=section, kind=kind,
        options=[{"value": "outside", "label": "Outside Canada"}] if kind == "select" else [])
    assert base_answer(q, profile)["answer"] == ""


def test_exact_choice_and_calendar_format():
    q = question(kind="select", options=[{"value": "CA", "label": "Canada"},
        {"value": "X", "label": "Outside Canada"}])
    assert exact_answer(q, "Canada") == "CA"
    assert exact_answer(q, "Can") is None
    assert exact_answer(question(kind="date"), "2027-05") is None
    assert exact_answer(question(kind="date"), "2027-02-30") is None
    assert exact_answer(question(kind="date"), "2027-05-15") == "2027-05-15"


def test_approval_revision_expiry_context_and_reuse(assistance):
    client, settings = assistance
    review = create_review(client)
    assert review["answers"][0]["answer"] == "Example"
    assert client.get(path(review, "/fill-plan"), params={"revision": 1}).json()["plans"] == []
    response = approve(client, review, remember=True)
    assert response.status_code == 200, response.text
    approved = response.json()
    assert approve(client, review).status_code == 409
    assert len(client.get(path(approved, "/fill-plan"), params={"revision": 2}).json()["plans"]) == 1
    again = create_review(client)
    assert again["answers"][0]["status"] == "previous_answer"
    assert not again["answers"][0]["approved"]
    other = create_review(client, job_id="job-two")
    assert other["answers"][0]["status"] == "profile_fact"
    with session_scope(settings) as db:
        row = db.get(ExtensionApplicationSession, approved["session_id"])
        row.answers = [{**row.answers[0], "approval_expires_at": 1}]
    assert client.get(path(approved, "/fill-plan"), params={"revision": 2}).json()["plans"] == []
    with session_scope(settings) as db:
        profile = db.get(CandidateProfile, CANDIDATE_ID)
        profile.profile_data = {**profile.profile_data, "first_name": "Changed"}
    assert client.get(path(approved, "/fill-plan"), params={"revision": 2}).status_code == 409


def test_sensitive_manual_overwrite_and_forged_receipts(assistance):
    client, _ = assistance
    review = create_review(client, [question(text="Are you available for this term?")])
    assert approve(client, review, answer="Yes").status_code == 422
    assert approve(client, review, answer="Yes", confirmed_sensitive=True).status_code == 200
    manual = create_review(client, [question(text="I certify the information is correct", kind="checkbox")])
    assert approve(client, manual, answer="true").status_code == 422
    existing = create_review(client, [question(current_value="Already here")])
    assert approve(client, existing).status_code == 422
    assert approve(client, existing, replace_existing=True).status_code == 200
    fresh = create_review(client)
    assert client.post(path(fresh, "/receipts"), json={"revision": 1,
        "results": [{"question_id": "q1", "status": "filled", "actual_value": "Example"}]}).status_code == 422


def test_merge_invalidates_original_approvals_without_reusing_them(assistance):
    from jobcockpit.api.job_merge import JobMergePlan, apply_authoritative_job_merge
    client, settings = assistance
    review = create_review(client)
    approved = approve(client, review, remember=True).json()
    with session_scope(settings) as db:
        apply_authoritative_job_merge(JobMergePlan(canonical_job_id="job-two",
            duplicate_job_ids=("job-one",), provider="synthetic", identity_hash="c" * 64), db)
    assert client.get(path(approved, "/fill-plan"), params={"revision": 2}).status_code == 409
    again = create_review(client, job_id="job-two")
    assert again["answers"][0]["status"] != "previous_answer"


def test_drafting_abstains_for_invalid_sources_and_options(assistance, monkeypatch):
    _, settings = assistance
    payload = {"answers": [{"question_id": "q1", "answer": "Canada", "status": "draft",
        "source_ids": ["profile.country"], "rationale": "Saved country", "missing_information": []}]}
    seen = {}
    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout=json.dumps(payload), error="")
    monkeypatch.setattr("jobcockpit.extension.answers.run_codex_prompt", fake_run)
    context = {"profile": {"country": "Canada"}, "job": {"title": "Test"}, "evidence": []}
    q = question(kind="select", options=[{"value": "outside", "label": "Outside Canada"}])
    result = generate_answers(settings, questions=[q], context=context)[0]
    assert result["answer"] == "" and result["status"] == "needs_input"
    assert seen["model"] == "gpt-6-sol"
    assert seen["reasoning_effort"] == "medium"
    assert seen["config_overrides"]["features.shell_tool"] is False
    assert seen["ephemeral"] and seen["ignore_user_config"]
    payload["answers"][0]["source_ids"] = ["evidence.invented"]
    result = generate_answers(settings, questions=[question()], context=context)[0]
    assert result["source_ids"] == [] and not result["approved"]


def test_background_draft_survives_failures_and_rechecks_source_permission(assistance, monkeypatch):
    client, settings = assistance
    extension_context = client.get("/api/extension/context/job-one").json()
    assert [e["id"] for e in extension_context["evidence"]] == ["permitted"]
    assert extension_context["ai"]["model"] == "gpt-6-sol"
    review = create_review(client)
    invalid = client.post(path(review, "/draft"), json={"revision": 1,
        "evidence_ids": ["private-note"], "consent_to_use_sources": True})
    assert invalid.status_code == 409
    def revoke(**kwargs):
        with session_scope(settings) as db:
            db.get(EvidenceItem, "permitted").private = True
        return []
    monkeypatch.setattr("jobcockpit.extension.sessions.generate_answers", lambda *a, **kw: revoke(**kw))
    started = client.post(path(review, "/draft"), json={"revision": 1,
        "evidence_ids": ["permitted"], "consent_to_use_sources": True})
    assert started.status_code == 202, started.text
    finished = client.get(path(review)).json()
    assert finished["draft_status"] == "failed"
    assert not any(a["approved"] for a in finished["answers"])
    with session_scope(settings) as db:
        row = db.get(ExtensionApplicationSession, review["session_id"])
        row.draft_status = "running"
        row.draft_started_at = datetime.now(UTC) - timedelta(hours=1)
    assert client.get(path(review)).json()["draft_status"] == "failed"


def test_resume_is_bound_to_reviewed_purpose_bytes_and_session(assistance):
    client, settings = assistance
    storage = client.app.state.storage
    record = storage.write_bytes(artifact_type="candidate_document", content=b"%PDF-1.4\nsynthetic",
        original_file_name="test-resume.pdf", content_type="application/pdf",
        privacy_class="private", candidate_id=CANDIDATE_ID, source_kind="profile_resume_upload")
    with session_scope(settings) as db:
        db.add(StoredArtifact.from_record(record))
    wrong = create_review(client, [question(kind="file", text="Transcript", purpose="transcript")])
    payload = {"revision": 1, "question_id": "q1", "artifact_id": record.artifact_id,
        "sha256": record.sha256, "reviewed_document_and_target": True}
    assert client.post(path(wrong, "/approve-document"), json=payload).status_code == 422
    review = create_review(client, [question(kind="file", text="Resume", purpose="resume")])
    response = client.post(path(review, "/approve-document"), json=payload)
    assert response.status_code == 200, response.text
    approved = response.json()
    assert client.get(approved["download_url"]).content == b"%PDF-1.4\nsynthetic"
    replay = approved["download_url"].replace(review["session_id"], wrong["session_id"])
    assert client.get(replay).status_code == 403
    with session_scope(settings) as db:
        db.get(StoredArtifact, record.artifact_id).deleted_at = datetime.now(UTC)
    assert client.get(approved["download_url"]).status_code == 403
