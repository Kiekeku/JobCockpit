from __future__ import annotations

from jobcockpit.ingestion.inputs import JobIngestPayload as JobIngestPayload, request_source_url_dedup_safe

import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jobcockpit.services.posting_state import (
    PreparedPostingArtifacts, parsed_posting_values, prepare_posting_artifacts,
)
from jobcockpit.services.job_deduplication import (
    find_authoritative_source_duplicate,
    find_body_requisition_duplicate,
    find_linked_portal_duplicate,
    find_cross_source_duplicate,
    find_portal_identity_duplicate,
    find_source_evidence_duplicate,
    find_workday_requisition_duplicate,
    workday_requisition_identity,
    fill_missing_posting_fields,
    same_opportunity_fields,
)
from jobcockpit.ingestion.ingestion_identity import authoritative_ingestion_identity, is_verification_url

from jobcockpit.ingestion.concurrency import (
    ingestion_link_concurrency,
    ats_scope, begin_ingestion_write, bounded_call, coordinated_ingestion, track_ingestion_artifact,
    on_ingestion_failure,
)

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    begin_background_worker_lease_transaction,
    background_worker_lease_is_current,
    background_worker_lease_is_valid,
    bootstrap_background_worker,
)
from jobcockpit.api.application_documents import (
    MAX_COVER_LETTER_DOCX_UPLOAD_BYTES as MAX_COVER_LETTER_DOCX_UPLOAD_BYTES,
    begin_serialized_package_write as begin_serialized_package_write,
    cover_letter_document_to_api,
    cover_letter_generator_to_api,
    cover_letter_workspace_to_api as cover_letter_workspace_to_api,
    create_cover_letter_package_document,
    job_requires_cover_letter,
    next_application_package_version,
    prepare_cover_letter_document,
    prepare_cover_letter_word_revision as prepare_cover_letter_word_revision,
)
from jobcockpit.api.cover_letter_generation_runs import (
    COVER_LETTER_BACKGROUND_WORKER_LEASE,
    CoverLetterGenerationProcessingError,
    active_cover_letter_generation_run as active_cover_letter_generation_run,
    bootstrap_cover_letter_generation_worker,
    claim_next_cover_letter_generation,
    cover_letter_generation_requested_by_user,
    latest_cover_letter_generation_run as latest_cover_letter_generation_run,
    mark_cover_letter_generation_succeeded,
    reconcile_cover_letter_generation,
    record_cover_letter_generation_failure,
    recover_stale_cover_letter_generation_runs,
    satisfy_cover_letter_generation as satisfy_cover_letter_generation,
)
from jobcockpit.api.job_access import (
    active_applications as active_applications,
    active_job_postings as active_job_postings,
    job_is_merged_duplicate,
    job_merged_into_id,
    job_source_metadata,
    require_active_job as require_active_job,
    resolve_active_job_for_write,
)
from jobcockpit.api.ingestion_runs import (
    create_discovered_job_ingestion_items,
    create_job_ingestion_run,
    fail_job_ingestion_item,
    finish_job_ingestion_run,
    ingestion_run_alerts,
    update_job_ingestion_item,
)
from jobcockpit.api.job_reprocess_runs import JOB_REPROCESS_STALE_AFTER as JOB_REPROCESS_STALE_AFTER
from jobcockpit.api.job_reprocess_runs import (
    ACTIVE_JOB_REPROCESS_STATUSES as ACTIVE_JOB_REPROCESS_STATUSES,
    JOB_REPROCESS_TYPE_FIT_ANALYSIS,
    JOB_REPROCESS_TYPE_FULL,
    JobReprocessClaimLost,
    JobReprocessProcessingError,
    claim_job_reprocess_run,
    claim_next_job_reprocess,
    enqueue_job_reprocess,
    ensure_job_reprocess_transition,
    job_reprocess_error_is_retryable,
    job_reprocess_run_is_stale,
    latest_job_reprocess_run as latest_job_reprocess_run,
    latest_job_reprocess_runs_by_job as latest_job_reprocess_runs_by_job,
    record_job_reprocess_failure,
    recover_stale_job_reprocess_runs,
)
from jobcockpit.api.job_reprocess_sources import (
    _is_weak_reprocess_company,  # noqa: F401 - compatibility re-export
    clean_reprocess_evidence_company,  # noqa: F401 - compatibility re-export
    clean_reprocess_evidence_title,  # noqa: F401 - compatibility re-export
    company_from_source_evidence,  # noqa: F401 - compatibility re-export
    enrich_parsed_job_fit_analysis as _enrich_parsed_job_fit_analysis,
    fit_analysis_candidate_context,
    linkedin_job_url_from_text,  # noqa: F401 - compatibility re-export
    preserve_existing_company_for_weak_reprocess,
    raw_job_snapshot_text,
    reprocess_embedded_linkedin_job_url,  # noqa: F401 - compatibility re-export
    reprocess_hint_text,
    reprocess_source_is_auth_polluted,
    reprocess_source_url,
    structured_reprocess_hint,
    title_from_source_evidence,  # noqa: F401 - compatibility re-export
)
from jobcockpit.api.page_capture_processing import process_page_capture_run
from jobcockpit.api.page_capture_runs import (
    ACTIVE_PAGE_CAPTURE_STATUSES as ACTIVE_PAGE_CAPTURE_STATUSES,
    claim_next_page_capture,
    enqueue_page_capture as enqueue_page_capture,
    latest_page_capture_processing_for_job as latest_page_capture_processing_for_job,
    latest_page_capture_processing_by_job as latest_page_capture_processing_by_job,
    recover_stale_page_capture_runs,
    sync_job_source_identities,
)
from jobcockpit.api.file_responses import (
    attachment_headers as attachment_headers,
)
from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    ApplicationPackage,
    ApplicationPackageDocument,
    AuditLogEntry,
    CandidateProfile,
    CoverLetterGenerationRun,
    GeneratedDocument,
    JobIngestionItem,
    JobIngestionRun,
    JobPosting,
    JobReprocessRun,
    JobSourceIdentity,
    JobStatusCheck as JobStatusCheck,
    JobStatusScanRun as JobStatusScanRun,
    PageCaptureRun,
    StoredArtifact,
    api_datetime,
    availability_state as availability_state,
)
from jobcockpit.db.session import get_engine, session_scope

from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsProviderError, SourceArchive
from jobcockpit.ingestion.ats_adapters.registry import (
    get_ats_adapter as get_ats_adapter,
    registered_ats_providers as registered_ats_providers,
)
from jobcockpit.ingestion.browser_cleanup import retain_pending_browser_cleanup
from jobcockpit.ingestion.item_execution import execute_posting_items
from jobcockpit.ingestion.enrichment import FitAnalysisMode, ingestion_fit_mode
from jobcockpit.ingestion.fit_analysis import (
    analyze_fit_with_settings,
    require_fit_analysis_with_settings,
)
from jobcockpit.ingestion.language_filter import (
    evaluate_language_requirements,
    language_filter_error_message,
)
from jobcockpit.ingestion.parser import ParsedJob, parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.posting_batch import (
    PostingBatchItem, ats_job_needs_source_fetch, persist_posting_batch_item, prepare_posting_batch_item,
)
from jobcockpit.ingestion.posting_scope import _posting_content_scope
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text
from jobcockpit.ingestion.posting_links import (
    PostingLinkContext,
    PostingLinkClassification,
    PostingLinkProcessingResult,
    PostingIngestPayload,
    canonicalize_posting_url,
    default_posting_resolver,
    is_aggregator_fallback_source_resolution,
    is_successful_posting_link_import,
    official_discovery_outcome_for_resolution,
    posting_url_identity,
    process_posting_link as process_posting_link,
    prepare_posting_link,
    raw_text_with_resolution_evidence,
    source_resolution_for_resolution,
    source_resolution_trace_for_resolution,
)
from jobcockpit.ingestion.role_interest import match_title_interest
from jobcockpit.ingestion.posting_discovery import (
    external_aggregator_kind,
    normalize_posting_resolution,
    resolved_external_posting_terminal_status,
)
from jobcockpit.ingestion.source_resolution import (
    official_discovery_outcome,
    official_discovery_outcome_from_source_resolution,
    official_discovery_trace_source_resolution,
)
from jobcockpit.ingestion.status_reconciliation import (
    StatusCheckDecision,
    infer_status_from_page,
    ordered_status_urls,
    status_urls_match,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult

from jobcockpit.schemas.requests import (
    ExtensionPageCaptureRequest,
    JobIngestRequest,
    JobOpportunityStatus,
    AtsIngestRequest,
)
from jobcockpit.storage.artifacts import ArtifactStorageService, ArtifactStorageWriteTracker
from jobcockpit.services.worker_supervisor import LeasedWorker
from jobcockpit.utils.ids import new_id
from jobcockpit.utils.public_urls import sanitize_public_source_url


BROWSER_CAPTURE_SOURCE_TYPE = "browser_capture"
URL_SOURCE_TYPES = {
    "url",
    "gmail_alert",
    "outlook_alert",
    "ats_mock",
    "workday",
    BROWSER_CAPTURE_SOURCE_TYPE,
}
EMAIL_ALERT_SOURCE_TYPES = {"gmail_alert", "outlook_alert"}
logger = logging.getLogger(__name__)
MERGED_JOB_REPROCESS_CANCELLED_ERROR = "job_merged_into_another_posting"


def language_filter_skip_detail(result) -> dict:
    return {
        "ingestion_status": "skipped_by_language_filter",
        "error_message": language_filter_error_message(result),
        "language_filter": {
            "reason": result.reason,
            "required_languages": result.required_languages,
            "unmet_languages": result.unmet_languages,
            "evidence": result.evidence,
        },
    }


def enforce_language_filter_for_ingest_payload(
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
) -> None:
    profile = db.get(CandidateProfile, CANDIDATE_ID)
    profile_data = profile.profile_data if profile else None
    result = evaluate_language_requirements(
        request.raw_text,
        profile_data,
        skip_language_filter=bool(getattr(request, "skip_language_filter", False)),
    )
    if result.should_skip:
        raise HTTPException(status_code=400, detail=language_filter_skip_detail(result))


@dataclass(frozen=True)
class JobIngestResult:
    payload: dict
    created: bool
    duplicate_reason: str | None = None




def enrich_parsed_job_fit_analysis(
    parsed,
    *,
    raw_text: str,
    source_url: str | None,
    db: Session | None,
    settings: Settings | None,
) -> None:
    _enrich_parsed_job_fit_analysis(
        parsed,
        raw_text=raw_text,
        source_url=source_url,
        db=db,
        settings=settings,
        analyzer=analyze_fit_with_settings,
    )


def job_to_api(
    job: JobPosting,
    application: Application | None = None,
    reprocess_run: JobReprocessRun | None = None,
    page_capture_run: PageCaptureRun | dict[str, Any] | None = None,
) -> dict:
    payload = job.to_api()
    payload["application_summary"] = {
        "application_id": application.application_id if application else None,
        "application_status": application.status if application else None,
        "applied_at": api_datetime(application.applied_at)
        if application and application.applied_at
        else None,
        "decision": application.decision if application else None,
        "priority": "unknown",
        "next_action_at": application.follow_up_date if application else None,
    }
    payload["reprocess"] = job_reprocess_run_to_api(reprocess_run) if reprocess_run else None
    payload["capture_processing"] = (
        page_capture_run
        if isinstance(page_capture_run, dict)
        else page_capture_run.to_api()
        if page_capture_run
        else None
    )
    return payload


def job_reprocess_run_to_api(run: JobReprocessRun) -> dict:
    payload = run.to_api()
    payload["stale"] = job_reprocess_run_is_stale(run)
    return payload


def page_capture_source_resolution() -> dict[str, str]:
    return {
        "kind": "browser_capture",
        "provider": "user_browser_capture",
        "status": "captured",
        "reason_code": "user_submitted_page_capture",
    }


def page_capture_raw_text(request: ExtensionPageCaptureRequest) -> str:
    lines = [
        f"Page Title: {request.page_title}" if request.page_title else "",
        f"Source URL: {sanitize_public_source_url(request.source_url)}",
        request.visible_text,
    ]
    return "\n".join(line for line in lines if line).strip()


def page_capture_source_archive(request: ExtensionPageCaptureRequest) -> SourceArchive:
    if request.html:
        body = request.html.encode("utf-8")
        content_type = "text/html"
    else:
        body = request.visible_text.encode("utf-8")
        content_type = "text/plain"
    return SourceArchive(
        source_url=request.source_url,
        final_url=request.source_url,
        status_code=0,
        content_type=content_type,
        body=body,
        headers={"x-jobcockpit-capture-source": "browser_extension"},
    )


def page_capture_ingest_payload(request: ExtensionPageCaptureRequest) -> JobIngestPayload:
    return JobIngestPayload(
        source_type=BROWSER_CAPTURE_SOURCE_TYPE,
        raw_text=page_capture_raw_text(request),
        source_url=request.source_url,
        source_archive=page_capture_source_archive(request),
        source_resolution=page_capture_source_resolution(),
        skip_language_filter=request.skip_language_filter,
    )


def job_posting_snapshot_hash(request: JobIngestRequest | JobIngestPayload) -> str:
    fingerprint = request.raw_text
    source_identity_hash = getattr(request, "source_identity_hash", None)
    if source_identity_hash:
        fingerprint = f"{request.source_type}\nidentity:{source_identity_hash}\n{request.raw_text}"
    elif source_type_uses_url(request.source_type) and request.source_url:
        fingerprint = f"{request.source_type}\n{request.source_url}\n{request.raw_text}"
    return sha256(fingerprint.encode("utf-8")).hexdigest()


def source_type_uses_url(source_type: str) -> bool:
    return source_type in URL_SOURCE_TYPES or source_type.startswith("ats_")


def inferred_job_opportunity_status(
    raw_text: str, source_url: str | None
) -> JobOpportunityStatus | None:
    if not source_url:
        return None
    decision = infer_status_from_page(
        source_url,
        raw_text,
        evidence_scope="ingestion_snapshot",
    )
    return decision.detected_status if decision else None


def url_policy_result(
    request: JobIngestRequest | JobIngestPayload,
    settings: Settings | None = None,
) -> UrlPolicyResult | None:
    if getattr(request, "source_type", "") == BROWSER_CAPTURE_SOURCE_TYPE:
        return None
    if not source_type_uses_url(request.source_type) and not request.source_url:
        return None
    if not request.source_url:
        return UrlPolicyResult(False, "missing_source_url")
    settings = settings or Settings()
    return UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
        request.source_url
    )


