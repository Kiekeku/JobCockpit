from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobcockpit.storage.artifacts import StoredArtifactRecord
from jobcockpit.status_evidence import (
    status_evidence_category,
    status_evidence_category_label,
    status_evidence_label,
)
from jobcockpit.utils.ids import new_id


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def api_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


ActiveValidationState = Literal["confirmed_open", "unverified_active", "not_active"]
ACTIVE_OPEN_FRESHNESS = timedelta(days=7)
AvailabilityState = Literal[
    "provider_confirmed_open",
    "page_present",
    "recently_observed_open",
    "source_repair_required",
    "unverified_active",
    "not_active",
]
PROVIDER_CONFIRMED_OPEN_EVIDENCE = frozenset(
    {
        "ashby_public_feed_match",
        "greenhouse_public_feed_match",
        "oracle_structured_job_match",
        "recruitee_public_feed_match",
        "workday_structured_job_match",
    }
)


def active_validation_state(
    status: str,
    check_result: str | None,
    last_checked_at: datetime | None,
    *,
    now: datetime | None = None,
) -> ActiveValidationState:
    if status != "active":
        return "not_active"
    if check_result != "open" or last_checked_at is None:
        return "unverified_active"
    observed_at = last_checked_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if observed_at > current or current - observed_at > ACTIVE_OPEN_FRESHNESS:
        return "unverified_active"
    return "confirmed_open"


def availability_state(
    status: str,
    check_result: str | None,
    evidence_code: str | None,
    last_checked_at: datetime | None,
    last_open_at: datetime | None,
    source_repair_required: bool,
    *,
    now: datetime | None = None,
) -> AvailabilityState:
    if status != "active":
        return "not_active"
    if source_repair_required:
        return "source_repair_required"
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    def is_fresh(value: datetime | None) -> bool:
        if value is None:
            return False
        observed = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return observed <= current and current - observed <= ACTIVE_OPEN_FRESHNESS

    if check_result == "open" and is_fresh(last_checked_at):
        if evidence_code in PROVIDER_CONFIRMED_OPEN_EVIDENCE:
            return "provider_confirmed_open"
        return "page_present"
    if is_fresh(last_open_at):
        return "recently_observed_open"
    return "unverified_active"


AVAILABILITY_STATE_LABELS: dict[AvailabilityState, str] = {
    "provider_confirmed_open": "Provider confirms posting is published",
    "page_present": "Matching posting page is present",
    "recently_observed_open": "Recently observed, latest check inconclusive",
    "source_repair_required": "Source URL needs repair",
    "unverified_active": "Availability unverified",
    "not_active": "Not active",
}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = "candidate_profiles"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_name: Mapped[str] = mapped_column(String(255))
    profile_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CompanyProfile(Base, TimestampMixin):
    __tablename__ = "company_profiles"

    company_profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    normalized_company_name: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    summary: Mapped[str] = mapped_column(Text)
    official_website_url: Mapped[str] = mapped_column(String(1024))
    official_website_domain: Mapped[str] = mapped_column(String(255), index=True)
    evidence: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    research_provider: Mapped[str] = mapped_column(String(64))
    research_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    research_reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    researched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_context(self) -> dict[str, Any]:
        source_urls = list(
            dict.fromkeys(
                str(item.get("source_url") or "").strip()
                for item in self.evidence
                if str(item.get("source_url") or "").strip()
            )
        )
        return {
            "company_name": self.company_name,
            "summary": self.summary,
            "official_website_url": self.official_website_url,
            "source_urls": source_urls,
            "evidence": self.evidence,
            "researched_at": api_datetime(self.researched_at),
        }


class CompanyProfileAlias(Base, TimestampMixin):
    __tablename__ = "company_profile_aliases"

    company_profile_alias_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_profile_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("company_profiles.company_profile_id"), index=True
    )
    alias_key: Mapped[str] = mapped_column(String(128), index=True)
    normalized_company_name: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint(
            "alias_key",
            name="uq_company_profile_alias_key",
        ),
    )


