from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from test_extension_assistance import assistance as assistance, approve, create_review, path, question
from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.db.models import CandidateLearnedFact, CandidateProfile, ExtensionLearningRun, JobPosting
from jobcockpit.db.session import session_scope
from jobcockpit.extension.answers import AnswerGenerationError, generate_answers
from jobcockpit.extension.memory import facts_for_job, propose_facts


BASE = "/api/extension/memory"
FACT = {"subject_key": "python_experience", "value": "Two years",
    "statement": "I have two years of Python experience.", "scope": "general"}


def learning(client, monkeypatch, q=None, answer="Two years"):
    q = q or question(text="How much Python experience do you have?")
    monkeypatch.setattr("jobcockpit.extension.memory.propose_facts", lambda *args: [{
        **FACT, "observation_id": "q1", "country": "", "term": "", "year": None,
        "rationale": "Applicant's selected answer", "missing_information": []}])
    review = create_review(client, [q])
    response = client.post(BASE + "/learning-runs", json={"session_id": review["session_id"],
        "revision": review["revision"], "confirmed_my_answers": True,
        "observations": [{"question_id": "q1", "answer": answer, "source": "page"}]})
    assert response.status_code == 202, response.text
    run = client.get(BASE + "/learning-runs/" + response.json()["run_id"]).json()
    assert run["status"] == "succeeded", run
    return run


def confirm(client, run, **fields):
    return client.post(BASE + f"/learning-runs/{run['run_id']}/confirm", json={
        **FACT, "observation_id": "q1", "run_revision": run["revision"], "confirmed": True, **fields})


def test_learning_requires_confirmation_retains_provenance_and_invalidates_drafts(assistance, monkeypatch):
    client, _ = assistance
    review = create_review(client)
    approved = approve(client, review).json()
    run = learning(client, monkeypatch)
    assert client.get(BASE + "/facts").json() == []
    assert client.get("/api/extension/context/job-two").json()["personal_facts"] == []
    response = confirm(client, run)
    assert response.status_code == 201, response.text
    fact = response.json()["fact"]
    assert fact["source"]["question"]["text"] == "How much Python experience do you have?"
    assert fact["source"]["answer"] == "Two years"
    assert confirm(client, run).status_code == 409
    assert client.get(path(approved, "/fill-plan"), params={"revision": approved["revision"]}).status_code == 409
    assert client.get("/api/extension/context/job-two").json()["personal_facts"][0]["fact_id"] == fact["fact_id"]
    fresh = create_review(client)
    approved = approve(client, fresh).json()
    assert client.delete(BASE + f"/facts/{fact['fact_id']}?revision=99").status_code == 409
    assert client.delete(BASE + f"/facts/{fact['fact_id']}?revision=1").status_code == 204
    assert client.get(BASE + "/facts").json() == []
    assert client.get(path(approved, "/fill-plan"), params={"revision": approved["revision"]}).status_code == 409
    updated_run = response.json()["run"]
    assert confirm(client, updated_run).status_code == 409  # Deleting never revives an old proposal.


