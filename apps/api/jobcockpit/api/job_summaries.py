"""Small dashboard read model. Evidence stays on the full job detail endpoint."""

from collections import Counter
from datetime import date, datetime
import re
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from jobcockpit.db.models import Application, JobPosting, api_datetime


SUMMARY_JSON_FIELDS = {
    "source": ("source_archive_artifact_id",),
    "employer": ("company_name",),
    "role": ("job_title", "normalized_title"),
    "employment_timing": (
        "raw_label",
        "timing_type",
        "start_date",
        "duration_months",
        "duration_label",
    ),
    "location": ("city", "province_state", "country"),
    "application": ("documents_required",),
    "fit_analysis": ("fit_score",),
    "data_quality": ("needs_manual_review",),
}
SUMMARY_SCALARS = (
    "id",
    "created_at",
    "status",
    "duration_months",
    "source_type",
    "source_url",
    "status_last_checked_at",
    "status_next_check_at",
    "status_check_result",
    "status_check_evidence_code",
    "status_check_failure_count",
    "status_last_open_at",
    "status_last_open_evidence_code",
    "status_source_repair_required",
)


def load_job_summaries(db):
    columns = [getattr(JobPosting, name).label(name) for name in SUMMARY_SCALARS]
    for section, fields in SUMMARY_JSON_FIELDS.items():
        for field in fields:
            value = getattr(JobPosting, section)[field]
            if field == "needs_manual_review":
                value = value.as_boolean()
            columns.append(value.label(f"{section}__{field}"))
    columns.extend(
        (
            Application.application_id.label("application_id"),
            Application.status.label("application_status"),
            Application.applied_at.label("applied_at"),
            Application.decision.label("decision"),
            Application.follow_up_date.label("next_action_at"),
        )
    )
    statement = (
        select(*columns)
        .outerjoin(
            Application,
            Application.job_id == JobPosting.id,
        )
        .where(JobPosting.source["merged_into_job_id"].as_string().is_(None))
    )
    result = []
    for row in db.execute(statement).mappings():
        values = {name: row[name] for name in SUMMARY_SCALARS}
        for section, fields in SUMMARY_JSON_FIELDS.items():
            values[section] = {field: row[f"{section}__{field}"] for field in fields}
        # Reuse the canonical availability/evidence presentation, with no ORM loads.
        job = JobPosting(**values)
        payload = job.to_api()
        for field in ("eligibility", "compensation"):
            payload.pop(field, None)
        payload["source"].pop("posting_snapshot_hash", None)
        payload["source"].pop("raw_content_artifact_id", None)
        payload["application_summary"] = {
            "application_id": row["application_id"],
            "application_status": row["application_status"],
            "applied_at": api_datetime(row["applied_at"]) if row["applied_at"] else None,
            "decision": row["decision"],
            "next_action_at": row["next_action_at"],
        }
        result.append(payload)
    return result


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def timing_display(timing):
    """Presentation of stored timing only; does not infer or change parser output."""
    raw = (timing.get("raw_label") or "").strip()
    start = (timing.get("start_date") or "").strip()
    parsed = _date(start)
    if parsed and timing.get("timing_type") == "fixed_term":
        term = "Winter" if parsed.month <= 4 else "Summer" if parsed.month <= 8 else "Fall"
        display = f"{term} {parsed.year}"
    else:
        display = raw or (parsed.strftime("%b %Y") if parsed else start or "Unknown")
    duration = re.fullmatch(
        r"\d+\s*(?:[-–—/]\s*\d+\s*)?[- ]?\s*(?:months?|weeks?)"
        r"(?:\s+(?:co-?op|internship|work term))?",
        raw,
        re.I,
    )
    missing = timing.get("timing_type") == "posted_date_fallback" or (
        parsed is None and (bool(duration) or display.lower() == "unknown")
    )
    return display, f"{raw} {start} {display}".lower(), missing


def _term_sort(timing):
    for value in (timing.get("start_date"), timing.get("raw_label")):
        if not value:
            continue
        parsed = _date(value)
        if parsed:
            return parsed.toordinal()
        match = re.search(r"\b(winter|spring|summer|fall|autumn)\s+(\d{4})\b", value, re.I)
        if match:
            month = {"winter": 1, "spring": 5, "summer": 5, "fall": 9, "autumn": 9}[
                match[1].lower()
            ]
            return date(int(match[2]), month, 1).toordinal()
        for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(value, fmt).date().toordinal()
            except ValueError:
                pass
    return None


def _matches_status(job, status):
    if status == "all":
        return True
    if status == "not_active":
        return job["status"] != "active"
    return job["status"] == status or job["availability"]["state"] == status


def _needs_review(job):
    return (
        not job["application_summary"]["application_id"]
        and job["data_quality"].get("needs_manual_review") is True
    )


def create_job_summary_router(get_db):
    router = APIRouter(tags=["jobs"])

    @router.get("/api/job-summaries")
    def list_summaries(
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
        q: str = Query("", max_length=500),
        term: str = Query("", max_length=500),
        status: str = "all",
        verification: str = "all",
        missing_term: bool = False,
        needs_review: bool = False,
        hide_applied: bool = False,
        sort: Literal["created_at", "fit", "term"] = "created_at",
        direction: Literal["asc", "desc"] = "desc",
        db=Depends(get_db),
    ):
        jobs = load_job_summaries(db)
        stats = {
            "total": len(jobs),
            "availability": dict(Counter(job["availability"]["state"] for job in jobs)),
            "needs_review": sum(_needs_review(job) for job in jobs),
            "status_needs_review": sum(
                _needs_review(job) and _matches_status(job, status) for job in jobs
            ),
            "tracked": sum(bool(job["application_summary"]["application_id"]) for job in jobs),
            "draft_applications": sum(
                job["application_summary"]["application_status"] == "draft" for job in jobs
            ),
        }
        search, term_search = q.strip().lower(), term.strip().lower()
        filtered = []
        for job in jobs:
            title = (
                job["role"].get("job_title")
                or job["role"].get("normalized_title")
                or "Untitled role"
            )
            company = job["employer"].get("company_name") or "Unknown company"
            _, timing_text, missing = timing_display(job["employment_timing"])
            category = (job["status_check"] or {}).get("category", "unknown")
            if (
                search
                and search not in title.lower()
                and search not in company.lower()
                or term_search
                and term_search not in timing_text
                or missing_term
                and not missing
                or not _matches_status(job, status)
                or verification != "all"
                and verification != category
                or needs_review
                and not _needs_review(job)
                or hide_applied
                and job["application_summary"]["application_status"] == "applied"
            ):
                continue
            filtered.append(job)

        def sort_value(job):
            if sort == "fit":
                return job["fit_analysis"].get("fit_score")
            if sort == "term":
                return _term_sort(job["employment_timing"])
            return job["created_at"]

        # Stable ID tiebreaker keeps pagination deterministic; unknown values stay last.
        filtered.sort(key=lambda job: job["id"])
        known = [job for job in filtered if sort_value(job) is not None]
        unknown = [job for job in filtered if sort_value(job) is None]
        known.sort(key=sort_value, reverse=direction == "desc")
        filtered = known + unknown
        total = len(filtered)
        page = min(page, max(1, (total + page_size - 1) // page_size))
        offset = (page - 1) * page_size
        return {
            "items": filtered[offset : offset + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "stats": stats,
        }

    return router
