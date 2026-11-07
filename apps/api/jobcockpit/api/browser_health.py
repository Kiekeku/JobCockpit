import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.db.models import JobPosting
from jobcockpit.ingestion.browser_health import dismiss_issues, safe_posting_url, summarize, url_key


def create_browser_health_router(get_db, settings):
    router = APIRouter()

    @router.get("/api/browser-health")
    def browser_health(db: Session = Depends(get_db)) -> dict:
        result = summarize(settings.artifact_root)
        result["enabled"] = settings.browser_health_enabled
        result["configured_provider"] = settings.posting_rendered_fallback_provider
        keys = {row.get("url_key") for row in result["recent_issues"]}
        jobs = {}
        if keys:
            # The private hash retains exact identity even when display links
            # omit tracking/credential fields. Stream only URL/id columns.
            rows = db.execute(select(JobPosting.source_url, JobPosting.id).where(
                JobPosting.source_url.is_not(None),
                JobPosting.source["merged_into_job_id"].as_string().is_(None),
            ).execution_options(yield_per=500))
            for source_url, job_id in rows:
                key = url_key(source_url)
                if key in keys:
                    jobs[key] = (job_id, source_url)
        for row in result["recent_issues"]:
            match = jobs.get(row.get("url_key"))
            row["job_id"] = match[0] if match else None
            if match:
                # Also repair links in older telemetry that lost job parameters.
                row["source_url"] = safe_posting_url(match[1])
            row.pop("url_key", None)
        return result

    @router.post("/api/browser-health/issues/dismiss-all")
    def dismiss_all_browser_issues() -> dict:
        try:
            return {"dismissed": dismiss_issues(settings.artifact_root, None)}
        except OSError as error:
            raise HTTPException(503, "Browser health records are unavailable") from error

    @router.post("/api/browser-health/issues/{attempt_id}/dismiss")
    def dismiss_browser_issue(attempt_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise HTTPException(422, "Invalid browser attempt ID")
        try:
            return {"dismissed": dismiss_issues(settings.artifact_root, [attempt_id])}
        except KeyError as error:
            raise HTTPException(404, "Browser issue not found") from error
        except OSError as error:
            raise HTTPException(503, "Browser health records are unavailable") from error

    return router
