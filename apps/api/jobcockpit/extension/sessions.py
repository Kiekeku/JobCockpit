from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.job_access import require_active_job
from jobcockpit.api.application_documents import begin_serialized_package_write
from jobcockpit.api.profile_routes import CANDIDATE_ID, normalize_profile_data
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry, CandidateProfile, EvidenceItem, ExtensionApplicationSession,
    ExtensionApprovedAnswer, JobPosting, StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.extension.answers import (
    PROFILE_KEYS, AnswerGenerationError, base_answer, digest, generate_answers, question_signature,
)
from jobcockpit.schemas.extension_assistance import ApplicationQuestion, question_is_sensitive
from jobcockpit.extension.memory import facts_for_job


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def allowed_evidence(db: Session) -> list[EvidenceItem]:
    return list(db.scalars(
        select(EvidenceItem).outerjoin(StoredArtifact, StoredArtifact.artifact_id == EvidenceItem.source_artifact_id)
        .where(
            EvidenceItem.candidate_id == CANDIDATE_ID,
            EvidenceItem.private.is_(False),
            EvidenceItem.allowed_for_cover_letter.is_(True),
            (EvidenceItem.source_artifact_id.is_(None)) | (
                StoredArtifact.deleted_at.is_(None) & StoredArtifact.candidate_id.__eq__(CANDIDATE_ID)
            ),
        ).order_by(EvidenceItem.updated_at.desc()).limit(200)
    ))


def answer_context(db: Session, job: JobPosting, evidence_ids: list[str]) -> dict:
    candidate = db.get(CandidateProfile, CANDIDATE_ID)
    data = normalize_profile_data(candidate.profile_data) if candidate else {}
    profile = {key: data[key] for key in PROFILE_KEYS if data.get(key) not in (None, "", [])}
    available = {item.evidence_id: item for item in allowed_evidence(db)}
    if set(evidence_ids) - available.keys():
        raise HTTPException(status_code=409, detail="Selected experience changed or is no longer permitted. Refresh sources.")
    return {
        "profile": profile,
        "personal_facts": facts_for_job(db, job),
        "profile_updated_at": candidate.updated_at.isoformat() if candidate else None,
        "job": {
            "id": job.id, "company": job.company_name, "title": job.normalized_title,
            "source_url": job.source_url, "snapshot_hash": job.posting_snapshot_hash,
            "role": job.role, "employment_timing": job.employment_timing,
            "location": job.location, "eligibility": job.eligibility,
            "application": job.application,
        },
        "evidence": [{
            "id": item.evidence_id, "claim": item.claim, "skills": item.skills,
            "updated_at": item.updated_at.isoformat(),
        } for key in sorted(set(evidence_ids)) for item in [available[key]]],
    }


def require_session(db: Session, session_id: str, revision: int | None = None) -> ExtensionApplicationSession:
    session = db.get(ExtensionApplicationSession, session_id)
    if not session or session.candidate_id != CANDIDATE_ID:
        raise HTTPException(status_code=404, detail="Application review not found")
    require_active_job(db, session.job_id)
    if utc(session.expires_at) <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail="This review expired. Scan the current application again.")
    if revision is not None and session.revision != revision:
        raise HTTPException(status_code=409, detail="This review changed. Refresh before continuing.")
    return session


def validate_context(db: Session, session: ExtensionApplicationSession) -> dict:
    job = require_active_job(db, session.job_id)
    context = answer_context(db, job, session.evidence_ids)
    if digest(context) != session.context_fingerprint:
        raise HTTPException(status_code=409, detail="The profile, personal facts, job or experience changed. Rescan and review the new answers.")
    return context


def initial_answers(db: Session, job: JobPosting, questions: list[ApplicationQuestion], context: dict) -> list[dict]:
    saved = {item.question_signature: item for item in db.scalars(
        select(ExtensionApprovedAnswer).where(
            ExtensionApprovedAnswer.job_id == job.id,
            ExtensionApprovedAnswer.candidate_id == CANDIDATE_ID,
            ExtensionApprovedAnswer.expires_at > datetime.now(UTC),
            ExtensionApprovedAnswer.context_fingerprint == digest(context),
        )
    )}
    answers = []
    for question in questions:
        answer = base_answer(question, context["profile"])
        previous = saved.get(question_signature(question))
        if previous and answer["status"] not in {"manual", "existing"} and not question_is_sensitive(question):
            answer.update(
                answer=previous.answer, status="previous_answer", source_ids=previous.source_ids,
                rationale="Previously approved for this exact question and job with unchanged sources. Review again before filling.",
            )
        answers.append(answer)
    return answers


def session_to_api(session: ExtensionApplicationSession) -> dict:
    return {
        "session_id": session.session_id, "job_id": session.job_id, "revision": session.revision,
        "page_url": session.page_url, "page_title": session.page_title,
        "questions": session.questions, "answers": session.answers,
        "evidence_ids": session.evidence_ids, "draft_status": session.draft_status,
        "draft_model": session.draft_model, "draft_error": session.draft_error,
        "receipts": session.receipts, "expires_at": utc(session.expires_at).isoformat(),
    }


def process_answer_draft(settings: Settings, session_id: str, request_id: str, instructions: str) -> None:
    try:
        with session_scope(settings) as db:
            session = require_session(db, session_id)
            if session.draft_request_id != request_id or session.draft_status != "running":
                return
            context = validate_context(db, session)
            questions = [ApplicationQuestion.model_validate(item) for item in session.questions]
        proposals = generate_answers(settings, questions=questions, context=context, instructions=instructions)
        with session_scope(settings) as db:
            begin_serialized_package_write(db)
            session = require_session(db, session_id)
            if session.draft_request_id != request_id or session.draft_status != "running":
                return
            validate_context(db, session)
            by_id = {item["question_id"]: item for item in proposals}
            # Missing model answers remain visible as needs_input/profile_fact. Nothing is approved.
            session.answers = [by_id.get(item["question_id"], item) for item in session.answers]
            session.draft_status = "succeeded"
            session.revision += 1
            db.add(AuditLogEntry.create("extension.answers.drafted", "extension_application_session", session_id,
                                       {"model": session.draft_model, "question_count": len(proposals)}))
    except (AnswerGenerationError, HTTPException) as error:
        message = str(error) if isinstance(error, AnswerGenerationError) else str(error.detail)
        _fail_draft(settings, session_id, request_id, message)
    except Exception:
        _fail_draft(settings, session_id, request_id, "AI drafting failed. No answers were approved; retry or enter answers manually.")


def _fail_draft(settings: Settings, session_id: str, request_id: str, message: str) -> None:
    with session_scope(settings) as db:
        begin_serialized_package_write(db)
        session = db.get(ExtensionApplicationSession, session_id)
        if session and session.draft_request_id == request_id and session.draft_status == "running":
            session.draft_status = "failed"
            session.draft_error = message[:1000]
            session.revision += 1


def recover_interrupted_draft(session: ExtensionApplicationSession, settings: Settings) -> None:
    if (session.draft_status == "running" and session.draft_started_at
            and utc(session.draft_started_at) + timedelta(seconds=settings.extension_answer_timeout_seconds + 60) < datetime.now(UTC)):
        session.draft_status = "failed"
        session.draft_error = "Drafting was interrupted or timed out. Retry when ready; nothing was filled."
        session.revision += 1
