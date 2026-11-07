"""Bounded source discovery; enrich metadata-only records from verified content."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from time import monotonic
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import load_only
from sqlalchemy.exc import IntegrityError

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    begin_background_worker_lease_transaction,
)
from jobcockpit.db.models import Application, AuditLogEntry, JobPosting, JobSourceIdentity, JobSourceReconciliation, StoredArtifact
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.posting_discovery import (
    PostingDiscoveryResolver, _official_candidate_url_matches_hint, _official_posting_confidence,
    external_source_kind,
    posting_has_description,
)
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, PostingResolution
from jobcockpit.ingestion.posting_content import PostingContentState, posting_content_state
from jobcockpit.ingestion.source_authority import job_portal_kind
from jobcockpit.ingestion.source_resolution import official_discovery_source_resolution
from jobcockpit.ingestion.status_identity import posting_identity, posting_url_supports_open_evidence
from jobcockpit.storage.artifacts import ArtifactStorageService, ArtifactStorageWriteTracker
from jobcockpit.ingestion.url_policy import UrlFetchPolicy
from jobcockpit.services.worker_supervisor import LeasedWorker
from jobcockpit.utils.ids import new_id

RETRY_DELAYS = (timedelta(hours=1), timedelta(hours=6), timedelta(days=1), timedelta(days=7))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
SOURCE_WORKER_LEASE = "source_reconciliation_queue"
logger = logging.getLogger(__name__)


def source_url_needs_discovery(url: str) -> bool:
    """Recognize missing/listing sources without rejecting unfamiliar detail routes."""
    if not url or job_portal_kind(url) or not posting_url_supports_open_evidence(url):
        return True
    identity = posting_identity(url)
    if identity.source_kind == "eightfold" and identity.is_detail:
        return False
    parsed = urlparse(url)
    params = {key.casefold(): value for key, value in parse_qs(parsed.query).items()}
    if any(params.get(key) for key in ("jobid", "job_id", "reqid", "requisitionid", "postingid")):
        return False
    parts = [unquote(part).casefold() for part in parsed.path.split("/") if part]
    return not parts or parts[0] in {"go", "departments"} or parts[-1] in {
        "apply", "careers", "jobs", "search", "search-results", "openings", "positions",
    }


def source_reconciliation_eligible(db, job: JobPosting) -> bool:
    source = job.source or {}
    if (job.status != "active" or source.get("merged_into_job_id")
            or (source.get("source_resolution") or {}).get("kind") == "manual_override"):
        return False
    if not (_structured_fallback(job) or job.status_source_repair_required or source_url_needs_discovery(job.source_url or "")):
        return False
    if db.scalar(select(Application.application_id).where(
        Application.job_id == job.id,
        or_(Application.status == "applied", Application.applied_at.is_not(None)),
    ).limit(1)):
        return False
    return True


def _structured_fallback(job) -> bool:
    return ((job.source or {}).get("source_resolution") or {}).get("path") == "aggregator_structured_fallback"


def _description_needs_recovery(db, job, settings) -> bool:
    # The structured path also preserves complete source bodies. Inspect the
    # actual snapshot, including legacy rows, before authorizing replacement.
    if not _structured_fallback(job):
        return False
    artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
    if artifact is None or artifact.deleted_at is not None:
        return False
    try:
        raw_text = ArtifactStorageService(settings.artifact_root).read_bytes(artifact.storage_uri).decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        return False
    if not raw_text.strip():
        return False
    return posting_content_state(raw_text, _hint(job)) in {
        PostingContentState.METADATA_ONLY, PostingContentState.BLOCKED,
    }


@contextmanager
def _enrichment_storage(settings):
    storage = ArtifactStorageWriteTracker(ArtifactStorageService(settings.artifact_root))
    try:
        yield storage
    except Exception:
        storage.rollback()
        raise


def enqueue_source_reconciliation(db, job: JobPosting, *, retry=False):
    if not source_reconciliation_eligible(db, job):
        return None
    run = db.get(JobSourceReconciliation, job.id)
    if run is not None:
        if retry and run.status not in {"pending", "running", "retry_scheduled"}:
            run.status = "pending"
            run.attempts = 0
            run.next_attempt_at = datetime.now(UTC)
            run.updated_at = datetime.now(UTC)
        elif (run.status == "cancelled" and run.reason_code == "source_changed_during_discovery"
              and run.attempts < MAX_ATTEMPTS):
            # Recover legacy cancellations without resetting their retry budget.
            run.status = "retry_scheduled"
            run.next_attempt_at = datetime.now(UTC)
            run.updated_at = datetime.now(UTC)
        return run
    run = JobSourceReconciliation(job_id=job.id, status="pending", attempts=0,
                                  next_attempt_at=datetime.now(UTC), history=[])
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        return db.get(JobSourceReconciliation, job.id)
    db.add(AuditLogEntry.create("job.source_reconciliation_queued", "job_posting", job.id))
    return run


def _unscheduled_source_jobs(*, limit, after=None):
    statement = select(JobPosting).options(load_only(
        JobPosting.id, JobPosting.created_at, JobPosting.source, JobPosting.source_url,
        JobPosting.status, JobPosting.status_source_repair_required,
    )).where(
        JobPosting.status == "active",
        or_(
            ~select(JobSourceReconciliation.job_id).where(
                JobSourceReconciliation.job_id == JobPosting.id,
            ).exists(),
            select(JobSourceReconciliation.job_id).where(
                JobSourceReconciliation.job_id == JobPosting.id,
                JobSourceReconciliation.status == "cancelled",
                JobSourceReconciliation.reason_code == "source_changed_during_discovery",
                JobSourceReconciliation.attempts < MAX_ATTEMPTS,
            ).exists(),
        ),
        ~select(Application.application_id).where(
            Application.job_id == JobPosting.id,
            or_(Application.status == "applied", Application.applied_at.is_not(None)),
        ).exists(),
    )
    if after:
        statement = statement.where(or_(
            JobPosting.created_at > after[0],
            and_(JobPosting.created_at == after[0], JobPosting.id > after[1]),
        ))
    return statement.order_by(JobPosting.created_at, JobPosting.id).limit(limit)


def schedule_source_reconciliations(db, *, limit=25) -> int:
    """Bound both rows examined and enqueues; intended for explicit maintenance."""
    return sum(enqueue_source_reconciliation(db, job) is not None
               for job in list(db.scalars(_unscheduled_source_jobs(limit=limit))))


@dataclass(frozen=True)
class SourceSweepResult:
    examined: int
    enqueued: int
    cursor: tuple[datetime, str] | None


def sweep_source_reconciliations(settings, *, batch_size=100, after=None):
    """Read a bounded page without a writer lock, then recheck each candidate."""
    with session_scope(settings) as db:
        jobs = list(db.scalars(_unscheduled_source_jobs(limit=batch_size, after=after)))
        candidate_ids = [job.id for job in jobs if source_reconciliation_eligible(db, job)]
        cursor = (jobs[-1].created_at, jobs[-1].id) if len(jobs) == batch_size else None
        examined = len(jobs)
    enqueued = 0
    for job_id in candidate_ids:
        with session_scope(settings) as db:
            if db.get_bind().dialect.name == "sqlite":
                from sqlalchemy import text
                db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(JobPosting, job_id)
            if job is not None:
                existing = db.get(JobSourceReconciliation, job_id)
                previous_status = existing.status if existing else None
                run = enqueue_source_reconciliation(db, job)
                enqueued += int(run is not None and (existing is None or run.status != previous_status))
    return SourceSweepResult(examined, enqueued, cursor)


def cancel_source_reconciliation(db, job_id, *, reason):
    run = db.get(JobSourceReconciliation, job_id)
    if run and run.status in {"pending", "running", "retry_scheduled"}:
        _finish(run, outcome="cancelled", reason=reason)


def _input_guard(job):
    values = {key: getattr(job, key) for key in (
        "source_url", "source", "company_name", "role", "location", "employment_timing",
        "status", "raw_content_artifact_id", "posting_snapshot_hash", "application",
        "updated_at", "eligibility", "compensation", "data_quality", "fit_analysis",
    )}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _hint(job):
    timing = job.employment_timing or {}
    term = str(timing.get("raw_label") or "")
    if timing.get("start_date"):
        term = f"Start Date: {timing['start_date']}"
    if timing.get("duration_months"):
        term += f"\nDuration: {timing['duration_months']} months"
    source = job.source or {}
    resolution = source.get("source_resolution") or {}
    candidate_urls = tuple(dict.fromkeys(
        url.strip() for url in (resolution.get("original_url"), source.get("portal_source_url"))
        if isinstance(url, str) and url.strip()
    ))
    return ExternalPostingHint(
        source_url=job.source_url or "", source_kind=job_portal_kind(job.source_url or "") or "source",
        company=job.company_name or "", title=(job.role or {}).get("job_title") or job.normalized_title or "",
        location=", ".join(str((job.location or {}).get(key) or "") for key in ("city", "province_state")
                           if (job.location or {}).get(key)), term=term.strip(), candidate_urls=candidate_urls,
    )


def _finish(run, *, outcome, reason, attempts=(), now=None):
    now = now or datetime.now(UTC)
    run.outcome = outcome
    run.reason_code = reason
    retry = outcome in {"unavailable", "attempted"} and run.attempts < MAX_ATTEMPTS
    run.status = "retry_scheduled" if retry else outcome
    run.next_attempt_at = now + RETRY_DELAYS[max(0, run.attempts - 1)] if retry else None
    run.history = [*run.history, {
        "at": now.isoformat(), "attempt": run.attempts, "outcome": outcome,
        "reason_code": reason, "attempts": list(attempts),
    }]
    run.worker_id = run.lease_token = run.claim_token = None
    run.updated_at = now


def process_next_source_reconciliation(settings, *, resolver=None, worker_id=None, supervisor_lease_token=None, sweep=True):
    worker_id = worker_id or new_id("source_worker")
    if sweep:
        sweep_source_reconciliations(settings)
    if supervisor_lease_token is None:
        with BackgroundWorkerLeaseGuard(settings, worker_id, lease_name=SOURCE_WORKER_LEASE) as lease:
            if not lease.acquired or lease.lease_token is None:
                return None
            return process_next_source_reconciliation(
                settings, resolver=resolver, worker_id=worker_id, supervisor_lease_token=lease.lease_token, sweep=False,
            )
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db, worker_id, supervisor_lease_token, lease_name=SOURCE_WORKER_LEASE,
        ):
            return None
        # The supervisor lease fences claims and commits. An interrupted claim is
        # retried with backoff and leaves a durable interruption in its history.
        for stale in db.scalars(select(JobSourceReconciliation).where(
            JobSourceReconciliation.status == "running",
            or_(JobSourceReconciliation.lease_token != supervisor_lease_token,
                JobSourceReconciliation.started_at < datetime.now(UTC) - timedelta(minutes=30)),
        )):
            _finish(stale, outcome="unavailable", reason="source_worker_interrupted")
        run = db.scalar(select(JobSourceReconciliation).where(
            JobSourceReconciliation.status.in_(["pending", "retry_scheduled"]),
            JobSourceReconciliation.next_attempt_at <= datetime.now(UTC),
        ).order_by(JobSourceReconciliation.next_attempt_at, JobSourceReconciliation.job_id).limit(1))
        if run is None:
            return None
        job = db.get(JobPosting, run.job_id)
        if job is None or not source_reconciliation_eligible(db, job):
            _finish(run, outcome="cancelled", reason="source_no_longer_eligible")
            return run.to_api()
        run.status = "running"
        run.attempts += 1
        run.started_at = datetime.now(UTC)
        run.worker_id, run.lease_token = worker_id, supervisor_lease_token
        run.claim_token = claim_token = new_id("source_claim")
        run.input_guard = guard = _input_guard(job)
        job_id, hint = job.id, _hint(job)
        enrich_description, source_type = _description_needs_recovery(db, job, settings), job.source_type
    # No database transaction or session survives the network calls.
    try:
        if resolver is None:
            from jobcockpit.ingestion.posting_links import default_posting_resolver
            resolver = default_posting_resolver(settings)
            if isinstance(resolver, PostingDiscoveryResolver):
                resolver.allow_aggregator_fallback = False
                resolver.allow_linkedin_fallback = enrich_description
            else:
                resolver = None
        resolution = resolver.resolve(hint) if resolver else PostingResolution(
            status="needs_review", error="posting_search_not_configured",
        )
    except Exception:
        resolution = PostingResolution(status="needs_review", error="posting_resolver_failed")
    posting = resolution.posting
    candidate_verified = False
    parsed_job = None
    if resolution.status == "resolved" and posting is not None:
        # DNS policy checks and content verification also stay outside the writer.
        policy = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(posting.source_url)
        candidate_verified = (
            policy.allowed and (_official_candidate_url_matches_hint(posting.source_url, hint)
                or (enrich_description and external_source_kind(posting.source_url) == "linkedin"
                    and posting_identity(posting.source_url).is_detail
                    and resolution.source_path in {"aggregator_rendered_fallback", "aggregator_direct_fallback"}))
            and _official_posting_confidence(hint, posting.raw_text) >= 0.8
        )
        if candidate_verified and enrich_description:
            candidate_verified = posting_has_description(posting, hint)
            if candidate_verified:
                try:
                    from jobcockpit.ingestion.parser import parse_job_text_with_settings
                    parsed_job = parse_job_text_with_settings(
                        posting.raw_text, posting.source_url, settings,
                        source_type=source_type, title_hint=hint.title,
                    )
                except Exception:
                    resolution = PostingResolution(status="needs_review", error="source_enrichment_parse_failed",
                                                   attempts=resolution.attempts)
    with _enrichment_storage(settings) as storage, session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(
            db, worker_id, supervisor_lease_token, lease_name=SOURCE_WORKER_LEASE,
        ):
            return None
        run = db.get(JobSourceReconciliation, job_id)
        if run is None or run.status != "running" or run.claim_token != claim_token or run.lease_token != supervisor_lease_token:
            return None
        job = db.get(JobPosting, job_id)
        if job is None or not source_reconciliation_eligible(db, job):
            _finish(run, outcome="cancelled", reason="source_changed_during_discovery")
            return run.to_api()
        if _input_guard(job) != guard:
            _finish(run, outcome="unavailable", reason="source_changed_during_discovery")
            return run.to_api()
        trace = [attempt.to_dict() for attempt in resolution.attempts]
        if resolution.status == "resolved" and posting is not None:
            url = posting.source_url
            if not candidate_verified:
                _finish(run, outcome="attempted" if enrich_description else "blocked",
                        reason="candidate_not_independently_verified", attempts=trace)
            elif _candidate_already_saved(db, job, url):
                _finish(run, outcome="blocked", reason="candidate_matches_another_saved_job", attempts=trace)
            else:
                from jobcockpit.services.workflows import apply_job_source_url
                from jobcockpit.ingestion.posting_links import source_resolution_for_resolution
                metadata = (source_resolution_for_resolution(job.source_url or "", resolution)
                            if external_source_kind(url) == "linkedin"
                            else official_discovery_source_resolution(url, attempts=trace))
                previous = (job.source or {}).get("source_resolution") or {}
                metadata.update(original_url=previous.get("original_url") or job.source_url,
                                fallback_url=previous.get("fallback_url") or job.source_url)
                metadata["verification"] = {
                    "observed_content_sha256": hashlib.sha256(posting.raw_text.encode()).hexdigest(),
                    "verified_at": datetime.now(UTC).isoformat(),
                    "confidence": posting.confidence,
                    "input_guard": guard,
                }
                if enrich_description:
                    from jobcockpit.ingestion.posting_links import PostingIngestPayload
                    from jobcockpit.services.workflows import update_existing_job_from_ingest_payload
                    try:
                        with db.begin_nested():
                            update_existing_job_from_ingest_payload(
                                job, PostingIngestPayload(
                                    source_type=source_type, source_url=url, raw_text=posting.raw_text,
                                    source_archive=posting.source_archive, source_resolution=metadata,
                                    title_hint=hint.title,
                                    portal_source_url=(job.source or {}).get("portal_source_url"),
                                ), db, storage, settings, run_fit_analysis=False, parsed_job=parsed_job,
                            )
                            # Previous fit analysis used the incomplete email description.
                            job.fit_analysis = {}
                    except Exception:
                        storage.rollback()
                        _finish(run, outcome="attempted", reason="source_enrichment_failed", attempts=trace)
                        return run.to_api()
                else:
                    apply_job_source_url(job, url, metadata, db)
                # A verified same-URL repair must also unpark ordinary status checks.
                job.status_source_repair_required = False
                job.status_check_failure_count = job.status_check_not_found_count = 0
                job.status_next_check_at = datetime.now(UTC)
                _finish(run, outcome="resolved", reason=("source_description_enriched" if enrich_description
                                                        else "official_posting_resolved"), attempts=trace)
        else:
            reason = resolution.error or resolution.reason_code or "official_posting_not_found"
            blocked = any(word in reason for word in ("policy", "unsafe", "not_configured", "missing_company"))
            _finish(run, outcome="blocked" if blocked else (
                "unavailable" if resolution.status == "not_found" or not resolution.candidates else "attempted"
            ), reason=reason, attempts=trace)
            if job_portal_kind(job.source_url or ""):
                from jobcockpit.ingestion.posting_links import source_resolution_trace_for_resolution
                previous = (job.source or {}).get("source_resolution") or {}
                job.source = {**(job.source or {}), "source_resolution": {
                    **previous, "kind": "aggregator_fallback",
                    "path": previous.get("path") or "aggregator_structured_fallback",
                    "fallback_url": previous.get("fallback_url") or job.source_url,
                    **source_resolution_trace_for_resolution(resolution),
                }}
                job.updated_at = datetime.now(UTC)
        db.add(AuditLogEntry.create("job.source_reconciliation_finished", "job_posting", job_id,
                                   payload={"outcome": run.outcome, "reason_code": run.reason_code}))
        return run.to_api()


def _candidate_already_saved(db, job, url):
    from jobcockpit.ingestion.ingestion_identity import authoritative_ingestion_identity
    from jobcockpit.ingestion.status_reconciliation import status_urls_match
    from jobcockpit.api.page_capture_runs import job_source_identity_candidates
    from jobcockpit.ingestion.browser_capture_adapters import capture_source_identity
    identity = capture_source_identity(url)
    ingestion_identity = authoritative_ingestion_identity(url)
    statement = select(JobPosting.id, JobPosting.source_url).where(
        JobPosting.id != job.id,
        JobPosting.source["merged_into_job_id"].as_string().is_(None),
    )
    if identity is None:
        return any(status_urls_match(source_url or "", url) for _, source_url in db.execute(statement))

    # Preserve canonical identity rules using only URL-bearing fields.
    source_fields = ("source_url", "source_url_dedup_safe", "portal_source_url", "portal_source_urls")
    resolution_fields = ("original_url", "resolved_url", "canonical_url", "fallback_url")
    archive_fields = ("source_url", "final_url")
    columns = [JobPosting.application["external_apply_url"].label("external_apply_url")]
    for field in source_fields:
        value = JobPosting.source[field]
        if field == "source_url_dedup_safe":
            value = value.as_boolean()
        columns.append(value.label(f"source_{field}"))
    columns.extend(JobPosting.source["source_resolution"][field].label(f"resolution_{field}")
                   for field in resolution_fields)
    columns.extend(JobPosting.source["source_archive"][field].label(f"archive_{field}")
                   for field in archive_fields)
    statement = statement.add_columns(*columns)

    def matches(row):
        if ingestion_identity and authoritative_ingestion_identity(row.source_url) == ingestion_identity:
            return True
        if status_urls_match(row.source_url or "", url):
            return True
        source = {field: row._mapping[f"source_{field}"] for field in source_fields}
        source["source_resolution"] = {field: row._mapping[f"resolution_{field}"] for field in resolution_fields}
        source["source_archive"] = {field: row._mapping[f"archive_{field}"] for field in archive_fields}
        other = SimpleNamespace(source_url=row.source_url, source=source,
                                application={"external_apply_url": row.external_apply_url})
        return any(
            candidate.provider == identity.provider and candidate.identity_hash == identity.identity_hash
            for candidate, _, _ in job_source_identity_candidates(other)
        )

    indexed = statement.join(JobSourceIdentity, JobSourceIdentity.job_id == JobPosting.id).where(
        JobSourceIdentity.provider == identity.provider,
        JobSourceIdentity.identity_hash == identity.identity_hash,
    )
    checked = set()
    for row in db.execute(indexed):
        checked.add(row.id)
        # The registry retains historical identities; verify the current URLs.
        if matches(row):
            return True
    # Keep legacy postings without registry rows protected with a compact fallback.
    if checked:
        statement = statement.where(JobPosting.id.not_in(checked))
    return any(matches(row) for row in db.execute(statement))


class SourceBackgroundWorker(LeasedWorker):
    """Source discovery has its own lease and bounded periodic scheduling sweep."""

    def __init__(self, settings):
        super().__init__(settings, lease_name=SOURCE_WORKER_LEASE, prefix="source_worker")
        self.sweep_cursor = None
        self.next_sweep_at = 0.0

    def _process_once(self, token):
        if monotonic() >= self.next_sweep_at:
            result = sweep_source_reconciliations(self.settings, after=self.sweep_cursor)
            self.sweep_cursor = result.cursor
            self.next_sweep_at = monotonic() + self.settings.source_sweep_interval_seconds
        return process_next_source_reconciliation(
            self.settings, worker_id=self.worker_id, supervisor_lease_token=token, sweep=False,
        )