class EvidenceItem(Base, TimestampMixin):
    __tablename__ = "evidence_items"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(64))
    source_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim: Mapped[str] = mapped_column(Text)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_for_resume: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_for_cover_letter: Mapped[bool] = mapped_column(Boolean, default=True)


class StoredArtifact(Base, TimestampMixin):
    __tablename__ = "stored_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    application_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), index=True)
    storage_backend: Mapped[str] = mapped_column(String(32), default="local")
    storage_uri: Mapped[str] = mapped_column(String(1024))
    original_file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    privacy_class: Mapped[str] = mapped_column(String(32), index=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @classmethod
    def from_record(cls, record: StoredArtifactRecord) -> "StoredArtifact":
        return cls(**record.model_dump())


class JobPosting(Base, TimestampMixin):
    __tablename__ = "job_postings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    posting_snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_content_artifact_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stored_artifacts.artifact_id")
    )
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    normalized_title: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    role_type: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    application_deadline: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    duration_months: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        "opportunity_status",
        String(64),
        default="active",
        server_default="active",
        index=True,
    )
    status_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_check_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_check_evidence_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status_check_failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status_check_not_found_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    status_last_open_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_last_open_evidence_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    status_source_repair_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True
    )
    last_applied_capture_seq: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", index=True
    )
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    employer: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    role: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    employment_timing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    location: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    eligibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compensation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    application: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fit_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_job_company_title", "company_name", "normalized_title"),
        Index(
            "ix_job_postings_status_due",
            "opportunity_status",
            "status_next_check_at",
            "created_at",
        ),
        UniqueConstraint("posting_snapshot_hash", name="uq_job_postings_posting_snapshot_hash"),
    )

    def to_api(self) -> dict[str, Any]:
        # Display structured-data titles as text without altering stored evidence.
        role = dict(self.role)
        if isinstance(role.get("job_title"), str):
            role["job_title"] = unescape(role["job_title"])
        source = {
            **self.source,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "posting_snapshot_hash": self.posting_snapshot_hash,
            "raw_content_artifact_id": self.raw_content_artifact_id,
        }
        source_archive_artifact_id = source.get("source_archive_artifact_id")
        if source_archive_artifact_id:
            source["source_archive_download_url"] = (
                f"/api/jobs/{self.id}/source-archive/{source_archive_artifact_id}/download"
            )
        validation = active_validation_state(
            self.status,
            self.status_check_result,
            self.status_last_checked_at,
        )
        status_category = status_evidence_category(
            self.status_check_result,
            self.status_check_evidence_code,
        )
        if validation == "unverified_active" and self.status_check_result == "open":
            status_category = "stale"
        availability = availability_state(
            self.status,
            self.status_check_result,
            self.status_check_evidence_code,
            self.status_last_checked_at,
            self.status_last_open_at,
            self.status_source_repair_required,
        )
        return {
            "id": self.id,
            "created_at": api_datetime(self.created_at),
            "status": self.status,
            "status_validation": validation,
            "availability": {
                "state": availability,
                "label": AVAILABILITY_STATE_LABELS[availability],
                "source_repair_required": self.status_source_repair_required,
                "last_open_at": api_datetime(self.status_last_open_at)
                if self.status_last_open_at
                else None,
                "last_open_evidence_code": self.status_last_open_evidence_code,
                "last_open_evidence_label": status_evidence_label(
                    self.status_last_open_evidence_code
                ),
                "deadline_based": self.status == "expired"
                and self.status_check_result == "expired"
                and self.status_check_evidence_code == "application_deadline_passed",
            },
            "status_check": (
                {
                    "last_checked_at": api_datetime(self.status_last_checked_at)
                    if self.status_last_checked_at
                    else None,
                    "next_check_at": api_datetime(self.status_next_check_at)
                    if self.status_next_check_at
                    else None,
                    "result": self.status_check_result,
                    "evidence_code": self.status_check_evidence_code,
                    "evidence_label": status_evidence_label(self.status_check_evidence_code),
                    "category": status_category,
                    "category_label": status_evidence_category_label(status_category),
                    "failure_count": self.status_check_failure_count,
                }
                if (
                    self.status_last_checked_at
                    or self.status_next_check_at
                    or self.status_check_result
                    or self.status_check_evidence_code
                )
                else None
            ),
            "source": source,
            "employer": self.employer,
            "role": role,
            "duration_months": self.duration_months,
            "employment_timing": self.employment_timing,
            "location": self.location,
            "eligibility": self.eligibility,
            "compensation": self.compensation,
            "application": self.application,
            "fit_analysis": self.fit_analysis,
            "application_summary": {
                "application_id": None,
                "application_status": None,
                "applied_at": None,
                "decision": None,
                "priority": "unknown",
                "next_action_at": None,
            },
            "data_quality": self.data_quality,
        }


