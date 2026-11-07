from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from jobcockpit.api.background_worker_leases import (
    begin_background_worker_lease_transaction,
)

from jobcockpit.api.job_access import job_merged_into_id, resolve_active_job_for_write
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    AuditLogEntry,
    JobPosting,
    JobSourceIdentity,
    PageCaptureEffect,
    PageCaptureRun,
    PageCaptureSnapshotScope,
    PageCaptureSnapshot,
    PageCaptureSubjectHead,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.audit import IngestionAuditService
from jobcockpit.ingestion.output import empty_ingestion_counts
from jobcockpit.ingestion.browser_capture_adapters import (
    CaptureSourceIdentity,
    capture_source_identity,
)
from jobcockpit.schemas.requests import ExtensionPageCaptureRequest
from jobcockpit.storage.artifacts import (
    ArtifactStorageService,
    ArtifactStorageWriteTracker,
)
from jobcockpit.utils.ids import new_id


ACTIVE_PAGE_CAPTURE_STATUSES = {"queued", "running", "retry_scheduled"}
PENDING_PAGE_CAPTURE_STATUSES = {"queued", "retry_scheduled"}
CLAIMABLE_PAGE_CAPTURE_STATUSES = PENDING_PAGE_CAPTURE_STATUSES
PAGE_CAPTURE_MAX_ATTEMPTS = 3
PAGE_CAPTURE_RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=30))
PAGE_CAPTURE_STALE_AFTER = timedelta(minutes=15)
PAGE_CAPTURE_STALE_ERROR = "page_capture_worker_stale"
JOB_IDENTITY_PRIORITIES = {
    "source_url": 100,
    "source_metadata": 125,
    "source_archive": 150,
    "source_resolution": 200,
    "external_apply_url": 300,
    "capture_run": 250,
}
PAGE_CAPTURE_TRANSITIONS = {
    "queued": frozenset({"running", "superseded"}),
    "retry_scheduled": frozenset({"running", "superseded"}),
    "running": frozenset({"retry_scheduled", "succeeded", "skipped", "failed", "superseded"}),
    "succeeded": frozenset(),
    "skipped": frozenset(),
    "failed": frozenset(),
    "superseded": frozenset(),
}
PUBLIC_PAGE_CAPTURE_ERROR_MESSAGES = {
    "capture_artifact_missing": "Captured page artifacts are unavailable",
    "capture_source_identity_missing": "Captured page source identity is unavailable",
    "target_job_not_found": "Target job not found",
    "capture_identity_conflict": "Captured page identity conflicts with multiple jobs",
    "capture_merge_conflict": "Captured page matched jobs that cannot be merged safely",
    "posting_parser_fallback": "Configured posting parser did not complete successfully",
    "codex_cli_timeout": "Configured posting parser timed out",
    "codex_cli_invalid_json": "Configured posting parser returned an invalid response",
    "codex_cli_invalid_parsed_job": "Configured posting parser returned an invalid response",
    "captured_job_missing": "Captured job could not be loaded",
    "capture_processing_rejected": "Captured page processing was rejected",
    "capture_authentication_wall": "Captured page is an authentication wall",
    "capture_page_challenge": "Captured page is a browser challenge",
    "capture_page_incomplete": "Captured page content is incomplete",
    "capture_non_job_page": "Captured page is not a job posting",
}


class PageCaptureClaimLost(RuntimeError):
    pass


class PageCaptureProcessingError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def ensure_page_capture_transition(current: str, next_status: str) -> None:
    if next_status not in PAGE_CAPTURE_TRANSITIONS.get(current, frozenset()):
        raise RuntimeError(f"Invalid page capture transition: {current} -> {next_status}")


def public_page_capture_error(error: Exception, error_code: str | None) -> tuple[str, str]:
    raw_code = error_code or getattr(error, "error_code", None) or error.__class__.__name__
    code = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw_code)).strip("._-")[:128]
    if not code:
        code = "capture_processing_error"
    message = PUBLIC_PAGE_CAPTURE_ERROR_MESSAGES.get(
        code, f"Captured page processing failed ({code})"
    )
    return code, message


def page_capture_error_is_retryable(error: Exception) -> bool:
    if isinstance(error, PageCaptureProcessingError):
        return error.retryable
    if isinstance(error, HTTPException):
        return error.status_code in {408, 429} or error.status_code >= 500
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OperationalError):
        message = str(error).casefold()
        return "locked" in message or "busy" in message
    return False


def _scope_hash(kind: str, value: str) -> str:
    return sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


def capture_scope_values(
    identity: CaptureSourceIdentity, target_job_id: str | None
) -> tuple[tuple[str, str], ...]:
    scopes = [("source", _scope_hash("source", identity.identity_hash))]
    if target_job_id:
        scopes.append(("job", _scope_hash("job", target_job_id)))
    return tuple(scopes)


@dataclass(frozen=True)
class CaptureAssociationSpec:
    scope_kind: str
    scope_hash: str
    job_id: str | None
    association_basis: str
    identity_strength: str
    association_verdict: str


def capture_association_values(
    db: Session,
    identity: CaptureSourceIdentity,
    target_job_id: str | None,
) -> tuple[CaptureAssociationSpec, ...]:
    source_subject = (
        identity.identity_hash
        if identity.strength == "authoritative" or not target_job_id
        else f"{identity.identity_hash}\0job:{target_job_id}"
    )
    associations = [
        CaptureAssociationSpec(
            scope_kind="source",
            scope_hash=_scope_hash("source", source_subject),
            job_id=None,
            association_basis="source_identity",
            identity_strength=identity.strength,
            association_verdict="asserted",
        )
    ]
    jobs: dict[str, tuple[JobPosting, str]] = {}
    if target_job_id:
        target = db.get(JobPosting, target_job_id)
        if target is not None:
            jobs[target.id] = (target, "explicit_target")
    if identity.strength == "authoritative":
        for identity_row in db.execute(
            select(JobSourceIdentity).where(
                JobSourceIdentity.provider == identity.provider,
                JobSourceIdentity.identity_hash == identity.identity_hash,
                JobSourceIdentity.identity_strength == "authoritative",
            )
        ).scalars():
            job = db.get(JobPosting, identity_row.job_id)
            if job is None:
                continue
            canonical_id = job_merged_into_id(job) or job.id
            canonical = db.get(JobPosting, canonical_id)
            if canonical is not None:
                jobs.setdefault(canonical.id, (canonical, "authoritative_identity"))
    for job, basis in jobs.values():
        associations.append(
            CaptureAssociationSpec(
                scope_kind="job",
                scope_hash=_scope_hash("job", job.id),
                job_id=job.id,
                association_basis=basis,
                identity_strength=identity.strength,
                association_verdict=("asserted" if basis == "explicit_target" else "verified"),
            )
        )
    return tuple(associations)


