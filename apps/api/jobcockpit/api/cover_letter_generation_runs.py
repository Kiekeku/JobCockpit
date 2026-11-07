from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from jobcockpit.api.background_worker_leases import begin_background_worker_lease_transaction
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    CoverLetterGenerationRun,
    JobPosting,
)
from jobcockpit.db.session import session_scope
from jobcockpit.utils.ids import new_id


COVER_LETTER_BACKGROUND_WORKER_LEASE = "cover_letter_generation_queue"
ACTIVE_COVER_LETTER_GENERATION_STATUSES = {
    "queued",
    "running",
    "retry_scheduled",
    "waiting_for_profile",
}
CLAIMABLE_COVER_LETTER_GENERATION_STATUSES = {"queued", "retry_scheduled"}
COVER_LETTER_GENERATION_MAX_ATTEMPTS = 3
COVER_LETTER_GENERATION_RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=30))
COVER_LETTER_GENERATION_STALE_AFTER = timedelta(hours=1)
COVER_LETTER_GENERATION_STALE_ERROR = "cover_letter_worker_stale"


def queue_manual_cover_letter(db, job_id, request, *, candidate_id, target_package_id=None, response_view="workspace"):
    from jobcockpit.api.application_documents import begin_serialized_package_write
    from jobcockpit.api.job_access import require_active_job
    from jobcockpit.api.profile_routes import resolve_cover_letter_resume_artifact

    begin_serialized_package_write(db)
    job = require_active_job(db, job_id)
    if job.status != "active":
        raise HTTPException(status_code=409, detail="Cover-letter generation requires an active job")
    if db.get(CandidateProfile, candidate_id) is None:
        raise HTTPException(status_code=409, detail="Create a candidate profile before generating a cover letter.")
    resume = resolve_cover_letter_resume_artifact(
        db, candidate_id, request.resume_artifact_id if request else None,
    )
    if target_package_id:
        from jobcockpit.api.job_access import require_active_package_job
        package, _, package_job = require_active_package_job(db, target_package_id)
        if package_job.id != job.id or package.status != "draft" or package.approved_for_extension:
            raise HTTPException(status_code=409, detail="Cover-letter generation requires a draft package for this job")
    if request is None and target_package_id is None:
        active = active_cover_letter_generation_run(db, job.id, posting_snapshot_hash=job.posting_snapshot_hash)
        if active:
            return {"run": active.to_api(), "queued": False,
                    "result_url": f"/api/cover-letter-generation-runs/{active.generation_run_id}/result?view={response_view}"}
    payload = {
        "target_package_id": target_package_id,
        "request_id": (request.request_id if request else None) or new_id("manual"),
        "requested_by_user": True,
        "resume_artifact_id": resume.artifact_id if resume else None,
        "resume_sha256": resume.sha256 if resume else None,
    }
    run, queued = enqueue_cover_letter_generation(
        job, db, requested_by_user=True, request_payload=payload,
    )
    db.flush()
    return {"run": run.to_api(), "queued": queued,
            "result_url": f"/api/cover-letter-generation-runs/{run.generation_run_id}/result?view={response_view}"}


class CoverLetterGenerationProcessingError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def validate_generation_resume(db, run):
    from jobcockpit.api.profile_routes import CANDIDATE_ID, resolve_cover_letter_resume_artifact

    request = run.request_payload or {}
    artifact_id = request.get("resume_artifact_id")
    if artifact_id:
        artifact = resolve_cover_letter_resume_artifact(db, CANDIDATE_ID, artifact_id)
        if artifact is None or artifact.sha256 != request.get("resume_sha256"):
            raise CoverLetterGenerationProcessingError(
                "resume_changed", "The selected resume changed after generation was queued",
                retryable=False,
            )


