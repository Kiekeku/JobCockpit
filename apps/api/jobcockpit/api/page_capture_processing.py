from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    begin_background_worker_lease_transaction,
    background_worker_lease_is_valid,
    bootstrap_background_worker,
)
from jobcockpit.api.job_merge import (
    JobMergeConflict,
    merge_jobs_for_authoritative_identity,
)
from jobcockpit.api.page_capture_runs import (
    PageCaptureClaimLost,
    PageCaptureProcessingError,
    associate_snapshot_with_job,
    claim_page_capture_run,
    ensure_page_capture_transition,
    page_capture_error_is_retryable,
    page_capture_snapshot_is_current,
    page_capture_snapshot_is_current_for_jobs,
    record_page_capture_failure,
    resolve_job_by_source_identity,
    upsert_job_source_identity,
    upsert_page_capture_effects,
)
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    JobPosting,
    PageCaptureRun,
    PageCaptureSnapshot,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters.base import SourceArchive
from jobcockpit.ingestion.audit import IngestionAuditService
from jobcockpit.ingestion.inputs import JobIngestPayload as QueuedPageCapturePayload
from jobcockpit.ingestion.output import empty_ingestion_counts
from jobcockpit.ingestion.browser_capture_adapters import (
    CaptureProcessingAction,
    PageCaptureAssessment,
    capture_source_identity,
)
from jobcockpit.ingestion.posting_discovery import (
    assess_page_capture,
    capture_processing_action,
)
from jobcockpit.ingestion.status_reconciliation import (
    ordered_status_urls,
    status_urls_match,
)
from jobcockpit.schemas.requests import ExtensionPageCaptureRequest
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedPageCaptureAttempt:
    expected_attempt: int
    expected_claim_token: str
    expected_supervisor_lease_token: str


@dataclass(frozen=True)
class LoadedPageCapture:
    request: ExtensionPageCaptureRequest
    payload: QueuedPageCapturePayload
    private_source_url: str
    snapshot_id: str
    expected_attempt: int
    expected_claim_token: str
    expected_supervisor_lease_token: str
    raw_content_artifact_id: str
    source_archive_artifact_id: str
    assessment: PageCaptureAssessment


def page_capture_request_from_run(
    run: PageCaptureRun,
    snapshot: PageCaptureSnapshot,
    db: Session,
    storage: ArtifactStorageService,
) -> tuple[ExtensionPageCaptureRequest, QueuedPageCapturePayload]:
    raw_artifact = db.get(StoredArtifact, snapshot.raw_content_artifact_id)
    archive_artifact = db.get(StoredArtifact, snapshot.source_archive_artifact_id)
    if not raw_artifact or not archive_artifact:
        raise PageCaptureProcessingError(
            "capture_artifact_missing",
            "Captured page artifacts are unavailable",
            retryable=False,
        )
    raw_text = storage.read_bytes(raw_artifact.storage_uri).decode("utf-8")
    archive_body = storage.read_bytes(archive_artifact.storage_uri)
    archive_text = archive_body.decode("utf-8")
    archive_is_html = archive_artifact.content_type == "text/html"
    request = ExtensionPageCaptureRequest.model_construct(
        source_url=snapshot.source_url,
        page_title=snapshot.page_title,
        visible_text=raw_text if archive_is_html else archive_text,
        html=archive_text if archive_is_html else None,
        raw_text=raw_text,
        target_job_id=run.target_job_id,
        skip_language_filter=run.skip_language_filter,
    )
    source_identity = capture_source_identity(snapshot.source_url)
    if source_identity is None or not source_identity.public_source_url:
        raise PageCaptureProcessingError(
            "capture_source_identity_missing",
            "Captured page source identity is unavailable",
            retryable=False,
        )
    source_archive = SourceArchive(
        source_url=source_identity.canonical_source_url,
        final_url=source_identity.canonical_source_url,
        status_code=0,
        content_type=archive_artifact.content_type,
        body=archive_body,
        headers={"x-jobcockpit-capture-source": "browser_extension"},
    )
    payload = QueuedPageCapturePayload(
        source_type="browser_capture",
        raw_text=raw_text,
        source_url=source_identity.canonical_source_url,
        source_archive=source_archive,
        source_resolution={
            "kind": "browser_capture",
            "provider": "user_browser_capture",
            "status": "captured",
            "reason_code": "user_submitted_page_capture",
        },
        skip_language_filter=run.skip_language_filter,
        source_identity_hash=run.identity_hash,
        source_url_dedup_safe=source_identity.source_url_dedup_safe,
    )
    return request, payload


