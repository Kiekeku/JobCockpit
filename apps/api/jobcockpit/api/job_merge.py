from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from jobcockpit.api.cover_letter_generation_runs import (
    ACTIVE_COVER_LETTER_GENERATION_STATUSES,
)
from jobcockpit.api.page_capture_runs import reassign_page_capture_job_references
from jobcockpit.db.models import (
    Application,
    AuditLogEntry,
    CoverLetterGenerationRun,
    FitAnalysis,
    JobAlertLink,
    JobIngestionItem,
    JobPosting,
    JobReprocessRun,
    JobSourceIdentity,
    JobStatusCheck,
    StoredArtifact,
)
from jobcockpit.ingestion.status_reconciliation import (
    SOURCE_REPAIR_EVIDENCE_CODES,
    status_urls_match,
)


ACTIVE_REPROCESS_STATUSES = {"queued", "running", "retry_scheduled"}
JOB_POSTING_FOREIGN_KEY_RELATIONSHIPS = {
    ("applications", "job_id"),
    ("cover_letter_generation_runs", "job_id"),
    # Reviews and remembered answers stay on the original job for audit. Merged
    # jobs fail require_active_job, so approvals are never transferred by a merge.
    ("extension_application_sessions", "job_id"),
    ("extension_approved_answers", "job_id"),
    ("candidate_learned_facts", "job_id"),
    ("extension_learning_runs", "job_id"),
    ("fit_analyses", "job_id"),
    ("job_alert_links", "linked_job_id"),
    ("job_ingestion_items", "linked_job_id"),
    ("job_reprocess_runs", "job_id"),
    ("job_source_identities", "job_id"),
    ("job_source_reconciliations", "job_id"),
    ("job_status_checks", "job_id"),
    # Delivery identities stay on the original job. The publication hook withdraws
    # delivered duplicates while retaining their inbound echo-protection identity.
    ("job_publications", "job_id"),
    ("page_capture_runs", "job_id"),
    ("page_capture_runs", "target_job_id"),
    ("page_capture_snapshot_scopes", "job_id"),
    ("page_capture_effects", "job_id"),
}


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _job_status_urls(job: JobPosting) -> tuple[str, ...]:
    source = job.source if isinstance(job.source, dict) else {}
    resolution = source.get("source_resolution")
    if not isinstance(resolution, dict):
        resolution = {}
    application = job.application if isinstance(job.application, dict) else {}
    return tuple(
        value
        for value in (
            str(resolution.get("official_url") or "").strip(),
            str(application.get("external_apply_url") or "").strip(),
            str(job.source_url or source.get("source_url") or "").strip(),
            str(resolution.get("fallback_url") or "").strip(),
        )
        if value
    )


def _source_repair_url_from_history(duplicate: JobPosting, db: Session) -> str | None:
    recent_checks = db.execute(
        select(JobStatusCheck.evidence_code, JobStatusCheck.source_url)
        .where(JobStatusCheck.job_id == duplicate.id)
        .order_by(
            JobStatusCheck.checked_at.desc(),
            JobStatusCheck.created_at.desc(),
            JobStatusCheck.status_check_id.desc(),
        )
        .limit(100)
    ).all()
    for index in range(len(recent_checks) - 2):
        window = recent_checks[index : index + 3]
        source_url = window[0].source_url
        if not source_url:
            continue
        if all(
            check.evidence_code in SOURCE_REPAIR_EVIDENCE_CODES
            and check.source_url
            and status_urls_match(source_url, check.source_url)
            for check in window
        ):
            return source_url
    return None


def _merge_job_availability_evidence(
    canonical_job: JobPosting,
    duplicate: JobPosting,
    db: Session,
) -> None:
    duplicate_last_open = duplicate.status_last_open_at
    canonical_last_open = canonical_job.status_last_open_at
    if duplicate_last_open is not None and (
        canonical_last_open is None
        or _aware_utc(duplicate_last_open) > _aware_utc(canonical_last_open)
    ):
        canonical_job.status_last_open_at = duplicate_last_open
        canonical_job.status_last_open_evidence_code = duplicate.status_last_open_evidence_code

    if not duplicate.status_source_repair_required:
        return
    repair_source_url = _source_repair_url_from_history(duplicate, db)
    if not repair_source_url:
        return
    canonical_urls = _job_status_urls(canonical_job)
    if any(status_urls_match(canonical_url, repair_source_url) for canonical_url in canonical_urls):
        canonical_job.status_source_repair_required = True
        canonical_job.status_next_check_at = None


class JobMergeConflict(RuntimeError):
    """A deterministic relationship conflict makes automatic merging unsafe."""


@dataclass(frozen=True)
class JobMergePlan:
    canonical_job_id: str
    duplicate_job_ids: tuple[str, ...]
    provider: str
    identity_hash: str