class JobPublication(Base, TimestampMixin):
    __tablename__ = "job_publications"

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    destination: Mapped[str] = mapped_column(String(1024))
    remote_id: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    input_data: Mapped[dict] = mapped_column(JSON, default=dict)
    assessment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assessment: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)

    def to_api(self) -> dict:
        return {
            "publication_id": self.publication_id, "job_id": self.job_id,
            "remote_id": self.remote_id, "status": self.status, "attempts": self.attempts,
            "next_attempt_at": api_datetime(self.next_attempt_at) if self.next_attempt_at else None,
            "published_at": api_datetime(self.published_at) if self.published_at else None,
            "reason_code": self.reason_code, "history": self.history,
        }


class JobStatusScanRun(Base, TimestampMixin):
    __tablename__ = "job_status_scan_runs"

    status_scan_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(64), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    checked_count: Mapped[int] = mapped_column(Integer, default=0)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_count: Mapped[int] = mapped_column(Integer, default=0)
    expired_count: Mapped[int] = mapped_column(Integer, default=0)
    inconclusive_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    deferred_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index(
            "uq_job_status_scan_runs_running",
            "status",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    def to_api(self) -> dict[str, Any]:
        return {
            "run_id": self.status_scan_run_id,
            "status": self.status,
            "started_at": api_datetime(self.started_at),
            "finished_at": api_datetime(self.finished_at) if self.finished_at else None,
            "error_message": self.error_message,
            "request": self.request_payload,
            "selected": self.selected_count,
            "checked": self.checked_count,
            "open": self.open_count,
            "closed": self.closed_count,
            "expired": self.expired_count,
            "inconclusive": self.inconclusive_count,
            "failed": self.failed_count,
            "deferred": self.deferred_count,
            "skipped": self.skipped_count,
        }


class JobStatusCheck(Base, TimestampMixin):
    __tablename__ = "job_status_checks"

    status_check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status_scan_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("job_status_scan_runs.status_scan_run_id"), index=True
    )
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    candidate_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(64), index=True)
    detected_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_code: Mapped[str] = mapped_column(String(128))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    status_before: Mapped[str] = mapped_column(String(64))
    status_after: Mapped[str] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_job_status_checks_job_checked", "job_id", "checked_at"),)

    def to_api(self) -> dict[str, Any]:
        return {
            "status_check_id": self.status_check_id,
            "run_id": self.status_scan_run_id,
            "job_id": self.job_id,
            "source_url": self.source_url,
            "source_kind": self.source_kind,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "candidate_kind": self.candidate_kind,
            "diagnostics": self.diagnostics,
            "result": self.result,
            "detected_status": self.detected_status,
            "evidence_code": self.evidence_code,
            "evidence_label": status_evidence_label(self.evidence_code),
            "category": status_evidence_category(self.result, self.evidence_code),
            "category_label": status_evidence_category_label(
                status_evidence_category(self.result, self.evidence_code)
            ),
            "http_status": self.http_status,
            "retryable": self.retryable,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "error_message": self.error_message,
            "checked_at": api_datetime(self.checked_at),
        }


class JobSourceReconciliation(Base, TimestampMixin):
    __tablename__ = "job_source_reconciliations"

    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_guard: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "status": self.status, "outcome": self.outcome,
            "attempts": self.attempts, "reason_code": self.reason_code,
            "next_attempt_at": api_datetime(self.next_attempt_at) if self.next_attempt_at else None,
            "history": self.history,
        }