def enforce_url_policy(
    request: JobIngestRequest | JobIngestPayload, settings: Settings | None = None
) -> None:
    result = url_policy_result(request, settings)
    if result and not result.allowed:
        raise HTTPException(status_code=400, detail=f"URL is blocked by policy: {result.reason}")


def find_duplicate_job_posting(
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
    snapshot_hash: str,
    parsed,
    *, storage: ArtifactStorageService | None = None,
) -> tuple[JobPosting | None, str | None]:
    duplicate = find_duplicate_job_posting_by_snapshot_hash(db, snapshot_hash)
    if duplicate:
        return duplicate, "snapshot_hash"
    duplicate = find_workday_requisition_duplicate(request, db, storage)
    if duplicate:
        return duplicate, "workday_requisition"
    duplicate = find_authoritative_source_duplicate(request, db)
    if duplicate:
        return duplicate, "source_url_identity"
    if (
        source_type_uses_url(request.source_type)
        and request.source_url
        and request_source_url_dedup_safe(request)
    ):
        duplicates = list(
            db.execute(
                select(JobPosting).where(JobPosting.source_url == request.source_url)
            ).scalars()
        )
        for duplicate in duplicates:
            if job_is_merged_duplicate(duplicate) or (duplicate.source or {}).get("source_url_dedup_safe") is False:
                continue
            if (
                request.source_type in EMAIL_ALERT_SOURCE_TYPES
                and not parsed_job_identity_matches_existing(parsed, duplicate)
            ):
                continue
            return duplicate, "source_url"
        # Consult the shared identity registry inside the writer transaction;
        # a per-run index cannot see a different source's concurrent insert.
        from jobcockpit.ingestion.browser_capture_adapters import capture_source_identity

        identity = capture_source_identity(request.source_url)
        if identity and identity.source_url_dedup_safe:
            candidates = db.execute(
                select(JobPosting).join(JobSourceIdentity, JobSourceIdentity.job_id == JobPosting.id)
                .where(JobSourceIdentity.provider == identity.provider,
                       JobSourceIdentity.identity_hash == identity.identity_hash)
                .order_by(JobPosting.created_at, JobPosting.id)
            ).scalars()
            for candidate in candidates:
                if job_is_merged_duplicate(candidate) or (candidate.source or {}).get("source_url_dedup_safe") is False:
                    continue
                # Registry entries are historical. Recheck the current locator
                # instead of resurrecting an obsolete association.
                current = capture_source_identity(candidate.source_url or "")
                if not current or not current.source_url_dedup_safe or (
                    current.provider, current.identity_hash
                ) != (identity.provider, identity.identity_hash):
                    continue
                if request.source_type in EMAIL_ALERT_SOURCE_TYPES and not parsed_job_identity_matches_existing(parsed, candidate):
                    continue
                return candidate, "source_url_identity"
    duplicate = find_linked_portal_duplicate(request, db)
    if duplicate:
        return duplicate, "portal_source_identity"
    duplicate = find_portal_identity_duplicate(request, parsed, db)
    if duplicate:
        return duplicate, "portal_source_identity"
    duplicate = find_source_evidence_duplicate(request, parsed, db, storage)
    if duplicate:
        return duplicate, "source_evidence_identity"
    duplicate = find_body_requisition_duplicate(request, parsed, db, storage)
    if duplicate:
        return duplicate, "body_requisition_identity"
    duplicate = find_cross_source_duplicate(request, parsed, db, storage=storage)
    return (duplicate, "cross_source_identity") if duplicate else (None, None)


def enrich_cross_source_duplicate(job, request, parsed, db, storage, *, queue_fit=True):
    """Retain supplemental evidence and fill gaps without replacing existing values."""
    snapshot_hash = job_posting_snapshot_hash(request)
    observations = list((job.source or {}).get("cross_source_matches", []))
    if any(item.get("posting_snapshot_hash") == snapshot_hash for item in observations):
        return
    artifact = storage.write_bytes(
        artifact_type="job_snapshot", content=request.raw_text.encode("utf-8"),
        original_file_name="supplemental-posting.txt", content_type="text/plain",
        candidate_id=CANDIDATE_ID, job_id=job.id, privacy_class="private",
    )
    track_ingestion_artifact(db, storage, artifact.storage_uri)
    db.add(StoredArtifact.from_record(artifact))
    # Reuse the existing aggregator cleaner, not a second parser or new rules.
    supplemental = parsed.model_copy(deep=True)
    cleaned = clean_aggregator_posting_text(request.raw_text, source_url=request.source_url)
    supplemental.role.description_raw = cleaned
    supplemental.role.description_clean = _posting_content_scope(cleaned).text
    changed = fill_missing_posting_fields(job, supplemental)
    observation = {
        "source_url": request.source_url, "source_type": request.source_type,
        "posting_snapshot_hash": snapshot_hash, "artifact_id": artifact.artifact_id,
        "enriched_fields": changed, "matched_at": datetime.now(UTC).isoformat(),
    }
    observations.append(observation)
    source = source_metadata_with_portal_url(job.source or {}, request.source_url)
    portal_url = getattr(request, "portal_source_url", None)
    if portal_url:
        source = source_metadata_with_portal_url(source, portal_url)
    job.source = {**source, "cross_source_matches": observations}
    if (external_aggregator_kind(job.source_url or "")
            and not external_aggregator_kind(request.source_url)
            and (source.get("source_resolution") or {}).get("kind") != "manual_override"
            and not is_aggregator_fallback_source_resolution(
                getattr(request, "source_resolution", None))):
        job.source = source_metadata_with_portal_url(job.source, job.source_url)
        apply_job_source_url(job, request.source_url, {
            "kind": "direct", "path": "cross_source_identity",
            "official_url": request.source_url,
            "original_url": job.source_url,
        }, db)
        application = dict(job.application or {})
        if external_aggregator_kind(application.get("external_apply_url") or ""):
            application["external_apply_url"] = request.source_url
            job.application = application
    if changed:
        quality = dict(job.data_quality or {})
        quality["missing_fields"] = [
            field for field in quality.get("missing_fields", []) if field not in changed
        ]
        job.data_quality = quality
        if queue_fit:
            enqueue_job_reprocess(job, db, run_type=JOB_REPROCESS_TYPE_FIT_ANALYSIS)
    job.updated_at = datetime.now(UTC)
    db.add(AuditLogEntry.create("job.cross_source_enriched", "job_posting", job.id,
                                payload=observation))


def should_enrich_duplicate(job, parsed, reason):
    return reason in {"cross_source_identity", "workday_requisition", "portal_source_identity", "body_requisition_identity", "source_evidence_identity"} or (
        bool((job.source or {}).get("cross_source_matches"))
        and same_opportunity_fields(parsed, job)
    )


def remember_ingestion_portal(job, request):
    """Keep a newly observed alias even when an exact ID avoids reparsing."""
    portal = getattr(request, "portal_source_url", None)
    if portal and ((job.source or {}).get("source_resolution") or {}).get("kind") != "manual_override":
        job.source = source_metadata_with_portal_url(job.source or {}, portal)


def retained_source_portals(job, source, source_url, resolution):
    """Preserve aliases across a verified promotion or refresh, never a manual reassignment."""
    if (resolution or {}).get("kind") == "manual_override":
        return source
    previous_identity = authoritative_ingestion_identity(job.source_url)
    same_identity = job.source_url == source_url or (
        previous_identity and previous_identity == authoritative_ingestion_identity(source_url)
    )
    promotion = bool(previous_identity and previous_identity[0] in {"linkedin", "indeed"}
                     and (resolution or {}).get("kind") == "official_discovery")
    if same_identity or promotion:
        for portal in portal_source_urls_from_metadata(job.source or {}):
            source = source_metadata_with_portal_url(source, portal)
    if promotion:
        source = source_metadata_with_portal_url(source, job.source_url)
    return source


def queue_ingestion_fit(job: JobPosting, db: Session, settings: Settings | None) -> JobReprocessRun | None:
    if ingestion_fit_mode(settings, run_inline=False) is FitAnalysisMode.DISABLED:
        return None
    run, _queued = enqueue_job_reprocess(job, db, run_type=JOB_REPROCESS_TYPE_FIT_ANALYSIS)
    return run


def replay_cross_source_evidence(job, observations, db, storage, settings):
    """Reparse retained evidence without creating artifacts or ingestion observations."""
    changed = set()
    for observation in observations:
        artifact = db.get(StoredArtifact, observation.get("artifact_id"))
        if (artifact is None or artifact.job_id != job.id
                or artifact.artifact_type != "job_snapshot" or artifact.deleted_at is not None):
            raise JobReprocessProcessingError(
                "supplemental_snapshot_unavailable", "Supplemental posting snapshot is unavailable",
                retryable=False,
            )
        try:
            content = storage.read_bytes(artifact.storage_uri)
            if sha256(content).hexdigest() != artifact.sha256:
                raise ValueError("Supplemental snapshot checksum does not match")
            raw_text = content.decode("utf-8")
        except (OSError, ValueError) as error:
            raise JobReprocessProcessingError(
                "supplemental_snapshot_unavailable", "Supplemental posting snapshot could not be read",
                retryable=False,
            ) from error
        parsed = parse_job_text_with_settings(
            raw_text, observation.get("source_url"), settings,
            source_type=observation.get("source_type"),
        )
        if (str(settings.posting_parser_provider or "").lower() not in {"", "heuristic"}
                and parsed.source.parser_outcome == "provider_fallback"):
            raise JobReprocessProcessingError(
                "posting_parser_fallback", "Supplemental posting parser did not complete successfully",
                retryable=True,
            )
        # A refresh can expose a different opening at a reused URL. Missing
        # fields can be supplemented, but conflicting identity fields cannot.
        identities = [
            (job.company_name, parsed.employer.company_name),
            (job.normalized_title, parsed.role.normalized_title),
            *[((job.location or {}).get(field), getattr(parsed.location, field))
              for field in ("city", "province_state", "country")],
            *[((job.employment_timing or {}).get(field), getattr(parsed.employment_timing, field))
              for field in ("start_date", "end_date", "duration_months")],
        ]
        if any(old and new and str(old).strip().casefold() != str(new).strip().casefold()
               for old, new in identities):
            continue
        cleaned = clean_aggregator_posting_text(raw_text, source_url=observation.get("source_url"))
        parsed.role.description_raw = cleaned
        parsed.role.description_clean = _posting_content_scope(cleaned).text
        changed.update(fill_missing_posting_fields(job, parsed))
    if changed:
        quality = dict(job.data_quality or {})
        quality["missing_fields"] = [field for field in quality.get("missing_fields", [])
                                     if field not in changed]
        job.data_quality = quality
        parsed = parsed_job_from_posting(job)
        enrich_parsed_job_fit_analysis(
            parsed, raw_text=(job.role or {}).get("description_raw") or "",
            source_url=job.source_url, db=db, settings=settings,
        )
        job.fit_analysis = parsed.fit_analysis.model_dump()
    return sorted(changed)


def find_duplicate_job_posting_by_snapshot_hash(
    db: Session, snapshot_hash: str
) -> JobPosting | None:
    return db.execute(
        select(JobPosting).where(JobPosting.posting_snapshot_hash == snapshot_hash)
    ).scalar_one_or_none()


def find_previously_imported_ats_job(
    db: Session,
    *,
    source_type: str,
    source_url: str | None,
    raw_text: str,
    jobs_by_source_url_identity: dict[str, str] | None = None,
) -> tuple[JobPosting | None, str | None]:
    payload = PostingIngestPayload(
        source_type=source_type, source_url=source_url, raw_text=raw_text
    )
    snapshot_hash = job_posting_snapshot_hash(payload)
    duplicate = find_duplicate_job_posting_by_snapshot_hash(db, snapshot_hash)
    if duplicate:
        return duplicate, "snapshot_hash"
    if source_url:
        duplicate = (
            db.execute(select(JobPosting).where(JobPosting.source_url == source_url))
            .scalars()
            .first()
        )
        if duplicate:
            return duplicate, "source_url"
        source_url_identity = posting_url_identity(source_url)
        duplicate_id = (jobs_by_source_url_identity or {}).get(source_url_identity)
        duplicate = db.get(JobPosting, duplicate_id) if duplicate_id else None
        if duplicate:
            return duplicate, "source_url_identity"
    return None, None


def previously_imported_jobs_by_source_url_identity(db: Session) -> dict[str, str]:
    """Batch-local identities contain IDs only; load matches in the item's session."""
    result: dict[str, str] = {}
    for job_id, url in db.execute(
        select(JobPosting.id, JobPosting.source_url).where(JobPosting.source_url.is_not(None))
    ):
        identity = posting_url_identity(url)
        if identity:
            result.setdefault(identity, job_id)
    return result


