from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from jobcockpit.db.models import JobPosting, JobPublication
from jobcockpit.services.publication import enqueue_job
from jobcockpit.services.publication_policy import eligible, load_policy


def create_publication_router(get_db, settings):
    router = APIRouter(prefix="/api/publications", tags=["publications"])

    @router.get("")
    def list_publications(limit: int = 100, db=Depends(get_db)):
        if not 1 <= limit <= 500:
            raise HTTPException(422, "limit must be between 1 and 500")
        return [row.to_api() for row in db.scalars(select(JobPublication)
                .order_by(JobPublication.updated_at.desc()).limit(limit))]

    @router.post("/enqueue-existing")
    def enqueue_existing(dry_run: bool = True, limit: int = 100, db=Depends(get_db)):
        if not 1 <= limit <= 500:
            raise HTTPException(422, "limit must be between 1 and 500")
        policy = load_policy(settings)
        if not policy:
            raise HTTPException(503, "Publication policy is not configured")
        matches = []
        existing = set(db.scalars(select(JobPublication.job_id).where(
            JobPublication.destination == policy.endpoint,
        )))
        for job in db.scalars(select(JobPosting).order_by(JobPosting.created_at, JobPosting.id)):
            if job.id not in existing and eligible(job, policy):
                matches.append(job.id)
                if not dry_run:
                    enqueue_job(db, job, settings, policy=policy)
                if len(matches) >= limit:
                    break
        return {"dry_run": dry_run, "eligible_job_ids": matches}

    @router.post("/{publication_id}/retry")
    def retry(publication_id: str, db=Depends(get_db)):
        row = db.get(JobPublication, publication_id)
        job = db.get(JobPosting, row.job_id) if row and row.job_id else None
        if not job:
            raise HTTPException(404, "Publication or source posting not found")
        if row.status == "running":
            raise HTTPException(409, "Publication is being processed")
        policy = load_policy(settings)
        if not policy or row.destination != policy.endpoint:
            raise HTTPException(409, "Publication destination is not currently configured")
        enqueue_job(db, job, settings, retry=True)
        db.flush()
        return row.to_api()

    return router