class JobReprocessRun(Base, TimestampMixin):
    __tablename__ = "job_reprocess_runs"

    reprocess_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    run_type: Mapped[str] = mapped_column("type", String(64), default="full_reprocess", index=True)
    status: Mapped[str] = mapped_column(String(64), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    supervisor_lease_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_job_reprocess_runs_job_status", "job_id", "status"),
        Index("ix_job_reprocess_runs_job_created", "job_id", "created_at", "reprocess_run_id"),
        Index(
            "uq_job_reprocess_runs_active_job",
            "job_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'retry_scheduled')"),
        ),
    )

    def to_api(self) -> dict[str, Any]:
        return {
            "reprocess_run_id": self.reprocess_run_id,
            "job_id": self.job_id,
            "type": self.run_type,
            "status": self.status,
            "attempts": self.attempts,
            "next_attempt_at": (
                api_datetime(self.next_attempt_at) if self.next_attempt_at else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "result": self.result,
            "created_at": api_datetime(self.created_at),
            "updated_at": api_datetime(self.updated_at),
            "started_at": api_datetime(self.started_at) if self.started_at else None,
            "finished_at": api_datetime(self.finished_at) if self.finished_at else None,
        }


class CoverLetterGenerationRun(Base, TimestampMixin):
    __tablename__ = "cover_letter_generation_runs"

    generation_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    posting_snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    supervisor_lease_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_cover_letter_generation_runs_job_status", "job_id", "status"),
        Index(
            "uq_cover_letter_generation_runs_active_job",
            "job_id",
            unique=True,
            sqlite_where=text(
                "status IN ('queued', 'running', 'retry_scheduled', 'waiting_for_profile')"
            ),
        ),
    )

    def to_api(self) -> dict[str, Any]:
        return {
            "generation_run_id": self.generation_run_id,
            "job_id": self.job_id,
            "posting_snapshot_hash": self.posting_snapshot_hash,
            "status": self.status,
            "attempts": self.attempts,
            "next_attempt_at": (
                api_datetime(self.next_attempt_at) if self.next_attempt_at else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "result": self.result,
            "request": self.request_payload,
            "created_at": api_datetime(self.created_at),
            "updated_at": api_datetime(self.updated_at),
            "started_at": api_datetime(self.started_at) if self.started_at else None,
            "finished_at": api_datetime(self.finished_at) if self.finished_at else None,
        }


class PageCaptureSnapshot(Base, TimestampMixin):
    """Immutable private input captured by the browser extension."""

    __tablename__ = "page_capture_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    page_title: Mapped[str] = mapped_column(String(512), default="")
    raw_content_artifact_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stored_artifacts.artifact_id")
    )
    source_archive_artifact_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stored_artifacts.artifact_id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    evidence_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("evidence_seq", name="uq_page_capture_snapshots_evidence_seq"),
    )


class PageCaptureRun(Base, TimestampMixin):
    __tablename__ = "page_capture_runs"

    capture_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("page_capture_snapshots.snapshot_id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), default="generic", index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    identity_strength: Mapped[str] = mapped_column(String(32), default="weak", index=True)
    public_source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    target_job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id"), nullable=True, index=True
    )
    skip_language_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    page_access_verdict: Mapped[str] = mapped_column(String(32), default="pending")
    page_kind: Mapped[str] = mapped_column(String(32), default="pending")
    identity_verdict: Mapped[str] = mapped_column(String(32), default="pending")
    association_verdict: Mapped[str] = mapped_column(String(32), default="pending")
    assessment_evidence_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    supervisor_lease_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    retry_of_capture_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("page_capture_runs.capture_id"), nullable=True, index=True
    )

    __table_args__ = (
        Index(
            "ix_page_capture_runs_status_due",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_page_capture_runs_identity_status",
            "identity_hash",
            "status",
        ),
    )

    def to_api(self) -> dict[str, Any]:
        public_result_keys = {
            "status",
            "job_id",
            "created",
            "fit_reprocess_run_id",
            "opportunity_status",
            "evidence_code",
            "reason",
        }
        public_result = {
            key: value
            for key, value in (self.result or {}).items()
            if key in public_result_keys
            and (value is None or isinstance(value, (str, int, float, bool)))
        }
        return {
            "capture_id": self.capture_id,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": 3,
            "source_url": self.public_source_url or "",
            "target_job_id": self.target_job_id,
            "job_id": self.job_id,
            "page_access_verdict": self.page_access_verdict,
            "page_kind": self.page_kind,
            "identity_verdict": self.identity_verdict,
            "association_verdict": self.association_verdict,
            "assessment_evidence_code": self.assessment_evidence_code,
            "next_attempt_at": (
                api_datetime(self.next_attempt_at) if self.next_attempt_at else None
            ),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "result": public_result,
            "retry_of_capture_id": self.retry_of_capture_id,
            "created_at": api_datetime(self.created_at),
            "updated_at": api_datetime(self.updated_at),
            "started_at": api_datetime(self.started_at) if self.started_at else None,
            "finished_at": api_datetime(self.finished_at) if self.finished_at else None,
            "status_url": f"/api/extension/page-captures/{self.capture_id}",
            "retry_url": f"/api/extension/page-captures/{self.capture_id}/retry",
        }