LEGACY_PORTAL_SOURCE_URL_PATTERNS = {
    "ats_hanzilla": re.compile(r"Hanzilla URL:\s*(https?://jobs\.hanzilla\.co/jobs/[^\s]+)"),
    "ats_eluta": re.compile(r"Eluta URL:\s*(https?://(?:www\.)?eluta\.ca/spl/[^\s]+)"),
    "ats_canadian_tech_internships": re.compile(r"GitHub List Apply URL:\s*(https?://[^\s]+)"),
    "ats_canadian_tech_internships_2027": re.compile(r"GitHub List Apply URL:\s*(https?://[^\s]+)"),
    "ats_canada_tech_internships_summer_2027": re.compile(
        r"GitHub List Apply URL:\s*(https?://[^\s]+)"
    ),
}

ATS_LINK_FAILURE_COOLDOWN = timedelta(hours=24)
ATS_LINK_FAILURE_COOLDOWN_OVERRIDES = {
    # Keep failed Canadian Tech links eligible while the Workday CDP fallback is
    # being debugged. Permanent failures remain excluded independently.
    "canadian_tech_internships": timedelta(0),
    "canadian_tech_internships_2027": timedelta(0),
    "csjobs": timedelta(0),
}


def legacy_portal_source_url(source_type: str, role: dict) -> str | None:
    pattern = LEGACY_PORTAL_SOURCE_URL_PATTERNS.get(source_type)
    if not pattern:
        return None
    description_raw = str(role.get("description_raw") or "")
    match = pattern.search(description_raw)
    if not match:
        return None
    return canonicalize_posting_url(match.group(1))


def portal_source_urls_from_metadata(source: dict) -> set[str]:
    source_urls: set[str] = set()
    portal_source_url = canonicalize_posting_url(str(source.get("portal_source_url") or ""))
    if portal_source_url:
        source_urls.add(portal_source_url)
    portal_source_urls = source.get("portal_source_urls")
    if isinstance(portal_source_urls, (list, tuple, set)):
        for value in portal_source_urls:
            cleaned_url = canonicalize_posting_url(str(value or ""))
            if cleaned_url:
                source_urls.add(cleaned_url)
    return source_urls


def source_metadata_with_portal_url(source: dict, portal_source_url: str) -> dict:
    cleaned_url = canonicalize_posting_url(portal_source_url)
    if not cleaned_url:
        return dict(source)
    updated_source = dict(source)
    portal_source_urls = portal_source_urls_from_metadata(updated_source)
    portal_source_urls.add(cleaned_url)
    updated_source["portal_source_url"] = cleaned_url
    updated_source["portal_source_urls"] = sorted(portal_source_urls)
    return updated_source


def previously_imported_source_urls(db: Session) -> frozenset[str]:
    source_urls = set(
        db.execute(
            select(JobPosting.source_url).where(JobPosting.source_url.is_not(None))
        ).scalars()
    )
    for source in db.execute(select(JobPosting.source)).scalars():
        if isinstance(source, dict):
            source_urls.update(portal_source_urls_from_metadata(source))
    legacy_rows = db.execute(
        select(JobPosting.source_type, JobPosting.role).where(
            JobPosting.source_type.in_(LEGACY_PORTAL_SOURCE_URL_PATTERNS)
        )
    ).all()
    for source_type, role in legacy_rows:
        if not isinstance(role, dict):
            continue
        legacy_source_url = legacy_portal_source_url(source_type, role)
        if legacy_source_url:
            source_urls.add(legacy_source_url)
    return frozenset(source_urls)


def failed_link_exclusion_urls(
    db: Session,
    *,
    provider: str,
    source_url: str,
    now: datetime | None = None,
) -> frozenset[str]:
    cooldown = ATS_LINK_FAILURE_COOLDOWN_OVERRIDES.get(
        provider,
        ATS_LINK_FAILURE_COOLDOWN,
    )
    cutoff = (now or datetime.now(UTC)) - cooldown
    item_failure_statuses = {
        "blocked_by_policy",
        "blocked_by_url_policy",
        "expired",
        "failed",
        "ignored",
        "needs_external_resolution",
        "ready_for_review",
    }
    run_rows = db.execute(
        select(JobIngestionRun.request_payload, JobIngestionRun.started_at).where(
            JobIngestionRun.provider == provider,
            JobIngestionRun.source_url == source_url,
            JobIngestionRun.status.in_(("completed", "completed_with_warnings", "failed")),
        )
    ).all()
    excluded_urls: set[str] = set()
    for payload, started_at in run_rows:
        if not isinstance(payload, dict):
            continue
        run_started_at = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at
        is_recent = run_started_at >= cutoff
        for failure in payload.get("link_failures", []):
            if not isinstance(failure, dict):
                continue
            category = str(failure.get("category") or "")
            if category != "permanent" and not is_recent:
                continue
            excluded_urls.update(_failed_link_url_variants(failure.get("source_url")))
        if is_recent:
            for value in payload.get("failed_source_urls", []):
                excluded_urls.update(_failed_link_url_variants(value))

    item_rows = db.execute(
        select(
            JobIngestionItem.source_url,
            JobIngestionItem.canonical_url,
            JobIngestionItem.retryable,
            JobIngestionRun.started_at,
        )
        .join(
            JobIngestionRun,
            JobIngestionItem.ingestion_run_id == JobIngestionRun.ingestion_run_id,
        )
        .where(
            JobIngestionRun.provider == provider,
            JobIngestionRun.source_url == source_url,
            JobIngestionRun.status.in_(("completed", "completed_with_warnings", "failed")),
            JobIngestionItem.status.in_(item_failure_statuses),
        )
    ).all()
    for item_source_url, canonical_url, retryable, started_at in item_rows:
        run_started_at = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at
        if retryable and run_started_at < cutoff:
            continue
        excluded_urls.update(_failed_link_url_variants(item_source_url))
        excluded_urls.update(_failed_link_url_variants(canonical_url))
    return frozenset(excluded_urls)


def _failed_link_url_variants(value: object) -> set[str]:
    original_url = str(value or "").strip()
    canonical_url = canonicalize_posting_url(original_url)
    return {url for url in (original_url, canonical_url) if url}


@dataclass(frozen=True)
class JobStatusScanInputFingerprint:
    candidates: tuple[tuple[str, str, str], ...]
    company: str
    title: str
    location: str


def job_status_scan_input_fingerprint(job: JobPosting) -> JobStatusScanInputFingerprint:
    role = job.role if isinstance(job.role, dict) else {}
    location = job.location if isinstance(job.location, dict) else {}
    display_title = str(role.get("job_title") or "").strip()
    if not display_title:
        display_title = str(job.normalized_title or "").replace("_", " ").strip()
    return JobStatusScanInputFingerprint(
        candidates=tuple(
            (candidate.kind, candidate.source_kind, candidate.url)
            for candidate in ordered_status_urls(job)
        ),
        company=_normalized_status_scan_identity(str(job.company_name or "")),
        title=_normalized_status_scan_identity(display_title),
        location=_normalized_status_scan_identity(
            ", ".join(
                str(location.get(key) or "").strip()
                for key in ("city", "province_state", "country")
                if str(location.get(key) or "").strip()
            )
        ),
    )


def invalidate_job_status_scan_if_inputs_changed(
    job: JobPosting,
    previous: JobStatusScanInputFingerprint,
    *,
    changed_at: datetime | None = None,
) -> bool:
    current = job_status_scan_input_fingerprint(job)
    if _status_scan_input_fingerprints_match(previous, current):
        return False

    observed_at = changed_at or datetime.now(UTC)
    if job.status == "active" and job_merged_into_id(job) is None:
        job.status_next_check_at = observed_at
        job.status_check_failure_count = 0
        job.status_check_not_found_count = 0
        job.status_source_repair_required = False
    else:
        job.status_next_check_at = None
    job.updated_at = observed_at
    return True


def _status_scan_input_fingerprints_match(
    left: JobStatusScanInputFingerprint,
    right: JobStatusScanInputFingerprint,
) -> bool:
    if (
        left.company != right.company
        or left.title != right.title
        or left.location != right.location
        or len(left.candidates) != len(right.candidates)
    ):
        return False
    return all(
        left_kind == right_kind
        and left_source_kind == right_source_kind
        and status_urls_match(left_url, right_url)
        for (
            left_kind,
            left_source_kind,
            left_url,
        ), (
            right_kind,
            right_source_kind,
            right_url,
        ) in zip(left.candidates, right.candidates, strict=True)
    )


def _normalized_status_scan_identity(value: str) -> str:
    return " ".join(re.sub(r"[^\w#+.-]+", " ", value.casefold()).split())


def find_conflicting_job_posting_for_update(
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
    snapshot_hash: str,
    current_job_id: str,
    ignored_conflict_job_ids: set[str] | None = None,
) -> tuple[JobPosting | None, str | None]:
    ignored_conflict_job_ids = ignored_conflict_job_ids or set()
    duplicate = find_duplicate_job_posting_by_snapshot_hash(db, snapshot_hash)
    if (
        duplicate
        and duplicate.id != current_job_id
        and duplicate.id not in ignored_conflict_job_ids
    ):
        return duplicate, "snapshot_hash"
    if (
        source_type_uses_url(request.source_type)
        and request.source_url
        and request_source_url_dedup_safe(request)
    ):
        duplicates = list(
            db.execute(
                select(JobPosting).where(
                    JobPosting.source_url == request.source_url,
                    JobPosting.id != current_job_id,
                )
            ).scalars()
        )
        for duplicate in duplicates:
            if duplicate.id in ignored_conflict_job_ids:
                continue
            return duplicate, "source_url"
    return None, None


def parsed_job_identity_matches_existing(parsed, job: JobPosting) -> bool:
    parsed_company = parsed.employer.company_name
    parsed_title = parsed.role.normalized_title
    company_matches = bool(
        parsed_company
        and job.company_name
        and parsed_company.casefold() == job.company_name.casefold()
    )
    parsed_company_tokens = set(re.findall(r"[a-z0-9]+", str(parsed_company or "").casefold()))
    existing_company_tokens = set(re.findall(r"[a-z0-9]+", str(job.company_name or "").casefold()))
    parsed_title_tokens = (
        set(re.findall(r"[a-z0-9]+", str(parsed_title).casefold())) - parsed_company_tokens
    )
    existing_title_tokens = (
        set(re.findall(r"[a-z0-9]+", str(job.normalized_title or "").casefold()))
        - existing_company_tokens
    )
    title_matches = bool(
        parsed_title_tokens
        and existing_title_tokens
        and (
            parsed_title_tokens == existing_title_tokens
            or (
                min(len(parsed_title_tokens), len(existing_title_tokens)) >= 2
                and (
                    parsed_title_tokens <= existing_title_tokens
                    or existing_title_tokens <= parsed_title_tokens
                )
            )
        )
    )
    if parsed_company and job.company_name and not company_matches:
        return False
    if parsed_title and job.normalized_title and not title_matches:
        return False
    return company_matches or title_matches


def prefer_resolved_non_aggregator_application_url(
    parsed: ParsedJob, source_url: str | None
) -> None:
    if not source_url or external_aggregator_kind(source_url):
        return

    current_url = parsed.application.external_apply_url
    if not current_url or external_aggregator_kind(current_url):
        parsed.application.external_apply_url = source_url


def source_metadata_for_ingest(
    parsed_source: dict,
    request: JobIngestRequest | JobIngestPayload,
) -> dict:
    source = dict(parsed_source)
    requisition = workday_requisition_identity(request.source_url, request.raw_text)
    if requisition:
        source["workday_requisition"] = requisition
    source_resolution = getattr(request, "source_resolution", None)
    if source_resolution:
        source["source_resolution"] = dict(source_resolution)
    source_url_dedup_safe = getattr(request, "source_url_dedup_safe", None)
    if source_url_dedup_safe is not None:
        source["source_url_dedup_safe"] = bool(source_url_dedup_safe)
    portal_source_url = getattr(request, "portal_source_url", None)
    if portal_source_url:
        source = source_metadata_with_portal_url(source, portal_source_url)
    return source


def job_ingest_duplicate_result(
    job: JobPosting, db: Session, reason: str | None
) -> JobIngestResult:
    application = db.execute(
        select(Application).where(Application.job_id == job.id)
    ).scalar_one_or_none()
    return JobIngestResult(
        payload=job_to_api(job, application, reprocess_run=latest_job_reprocess_run(db, job.id)),
        created=False, duplicate_reason=reason
    )


def data_quality_with_preserved_manual_review(
    job: JobPosting,
    parsed_data_quality: dict[str, Any],
    *,
    snapshot_hash: str,
    preserve_mismatched_snapshot: bool = False,
) -> dict[str, Any]:
    existing_data_quality = job.data_quality if isinstance(job.data_quality, dict) else {}
    pending_snapshot_hash = existing_data_quality.get("manual_review_pending_snapshot_hash")
    if pending_snapshot_hash and (
        pending_snapshot_hash == snapshot_hash or preserve_mismatched_snapshot
    ):
        return {
            **parsed_data_quality,
            "needs_manual_review": True,
            "manual_review_pending_snapshot_hash": pending_snapshot_hash,
        }

    completed_at = existing_data_quality.get("manual_review_completed_at")
    completed_snapshot_hash = existing_data_quality.get("manual_review_completed_snapshot_hash")
    if not completed_at or (
        completed_snapshot_hash != snapshot_hash and not preserve_mismatched_snapshot
    ):
        return parsed_data_quality
    return {
        **parsed_data_quality,
        "needs_manual_review": False,
        "manual_review_completed_at": completed_at,
        "manual_review_completed_snapshot_hash": completed_snapshot_hash,
    }