def associate_snapshot_with_job(
    db: Session,
    *,
    snapshot_id: str,
    job_id: str,
    association_basis: str,
    identity_strength: str,
    association_verdict: str = "verified",
) -> PageCaptureSnapshotScope:
    job = db.get(JobPosting, job_id)
    if job is None:
        raise PageCaptureProcessingError(
            "target_job_not_found", "Target job not found", retryable=False
        )
    canonical = resolve_active_job_for_write(job, db)
    scope_hash = _scope_hash("job", canonical.id)
    existing = db.execute(
        select(PageCaptureSnapshotScope).where(
            PageCaptureSnapshotScope.snapshot_id == snapshot_id,
            PageCaptureSnapshotScope.scope_hash == scope_hash,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = PageCaptureSnapshotScope(
            capture_scope_id=new_id("capture_scope"),
            snapshot_id=snapshot_id,
            job_id=canonical.id,
            scope_hash=scope_hash,
            scope_kind="job",
            association_basis=association_basis,
            identity_strength=identity_strength,
            association_verdict=association_verdict,
        )
        db.add(existing)
        db.flush()
    else:
        existing.job_id = canonical.id
        existing.association_basis = association_basis
        existing.identity_strength = identity_strength
        existing.association_verdict = association_verdict

    snapshot = db.get(PageCaptureSnapshot, snapshot_id)
    current_head = db.get(PageCaptureSubjectHead, scope_hash)
    current_snapshot = (
        db.get(PageCaptureSnapshot, current_head.snapshot_id) if current_head is not None else None
    )
    if snapshot is not None and (
        current_snapshot is None or snapshot.evidence_seq >= current_snapshot.evidence_seq
    ):
        db.merge(PageCaptureSubjectHead(scope_hash=scope_hash, snapshot_id=snapshot_id))
    db.flush()
    return existing


def capture_associated_job_ids(db: Session, snapshot_id: str) -> set[str]:
    return set(
        db.execute(
            select(PageCaptureSnapshotScope.job_id).where(
                PageCaptureSnapshotScope.snapshot_id == snapshot_id,
                PageCaptureSnapshotScope.job_id.is_not(None),
            )
        ).scalars()
    )


def upsert_page_capture_effects(
    db: Session,
    run: PageCaptureRun,
    *,
    status: str,
    job_ids: set[str] | None = None,
    outcome_code: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    result: dict | None = None,
    applied: bool = False,
) -> None:
    affected_job_ids = (
        job_ids if job_ids is not None else capture_associated_job_ids(db, run.snapshot_id)
    )
    now = datetime.now(UTC)
    for job_id in sorted(affected_job_ids):
        effect = db.execute(
            select(PageCaptureEffect).where(
                PageCaptureEffect.capture_id == run.capture_id,
                PageCaptureEffect.job_id == job_id,
                PageCaptureEffect.effect_kind == "processing",
            )
        ).scalar_one_or_none()
        if effect is None:
            effect = PageCaptureEffect(
                effect_id=new_id("capture_effect"),
                capture_id=run.capture_id,
                snapshot_id=run.snapshot_id,
                job_id=job_id,
                effect_kind="processing",
                status=status,
            )
            db.add(effect)
        effect.status = status
        effect.outcome_code = outcome_code
        effect.error_code = error_code
        effect.error_message = error_message
        effect.result = result or {}
        effect.applied_at = now if applied else None


def canonical_capture_target_job_id(db: Session, target_job_id: str | None) -> str | None:
    """Normalize an optional target and project merged IDs onto the active job."""
    normalized = str(target_job_id or "").strip()
    if not normalized:
        return None
    target = db.get(JobPosting, normalized)
    if target is None:
        raise HTTPException(status_code=404, detail="Target job not found")
    return resolve_active_job_for_write(target, db).id


def capture_content_hash(
    request: ExtensionPageCaptureRequest,
    raw_text: str,
    identity: CaptureSourceIdentity | None = None,
) -> str:
    archive_text = request.html if request.html is not None else request.visible_text
    identity = identity or capture_source_identity(request.source_url)
    identity_hash = identity.identity_hash if identity else ""
    processing_inputs = (
        raw_text,
        archive_text,
        "html" if request.html is not None else "text",
        identity_hash,
        request.page_title,
        request.target_job_id or "",
        "skip_language_filter" if request.skip_language_filter else "enforce_language_filter",
    )
    return sha256("\0".join(processing_inputs).encode("utf-8")).hexdigest()


def _begin_immediate(db: Session) -> None:
    if db.get_bind().dialect.name == "sqlite" and not db.in_transaction():
        db.execute(text("BEGIN IMMEDIATE"))


def _capture_scopes(db: Session, capture_id: str) -> list[PageCaptureSnapshotScope]:
    run = db.get(PageCaptureRun, capture_id)
    if run is None:
        return []
    return list(
        db.execute(
            select(PageCaptureSnapshotScope).where(
                PageCaptureSnapshotScope.snapshot_id == run.snapshot_id
            )
        ).scalars()
    )


def _next_evidence_seq(db: Session) -> int:
    statement = select(func.coalesce(func.max(PageCaptureSnapshot.evidence_seq), 0))
    return int(db.execute(statement).scalar_one()) + 1


def _active_identical_capture(
    db: Session,
    *,
    scope_hashes: tuple[str, ...],
    content_hash: str,
) -> PageCaptureRun | None:
    expected_scopes = set(scope_hashes)
    candidates = list(
        db.execute(
            select(PageCaptureRun)
            .join(
                PageCaptureSnapshot,
                PageCaptureSnapshot.snapshot_id == PageCaptureRun.snapshot_id,
            )
            .where(
                PageCaptureSnapshot.content_hash == content_hash,
                PageCaptureRun.status.in_(ACTIVE_PAGE_CAPTURE_STATUSES),
            )
            .order_by(PageCaptureRun.created_at.desc(), PageCaptureRun.capture_id.desc())
        ).scalars()
    )
    for candidate in candidates:
        candidate_scopes = {scope.scope_hash for scope in _capture_scopes(db, candidate.capture_id)}
        if candidate_scopes == expected_scopes:
            return candidate
    return None


def _supersede_pending_for_scopes(db: Session, scope_hashes: tuple[str, ...]) -> list[str]:
    snapshot_ids = set(
        db.execute(
            select(PageCaptureSubjectHead.snapshot_id).where(
                PageCaptureSubjectHead.scope_hash.in_(scope_hashes)
            )
        ).scalars()
    )
    superseded: list[str] = []
    now = datetime.now(UTC)
    pending_runs = db.execute(
        select(PageCaptureRun).where(
            PageCaptureRun.snapshot_id.in_(snapshot_ids),
            PageCaptureRun.status.in_(PENDING_PAGE_CAPTURE_STATUSES),
        )
    ).scalars()
    for run in pending_runs:
        ensure_page_capture_transition(run.status, "superseded")
        run.status = "superseded"
        upsert_page_capture_effects(
            db,
            run,
            status="superseded",
            outcome_code="newer_capture_queued",
        )
        run.finished_at = now
        run.next_attempt_at = None
        run.error_code = "newer_capture_queued"
        run.error_message = "Superseded by a newer page capture"
        run.retryable = False
        run.updated_at = now
        superseded.append(run.capture_id)
        db.add(
            AuditLogEntry.create(
                "extension.page_capture_superseded",
                "page_capture_run",
                run.capture_id,
                payload={"job_id": run.job_id or run.target_job_id},
            )
        )
    return superseded


def _insert_scopes_and_heads(
    db: Session,
    snapshot_id: str,
    associations: tuple[CaptureAssociationSpec, ...],
    *,
    create_heads: bool = True,
) -> None:
    for association in associations:
        db.add(
            PageCaptureSnapshotScope(
                capture_scope_id=new_id("capture_scope"),
                snapshot_id=snapshot_id,
                job_id=association.job_id,
                scope_hash=association.scope_hash,
                scope_kind=association.scope_kind,
                association_basis=association.association_basis,
                identity_strength=association.identity_strength,
                association_verdict=association.association_verdict,
            )
        )
        if create_heads:
            db.merge(
                PageCaptureSubjectHead(
                    scope_hash=association.scope_hash,
                    snapshot_id=snapshot_id,
                )
            )


def _newer_snapshot_run_for_scopes(
    db: Session,
    *,
    retry_of: PageCaptureRun,
    snapshot: PageCaptureSnapshot,
    scope_hashes: tuple[str, ...],
) -> PageCaptureRun | None:
    """Find work whose captured evidence must outrank this replay."""
    candidates = db.execute(
        select(PageCaptureRun, PageCaptureSnapshot)
        .join(
            PageCaptureSnapshot,
            PageCaptureSnapshot.snapshot_id == PageCaptureRun.snapshot_id,
        )
        .join(
            PageCaptureSnapshotScope,
            PageCaptureSnapshotScope.snapshot_id == PageCaptureRun.snapshot_id,
        )
        .where(
            PageCaptureRun.capture_id != retry_of.capture_id,
            PageCaptureSnapshotScope.scope_hash.in_(scope_hashes),
        )
    ).all()
    blockers: list[tuple[int, PageCaptureRun]] = []
    for run, candidate_snapshot in candidates:
        if candidate_snapshot.evidence_seq > snapshot.evidence_seq:
            blockers.append((candidate_snapshot.evidence_seq, run))
        elif (
            candidate_snapshot.snapshot_id == snapshot.snapshot_id
            and run.status in ACTIVE_PAGE_CAPTURE_STATUSES.union({"succeeded", "skipped"})
        ):
            blockers.append((candidate_snapshot.evidence_seq, run))
    if not blockers:
        return None
    return max(
        blockers,
        key=lambda item: (item[0], item[1].created_at, item[1].capture_id),
    )[1]


def enqueue_page_capture(
    request: ExtensionPageCaptureRequest,
    raw_text: str,
    db: Session,
    storage: ArtifactStorageService,
    *,
    retry_of: PageCaptureRun | None = None,
    resolved_job_id: str | None = None,
) -> tuple[PageCaptureRun, bool]:
    if resolved_job_id is not None:
        raise ValueError("Capture enqueue cannot resolve or associate jobs")
    _begin_immediate(db)
    target_job_id = canonical_capture_target_job_id(db, request.target_job_id)
    request = request.model_copy(update={"target_job_id": target_job_id})

    if retry_of is None:
        identity = capture_source_identity(request.source_url)
        if identity is None:
            raise HTTPException(status_code=422, detail="Source URL is not a valid capture URL")
        associations = capture_association_values(db, identity, request.target_job_id)
        content_hash = capture_content_hash(request, raw_text, identity)
        provider = identity.provider
        identity_hash = identity.identity_hash
        identity_strength = identity.strength
        public_source_url = identity.public_source_url
        snapshot = None
    else:
        snapshot = db.get(PageCaptureSnapshot, retry_of.snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=409, detail="Captured snapshot is unavailable")
        associations = tuple(
            CaptureAssociationSpec(
                scope_kind=scope.scope_kind,
                scope_hash=scope.scope_hash,
                job_id=scope.job_id,
                association_basis=scope.association_basis,
                identity_strength=scope.identity_strength,
                association_verdict=scope.association_verdict,
            )
            for scope in _capture_scopes(db, retry_of.capture_id)
        )
        content_hash = snapshot.content_hash
        provider = retry_of.provider
        identity_hash = retry_of.identity_hash
        identity_strength = retry_of.identity_strength
        public_source_url = retry_of.public_source_url
    scope_hashes = tuple(association.scope_hash for association in associations)
    if retry_of is None:
        active = _active_identical_capture(
            db,
            scope_hashes=scope_hashes,
            content_hash=content_hash,
        )
        if active:
            return active, False

    capture_id = new_id("capture")
    tracker = ArtifactStorageWriteTracker(storage)
    commit_succeeded = False
    try:
        if retry_of is not None:
            assert snapshot is not None
        else:
            raw_record = tracker.write_bytes(
                artifact_type="job_snapshot",
                content=raw_text.encode("utf-8"),
                original_file_name="captured-job-posting.txt",
                content_type="text/plain",
                privacy_class="private",
                source_kind="browser_capture_pending",
            )
            archive_body = (
                request.html.encode("utf-8")
                if request.html is not None
                else request.visible_text.encode("utf-8")
            )
            archive_record = tracker.write_bytes(
                artifact_type="job_source_archive",
                content=archive_body,
                original_file_name=(
                    "captured-job-posting.html"
                    if request.html is not None
                    else "captured-job-posting.txt"
                ),
                content_type=("text/html" if request.html is not None else "text/plain"),
                privacy_class="private",
                source_kind="browser_capture_pending_archive",
            )
            db.add(StoredArtifact.from_record(raw_record))
            db.add(StoredArtifact.from_record(archive_record))
            db.flush()
            snapshot = PageCaptureSnapshot(
                snapshot_id=new_id("capture_snapshot"),
                source_url=request.source_url,
                page_title=request.page_title,
                raw_content_artifact_id=raw_record.artifact_id,
                source_archive_artifact_id=archive_record.artifact_id,
                content_hash=content_hash,
                evidence_seq=_next_evidence_seq(db),
            )
            db.add(snapshot)
            db.flush()

        newer_snapshot_run = (
            _newer_snapshot_run_for_scopes(
                db,
                retry_of=retry_of,
                snapshot=snapshot,
                scope_hashes=scope_hashes,
            )
            if retry_of is not None
            else None
        )
        superseded_ids = (
            []
            if newer_snapshot_run is not None
            else _supersede_pending_for_scopes(db, scope_hashes)
        )
        queued = newer_snapshot_run is None
        now = datetime.now(UTC)
        run = PageCaptureRun(
            capture_id=capture_id,
            snapshot_id=snapshot.snapshot_id,
            provider=provider,
            identity_hash=identity_hash,
            identity_strength=identity_strength,
            public_source_url=public_source_url,
            target_job_id=target_job_id,
            job_id=None,
            skip_language_filter=request.skip_language_filter,
            status="queued" if queued else "superseded",
            attempts=0,
            finished_at=None if queued else now,
            error_code=None if queued else "stale_capture_replay",
            error_message=(
                None if queued else "Replay suppressed because a newer page capture exists"
            ),
            retryable=False,
            result={},
            retry_of_capture_id=retry_of.capture_id if retry_of else None,
        )
        db.add(run)
        db.flush()
        if retry_of is None:
            _insert_scopes_and_heads(db, snapshot.snapshot_id, associations, create_heads=queued)
            db.flush()
        upsert_page_capture_effects(
            db,
            run,
            status=run.status,
            outcome_code=run.error_code,
        )
        db.add(
            AuditLogEntry.create(
                (
                    "extension.page_capture_queued"
                    if queued
                    else "extension.page_capture_superseded"
                ),
                "page_capture_run",
                run.capture_id,
                payload={
                    "source_url": run.public_source_url,
                    "target_job_id": run.target_job_id,
                    "retry_of_capture_id": run.retry_of_capture_id,
                    "superseded_capture_ids": superseded_ids,
                    "newer_capture_id": (
                        newer_snapshot_run.capture_id if newer_snapshot_run else None
                    ),
                },
            )
        )
        db.commit()
        commit_succeeded = True
        db.refresh(run)
        return run, queued
    except Exception:
        db.rollback()
        if not commit_succeeded:
            tracker.rollback()
        raise


def retry_page_capture(
    capture_id: str, db: Session, storage: ArtifactStorageService
) -> tuple[PageCaptureRun, bool]:
    _begin_immediate(db)
    run = db.get(PageCaptureRun, capture_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Page capture not found")
    if run.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed page captures can be retried")
    snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=409, detail="Captured snapshot is unavailable")
    raw_artifact = db.get(StoredArtifact, snapshot.raw_content_artifact_id)
    archive_artifact = db.get(StoredArtifact, snapshot.source_archive_artifact_id)
    if not raw_artifact or not archive_artifact:
        raise HTTPException(status_code=409, detail="Captured page artifacts are unavailable")
    archive = storage.read_bytes(archive_artifact.storage_uri).decode("utf-8")
    raw_text = storage.read_bytes(raw_artifact.storage_uri).decode("utf-8")
    request = ExtensionPageCaptureRequest.model_construct(
        source_url=snapshot.source_url,
        page_title=snapshot.page_title,
        visible_text=archive if archive_artifact.content_type != "text/html" else raw_text,
        html=archive if archive_artifact.content_type == "text/html" else None,
        raw_text=raw_text,
        target_job_id=run.target_job_id,
        skip_language_filter=run.skip_language_filter,
    )
    return enqueue_page_capture(
        request,
        raw_text,
        db,
        storage,
        retry_of=run,
    )


def reassign_page_capture_job_references(
    db: Session,
    *,
    duplicate_job_id: str,
    canonical_job_id: str,
) -> None:
    """Merge job subjects using immutable snapshot evidence order."""
    duplicate_scope_hash = _scope_hash("job", duplicate_job_id)
    canonical_scope_hash = _scope_hash("job", canonical_job_id)
    job_scope_hashes = (duplicate_scope_hash, canonical_scope_hash)
    job_scopes = list(
        db.execute(
            select(PageCaptureSnapshotScope).where(
                PageCaptureSnapshotScope.scope_kind == "job",
                PageCaptureSnapshotScope.scope_hash.in_(job_scope_hashes),
            )
        ).scalars()
    )
    candidate_snapshot_ids = {scope.snapshot_id for scope in job_scopes}
    candidate_snapshot_ids.update(
        db.execute(
            select(PageCaptureSubjectHead.snapshot_id).where(
                PageCaptureSubjectHead.scope_hash.in_(job_scope_hashes)
            )
        ).scalars()
    )

    duplicate_scopes = [scope for scope in job_scopes if scope.scope_hash == duplicate_scope_hash]
    for scope in duplicate_scopes:
        canonical_scope = db.execute(
            select(PageCaptureSnapshotScope).where(
                PageCaptureSnapshotScope.snapshot_id == scope.snapshot_id,
                PageCaptureSnapshotScope.scope_hash == canonical_scope_hash,
            )
        ).scalar_one_or_none()
        if canonical_scope is not None:
            db.delete(scope)
        else:
            scope.scope_hash = canonical_scope_hash
            scope.job_id = canonical_job_id

    snapshots = list(
        db.execute(
            select(PageCaptureSnapshot).where(
                PageCaptureSnapshot.snapshot_id.in_(candidate_snapshot_ids)
            )
        ).scalars()
    )
    winner_snapshot = max(snapshots, key=lambda item: item.evidence_seq, default=None)
    pending_runs = list(
        db.execute(
            select(PageCaptureRun).where(
                PageCaptureRun.snapshot_id.in_(candidate_snapshot_ids),
                PageCaptureRun.status.in_(PENDING_PAGE_CAPTURE_STATUSES),
            )
        ).scalars()
    )
    winner_run = max(
        (
            run
            for run in pending_runs
            if winner_snapshot is not None and run.snapshot_id == winner_snapshot.snapshot_id
        ),
        key=lambda run: (run.created_at, run.capture_id),
        default=None,
    )
    now = datetime.now(UTC)
    for run in pending_runs:
        if winner_run is not None and run.capture_id == winner_run.capture_id:
            continue
        ensure_page_capture_transition(run.status, "superseded")
        run.status = "superseded"
        run.finished_at = now
        run.next_attempt_at = None
        run.error_code = "newer_capture_queued"
        run.error_message = "Superseded by a newer page capture"
        upsert_page_capture_effects(
            db,
            run,
            status="superseded",
            outcome_code="newer_capture_queued",
        )

        run.retryable = False
        run.updated_at = now
        db.add(
            AuditLogEntry.create(
                "extension.page_capture_superseded",
                "page_capture_run",
                run.capture_id,
                payload={"job_id": canonical_job_id},
            )
        )

    db.execute(
        delete(PageCaptureSubjectHead).where(
            PageCaptureSubjectHead.scope_hash.in_(job_scope_hashes)
        )
    )
    if winner_snapshot is not None:
        db.add(
            PageCaptureSubjectHead(
                scope_hash=canonical_scope_hash,
                snapshot_id=winner_snapshot.snapshot_id,
            )
        )

    duplicate_effects = list(
        db.execute(
            select(PageCaptureEffect).where(PageCaptureEffect.job_id == duplicate_job_id)
        ).scalars()
    )
    for effect in duplicate_effects:
        canonical_effect = db.execute(
            select(PageCaptureEffect).where(
                PageCaptureEffect.capture_id == effect.capture_id,
                PageCaptureEffect.job_id == canonical_job_id,
                PageCaptureEffect.effect_kind == effect.effect_kind,
            )
        ).scalar_one_or_none()
        if canonical_effect is not None:
            db.delete(effect)
        else:
            effect.job_id = canonical_job_id

    db.execute(
        update(PageCaptureRun)
        .where(PageCaptureRun.target_job_id == duplicate_job_id)
        .values(target_job_id=canonical_job_id)
    )
    db.execute(
        update(PageCaptureRun)
        .where(PageCaptureRun.job_id == duplicate_job_id)
        .values(job_id=canonical_job_id)
    )


def latest_page_capture_for_job(db: Session, job_id: str) -> PageCaptureRun | None:
    return latest_page_captures_by_job(db, [job_id]).get(job_id)


def latest_page_captures_by_job(db: Session, job_ids: list[str]) -> dict[str, PageCaptureRun]:
    if not job_ids:
        return {}
    ranked = select(
        PageCaptureRun.capture_id,
        PageCaptureSnapshotScope.job_id,
        func.row_number().over(
            partition_by=PageCaptureSnapshotScope.job_id,
            order_by=(
                PageCaptureSnapshot.evidence_seq.desc(),
                case((PageCaptureRun.status == "superseded", 0), else_=1).desc(),
                PageCaptureRun.created_at.desc(), PageCaptureRun.capture_id.desc(),
            ),
        ).label("position"),
    ).join(
        PageCaptureSnapshot, PageCaptureSnapshot.snapshot_id == PageCaptureRun.snapshot_id,
    ).join(
        PageCaptureSnapshotScope,
        PageCaptureSnapshotScope.snapshot_id == PageCaptureRun.snapshot_id,
    ).where(PageCaptureSnapshotScope.job_id.in_(job_ids)).subquery()
    rows = db.execute(select(PageCaptureRun, ranked.c.job_id).join(
        ranked, ranked.c.capture_id == PageCaptureRun.capture_id,
    ).where(ranked.c.position == 1))
    return {job_id: run for run, job_id in rows}


def _page_capture_processing_api(
    run: PageCaptureRun,
    effect: PageCaptureEffect | None,
) -> dict:
    payload = run.to_api()
    if effect is None:
        return payload
    effect_is_behind_terminal_run = (
        run.status not in ACTIVE_PAGE_CAPTURE_STATUSES
        and effect.status in ACTIVE_PAGE_CAPTURE_STATUSES
    )
    payload.update(
        {
            "status": (run.status if effect_is_behind_terminal_run else effect.status),
            "effect_outcome_code": effect.outcome_code,
            "error_code": (
                payload["error_code"] if effect_is_behind_terminal_run else effect.error_code
            ),
            "error_message": (
                payload["error_message"] if effect_is_behind_terminal_run else effect.error_message
            ),
        }
    )
    return payload


def latest_page_capture_processing_by_job(
    db: Session,
    job_ids: list[str],
) -> dict[str, dict]:
    runs_by_job = latest_page_captures_by_job(db, job_ids)
    if not runs_by_job:
        return {}
    effects = list(
        db.execute(
            select(PageCaptureEffect).where(
                PageCaptureEffect.job_id.in_(runs_by_job),
                PageCaptureEffect.capture_id.in_({run.capture_id for run in runs_by_job.values()}),
                PageCaptureEffect.effect_kind == "processing",
            )
        ).scalars()
    )
    effects_by_run_and_job = {(effect.capture_id, effect.job_id): effect for effect in effects}
    return {
        job_id: _page_capture_processing_api(
            run,
            effects_by_run_and_job.get((run.capture_id, job_id)),
        )
        for job_id, run in runs_by_job.items()
    }


def latest_page_capture_processing_for_job(db: Session, job_id: str) -> dict | None:
    return latest_page_capture_processing_by_job(db, [job_id]).get(job_id)


def _retain_latest_capture(
    result: dict[str, PageCaptureRun],
    freshness: dict[str, tuple[int, int, datetime, str]],
    job_id: str,
    candidate: PageCaptureRun,
    evidence_seq: int,
) -> None:
    run_created_at = candidate.created_at
    if run_created_at.tzinfo is None:
        run_created_at = run_created_at.replace(tzinfo=UTC)
    candidate_freshness = (
        evidence_seq,
        0 if candidate.status == "superseded" else 1,
        run_created_at.astimezone(UTC),
        candidate.capture_id,
    )
    empty_freshness = (
        0,
        0,
        datetime.min.replace(tzinfo=UTC),
        "",
    )
    if candidate_freshness > freshness.get(job_id, empty_freshness):
        result[job_id] = candidate
        freshness[job_id] = candidate_freshness


def claim_page_capture_run(
    db: Session,
    capture_id: str,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> PageCaptureRun | None:
    worker_id = worker_id or new_id("worker")
    if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
        return None
    now = datetime.now(UTC)
    claim_token = new_id("claim")
    claimed = db.execute(
        update(PageCaptureRun)
        .where(
            PageCaptureRun.capture_id == capture_id,
            PageCaptureRun.status.in_(CLAIMABLE_PAGE_CAPTURE_STATUSES),
            or_(
                PageCaptureRun.next_attempt_at.is_(None),
                PageCaptureRun.next_attempt_at <= now,
            ),
        )
        .values(
            status="running",
            attempts=PageCaptureRun.attempts + 1,
            started_at=now,
            next_attempt_at=None,
            error_code=None,
            error_message=None,
            retryable=False,
            worker_id=worker_id,
            claim_token=claim_token,
            supervisor_lease_token=supervisor_lease_token,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    db.expire_all()
    run = db.get(PageCaptureRun, capture_id)
    if run is None:
        return None
    upsert_page_capture_effects(db, run, status="running")
    db.add(
        AuditLogEntry.create(
            "extension.page_capture_attempt_started",
            "page_capture_run",
            capture_id,
            payload={"attempt": run.attempts if run else None},
        )
    )
    if run is not None and run.attempts == 1:
        IngestionAuditService().record(
            db,
            lifecycle="started",
            provider="browser_capture",
            run_id=run.capture_id,
            status="running",
            counts=empty_ingestion_counts(),
            source_url=run.public_source_url,
        )
    db.flush()
    return run


def claim_next_page_capture(
    settings: Settings,
    *,
    worker_id: str | None = None,
    supervisor_lease_token: str | None = None,
) -> str | None:
    worker_id = worker_id or new_id("worker")
    now = datetime.now(UTC)
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(db, worker_id, supervisor_lease_token):
            return None
        capture_id = db.execute(
            select(PageCaptureRun.capture_id)
            .where(
                PageCaptureRun.status.in_(CLAIMABLE_PAGE_CAPTURE_STATUSES),
                or_(
                    PageCaptureRun.next_attempt_at.is_(None),
                    PageCaptureRun.next_attempt_at <= now,
                ),
            )
            .order_by(PageCaptureRun.created_at, PageCaptureRun.capture_id)
            .limit(1)
        ).scalar_one_or_none()
        if capture_id is None:
            return None
        return (
            capture_id
            if claim_page_capture_run(
                db,
                capture_id,
                worker_id=worker_id,
                supervisor_lease_token=supervisor_lease_token,
            )
            else None
        )


def page_capture_snapshot_is_current(db: Session, snapshot_id: str) -> bool:
    scopes = list(
        db.execute(
            select(PageCaptureSnapshotScope).where(
                PageCaptureSnapshotScope.snapshot_id == snapshot_id
            )
        ).scalars()
    )
    if not scopes:
        return False
    heads = {
        head.scope_hash: head.snapshot_id
        for head in db.execute(
            select(PageCaptureSubjectHead).where(
                PageCaptureSubjectHead.scope_hash.in_([scope.scope_hash for scope in scopes])
            )
        ).scalars()
    }
    return all(heads.get(scope.scope_hash) == snapshot_id for scope in scopes)


def page_capture_snapshot_is_current_for_jobs(
    db: Session,
    snapshot_id: str,
    job_ids: set[str],
) -> bool:
    if not job_ids:
        return page_capture_snapshot_is_current(db, snapshot_id)
    snapshot = db.get(PageCaptureSnapshot, snapshot_id)
    if snapshot is None:
        return False
    job_scope_hashes = [_scope_hash("job", job_id) for job_id in job_ids]
    newest_job_evidence_seq = db.execute(
        select(func.max(PageCaptureSnapshot.evidence_seq))
        .join(
            PageCaptureSubjectHead,
            PageCaptureSubjectHead.snapshot_id == PageCaptureSnapshot.snapshot_id,
        )
        .where(PageCaptureSubjectHead.scope_hash.in_(job_scope_hashes))
    ).scalar_one_or_none()
    return newest_job_evidence_seq is None or int(newest_job_evidence_seq) <= snapshot.evidence_seq


def _restore_pending_heads(db: Session, run: PageCaptureRun) -> bool:
    return page_capture_snapshot_is_current(db, run.snapshot_id)


def _record_terminal_capture_ingestion_audit(
    db: Session,
    run: PageCaptureRun,
) -> None:
    if run.status not in {"failed", "superseded"}:
        return
    counts = empty_ingestion_counts()
    if run.status == "failed":
        lifecycle = "failed"
        counts["runs_failed_count"] = 1
    else:
        lifecycle = "completed"
        counts["items_seen_count"] = 1
        counts["items_skipped_count"] = 1
    IngestionAuditService().record(
        db,
        lifecycle=lifecycle,
        provider="browser_capture",
        run_id=run.capture_id,
        status=run.status,
        counts=counts,
        source_url=run.public_source_url,
        error_code=run.error_code,
    )


def record_page_capture_failure(
    capture_id: str,
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
    code, message = public_page_capture_error(error, error_code)
    now = datetime.now(UTC)
    with session_scope(settings) as db:
        if expected_supervisor_lease_token is not None and not (
            begin_background_worker_lease_transaction(
                db, worker_id, expected_supervisor_lease_token
            )
        ):
            return None
        run = db.get(PageCaptureRun, capture_id)
        if not run:
            return None
        if run.status != "running":
            return run.to_api()
        if worker_id is not None and run.worker_id != worker_id:
            return run.to_api()
        if expected_attempt is not None and run.attempts != expected_attempt:
            return run.to_api()
        if expected_claim_token is not None and run.claim_token != expected_claim_token:
            return run.to_api()
        if (
            expected_supervisor_lease_token is not None
            and run.supervisor_lease_token != expected_supervisor_lease_token
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

        is_current = page_capture_snapshot_is_current(db, run.snapshot_id)
        should_retry = is_current and retryable and run.attempts < PAGE_CAPTURE_MAX_ATTEMPTS
        next_status = (
            "superseded" if not is_current else "retry_scheduled" if should_retry else "failed"
        )
        if not is_current:
            code = "newer_capture_queued"
            message = "Superseded by a newer page capture"
        ensure_page_capture_transition(run.status, next_status)
        run.status = next_status
        run.error_code = code
        run.error_message = message
        if code in {
            "capture_authentication_wall",
            "capture_page_challenge",
            "capture_page_incomplete",
            "capture_non_job_page",
        }:
            run.identity_verdict = "rejected"
        elif code == "capture_identity_conflict":
            run.identity_verdict = "rejected"
            run.association_verdict = "conflict"
        run.retryable = retryable and next_status != "superseded"
        run.worker_id = None
        run.claim_token = None
        run.supervisor_lease_token = None
        run.updated_at = now
        if should_retry:
            run.next_attempt_at = now + PAGE_CAPTURE_RETRY_DELAYS[run.attempts - 1]
            run.finished_at = None
        else:
            run.next_attempt_at = None
            run.finished_at = now
        upsert_page_capture_effects(
            db,
            run,
            status=next_status,
            outcome_code=code,
            error_code=code if next_status in {"failed", "retry_scheduled"} else None,
            error_message=(message if next_status in {"failed", "retry_scheduled"} else None),
        )
        db.add(
            AuditLogEntry.create(
                (
                    "extension.page_capture_retry_scheduled"
                    if should_retry
                    else f"extension.page_capture_{next_status}"
                ),
                "page_capture_run",
                run.capture_id,
                payload={
                    "attempts": run.attempts,
                    "error_code": code,
                    "retryable": should_retry,
                    "page_access_verdict": run.page_access_verdict,
                    "page_kind": run.page_kind,
                    "identity_verdict": run.identity_verdict,
                    "association_verdict": run.association_verdict,
                    "assessment_evidence_code": run.assessment_evidence_code,
                },
            )
        )
        _record_terminal_capture_ingestion_audit(db, run)
        db.flush()
        return run.to_api()


def recover_stale_page_capture_runs(
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
    stale_before = now - PAGE_CAPTURE_STALE_AFTER
    recovered = 0
    with session_scope(settings) as db:
        if required_lease_token is not None and not (
            begin_background_worker_lease_transaction(db, required_worker_id, required_lease_token)
        ):
            return 0
        runs = list(
            db.execute(select(PageCaptureRun).where(PageCaptureRun.status == "running")).scalars()
        )
        for run in runs:
            started_at = run.started_at
            if started_at is not None and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            stale = started_at is None or started_at <= stale_before
            orphaned = bool(
                recover_orphaned
                and active_worker_id
                and (
                    run.worker_id != active_worker_id
                    or run.supervisor_lease_token != active_lease_token
                )
            )
            owned = bool(recover_owned_by and run.worker_id == recover_owned_by)
            if not (stale or orphaned or owned):
                continue
            if not _restore_pending_heads(db, run):
                next_status = "superseded"
            elif run.attempts >= PAGE_CAPTURE_MAX_ATTEMPTS:
                next_status = "failed"
            else:
                next_status = "retry_scheduled"
            ensure_page_capture_transition(run.status, next_status)
            run.status = next_status
            run.worker_id = None
            run.claim_token = None
            run.supervisor_lease_token = None
            run.error_code = PAGE_CAPTURE_STALE_ERROR
            run.error_message = "Capture worker ownership expired"
            run.retryable = next_status == "retry_scheduled"
            run.updated_at = now
            if next_status == "retry_scheduled":
                run.next_attempt_at = now
                run.finished_at = None
            else:
                run.next_attempt_at = None
                run.finished_at = now
            db.add(
                AuditLogEntry.create(
                    "extension.page_capture_stale_recovered",
                    "page_capture_run",
                    run.capture_id,
                    payload={"status": next_status, "attempts": run.attempts},
                )
            )
            upsert_page_capture_effects(
                db,
                run,
                status=next_status,
                outcome_code=PAGE_CAPTURE_STALE_ERROR,
                error_code=(
                    PAGE_CAPTURE_STALE_ERROR
                    if next_status in {"failed", "retry_scheduled"}
                    else None
                ),
                error_message=(
                    run.error_message if next_status in {"failed", "retry_scheduled"} else None
                ),
            )

            _record_terminal_capture_ingestion_audit(db, run)
            recovered += 1
    return recovered


@dataclass(frozen=True)
class PageCaptureJobMatch:
    jobs: tuple[JobPosting, ...]
    identity_strength: str

    @property
    def job(self) -> JobPosting:
        return self.jobs[0]

    @property
    def authoritative_merge_job_ids(self) -> frozenset[str]:
        if self.identity_strength != "authoritative":
            return frozenset()
        return frozenset(job.id for job in self.jobs[1:])


def resolve_job_by_source_identity(
    db: Session,
    run: PageCaptureRun,
    snapshot: PageCaptureSnapshot,
) -> PageCaptureJobMatch | None:
    identity_rows = list(
        db.execute(
            select(JobSourceIdentity).where(
                JobSourceIdentity.provider == run.provider,
                JobSourceIdentity.identity_hash == run.identity_hash,
            )
        ).scalars()
    )
    candidates: dict[str, tuple[int, JobPosting]] = {}
    for identity in identity_rows:
        job = db.get(JobPosting, identity.job_id)
        if job is None:
            continue
        canonical_id = job_merged_into_id(job) or job.id
        canonical_job = db.get(JobPosting, canonical_id)
        if canonical_job is None:
            continue
        current = candidates.get(canonical_id)
        if current is None or identity.match_priority > current[0]:
            candidates[canonical_id] = (identity.match_priority, canonical_job)

    if candidates:
        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                -item[0],
                item[1].created_at,
                item[1].id,
            ),
        )
        selected = ranked[0][1]
        return PageCaptureJobMatch(
            jobs=tuple(job for _priority, job in ranked),
            identity_strength=run.identity_strength,
        )

    # Compatibility fallback for rows created before the identity registry was
    # introduced. A redacted display URL is not a locator when its private URL
    # contained only non-public query keys; matching on it would collapse
    # distinct postings that share a board route.
    source_identity = capture_source_identity(snapshot.source_url)
    if source_identity is None or not source_identity.source_url_dedup_safe:
        return None
    source_urls = {
        source_identity.canonical_source_url,
        snapshot.source_url,
    }
    candidates = list(
        db.execute(select(JobPosting).where(JobPosting.source_url.in_(source_urls))).scalars()
    )
    if not candidates:
        return None
    canonical_jobs: dict[str, JobPosting] = {}
    for selected in candidates:
        canonical_id = job_merged_into_id(selected) or selected.id
        canonical_jobs[canonical_id] = db.get(JobPosting, canonical_id) or selected
    return PageCaptureJobMatch(
        jobs=tuple(sorted(canonical_jobs.values(), key=lambda job: (job.created_at, job.id))),
        identity_strength=run.identity_strength,
    )


def job_source_identity_candidates(
    job: JobPosting,
) -> tuple[tuple[CaptureSourceIdentity, str, int], ...]:
    source = job.source if isinstance(job.source, dict) else {}
    if source.get("merged_into_job_id"):
        return ()
    application = job.application if isinstance(job.application, dict) else {}
    source_resolution = (
        source.get("source_resolution") if isinstance(source.get("source_resolution"), dict) else {}
    )
    source_archive = (
        source.get("source_archive") if isinstance(source.get("source_archive"), dict) else {}
    )
    values: list[tuple[object, str]] = [
        (application.get("external_apply_url"), "external_apply_url"),
        (source_resolution.get("original_url"), "source_resolution"),
        (source_resolution.get("resolved_url"), "source_resolution"),
        (source_resolution.get("canonical_url"), "source_resolution"),
        (source_resolution.get("fallback_url"), "source_resolution"),
        (source.get("portal_source_url"), "source_metadata"),
    ]
    if source.get("source_url_dedup_safe") is not False:
        values.extend(
            (
                (job.source_url, "source_url"),
                (source.get("source_url"), "source_url"),
                (source_archive.get("source_url"), "source_archive"),
                (source_archive.get("final_url"), "source_archive"),
            )
        )
    portal_urls = source.get("portal_source_urls")
    if isinstance(portal_urls, (list, tuple, set)):
        values.extend((value, "source_metadata") for value in portal_urls)

    by_key: dict[tuple[str, str], tuple[CaptureSourceIdentity, str, int]] = {}
    for raw_url, source_kind in values:
        identity = capture_source_identity(str(raw_url or ""))
        if identity is None:
            continue
        priority = JOB_IDENTITY_PRIORITIES[source_kind]
        key = (identity.provider, identity.identity_hash)
        current = by_key.get(key)
        if current is None or priority > current[2]:
            by_key[key] = (identity, source_kind, priority)
    return tuple(by_key.values())


def upsert_job_source_identity(
    db: Session,
    job: JobPosting,
    *,
    source_url: str | None = None,
    provider: str | None = None,
    identity_hash: str | None = None,
    public_source_url: str | None = None,
    identity_strength: str | None = None,
    source_kind: str = "source_url",
    match_priority: int | None = None,
) -> JobSourceIdentity | None:
    if identity_hash is None:
        identity = capture_source_identity(source_url or job.source_url or "")
        if identity is None:
            return None
        provider = identity.provider
        identity_hash = identity.identity_hash
        public_source_url = identity.public_source_url
        identity_strength = identity.strength
    assert provider is not None
    priority = (
        match_priority
        if match_priority is not None
        else JOB_IDENTITY_PRIORITIES.get(source_kind, 100)
    )
    existing = next(
        (
            candidate
            for candidate in db.new
            if isinstance(candidate, JobSourceIdentity)
            and candidate.job_id == job.id
            and candidate.provider == provider
            and candidate.identity_hash == identity_hash
        ),
        None,
    )
    if existing is None:
        existing = db.execute(
            select(JobSourceIdentity).where(
                JobSourceIdentity.job_id == job.id,
                JobSourceIdentity.provider == provider,
                JobSourceIdentity.identity_hash == identity_hash,
            )
        ).scalar_one_or_none()
    if existing is None:
        existing = JobSourceIdentity(
            job_source_identity_id=new_id("job_identity"),
            job_id=job.id,
            provider=provider,
            identity_hash=identity_hash,
            public_source_url=public_source_url,
            identity_strength=identity_strength or "weak",
            source_kind=source_kind,
            match_priority=priority,
        )
        db.add(existing)
    else:
        canonical_job_id = job_merged_into_id(job) or job.id
        existing.job_id = canonical_job_id
        existing.public_source_url = public_source_url or existing.public_source_url
        if identity_strength == "authoritative":
            existing.identity_strength = "authoritative"
        if priority > existing.match_priority:
            existing.source_kind = source_kind
            existing.match_priority = priority
    return existing


def sync_job_source_identities(db: Session, job: JobPosting) -> int:
    created = 0
    for identity, source_kind, priority in job_source_identity_candidates(job):
        exists = (
            any(
                isinstance(candidate, JobSourceIdentity)
                and candidate.job_id == job.id
                and candidate.provider == identity.provider
                and candidate.identity_hash == identity.identity_hash
                for candidate in db.new
            )
            or db.execute(
                select(JobSourceIdentity.job_source_identity_id).where(
                    JobSourceIdentity.job_id == job.id,
                    JobSourceIdentity.provider == identity.provider,
                    JobSourceIdentity.identity_hash == identity.identity_hash,
                )
            ).first()
            is not None
        )
        upsert_job_source_identity(
            db,
            job,
            provider=identity.provider,
            identity_hash=identity.identity_hash,
            public_source_url=identity.public_source_url,
            identity_strength=identity.strength,
            source_kind=source_kind,
            match_priority=priority,
        )
        if not exists:
            created += 1
    return created


def page_capture_identity_was_accepted(run: PageCaptureRun) -> bool:
    return run.identity_verdict == "accepted"


def backfill_job_source_identities(db: Session) -> int:
    created = 0
    for job in db.execute(
        select(JobPosting).order_by(JobPosting.created_at, JobPosting.id)
    ).scalars():
        created += sync_job_source_identities(db, job)
    for run in db.execute(
        select(PageCaptureRun)
        .where(PageCaptureRun.job_id.is_not(None))
        .order_by(PageCaptureRun.created_at, PageCaptureRun.capture_id)
    ).scalars():
        if not page_capture_identity_was_accepted(run):
            continue
        job = db.get(JobPosting, run.job_id)
        if job is None:
            continue
        job = db.get(JobPosting, job_merged_into_id(job) or job.id) or job
        exists = (
            any(
                isinstance(candidate, JobSourceIdentity)
                and candidate.job_id == job.id
                and candidate.provider == run.provider
                and candidate.identity_hash == run.identity_hash
                for candidate in db.new
            )
            or db.execute(
                select(JobSourceIdentity.job_source_identity_id).where(
                    JobSourceIdentity.job_id == job.id,
                    JobSourceIdentity.provider == run.provider,
                    JobSourceIdentity.identity_hash == run.identity_hash,
                )
            ).first()
            is not None
        )
        upsert_job_source_identity(
            db,
            job,
            provider=run.provider,
            identity_hash=run.identity_hash,
            public_source_url=run.public_source_url,
            identity_strength=run.identity_strength,
            source_kind="capture_run",
            match_priority=JOB_IDENTITY_PRIORITIES["capture_run"],
        )
        if not exists:
            created += 1
    return created