class PageCaptureSnapshotScope(Base, TimestampMixin):
    """An immutable snapshot-to-subject association used for status and fencing."""

    __tablename__ = "page_capture_snapshot_scopes"

    capture_scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("page_capture_snapshots.snapshot_id"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id"), nullable=True, index=True
    )
    scope_hash: Mapped[str] = mapped_column(String(64), index=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    association_basis: Mapped[str] = mapped_column(
        String(32), default="source_identity", index=True
    )
    identity_strength: Mapped[str] = mapped_column(String(32), default="weak", index=True)
    association_verdict: Mapped[str] = mapped_column(String(32), default="asserted", index=True)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "scope_hash",
            name="uq_page_capture_snapshot_scopes_snapshot_scope",
        ),
        Index(
            "ix_page_capture_snapshot_scopes_scope_snapshot",
            "scope_hash",
            "snapshot_id",
        ),
    )


# Domain name for new code; keep the old class name while this feature is uncommitted.
PageCaptureAssociation = PageCaptureSnapshotScope


class PageCaptureEffect(Base, TimestampMixin):
    """One execution run's observable outcome for one associated job."""

    __tablename__ = "page_capture_effects"

    effect_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capture_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("page_capture_runs.capture_id"), index=True
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("page_capture_snapshots.snapshot_id"), index=True
    )
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    effect_kind: Mapped[str] = mapped_column(String(32), default="processing")
    status: Mapped[str] = mapped_column(String(64), index=True)
    outcome_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "capture_id",
            "job_id",
            "effect_kind",
            name="uq_page_capture_effects_run_job_kind",
        ),
    )


class PageCaptureSubjectHead(Base, TimestampMixin):
    __tablename__ = "page_capture_subject_heads"

    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("page_capture_snapshots.snapshot_id"), index=True
    )


class JobSourceIdentity(Base, TimestampMixin):
    __tablename__ = "job_source_identities"

    job_source_identity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    identity_strength: Mapped[str] = mapped_column(String(32), default="weak", index=True)
    public_source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(64), default="source_url")
    match_priority: Mapped[int] = mapped_column(Integer, default=100)

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "provider",
            "identity_hash",
            name="uq_job_source_identities_job_provider_identity",
        ),
        Index(
            "ix_job_source_identities_provider_identity",
            "provider",
            "identity_hash",
        ),
    )


class BackgroundWorkerLease(Base, TimestampMixin):
    __tablename__ = "background_worker_leases"

    lease_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("lease_token", name="uq_background_worker_leases_lease_token"),
    )


class JobIngestionRun(Base, TimestampMixin):
    __tablename__ = "job_ingestion_runs"

    ingestion_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, index=True)
    search_text: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(64), default="running", index=True)
    alert_level: Mapped[str] = mapped_column(String(32), default="info", index=True)
    jobs_seen_count: Mapped[int] = mapped_column(Integer, default=0)
    jobs_created_count: Mapped[int] = mapped_column(Integer, default=0)
    jobs_duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    jobs_skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    jobs_language_skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def to_api(self) -> dict[str, Any]:
        return {
            "ingestion_run_id": self.ingestion_run_id,
            "provider": self.provider,
            "source_url": self.source_url,
            "search_text": self.search_text,
            "status": self.status,
            "alert_level": self.alert_level,
            "jobs_seen_count": self.jobs_seen_count,
            "jobs_created_count": self.jobs_created_count,
            "jobs_duplicate_count": self.jobs_duplicate_count,
            "jobs_skipped_count": self.jobs_skipped_count,
            "jobs_language_skipped_count": self.jobs_language_skipped_count,
            "error_count": self.error_count,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "request_payload": self.request_payload,
        }


