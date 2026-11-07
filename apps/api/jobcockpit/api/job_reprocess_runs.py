from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from jobcockpit.api.background_worker_leases import (
    begin_background_worker_lease_transaction,
)
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, JobPosting, JobReprocessRun
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.fit_analysis import FitAnalysisUnavailableError
from jobcockpit.utils.ids import new_id


ACTIVE_JOB_REPROCESS_STATUSES = {"queued", "running", "retry_scheduled"}
CLAIMABLE_JOB_REPROCESS_STATUSES = {"queued", "retry_scheduled"}
JOB_REPROCESS_TYPE_FULL = "full_reprocess"
JOB_REPROCESS_TYPE_FIT_ANALYSIS = "fit_analysis"
JOB_REPROCESS_STALE_AFTER = timedelta(hours=1)
JOB_REPROCESS_STALE_ERROR = "reprocess_worker_stale"
JOB_REPROCESS_MAX_ATTEMPTS = 3
JOB_REPROCESS_RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=30))
JOB_REPROCESS_TRANSITIONS = {
    "queued": frozenset({"running"}),
    "retry_scheduled": frozenset({"running"}),
    "running": frozenset({"retry_scheduled", "succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class JobReprocessClaimLost(RuntimeError):
    pass


class JobReprocessProcessingError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def ensure_job_reprocess_transition(current: str, next_status: str) -> None:
    if next_status not in JOB_REPROCESS_TRANSITIONS.get(current, frozenset()):
        raise RuntimeError(f"Invalid job reprocess transition: {current} -> {next_status}")


def public_job_reprocess_error(error: Exception, error_code: str | None = None) -> tuple[str, str]:
    if isinstance(error, JobReprocessProcessingError):
        return error.error_code, str(error)
    if isinstance(error, FitAnalysisUnavailableError):
        if str(error) == "fit_analysis_provider_disabled":
            return "fit_analysis_provider_disabled", "Fit analysis provider is disabled"
        return "fit_analysis_unavailable", "Fit analysis could not be completed"
    if isinstance(error, HTTPException):
        return "job_reprocess_rejected", "Job reprocessing was rejected"
    raw_code = error_code or error.__class__.__name__
    code = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_code)).strip("._-")[:128]
    if not code:
        code = "job_reprocess_error"
    return code, f"Background job processing failed ({code})"


def latest_job_reprocess_run(db: Session, job_id: str) -> JobReprocessRun | None:
    return db.execute(
        select(JobReprocessRun)
        .where(JobReprocessRun.job_id == job_id)
        .order_by(JobReprocessRun.created_at.desc(), JobReprocessRun.reprocess_run_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_job_reprocess_runs_by_job(db: Session, job_ids: list[str]) -> dict[str, JobReprocessRun]:
    if not job_ids:
        return {}
    ranked = select(
        JobReprocessRun.reprocess_run_id,
        func.row_number().over(
            partition_by=JobReprocessRun.job_id,
            order_by=(JobReprocessRun.created_at.desc(), JobReprocessRun.reprocess_run_id.desc()),
        ).label("position"),
    ).where(JobReprocessRun.job_id.in_(job_ids)).subquery()
    runs = db.scalars(select(JobReprocessRun).join(
        ranked, ranked.c.reprocess_run_id == JobReprocessRun.reprocess_run_id,
    ).where(ranked.c.position == 1))
    return {run.job_id: run for run in runs}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def job_reprocess_run_is_stale(run: JobReprocessRun, now: datetime | None = None) -> bool:
    if run.status != "running":
        return False
    now = now or datetime.now(UTC)
    last_activity = run.started_at or run.updated_at or run.created_at
    return _utc(last_activity) <= now - JOB_REPROCESS_STALE_AFTER


def fail_stale_job_reprocess_runs(
    job_id: str, db: Session, now: datetime | None = None
) -> list[JobReprocessRun]:
    now = now or datetime.now(UTC)
    running_runs = db.execute(
        select(JobReprocessRun).where(
            JobReprocessRun.job_id == job_id,
            JobReprocessRun.status == "running",
        )
    ).scalars()
    stale_runs: list[JobReprocessRun] = []
    for run in running_runs:
        if not job_reprocess_run_is_stale(run, now):
            continue
        ensure_job_reprocess_transition(run.status, "failed")
        run.status = "failed"
        run.error_code = JOB_REPROCESS_STALE_ERROR
        run.error_message = JOB_REPROCESS_STALE_ERROR
        run.retryable = True
        run.next_attempt_at = None
        run.worker_id = None
        run.claim_token = None
        run.supervisor_lease_token = None
        run.finished_at = now
        run.updated_at = now
        stale_runs.append(run)
        db.add(
            AuditLogEntry.create(
                "job.reprocess_stale",
                "job_reprocess_run",
                run.reprocess_run_id,
                payload={
                    "job_id": run.job_id,
                    "type": run.run_type,
                    "error_message": JOB_REPROCESS_STALE_ERROR,
                },
            )
        )
    return stale_runs


def active_job_reprocess_run(db: Session, job_id: str) -> JobReprocessRun | None:
    return db.execute(
        select(JobReprocessRun)
        .where(
            JobReprocessRun.job_id == job_id,
            JobReprocessRun.status.in_(ACTIVE_JOB_REPROCESS_STATUSES),
        )
        .order_by(JobReprocessRun.created_at.desc(), JobReprocessRun.reprocess_run_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _reuse_active_job_reprocess(
    run: JobReprocessRun, db: Session, requested_type: str,
) -> tuple[JobReprocessRun, bool]:
    if requested_type == JOB_REPROCESS_TYPE_FULL and run.run_type == JOB_REPROCESS_TYPE_FIT_ANALYSIS:
        # A new import may already have queued fitting. A requested full
        # refresh can widen unclaimed work, but must not change a worker's
        # running claim or the kind of result that it is computing.
        promoted = db.execute(
            update(JobReprocessRun)
            .where(
                JobReprocessRun.reprocess_run_id == run.reprocess_run_id,
                JobReprocessRun.run_type == JOB_REPROCESS_TYPE_FIT_ANALYSIS,
                JobReprocessRun.status.in_(CLAIMABLE_JOB_REPROCESS_STATUSES),
            )
            .values(run_type=JOB_REPROCESS_TYPE_FULL, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session="fetch")
        )
        db.refresh(run)
        if promoted.rowcount == 1:
            db.add(AuditLogEntry.create(
                "job.reprocess_upgraded", "job_reprocess_run", run.reprocess_run_id,
                payload={"job_id": run.job_id, "type": JOB_REPROCESS_TYPE_FULL},
            ))
    return run, False


def enqueue_job_reprocess(
    job: JobPosting,
    db: Session,
    *,
    run_type: str = JOB_REPROCESS_TYPE_FULL,
) -> tuple[JobReprocessRun, bool]:
    job_id = job.id
    stale_runs = fail_stale_job_reprocess_runs(job_id, db)
    if stale_runs:
        db.flush()
    active_run = active_job_reprocess_run(db, job_id)
    if active_run:
        return _reuse_active_job_reprocess(active_run, db, run_type)

    run = JobReprocessRun(
        reprocess_run_id=new_id("reprocess"),
        job_id=job_id,
        run_type=run_type,
        status="queued",
        result={},
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.add(
                AuditLogEntry.create(
                    "job.reprocess_queued",
                    "job_posting",
                    job_id,
                    payload={
                        "reprocess_run_id": run.reprocess_run_id,
                        "type": run.run_type,
                    },
                )
            )
            db.flush()
    except IntegrityError:
        active_run = active_job_reprocess_run(db, job_id)
        if active_run:
            return _reuse_active_job_reprocess(active_run, db, run_type)
        raise
    return run, True


def claim_job_reprocess_run(
    db: Session,
    reprocess_run_id: str,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> JobReprocessRun | None:
    if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
        return None
    now = datetime.now(UTC)
    claim_token = new_id("claim")
    result = db.execute(
        update(JobReprocessRun)
        .where(
            JobReprocessRun.reprocess_run_id == reprocess_run_id,
            JobReprocessRun.status.in_(CLAIMABLE_JOB_REPROCESS_STATUSES),
            or_(
                JobReprocessRun.next_attempt_at.is_(None),
                JobReprocessRun.next_attempt_at <= now,
            ),
        )
        .values(
            status="running",
            attempts=JobReprocessRun.attempts + 1,
            started_at=now,
            finished_at=None,
            next_attempt_at=None,
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
    run = db.get(JobReprocessRun, reprocess_run_id)
    if not run:
        return None
    db.refresh(run)
    db.add(
        AuditLogEntry.create(
            "job.reprocess_started",
            "job_reprocess_run",
            run.reprocess_run_id,
            payload={
                "job_id": run.job_id,
                "attempts": run.attempts,
                "type": run.run_type,
                "worker_id": worker_id,
            },
        )
    )
    db.flush()
    return run


def claim_next_job_reprocess(
    settings: Settings,
    reprocess_run_id: str | None = None,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> str | None:
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
            return None
        statement = (
            select(JobReprocessRun.reprocess_run_id)
            .join(JobPosting, JobPosting.id == JobReprocessRun.job_id)
            .where(
                JobReprocessRun.status.in_(CLAIMABLE_JOB_REPROCESS_STATUSES),
                or_(
                    JobReprocessRun.next_attempt_at.is_(None),
                    JobReprocessRun.next_attempt_at <= datetime.now(UTC),
                ),
                text("json_extract(job_postings.source, '$.merged_into_job_id') IS NULL"),
            )
        )
        if reprocess_run_id:
            statement = statement.where(JobReprocessRun.reprocess_run_id == reprocess_run_id)
        else:
            statement = statement.order_by(
                JobReprocessRun.created_at.asc(),
                JobReprocessRun.reprocess_run_id.asc(),
            )
        candidate_run_id = db.execute(statement.limit(1)).scalar_one_or_none()
        if not candidate_run_id:
            return None
        run = claim_job_reprocess_run(
            db,
            candidate_run_id,
            worker_id=worker_id,
            supervisor_lease_token=supervisor_lease_token,
        )
        return run.reprocess_run_id if run else None


def record_job_reprocess_failure(
    reprocess_run_id: str,
    settings: Settings,
    error: Exception,
    *,
    retryable: bool,
    error_code: str | None = None,
    worker_id: str | None = None,
    expected_attempt: int | None = None,
    expected_claim_token: str | None = None,
    expected_supervisor_lease_token: str | None = None,
) -> dict | None:
    with session_scope(settings) as db:
        if expected_supervisor_lease_token is not None and not (
            begin_background_worker_lease_transaction(
                db, worker_id, expected_supervisor_lease_token
            )
        ):
            return None
        run = db.get(JobReprocessRun, reprocess_run_id)
        if not run:
            return None
        if (
            run.status != "running"
            or (worker_id is not None and run.worker_id != worker_id)
            or (expected_attempt is not None and run.attempts != expected_attempt)
            or (expected_claim_token is not None and run.claim_token != expected_claim_token)
            or (
                expected_supervisor_lease_token is not None
                and run.supervisor_lease_token != expected_supervisor_lease_token
            )
        ):
            return run.to_api()
        if expected_supervisor_lease_token is not None:
            from jobcockpit.api.background_worker_leases import (
                background_worker_lease_is_valid,
            )

            if not background_worker_lease_is_valid(
                db, run.worker_id or "", expected_supervisor_lease_token
            ):
                return run.to_api()
        attempt_worker_id = run.worker_id
        code, error_message = public_job_reprocess_error(error, error_code)
        now = datetime.now(UTC)
        will_retry = retryable and run.attempts < JOB_REPROCESS_MAX_ATTEMPTS
        next_status = "retry_scheduled" if will_retry else "failed"
        ensure_job_reprocess_transition(run.status, next_status)
        run.status = next_status
        run.error_code = code
        run.error_message = error_message
        run.retryable = retryable
        run.worker_id = None
        run.claim_token = None
        run.supervisor_lease_token = None
        if will_retry:
            delay = JOB_REPROCESS_RETRY_DELAYS[max(0, run.attempts - 1)]
            run.next_attempt_at = now + delay
            run.finished_at = None
            event_type = "job.reprocess_retry_scheduled"
        else:
            run.next_attempt_at = None
            run.finished_at = now
            event_type = "job.reprocess_failed"
        run.updated_at = now
        db.add(
            AuditLogEntry.create(
                event_type,
                "job_reprocess_run",
                run.reprocess_run_id,
                payload={
                    "job_id": run.job_id,
                    "type": run.run_type,
                    "error_code": run.error_code,
                    "retryable": retryable,
                    "worker_id": attempt_worker_id,
                    "next_attempt_at": (
                        run.next_attempt_at.isoformat() if run.next_attempt_at else None
                    ),
                },
            )
        )
        db.flush()
        return run.to_api()


def recover_stale_job_reprocess_runs(
    settings: Settings,
    *,
    active_worker_id: str | None = None,
    active_lease_token: str | None = None,
    recover_orphaned: bool = False,
    recover_owned_by: str | None = None,
    required_worker_id: str | None = None,
    required_lease_token: str | None = None,
) -> int:
    with session_scope(settings) as db:
        if required_lease_token is not None and not (
            begin_background_worker_lease_transaction(db, required_worker_id, required_lease_token)
        ):
            return 0
        now = datetime.now(UTC)
        runs = list(
            db.execute(select(JobReprocessRun).where(JobReprocessRun.status == "running")).scalars()
        )
        stale_runs = [
            run
            for run in runs
            if job_reprocess_run_is_stale(run, now)
            or (
                recover_orphaned
                and active_worker_id is not None
                and (
                    run.worker_id != active_worker_id
                    or run.supervisor_lease_token != active_lease_token
                )
            )
            or (recover_owned_by is not None and run.worker_id == recover_owned_by)
        ]
        for run in stale_runs:
            previous_worker_id = run.worker_id
            next_status = (
                "retry_scheduled" if run.attempts < JOB_REPROCESS_MAX_ATTEMPTS else "failed"
            )
            ensure_job_reprocess_transition(run.status, next_status)
            run.worker_id = None
            run.claim_token = None
            run.supervisor_lease_token = None
            run.error_code = JOB_REPROCESS_STALE_ERROR
            run.error_message = JOB_REPROCESS_STALE_ERROR
            run.retryable = True
            run.updated_at = now
            if run.attempts < JOB_REPROCESS_MAX_ATTEMPTS:
                run.status = "retry_scheduled"
                run.next_attempt_at = now
                run.finished_at = None
                event_type = "job.reprocess_stale_recovered"
            else:
                run.status = "failed"
                run.next_attempt_at = None
                run.finished_at = now
                event_type = "job.reprocess_failed"
            db.add(
                AuditLogEntry.create(
                    event_type,
                    "job_reprocess_run",
                    run.reprocess_run_id,
                    payload={
                        "job_id": run.job_id,
                        "type": run.run_type,
                        "attempts": run.attempts,
                        "error_code": JOB_REPROCESS_STALE_ERROR,
                        "retryable": True,
                        "worker_id": previous_worker_id,
                    },
                )
            )
        return len(stale_runs)


def job_reprocess_error_is_retryable(error: Exception) -> bool:
    if isinstance(error, JobReprocessProcessingError):
        return error.retryable
    if isinstance(error, HTTPException):
        return error.status_code in {408, 429} or error.status_code >= 500
    if str(error) == "fit_analysis_provider_disabled":
        return False
    if isinstance(error, FitAnalysisUnavailableError):
        return True
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OperationalError):
        message = str(error).casefold()
        return "locked" in message or "busy" in message
    return False
