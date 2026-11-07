from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.output import (
    INGESTION_COUNT_KEYS,
    OUTPUT_SCHEMA_VERSION,
    empty_ingestion_counts,
    normalize_ats_ingestion,
    normalize_gmail_ingestion,
)

IngestionAuditLifecycle = Literal["started", "completed", "failed"]

_EVENT_TYPES = {
    "started": "ingestion.run_started",
    "completed": "ingestion.run_completed",
    "failed": "ingestion.run_failed",
}


def failed_ingestion_counts() -> dict[str, int]:
    counts = empty_ingestion_counts()
    counts["runs_failed_count"] = 1
    return counts


def ats_ingestion_counts(
    *,
    items: Iterable[dict[str, Any]],
    jobs_created_count: int,
    jobs_duplicate_count: int,
    failed: bool = False,
) -> dict[str, int]:
    output = normalize_ats_ingestion(
        {
            "provider": "ats",
            "jobs_created_count": jobs_created_count,
            "jobs_duplicate_count": jobs_duplicate_count,
            "items": list(items),
        }
    )
    counts = output["totals"]
    counts["runs_failed_count"] = 1 if failed else 0
    return counts


def gmail_ingestion_counts(result: dict[str, Any], *, source_id: str) -> tuple[str, dict[str, int]]:
    output = normalize_gmail_ingestion([{"source_id": source_id, **result}], [])
    return output["status"], output["totals"]


class IngestionAuditService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings

    def record(
        self,
        db: Session,
        *,
        lifecycle: IngestionAuditLifecycle,
        provider: str,
        run_id: str,
        status: str,
        counts: Mapping[str, int],
        source_id: str | None = None,
        source_url: str | None = None,
        error_code: str | None = None,
    ) -> AuditLogEntry:
        normalized_counts = empty_ingestion_counts()
        normalized_counts.update({key: int(counts.get(key) or 0) for key in INGESTION_COUNT_KEYS})
        payload = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "operation": "job_ingestion",
            "provider": provider,
            "run_id": run_id,
            "source_id": source_id,
            "source_url": None if provider == "gmail" else source_url,
            "status": status,
            "counts": normalized_counts,
            "error_code": error_code,
        }
        entry = AuditLogEntry.create(
            _EVENT_TYPES[lifecycle],
            "ingestion_run",
            run_id,
            payload=payload,
        )
        db.add(entry)
        return entry

    def record_durable(self, **kwargs: Any) -> str:
        if self.settings is None:
            raise RuntimeError("Settings are required for durable ingestion audit events")
        with session_scope(self.settings) as db:
            entry = self.record(db, **kwargs)
            db.flush()
            return entry.audit_log_entry_id