class JobIngestionItem(Base, TimestampMixin):
    __tablename__ = "job_ingestion_items"

    ingestion_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ingestion_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("job_ingestion_runs.ingestion_run_id"), index=True
    )
    source_url: Mapped[str] = mapped_column(String(2048))
    resolved_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    linked_job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_api(self) -> dict[str, Any]:
        return {
            "ingestion_item_id": self.ingestion_item_id,
            "ingestion_run_id": self.ingestion_run_id,
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "canonical_url": self.canonical_url,
            "status": self.status,
            "linked_job_id": self.linked_job_id,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "created_at": api_datetime(self.created_at),
            "updated_at": api_datetime(self.updated_at),
        }


class Application(Base, TimestampMixin):
    __tablename__ = "applications"

    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    status: Mapped[str] = mapped_column(String(64), default="draft", index=True)
    decision: Mapped[str] = mapped_column(String(64), default="undecided", index=True)
    would_accept_if_offered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    follow_up_date: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_applications_candidate_id_job_id"),
    )

    def to_api(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "candidate_id": self.candidate_id,
            "job_id": self.job_id,
            "status": self.status,
            "decision": self.decision,
            "would_accept_if_offered": self.would_accept_if_offered,
            "applied_at": api_datetime(self.applied_at) if self.applied_at else None,
            "follow_up_date": self.follow_up_date,
            "outcome": self.outcome,
            "notes": self.notes,
        }


class CandidateLearnedFact(Base, TimestampMixin):
    __tablename__ = "candidate_learned_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    subject_key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(100), default="")
    term: Mapped[str] = mapped_column(String(16), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("job_postings.id"), nullable=True)
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtensionLearningRun(Base, TimestampMixin):
    __tablename__ = "extension_learning_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="running")
    observations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confirmed_observations: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(String(1000), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExtensionApplicationSession(Base, TimestampMixin):
    __tablename__ = "extension_application_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    page_url: Mapped[str] = mapped_column(String(2048))
    page_title: Mapped[str] = mapped_column(String(500), default="")
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    answers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    draft_status: Mapped[str] = mapped_column(String(32), default="idle")
    draft_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    draft_error: Mapped[str] = mapped_column(String(1000), default="")
    draft_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    document_approval: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExtensionApprovedAnswer(Base, TimestampMixin):
    __tablename__ = "extension_approved_answers"

    answer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    question_signature: Mapped[str] = mapped_column(String(64))
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    answer: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", "question_signature", name="uq_extension_answer_scope"),
    )


class ApplicationPackage(Base, TimestampMixin):
    __tablename__ = "application_packages"

    application_package_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id"), index=True
    )
    package_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(64), default="draft")
    approved_for_extension: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_api(self) -> dict[str, Any]:
        return {
            "application_package_id": self.application_package_id,
            "application_id": self.application_id,
            "package_version": self.package_version,
            "status": self.status,
            "approved_for_extension": self.approved_for_extension,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
        }