def mark_aggregator_fallback_duplicate_for_review(
    job: JobPosting,
    request: JobIngestRequest | JobIngestPayload,
) -> None:
    source_resolution = getattr(request, "source_resolution", None)
    if not is_aggregator_fallback_source_resolution(source_resolution):
        return

    existing_source = job.source if isinstance(job.source, dict) else {}
    existing_resolution = existing_source.get("source_resolution")
    if (
        isinstance(existing_resolution, dict)
        and existing_resolution.get("kind")
        and not is_aggregator_fallback_source_resolution(existing_resolution)
    ):
        return

    previous_scan_inputs = job_status_scan_input_fingerprint(job)
    job.source = {**existing_source, "source_resolution": dict(source_resolution)}
    existing_data_quality = job.data_quality if isinstance(job.data_quality, dict) else {}
    incoming_snapshot_hash = job_posting_snapshot_hash(request)
    reviewed_snapshot_hash = existing_data_quality.get("manual_review_completed_snapshot_hash")
    if reviewed_snapshot_hash != incoming_snapshot_hash:
        job.data_quality = {
            **{
                key: value
                for key, value in existing_data_quality.items()
                if not key.startswith("manual_review_")
            },
            "needs_manual_review": True,
            "manual_review_pending_snapshot_hash": incoming_snapshot_hash,
        }
    invalidate_job_status_scan_if_inputs_changed(job, previous_scan_inputs)


def apply_inferred_opportunity_status(
    job: JobPosting,
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
) -> bool:
    if not request.source_url or not _stored_status_url_matches(job, request.source_url):
        return False
    role = job.role if isinstance(job.role, dict) else {}
    expected_title = str(role.get("job_title") or "").strip()
    if not expected_title:
        expected_title = str(job.normalized_title or "").replace("_", " ").strip()
    decision = infer_status_from_page(
        request.source_url,
        request.raw_text,
        evidence_scope="ingestion_snapshot",
        expected_company=str(job.company_name or "").strip(),
        expected_title=expected_title,
    )
    if decision is None:
        return False

    return apply_opportunity_status_decision(job, decision, db, source="ingestion_snapshot")


def prepare_opportunity_status(job: JobPosting, decision: StatusCheckDecision) -> bool:
    if decision.detected_status is None or job.status != "active":
        return False

    inferred_status = decision.detected_status
    observed_at = datetime.now(UTC)
    job.status = inferred_status
    job.status_last_checked_at = observed_at
    job.status_next_check_at = None
    job.status_check_result = decision.result
    job.status_check_evidence_code = decision.evidence_code
    job.status_check_failure_count = 0
    job.status_check_not_found_count = 0
    job.status_source_repair_required = False
    job.updated_at = observed_at
    return True


def apply_opportunity_status_decision(
    job: JobPosting,
    decision: StatusCheckDecision,
    db: Session,
    *,
    source: str,
) -> bool:
    previous_status = job.status
    if not prepare_opportunity_status(job, decision):
        return False
    reconcile_job_cover_letter_generation(job, db)
    db.add(
        AuditLogEntry.create(
            "job.opportunity_status_updated",
            "job_posting",
            job.id,
            payload={
                "previous_status": previous_status,
                "status": job.status,
                "source": source,
                "evidence_code": decision.evidence_code,
            },
        )
    )
    return True


def reconcile_job_cover_letter_generation(
    job: JobPosting,
    db: Session,
) -> tuple[CoverLetterGenerationRun | None, bool]:
    should_generate = (
        job.status == "active"
        and job_merged_into_id(job) is None
        and job_requires_cover_letter(job)
    )
    return reconcile_cover_letter_generation(
        job,
        db,
        should_generate=should_generate,
    )


def _stored_status_url_matches(job: JobPosting, source_url: str) -> bool:
    return any(
        status_urls_match(source_url, candidate.url) for candidate in ordered_status_urls(job)
    )


def prepare_ingest_parsed(request, settings, *, parsed_job=None, title_hint=None) -> ParsedJob:
    parsed = parsed_job or bounded_call(
        "parse", parse_job_text_with_settings,
        request.raw_text, request.source_url, settings,
        source_type=request.source_type, title_hint=request.title_hint or title_hint,
    )
    if is_aggregator_fallback_source_resolution(request.source_resolution):
        parsed.data_quality.needs_manual_review = True
    if request_source_url_dedup_safe(request):
        prefer_resolved_non_aggregator_application_url(parsed, request.source_url)
    elif parsed.application.external_apply_url == request.source_url:
        parsed.application.external_apply_url = None
    return parsed


def finish_duplicate_ingest(job, request, db, storage, *, reason, parsed=None, queue_fit=True):
    if parsed is not None and should_enrich_duplicate(job, parsed, reason):
        enrich_cross_source_duplicate(job, request, parsed, db, storage, queue_fit=queue_fit)
    apply_inferred_opportunity_status(job, request, db)
    mark_aggregator_fallback_duplicate_for_review(job, request)
    remember_ingestion_portal(job, request)
    sync_job_source_identities(db, job)
    reconcile_job_cover_letter_generation(job, db)
    return job_ingest_duplicate_result(job, db, reason)


