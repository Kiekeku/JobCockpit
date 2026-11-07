from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.base import AtsJob, SourceArchive
from jobcockpit.ingestion.posting_discovery import external_aggregator_kind
from jobcockpit.ingestion.language_filter import evaluate_language_requirements, language_filter_error_message
from jobcockpit.ingestion.posting_links import (
    JobImporter,
    PostingLinkClassification,
    PostingLinkContext,
    PostingLinkProcessingResult,
    PostingResolver,
    canonicalize_posting_url,
    posting_url_identity,
    is_successful_posting_link_import,
    prepare_posting_link,
    persist_prepared_posting_link,
)
from jobcockpit.ingestion.concurrency import link_request_slot
from jobcockpit.ingestion.role_interest import RoleInterestMatch, match_job_text_interest, profile_has_role_interest
from jobcockpit.storage.artifacts import ArtifactStorageService


def ats_job_needs_source_fetch(job: AtsJob) -> bool:
    content_url = getattr(job, "content_source_url", None) or job.source_url
    return bool(
        external_aggregator_kind(content_url)
        or posting_url_identity(content_url) != posting_url_identity(job.source_url)
    )


@dataclass(frozen=True)
class PostingBatchItem:
    source_type: str
    source_url: str
    raw_text: str
    title_hint: str | None = None
    portal_source_url: str | None = None
    link_text: str = ""
    surrounding_text: str = ""
    interest_text: str = ""
    trusted_posting: bool = False
    source_archive: SourceArchive | None = None
    fetch_source_archive: bool = True
    content_source_url: str | None = None
    classification: PostingLinkClassification | None = None
    metadata: Any = None


class ExistingPostingResult(Protocol):
    payload: dict
    created: bool


DuplicateLookup = Callable[[PostingBatchItem], ExistingPostingResult | None]


@dataclass(frozen=True)
class PostingBatchItemResult:
    item: PostingBatchItem
    processing_result: PostingLinkProcessingResult
    interest_match: RoleInterestMatch | None = None

    @property
    def counts(self) -> PostingBatchCounts:
        counts = PostingBatchCounts()
        _record_result_count(counts, self.processing_result)
        return counts


@dataclass
class PostingBatchCounts:
    created: int = 0
    reused: int = 0
    interest_skipped: int = 0
    language_skipped: int = 0
    terminal_skipped: int = 0
    blocked: int = 0
    ignored: int = 0
    needs_review: int = 0
    failed: int = 0


@dataclass(frozen=True)
class PreparedPostingBatchItem:
    item: PostingBatchItem
    processing_result: PostingLinkProcessingResult | None
    interest_match: RoleInterestMatch | None = None
    filtered: bool = False


def prepare_posting_batch_item(
    item: PostingBatchItem, *, settings: Settings, profile_data: dict | None,
    skip_interest_filter: bool, skip_language_filter: bool,
    precheck_language_filter: bool = False,
    posting_resolver: PostingResolver | None = None,
    defer_source_fetch: bool = False,
) -> PreparedPostingBatchItem:
    """Filter once and prepare source content without a database or storage."""
    interest_match = match_job_text_interest(item.interest_text or item.raw_text, profile_data)
    if (not skip_interest_filter and profile_has_role_interest(profile_data)
            and not interest_match.matched):
        return PreparedPostingBatchItem(
            item, _interest_skipped_result(item, interest_match), interest_match, filtered=True,
        )
    if precheck_language_filter:
        language_result = evaluate_language_requirements(
            item.raw_text, profile_data, skip_language_filter=skip_language_filter,
        )
        if language_result.should_skip:
            return PreparedPostingBatchItem(
                item, _language_skipped_result(item, language_filter_error_message(language_result)),
                interest_match, filtered=True,
            )
    # An index hit avoids network work, but never authorizes reusing a DB row.
    # The writer must look it up again; a disappeared hit needs normal preparation.
    if defer_source_fetch:
        return PreparedPostingBatchItem(item, None)
    with link_request_slot(item.source_url):
        result = prepare_posting_link(
            _posting_context_for_item(item), settings=settings, posting_resolver=posting_resolver,
            language_filter_profile_data=profile_data, skip_language_filter=skip_language_filter,
        )
    return PreparedPostingBatchItem(item, result)