class ApplicationPackageDocument(Base, TimestampMixin):
    __tablename__ = "application_package_documents"

    application_package_document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("application_packages.application_package_id"), index=True
    )
    generated_document_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("generated_documents.document_id"), nullable=True
    )
    artifact_id: Mapped[str] = mapped_column(String(64), ForeignKey("stored_artifacts.artifact_id"))
    document_type: Mapped[str] = mapped_column(String(64), index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_for_upload: Mapped[bool] = mapped_column(Boolean, default=False)


class GeneratedDocument(Base, TimestampMixin):
    __tablename__ = "generated_documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("application_packages.application_package_id"), index=True
    )
    document_type: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str] = mapped_column(String(64), ForeignKey("stored_artifacts.artifact_id"))
    editable_artifact_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("stored_artifacts.artifact_id"),
        nullable=True,
        index=True,
    )
    generation_provider: Mapped[str] = mapped_column(String(64), default="heuristic")
    source_format: Mapped[str] = mapped_column(String(32))
    rendered_format: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    content_preview: Mapped[str] = mapped_column(Text)
    claim_mapping_count: Mapped[int] = mapped_column(Integer, default=0)
    truthfulness_check_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("truthfulness_checks.truthfulness_check_id"), nullable=True
    )
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_api(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "application_package_id": self.application_package_id,
            "document_type": self.document_type,
            "artifact_id": self.artifact_id,
            "editable_artifact_id": self.editable_artifact_id,
            "generation_provider": self.generation_provider,
            "source_format": self.source_format,
            "rendered_format": self.rendered_format,
            "sha256": self.sha256,
            "content_preview": self.content_preview,
            "claim_mapping_count": self.claim_mapping_count,
            "approved_by_user": self.approved_by_user,
        }


class TruthfulnessCheck(Base, TimestampMixin):
    __tablename__ = "truthfulness_checks"

    truthfulness_check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("application_packages.application_package_id"), index=True
    )
    status: Mapped[str] = mapped_column(String(64))
    blocking_errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)


class FitAnalysis(Base, TimestampMixin):
    __tablename__ = "fit_analyses"

    fit_analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("job_postings.id"), index=True)
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)


class JobAlertSource(Base, TimestampMixin):
    __tablename__ = "job_alert_sources"

    job_alert_source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="gmail", index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    gmail_query: Mapped[str] = mapped_column(Text)
    sender_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    skip_interest_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_language_filter: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_api(self) -> dict[str, Any]:
        return {
            "job_alert_source_id": self.job_alert_source_id,
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "display_name": self.display_name,
            "gmail_query": self.gmail_query,
            "sender_allowlist": self.sender_allowlist,
            "enabled": self.enabled,
            "skip_interest_filter": self.skip_interest_filter,
            "skip_language_filter": self.skip_language_filter,
        }


class EmailIngestionRun(Base, TimestampMixin):
    __tablename__ = "email_ingestion_runs"

    email_ingestion_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_alert_source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("job_alert_sources.job_alert_source_id")
    )
    provider: Mapped[str] = mapped_column(String(64), default="gmail")
    query_used: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), default="running")
    messages_imported_count: Mapped[int] = mapped_column(Integer, default=0)
    jobs_created_count: Mapped[int] = mapped_column(Integer, default=0)


class JobAlertEmail(Base, TimestampMixin):
    __tablename__ = "job_alert_emails"

    job_alert_email_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email_ingestion_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("email_ingestion_runs.email_ingestion_run_id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), default="gmail")
    provider_message_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    provider_thread_id_hash: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(255))
    sender_email_hash: Mapped[str] = mapped_column(String(64))
    sender_domain: Mapped[str] = mapped_column(String(255))
    received_at: Mapped[str] = mapped_column(String(64))
    body_content_hash: Mapped[str] = mapped_column(String(64))
    extraction_status: Mapped[str] = mapped_column(String(64), default="parsed")

    @classmethod
    def from_message(cls, run_id: str, message: dict[str, Any]) -> "JobAlertEmail":
        return cls(
            job_alert_email_id=new_id("email"),
            email_ingestion_run_id=run_id,
            provider_message_id_hash=message["id_hash"],
            provider_thread_id_hash=message["thread_hash"],
            subject=message["subject"],
            sender_email_hash=message["sender_hash"],
            sender_domain=message["sender_domain"],
            received_at=message["received_at"],
            body_content_hash=message["body_hash"],
        )