def persist_job_ingest_result(
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings | None = None,
    *,
    run_fit_analysis: bool | None = None,
    parsed_job: ParsedJob | None = None,
    prepared_artifacts: tuple[StoredArtifact, StoredArtifact] | None = None,
) -> JobIngestResult:
    request = JobIngestPayload.from_request(request)
    fit_mode = ingestion_fit_mode(settings, run_inline=run_fit_analysis)
    if is_verification_url(request.source_url):
        raise HTTPException(status_code=422, detail="Posting requires user verification; no job was imported")
    enforce_url_policy(request, settings)
    enforce_language_filter_for_ingest_payload(request, db)
    snapshot_hash = job_posting_snapshot_hash(request)
    duplicate = find_duplicate_job_posting_by_snapshot_hash(db, snapshot_hash)
    if duplicate:
        begin_ingestion_write(db)
        db.refresh(duplicate)
        return finish_duplicate_ingest(
            duplicate, request, db, storage, reason="snapshot_hash",
        )

    parsed = prepare_ingest_parsed(request, settings, parsed_job=parsed_job)
    duplicate, duplicate_reason = find_duplicate_job_posting(request, db, snapshot_hash, parsed, storage=storage)
    if duplicate:
        begin_ingestion_write(db)
        duplicate, duplicate_reason = find_duplicate_job_posting(request, db, snapshot_hash, parsed, storage=storage)
        if duplicate:
            db.refresh(duplicate)
            return finish_duplicate_ingest(
                duplicate, request, db, storage, reason=duplicate_reason, parsed=parsed,
                queue_fit=fit_mode is not FitAnalysisMode.DISABLED,
            )

    if fit_mode is FitAnalysisMode.INLINE:
        bounded_call(
            "parse", enrich_parsed_job_fit_analysis,
            parsed,
            raw_text=request.raw_text,
            source_url=request.source_url,
            db=db,
            settings=settings,
        )

    job_id = new_id("job")
    artifacts = prepare_posting_artifacts(
        request, storage, job_id=job_id, candidate_id=CANDIDATE_ID, existing=prepared_artifacts,
    )
    artifacts.track(db, storage)
    source = artifacts.source_metadata(source_metadata_for_ingest(parsed.source.model_dump(), request), request)

    initial_status_decision = (
        infer_status_from_page(
            request.source_url,
            request.raw_text,
            evidence_scope="ingestion_snapshot",
            expected_company=parsed.employer.company_name,
            expected_title=parsed.role.job_title,
        )
        if request.source_url
        else None
    )
    initial_status_observed_at = datetime.now(UTC) if initial_status_decision else None
    # Parsing, enrichment and file preparation are complete. The authoritative
    # recheck and all DB effects share the caller's transaction through commit.
    begin_ingestion_write(db)
    duplicate, duplicate_reason = find_duplicate_job_posting(request, db, snapshot_hash, parsed, storage=storage)
    if duplicate:
        artifacts.discard(storage)
        db.refresh(duplicate)
        return finish_duplicate_ingest(
            duplicate, request, db, storage, reason=duplicate_reason, parsed=parsed,
            queue_fit=fit_mode is not FitAnalysisMode.DISABLED,
        )
    if artifacts.created:
        artifacts.attach(db, job_id=job_id, candidate_id=CANDIDATE_ID, source_type=request.source_type)
        db.flush()
    job = JobPosting(
        id=job_id,
        source_type=request.source_type,
        source_url=request.source_url,
        posting_snapshot_hash=snapshot_hash,
        raw_content_artifact_id=artifacts.snapshot.artifact_id,
        status=(
            initial_status_decision.detected_status
            if initial_status_decision and initial_status_decision.detected_status
            else "active"
        ),
        status_last_checked_at=initial_status_observed_at,
        status_check_result=initial_status_decision.result if initial_status_decision else None,
        status_check_evidence_code=(
            initial_status_decision.evidence_code if initial_status_decision else None
        ),
        source=source,
        **parsed_posting_values(parsed),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.add(
                AuditLogEntry.create(
                    "job.ingested",
                    "job_posting",
                    job.id,
                    payload={
                        "source_type": request.source_type,
                        "source_url": request.source_url,
                        "posting_snapshot_hash": snapshot_hash,
                    },
                )
            )
            if initial_status_decision and initial_status_decision.detected_status:
                db.add(
                    AuditLogEntry.create(
                        "job.opportunity_status_updated",
                        "job_posting",
                        job.id,
                        payload={
                            "previous_status": "active",
                            "status": initial_status_decision.detected_status,
                            "source": "ingestion_snapshot",
                            "evidence_code": initial_status_decision.evidence_code,
                        },
                    )
                )
            db.flush()
        if not artifacts.created:
            artifacts.attach(db, job_id=job_id, candidate_id=CANDIDATE_ID, source_type=request.source_type)
    except IntegrityError:
        if artifacts.created:
            for row in artifacts.rows:
                db.delete(row)
            db.flush()
            artifacts.discard(storage)
        existing, duplicate_reason = find_duplicate_job_posting(request, db, snapshot_hash, parsed)
        if not existing:
            raise
        return finish_duplicate_ingest(
            existing, request, db, storage, reason=duplicate_reason, parsed=parsed,
            queue_fit=fit_mode is not FitAnalysisMode.DISABLED,
        )
    sync_job_source_identities(db, job)
    reconcile_job_cover_letter_generation(job, db)
    from jobcockpit.ingestion.source_reconciliation import enqueue_source_reconciliation

    enqueue_source_reconciliation(db, job)
    fit_run = queue_ingestion_fit(job, db, settings) if fit_mode is FitAnalysisMode.QUEUED else None
    return JobIngestResult(payload=job_to_api(job, reprocess_run=fit_run) if fit_run else job.to_api(), created=True)


def persist_job_ingest(
    request: JobIngestRequest | JobIngestPayload,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings | None = None,
) -> dict:
    return persist_job_ingest_result(request, db, storage, settings).payload


def apply_parsed_job_to_existing_posting(
    job: JobPosting,
    raw_text: str,
    settings: Settings,
    *,
    db: Session | None = None,
    source_url: str | None = None,
    parser_text: str | None = None,
) -> None:
    previous_scan_inputs = job_status_scan_input_fingerprint(job)
    resolved_source_url = source_url or job.source_url
    parsed = parse_job_text_with_settings(
        parser_text or raw_text,
        resolved_source_url,
        settings,
        source_type=job.source_type,
    )
    if parser_text is not None:
        cleaned_raw_text = clean_aggregator_posting_text(
            raw_text,
            source_url=resolved_source_url,
        )
        parsed.role.description_raw = cleaned_raw_text
        parsed.role.description_clean = _posting_content_scope(cleaned_raw_text).text
    parser_provider = str(settings.posting_parser_provider or "").lower()
    if (
        parser_provider not in {"", "heuristic"}
        and parsed.source.parser_outcome == "provider_fallback"
    ):
        raise JobReprocessProcessingError(
            "posting_parser_fallback",
            "Configured posting parser did not complete successfully",
            retryable=True,
        )
    prefer_resolved_non_aggregator_application_url(parsed, resolved_source_url)
    enrich_parsed_job_fit_analysis(
        parsed,
        raw_text=raw_text,
        source_url=resolved_source_url,
        db=db,
        settings=settings,
    )
    values = parsed_posting_values(parsed)
    values["data_quality"] = data_quality_with_preserved_manual_review(
        job, values["data_quality"], snapshot_hash=job.posting_snapshot_hash,
        preserve_mismatched_snapshot=True,
    )
    for key, value in values.items():
        setattr(job, key, value)
    updated_at = datetime.now(UTC)
    job.updated_at = updated_at
    invalidate_job_status_scan_if_inputs_changed(
        job,
        previous_scan_inputs,
        changed_at=updated_at,
    )


def apply_parsed_employment_timing_to_existing_posting(
    job: JobPosting,
    raw_text: str,
    *,
    db: Session,
) -> bool:
    """Reparse and persist only parser-owned employment timing fields."""

    parsed = parse_job_text(
        raw_text,
        job.source_url,
        source_type=job.source_type,
        title_hint=str((job.role or {}).get("job_title") or "") or None,
    )
    previous_timing = deepcopy(job.employment_timing or {})
    next_timing = parsed.employment_timing.model_dump()
    previous_duration = job.duration_months
    previous_quality = deepcopy(job.data_quality or {})
    next_quality = deepcopy(previous_quality)
    for field_name in ("missing_fields", "ambiguous_fields", "inferred_fields"):
        existing_fields = previous_quality.get(field_name)
        preserved_fields = (
            [
                field
                for field in existing_fields
                if isinstance(field, str) and not field.startswith("employment_timing.")
            ]
            if isinstance(existing_fields, list)
            else []
        )
        parsed_fields = [
            field
            for field in getattr(parsed.data_quality, field_name)
            if field.startswith("employment_timing.")
        ]
        next_quality[field_name] = list(dict.fromkeys([*preserved_fields, *parsed_fields]))
    next_quality["needs_manual_review"] = bool(
        next_quality["missing_fields"] or next_quality["ambiguous_fields"]
    )
    next_quality = data_quality_with_preserved_manual_review(
        job,
        next_quality,
        snapshot_hash=job.posting_snapshot_hash,
        preserve_mismatched_snapshot=True,
    )
    if (
        previous_timing == next_timing
        and previous_duration == parsed.employment_timing.duration_months
        and previous_quality == next_quality
    ):
        return False

    previous_scan_inputs = job_status_scan_input_fingerprint(job)
    job.employment_timing = next_timing
    job.duration_months = parsed.employment_timing.duration_months
    job.data_quality = next_quality

    updated_at = datetime.now(UTC)
    job.updated_at = updated_at
    invalidate_job_status_scan_if_inputs_changed(
        job,
        previous_scan_inputs,
        changed_at=updated_at,
    )
    db.add(
        AuditLogEntry.create(
            "job.employment_timing_reparsed",
            "job_posting",
            job.id,
            payload={
                "posting_snapshot_hash": job.posting_snapshot_hash,
                "previous_employment_timing": previous_timing,
                "employment_timing": next_timing,
                "previous_duration_months": previous_duration,
                "duration_months": job.duration_months,
                "previous_data_quality": previous_quality,
                "data_quality": next_quality,
            },
        )
    )
    db.flush()
    return True


@dataclass(frozen=True)
class PreparedJobUpdate:
    request: JobIngestPayload
    values: dict[str, Any]
    artifacts: PreparedPostingArtifacts


def prepare_existing_job_update(
    job: JobPosting, request: JobIngestPayload, db: Session,
    storage: ArtifactStorageService, settings: Settings, *, run_fit_analysis: bool = True,
    parsed_job: ParsedJob | None = None,
    prepared_artifacts: tuple[StoredArtifact, StoredArtifact] | None = None,
) -> PreparedJobUpdate:
    """Prepare an update without changing the job or adding any database rows."""
    request = JobIngestPayload.from_request(request)
    if is_verification_url(request.source_url):
        raise HTTPException(status_code=422, detail="Posting requires user verification; no job was updated")
    enforce_url_policy(request, settings)
    snapshot_hash = job_posting_snapshot_hash(request)
    parsed = prepare_ingest_parsed(
        request, settings, parsed_job=parsed_job,
        title_hint=str((job.role or {}).get("job_title") or "") or None,
    )
    if run_fit_analysis:
        enrich_parsed_job_fit_analysis(
            parsed, raw_text=request.raw_text, source_url=request.source_url, db=db, settings=settings,
        )
    preserve_existing_company_for_weak_reprocess(job, parsed)
    values = parsed_posting_values(parsed, include_fit=run_fit_analysis)
    values["data_quality"] = data_quality_with_preserved_manual_review(
        job, values["data_quality"], snapshot_hash=snapshot_hash,
    )
    artifacts = prepare_posting_artifacts(
        request, storage, job_id=job.id, candidate_id=CANDIDATE_ID, existing=prepared_artifacts,
    )
    source = artifacts.source_metadata(source_metadata_for_ingest(parsed.source.model_dump(), request), request)
    values.update(
        source_type=request.source_type, source_url=request.source_url,
        posting_snapshot_hash=snapshot_hash, raw_content_artifact_id=artifacts.snapshot.artifact_id,
        source=retained_source_portals(job, source, request.source_url, request.source_resolution),
    )
    return PreparedJobUpdate(request, values, artifacts)


def apply_prepared_job_values(job: JobPosting, prepared: PreparedJobUpdate) -> None:
    previous_scan_inputs = job_status_scan_input_fingerprint(job)
    for key, value in prepared.values.items():
        setattr(job, key, deepcopy(value))
    job.updated_at = datetime.now(UTC)
    invalidate_job_status_scan_if_inputs_changed(job, previous_scan_inputs, changed_at=job.updated_at)


def ensure_prepared_update_has_no_conflict(
    job: JobPosting, prepared: PreparedJobUpdate, db: Session,
    ignored_conflict_job_ids: set[str] | None = None,
) -> None:
    duplicate, reason = find_conflicting_job_posting_for_update(
        prepared.request, db, prepared.values["posting_snapshot_hash"], job.id, ignored_conflict_job_ids,
    )
    if duplicate:
        raise HTTPException(
            status_code=409, detail=f"Refreshed posting matches a different existing job by {reason}",
        )


def update_existing_job_from_ingest_payload(
    job: JobPosting, request: JobIngestPayload, db: Session,
    storage: ArtifactStorageService, settings: Settings,
    ignored_conflict_job_ids: set[str] | None = None, *, run_fit_analysis: bool | None = None,
    parsed_job: ParsedJob | None = None,
    prepared_artifacts: tuple[StoredArtifact, StoredArtifact] | None = None,
) -> JobIngestResult:
    job = resolve_active_job_for_write(job, db)
    fit_mode = ingestion_fit_mode(settings, run_inline=run_fit_analysis)
    prepared = prepare_existing_job_update(
        job, request, db, storage, settings, run_fit_analysis=fit_mode is FitAnalysisMode.INLINE,
        parsed_job=parsed_job, prepared_artifacts=prepared_artifacts,
    )
    prepared.artifacts.track(db, storage)
    ensure_prepared_update_has_no_conflict(job, prepared, db, ignored_conflict_job_ids)
    prepared.artifacts.attach(
        db, job_id=job.id, candidate_id=CANDIDATE_ID, source_type=prepared.request.source_type,
    )
    db.flush()
    apply_prepared_job_values(job, prepared)
    application = db.execute(select(Application).where(Application.job_id == job.id)).scalar_one_or_none()
    sync_job_source_identities(db, job)
    reconcile_job_cover_letter_generation(job, db)
    fit_run = queue_ingestion_fit(job, db, settings) if fit_mode is FitAnalysisMode.QUEUED else None
    return JobIngestResult(payload=job_to_api(job, application, reprocess_run=fit_run), created=False)


def prepare_reprocess_source(
    job: JobPosting,
    raw_text: str,
    settings: Settings,
) -> PostingLinkProcessingResult | None:
    auth_polluted_source = reprocess_source_is_auth_polluted(job, raw_text)
    source_url = reprocess_source_url(job, raw_text)
    if not source_url:
        return None
    hint_text = reprocess_hint_text(job, raw_text)

    if auth_polluted_source:
        direct_result = prepare_auth_polluted_reprocess_source(
            job,
            source_url,
            hint_text,
            settings,
        )
        if direct_result is not None:
            return direct_result

    return prepare_posting_link(
        PostingLinkContext(
            source_type=job.source_type,
            source_url=source_url,
            raw_text=hint_text,
            link_text=" ".join(
                part
                for part in (
                    str((job.role or {}).get("job_title") or ""),
                    str((job.employer or {}).get("company_name") or ""),
                )
                if part
            ),
            surrounding_text=hint_text,
        ),
        settings=settings,
        posting_resolver=default_posting_resolver(settings),
    )


def prepare_auth_polluted_reprocess_source(
    job: JobPosting,
    source_url: str,
    hint_text: str,
    settings: Settings,
) -> PostingLinkProcessingResult | None:
    hint = structured_reprocess_hint(job, source_url, hint_text)
    if not hint.title or not hint.company:
        return None
    resolver = default_posting_resolver(settings)
    classification = PostingLinkClassification(
        classification="requires_external_resolution",
        confidence=0.9,
        reason="auth_polluted_reprocess_structured_hint",
    )
    if resolver is None:
        outcome = official_discovery_outcome("posting_resolver_not_configured")
        source_resolution = official_discovery_trace_source_resolution(outcome=outcome)
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="needs_external_resolution",
            original_url=source_url,
            resolved_url=source_url,
            canonical_url=source_url,
            error_message=outcome.reason_code,
            hint=hint,
            official_discovery_outcome=outcome,
            source_resolution=source_resolution,
        )
    try:
        resolution = normalize_posting_resolution(resolver.resolve(hint))
    except Exception:
        outcome = official_discovery_outcome("posting_resolver_failed")
        source_resolution = official_discovery_trace_source_resolution(outcome=outcome)
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="needs_external_resolution",
            original_url=source_url,
            resolved_url=source_url,
            canonical_url=source_url,
            error_message=outcome.reason_code,
            hint=hint,
            official_discovery_outcome=outcome,
            source_resolution=source_resolution,
        )
    if resolution.status != "resolved" or resolution.posting is None:
        outcome = official_discovery_outcome_for_resolution(resolution)
        source_resolution = source_resolution_trace_for_resolution(resolution)
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="needs_external_resolution",
            original_url=source_url,
            resolved_url=source_url,
            canonical_url=source_url,
            error_message=outcome.reason_code,
            hint=hint,
            official_discovery_outcome=outcome,
            source_resolution=source_resolution,
        )
    policy_result = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
        resolution.posting.source_url
    )
    if not policy_result.allowed:
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="blocked_by_policy",
            original_url=source_url,
            resolved_url=resolution.posting.source_url,
            canonical_url=resolution.posting.source_url,
            error_message=policy_result.reason,
            hint=hint,
            official_discovery_outcome=official_discovery_outcome_for_resolution(resolution),
            source_resolution=source_resolution_trace_for_resolution(resolution),
        )
    terminal_status = resolved_external_posting_terminal_status(resolution.posting)
    if terminal_status is not None:
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="terminal_status_detected",
            original_url=source_url,
            resolved_url=resolution.posting.source_url,
            canonical_url=resolution.posting.source_url,
            hint=hint,
            source_resolution=source_resolution_trace_for_resolution(resolution),
            detected_status=terminal_status.status,
            status_evidence_code=terminal_status.evidence_code,
        )
    source_resolution = source_resolution_for_resolution(source_url, resolution)
    outcome = official_discovery_outcome_from_source_resolution(source_resolution)
    fallback_resolution = is_aggregator_fallback_source_resolution(source_resolution)
    request = JobIngestPayload(
        source_type=job.source_type,
        raw_text=raw_text_with_resolution_evidence(resolution.posting, hint, source_resolution=source_resolution),
        source_url=resolution.posting.source_url,
        source_archive=resolution.posting.source_archive,
        source_resolution=source_resolution,
    )
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="fallback_imported" if fallback_resolution else "imported",
        original_url=source_url,
        resolved_url=resolution.posting.source_url,
        canonical_url=resolution.posting.source_url,
        ingest_request=request,
        error_message=outcome.reason_code if fallback_resolution and outcome is not None else "",
        hint=hint,
        official_discovery_outcome=outcome,
        source_resolution=source_resolution,
    )


def merge_reprocess_source_resolution(
    job: JobPosting,
    source_resolution: dict[str, Any],
) -> dict[str, Any]:
    existing_source = job.source if isinstance(job.source, dict) else {}
    existing_resolution = existing_source.get("source_resolution")
    if source_resolution.get("kind"):
        merged_resolution = dict(source_resolution)
    else:
        merged_resolution = {
            **(existing_resolution if isinstance(existing_resolution, dict) else {}),
            **source_resolution,
        }
    for trace_key in ("attempts", "trace_truncated"):
        if trace_key not in source_resolution:
            merged_resolution.pop(trace_key, None)
    job.source = {**existing_source, "source_resolution": merged_resolution}
    return merged_resolution


def prepare_full_job_reprocess(
    job: JobPosting,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
) -> tuple[dict, PreparedJobUpdate | None]:
    retained_source = deepcopy(job.source or {})
    observations = retained_source.get("cross_source_matches", [])
    raw_text = raw_job_snapshot_text(job, db, storage)
    refresh_result = prepare_reprocess_source(job, raw_text, settings)
    from jobcockpit.ingestion.posting_completeness import require_complete_posting

    if refresh_result and refresh_result.ingest_request is not None:
        require_complete_posting(
            refresh_result.ingest_request.raw_text, refresh_result.ingest_request.source_url or "",
        )
    posting_update = None
    if refresh_result and refresh_result.ingest_request is not None:
        posting_update = prepare_existing_job_update(
            job, refresh_result.ingest_request, db, storage, settings,
        )
        apply_prepared_job_values(job, posting_update)
    refreshed = refresh_result is not None and is_successful_posting_link_import(
        refresh_result.ingestion_status
    )
    fallback_refreshed = (
        refresh_result is not None and refresh_result.ingestion_status == "fallback_imported"
    )
    terminal_status_detected = bool(
        refresh_result
        and refresh_result.detected_status
        and refresh_result.status_evidence_code
        and _stored_status_url_matches(job, refresh_result.resolved_url)
    )
    if terminal_status_detected and refresh_result is not None:
        decision = StatusCheckDecision(
            refresh_result.detected_status,
            refresh_result.status_evidence_code or "",
            refresh_result.resolved_url,
            external_aggregator_kind(refresh_result.resolved_url),
            refresh_result.detected_status,
            200,
        )
        prepare_opportunity_status(job, decision)
    elif not refreshed:
        apply_parsed_job_to_existing_posting(
            job,
            raw_text,
            settings,
            db=db,
            parser_text=reprocess_hint_text(job, raw_text),
        )
    replayed_fields = []
    if observations:
        # Successful refresh replaces source metadata. Keep the original
        # observations and aliases so subsequent imports still resolve here.
        job.source = {**(job.source or {}), "cross_source_matches": observations}
        for url in portal_source_urls_from_metadata(retained_source):
            job.source = source_metadata_with_portal_url(job.source, url)
        if not terminal_status_detected:
            replayed_fields = replay_cross_source_evidence(job, observations, db, storage, settings)
    reprocess_result = {
        "status": "terminal_status_updated"
        if terminal_status_detected
        else "fallback_refreshed"
        if fallback_refreshed
        else "refreshed"
        if refreshed
        else "snapshot_reparsed",
        "refreshed": refreshed,
        "ingestion_status": refresh_result.ingestion_status
        if refresh_result
        else "snapshot_reparsed",
        "resolved_url": refresh_result.resolved_url if refresh_result else job.source_url,
        "error_message": refresh_result.error_message if refresh_result else "",
    }
    if observations:
        reprocess_result["supplemental_enriched_fields"] = replayed_fields
    if terminal_status_detected and refresh_result is not None:
        reprocess_result["detected_status"] = refresh_result.detected_status
        reprocess_result["status_evidence_code"] = refresh_result.status_evidence_code
    if refresh_result:
        source_resolution = refresh_result.source_resolution
        if source_resolution is None and refresh_result.payload:
            payload_resolution = refresh_result.payload.get("source_resolution")
            if isinstance(payload_resolution, dict):
                source_resolution = payload_resolution
        if source_resolution:
            reprocess_result["source_resolution"] = merge_reprocess_source_resolution(
                job,
                source_resolution,
            )
    return reprocess_result, posting_update