def plan_authoritative_job_merge(
    canonical_job: JobPosting,
    duplicate_job_ids: set[str],
    db: Session,
    *,
    provider: str,
    identity_hash: str,
    identity_strength: str,
) -> JobMergePlan:
    if identity_strength != "authoritative":
        raise JobMergeConflict("weak_source_identity_cannot_merge_jobs")
    duplicates = tuple(
        db.execute(
            select(JobPosting)
            .where(
                JobPosting.id.in_(duplicate_job_ids),
                JobPosting.id != canonical_job.id,
            )
            .order_by(JobPosting.created_at, JobPosting.id)
        ).scalars()
    )
    canonical_candidates = {
        row.candidate_id
        for row in db.execute(
            select(Application).where(Application.job_id == canonical_job.id)
        ).scalars()
    }
    canonical_has_active_reprocess = db.execute(
        select(JobReprocessRun.reprocess_run_id)
        .where(
            JobReprocessRun.job_id == canonical_job.id,
            JobReprocessRun.status.in_(ACTIVE_REPROCESS_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none() is not None
    for duplicate in duplicates:
        duplicate_candidates = {
            row.candidate_id
            for row in db.execute(
                select(Application).where(Application.job_id == duplicate.id)
            ).scalars()
        }
        if canonical_candidates.intersection(duplicate_candidates):
            raise JobMergeConflict("duplicate_application_relationship")
        duplicate_has_active_reprocess = db.execute(
            select(JobReprocessRun.reprocess_run_id)
            .where(
                JobReprocessRun.job_id == duplicate.id,
                JobReprocessRun.status.in_(ACTIVE_REPROCESS_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none() is not None
        if canonical_has_active_reprocess and duplicate_has_active_reprocess:
            raise JobMergeConflict("duplicate_active_reprocess_relationship")
        canonical_candidates.update(duplicate_candidates)
        canonical_has_active_reprocess = (
            canonical_has_active_reprocess or duplicate_has_active_reprocess
        )
    return JobMergePlan(
        canonical_job_id=canonical_job.id,
        duplicate_job_ids=tuple(job.id for job in duplicates),
        provider=provider,
        identity_hash=identity_hash,
    )


def apply_authoritative_job_merge(
    plan: JobMergePlan,
    db: Session,
) -> None:
    canonical_job = db.get(JobPosting, plan.canonical_job_id)
    if canonical_job is None:
        raise JobMergeConflict("canonical_job_missing")
    for duplicate_id in plan.duplicate_job_ids:
        duplicate = db.get(JobPosting, duplicate_id)
        if duplicate is None:
            continue
        from jobcockpit.ingestion.source_reconciliation import cancel_source_reconciliation
        cancel_source_reconciliation(db, duplicate.id, reason="job_merged")
        canonical_job.last_applied_capture_seq = max(
            canonical_job.last_applied_capture_seq,
            duplicate.last_applied_capture_seq,
        )
        _merge_job_availability_evidence(canonical_job, duplicate, db)
        _merge_job_source_locators(canonical_job, duplicate)
        _move_job_source_identities(canonical_job.id, duplicate.id, db)
        for model, column in (
            (JobStatusCheck, JobStatusCheck.job_id),
            (StoredArtifact, StoredArtifact.job_id),
            (FitAnalysis, FitAnalysis.job_id),
            (Application, Application.job_id),
        ):
            db.execute(
                update(model)
                .where(column == duplicate.id)
                .values({column.key: canonical_job.id})
            )
        for run in db.execute(
            select(JobReprocessRun).where(JobReprocessRun.job_id == duplicate.id)
        ).scalars():
            if run.status in ACTIVE_REPROCESS_STATUSES:
                run.status = "cancelled"
                run.error_code = "job_merged"
                run.error_message = "job_merged_into_another_posting"
                run.retryable = False
                run.next_attempt_at = None
                run.finished_at = datetime.now(UTC)
                run.worker_id = None
                run.claim_token = None
                run.supervisor_lease_token = None
                run.result = {
                    **(run.result or {}),
                    "merged_into_job_id": canonical_job.id,
                }
                db.add(
                    AuditLogEntry.create(
                        "job.reprocess_cancelled",
                        "job_reprocess_run",
                        run.reprocess_run_id,
                        payload={
                            "job_id": duplicate.id,
                            "merged_into_job_id": canonical_job.id,
                            "error_message": "job_merged_into_another_posting",
                        },
                    )
                )
            else:
                run.job_id = canonical_job.id
        for run in db.execute(
            select(CoverLetterGenerationRun).where(
                CoverLetterGenerationRun.job_id == duplicate.id
            )
        ).scalars():
            if run.status in ACTIVE_COVER_LETTER_GENERATION_STATUSES:
                run.status = "cancelled"
                run.error_code = "job_merged"
                run.error_message = "job_merged_into_another_posting"
                run.retryable = False
                run.next_attempt_at = None
                run.finished_at = datetime.now(UTC)
                run.worker_id = None
                run.claim_token = None
                run.supervisor_lease_token = None
                run.result = {
                    **(run.result or {}),
                    "merged_into_job_id": canonical_job.id,
                }
                db.add(
                    AuditLogEntry.create(
                        "cover_letter.generation_cancelled",
                        "cover_letter_generation_run",
                        run.generation_run_id,
                        payload={
                            "job_id": duplicate.id,
                            "merged_into_job_id": canonical_job.id,
                            "error_message": "job_merged_into_another_posting",
                        },
                    )
                )
            else:
                run.job_id = canonical_job.id
        db.execute(
            update(JobAlertLink)
            .where(JobAlertLink.linked_job_id == duplicate.id)
            .values(linked_job_id=canonical_job.id)
        )
        db.execute(
            update(JobIngestionItem)
            .where(JobIngestionItem.linked_job_id == duplicate.id)
            .values(linked_job_id=canonical_job.id)
        )
        reassign_page_capture_job_references(
            db,
            duplicate_job_id=duplicate.id,
            canonical_job_id=canonical_job.id,
        )
        source = duplicate.source if isinstance(duplicate.source, dict) else {}
        application = (
            duplicate.application if isinstance(duplicate.application, dict) else {}
        )
        duplicate.source = {
            **source,
            "merged_into_job_id": canonical_job.id,
            "merged_from_source_url": duplicate.source_url,
            "merged_from_posting_snapshot_hash": duplicate.posting_snapshot_hash,
            "merge_reason": "authoritative_browser_capture_identity",
            "merge_provider": plan.provider,
        }
        duplicate.application = {
            **application,
            "merged_external_apply_url": application.get("external_apply_url"),
            "external_apply_url": None,
        }
        duplicate.source_url = None
        duplicate.posting_snapshot_hash = sha256(
            f"merged\0{canonical_job.id}\0{duplicate.id}".encode("utf-8")
        ).hexdigest()
        duplicate.status_next_check_at = None
        duplicate.updated_at = datetime.now(UTC)
        db.add(
            AuditLogEntry.create(
                "job.duplicate_merged",
                "job_posting",
                canonical_job.id,
                payload={
                    "duplicate_job_id": duplicate.id,
                    "reason": "authoritative_browser_capture_identity",
                    "provider": plan.provider,
                },
            )
        )
    db.flush()


def _merge_job_source_locators(canonical_job: JobPosting, duplicate: JobPosting) -> None:
    """Keep URLs that reconciliation revalidates after moving registry entries."""
    from jobcockpit.services.workflows import (
        portal_source_urls_from_metadata,
        source_metadata_with_portal_url,
    )

    source = dict(canonical_job.source or {})
    duplicate_source = duplicate.source or {}
    urls = portal_source_urls_from_metadata(duplicate_source)
    if duplicate.source_url and duplicate_source.get("source_url_dedup_safe") is not False:
        urls.add(duplicate.source_url)
    primary_portal = source.get("portal_source_url")
    for url in sorted(urls):
        source = source_metadata_with_portal_url(source, url)
    if primary_portal:
        source["portal_source_url"] = primary_portal
    canonical_job.source = source


def merge_jobs_for_authoritative_identity(
    canonical_job: JobPosting,
    duplicate_job_ids: set[str],
    db: Session,
    *,
    provider: str,
    identity_hash: str,
    identity_strength: str,
) -> None:
    if not duplicate_job_ids:
        return
    plan = plan_authoritative_job_merge(
        canonical_job,
        duplicate_job_ids,
        db,
        provider=provider,
        identity_hash=identity_hash,
        identity_strength=identity_strength,
    )
    apply_authoritative_job_merge(plan, db)


def _move_job_source_identities(
    canonical_job_id: str,
    duplicate_job_id: str,
    db: Session,
) -> None:
    identities = tuple(
        db.execute(
            select(JobSourceIdentity).where(
                JobSourceIdentity.job_id == duplicate_job_id
            )
        ).scalars()
    )
    for identity in identities:
        # Sessions disable autoflush. A preceding duplicate may already have
        # moved this key in memory while its database row still has the old job.
        existing = next((candidate for candidate in (*db.identity_map.values(), *db.new)
                         if isinstance(candidate, JobSourceIdentity)
                         and candidate not in db.deleted
                         and candidate.job_id == canonical_job_id
                         and candidate.provider == identity.provider
                         and candidate.identity_hash == identity.identity_hash), None)
        if existing is None:
            existing = db.execute(
                select(JobSourceIdentity).where(
                    JobSourceIdentity.job_id == canonical_job_id,
                    JobSourceIdentity.provider == identity.provider,
                    JobSourceIdentity.identity_hash == identity.identity_hash,
                )
            ).scalar_one_or_none()
        if existing is None:
            identity.job_id = canonical_job_id
            continue
        if identity.identity_strength == "authoritative":
            existing.identity_strength = "authoritative"
        if identity.match_priority > existing.match_priority:
            existing.match_priority = identity.match_priority
            existing.source_kind = identity.source_kind
            existing.public_source_url = (
                identity.public_source_url or existing.public_source_url
            )
        db.delete(identity)