class JobAlertEmailProcessingState(Base, TimestampMixin):
    __tablename__ = "job_alert_email_processing_states"

    processing_state_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_alert_source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("job_alert_sources.job_alert_source_id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), default="gmail")
    provider_message_id_hash: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(64), default="previewed")
    last_ingestion_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("email_ingestion_runs.email_ingestion_run_id"), nullable=True
    )
    linked_job_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_link_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0)
    local_notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint(
            "job_alert_source_id",
            "provider_message_id_hash",
            name="uq_email_processing_state_source_message",
        ),
    )

    @classmethod
    def _find_by_source_message(
        cls,
        db,
        source_id: str,
        provider_message_id_hash: str,
    ) -> "JobAlertEmailProcessingState | None":
        return db.execute(
            select(cls).where(
                cls.job_alert_source_id == source_id,
                cls.provider_message_id_hash == provider_message_id_hash,
            )
        ).scalar_one_or_none()

    @classmethod
    def _get_or_create(
        cls, db, source_id: str, message: dict[str, Any]
    ) -> "JobAlertEmailProcessingState":
        row = cls._find_by_source_message(db, source_id, message["id_hash"])
        if row:
            return row
        row = cls(
            processing_state_id=new_id("emailstate"),
            job_alert_source_id=source_id,
            provider_message_id_hash=message["id_hash"],
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            row = cls._find_by_source_message(db, source_id, message["id_hash"])
            if row:
                return row
            db.rollback()
            row = cls._find_by_source_message(db, source_id, message["id_hash"])
            if row:
                return row
            raise
        return row

    @classmethod
    def upsert_preview(
        cls, db, source_id: str, message: dict[str, Any]
    ) -> "JobAlertEmailProcessingState":
        row = cls._get_or_create(db, source_id, message)
        if row.processing_status not in {"processed", "needs_review"}:
            row.processing_status = "previewed"
        row.last_seen_at = utcnow()
        return row

    @classmethod
    def upsert_processed(
        cls,
        db,
        source_id: str,
        message: dict[str, Any],
        run_id: str,
        linked_jobs: int,
        skipped_links: int = 0,
        needs_review: int = 0,
    ) -> "JobAlertEmailProcessingState":
        row = cls._get_or_create(db, source_id, message)
        row.processing_status = "processed"
        row.processed_at = utcnow()
        row.last_ingestion_run_id = run_id
        row.linked_job_count = linked_jobs
        row.skipped_link_count = skipped_links
        row.needs_review_count = needs_review
        return row

    @classmethod
    def upsert_needs_review(
        cls,
        db,
        source_id: str,
        message: dict[str, Any],
        run_id: str,
        linked_jobs: int,
        skipped_links: int = 0,
        needs_review: int = 0,
    ) -> "JobAlertEmailProcessingState":
        row = cls._get_or_create(db, source_id, message)
        row.processing_status = "needs_review"
        row.processed_at = None
        row.last_ingestion_run_id = run_id
        row.linked_job_count = linked_jobs
        row.skipped_link_count = skipped_links
        row.needs_review_count = needs_review
        return row


class JobAlertLink(Base, TimestampMixin):
    __tablename__ = "job_alert_links"

    job_alert_link_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_alert_email_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("job_alert_emails.job_alert_email_id"), index=True
    )
    original_url: Mapped[str] = mapped_column(String(2048))
    resolved_url: Mapped[str] = mapped_column(String(2048))
    canonical_url: Mapped[str] = mapped_column(String(2048), index=True)
    link_text: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    surrounding_text: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column(Integer)
    ingestion_status: Mapped[str] = mapped_column(
        String(64), default="ready_for_review", index=True
    )
    linked_job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("job_postings.id"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    @classmethod
    def from_extracted(cls, email_id: str, link, classification) -> "JobAlertLink":
        return cls(
            job_alert_link_id=new_id("link"),
            job_alert_email_id=email_id,
            original_url=link.href,
            resolved_url=link.href,
            canonical_url=link.href,
            link_text=link.link_text,
            raw_text="",
            surrounding_text=link.surrounding_text,
            classification=classification.classification,
            confidence=int(classification.confidence * 100),
            ingestion_status="ready_for_review",
        )


class AuditLogEntry(Base, TimestampMixin):
    __tablename__ = "audit_log_entries"

    audit_log_entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @classmethod
    def create(
        cls,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> "AuditLogEntry":
        return cls(
            audit_log_entry_id=new_id("audit"),
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload or {},
        )