def parsed_job_from_posting(job: JobPosting) -> ParsedJob:
    source = {
        "platform": "manual",
        "source_type": job.source_type,
        "source_url": job.source_url,
        "raw_content_artifact_id": job.raw_content_artifact_id,
        **(job.source or {}),
    }
    return ParsedJob.model_validate(
        {
            "source": source,
            "employer": job.employer,
            "role": job.role,
            "employment_timing": job.employment_timing,
            "location": job.location,
            "eligibility": job.eligibility,
            "compensation": job.compensation,
            "application": job.application,
            "fit_analysis": job.fit_analysis,
            "data_quality": job.data_quality,
        }
    )


def prepare_job_fit_analysis(
    job: JobPosting,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
) -> dict:
    raw_text = raw_job_snapshot_text(job, db, storage)
    if (job.source or {}).get("cross_source_matches"):
        raw_text = (job.role or {}).get("description_raw") or raw_text
    parsed = parsed_job_from_posting(job)
    profile_data, evidence_items = fit_analysis_candidate_context(db)
    parsed.fit_analysis = require_fit_analysis_with_settings(
        parsed,
        raw_text=raw_text,
        source_url=job.source_url,
        settings=settings,
        profile_data=profile_data,
        evidence_items=evidence_items,
    )
    job.fit_analysis = parsed.fit_analysis.model_dump()
    job.updated_at = datetime.now(UTC)
    result = {
        "status": "fit_analysis_refreshed",
        "fit_score": parsed.fit_analysis.fit_score,
    }
    return result


@dataclass(frozen=True)
class JobReprocessComputation:
    run_type: str
    job_updated_at: datetime
    status_before: str
    job_values: dict[str, Any]
    posting_update: PreparedJobUpdate | None
    result: dict[str, Any]


