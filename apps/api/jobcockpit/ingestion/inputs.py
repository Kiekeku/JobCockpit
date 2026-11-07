"""The internal posting input shared by transport and source adapters."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from jobcockpit.ingestion.ats_adapters.base import SourceArchive


@dataclass(frozen=True)
class JobIngestPayload:
    source_type: str
    raw_text: str
    title_hint: str | None = None
    source_url: str | None = None
    source_archive: SourceArchive | None = None
    source_resolution: dict[str, Any] | None = None
    skip_language_filter: bool = False
    portal_source_url: str | None = None
    source_identity_hash: str | None = None
    # None preserves the historical absence of this field in source metadata.
    source_url_dedup_safe: bool | None = None

    @classmethod
    def from_request(cls, request) -> JobIngestPayload:
        if isinstance(request, cls):
            return request
        return cls(**{field.name: getattr(request, field.name)
                      for field in fields(cls) if hasattr(request, field.name)})


def request_source_url_dedup_safe(request) -> bool:
    value = getattr(request, "source_url_dedup_safe", None)
    return True if value is None else bool(value)
