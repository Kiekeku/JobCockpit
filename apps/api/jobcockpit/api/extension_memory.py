from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.application_documents import begin_serialized_package_write
from jobcockpit.api.job_access import require_active_job
from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, CandidateLearnedFact, ExtensionLearningRun
from jobcockpit.extension.answers import exact_answer
from jobcockpit.extension.memory import (
    all_facts, check_profile_conflict, conflicts, fact_api, memory_question_allowed,
    process_learning, run_api, utc, validate_fact_scope,
)
from jobcockpit.extension.sessions import require_session
from jobcockpit.schemas.extension_assistance import ApplicationQuestion
from jobcockpit.schemas.extension_memory import (
    ConfirmPersonalFact, LearnAnswersRequest, PersonalFactFields, UpdatePersonalFact,
)
from jobcockpit.utils.ids import new_id


def create_extension_memory_router(*, get_db: Callable[..., Session], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/extension/memory", tags=["extension personal facts"])

    def require_run(db, run_id):
        run = db.get(ExtensionLearningRun, run_id)
        if not run or run.candidate_id != CANDIDATE_ID:
            raise HTTPException(404, "Learning review not found")
        if utc(run.expires_at) <= datetime.now(UTC):
            raise HTTPException(409, "These proposals expired. Capture your answers again.")
        require_active_job(db, run.job_id)
        return run

    def require_fact(db, fact_id):
        fact = db.get(CandidateLearnedFact, fact_id)
        if not fact or fact.candidate_id != CANDIDATE_ID:
            raise HTTPException(404, "Personal fact not found")
        return fact

    def replace_conflicts(db, fields, revisions, exclude=None):
        found = conflicts(db, fields, exclude)
        expected = {item["fact_id"]: item["revision"] for item in found}
        if revisions != expected:
            raise HTTPException(409, detail={"message": "Review the conflicting facts before replacing them.", "conflicts": found})
        for fact_id in expected:
            db.delete(require_fact(db, fact_id))
        check_profile_conflict(db, fields)

    @router.get("/facts")
    def list_facts(db: Session = Depends(get_db)) -> list[dict]:
        return [fact_api(fact) for fact in all_facts(db)]

    @router.post("/learning-runs", status_code=202)
    def learn(request: LearnAnswersRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, request.session_id, request.revision)
        if settings.extension_answer_provider != "codex_cli":
            raise HTTPException(409, "AI learning is disabled in the local settings.")
        existing = db.scalar(select(ExtensionLearningRun).where(
            ExtensionLearningRun.session_id == session.session_id, ExtensionLearningRun.status == "running"))
        if existing:
            run_api(existing, db, settings)
            if existing.status == "running":
                raise HTTPException(409, "This application's answers are already being interpreted.")
        questions = {q["id"]: ApplicationQuestion.model_validate(q) for q in session.questions}
        observations = []
        for item in request.observations:
            q = questions.get(item.question_id)
            if q is None or not memory_question_allowed(q):
                raise HTTPException(422, "This field is not eligible for personal-fact learning.")
            value = exact_answer(q, item.answer)
            if value is None:
                raise HTTPException(422, "The captured answer does not match the original question's options or format.")
            label = next((o.label for o in q.options if o.value == value), value)
            observations.append({"question_id": q.id, "question": q.model_dump(), "answer": value,
                "answer_label": label, "source": item.source})
        if sum(len(str(item)) for item in observations) > settings.extension_answer_max_input_chars:
            raise HTTPException(422, "Select fewer completed answers.")
        run = ExtensionLearningRun(run_id=new_id("learn"), candidate_id=CANDIDATE_ID,
            session_id=session.session_id, job_id=session.job_id, observations=observations,
            expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(run)
        db.flush()
        payload = run_api(run, db, settings)
        db.commit()
        background_tasks.add_task(process_learning, settings, run.run_id)
        return payload

    @router.get("/learning-runs/{run_id}")
    def get_run(run_id: str, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        return run_api(require_run(db, run_id), db, settings)

    @router.delete("/learning-runs/{run_id}", status_code=204)
    def discard_run(run_id: str, db: Session = Depends(get_db)) -> Response:
        begin_serialized_package_write(db)
        run = db.get(ExtensionLearningRun, run_id)
        if run and run.candidate_id == CANDIDATE_ID:
            db.delete(run)
        return Response(status_code=204)

    @router.post("/learning-runs/{run_id}/confirm", status_code=201)
    def confirm(run_id: str, request: ConfirmPersonalFact, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        run = require_run(db, run_id)
        if run.status != "succeeded" or run.revision != request.run_revision:
            raise HTTPException(409, "These proposals changed. Refresh them before confirming.")
        observation = next((o for o in run.observations if o["question_id"] == request.observation_id), None)
        if not observation or request.observation_id in run.confirmed_observations:
            raise HTTPException(409, "This answer was already saved or is not part of this learning review.")
        fields = PersonalFactFields.model_validate(request.model_dump(include=set(PersonalFactFields.model_fields)))
        if fields.scope == "job" and fields.job_id != run.job_id:
            raise HTTPException(422, "Application-specific facts must stay with the original application.")
        validate_fact_scope(fields, ApplicationQuestion.model_validate(observation["question"]))
        replace_conflicts(db, fields, request.replace_revisions)
        fact = CandidateLearnedFact(fact_id=new_id("fact"), candidate_id=CANDIDATE_ID,
            **fields.model_dump(), source={**observation, "job_id": run.job_id,
                "confirmed_at": datetime.now(UTC).isoformat()})
        db.add(fact)
        run.confirmed_observations = [*run.confirmed_observations, request.observation_id]
        run.revision += 1
        db.add(AuditLogEntry.create("extension.fact.confirmed", "candidate_learned_fact", fact.fact_id,
            {"scope": fact.scope, "replaced_fact_ids": list(request.replace_revisions)}))
        db.flush()
        return {"fact": fact_api(fact), "run": run_api(run, db, settings)}

    @router.put("/facts/{fact_id}")
    def update_fact(fact_id: str, request: UpdatePersonalFact, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        fact = require_fact(db, fact_id)
        if fact.revision != request.revision:
            raise HTTPException(409, "This fact changed. Refresh before editing it.")
        fields = PersonalFactFields.model_validate(request.model_dump(include=set(PersonalFactFields.model_fields)))
        if fields.scope == "job":
            require_active_job(db, fields.job_id)
        q = ApplicationQuestion.model_validate(fact.source["question"]) if fact.source.get("question") else None
        validate_fact_scope(fields, q)
        replace_conflicts(db, fields, request.replace_revisions, fact_id)
        for key, value in fields.model_dump().items():
            setattr(fact, key, value)
        fact.revision += 1
        fact.source = {**fact.source, "last_corrected_at": datetime.now(UTC).isoformat()}
        db.add(AuditLogEntry.create("extension.fact.updated", "candidate_learned_fact", fact_id))
        db.flush()
        return fact_api(fact)

    @router.delete("/facts/{fact_id}", status_code=204)
    def delete_fact(fact_id: str, revision: int, db: Session = Depends(get_db)) -> Response:
        begin_serialized_package_write(db)
        fact = require_fact(db, fact_id)
        if fact.revision != revision:
            raise HTTPException(409, "This fact changed. Refresh before deleting it.")
        db.delete(fact)
        db.add(AuditLogEntry.create("extension.fact.deleted", "candidate_learned_fact", fact_id))
        return Response(status_code=204)

    return router
