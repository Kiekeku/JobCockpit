from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jobcockpit.db.models import Application, ApplicationPackage, JobPosting


def job_source_metadata(job: JobPosting) -> dict:
    return job.source if isinstance(job.source, dict) else {}


def job_merged_into_id(job: JobPosting) -> str | None:
    merged_into = job_source_metadata(job).get("merged_into_job_id")
    return str(merged_into) if merged_into else None


def job_is_merged_duplicate(job: JobPosting) -> bool:
    return job_merged_into_id(job) is not None


def resolve_active_job_for_write(job: JobPosting, db: Session) -> JobPosting:
    seen_job_ids = {job.id}
    while merged_into_job_id := job_merged_into_id(job):
        if merged_into_job_id in seen_job_ids:
            raise HTTPException(status_code=409, detail="Job merge chain is cyclic")
        seen_job_ids.add(merged_into_job_id)
        canonical_job = db.get(JobPosting, merged_into_job_id)
        if not canonical_job:
            raise HTTPException(status_code=409, detail="Job was merged into another posting")
        job = canonical_job
    return job


def reject_merged_job_write(job: JobPosting) -> None:
    if job_is_merged_duplicate(job):
        raise HTTPException(status_code=409, detail="Job was merged into another posting")


def active_job_postings(db: Session) -> list[JobPosting]:
    statement = select(JobPosting).from_statement(text("SELECT * FROM active_job_postings"))
    return list(db.execute(statement).scalars())


def active_applications(db: Session) -> list[Application]:
    return list(
        db.execute(
            select(Application)
            .join(JobPosting, JobPosting.id == Application.job_id)
            .where(text("json_extract(job_postings.source, '$.merged_into_job_id') IS NULL"))
        ).scalars()
    )


def require_active_job(
    db: Session, job_id: str, *, not_found_detail: str = "Job not found"
) -> JobPosting:
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=not_found_detail)
    reject_merged_job_write(job)
    return job


def require_active_application_job(
    db: Session, application_id: str
) -> tuple[Application, JobPosting]:
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    job = require_active_job(db, application.job_id)
    return application, job


def require_active_package_job(
    db: Session, package_id: str
) -> tuple[ApplicationPackage, Application, JobPosting]:
    package = db.get(ApplicationPackage, package_id)
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    application, job = require_active_application_job(db, package.application_id)
    return package, application, job