def _mapped_row_values(instance, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    return {
        attribute.key: deepcopy(getattr(instance, attribute.key))
        for attribute in instance.__mapper__.column_attrs
        if attribute.key not in excluded
    }


def apply_job_source_url(
    job: JobPosting, source_url: str, source_resolution: dict, db: Session
) -> None:
    if is_verification_url(source_url):
        raise HTTPException(status_code=422, detail="Posting requires user verification; source was not changed")
    previous_scan_inputs = job_status_scan_input_fingerprint(job)
    previous_source_url = job.source_url
    existing_source = job.source if isinstance(job.source, dict) else {}
    existing_source = retained_source_portals(job, existing_source, source_url, source_resolution)
    job.source_url = source_url
    job.source = {
        **existing_source,
        "source_url": source_url,
        "source_url_dedup_safe": True,
        "source_resolution": source_resolution,
    }
    now = datetime.now(UTC)
    job.updated_at = now
    invalidate_job_status_scan_if_inputs_changed(job, previous_scan_inputs, changed_at=now)
    sync_job_source_identities(db, job)
    db.add(
        AuditLogEntry.create(
            "job.source_url_updated",
            "job_posting",
            job.id,
            payload={
                "previous_source_url": previous_source_url,
                "source_url": source_url,
            },
        )
    )


def prepare_job_reprocess(
    job: JobPosting, db: Session, storage: ArtifactStorageService, settings: Settings,
    *, run_type: str = JOB_REPROCESS_TYPE_FULL,
) -> JobReprocessComputation:
    """Calculate changes on a detached copy; the caller owns the eventual write."""
    baseline = _mapped_row_values(job)
    working_job = JobPosting(**deepcopy(baseline))
    posting_update = None
    if run_type == JOB_REPROCESS_TYPE_FULL:
        result, posting_update = prepare_full_job_reprocess(working_job, db, storage, settings)
    elif run_type == JOB_REPROCESS_TYPE_FIT_ANALYSIS:
        result = prepare_job_fit_analysis(working_job, db, storage, settings)
    else:
        raise ValueError(f"Unsupported job reprocess type: {run_type}")
    changes = {
        key: value for key, value in _mapped_row_values(working_job, exclude={"id", "created_at"}).items()
        if value != baseline[key]
    }
    return JobReprocessComputation(
        run_type=run_type, job_updated_at=job.updated_at, status_before=job.status,
        job_values=changes, posting_update=posting_update, result=deepcopy(result),
    )


def compute_job_reprocess(
    job_id: str, run_type: str, settings: Settings, storage: ArtifactStorageWriteTracker,
) -> JobReprocessComputation | None:
    """Read the persisted inputs and prepare changes without any database writes."""
    with Session(bind=get_engine(settings), autoflush=False) as read_db:
        job = read_db.get(JobPosting, job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        if job_is_merged_duplicate(job):
            return None
        computation = prepare_job_reprocess(job, read_db, storage, settings, run_type=run_type)
        if read_db.new or read_db.dirty or read_db.deleted:
            raise RuntimeError("Reprocess preparation changed database state")
        return computation


def execute_job_reprocess(
    job: JobPosting, db: Session, storage: ArtifactStorageService, settings: Settings,
) -> dict:
    """Synchronous composition for callers that already own a write transaction."""
    tracked_storage = ArtifactStorageWriteTracker(storage)
    try:
        computation = prepare_job_reprocess(job, db, tracked_storage, settings)
        if computation.posting_update:
            computation.posting_update.artifacts.track(db, storage)
        apply_job_reprocess_computation(job, computation, db)
        db.flush()
    except BaseException:
        tracked_storage.rollback()
        raise
    return computation.result


def apply_job_reprocess_computation(
    job: JobPosting,
    computation: JobReprocessComputation,
    db: Session,
) -> None:
    if job.updated_at != computation.job_updated_at:
        raise JobReprocessProcessingError(
            "job_changed_during_reprocess",
            "Job changed while background reprocessing was running",
            retryable=True,
        )
    if computation.posting_update:
        ensure_prepared_update_has_no_conflict(job, computation.posting_update, db)
        computation.posting_update.artifacts.attach(
            db, job_id=job.id, candidate_id=CANDIDATE_ID,
            source_type=computation.posting_update.request.source_type,
        )
        db.flush()
    for key, value in computation.job_values.items():
        setattr(job, key, deepcopy(value))
    sync_job_source_identities(db, job)
    reconcile_job_cover_letter_generation(job, db)
    event_type = (
        "job.fit_analysis_reprocessed"
        if computation.run_type == JOB_REPROCESS_TYPE_FIT_ANALYSIS
        else "job.reprocessed"
    )
    db.add(
        AuditLogEntry.create(
            event_type,
            "job_posting",
            job.id,
            payload={
                "raw_content_artifact_id": job.raw_content_artifact_id,
                **computation.result,
            },
        )
    )
    if computation.status_before != job.status:
        db.add(
            AuditLogEntry.create(
                "job.opportunity_status_updated",
                "job_posting",
                job.id,
                payload={
                    "previous_status": computation.status_before,
                    "status": job.status,
                    "source": "job_reprocess",
                    "evidence_code": job.status_check_evidence_code,
                },
            )
        )


def process_next_job_reprocess(
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
                return None
            bootstrap_background_worker(
                settings,
                worker_id=worker_id,
                lease_token=lease.lease_token,
            )
            return process_next_job_reprocess(
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=lease.lease_token,
            )
    reprocess_run_id = claim_next_job_reprocess(
        settings,
        worker_id=worker_id,
        supervisor_lease_token=supervisor_lease_token,
    )
    if not reprocess_run_id:
        return None
    return process_job_reprocess_run(
        reprocess_run_id,
        settings,
        storage,
        worker_id=worker_id,
        supervisor_lease_token=supervisor_lease_token,
    )


def process_job_reprocess_run(
    reprocess_run_id: str,
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
                    current = db.get(JobReprocessRun, reprocess_run_id)
                    return current.to_api() if current else None
            bootstrap_background_worker(
                settings,
                worker_id=worker_id,
                lease_token=lease.lease_token,
            )
            return process_job_reprocess_run(
                reprocess_run_id,
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=lease.lease_token,
            )
    storage = storage or ArtifactStorageService(settings.artifact_root)
    tracked_storage = ArtifactStorageWriteTracker(storage)
    expected_attempt: int | None = None
    expected_claim_token: str | None = None
    claimed_job_id: str | None = None
    claimed_run_type: str | None = None
    try:
        # The claim must commit before processing starts so a later failure can
        # durably transition this attempt instead of rolling the claim back.
        with session_scope(settings) as db:
            if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
                return None

            run = db.get(JobReprocessRun, reprocess_run_id)
            if not run:
                return None
            if run.status in {"queued", "retry_scheduled"}:
                run = claim_job_reprocess_run(
                    db,
                    run.reprocess_run_id,
                    worker_id=worker_id,
                    supervisor_lease_token=supervisor_lease_token,
                )
                if not run:
                    latest = db.get(JobReprocessRun, reprocess_run_id)
                    return latest.to_api() if latest else None
            if (
                run.status != "running"
                or run.worker_id != worker_id
                or run.supervisor_lease_token != supervisor_lease_token
                or not background_worker_lease_is_valid(db, worker_id, supervisor_lease_token)
            ):
                return run.to_api()
            expected_attempt = run.attempts
            expected_claim_token = run.claim_token
            claimed_job_id = run.job_id
            claimed_run_type = run.run_type

        if claimed_job_id is None or claimed_run_type is None:
            raise JobReprocessClaimLost("Job reprocess claim is incomplete")
        computation = compute_job_reprocess(
            claimed_job_id,
            claimed_run_type,
            settings,
            tracked_storage,
        )

        with session_scope(settings) as db:
            if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
                raise JobReprocessClaimLost("Job reprocess supervisor lease changed")

            run = db.get(JobReprocessRun, reprocess_run_id)
            if (
                not run
                or run.status != "running"
                or run.worker_id != worker_id
                or run.attempts != expected_attempt
                or run.claim_token != expected_claim_token
                or run.supervisor_lease_token != supervisor_lease_token
                or not background_worker_lease_is_valid(db, worker_id, supervisor_lease_token)
            ):
                raise JobReprocessClaimLost("Job reprocess ownership changed after computation")
            job = db.get(JobPosting, run.job_id)
            if not job:
                raise ValueError(f"Job not found: {run.job_id}")

            now = datetime.now(UTC)
            if job_is_merged_duplicate(job):
                tracked_storage.rollback()
                ensure_job_reprocess_transition(run.status, "cancelled")
                merged_into_job_id = job_merged_into_id(job)
                result = {**(run.result or {}), "merged_into_job_id": merged_into_job_id}
                completed = db.execute(
                    update(JobReprocessRun)
                    .where(
                        JobReprocessRun.reprocess_run_id == reprocess_run_id,
                        JobReprocessRun.status == "running",
                        JobReprocessRun.worker_id == worker_id,
                        JobReprocessRun.attempts == expected_attempt,
                        JobReprocessRun.claim_token == expected_claim_token,
                        JobReprocessRun.supervisor_lease_token == supervisor_lease_token,
                    )
                    .values(
                        status="cancelled",
                        result=result,
                        next_attempt_at=None,
                        retryable=False,
                        error_message=MERGED_JOB_REPROCESS_CANCELLED_ERROR,
                        finished_at=now,
                        worker_id=None,
                        claim_token=None,
                        supervisor_lease_token=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session="fetch")
                )
                if completed.rowcount != 1:
                    raise JobReprocessClaimLost(
                        "Job reprocess ownership changed before cancellation"
                    )
                db.add(
                    AuditLogEntry.create(
                        "job.reprocess_cancelled",
                        "job_reprocess_run",
                        reprocess_run_id,
                        payload={
                            "job_id": job.id,
                            "merged_into_job_id": merged_into_job_id,
                            "error_message": MERGED_JOB_REPROCESS_CANCELLED_ERROR,
                        },
                    )
                )
                db.refresh(run)
                return run.to_api()

            if computation is None:
                raise JobReprocessClaimLost("Job merge state changed before reprocess commit")
            apply_job_reprocess_computation(job, computation, db)
            result = computation.result

            if not background_worker_lease_is_current(settings, worker_id, supervisor_lease_token):
                raise JobReprocessClaimLost("Job reprocess supervisor lease changed before commit")

            ensure_job_reprocess_transition(run.status, "succeeded")
            completed = db.execute(
                update(JobReprocessRun)
                .where(
                    JobReprocessRun.reprocess_run_id == reprocess_run_id,
                    JobReprocessRun.status == "running",
                    JobReprocessRun.worker_id == worker_id,
                    JobReprocessRun.attempts == expected_attempt,
                    JobReprocessRun.claim_token == expected_claim_token,
                    JobReprocessRun.supervisor_lease_token == supervisor_lease_token,
                )
                .values(
                    status="succeeded",
                    result=result,
                    next_attempt_at=None,
                    error_code=None,
                    retryable=False,
                    error_message=None,
                    finished_at=now,
                    worker_id=None,
                    claim_token=None,
                    supervisor_lease_token=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
            if completed.rowcount != 1:
                raise JobReprocessClaimLost("Job reprocess ownership changed before completion")
            db.add(
                AuditLogEntry.create(
                    "job.reprocess_succeeded",
                    "job_reprocess_run",
                    run.reprocess_run_id,
                    payload={"job_id": run.job_id, "type": run.run_type, "result": result},
                )
            )
            db.refresh(run)
            return run.to_api()
    except JobReprocessClaimLost:
        tracked_storage.rollback()
        logger.warning(
            "Job reprocess %s lost ownership before committing attempt %s",
            reprocess_run_id,
            expected_attempt,
        )
        with session_scope(settings) as db:
            current = db.get(JobReprocessRun, reprocess_run_id)
            return current.to_api() if current else None
    except Exception as error:
        tracked_storage.rollback()
        return record_job_reprocess_failure(
            reprocess_run_id,
            settings,
            error,
            retryable=job_reprocess_error_is_retryable(error),
            error_code=getattr(error, "error_code", None),
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_claim_token=expected_claim_token,
            expected_supervisor_lease_token=supervisor_lease_token,
        )


def process_next_cover_letter_generation(
    settings: Settings,
    storage: ArtifactStorageService | None = None,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> dict | None:
    worker_id = worker_id or new_id("worker")
    storage = storage or ArtifactStorageService(settings.artifact_root)
    if supervisor_lease_token is None:
        with BackgroundWorkerLeaseGuard(
            settings,
            worker_id,
            lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
        ) as lease:
            if not lease.acquired or lease.lease_token is None:
                return None
            bootstrap_cover_letter_generation_worker(
                settings,
                worker_id=worker_id,
                lease_token=lease.lease_token,
            )
            return process_next_cover_letter_generation(
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=lease.lease_token,
            )
    generation_run_id = claim_next_cover_letter_generation(
        settings,
        worker_id=worker_id,
        supervisor_lease_token=supervisor_lease_token,
    )
    if not generation_run_id:
        return None
    return process_cover_letter_generation_run(
        generation_run_id,
        settings,
        storage,
        worker_id=worker_id,
        supervisor_lease_token=supervisor_lease_token,
    )


def completed_cover_letter_generation_draft_response(
    run: CoverLetterGenerationRun,
    db: Session,
    settings: Settings,
) -> dict | None:
    if run.status != "succeeded":
        return None
    package_id = str((run.result or {}).get("application_package_id") or "")
    document_id = str((run.result or {}).get("document_id") or "")
    if not package_id or not document_id:
        return None
    package = db.get(ApplicationPackage, package_id)
    document = db.get(GeneratedDocument, document_id)
    if package is None or document is None:
        return None
    package_document = db.execute(
        select(ApplicationPackageDocument).where(
            ApplicationPackageDocument.application_package_id == package_id,
            ApplicationPackageDocument.generated_document_id == document_id,
        )
    ).scalar_one_or_none()
    artifact = db.get(StoredArtifact, document.artifact_id)
    if package_document is None or artifact is None:
        return None
    editable_artifact = (
        db.get(StoredArtifact, document.editable_artifact_id)
        if document.editable_artifact_id
        else None
    )
    return {
        "generator": cover_letter_generator_to_api(settings),
        "package": package.to_api(),
        "document": cover_letter_document_to_api(
            package_document,
            document,
            artifact,
            editable_artifact,
        ),
    }


def process_cover_letter_generation_run(
    generation_run_id: str,
    settings: Settings,
    storage: ArtifactStorageService,
    *,
    worker_id: str,
    supervisor_lease_token: str,
) -> dict | None:
    expected_attempt = 0
    expected_claim_token = ""
    tracked_storage = ArtifactStorageWriteTracker(storage)
    try:
        with Session(bind=get_engine(settings), autoflush=False) as compute_db:
            run = compute_db.get(CoverLetterGenerationRun, generation_run_id)
            if (
                run is None
                or run.status != "running"
                or run.worker_id != worker_id
                or run.supervisor_lease_token != supervisor_lease_token
                or not run.claim_token
            ):
                return run.to_api() if run else None
            expected_attempt = run.attempts
            expected_claim_token = run.claim_token
            job = compute_db.get(JobPosting, run.job_id)
            if job is None:
                raise CoverLetterGenerationProcessingError(
                    "job_not_found", "The queued job no longer exists", retryable=False
                )
            if job.status != "active" or job_is_merged_duplicate(job):
                raise CoverLetterGenerationProcessingError(
                    "job_not_active", "The queued job is no longer active", retryable=False
                )
            if job.posting_snapshot_hash != run.posting_snapshot_hash:
                raise CoverLetterGenerationProcessingError(
                    "posting_snapshot_changed",
                    "The posting changed after cover-letter generation was queued",
                    retryable=False,
                )
            if not (
                job_requires_cover_letter(job) or cover_letter_generation_requested_by_user(run)
            ):
                raise CoverLetterGenerationProcessingError(
                    "cover_letter_not_required",
                    "The posting no longer requires a cover letter",
                    retryable=False,
                )
            if compute_db.get(CandidateProfile, CANDIDATE_ID) is None:
                raise CoverLetterGenerationProcessingError(
                    "candidate_profile_missing",
                    "Create a candidate profile before generating a cover letter",
                    retryable=False,
                )
            from jobcockpit.api.cover_letter_generation_runs import validate_generation_resume

            validate_generation_resume(compute_db, run)
            prepared = prepare_cover_letter_document(
                db=compute_db,
                storage=storage,
                settings=settings,
                candidate_id=CANDIDATE_ID,
                job=job,
                operation_id=f"cover-letter request {generation_run_id}",
                resume_artifact_id=(run.request_payload or {}).get("resume_artifact_id"),
                use_latest_resume=not bool(run.request_payload),
            )

        with session_scope(settings) as db:
            if not begin_background_worker_lease_transaction(
                db,
                worker_id,
                supervisor_lease_token,
                lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
            ):
                tracked_storage.rollback()
                return None
            run = db.get(CoverLetterGenerationRun, generation_run_id)
            if (
                run is None
                or run.status != "running"
                or run.worker_id != worker_id
                or run.attempts != expected_attempt
                or run.claim_token != expected_claim_token
                or run.supervisor_lease_token != supervisor_lease_token
            ):
                tracked_storage.rollback()
                return run.to_api() if run else None
            job = db.get(JobPosting, run.job_id)
            if (
                job is None
                or job.status != "active"
                or job_is_merged_duplicate(job)
                or job.posting_snapshot_hash != run.posting_snapshot_hash
                or not (
                    job_requires_cover_letter(job) or cover_letter_generation_requested_by_user(run)
                )
            ):
                raise CoverLetterGenerationProcessingError(
                    "posting_changed_before_generation_commit",
                    "The posting changed while the cover letter was being generated",
                    retryable=False,
                )
            validate_generation_resume(db, run)
            application = db.execute(
                select(Application).where(Application.job_id == job.id)
            ).scalar_one_or_none()
            if application is None:
                application = Application(
                    application_id=new_id("app"),
                    candidate_id=CANDIDATE_ID,
                    job_id=job.id,
                    status="draft",
                    decision="undecided",
                )
                db.add(application)
                db.add(
                    AuditLogEntry.create(
                        "application.created", "application", application.application_id
                    )
                )
                db.flush()
            target_package_id = (run.request_payload or {}).get("target_package_id")
            if target_package_id:
                from jobcockpit.api.job_access import require_active_package_job

                package, target_application, _ = require_active_package_job(db, target_package_id)
                if (
                    target_application.application_id != application.application_id
                    or package.status != "draft"
                    or package.approved_for_extension
                ):
                    raise CoverLetterGenerationProcessingError(
                        "package_changed",
                        "The selected package is no longer an editable draft",
                        retryable=False,
                    )
            else:
                package = ApplicationPackage(
                    application_package_id=new_id("pkg"),
                    application_id=application.application_id,
                    package_version=next_application_package_version(
                        db, application.application_id
                    ),
                    status="draft",
                    approved_for_extension=False,
                )
                db.add(package)
                db.flush()
            document = create_cover_letter_package_document(
                db=db,
                storage=tracked_storage,
                settings=settings,
                package_id=package.application_package_id,
                prepared=prepared,
            )
            mark_cover_letter_generation_succeeded(
                run,
                db,
                result={
                    "application_id": application.application_id,
                    "application_package_id": package.application_package_id,
                    "document_id": document["document_id"],
                    "artifact_id": document["artifact_id"],
                },
                completion_source="background_worker",
            )
            db.flush()
            result = run.to_api()
        tracked_storage.storage_uris.clear()
        return result
    except Exception as error:
        tracked_storage.rollback()
        if expected_attempt == 0 or not expected_claim_token:
            raise
        return record_cover_letter_generation_failure(
            generation_run_id,
            settings,
            error,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_claim_token=expected_claim_token,
            expected_supervisor_lease_token=supervisor_lease_token,
            candidate_id=CANDIDATE_ID,
        )


def process_next_background_job(
    settings: Settings,
    storage: ArtifactStorageService | None = None,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
    prefer_reprocess: bool = False,
) -> dict | None:
    worker_id = worker_id or new_id("worker")
    storage = storage or ArtifactStorageService(settings.artifact_root)
    if supervisor_lease_token is None:
        with BackgroundWorkerLeaseGuard(settings, worker_id) as lease:
            if not lease.acquired or lease.lease_token is None:
                return None
            bootstrap_background_worker(
                settings,
                worker_id=worker_id,
                lease_token=lease.lease_token,
            )
            return process_next_background_job(
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=lease.lease_token,
                prefer_reprocess=prefer_reprocess,
            )
    if prefer_reprocess:
        reprocess = process_next_job_reprocess(
            settings,
            storage,
            worker_id=worker_id,
            supervisor_lease_token=supervisor_lease_token,
        )
        if reprocess:
            return {"kind": "job_reprocess", "run": reprocess}
    capture_id = claim_next_page_capture(
        settings,
        worker_id=worker_id,
        supervisor_lease_token=supervisor_lease_token,
    )
    if capture_id:
        return {
            "kind": "page_capture",
            "run": process_page_capture_run(
                capture_id,
                settings,
                storage,
                worker_id=worker_id,
                supervisor_lease_token=supervisor_lease_token,
            ),
        }
    if not prefer_reprocess:
        reprocess = process_next_job_reprocess(
            settings,
            storage,
            worker_id=worker_id,
            supervisor_lease_token=supervisor_lease_token,
        )
        if reprocess:
            return {"kind": "job_reprocess", "run": reprocess}
    return None


class ApiBackgroundWorker(LeasedWorker):
    def __init__(self, settings: Settings, storage: ArtifactStorageService) -> None:
        super().__init__(settings, lease_name="api_background_queue", prefix="worker")
        self.storage = storage
        self.capture_streak = 0

    def _bootstrap(self, token):
        bootstrap_background_worker(self.settings, worker_id=self.worker_id, lease_token=token)

    def _process_once(self, token):
        result = process_next_background_job(
            self.settings,
            self.storage,
            worker_id=self.worker_id,
            supervisor_lease_token=token,
            prefer_reprocess=self.capture_streak >= 5,
        )
        if result:
            self.capture_streak = self.capture_streak + 1 if result["kind"] == "page_capture" else 0
        return result

    def _recover_stale_work(
        self,
        lease_token: str,
        *,
        include_owned: bool = False,
        recover_orphaned: bool = False,
    ) -> tuple[int, int]:
        try:
            recovered_captures = recover_stale_page_capture_runs(
                self.settings,
                active_worker_id=self.worker_id if recover_orphaned else None,
                active_lease_token=lease_token if recover_orphaned else None,
                recover_orphaned=recover_orphaned,
                recover_owned_by=self.worker_id if include_owned else None,
                required_worker_id=self.worker_id,
                required_lease_token=lease_token,
            )
            recovered_reprocesses = recover_stale_job_reprocess_runs(
                self.settings,
                active_worker_id=self.worker_id if recover_orphaned else None,
                active_lease_token=lease_token if recover_orphaned else None,
                recover_orphaned=recover_orphaned,
                recover_owned_by=self.worker_id if include_owned else None,
                required_worker_id=self.worker_id,
                required_lease_token=lease_token,
            )
            if recovered_captures or recovered_reprocesses:
                logger.warning(
                    "API background worker %s recovered %s capture(s) and "
                    "%s reprocess run(s) after an interrupted iteration",
                    self.worker_id,
                    recovered_captures,
                    recovered_reprocesses,
                )
            return recovered_captures, recovered_reprocesses
        except Exception as error:
            logger.error(
                "API background worker %s could not recover interrupted work; error_type=%s",
                self.worker_id,
                error.__class__.__name__,
            )
            return 0, 0


class CoverLetterBackgroundWorker(LeasedWorker):
    def __init__(self, settings: Settings, storage: ArtifactStorageService) -> None:
        super().__init__(
            settings, lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE, prefix="cover_worker"
        )
        self.storage = storage

    def _bootstrap(self, token):
        bootstrap_cover_letter_generation_worker(
            self.settings, worker_id=self.worker_id, lease_token=token
        )

    def _process_once(self, token):
        return process_next_cover_letter_generation(
            self.settings,
            self.storage,
            worker_id=self.worker_id,
            supervisor_lease_token=token,
        )

    def _recover_stale_work(
        self,
        lease_token: str,
        *,
        include_owned: bool = False,
    ) -> int:
        try:
            recovered = recover_stale_cover_letter_generation_runs(
                self.settings,
                recover_owned_by=self.worker_id if include_owned else None,
                required_worker_id=self.worker_id,
                required_lease_token=lease_token,
            )
            if recovered:
                logger.warning(
                    "Cover-letter worker %s recovered %s interrupted request(s)",
                    self.worker_id,
                    recovered,
                )
            return recovered
        except Exception as error:
            logger.error(
                "Cover-letter worker %s could not recover interrupted work; error_type=%s",
                self.worker_id,
                error.__class__.__name__,
            )
            return 0


@coordinated_ingestion(ats_scope, lambda status, detail: HTTPException(status, detail))
def ingest_ats_request(
    request: AtsIngestRequest, db: Session, storage: ArtifactStorageService, settings: Settings
) -> dict:
    run = create_job_ingestion_run(request, db)
    run_id = run.ingestion_run_id
    db.commit()
    adapter = None
    ats_jobs = []
    jobs_seen_count = 0
    jobs_created_count = 0
    jobs_duplicate_count = 0
    jobs_skipped_count = 0
    jobs_language_skipped_count = 0
    item_error_count = 0
    skipped_jobs = []
    language_skipped_jobs = []
    fetch_request: AtsFetchRequest | None = None

    def record_interrupted_run(error: BaseException) -> None:
        with session_scope(settings) as failure_db:
            begin_ingestion_write(failure_db)
            interrupted = failure_db.get(JobIngestionRun, run_id)
            # This callback owns only this invocation's run. A handler may have
            # already finalized it; a replacement source run has a different ID.
            if interrupted is None or interrupted.status != "running":
                return
            message = str(error.detail) if isinstance(error, HTTPException) else str(error)
            pending_items = list(failure_db.scalars(
                select(JobIngestionItem).where(
                    JobIngestionItem.ingestion_run_id == run_id,
                    JobIngestionItem.status == "pending",
                )
            ))
            for item in pending_items:
                fail_job_ingestion_item(item, RuntimeError(message))
            finish_job_ingestion_run(
                interrupted, failure_db, status="failed",
                jobs_seen_count=jobs_seen_count,
                jobs_created_count=jobs_created_count,
                jobs_duplicate_count=jobs_duplicate_count,
                jobs_skipped_count=jobs_skipped_count,
                jobs_language_skipped_count=jobs_language_skipped_count,
                item_error_count=item_error_count + len(pending_items),
                error_message=message or type(error).__name__,
            )

    on_ingestion_failure(record_interrupted_run)

    def preserve_fetch_stats(run: JobIngestionRun) -> None:
        nonlocal jobs_seen_count
        if fetch_request is None:
            return
        jobs_seen_count = fetch_request.stats.jobs_seen_count or jobs_seen_count
        recorded_item_id = db.execute(
            select(JobIngestionItem.ingestion_item_id)
            .where(JobIngestionItem.ingestion_run_id == run_id)
            .limit(1)
        ).scalar_one_or_none()
        if not recorded_item_id:
            create_discovered_job_ingestion_items(run_id, [], fetch_request, db)
        if fetch_request.stats.link_failures:
            run.request_payload = {
                **run.request_payload,
                "link_failures": [
                    failure.to_dict() for failure in fetch_request.stats.link_failures
                ],
            }
        if fetch_request.stats.source_exclusions:
            run.request_payload = {
                **run.request_payload,
                "source_exclusions": [
                    exclusion.to_dict() for exclusion in fetch_request.stats.source_exclusions
                ],
            }

    try:
        adapter = get_ats_adapter(request.provider)
        configure_settings = getattr(adapter, "configure_settings", None)
        if callable(configure_settings):
            configure_settings(settings)
        source_type = (
            adapter.provider if adapter.provider == "workday" else f"ats_{adapter.provider}"
        )
        adapter.validate_source_url(request.source_url)
        policy_result = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
            request.source_url
        )
        if not policy_result.allowed:
            raise AtsProviderError(400, f"URL is blocked by policy: {policy_result.reason}")
        profile = db.get(CandidateProfile, CANDIDATE_ID)
        profile_data = profile.profile_data if profile else None
        listing_interest_matcher = None
        if not request.skip_interest_filter:
            listing_interest_matcher = partial(match_title_interest, profile_data=profile_data)

        excluded_source_urls = previously_imported_source_urls(db) | failed_link_exclusion_urls(
            db,
            provider=request.provider,
            source_url=request.source_url,
        )
        from jobcockpit.services.publication import excluded_publication_ids

        excluded_provider_job_ids = excluded_publication_ids(db, settings, request.source_url)
        db.commit()
        fetch_request = AtsFetchRequest(
            source_url=request.source_url,
            limit=request.limit,
            search_text=request.search_text,
            applied_facets=request.applied_facets,
            excluded_source_urls=excluded_source_urls,
            excluded_provider_job_ids=excluded_provider_job_ids,
            listing_interest_matcher=listing_interest_matcher,
            source_url_normalizer=posting_url_identity,
            link_concurrency=ingestion_link_concurrency(settings),
        )
        ats_jobs = bounded_call("source", adapter.fetch_jobs, fetch_request)
        # A publisher may commit and deliver while a paginated feed is in flight.
        # Refresh the identity set before constructing any ingestion work items.
        excluded_provider_job_ids |= excluded_publication_ids(db, settings, request.source_url)
        jobs_seen_count = fetch_request.stats.jobs_seen_count or len(ats_jobs)
        item_error_count = len(fetch_request.stats.link_failures)
        skipped_jobs.extend(skip.to_dict() for skip in fetch_request.stats.interest_skips)
        batch_items = [
            PostingBatchItem(
                source_type=source_type,
                source_url=ats_job.source_url,
                raw_text=ats_job.raw_text,
                title_hint=ats_job.title,
                portal_source_url=ats_job.portal_source_url,
                link_text=ats_job.source_url,
                interest_text=getattr(ats_job, "title", None) or ats_job.raw_text,
                trusted_posting=True,
                source_archive=ats_job.source_archive,
                fetch_source_archive=ats_job_needs_source_fetch(ats_job),
                content_source_url=getattr(ats_job, "content_source_url", None),
            )
            for ats_job in ats_jobs
            if getattr(ats_job, "provider_job_id", None) not in excluded_provider_job_ids
        ]
        item_rows = create_discovered_job_ingestion_items(
            run_id,
            batch_items,
            fetch_request,
            db,
        )
        item_ids = [item_row.ingestion_item_id for item_row in item_rows]
        db.commit()

        jobs_skipped_count = len(fetch_request.stats.interest_skips)
        jobs = []
        with session_scope(settings) as index_db:
            jobs_by_source_url_identity = previously_imported_jobs_by_source_url_identity(index_db)
        known_source_urls = frozenset(jobs_by_source_url_identity)

        def prepare_item(item, *, defer_source_fetch=False):
            return prepare_posting_batch_item(
                item, settings=settings, profile_data=profile_data,
                skip_interest_filter=request.skip_interest_filter,
                skip_language_filter=request.skip_language_filter,
                precheck_language_filter=True,
                defer_source_fetch=defer_source_fetch,
            )

        def persist_item(work, prepared, item_db, tracked_storage):
            batch_item, item_id = work

            def duplicate_lookup(item: PostingBatchItem):
                duplicate, duplicate_reason = find_previously_imported_ats_job(
                    item_db,
                    source_type=item.source_type,
                    source_url=item.source_url,
                    raw_text=item.raw_text,
                    jobs_by_source_url_identity=jobs_by_source_url_identity,
                )
                if not duplicate:
                    return None
                begin_ingestion_write(item_db)
                item_db.refresh(duplicate)
                if item.portal_source_url:
                    source = job_source_metadata(duplicate)
                    updated_source = source_metadata_with_portal_url(source, item.portal_source_url)
                    if updated_source != source:
                        duplicate.source = updated_source
                return job_ingest_duplicate_result(duplicate, item_db, duplicate_reason)

            result = persist_posting_batch_item(
                prepared, db=item_db, storage=tracked_storage, settings=settings,
                job_importer=persist_job_ingest_result, duplicate_lookup=duplicate_lookup,
                prepare_missing=prepare_item,
            )
            persisted_item = item_db.get(JobIngestionItem, item_id)
            if not persisted_item:
                raise RuntimeError("Ingestion item record was not found")
            update_job_ingestion_item(persisted_item, result.processing_result)
            return result

        def record_item_failure(work, error, failure_db):
            batch_item, item_id = work
            persisted_item = failure_db.get(JobIngestionItem, item_id)
            if not persisted_item:
                raise error
            fail_job_ingestion_item(persisted_item, error)
            logger.warning(
                "ATS ingestion item failed for %s in run %s", batch_item.source_url, run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

        def completed_item(work, item_result):
            nonlocal jobs_created_count, jobs_duplicate_count, jobs_skipped_count
            nonlocal jobs_language_skipped_count, item_error_count
            if isinstance(item_result, Exception):
                item_error_count += 1
                return
            ingest_result = item_result.processing_result
            if ingest_result.payload and ingest_result.linked_job_id:
                url = (ingest_result.payload.get("source") or {}).get("source_url")
                if url:
                    jobs_by_source_url_identity[posting_url_identity(url)] = ingest_result.linked_job_id
            counts = item_result.counts
            jobs_created_count += counts.created
            jobs_duplicate_count += counts.reused
            jobs_skipped_count += counts.interest_skipped + counts.terminal_skipped
            jobs_language_skipped_count += counts.language_skipped
            if ingest_result.ingestion_status == "skipped_by_interest_filter":
                match = item_result.interest_match
                skipped_jobs.append({
                    "source_url": item_result.item.source_url,
                    "title": match.title if match else "",
                    "reason": ingest_result.error_message,
                    "matched_exclude": match.matched_exclude if match else None,
                    "matched_include": match.matched_include if match else None,
                })
            elif ingest_result.ingestion_status == "skipped_by_language_filter":
                language_skipped_jobs.append({
                    "source_url": ingest_result.resolved_url or ingest_result.original_url,
                    "reason": ingest_result.error_message,
                })
            elif ingest_result.ingestion_status == "terminal_status_detected":
                return
            elif not is_successful_posting_link_import(ingest_result.ingestion_status) or not ingest_result.payload:
                item_error_count += 1
            else:
                jobs.append(ingest_result.payload)

        execute_posting_items(
            zip(batch_items, item_ids, strict=True), settings=settings, storage=storage,
            prepare=lambda work: prepare_item(
                work[0], defer_source_fetch=posting_url_identity(work[0].source_url) in known_source_urls,
            ),
            persist=persist_item, record_failure=record_item_failure, completed=completed_item,
        )
        run = db.get(JobIngestionRun, run_id)
        if not run:
            raise HTTPException(status_code=500, detail="Ingestion run record was not found")
        preserve_fetch_stats(run)
        finish_job_ingestion_run(
            run,
            db,
            status=("completed_with_warnings" if item_error_count else "completed"),
            jobs_seen_count=jobs_seen_count,
            jobs_created_count=jobs_created_count,
            jobs_duplicate_count=jobs_duplicate_count,
            jobs_skipped_count=jobs_skipped_count,
            jobs_language_skipped_count=jobs_language_skipped_count,
            item_error_count=item_error_count,
        )
        run_items = list(
            db.execute(
                select(JobIngestionItem)
                .where(JobIngestionItem.ingestion_run_id == run_id)
                .order_by(JobIngestionItem.created_at, JobIngestionItem.ingestion_item_id)
            ).scalars()
        )
        return {
            "provider": adapter.provider,
            "source_url": request.source_url,
            "jobs_seen_count": jobs_seen_count,
            "jobs_created_count": jobs_created_count,
            "jobs_duplicate_count": jobs_duplicate_count,
            "jobs_skipped_count": jobs_skipped_count,
            "jobs_language_skipped_count": jobs_language_skipped_count,
            "skipped_jobs": skipped_jobs,
            "language_skipped_jobs": language_skipped_jobs,
            "link_failures": [failure.to_dict() for failure in fetch_request.stats.link_failures],
            "source_exclusions": [
                exclusion.to_dict() for exclusion in fetch_request.stats.source_exclusions
            ],
            "ingestion_run": run.to_api(),
            "alerts": ingestion_run_alerts(run),
            "items": [item.to_api() for item in run_items],
            "jobs": jobs,
        }
    except AtsProviderError as error:
        db.rollback()
        run = db.get(JobIngestionRun, run_id)
        if run:
            preserve_fetch_stats(run)
            finish_job_ingestion_run(
                run,
                db,
                status="failed",
                jobs_seen_count=jobs_seen_count,
                jobs_created_count=jobs_created_count,
                jobs_duplicate_count=jobs_duplicate_count,
                error_message=error.detail,
            )
            db.commit()
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    except HTTPException as error:
        db.rollback()
        run = db.get(JobIngestionRun, run_id)
        if run:
            preserve_fetch_stats(run)
            error_message = error.detail if isinstance(error.detail, str) else str(error.detail)
            finish_job_ingestion_run(
                run,
                db,
                status="failed",
                jobs_seen_count=jobs_seen_count,
                jobs_created_count=jobs_created_count,
                jobs_duplicate_count=jobs_duplicate_count,
                error_message=error_message,
            )
            db.commit()
        raise
    except Exception as error:
        db.rollback()
        run = db.get(JobIngestionRun, run_id)
        if run:
            preserve_fetch_stats(run)
            finish_job_ingestion_run(
                run,
                db,
                status="failed",
                jobs_seen_count=jobs_seen_count,
                jobs_created_count=jobs_created_count,
                jobs_duplicate_count=jobs_duplicate_count,
                error_message=str(error) or error.__class__.__name__,
            )
            db.commit()
        logger.exception("Unexpected ATS ingestion failure for run %s", run_id)
        raise HTTPException(status_code=500, detail="ATS ingestion failed unexpectedly") from error
    finally:
        close_adapter = getattr(adapter, "close", None)
        cleanup_error: Exception | None = None
        if callable(close_adapter):
            for _cleanup_attempt in range(2):
                try:
                    close_adapter()
                    cleanup_error = None
                    break
                except Exception as error:
                    cleanup_error = error
        retained_cleanup_count = retain_pending_browser_cleanup(adapter)
        if cleanup_error is not None or retained_cleanup_count:
            logger.warning(
                "ATS adapter cleanup required durable supervision for run %s; "
                "retained %s independent browser cleanup lease(s): %s",
                run_id,
                retained_cleanup_count,
                cleanup_error or "close remained pending",
            )