def persist_posting_batch_item(
    prepared: PreparedPostingBatchItem, *, db: Session,
    storage: ArtifactStorageService, settings: Settings,
    job_importer: JobImporter | None = None,
    duplicate_lookup: DuplicateLookup | None = None,
    prepare_missing: Callable[[PostingBatchItem], PreparedPostingBatchItem] | None = None,
) -> PostingBatchItemResult:
    """Use the preparation decision and perform the authoritative duplicate check."""
    result = prepared.processing_result
    if not prepared.filtered:
        duplicate = duplicate_lookup(prepared.item) if duplicate_lookup else None
        if duplicate is not None:
            result = _duplicate_result(prepared.item, duplicate)
        else:
            if result is None:
                if prepare_missing is None:
                    raise ValueError("Deferred source preparation requires a missing-item preparer")
                result = prepare_missing(prepared.item).processing_result
            if result is None:
                raise ValueError("Missing source preparation")
            result = persist_prepared_posting_link(
                result, db=db, storage=storage, settings=settings, job_importer=job_importer,
            )
    if result is None:
        raise ValueError("Missing filter result")
    return PostingBatchItemResult(prepared.item, result, prepared.interest_match)


def _posting_context_for_item(item: PostingBatchItem) -> PostingLinkContext:
    return PostingLinkContext(
        source_type=item.source_type,
        source_url=item.source_url,
        raw_text=item.raw_text,
        title_hint=item.title_hint,
        link_text=item.link_text,
        surrounding_text=item.surrounding_text,
        trusted_posting=item.trusted_posting,
        source_archive=item.source_archive,
        fetch_source_archive=item.fetch_source_archive,
        content_source_url=item.content_source_url,
        portal_source_url=item.portal_source_url,
    )


def _interest_skipped_result(
    item: PostingBatchItem,
    interest_match: RoleInterestMatch,
) -> PostingLinkProcessingResult:
    canonical_url = canonicalize_posting_url(item.source_url)
    return PostingLinkProcessingResult(
        classification=_classification_for_item(item, reason="interest_filter"),
        ingestion_status="skipped_by_interest_filter",
        original_url=item.source_url,
        resolved_url=item.source_url,
        canonical_url=canonical_url,
        error_message=interest_match.reason,
    )


def _language_skipped_result(item: PostingBatchItem, error_message: str) -> PostingLinkProcessingResult:
    canonical_url = canonicalize_posting_url(item.source_url)
    return PostingLinkProcessingResult(
        classification=_classification_for_item(item, reason="language_filter"),
        ingestion_status="skipped_by_language_filter",
        original_url=item.source_url,
        resolved_url=canonical_url,
        canonical_url=canonical_url,
        error_message=error_message,
    )


def _duplicate_result(item: PostingBatchItem, duplicate: ExistingPostingResult) -> PostingLinkProcessingResult:
    job_id = str(duplicate.payload.get("id") or "")
    return PostingLinkProcessingResult(
        classification=_classification_for_item(item, reason="already_imported"),
        ingestion_status="imported",
        original_url=item.source_url,
        resolved_url=item.source_url,
        canonical_url=canonicalize_posting_url(item.source_url),
        linked_job_id=job_id or None,
        payload=duplicate.payload,
        created=False,
    )


def _classification_for_item(item: PostingBatchItem, *, reason: str) -> PostingLinkClassification:
    if item.classification:
        return item.classification
    return PostingLinkClassification(
        classification="trusted_posting" if item.trusted_posting else "direct_posting",
        confidence=1.0,
        reason=reason,
    )


def _record_result_count(counts: PostingBatchCounts, result: PostingLinkProcessingResult) -> None:
    if is_successful_posting_link_import(result.ingestion_status):
        if result.created:
            counts.created += 1
        else:
            counts.reused += 1
    elif result.ingestion_status == "skipped_by_interest_filter":
        counts.interest_skipped += 1
    elif result.ingestion_status == "skipped_by_language_filter":
        counts.language_skipped += 1
    elif result.ingestion_status == "terminal_status_detected":
        counts.terminal_skipped += 1
    elif result.ingestion_status == "blocked_by_url_policy":
        counts.blocked += 1
    elif result.ingestion_status == "ignored":
        counts.ignored += 1
    elif result.ingestion_status == "needs_external_resolution":
        counts.needs_review += 1
    else:
        counts.failed += 1
