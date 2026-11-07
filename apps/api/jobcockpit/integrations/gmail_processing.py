from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    CandidateProfile,
    EmailIngestionRun,
    JobAlertEmail,
    JobAlertEmailProcessingState,
    JobAlertLink,
    JobAlertSource,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.concurrency import (
    bounded_call, coordinated_ingestion, gmail_scope,
)
from jobcockpit.ingestion.item_execution import execute_posting_items
from jobcockpit.ingestion.posting_batch import (
    PostingBatchItem, persist_posting_batch_item, prepare_posting_batch_item,
)
from jobcockpit.ingestion.posting_links import (
    JobImporter,
    PostingLinkClassification,
    PostingResolver,
    SKIPPED_POSTING_LINK_INGESTION_STATUSES,
    canonicalize_posting_url,
    default_posting_resolver,
    durable_posting_link_error_message,
    is_successful_posting_link_import,
)
from jobcockpit.integrations.gmail import SYNTHETIC_GMAIL_MESSAGES, classify_email_link
from jobcockpit.integrations.gmail_adapters import gmail_adapter_for_source
from jobcockpit.integrations.gmail_live import (
    GmailProviderConfigurationError,
    GmailProviderOperationError,
    GoogleGmailMessageProvider,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


logger = logging.getLogger(__name__)


class GmailProcessingError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        provider_diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.provider_diagnostics = dict(provider_diagnostics or {})
        self.error_code = str(
            self.provider_diagnostics.get("error_code") or "gmail_processing_error"
        )


def message_allowed_by_source(source: JobAlertSource, message: dict) -> bool:
    allowed_senders = {sender.lower() for sender in source.sender_allowlist}
    if not allowed_senders:
        return True
    return str(message.get("sender_domain", "")).lower() in allowed_senders


def preview_gmail_source(
    *,
    db: Session,
    source: JobAlertSource,
    settings: Settings | None = None,
    messages: Sequence[dict] | None = None,
) -> dict:
    messages = _resolve_messages(settings=settings, source=source, messages=messages)
    adapter = gmail_adapter_for_source(source)
    previewed_messages = []
    for message in messages:
        if not message_allowed_by_source(source, message):
            continue
        links = adapter.extract_links(message)
        state = JobAlertEmailProcessingState.upsert_preview(db, source.job_alert_source_id, message)
        previewed_messages.append(
            {
                "message_review_id": message["id_hash"],
                "subject": message["subject"],
                "processing_status": state.processing_status,
                "links": [
                    {**link.model_dump(), **classify_email_link(link).model_dump()}
                    for link in links
                ],
            }
        )
    return {"messages": previewed_messages, "mailbox_mutations": []}


@coordinated_ingestion(gmail_scope, lambda status, detail: GmailProcessingError(status, detail))
def process_gmail_source(
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings,
    source: JobAlertSource,
    approved_message_review_ids: Iterable[str] | None = None,
    require_preview: bool = False,
    messages: Sequence[dict] | None = None,
    job_importer: JobImporter | None = None,
    posting_resolver: PostingResolver | None = None,
    ingestion_run_id: str | None = None,
) -> dict:
    source_id = source.job_alert_source_id
    candidate_id = source.candidate_id
    gmail_query = source.gmail_query
    skip_interest_filter = source.skip_interest_filter
    skip_language_filter = source.skip_language_filter
    requested_review_ids = None
    if approved_message_review_ids is not None:
        requested_review_ids = set(approved_message_review_ids)
        if not requested_review_ids:
            raise GmailProcessingError(400, "approved_message_review_ids cannot be empty")

    fetch_max_results = (
        settings.gmail_scan_max_results
        if requested_review_ids is not None or not require_preview
        else settings.gmail_max_results
    )
    messages = bounded_call(
        "fetch", _resolve_messages,
        settings=settings,
        source=source,
        messages=messages,
        max_results=fetch_max_results,
    )
    if job_importer is None:
        from jobcockpit.services.workflows import persist_job_ingest_result

        job_importer = persist_job_ingest_result
    provided_posting_resolver = posting_resolver
    if posting_resolver is None:
        posting_resolver = default_posting_resolver(settings)

    current_messages = [
        message for message in messages if message_allowed_by_source(source, message)
    ]
    current_messages_by_review_id = {message["id_hash"]: message for message in current_messages}
    if requested_review_ids is None:
        requested_review_ids = set(current_messages_by_review_id)
    else:
        unknown_review_ids = requested_review_ids - set(current_messages_by_review_id)
        if unknown_review_ids:
            raise GmailProcessingError(400, "Unknown message review ID")

    if require_preview:
        _validate_messages_were_previewed(db, source, requested_review_ids)

    run = EmailIngestionRun(
        email_ingestion_run_id=ingestion_run_id or new_id("emailrun"),
        job_alert_source_id=source_id,
        provider="gmail",
        query_used=gmail_query,
        status="running",
    )
    run_id = run.email_ingestion_run_id
    db.add(run)
    db.commit()

    processed = []
    jobs_created = 0
    jobs_discovered = 0
    jobs_reused = 0
    unresolved_links = 0
    messages_needing_review = 0
    processed_items = []
    adapter = gmail_adapter_for_source(source)
    profile = db.get(CandidateProfile, candidate_id)
    profile_data = dict(profile.profile_data) if profile else None
    db.commit()

    def checkpoint_message(message_state):
        nonlocal messages_needing_review
        if message_state["remaining"]:
            return
        message = message_state["message"]
        message_link_ids = message_state["link_ids"]
        linked_jobs_for_message = message_state["linked"]
        skipped_links_for_message = message_state["skipped"]
        needs_review_for_message = message_state["review"]
        db.expire_all()
        if needs_review_for_message:
            messages_needing_review += 1
            state = JobAlertEmailProcessingState.upsert_needs_review(
                db,
                source_id,
                message,
                run_id,
                linked_jobs_for_message,
                skipped_links_for_message,
                needs_review_for_message,
            )
        else:
            state = JobAlertEmailProcessingState.upsert_processed(
                db,
                source_id,
                message,
                run_id,
                linked_jobs_for_message,
                skipped_links_for_message,
                needs_review_for_message,
            )
        message_state["summary"] = {
            "message_review_id": message["id_hash"],
            "processing_status": state.processing_status,
        }
        persisted_message_links = [
            link_row
            for link_id in message_link_ids
            if (link_row := db.get(JobAlertLink, link_id)) is not None
        ]
        message_state["items"] = [_gmail_link_item(row) for row in persisted_message_links]
        db.commit()

    message_work = []
    all_pending_links = []
    for message in current_messages:
        if message["id_hash"] not in requested_review_ids:
            continue
        processing_state = _processing_state_for_message(db, source, message, require_preview)
        if processing_state.processing_status == "processed":
            continue
        retrying_needs_review = processing_state.processing_status == "needs_review"
        if retrying_needs_review and not posting_resolver:
            continue

        email = _existing_email_for_message(db, source, message) if retrying_needs_review else None
        if not email:
            email = JobAlertEmail.from_message(run_id, message)
            db.add(email)
            db.flush()
        linked_jobs_for_message = 0
        skipped_links_for_message = 0
        needs_review_for_message = 0
        pending_links: list[tuple[str, PostingBatchItem]] = []
        message_link_rows: list[JobAlertLink] = []
        for link in adapter.extract_links(message):
            classification = classify_email_link(link)
            link_raw_text = adapter.raw_text_for_link(message, link)
            link_row = (
                _existing_link_for_email(db, email.job_alert_email_id, link.href)
                if retrying_needs_review
                else None
            )
            if link_row:
                link_row.classification = classification.classification
                link_row.confidence = int(classification.confidence * 100)
            else:
                link_row = JobAlertLink.from_extracted(
                    email.job_alert_email_id, link, classification
                )
                db.add(link_row)
            message_link_rows.append(link_row)
            link_row.raw_text = link_raw_text
            link_row.surrounding_text = link.surrounding_text
            if retrying_needs_review and is_successful_posting_link_import(
                link_row.ingestion_status
            ):
                linked_jobs_for_message += 1
                jobs_discovered += 1
                jobs_reused += 1
                continue
            if (
                retrying_needs_review
                and link_row.ingestion_status in SKIPPED_POSTING_LINK_INGESTION_STATUSES
            ):
                skipped_links_for_message += 1
                continue
            batch_item = PostingBatchItem(
                source_type="gmail_alert",
                source_url=link.href,
                raw_text=link_raw_text,
                link_text=link.link_text,
                surrounding_text=link.surrounding_text,
                interest_text=link_raw_text or link.link_text or link.surrounding_text,
                classification=PostingLinkClassification(
                    classification=classification.classification,
                    confidence=classification.confidence,
                    reason=classification.reason,
                ),
            )
            pending_links.append((link_row.job_alert_link_id, batch_item))

        message_link_ids = [link_row.job_alert_link_id for link_row in message_link_rows]
        db.commit()
        message_state = {
            "message": message, "link_ids": message_link_ids,
            "linked": linked_jobs_for_message, "skipped": skipped_links_for_message,
            "review": needs_review_for_message, "remaining": len(pending_links),
        }
        message_work.append(message_state)
        checkpoint_message(message_state)
        all_pending_links.extend((message_state, link_id, item) for link_id, item in pending_links)

    def prepare_item(pair):
        return prepare_posting_batch_item(
            pair[2], settings=settings, profile_data=profile_data,
            skip_interest_filter=skip_interest_filter,
            skip_language_filter=skip_language_filter,
            posting_resolver=provided_posting_resolver,
        )

    def persist_item(work, prepared, item_db, tracked_storage):
        _message_state, link_id, _batch_item = work
        item_result = persist_posting_batch_item(
            prepared, db=item_db, storage=tracked_storage, settings=settings, job_importer=job_importer,
        )
        processing_result = item_result.processing_result
        persisted_link = item_db.get(JobAlertLink, link_id)
        if persisted_link is None:
            raise RuntimeError("Gmail ingestion link record was not found")
        persisted_link.classification = processing_result.classification.classification
        persisted_link.confidence = int(processing_result.classification.confidence * 100)
        persisted_link.ingestion_status = processing_result.ingestion_status
        persisted_link.resolved_url = processing_result.resolved_url
        persisted_link.canonical_url = processing_result.canonical_url
        persisted_link.error_message = durable_posting_link_error_message(processing_result)
        if processing_result.linked_job_id:
            persisted_link.linked_job_id = processing_result.linked_job_id
        return item_result

    def record_item_failure(work, error, failure_db):
        _message_state, link_id, _batch_item = work
        failed_link = failure_db.get(JobAlertLink, link_id)
        if failed_link is None:
            raise error
        failed_link.ingestion_status = "ready_for_review"
        failed_link.linked_job_id = None
        failed_link.error_message = "posting_processing_failed"
        logger.warning(
            "Gmail ingestion link failed for %s in run %s", link_id, run_id,
            exc_info=(type(error), error, error.__traceback__),
        )

    def completed_item(work, item_result):
        nonlocal jobs_discovered, jobs_created, jobs_reused, unresolved_links
        message_state, _link_id, _batch_item = work
        if isinstance(item_result, Exception):
            message_state["review"] += 1
            unresolved_links += 1
        else:
            processing_result = item_result.processing_result
            if is_successful_posting_link_import(processing_result.ingestion_status):
                message_state["linked"] += 1
                jobs_discovered += 1
                if processing_result.created:
                    jobs_created += 1
                else:
                    jobs_reused += 1
            elif processing_result.ingestion_status in SKIPPED_POSTING_LINK_INGESTION_STATUSES:
                message_state["skipped"] += 1
            elif processing_result.ingestion_status == "needs_external_resolution":
                message_state["review"] += 1
                unresolved_links += 1
        message_state["remaining"] -= 1
        checkpoint_message(message_state)

    execute_posting_items(
        all_pending_links, settings=settings, storage=storage, prepare=prepare_item,
        persist=persist_item, record_failure=record_item_failure, completed=completed_item,
    )

    # Keep response ordering independent of worker completion order.
    for message_state in message_work:
        processed.append(message_state["summary"])
        processed_items.extend(message_state["items"])

    run = db.get(EmailIngestionRun, run_id)
    if run is None:
        raise RuntimeError("Gmail ingestion run record was not found")
    run.messages_imported_count = len(processed)
    run.jobs_created_count = jobs_created
    run.status = "completed"
    db.commit()
    return {
        "email_ingestion_run_id": run_id,
        "processed_messages": processed,
        "jobs_created_count": jobs_created,
        "jobs_discovered_count": jobs_discovered,
        "jobs_reused_count": jobs_reused,
        "messages_needing_review_count": messages_needing_review,
        "unresolved_links_count": unresolved_links,
        "mailbox_mutations": [],
        "items": processed_items,
    }


def mark_email_ingestion_run_failed(settings: Settings, run_id: str) -> None:
    """Durably mark a Gmail run failed without rolling back completed item transactions."""
    with session_scope(settings) as failure_db:
        run = failure_db.get(EmailIngestionRun, run_id)
        if run is not None:
            run.status = "failed"


def _gmail_link_item(link: JobAlertLink) -> dict:
    # Email links can contain personal addresses, tracking values, or access tokens.
    # The durable database row remains available for local investigation.
    return {
        "item_id": link.job_alert_link_id,
        "source_url": None,
        "resolved_url": None,
        "canonical_url": None,
        "status": link.ingestion_status,
        "linked_job_id": link.linked_job_id,
        "error_message": None,
        "retryable": link.ingestion_status in {"needs_external_resolution", "ready_for_review"},
        "metadata": {},
    }


def _existing_email_for_message(
    db: Session,
    source: JobAlertSource,
    message: dict,
) -> JobAlertEmail | None:
    return (
        db.execute(
            select(JobAlertEmail)
            .join(
                EmailIngestionRun,
                JobAlertEmail.email_ingestion_run_id == EmailIngestionRun.email_ingestion_run_id,
            )
            .where(
                EmailIngestionRun.job_alert_source_id == source.job_alert_source_id,
                JobAlertEmail.provider_message_id_hash == message["id_hash"],
            )
            .order_by(JobAlertEmail.created_at.asc())
        )
        .scalars()
        .first()
    )


def _existing_link_for_email(
    db: Session,
    email_id: str,
    href: str,
) -> JobAlertLink | None:
    canonical_href = canonicalize_posting_url(href)
    direct_match = (
        db.execute(
            select(JobAlertLink)
            .where(
                JobAlertLink.job_alert_email_id == email_id,
            )
            .order_by(JobAlertLink.created_at.asc())
        )
        .scalars()
        .all()
    )
    for link in direct_match:
        stored_urls = (link.original_url, link.resolved_url, link.canonical_url)
        if href in stored_urls or canonical_href in stored_urls:
            return link
    for link in direct_match:
        for stored_url in (link.original_url, link.resolved_url, link.canonical_url):
            if canonicalize_posting_url(stored_url) == canonical_href:
                return link
    return None


def _validate_messages_were_previewed(
    db: Session,
    source: JobAlertSource,
    requested_review_ids: set[str],
) -> None:
    for review_id in requested_review_ids:
        processing_state = db.execute(
            select(JobAlertEmailProcessingState).where(
                JobAlertEmailProcessingState.job_alert_source_id == source.job_alert_source_id,
                JobAlertEmailProcessingState.provider_message_id_hash == review_id,
            )
        ).scalar_one_or_none()
        if not processing_state:
            raise GmailProcessingError(409, "Message must be previewed before import")


def _processing_state_for_message(
    db: Session,
    source: JobAlertSource,
    message: dict,
    require_preview: bool,
) -> JobAlertEmailProcessingState:
    processing_state = db.execute(
        select(JobAlertEmailProcessingState).where(
            JobAlertEmailProcessingState.job_alert_source_id == source.job_alert_source_id,
            JobAlertEmailProcessingState.provider_message_id_hash == message["id_hash"],
        )
    ).scalar_one_or_none()
    if processing_state:
        return processing_state
    if require_preview:
        raise GmailProcessingError(409, "Message must be previewed before import")
    return JobAlertEmailProcessingState.upsert_preview(db, source.job_alert_source_id, message)


def _resolve_messages(
    *,
    settings: Settings | None,
    source: JobAlertSource,
    messages: Sequence[dict] | None,
    max_results: int | None = None,
) -> Sequence[dict]:
    if messages is not None:
        return messages
    if settings is None or settings.gmail_provider == "synthetic":
        return SYNTHETIC_GMAIL_MESSAGES
    if settings.gmail_provider == "google":
        try:
            return GoogleGmailMessageProvider().fetch_messages(
                source=source,
                settings=settings,
                max_results=max_results,
            )
        except GmailProviderConfigurationError as error:
            raise GmailProcessingError(400, str(error)) from error
        except Exception as error:
            processing_error = _live_gmail_exception_to_processing_error(error)
            if processing_error is None:
                raise
            raise processing_error from error
    raise GmailProcessingError(400, f"Unsupported Gmail provider: {settings.gmail_provider}")


def _live_gmail_exception_to_processing_error(error: Exception) -> GmailProcessingError | None:
    provider_stage, provider_error = _gmail_provider_error_context(error)
    if _is_google_auth_error(provider_error):
        return GmailProcessingError(
            502,
            "Gmail provider authentication failed; run `pnpm --silent cli gmail auth` again.",
            provider_diagnostics=_provider_diagnostics(
                error_code="gmail_authentication_failed",
                provider_stage=provider_stage,
                retryable=False,
            ),
        )
    if _is_google_http_error(provider_error):
        status = getattr(getattr(provider_error, "resp", None), "status", None)
        reason, provider_status = _google_http_error_codes(provider_error)
        normalized_reason = (reason or "").lower()
        diagnostics = _provider_diagnostics(
            error_code="gmail_http_error",
            provider_stage=provider_stage,
            retryable=bool(status and status >= 500),
            http_status=status,
            provider_reason=reason,
            provider_status=provider_status,
        )
        if status == 429 or normalized_reason in {
            "dailylimitexceeded",
            "quotaexceeded",
            "ratelimitexceeded",
            "userratelimitexceeded",
        }:
            diagnostics.update(error_code="gmail_rate_limited", retryable=True)
            return GmailProcessingError(
                429,
                "Gmail provider rate limit exceeded; try again later.",
                provider_diagnostics=diagnostics,
            )
        if status == 401:
            diagnostics.update(error_code="gmail_unauthorized", retryable=False)
            return GmailProcessingError(
                502,
                "Gmail provider authorization failed; run `pnpm --silent cli gmail auth` again.",
                provider_diagnostics=diagnostics,
            )
        if status == 403 and normalized_reason in {
            "accessnotconfigured",
            "apidisable",
            "apidisabled",
            "servicedisabled",
        }:
            diagnostics.update(error_code="gmail_api_unavailable", retryable=False)
            return GmailProcessingError(
                502,
                "Gmail API is disabled or unavailable for the configured OAuth project.",
                provider_diagnostics=diagnostics,
            )
        if status == 403 and normalized_reason in {"forbidden", "insufficientpermissions"}:
            diagnostics.update(error_code="gmail_forbidden", retryable=False)
            return GmailProcessingError(
                502,
                "Gmail provider authorization failed; run `pnpm --silent cli gmail auth` again.",
                provider_diagnostics=diagnostics,
            )
        if status == 403:
            diagnostics.update(error_code="gmail_http_403", retryable=False)
            return GmailProcessingError(
                502,
                "Gmail provider request was forbidden.",
                provider_diagnostics=diagnostics,
            )
        if status:
            return GmailProcessingError(
                502,
                f"Gmail provider request failed with status {status}.",
                provider_diagnostics=diagnostics,
            )
        return GmailProcessingError(
            502,
            "Gmail provider request failed.",
            provider_diagnostics=diagnostics,
        )
    if _is_google_api_error(provider_error):
        return GmailProcessingError(
            502,
            "Gmail provider request failed.",
            provider_diagnostics=_provider_diagnostics(
                error_code="gmail_api_error",
                provider_stage=provider_stage,
                retryable=False,
            ),
        )
    if isinstance(provider_error, (OSError, TimeoutError)):
        return GmailProcessingError(
            502,
            "Gmail provider connection failed.",
            provider_diagnostics=_provider_diagnostics(
                error_code="gmail_connection_failed",
                provider_stage=provider_stage,
                retryable=True,
            ),
        )
    return None


_SAFE_PROVIDER_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


def _gmail_provider_error_context(error: Exception) -> tuple[str, Exception]:
    if isinstance(error, GmailProviderOperationError):
        return error.stage, error.cause
    return "provider_request", error


def _google_http_error_codes(error: Exception) -> tuple[str | None, str | None]:
    content = getattr(error, "content", b"")
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        payload = json.loads(content)
    except (UnicodeDecodeError, TypeError, ValueError):
        return None, None
    error_payload = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error_payload, dict):
        return None, None
    reason = None
    candidates = error_payload.get("errors")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and _safe_provider_code(candidate.get("reason")):
                reason = str(candidate["reason"])
                break
    if reason is None:
        details = error_payload.get("details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and _safe_provider_code(detail.get("reason")):
                    reason = str(detail["reason"])
                    break
    provider_status = _safe_provider_code(error_payload.get("status"))
    return reason, provider_status


def _safe_provider_code(value: object) -> str | None:
    candidate = str(value or "").strip()
    return candidate if _SAFE_PROVIDER_CODE.fullmatch(candidate) else None


def _provider_diagnostics(
    *,
    error_code: str,
    provider_stage: str,
    retryable: bool,
    http_status: object = None,
    provider_reason: str | None = None,
    provider_status: str | None = None,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "error_code": error_code,
        "provider_stage": _safe_provider_code(provider_stage) or "provider_request",
        "retryable": retryable,
    }
    if isinstance(http_status, int):
        diagnostics["http_status"] = http_status
    if provider_reason:
        diagnostics["provider_reason"] = provider_reason
    if provider_status:
        diagnostics["provider_status"] = provider_status
    return diagnostics


def _is_google_auth_error(error: Exception) -> bool:
    try:
        from google.auth.exceptions import GoogleAuthError
    except ImportError:
        return False
    return isinstance(error, GoogleAuthError)


def _is_google_http_error(error: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    return isinstance(error, HttpError)


def _is_google_api_error(error: Exception) -> bool:
    try:
        from googleapiclient.errors import Error as GoogleApiError
    except ImportError:
        return False
    return isinstance(error, GoogleApiError)