def _claim_capture_attempt(
    capture_id: str,
    settings: Settings,
    worker_id: str,
    supervisor_lease_token: str,
) -> ClaimedPageCaptureAttempt | dict | None:
    """Commit ownership before any private artifact or provider work begins."""
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
            return None

        run = db.get(PageCaptureRun, capture_id)
        if not run:
            return None
        if run.status in {"queued", "retry_scheduled"}:
            run = claim_page_capture_run(
                db,
                capture_id,
                worker_id=worker_id,
                supervisor_lease_token=supervisor_lease_token,
            )
            if not run:
                current = db.get(PageCaptureRun, capture_id)
                return current.to_api() if current else None
        if (
            run.status != "running"
            or run.worker_id != worker_id
            or run.supervisor_lease_token != supervisor_lease_token
            or not run.claim_token
            or not background_worker_lease_is_valid(db, worker_id, supervisor_lease_token)
        ):
            return run.to_api()
        return ClaimedPageCaptureAttempt(
            expected_attempt=run.attempts,
            expected_claim_token=run.claim_token,
            expected_supervisor_lease_token=supervisor_lease_token,
        )


def _load_claimed_capture(
    capture_id: str,
    settings: Settings,
    storage: ArtifactStorageService,
    worker_id: str,
    claim: ClaimedPageCaptureAttempt,
) -> LoadedPageCapture | dict | None:
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db, worker_id, claim.expected_supervisor_lease_token
        ):
            return None

        run = db.get(PageCaptureRun, capture_id)
        if not run:
            return None
        if (
            run.status != "running"
            or run.worker_id != worker_id
            or run.attempts != claim.expected_attempt
            or run.claim_token != claim.expected_claim_token
            or run.supervisor_lease_token != claim.expected_supervisor_lease_token
            or not background_worker_lease_is_valid(
                db, worker_id, claim.expected_supervisor_lease_token
            )
        ):
            return run.to_api()
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        if snapshot is None:
            raise PageCaptureProcessingError(
                "capture_artifact_missing",
                "Captured page snapshot is unavailable",
                retryable=False,
            )
        request, payload = page_capture_request_from_run(run, snapshot, db, storage)
        if run.target_job_id and db.get(JobPosting, run.target_job_id) is None:
            raise PageCaptureProcessingError(
                "target_job_not_found", "Target job not found", retryable=False
            )
        assessment = assess_page_capture(snapshot.source_url, payload.raw_text)
        run.page_access_verdict = assessment.access_verdict.value
        run.page_kind = assessment.page_kind.value
        run.assessment_evidence_code = assessment.evidence_code
        assessment_action = capture_processing_action(assessment)
        identity_match = resolve_job_by_source_identity(db, run, snapshot)
        if identity_match is not None and identity_match.identity_strength == "authoritative":
            for matched_job in identity_match.jobs:
                associate_snapshot_with_job(
                    db,
                    snapshot_id=snapshot.snapshot_id,
                    job_id=matched_job.id,
                    association_basis="authoritative_identity",
                    identity_strength="authoritative",
                )
            if run.target_job_id is None:
                run.job_id = identity_match.job.id
            run.association_verdict = "verified"
        if assessment_action is CaptureProcessingAction.REJECT:
            run.identity_verdict = "rejected"
        return LoadedPageCapture(
            request=request,
            payload=payload,
            private_source_url=snapshot.source_url,
            snapshot_id=snapshot.snapshot_id,
            expected_attempt=claim.expected_attempt,
            expected_claim_token=claim.expected_claim_token,
            expected_supervisor_lease_token=claim.expected_supervisor_lease_token,
            raw_content_artifact_id=snapshot.raw_content_artifact_id,
            source_archive_artifact_id=snapshot.source_archive_artifact_id,
            assessment=assessment,
        )


def _begin_fenced_commit(
    db: Session,
    worker_id: str,
    supervisor_lease_token: str,
) -> None:
    if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
        raise PageCaptureClaimLost("Page capture supervisor lease changed")