def enqueue_cover_letter_generation(
    job: JobPosting,
    db: Session,
    *,
    requested_by_user: bool = False,
    request_payload: dict | None = None,
) -> tuple[CoverLetterGenerationRun, bool]:
    if request_payload and request_payload.get("request_id"):
        previous = db.scalar(select(CoverLetterGenerationRun).where(
            CoverLetterGenerationRun.job_id == job.id,
            CoverLetterGenerationRun.request_payload["request_id"].as_string() == request_payload["request_id"],
        ))
        if previous:
            if previous.request_payload != request_payload or previous.posting_snapshot_hash != job.posting_snapshot_hash:
                raise HTTPException(status_code=409, detail="This generation request belongs to different inputs")
            return previous, False
    current_snapshot = db.execute(
        select(CoverLetterGenerationRun)
        .where(
            CoverLetterGenerationRun.job_id == job.id,
            CoverLetterGenerationRun.posting_snapshot_hash == job.posting_snapshot_hash,
        )
        .order_by(
            CoverLetterGenerationRun.created_at.desc(),
            CoverLetterGenerationRun.generation_run_id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()

    active = db.execute(
        select(CoverLetterGenerationRun)
        .where(
            CoverLetterGenerationRun.job_id == job.id,
            CoverLetterGenerationRun.status.in_(ACTIVE_COVER_LETTER_GENERATION_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active and active.posting_snapshot_hash == job.posting_snapshot_hash:
        if request_payload and not active.request_payload and active.status in {"queued", "waiting_for_profile"}:
            active.request_payload = request_payload
            active.result = {**(active.result or {}), "requested_by_user": True}
            active.status = "queued"
            active.updated_at = datetime.now(UTC)
            return active, False
        if request_payload is not None and active.request_payload != request_payload:
            raise HTTPException(status_code=409, detail="A cover-letter request is already in progress for this job")
        return active, False

    if active and active.request_payload and request_payload is None:
        # A refreshed snapshot replaces the run, not the user's resume/package
        # choices. A new request ID keeps retries of the original unambiguous.
        # The worker revalidates the pinned inputs before committing documents.
        request_payload = {**active.request_payload, "request_id": new_id("manual")}
        requested_by_user = True

    current_snapshot_is_terminal = (
        current_snapshot is not None and current_snapshot.status in {"succeeded", "failed"}
    )
    retry_failed_snapshot = bool(
        requested_by_user
        and current_snapshot is not None
        and (current_snapshot.status == "failed" or request_payload is not None)
    )
    if current_snapshot_is_terminal and active is None and not retry_failed_snapshot:
        return current_snapshot, False

    run = CoverLetterGenerationRun(
        generation_run_id=new_id("cover_letter"),
        job_id=job.id,
        posting_snapshot_hash=job.posting_snapshot_hash,
        status="queued",
        result={"requested_by_user": True} if requested_by_user else {},
        request_payload=request_payload or {},
    )
    try:
        with db.begin_nested():
            if active:
                now = datetime.now(UTC)
                replacement_generation_run_id = (
                    current_snapshot.generation_run_id
                    if current_snapshot_is_terminal
                    else run.generation_run_id
                )
                active.status = "cancelled"
                active.next_attempt_at = None
                active.finished_at = now
                active.error_code = "posting_snapshot_superseded"
                active.error_message = "A newer posting snapshot replaced this request"
                active.retryable = False
                active.worker_id = None
                active.claim_token = None
                active.supervisor_lease_token = None
                active.updated_at = now
                db.add(
                    AuditLogEntry.create(
                        "cover_letter.generation_cancelled",
                        "cover_letter_generation_run",
                        active.generation_run_id,
                        payload={
                            "job_id": job.id,
                            "error_code": "posting_snapshot_superseded",
                            "replacement_generation_run_id": replacement_generation_run_id,
                        },
                    )
                )
                db.flush()
            if current_snapshot_is_terminal and not retry_failed_snapshot:
                return current_snapshot, False
            db.add(run)
            db.add(
                AuditLogEntry.create(
                    "cover_letter.generation_queued",
                    "job_posting",
                    job.id,
                    payload={
                        "generation_run_id": run.generation_run_id,
                        "posting_snapshot_hash": job.posting_snapshot_hash,
                        "requested_by_user": requested_by_user,
                    },
                )
            )
            db.flush()
    except IntegrityError:
        active = db.execute(
            select(CoverLetterGenerationRun)
            .where(
                CoverLetterGenerationRun.job_id == job.id,
                CoverLetterGenerationRun.status.in_(ACTIVE_COVER_LETTER_GENERATION_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()
        if active:
            if request_payload is not None and active.request_payload != request_payload:
                raise HTTPException(status_code=409, detail="A different generation request is already active")
            return active, False
        raise
    return run, True


def cover_letter_generation_requested_by_user(
    run: CoverLetterGenerationRun,
) -> bool:
    return bool((run.request_payload or {}).get("requested_by_user") or (run.result or {}).get("requested_by_user"))


def cancel_active_cover_letter_generations(
    job: JobPosting,
    db: Session,
    *,
    error_code: str,
    error_message: str,
) -> int:
    active_runs = list(
        db.execute(
            select(CoverLetterGenerationRun).where(
                CoverLetterGenerationRun.job_id == job.id,
                CoverLetterGenerationRun.status.in_(ACTIVE_COVER_LETTER_GENERATION_STATUSES),
            )
        ).scalars()
    )
    now = datetime.now(UTC)
    for run in active_runs:
        run.status = "cancelled"
        run.next_attempt_at = None
        run.finished_at = now
        run.error_code = error_code
        run.error_message = error_message
        run.retryable = False
        run.worker_id = None
        run.claim_token = None
        run.supervisor_lease_token = None
        run.updated_at = now
        db.add(
            AuditLogEntry.create(
                "cover_letter.generation_cancelled",
                "cover_letter_generation_run",
                run.generation_run_id,
                payload={
                    "job_id": job.id,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        )
    return len(active_runs)


def reconcile_cover_letter_generation(
    job: JobPosting,
    db: Session,
    *,
    should_generate: bool,
) -> tuple[CoverLetterGenerationRun | None, bool]:
    """Make durable queue state match the job's current generation eligibility."""
    if should_generate:
        return enqueue_cover_letter_generation(job, db)
    if job.status == "active":
        active = active_cover_letter_generation_run(db, job.id)
        if active and cover_letter_generation_requested_by_user(active):
            return enqueue_cover_letter_generation(
                job,
                db,
                requested_by_user=True,
            )
    error_code = "job_not_active" if job.status != "active" else "cover_letter_not_required"
    error_message = (
        "The job is no longer active"
        if job.status != "active"
        else "The posting no longer requires a cover letter"
    )
    cancel_active_cover_letter_generations(
        job,
        db,
        error_code=error_code,
        error_message=error_message,
    )
    return latest_cover_letter_generation_run(db, job.id), False


def latest_cover_letter_generation_run(
    db: Session, job_id: str
) -> CoverLetterGenerationRun | None:
    return db.execute(
        select(CoverLetterGenerationRun)
        .where(CoverLetterGenerationRun.job_id == job_id)
        .order_by(
            CoverLetterGenerationRun.created_at.desc(),
            CoverLetterGenerationRun.generation_run_id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def active_cover_letter_generation_run(
    db: Session,
    job_id: str,
    *,
    posting_snapshot_hash: str | None = None,
) -> CoverLetterGenerationRun | None:
    predicates = [
        CoverLetterGenerationRun.job_id == job_id,
        CoverLetterGenerationRun.status.in_(ACTIVE_COVER_LETTER_GENERATION_STATUSES),
    ]
    if posting_snapshot_hash is not None:
        predicates.append(
            CoverLetterGenerationRun.posting_snapshot_hash == posting_snapshot_hash
        )
    return db.execute(
        select(CoverLetterGenerationRun)
        .where(*predicates)
        .order_by(
            CoverLetterGenerationRun.created_at.desc(),
            CoverLetterGenerationRun.generation_run_id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def mark_cover_letter_generation_succeeded(
    run: CoverLetterGenerationRun,
    db: Session,
    *,
    result: dict,
    completion_source: str,
) -> CoverLetterGenerationRun:
    now = datetime.now(UTC)
    run.status = "succeeded"
    run.finished_at = now
    run.updated_at = now
    run.next_attempt_at = None
    run.error_code = None
    run.error_message = None
    run.retryable = False
    run.worker_id = None
    run.claim_token = None
    run.supervisor_lease_token = None
    run.result = dict(result)
    db.add(
        AuditLogEntry.create(
            "cover_letter.generation_succeeded",
            "cover_letter_generation_run",
            run.generation_run_id,
            payload={
                "job_id": run.job_id,
                "completion_source": completion_source,
                **run.result,
            },
        )
    )
    return run


def satisfy_cover_letter_generation(
    job: JobPosting,
    db: Session,
    *,
    result: dict,
    completion_source: str,
) -> CoverLetterGenerationRun | None:
    run = active_cover_letter_generation_run(
        db,
        job.id,
        posting_snapshot_hash=job.posting_snapshot_hash,
    )
    if run is None:
        run = db.execute(
            select(CoverLetterGenerationRun)
            .where(
                CoverLetterGenerationRun.job_id == job.id,
                CoverLetterGenerationRun.posting_snapshot_hash == job.posting_snapshot_hash,
                CoverLetterGenerationRun.status == "failed",
            )
            .order_by(
                CoverLetterGenerationRun.created_at.desc(),
                CoverLetterGenerationRun.generation_run_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if run is None:
            return None
    return mark_cover_letter_generation_succeeded(
        run,
        db,
        result=result,
        completion_source=completion_source,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _claim_cover_letter_generation_run(
    db: Session,
    generation_run_id: str,
    *,
    worker_id: str | None,
    supervisor_lease_token: str | None,
) -> CoverLetterGenerationRun | None:
    if not begin_background_worker_lease_transaction(
        db,
        worker_id,
        supervisor_lease_token,
        lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
    ):
        return None
    now = datetime.now(UTC)
    claim_token = new_id("claim")
    result = db.execute(
        update(CoverLetterGenerationRun)
        .where(
            CoverLetterGenerationRun.generation_run_id == generation_run_id,
            CoverLetterGenerationRun.status.in_(CLAIMABLE_COVER_LETTER_GENERATION_STATUSES),
            or_(
                CoverLetterGenerationRun.next_attempt_at.is_(None),
                CoverLetterGenerationRun.next_attempt_at <= now,
            ),
        )
        .values(
            status="running",
            attempts=CoverLetterGenerationRun.attempts + 1,
            next_attempt_at=None,
            started_at=now,
            finished_at=None,
            error_code=None,
            error_message=None,
            retryable=False,
            worker_id=worker_id,
            claim_token=claim_token,
            supervisor_lease_token=supervisor_lease_token,
            updated_at=now,
        )
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:
        return None
    run = db.get(CoverLetterGenerationRun, generation_run_id)
    if not run:
        return None
    db.refresh(run)
    db.add(
        AuditLogEntry.create(
            "cover_letter.generation_started",
            "cover_letter_generation_run",
            run.generation_run_id,
            payload={"job_id": run.job_id, "attempts": run.attempts, "worker_id": worker_id},
        )
    )
    db.flush()
    return run


def recover_stale_cover_letter_generation_runs(
    settings: Settings,
    *,
    active_worker_id: str | None = None,
    active_lease_token: str | None = None,
    recover_orphaned: bool = False,
    recover_owned_by: str | None = None,
    required_worker_id: str | None = None,
    required_lease_token: str | None = None,
) -> int:
    now = datetime.now(UTC)
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db,
            required_worker_id,
            required_lease_token,
            lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
        ):
            return 0
        running = list(
            db.execute(
                select(CoverLetterGenerationRun).where(
                    CoverLetterGenerationRun.status == "running"
                )
            ).scalars()
        )
        runs = [
            run
            for run in running
            if (
                run.started_at is None
                or _utc(run.started_at) <= now - COVER_LETTER_GENERATION_STALE_AFTER
                or (
                    recover_orphaned
                    and active_worker_id is not None
                    and (
                        run.worker_id != active_worker_id
                        or run.supervisor_lease_token != active_lease_token
                    )
                )
                or (recover_owned_by is not None and run.worker_id == recover_owned_by)
            )
        ]
        for run in runs:
            run.status = (
                "retry_scheduled"
                if run.attempts < COVER_LETTER_GENERATION_MAX_ATTEMPTS
                else "failed"
            )
            run.next_attempt_at = now if run.status == "retry_scheduled" else None
            run.finished_at = now if run.status == "failed" else None
            run.error_code = COVER_LETTER_GENERATION_STALE_ERROR
            run.error_message = "Cover-letter worker stopped before completing the request"
            run.retryable = True
            run.worker_id = None
            run.claim_token = None
            run.supervisor_lease_token = None
            run.updated_at = now
        return len(runs)


def bootstrap_cover_letter_generation_worker(
    settings: Settings,
    *,
    worker_id: str,
    lease_token: str,
) -> int:
    return recover_stale_cover_letter_generation_runs(
        settings,
        active_worker_id=worker_id,
        active_lease_token=lease_token,
        recover_orphaned=True,
        required_worker_id=worker_id,
        required_lease_token=lease_token,
    )


def claim_next_cover_letter_generation(
    settings: Settings,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> str | None:
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db,
            worker_id,
            supervisor_lease_token,
            lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
        ):
            return None
        candidate_id = db.execute(
            select(CoverLetterGenerationRun.generation_run_id)
            .where(
                CoverLetterGenerationRun.status.in_(
                    CLAIMABLE_COVER_LETTER_GENERATION_STATUSES
                ),
                or_(
                    CoverLetterGenerationRun.next_attempt_at.is_(None),
                    CoverLetterGenerationRun.next_attempt_at <= datetime.now(UTC),
                ),
            )
            .order_by(
                CoverLetterGenerationRun.created_at.asc(),
                CoverLetterGenerationRun.generation_run_id.asc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if not candidate_id:
            return None
        run = _claim_cover_letter_generation_run(
            db,
            candidate_id,
            worker_id=worker_id,
            supervisor_lease_token=supervisor_lease_token,
        )
        return run.generation_run_id if run else None


def public_cover_letter_generation_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, CoverLetterGenerationProcessingError):
        return error.error_code, str(error)
    if isinstance(error, HTTPException):
        return "cover_letter_generation_rejected", "Cover-letter generation was rejected"
    raw_code = re.sub(r"[^A-Za-z0-9_.-]+", "_", error.__class__.__name__)[:128]
    return raw_code or "cover_letter_generation_error", "Cover-letter generation failed"


def cover_letter_generation_error_is_retryable(error: Exception) -> bool:
    if isinstance(error, CoverLetterGenerationProcessingError):
        return error.retryable
    if isinstance(error, HTTPException):
        return error.status_code in {408, 429} or error.status_code >= 500
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OperationalError):
        return "locked" in str(error).casefold() or "busy" in str(error).casefold()
    return False


def record_cover_letter_generation_failure(
    generation_run_id: str,
    settings: Settings,
    error: Exception,
    *,
    worker_id: str | None,
    expected_attempt: int,
    expected_claim_token: str,
    expected_supervisor_lease_token: str | None,
    candidate_id: str,
) -> dict | None:
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db,
            worker_id,
            expected_supervisor_lease_token,
            lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
        ):
            return None
        run = db.get(CoverLetterGenerationRun, generation_run_id)
        if not run:
            return None
        if (
            run.status != "running"
            or run.worker_id != worker_id
            or run.attempts != expected_attempt
            or run.claim_token != expected_claim_token
            or run.supervisor_lease_token != expected_supervisor_lease_token
        ):
            return run.to_api()
        error_code, error_message = public_cover_letter_generation_error(error)
        profile_was_missing = error_code == "candidate_profile_missing"
        profile_is_now_available = (
            profile_was_missing and db.get(CandidateProfile, candidate_id) is not None
        )
        waiting_for_profile = profile_was_missing and not profile_is_now_available
        retryable = cover_letter_generation_error_is_retryable(error)
        will_retry = retryable and run.attempts < COVER_LETTER_GENERATION_MAX_ATTEMPTS
        now = datetime.now(UTC)
        if profile_was_missing:
            run.attempts = max(0, run.attempts - 1)
        run.status = (
            "queued"
            if profile_is_now_available
            else "waiting_for_profile"
            if waiting_for_profile
            else "retry_scheduled"
            if will_retry
            else "failed"
        )
        run.next_attempt_at = (
            now + COVER_LETTER_GENERATION_RETRY_DELAYS[max(0, run.attempts - 1)]
            if will_retry
            else None
        )
        run.finished_at = (
            None if will_retry or waiting_for_profile or profile_is_now_available else now
        )
        run.error_code = None if profile_is_now_available else error_code
        run.error_message = None if profile_is_now_available else error_message
        run.retryable = False if profile_is_now_available else retryable
        run.worker_id = None
        run.claim_token = None
        run.supervisor_lease_token = None
        run.updated_at = now
        db.add(
            AuditLogEntry.create(
                (
                    "cover_letter.generation_prerequisite_resolved"
                    if profile_is_now_available
                    else "cover_letter.generation_waiting_for_profile"
                    if waiting_for_profile
                    else "cover_letter.generation_retry_scheduled"
                    if will_retry
                    else "cover_letter.generation_failed"
                ),
                "cover_letter_generation_run",
                run.generation_run_id,
                payload={
                    "job_id": run.job_id,
                    "error_code": run.error_code,
                    "retryable": run.retryable,
                },
            )
        )
        db.flush()
        return run.to_api()


def wake_cover_letter_generations_waiting_for_profile(db: Session) -> int:
    now = datetime.now(UTC)
    result = db.execute(
        update(CoverLetterGenerationRun)
        .where(CoverLetterGenerationRun.status == "waiting_for_profile")
        .values(
            status="queued",
            next_attempt_at=None,
            finished_at=None,
            error_code=None,
            error_message=None,
            retryable=False,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)