def test_conflicts_require_explicit_current_revisions_and_corrections(assistance, monkeypatch):
    client, _ = assistance
    first = confirm(client, learning(client, monkeypatch)).json()["fact"]
    run = learning(client, monkeypatch, answer="Three years")
    fields = {"value": "Three years", "statement": "I have three years of Python experience."}
    response = confirm(client, run, **fields)
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"][0]["fact_id"] == first["fact_id"]
    assert confirm(client, run, **fields, replace_revisions={first["fact_id"]: 9}).status_code == 409
    replaced = confirm(client, run, **fields, replace_revisions={first["fact_id"]: 1})
    assert replaced.status_code == 201, replaced.text
    fact = replaced.json()["fact"]
    assert len(client.get(BASE + "/facts").json()) == 1
    update = {**FACT, "revision": 1, "confirmed": True, "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    response = client.put(BASE + f"/facts/{fact['fact_id']}", json=update)
    assert response.status_code == 200 and response.json()["expired"]
    assert response.json()["source"]["answer"] == "Three years"
    assert client.put(BASE + f"/facts/{fact['fact_id']}", json=update).status_code == 409
    assert client.get("/api/extension/context/job-one").json()["personal_facts"] == []


def test_scope_filters_known_country_term_and_job_without_posted_date_inference(assistance):
    _, settings = assistance
    with session_scope(settings) as db:
        for fact_id, scope, limits in [("general", "general", {}), ("canada", "country", {"country": "CA"}),
            ("us", "country", {"country": "US"}), ("summer", "term", {"term": "Summer", "year": 2027}),
            ("job", "job", {"job_id": "job-one"}), ("expired", "general", {"expires_at": datetime.now(UTC) - timedelta(days=1)})]:
            db.add(CandidateLearnedFact(fact_id=fact_id, candidate_id=CANDIDATE_ID,
                **{**FACT, "scope": scope}, **limits, source={}))
        db.flush()
        job = db.get(JobPosting, "job-one")
        job.location = {"country": "Canada"}
        job.employment_timing = {"timing_type": "fixed_term", "start_date": "2027-05-01"}
        def ids():
            return {f["fact_id"] for f in facts_for_job(db, job)}
        assert ids() == {"general", "canada", "summer", "job"}
        job.employment_timing = {"timing_type": "posted_date_fallback", "start_date": "2027-05-01"}
        assert ids() == {"general", "canada", "job"}
        job.location = {}
        assert ids() == {"general", "job"}


def test_eligibility_and_availability_scope_and_profile_conflicts(assistance, monkeypatch):
    client, settings = assistance
    run = learning(client, monkeypatch, question(text="Are you available for eight months from May through December 2027?"), "Yes")
    fields = {"subject_key": "availability", "value": "May through December 2027",
        "statement": "I am available for eight months from May through December 2027."}
    assert confirm(client, run, **fields).status_code == 422
    response = confirm(client, run, **fields, scope="term", term="Summer", year=2027)
    assert response.status_code == 201
    assert response.json()["fact"]["expires_at"].startswith("2027-09-01")
    assert "eight months" in response.json()["fact"]["statement"]
    run = learning(client, monkeypatch, question(text="Are you authorized to work in Canada?"), "Yes")
    fields = {"subject_key": "work_authorization", "value": "Yes", "statement": "I am authorized to work in Canada."}
    assert confirm(client, run, **fields).status_code == 422
    assert confirm(client, run, **fields, scope="country", country="Canada").status_code == 201
    run = learning(client, monkeypatch, question(text="First name"), "Different")
    assert confirm(client, run, subject_key="first_name", value="Different", statement="My first name is Different.").status_code == 409
    assert confirm(client, run, subject_key="first_name", value="Example", statement="My first name is Example.").status_code == 201
    with session_scope(settings) as db:
        db.get(CandidateProfile, CANDIDATE_ID).profile_data = {"first_name": "Changed"}
    response = client.get("/api/extension/context/job-one")
    assert response.status_code == 409 and "profile" in response.text
    assert client.get(BASE + "/facts").status_code == 200  # Correction stays accessible.


@pytest.mark.parametrize("q,answer", [(question(text="Social insurance number"), "123"),
    (question(text="I certify this is correct", kind="checkbox"), "true"),
    (question(kind="select", options=[{"value": "CA", "label": "Canada"}]), "Outside Canada")])
def test_learning_rejects_credentials_manual_and_invalid_options(assistance, q, answer):
    client, _ = assistance
    review = create_review(client, [q])
    response = client.post(BASE + "/learning-runs", json={"session_id": review["session_id"], "revision": 1,
        "confirmed_my_answers": True, "observations": [{"question_id": "q1", "answer": answer, "source": "page"}]})
    assert response.status_code == 422
    assert client.get(BASE + "/facts").json() == []


def test_discard_during_generation_cannot_restore_run(assistance, monkeypatch):
    client, settings = assistance
    def discard(*args):
        with session_scope(settings) as db:
            run = db.query(ExtensionLearningRun).one()
            db.delete(run)
        return []
    monkeypatch.setattr("jobcockpit.extension.memory.propose_facts", discard)
    review = create_review(client)
    response = client.post(BASE + "/learning-runs", json={"session_id": review["session_id"], "revision": 1,
        "confirmed_my_answers": True, "observations": [{"question_id": "q1", "answer": "Example", "source": "panel"}]})
    assert response.status_code == 202
    assert client.get(BASE + "/learning-runs/" + response.json()["run_id"]).status_code == 404


def test_model_proposals_require_real_source_ids_and_facts_are_citable(assistance, monkeypatch):
    _, settings = assistance
    proposal = {**FACT, "observation_id": "missing", "rationale": "Selected answer"}
    result = SimpleNamespace(stdout=json.dumps({"proposals": [proposal]}), error="")
    monkeypatch.setattr("jobcockpit.extension.memory.run_data_prompt", lambda *a: result)
    with pytest.raises(AnswerGenerationError, match="unknown"):
        propose_facts(settings, [{"question_id": "q1", "answer": "Two years"}], {}, [], {})
    proposal["observation_id"] = "q1"
    result.stdout = json.dumps({"proposals": [proposal]})
    assert propose_facts(settings, [{"question_id": "q1", "answer": "Two years"}], {}, [], {})[0]["value"] == "Two years"
    result.stdout = json.dumps({"answers": [{"question_id": "q1", "answer": "Two years", "status": "draft",
        "source_ids": ["fact.test"], "rationale": "Confirmed applicant experience", "missing_information": []}]})
    monkeypatch.setattr("jobcockpit.extension.answers.run_codex_prompt", lambda *a, **kw: result)
    answer = generate_answers(settings, questions=[question(text="How much Python experience do you have?")],
        context={"profile": {}, "job": {}, "evidence": [], "personal_facts": [{**FACT, "fact_id": "test"}]})[0]
    assert answer["answer"] == "Two years" and not answer["approved"]
    assert answer["source_ids"] == ["fact.test"]
