from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from jobcockpit.api.application_documents import begin_serialized_package_write, next_application_package_version
from jobcockpit.api.document_approvals import has_manual_upload_approval
from jobcockpit.api.file_responses import attachment_headers
from jobcockpit.api.job_access import require_active_job, require_active_package_job
from jobcockpit.api.profile_routes import CANDIDATE_ID, is_profile_pdf_resume_artifact
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application, ApplicationPackage, ApplicationPackageDocument, AuditLogEntry, CandidateProfile,
    ExtensionApplicationSession, ExtensionApprovedAnswer, GeneratedDocument, JobPosting,
    StoredArtifact, TruthfulnessCheck,
)
from jobcockpit.extension.answers import PROFILE_KEYS, digest, exact_answer, question_signature
from jobcockpit.extension.memory import facts_for_job
from jobcockpit.extension.sessions import (
    allowed_evidence, answer_context, initial_answers, process_answer_draft,
    recover_interrupted_draft, require_session, session_to_api, validate_context,
)
from jobcockpit.extension.tokens import create_extension_download_token, verify_extension_download_token
from jobcockpit.schemas.extension_assistance import (
    ApplicationQuestion, ApplicationSessionRequest, DocumentApprovalRequest, DraftAnswersRequest,
    FillReceiptsRequest, ReviewAnswersRequest, question_is_manual, question_is_sensitive,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


def _question(session: ExtensionApplicationSession, question_id: str) -> ApplicationQuestion:
    value = next((item for item in session.questions if item["id"] == question_id), None)
    if not value:
        raise HTTPException(status_code=404, detail="Question not found in this review")
    return ApplicationQuestion.model_validate(value)


def _not_drafting(session: ExtensionApplicationSession) -> None:
    if session.draft_status == "running":
        raise HTTPException(status_code=409, detail="Wait for drafting to finish before reviewing or attaching.")


def _package_pdf_is_cleared(db: Session, row: ApplicationPackageDocument, artifact: StoredArtifact) -> bool:
    package, application, _job = require_active_package_job(db, row.application_package_id)
    if (
        application.candidate_id != CANDIDATE_ID or artifact.deleted_at is not None
        or (artifact.content_type != "application/pdf" and not is_profile_pdf_resume_artifact(artifact, CANDIDATE_ID))
        or not package.approved_for_extension
        or not row.approved_for_upload or row.artifact_id != artifact.artifact_id
    ):
        return False
    if row.generated_document_id:
        document = db.get(GeneratedDocument, row.generated_document_id)
        if not document or not document.approved_by_user or document.artifact_id != artifact.artifact_id:
            return False
        check = db.get(TruthfulnessCheck, document.truthfulness_check_id) if document.truthfulness_check_id else None
        return bool(check and check.application_package_id == package.application_package_id and check.status == "passed") or has_manual_upload_approval(
            db, package_id=package.application_package_id,
            document_id=document.document_id, artifact_id=artifact.artifact_id,
        )
    return is_profile_pdf_resume_artifact(artifact, CANDIDATE_ID) and row.document_type == "resume"


def _documents(db: Session, job_id: str) -> list[dict]:
    resumes = db.scalars(select(StoredArtifact).where(
        StoredArtifact.candidate_id == CANDIDATE_ID,
        StoredArtifact.source_kind == "profile_resume_upload",
        StoredArtifact.deleted_at.is_(None),
    ).order_by(StoredArtifact.created_at.desc()))
    result = [{
        "artifact_id": item.artifact_id, "file_name": item.original_file_name,
        "sha256": item.sha256, "document_type": "resume", "size_bytes": item.size_bytes,
        "approval": "Review and approve for this application before attaching",
    } for item in resumes if is_profile_pdf_resume_artifact(item, CANDIDATE_ID)]
    seen = {item["artifact_id"] for item in result}
    rows = db.execute(select(ApplicationPackageDocument, StoredArtifact)
        .join(StoredArtifact, StoredArtifact.artifact_id == ApplicationPackageDocument.artifact_id)
        .join(ApplicationPackage, ApplicationPackage.application_package_id == ApplicationPackageDocument.application_package_id)
        .join(Application, Application.application_id == ApplicationPackage.application_id)
        .where(Application.job_id == job_id, Application.candidate_id == CANDIDATE_ID)).all()
    for row, artifact in rows:
        if artifact.artifact_id not in seen and _package_pdf_is_cleared(db, row, artifact):
            result.append({
                "artifact_id": artifact.artifact_id, "file_name": artifact.original_file_name,
                "sha256": artifact.sha256, "document_type": row.document_type,
                "size_bytes": artifact.size_bytes, "approval": "Approved application package document",
            })
            seen.add(artifact.artifact_id)
    return result


def create_extension_assistance_router(
    *, get_db: Callable[..., Session], get_storage: Callable[..., ArtifactStorageService], settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/extension", tags=["extension assistance"])

    @router.get("/jobs")
    def jobs(q: str = "", db: Session = Depends(get_db)) -> list[dict]:
        statement = select(JobPosting).where(
            text("json_extract(job_postings.source, '$.merged_into_job_id') IS NULL"),
        )
        if q.strip():
            needle = "%" + q.strip().replace("%", "\\%").replace("_", "\\_")[:150] + "%"
            statement = statement.where(JobPosting.company_name.ilike(needle, escape="\\") | JobPosting.normalized_title.ilike(needle, escape="\\"))
        return [{"job_id": job.id, "company": job.company_name, "title": job.normalized_title,
                 "source_url": job.source_url, "employment_timing": job.employment_timing,
                 "status": job.status}
                for job in db.scalars(statement.order_by(JobPosting.updated_at.desc()).limit(80))]

    @router.get("/context/{job_id}")
    def context(job_id: str, db: Session = Depends(get_db)) -> dict:
        job = require_active_job(db, job_id)
        candidate = db.get(CandidateProfile, CANDIDATE_ID)
        profile = candidate.profile_data if candidate else {}
        return {
            "job": {"job_id": job.id, "company": job.company_name, "title": job.normalized_title,
                    "source_url": job.source_url, "employment_timing": job.employment_timing},
            "profile": {key: profile[key] for key in PROFILE_KEYS if key in profile},
            "personal_facts": facts_for_job(db, job),
            "evidence": [{"id": item.evidence_id, "claim": item.claim, "skills": item.skills}
                         for item in allowed_evidence(db)],
            "documents": _documents(db, job_id),
            "ai": {"provider": settings.extension_answer_provider, "model": settings.extension_answer_model},
        }

    @router.post("/application-sessions", status_code=201)
    def create_session(request: ApplicationSessionRequest, db: Session = Depends(get_db)) -> dict:
        job = require_active_job(db, request.job_id)
        evidence_context = answer_context(db, job, [])
        session = ExtensionApplicationSession(
            session_id=new_id("extreview"), candidate_id=CANDIDATE_ID, job_id=job.id,
            page_url=request.page_url, page_title=request.page_title, revision=1,
            questions=[item.model_dump() for item in request.questions],
            answers=initial_answers(db, job, request.questions, evidence_context),
            context_fingerprint=digest(evidence_context), evidence_ids=[],
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        db.add(session)
        db.add(AuditLogEntry.create("extension.review.created", "extension_application_session", session.session_id,
                                   {"job_id": job.id, "question_count": len(request.questions)}))
        db.flush()
        return session_to_api(session)

    @router.get("/application-sessions/{session_id}")
    def get_session(session_id: str, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, session_id)
        recover_interrupted_draft(session, settings)
        return session_to_api(session)

    @router.delete("/application-sessions/{session_id}", status_code=204)
    def delete_session(session_id: str, db: Session = Depends(get_db)) -> Response:
        begin_serialized_package_write(db)
        session = db.get(ExtensionApplicationSession, session_id)
        if session and session.candidate_id == CANDIDATE_ID:
            db.delete(session)
        return Response(status_code=204)

    @router.delete("/saved-answers/{job_id}", status_code=204)
    def forget_answers(job_id: str, db: Session = Depends(get_db)) -> Response:
        db.execute(delete(ExtensionApprovedAnswer).where(
            ExtensionApprovedAnswer.job_id == job_id, ExtensionApprovedAnswer.candidate_id == CANDIDATE_ID,
        ))
        return Response(status_code=204)

    @router.post("/application-sessions/{session_id}/draft", status_code=202)
    def draft(session_id: str, request: DraftAnswersRequest, background_tasks: BackgroundTasks,
              db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, session_id, request.revision)
        _not_drafting(session)
        if settings.extension_answer_provider == "disabled":
            raise HTTPException(status_code=409, detail="AI drafting is disabled. You can still review profile values or enter answers.")
        job = require_active_job(db, session.job_id)
        source_context = answer_context(db, job, request.evidence_ids)
        session.evidence_ids = sorted(set(request.evidence_ids))
        session.context_fingerprint = digest(source_context)
        session.answers = initial_answers(db, job, [ApplicationQuestion.model_validate(q) for q in session.questions], source_context)
        session.document_approval = {}
        session.draft_status = "running"
        session.draft_request_id = new_id("extdraft")
        session.draft_model = settings.extension_answer_model
        session.draft_error = ""
        session.draft_started_at = datetime.now(UTC)
        session.revision += 1
        db.commit()  # No SQLite write transaction is held while the model runs.
        background_tasks.add_task(process_answer_draft, settings, session_id, session.draft_request_id, request.instructions)
        return session_to_api(session)

    @router.post("/application-sessions/{session_id}/review")
    def review(session_id: str, request: ReviewAnswersRequest, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, session_id, request.revision)
        _not_drafting(session)
        validate_context(db, session)
        answers = {item["question_id"]: dict(item) for item in session.answers}
        for item in request.reviews:
            question = _question(session, item.question_id)
            answer = answers[item.question_id]
            if item.decision == "skip":
                answer.update(approved=False, skipped=True, approval_expires_at=None)
                continue
            if question_is_manual(question) or question.kind == "file":
                raise HTTPException(status_code=422, detail="Declarations and uploads require their separate manual workflow.")
            value = exact_answer(question, item.answer)
            if value is None:
                raise HTTPException(status_code=422, detail="Choose an exact enabled option or an answer within the field's format and length.")
            if question_is_sensitive(question) and not item.confirmed_sensitive:
                raise HTTPException(status_code=422, detail="Confirm this eligibility, availability or preference answer for this application.")
            if question.current_value and question.current_value != value and not item.replace_existing:
                raise HTTPException(status_code=422, detail="Explicitly approve replacing the existing value.")
            answer.update(answer=value, approved=True, skipped=False,
                          replace_existing=item.replace_existing,
                          approval_expires_at=int(time.time()) + 900,
                          reviewed_by="applicant")
            if item.remember and not question_is_sensitive(question):
                signature = question_signature(question)
                saved = db.scalar(select(ExtensionApprovedAnswer).where(
                    ExtensionApprovedAnswer.candidate_id == CANDIDATE_ID,
                    ExtensionApprovedAnswer.job_id == session.job_id,
                    ExtensionApprovedAnswer.question_signature == signature,
                ))
                if not saved:
                    saved = ExtensionApprovedAnswer(answer_id=new_id("extanswer"), candidate_id=CANDIDATE_ID,
                        job_id=session.job_id, question_signature=signature)
                    db.add(saved)
                saved.answer = value
                saved.source_ids = answer.get("source_ids", [])
                saved.context_fingerprint = session.context_fingerprint
                saved.expires_at = datetime.now(UTC) + timedelta(days=30)
        session.answers = list(answers.values())
        session.revision += 1
        db.add(AuditLogEntry.create("extension.answers.reviewed", "extension_application_session", session_id,
                                   {"question_ids": [item.question_id for item in request.reviews]}))
        db.flush()
        return session_to_api(session)

    @router.get("/application-sessions/{session_id}/fill-plan")
    def fill_plan(session_id: str, revision: int, db: Session = Depends(get_db)) -> dict:
        session = require_session(db, session_id, revision)
        validate_context(db, session)
        _not_drafting(session)
        plans = []
        for answer in session.answers:
            if not answer.get("approved") or answer.get("approval_expires_at", 0) <= time.time():
                continue
            question = _question(session, answer["question_id"])
            if not question.fillable or question_is_manual(question) or question.kind == "file":
                continue
            plans.append({"question": question.model_dump(), "answer": answer["answer"],
                          "replace_existing": answer.get("replace_existing", False),
                          "expires_at": answer["approval_expires_at"]})
        return {"session_id": session_id, "revision": session.revision,
                "page_url": session.page_url, "plans": plans}

    @router.post("/application-sessions/{session_id}/receipts")
    def receipts(session_id: str, request: FillReceiptsRequest, db: Session = Depends(get_db)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, session_id, request.revision)
        known = {item["id"] for item in session.questions}
        if any(item.question_id not in known for item in request.results):
            raise HTTPException(status_code=422, detail="Receipt contains an unknown question")
        approved = {item["question_id"]: item for item in session.answers if item.get("approved")}
        for item in request.results:
            if item.status in {"filled", "unchanged"} and (
                item.question_id not in approved or approved[item.question_id]["answer"] != item.actual_value
            ):
                raise HTTPException(status_code=422, detail="Successful receipt does not match a reviewed answer")
            if item.status == "attached" and item.question_id != session.document_approval.get("question_id"):
                raise HTTPException(status_code=422, detail="Attachment receipt does not match the approved target")
        session.receipts = (session.receipts + [{
            "at": datetime.now(UTC).isoformat(), "revision": request.revision,
            "reported_by": "browser_extension", "results": [item.model_dump() for item in request.results],
        }])[-30:]
        db.flush()
        return session_to_api(session)

    @router.post("/application-sessions/{session_id}/approve-document")
    def approve_document(session_id: str, request: DocumentApprovalRequest,
                         db: Session = Depends(get_db), storage: ArtifactStorageService = Depends(get_storage)) -> dict:
        begin_serialized_package_write(db)
        session = require_session(db, session_id, request.revision)
        validate_context(db, session)
        _not_drafting(session)
        question = _question(session, request.question_id)
        if question.kind != "file" or not question.fillable or question.purpose == "unknown" or question_is_manual(question):
            raise HTTPException(status_code=422, detail="Choose an explicitly identified, supported upload field.")
        if question.current_value and not request.replace_existing:
            raise HTTPException(status_code=422, detail="Approve replacing the existing attachment first.")
        artifact = db.get(StoredArtifact, request.artifact_id)
        if not artifact or artifact.deleted_at is not None or artifact.sha256 != request.sha256:
            raise HTTPException(status_code=409, detail="This document changed or was removed. Refresh and review it again.")
        if artifact.size_bytes > 10 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="This attachment exceeds the 10 MB extension limit.")
        content = storage.read_bytes(artifact.storage_uri)
        if not content.startswith(b"%PDF-") or hashlib.sha256(content).hexdigest() != request.sha256:
            raise HTTPException(status_code=409, detail="The stored file is not the reviewed PDF.")
        if is_profile_pdf_resume_artifact(artifact, CANDIDATE_ID):
            if question.purpose != "resume":
                raise HTTPException(status_code=422, detail="A resume cannot be attached to a different document field.")
            application = db.scalar(select(Application).where(Application.job_id == session.job_id, Application.candidate_id == CANDIDATE_ID))
            if not application:
                application = Application(application_id=new_id("app"), candidate_id=CANDIDATE_ID, job_id=session.job_id)
                db.add(application)
                db.flush()
            # A separate approved package freezes this exact static resume for this job.
            package = ApplicationPackage(application_package_id=new_id("pkg"), application_id=application.application_id,
                package_version=next_application_package_version(db, application.application_id),
                approved_for_extension=True, approved_at=datetime.now(UTC), status="reviewed")
            db.add(package)
            db.flush()
            row = ApplicationPackageDocument(application_package_document_id=new_id("pkgdoc"),
                application_package_id=package.application_package_id, artifact_id=artifact.artifact_id,
                document_type="resume", approved_for_upload=True, required=False)
            db.add(row)
            db.flush()
        else:
            row = db.scalar(select(ApplicationPackageDocument)
                .join(ApplicationPackage, ApplicationPackage.application_package_id == ApplicationPackageDocument.application_package_id)
                .join(Application, Application.application_id == ApplicationPackage.application_id)
                .where(Application.job_id == session.job_id, ApplicationPackageDocument.artifact_id == artifact.artifact_id,
                       ApplicationPackageDocument.document_type == question.purpose,
                       ApplicationPackageDocument.approved_for_upload.is_(True)))
            if not row or not _package_pdf_is_cleared(db, row, artifact):
                raise HTTPException(status_code=403, detail="The selected document is not an approved package document for this job and purpose.")
            package = db.get(ApplicationPackage, row.application_package_id)
        session.revision += 1
        session.document_approval = {
            "package_document_id": row.application_package_document_id, "artifact_id": artifact.artifact_id,
            "sha256": artifact.sha256, "question_id": question.id, "revision": session.revision,
        }
        db.add(AuditLogEntry.create("extension.document.approved", "application_package", package.application_package_id,
            {"session_id": session_id, "artifact_id": artifact.artifact_id, "sha256": artifact.sha256,
             "question_id": question.id, "purpose": question.purpose, "approval_source": "manual_review"}))
        token = create_extension_download_token(settings, package_id=package.application_package_id,
            document_id=f"{session_id}:{row.application_package_document_id}:{artifact.sha256}")
        db.flush()
        return {
            "session": session_to_api(session), "file_name": artifact.original_file_name,
            "sha256": artifact.sha256,
            "download_url": f"/api/extension/application-sessions/{session_id}/document?token={token}",
            "plan": {"question": question.model_dump(), "answer": artifact.original_file_name,
                     "replace_existing": request.replace_existing, "expires_at": int(time.time()) + 300},
        }

    @router.get("/application-sessions/{session_id}/document")
    def download_document(session_id: str, token: str, db: Session = Depends(get_db),
                          storage: ArtifactStorageService = Depends(get_storage)) -> Response:
        session = require_session(db, session_id)
        validate_context(db, session)
        approval = session.document_approval
        signed = verify_extension_download_token(settings, token)
        row = db.get(ApplicationPackageDocument, approval.get("package_document_id")) if approval else None
        artifact = db.get(StoredArtifact, approval.get("artifact_id")) if approval else None
        if (
            not signed or not row or not artifact
            or signed.package_id != row.application_package_id
            or signed.document_id != f"{session_id}:{row.application_package_document_id}:{artifact.sha256}"
            or artifact.sha256 != approval.get("sha256")
            or not _package_pdf_is_cleared(db, row, artifact)
        ):
            raise HTTPException(status_code=403, detail="Document approval expired, changed or was revoked.")
        _package, application, _job = require_active_package_job(db, row.application_package_id)
        if application.job_id != session.job_id:
            raise HTTPException(status_code=403, detail="Document is not approved for this application.")
        content = storage.read_bytes(artifact.storage_uri)
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise HTTPException(status_code=409, detail="The approved document bytes changed.")
        return Response(content=content, media_type="application/pdf", headers={
            **attachment_headers(artifact.original_file_name), "Cache-Control": "no-store",
        })

    return router