def _owned_run(
    db: Session,
    capture_id: str,
    *,
    worker_id: str,
    expected_attempt: int,
    expected_claim_token: str,
    expected_supervisor_lease_token: str,
) -> PageCaptureRun:
    run = db.get(PageCaptureRun, capture_id)
    if (
        run is None
        or run.status != "running"
        or run.worker_id != worker_id
        or run.attempts != expected_attempt
        or run.claim_token != expected_claim_token
        or run.supervisor_lease_token != expected_supervisor_lease_token
        or not background_worker_lease_is_valid(db, worker_id, expected_supervisor_lease_token)
    ):
        raise PageCaptureClaimLost("Page capture ownership changed before commit")
    return run


def process_page_capture_run(
    capture_id: str,
    settings: Settings,
    storage: ArtifactStorageService | None = None,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> dict | None:
    worker_id = worker_id or new_id("worker")
    if supervisor_lease_token is None:
        with BackgroundWorkerLeaseGuard(settings, worker_id) as lease:
            if not lease.acquired or lease.lease_token is None:
                with session_scope(settings) as db:
                    current = db.get(PageCaptureRun, capture_id)
                    return current.to_api() if current else None
            bootstrap_background_worker(
                settings,
                worker_id=worker_id,
                lease_token=lease.lease_token,
            )
            return process_page_capture_run(
                capture_id,
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=lease.lease_token,
            )
    expected_attempt: int | None = None
    expected_claim_token: str | None = None
    storage = storage or ArtifactStorageService(settings.artifact_root)
    audit = IngestionAuditService(settings)
    try:
        claim = _claim_capture_attempt(
            capture_id,
            settings,
            worker_id,
            supervisor_lease_token,
        )
        if claim is None or isinstance(claim, dict):
            return claim
        expected_attempt = claim.expected_attempt
        expected_claim_token = claim.expected_claim_token
        loaded = _load_claimed_capture(
            capture_id,
            settings,
            storage,
            worker_id,
            claim,
        )
        if loaded is None or isinstance(loaded, dict):
            return loaded

        from jobcockpit.services import workflows as api_main

        processing_action = capture_processing_action(loaded.assessment)
        if processing_action is CaptureProcessingAction.REJECT:
            raise PageCaptureProcessingError(
                loaded.assessment.evidence_code,
                "Captured page evidence is not safe to process",
                retryable=False,
            )
        terminal_status = loaded.assessment.terminal_status
        if processing_action is CaptureProcessingAction.PARSE:
            language_skip_reason = None
            with session_scope(settings) as db:
                try:
                    api_main.enforce_language_filter_for_ingest_payload(loaded.payload, db)
                except HTTPException as error:
                    if not _language_filter_skip(error):
                        raise PageCaptureProcessingError(
                            "capture_processing_rejected",
                            "Captured page processing was rejected",
                            retryable=False,
                        ) from error
                    language_skip_reason = _language_filter_reason(error)
            if language_skip_reason is not None:
                return _commit_language_skip(
                    capture_id,
                    settings,
                    worker_id,
                    loaded,
                    audit,
                    language_skip_reason,
                )
            parsed = api_main.parse_job_text_with_settings(
                loaded.payload.raw_text,
                loaded.payload.source_url,
                settings,
                source_type=loaded.payload.source_type,
            )
            _require_configured_parser_success({"source": parsed.source.model_dump()}, settings)
        else:
            parsed = None

        with session_scope(settings) as db:
            _begin_fenced_commit(
                db,
                worker_id,
                loaded.expected_supervisor_lease_token,
            )
            run = _owned_run(
                db,
                capture_id,
                worker_id=worker_id,
                expected_attempt=loaded.expected_attempt,
                expected_claim_token=loaded.expected_claim_token,
                expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
            )
            snapshot = db.get(PageCaptureSnapshot, loaded.snapshot_id)
            if snapshot is None:
                raise PageCaptureProcessingError(
                    "capture_artifact_missing",
                    "Captured page snapshot is unavailable",
                    retryable=False,
                )
            stale_result = _finish_stale_capture(
                run,
                snapshot,
                db,
                audit,
                worker_id=worker_id,
                loaded=loaded,
            )
            if stale_result is not None:
                return stale_result
            target_job = db.get(JobPosting, run.target_job_id) if run.target_job_id else None
            if run.target_job_id and target_job is None:
                raise PageCaptureProcessingError(
                    "target_job_not_found", "Target job not found", retryable=False
                )
            if target_job is not None:
                target_job = api_main.resolve_active_job_for_write(target_job, db)
            identity_match = resolve_job_by_source_identity(db, run, snapshot)
            match_decision = _select_capture_job(
                target_job,
                identity_match,
                identity_strength=run.identity_strength,
                parsed_job=parsed,
                terminal=terminal_status is not None,
                source_url=loaded.private_source_url,
            )
            associated_job = match_decision.job
            merge_candidate_job_ids = match_decision.merge_candidate_job_ids
            effect_job_ids = set(merge_candidate_job_ids)
            association_jobs = [associated_job] if associated_job is not None else []
            association_jobs.extend(
                candidate
                for job_id in sorted(merge_candidate_job_ids)
                if (candidate := db.get(JobPosting, job_id)) is not None
            )
            for association_job in {job.id: job for job in association_jobs}.values():
                associate_snapshot_with_job(
                    db,
                    snapshot_id=snapshot.snapshot_id,
                    job_id=association_job.id,
                    association_basis=(
                        "authoritative_identity"
                        if run.identity_strength == "authoritative"
                        else "parsed_identity"
                    ),
                    identity_strength=run.identity_strength,
                    association_verdict=match_decision.association_verdict,
                )

            if associated_job is not None:
                effect_job_ids.add(associated_job.id)
            stale_result = _finish_stale_capture(
                run,
                snapshot,
                db,
                audit,
                worker_id=worker_id,
                loaded=loaded,
                job_ids=effect_job_ids,
            )
            if stale_result is not None:
                return stale_result
            terminal_jobs = [associated_job] if associated_job is not None else []

            if associated_job and merge_candidate_job_ids:
                try:
                    merge_jobs_for_authoritative_identity(
                        associated_job,
                        set(merge_candidate_job_ids),
                        db,
                        provider=run.provider,
                        identity_hash=run.identity_hash,
                        identity_strength=run.identity_strength,
                    )
                except JobMergeConflict as error:
                    if terminal_status is not None:
                        terminal_jobs.extend(
                            job
                            for job_id in sorted(merge_candidate_job_ids)
                            if (job := db.get(JobPosting, job_id)) is not None
                        )
                        db.add(
                            AuditLogEntry.create(
                                "extension.page_capture_merge_deferred",
                                "page_capture_run",
                                run.capture_id,
                                payload={
                                    "error_code": "capture_merge_conflict",
                                    "job_ids": [job.id for job in terminal_jobs],
                                },
                            )
                        )
                    else:
                        raise PageCaptureProcessingError(
                            "capture_merge_conflict",
                            "Authoritative capture identity could not be merged safely",
                            retryable=False,
                        ) from error

            if terminal_status is not None:
                result, final_status = _apply_terminal_status_to_jobs(
                    terminal_jobs,
                    loaded.payload,
                    terminal_status,
                    api_main,
                    db,
                )
                identity_was_accepted = result["status"] in {
                    "terminal_status_updated",
                    "terminal_status_already_recorded",
                }
                if result["status"] in {
                    "terminal_status_updated",
                    "terminal_status_already_recorded",
                }:
                    for terminal_job in terminal_jobs:
                        terminal_job.last_applied_capture_seq = snapshot.evidence_seq
                    _associate_capture_artifacts(loaded, associated_job, db)
                _complete_capture(
                    run,
                    db,
                    result=result,
                    status=final_status,
                    worker_id=worker_id,
                    expected_attempt=loaded.expected_attempt,
                    expected_claim_token=loaded.expected_claim_token,
                    expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
                    identity_verdict=("accepted" if identity_was_accepted else "rejected"),
                    association_verdict=(
                        "verified"
                        if identity_was_accepted
                        else "conflict"
                        if terminal_jobs
                        else "unmatched"
                    ),
                )
                _record_completed_audit(
                    audit,
                    db,
                    run,
                    created=False,
                    skipped=final_status == "skipped",
                )
                logger.info(
                    "Page capture %s handled terminal posting status %s",
                    capture_id,
                    terminal_status.evidence_code,
                )
                return run.to_api()

            raw_artifact = db.get(StoredArtifact, loaded.raw_content_artifact_id)
            archive_artifact = db.get(StoredArtifact, loaded.source_archive_artifact_id)
            if not raw_artifact or not archive_artifact:
                raise PageCaptureProcessingError(
                    "capture_artifact_missing",
                    "Captured page artifacts are unavailable",
                    retryable=False,
                )
            prepared_artifacts = (raw_artifact, archive_artifact)
            try:
                if associated_job:
                    ingest_result = api_main.update_existing_job_from_ingest_payload(
                        associated_job,
                        loaded.payload,
                        db,
                        storage,
                        settings,
                        run_fit_analysis=False,
                        parsed_job=parsed,
                        prepared_artifacts=prepared_artifacts,
                        ignored_conflict_job_ids=set(match_decision.ignored_conflict_job_ids),
                    )
                else:
                    ingest_result = api_main.persist_job_ingest_result(
                        loaded.payload,
                        db,
                        storage,
                        settings,
                        run_fit_analysis=False,
                        parsed_job=parsed,
                        prepared_artifacts=prepared_artifacts,
                    )
            except HTTPException as error:
                if _language_filter_skip(error):
                    result = {
                        "status": "skipped_by_language_filter",
                        "job_id": run.job_id,
                        "reason": _language_filter_reason(error),
                    }
                    _complete_capture(
                        run,
                        db,
                        result=result,
                        status="skipped",
                        worker_id=worker_id,
                        expected_attempt=loaded.expected_attempt,
                        expected_claim_token=loaded.expected_claim_token,
                        expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
                        identity_verdict="rejected",
                        association_verdict=match_decision.association_verdict,
                    )
                    _record_completed_audit(audit, db, run, created=False, skipped=True)
                    return run.to_api()
                raise PageCaptureProcessingError(
                    "capture_processing_rejected",
                    "Captured page processing was rejected",
                    retryable=False,
                ) from error

            job_id = str(ingest_result.payload["id"])
            job = db.get(JobPosting, job_id)
            if not job:
                raise PageCaptureProcessingError(
                    "captured_job_missing",
                    "Captured job could not be loaded",
                    retryable=True,
                )
            associate_snapshot_with_job(
                db,
                snapshot_id=snapshot.snapshot_id,
                job_id=job.id,
                association_basis=("created_job" if ingest_result.created else "parsed_identity"),
                identity_strength=run.identity_strength,
            )

            job.last_applied_capture_seq = snapshot.evidence_seq
            upsert_job_source_identity(
                db,
                job,
                provider=run.provider,
                identity_hash=run.identity_hash,
                public_source_url=run.public_source_url,
                identity_strength=run.identity_strength,
            )
            _associate_capture_artifacts(loaded, job, db)
            downstream = api_main.queue_ingestion_fit(job, db, settings)
            downstream_run_id = downstream.reprocess_run_id if downstream else None
            result = {
                "status": "created" if ingest_result.created else "updated",
                "job_id": job_id,
                "created": ingest_result.created,
                "fit_reprocess_run_id": downstream_run_id,
            }
            _complete_capture(
                run,
                db,
                result=result,
                status="succeeded",
                worker_id=worker_id,
                expected_attempt=loaded.expected_attempt,
                expected_claim_token=loaded.expected_claim_token,
                expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
                identity_verdict="accepted",
                association_verdict="verified",
            )
            _record_completed_audit(
                audit,
                db,
                run,
                created=ingest_result.created,
                skipped=False,
            )
            logger.info("Page capture %s completed for job %s", capture_id, job_id)
            return run.to_api()
    except PageCaptureClaimLost:
        logger.warning(
            "Page capture %s lost ownership before committing attempt %s",
            capture_id,
            expected_attempt,
        )
        with session_scope(settings) as db:
            current = db.get(PageCaptureRun, capture_id)
            return current.to_api() if current else None
    except Exception as error:
        retryable = page_capture_error_is_retryable(error)
        error_code = getattr(error, "error_code", None)
        result = record_page_capture_failure(
            capture_id,
            settings,
            error,
            retryable=retryable,
            error_code=error_code,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_claim_token=expected_claim_token,
            expected_supervisor_lease_token=supervisor_lease_token,
        )
        if result and result["status"] == "failed":
            logger.error(
                "Page capture %s failed permanently; error_code=%s error_type=%s",
                capture_id,
                result.get("error_code"),
                error.__class__.__name__,
            )
        elif result and result["status"] == "superseded":
            logger.info(
                "Page capture %s was superseded after attempt failure; error_code=%s",
                capture_id,
                result.get("error_code"),
            )
        else:
            logger.error(
                "Page capture %s scheduled for retry; error_code=%s error_type=%s",
                capture_id,
                result.get("error_code") if result else error.__class__.__name__,
                error.__class__.__name__,
            )
        return result


@dataclass(frozen=True)
class CaptureJobDecision:
    job: JobPosting | None
    merge_candidate_job_ids: frozenset[str]
    ignored_conflict_job_ids: frozenset[str]
    association_verdict: str


def _select_capture_job(
    target_job,
    identity_match,
    *,
    identity_strength: str,
    source_url: str,
    parsed_job=None,
    terminal: bool = False,
):
    from jobcockpit.services import workflows as api_main

    candidate_ids = {job.id for job in identity_match.jobs} if identity_match is not None else set()
    if target_job is not None:
        authoritative_conflict = bool(
            identity_match is not None
            and identity_match.identity_strength == "authoritative"
            and target_job.id not in candidate_ids
        )
        if authoritative_conflict:
            raise PageCaptureProcessingError(
                "capture_identity_conflict",
                "Capture target conflicts with the authoritative source identity",
                retryable=False,
            )
        source_matches = bool(source_url) and any(
            status_urls_match(source_url, candidate.url)
            for candidate in ordered_status_urls(target_job)
        )
        parsed_matches = bool(
            parsed_job is not None
            and api_main.parsed_job_identity_matches_existing(parsed_job, target_job)
        )
        registry_matches = target_job.id in candidate_ids
        authoritative_evidence = identity_strength == "authoritative" and (
            source_matches or registry_matches
        )
        identity_verified = parsed_matches or authoritative_evidence
        if not terminal and not identity_verified:
            raise PageCaptureProcessingError(
                "capture_identity_conflict",
                "Targeted capture did not verify the target job identity",
                retryable=False,
            )
        merge_ids = (
            frozenset(candidate_ids - {target_job.id})
            if identity_match is not None and identity_match.identity_strength == "authoritative"
            else frozenset()
        )
        return CaptureJobDecision(
            target_job,
            merge_ids,
            frozenset(candidate_ids - {target_job.id}),
            ("verified" if identity_verified else "pending"),
        )
    if identity_match is None:
        return CaptureJobDecision(
            None,
            frozenset(),
            frozenset(),
            "unmatched",
        )
    if identity_match.identity_strength == "authoritative":
        merge_ids = identity_match.authoritative_merge_job_ids
        return CaptureJobDecision(
            identity_match.job,
            merge_ids,
            merge_ids,
            "verified",
        )
    if terminal or parsed_job is None:
        return CaptureJobDecision(None, frozenset(), frozenset(), "unmatched")

    compatible = [
        job
        for job in identity_match.jobs
        if api_main.parsed_job_identity_matches_existing(parsed_job, job)
    ]
    if len(compatible) > 1:
        raise PageCaptureProcessingError(
            "capture_identity_conflict",
            "Weak capture identity remains ambiguous after parsed-field verification",
            retryable=False,
        )
    if not compatible:
        raise PageCaptureProcessingError(
            "capture_identity_conflict",
            "Weak capture identity did not match the existing job",
            retryable=False,
        )
    selected = compatible[0]
    return CaptureJobDecision(
        selected,
        frozenset(),
        frozenset(candidate_ids - {selected.id}),
        "verified",
    )


def _commit_language_skip(
    capture_id: str,
    settings: Settings,
    worker_id: str,
    loaded: LoadedPageCapture,
    audit: IngestionAuditService,
    reason: str,
) -> dict:
    with session_scope(settings) as db:
        _begin_fenced_commit(
            db,
            worker_id,
            loaded.expected_supervisor_lease_token,
        )
        run = _owned_run(
            db,
            capture_id,
            worker_id=worker_id,
            expected_attempt=loaded.expected_attempt,
            expected_claim_token=loaded.expected_claim_token,
            expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
        )
        _complete_capture(
            run,
            db,
            result={
                "status": "skipped_by_language_filter",
                "job_id": run.job_id,
                "reason": reason,
            },
            status="skipped",
            worker_id=worker_id,
            expected_attempt=loaded.expected_attempt,
            expected_claim_token=loaded.expected_claim_token,
            expected_supervisor_lease_token=(loaded.expected_supervisor_lease_token),
            identity_verdict="rejected",
            association_verdict="unmatched",
        )
        _record_completed_audit(audit, db, run, created=False, skipped=True)
        return run.to_api()


def _apply_terminal_status(
    associated_job,
    payload,
    terminal_status,
    api_main,
    db: Session,
) -> tuple[dict, str]:
    if associated_job is None:
        return (
            {
                "status": "terminal_status_detected",
                "job_id": None,
                "opportunity_status": terminal_status.status,
                "evidence_code": terminal_status.evidence_code,
            },
            "skipped",
        )
    source_matches = bool(payload.source_url) and any(
        status_urls_match(payload.source_url, candidate.url)
        for candidate in ordered_status_urls(associated_job)
    )
    if not source_matches:
        return (
            {
                "status": "terminal_status_not_applied",
                "job_id": associated_job.id,
                "opportunity_status": associated_job.status,
                "evidence_code": terminal_status.evidence_code,
            },
            "skipped",
        )
    applied = api_main.apply_inferred_opportunity_status(associated_job, payload, db)
    return (
        {
            "status": (
                "terminal_status_updated"
                if applied
                else "terminal_status_already_recorded"
                if associated_job.status == terminal_status.status
                else "terminal_status_not_applied"
            ),
            "job_id": associated_job.id,
            "opportunity_status": associated_job.status,
            "evidence_code": terminal_status.evidence_code,
        },
        "succeeded" if applied else "skipped",
    )


def _apply_terminal_status_to_jobs(
    jobs: list[JobPosting],
    payload,
    terminal_status,
    api_main,
    db: Session,
) -> tuple[dict, str]:
    if not jobs:
        return _apply_terminal_status(None, payload, terminal_status, api_main, db)

    outcomes = [_apply_terminal_status(job, payload, terminal_status, api_main, db) for job in jobs]
    result = outcomes[0][0]
    updated_job_ids = [
        outcome[0]["job_id"]
        for outcome in outcomes
        if outcome[0]["status"] == "terminal_status_updated"
    ]
    if updated_job_ids:
        return (
            {
                **result,
                "status": "terminal_status_updated",
                "opportunity_status": terminal_status.status,
                "updated_job_ids": updated_job_ids,
            },
            "succeeded",
        )
    return result, "skipped"


def _require_configured_parser_success(payload: dict, settings: Settings) -> None:
    provider = str(settings.posting_parser_provider or "").lower()
    if provider in {"", "heuristic"}:
        return
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if source.get("parser_outcome") != "provider_fallback":
        return
    error_code = str(source.get("parser_error_code") or "posting_parser_fallback")
    retryable = error_code not in {
        "codex_cli_invalid_parsed_job",
        "provider_invalid_payload",
    }
    raise PageCaptureProcessingError(
        error_code,
        "Configured posting parser did not complete successfully",
        retryable=retryable,
    )


def _associate_capture_artifacts(loaded: LoadedPageCapture, job: JobPosting, db: Session) -> None:
    for artifact_id in (
        loaded.raw_content_artifact_id,
        loaded.source_archive_artifact_id,
    ):
        artifact = db.get(StoredArtifact, artifact_id)
        if artifact and artifact.job_id is None:
            artifact.job_id = job.id
            artifact.candidate_id = "candidate_local"
            artifact.source_kind = (
                artifact.source_kind.replace("_pending", "") if artifact.source_kind else None
            )


def _complete_capture(
    run: PageCaptureRun,
    db: Session,
    *,
    result: dict,
    status: str,
    worker_id: str,
    expected_attempt: int,
    expected_claim_token: str,
    expected_supervisor_lease_token: str,
    identity_verdict: str = "pending",
    association_verdict: str = "pending",
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = datetime.now(UTC)
    if status != "superseded" and not page_capture_snapshot_is_current(db, run.snapshot_id):
        status = "superseded"
        result = {"status": "superseded", "reason": "newer_capture_exists"}
        error_code = "stale_capture_evidence"
        error_message = "Superseded because newer captured evidence exists"
        identity_verdict = "pending"
        association_verdict = "pending"
    ensure_page_capture_transition(run.status, status)
    completed = db.execute(
        update(PageCaptureRun)
        .where(
            PageCaptureRun.capture_id == run.capture_id,
            PageCaptureRun.status == "running",
            PageCaptureRun.worker_id == worker_id,
            PageCaptureRun.attempts == expected_attempt,
            PageCaptureRun.claim_token == expected_claim_token,
            PageCaptureRun.supervisor_lease_token == expected_supervisor_lease_token,
        )
        .values(
            status=status,
            result=result,
            job_id=result.get("job_id") or run.job_id,
            identity_verdict=identity_verdict,
            association_verdict=association_verdict,
            error_code=error_code,
            error_message=error_message,
            retryable=False,
            next_attempt_at=None,
            worker_id=None,
            claim_token=None,
            supervisor_lease_token=None,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session="fetch")
    )
    if completed.rowcount != 1:
        raise PageCaptureClaimLost("Page capture ownership changed before completion")
    db.refresh(run)
    upsert_page_capture_effects(
        db,
        run,
        status=status,
        outcome_code=str(result.get("status") or error_code or status),
        error_code=error_code,
        error_message=error_message,
        result=result,
        applied=status == "succeeded",
    )

    db.add(
        AuditLogEntry.create(
            f"extension.page_capture_{status}",
            "page_capture_run",
            run.capture_id,
            payload={
                "attempts": run.attempts,
                "job_id": run.job_id,
                "result_status": result.get("status"),
                "page_access_verdict": run.page_access_verdict,
                "page_kind": run.page_kind,
                "identity_verdict": run.identity_verdict,
                "association_verdict": run.association_verdict,
                "assessment_evidence_code": run.assessment_evidence_code,
            },
        )
    )


def _finish_stale_capture(
    run: PageCaptureRun,
    snapshot: PageCaptureSnapshot,
    db: Session,
    audit: IngestionAuditService,
    *,
    worker_id: str,
    loaded: LoadedPageCapture,
    job_ids: set[str] | None = None,
) -> dict | None:
    current = (
        page_capture_snapshot_is_current_for_jobs(db, snapshot.snapshot_id, job_ids)
        if job_ids is not None
        else page_capture_snapshot_is_current(db, snapshot.snapshot_id)
    )
    if current and job_ids:
        current = all(
            (job := db.get(JobPosting, job_id)) is None
            or job.last_applied_capture_seq < snapshot.evidence_seq
            for job_id in job_ids
        )
    if current:
        return None
    _complete_capture(
        run,
        db,
        result={"status": "superseded", "reason": "newer_capture_exists"},
        status="superseded",
        worker_id=worker_id,
        expected_attempt=loaded.expected_attempt,
        expected_claim_token=loaded.expected_claim_token,
        expected_supervisor_lease_token=loaded.expected_supervisor_lease_token,
        error_code="stale_capture_evidence",
        error_message="Superseded because newer captured evidence exists",
    )
    _record_completed_audit(audit, db, run, created=False, skipped=True)
    return run.to_api()


def _record_completed_audit(
    audit: IngestionAuditService,
    db: Session,
    run: PageCaptureRun,
    *,
    created: bool,
    skipped: bool,
) -> None:
    counts = empty_ingestion_counts()
    counts["items_seen_count"] = 1
    counts["jobs_created_count"] = 1 if created else 0
    counts["jobs_reused_count"] = 1 if run.job_id and not created and not skipped else 0
    counts["items_skipped_count"] = 1 if skipped else 0
    audit.record(
        db,
        lifecycle="completed",
        provider="browser_capture",
        run_id=run.capture_id,
        status=run.status,
        counts=counts,
        source_url=run.public_source_url,
    )


def _language_filter_skip(error: HTTPException) -> bool:
    return (
        error.status_code == 400
        and isinstance(error.detail, dict)
        and error.detail.get("ingestion_status") == "skipped_by_language_filter"
    )


def _language_filter_reason(error: HTTPException) -> str:
    if not isinstance(error.detail, dict):
        return "language_filter_rejected"
    language_filter = error.detail.get("language_filter")
    if not isinstance(language_filter, dict):
        return "language_filter_rejected"
    reason = str(language_filter.get("reason") or "").strip()
    return reason[:128] or "language_filter_rejected"
