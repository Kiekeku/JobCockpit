from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import re
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.inputs import JobIngestPayload as PostingIngestPayload
from jobcockpit.ingestion.ingestion_identity import PostingVerificationRequired, is_verification_url
from jobcockpit.ingestion.concurrency import bounded_call
from jobcockpit.ingestion.ats_adapters.base import (
    AtsJob,
    AtsProviderError,
    SourceArchive,
    normalize_source_url,
)
from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
    oracle_candidate_experience_job_identity,
)
from jobcockpit.ingestion.ats_adapters.phenom_oracle import (
    phenom_oracle_job_identity,
)
from jobcockpit.ingestion.ats_adapters.workday import canonicalize_workday_job_url
from jobcockpit.ingestion.url_hosts import hostname_matches_indeed
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.posting_completeness import (
    IncompletePostingError, incomplete_posting_reason, require_complete_posting,
)
from jobcockpit.ingestion.llm_providers import create_posting_resolver, create_search_provider
from jobcockpit.ingestion.language_filter import (
    evaluate_language_requirements,
    language_filter_error_message,
)
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    CodexCliRenderedPageResolver,
    ExternalPostingHint,
    PlaywrightProfileRenderedPageResolver,
    PrioritizedRenderedPageResolver,
    PostingDiscoveryResolver,
    PostingResolution,
    ResolvedExternalPosting,
    TracedRenderedPageResolver,
    build_external_posting_hint,
    external_aggregator_kind,
    external_source_kind,
    normalize_posting_resolution,
    resolved_external_posting_terminal_status,
    rendered_posting_identity_verdict,
)
from jobcockpit.ingestion.source_resolution import (
    AGGREGATOR_FALLBACK_SOURCE_PATHS,
    OfficialDiscoveryOutcome,
    SourceResolution,
    aggregator_fallback_source_resolution as build_aggregator_fallback_source_resolution,
    direct_posting_source_resolution as build_direct_posting_source_resolution,
    is_aggregator_fallback_source_resolution,
    official_discovery_outcome,
    official_discovery_source_resolution,
    official_discovery_trace_source_resolution,
    official_discovery_outcome_from_source_resolution,
    normalized_official_discovery_reason,
    official_discovery_status_from_reason as official_discovery_status_from_reason,
)
from jobcockpit.ingestion.url_hosts import hostname_matches_domain
from jobcockpit.ingestion.url_policy import UrlFetchPolicy
from jobcockpit.storage.artifacts import ArtifactStorageService


@dataclass(frozen=True)
class PostingLinkClassification:
    classification: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class PostingLinkContext:
    source_type: str
    source_url: str
    raw_text: str
    title_hint: str | None = None
    link_text: str = ""
    surrounding_text: str = ""
    trusted_posting: bool = False
    source_archive: SourceArchive | None = None
    fetch_source_archive: bool = True
    content_source_url: str | None = None
    portal_source_url: str | None = None


@dataclass(frozen=True)
class PostingLinkProcessingResult:
    classification: PostingLinkClassification
    ingestion_status: str
    original_url: str
    resolved_url: str
    canonical_url: str
    linked_job_id: str | None = None
    payload: dict | None = None
    created: bool = False
    error_message: str = ""
    hint: ExternalPostingHint | None = None
    official_discovery_outcome: OfficialDiscoveryOutcome | None = None
    source_resolution: SourceResolution | None = None
    detected_status: str | None = None
    status_evidence_code: str | None = None
    ingest_request: PostingIngestPayload | None = None


SUCCESSFUL_POSTING_LINK_IMPORT_STATUSES = frozenset({"imported", "fallback_imported"})
SKIPPED_POSTING_LINK_INGESTION_STATUSES = frozenset(
    {
        "blocked_by_policy",
        "blocked_by_url_policy",
        "ignored",
        "skipped_by_interest_filter",
        "skipped_by_language_filter",
        "terminal_status_detected",
    }
)
OFFICIAL_DISCOVERY_LINK_STATUSES = frozenset({"needs_external_resolution", "fallback_imported"})


def is_successful_posting_link_import(ingestion_status: str) -> bool:
    return ingestion_status in SUCCESSFUL_POSTING_LINK_IMPORT_STATUSES


def durable_posting_link_error_message(result: PostingLinkProcessingResult) -> str:
    if result.ingestion_status == "imported":
        return ""
    if result.ingestion_status == "terminal_status_detected":
        return (result.status_evidence_code or result.error_message).strip()
    if result.ingestion_status not in OFFICIAL_DISCOVERY_LINK_STATUSES:
        return result.error_message.strip()
    if result.official_discovery_outcome is not None:
        outcome = result.official_discovery_outcome
        return "" if outcome.status == "resolved" else outcome.reason_code
    return "official_discovery_failed"


class JobImportResult(Protocol):
    payload: dict
    created: bool


JobImporter = Callable[
    [PostingIngestPayload, Session, ArtifactStorageService, Settings | None],
    JobImportResult,
]


class PostingResolver(Protocol):
    def resolve(self, hint: ExternalPostingHint) -> object:
        """Resolve an external job-board hint to an official posting candidate."""


class PostingFetcher(Protocol):
    def resolve(self, apply_url: str, context: AtsPostingContext | None = None) -> AtsJob:
        """Fetch a direct posting URL and normalize it to an ATS job snapshot."""


class RenderedPageResolver(Protocol):
    def resolve(self, hint: ExternalPostingHint) -> object:
        """Fetch a rendered posting URL and normalize it to a posting resolution."""


_LINKEDIN_JOB_PATH = re.compile(
    r"^/(?:comm/)?jobs/view/(?:[^/]*-)?(?P<job_id>\d+)/?$",
    re.IGNORECASE,
)

_INDEED_DIRECT_JOB_PATHS = {
    "/viewjob",
    "/rc/clk",
    "/rc/clk/dl",
    "/pagead/clk",
    "/pagead/clk/dl",
}


def canonicalize_indeed_job_url(
    source_url: str,
    *,
    include_search_page: bool = False,
) -> str:
    parsed = urlparse(_strip_url_boundary_punctuation(source_url.strip()))
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    if not hostname_matches_indeed(parsed.hostname):
        return ""
    path = parsed.path.casefold().rstrip("/")
    path_segments = {segment for segment in path.split("/") if segment}
    is_search_page = include_search_page and bool(path_segments & {"job", "jobs"})
    if path not in _INDEED_DIRECT_JOB_PATHS and not is_search_page:
        return ""

    params = parse_qs(parsed.query)
    job_keys = params.get("jk") or params.get("vjk") or []
    job_key = str(job_keys[0]).strip() if job_keys else ""
    if re.fullmatch(r"[a-z0-9]+", job_key, re.IGNORECASE):
        return f"https://ca.indeed.com/viewjob?jk={job_key.casefold()}"
    if path not in {"/pagead/clk", "/pagead/clk/dl"}:
        return ""

    local_identities = params.get("jobcard") or []
    local_identity = str(local_identities[0]).strip() if local_identities else ""
    if re.fullmatch(r"[a-f0-9]{64}", local_identity, re.IGNORECASE):
        return f"https://ca.indeed.com/pagead/clk?jobcard={local_identity.casefold()}"
    ad_tokens = params.get("ad") or []
    ad_token = str(ad_tokens[0]).strip() if ad_tokens else ""
    if not ad_token:
        return ""
    local_identity = hashlib.sha256(ad_token.encode("utf-8")).hexdigest()
    return f"https://ca.indeed.com/pagead/clk?jobcard={local_identity}"


def canonicalize_linkedin_job_url(source_url: str) -> str:
    parsed = urlparse(_strip_url_boundary_punctuation(source_url.strip()))
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    if not hostname_matches_domain(parsed.hostname, "linkedin.com"):
        return ""
    match = _LINKEDIN_JOB_PATH.fullmatch(parsed.path)
    if not match:
        return ""
    return f"https://www.linkedin.com/jobs/view/{match.group('job_id')}"


def canonicalize_posting_url(source_url: str) -> str:
    stripped_url = normalize_source_url(_strip_url_boundary_punctuation(source_url.strip()))
    parsed = urlparse(stripped_url)
    params = parse_qs(parsed.query)
    for key in ("u", "url", "target", "redirect", "sessionRedirect"):
        if params.get(key):
            target_url = unquote(params[key][0])
            if _is_absolute_http_url(target_url):
                return canonicalize_posting_url(target_url)

    if _is_awstrack_host(parsed.hostname):
        awstrack_target = _target_url_from_awstrack_path(parsed.path)
        if awstrack_target:
            return canonicalize_posting_url(awstrack_target)

    linkedin_url = canonicalize_linkedin_job_url(stripped_url)
    if linkedin_url:
        return linkedin_url

    indeed_url = canonicalize_indeed_job_url(stripped_url, include_search_page=True)
    if indeed_url:
        return indeed_url

    workday_url = canonicalize_workday_job_url(stripped_url)
    if workday_url:
        return workday_url

    return stripped_url


def posting_url_identity(source_url: str) -> str:
    canonical_url = canonicalize_posting_url(source_url)
    for identity_resolver in (
        oracle_candidate_experience_job_identity,
        phenom_oracle_job_identity,
    ):
        identity = identity_resolver(canonical_url)
        if identity:
            return identity
    return canonical_url


def classify_posting_link(source_url: str, link_text: str = "") -> PostingLinkClassification:
    haystack = f"{source_url} {link_text}".lower()
    parsed = urlparse(source_url)
    if parsed.hostname == "apply.workable.com" and re.fullmatch(r"/[^/]+/j/[A-Za-z0-9]+/?", parsed.path):
        return PostingLinkClassification(
            classification="probable_job_posting", confidence=0.95,
            reason="workable_job_detail_url",
        )
    if _is_footer_management_link(source_url, link_text):
        return PostingLinkClassification(
            classification="unsubscribe_or_preferences",
            confidence=0.98,
            reason="footer_management_link",
        )
    aggregator_kind = external_aggregator_kind(source_url)
    if aggregator_kind and (
        aggregator_kind not in {"linkedin", "indeed"} or external_source_kind(source_url)
    ):
        return PostingLinkClassification(
            classification="requires_external_resolution",
            confidence=0.9,
            reason="external_job_board_link",
        )
    if _is_contact_or_support_link(source_url, link_text):
        return PostingLinkClassification(
            classification="contact_or_support",
            confidence=0.98,
            reason="contact_or_support_link",
        )
    if any(
        term in haystack
        for term in (
            "instagram.com",
            "linkedin.com/company",
            "facebook.com",
            "twitter.com",
            "x.com/",
            "youtube.com",
            "tiktok.com",
        )
    ):
        return PostingLinkClassification(
            classification="social_or_marketing",
            confidence=0.95,
            reason="social_link",
        )
    if any(
        term in haystack
        for term in ("job", "career", "role", "ats", "greenhouse", "lever", "oraclecloud")
    ):
        return PostingLinkClassification(
            classification="probable_job_posting",
            confidence=0.9,
            reason="job_terms_in_url_or_text",
        )
    return PostingLinkClassification(
        classification="irrelevant", confidence=0.6, reason="no_job_signal"
    )


def process_posting_link(
    context: PostingLinkContext,
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings | None,
    job_importer: JobImporter | None = None,
    posting_resolver: PostingResolver | None = None,
    posting_fetcher: PostingFetcher | None = None,
    posting_resolution: object | None = None,
    language_filter_profile_data: dict | None = None,
    skip_language_filter: bool = False,
) -> PostingLinkProcessingResult:
    settings = settings or Settings()
    prepared = prepare_posting_link(
        context, settings=settings, posting_resolver=posting_resolver,
        posting_fetcher=posting_fetcher, posting_resolution=posting_resolution,
        language_filter_profile_data=language_filter_profile_data,
        skip_language_filter=skip_language_filter,
    )
    return persist_prepared_posting_link(
        prepared, db=db, storage=storage, settings=settings, job_importer=job_importer,
    )


def persist_prepared_posting_link(
    prepared: PostingLinkProcessingResult, *, db: Session,
    storage: ArtifactStorageService, settings: Settings | None,
    job_importer: JobImporter | None = None,
) -> PostingLinkProcessingResult:
    """Persist on the caller's thread; preparation receives no session or storage."""
    if prepared.ingest_request is None:
        return prepared
    if job_importer is None:
        from jobcockpit.services.workflows import persist_job_ingest_result
        job_importer = persist_job_ingest_result
    imported = job_importer(prepared.ingest_request, db, storage, settings)
    payload = dict(imported.payload)
    if prepared.hint is not None and prepared.source_resolution:
        payload["source_resolution"] = prepared.source_resolution
    return replace(prepared, ingest_request=None, linked_job_id=payload["id"],
                   payload=payload, created=imported.created)


def prepare_posting_link(
    context: PostingLinkContext,
    *,
    settings: Settings | None,
    posting_resolver: PostingResolver | None = None,
    posting_fetcher: PostingFetcher | None = None,
    posting_resolution: object | None = None,
    language_filter_profile_data: dict | None = None,
    skip_language_filter: bool = False,
) -> PostingLinkProcessingResult:
    settings = settings or Settings()
    context = _canonical_context(context)
    if posting_resolver is None and posting_resolution is None:
        posting_resolver = default_posting_resolver(settings)

    classification = _classification_for_context(context)
    if incomplete_posting_reason(context.raw_text, context.source_url) == "listing_page":
        classification = PostingLinkClassification(
            classification="requires_external_resolution", confidence=0.95,
            reason="listing_requires_individual_posting",
        )
    if classification.classification == "probable_job_posting":
        return _prepare_direct_posting_link(
            context,
            classification,
            settings=settings,
            posting_fetcher=posting_fetcher,
            language_filter_profile_data=language_filter_profile_data,
            skip_language_filter=skip_language_filter,
        )
    if classification.classification == "requires_external_resolution":
        return _prepare_external_link(
            context,
            classification,
            settings=settings,
            posting_resolver=posting_resolver,
            posting_resolution=posting_resolution,
            language_filter_profile_data=language_filter_profile_data,
            skip_language_filter=skip_language_filter,
        )
    if _is_ignored_link_classification(classification.classification):
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="ignored",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message=classification.reason,
        )
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="ready_for_review",
        original_url=context.source_url,
        resolved_url=context.source_url,
        canonical_url=context.source_url,
    )


def process_posting_links(
    contexts: list[PostingLinkContext],
    *,
    db: Session,
    storage: ArtifactStorageService,
    settings: Settings | None,
    job_importer: JobImporter | None = None,
    posting_resolver: PostingResolver | None = None,
    posting_fetcher: PostingFetcher | None = None,
    external_resolution_concurrency: int | None = None,
    language_filter_profile_data: dict | None = None,
    skip_language_filter: bool = False,
) -> list[PostingLinkProcessingResult]:
    settings = settings or Settings()
    if not contexts:
        return []
    if job_importer is None:
        from jobcockpit.services.workflows import persist_job_ingest_result

        job_importer = persist_job_ingest_result

    canonical_contexts = [_canonical_context(context) for context in contexts]
    concurrency = _external_resolution_concurrency(settings, external_resolution_concurrency)
    default_resolver = None if posting_resolver is not None else default_posting_resolver(settings)
    pre_resolutions: dict[int, object] = {}
    if concurrency > 1 and default_resolver is not None:
        pre_resolutions = _resolve_external_contexts_in_parallel(
            canonical_contexts,
            settings=settings,
            concurrency=concurrency,
        )

    results: list[PostingLinkProcessingResult] = []
    for index, context in enumerate(canonical_contexts):
        resolution = pre_resolutions.get(index)
        results.append(
            process_posting_link(
                context,
                db=db,
                storage=storage,
                settings=settings,
                job_importer=job_importer,
                posting_resolver=posting_resolver or default_resolver,
                posting_fetcher=posting_fetcher,
                posting_resolution=resolution,
                language_filter_profile_data=language_filter_profile_data,
                skip_language_filter=skip_language_filter,
            )
        )
    return results


def default_posting_resolver(settings: Settings) -> PostingResolver | None:
    configured_resolver = create_posting_resolver(settings)
    if configured_resolver is not None:
        attach_rendered_fallback = getattr(configured_resolver, "attach_rendered_fallback", None)
        if callable(attach_rendered_fallback):
            configured_resolver = attach_rendered_fallback(default_rendered_page_resolver(settings))
        return configured_resolver
    if settings.posting_resolver_provider == "disabled":
        return None
    search_client = create_search_provider(settings)
    if search_client is None:
        return None
    from jobcockpit.integrations.official_careers import (
        ReviewedOfficialCareerSearchClient,
    )

    return PostingDiscoveryResolver(
        search_client=search_client,
        official_career_site_search_client=ReviewedOfficialCareerSearchClient(
            search_client=search_client,
            timeout_seconds=settings.serper_timeout_seconds,
        ),
        rendered_fallback_resolver=default_rendered_page_resolver(settings),
        max_results=settings.posting_search_max_results,
    )


def _external_resolution_concurrency(settings: Settings, requested_concurrency: int | None) -> int:
    concurrency = requested_concurrency or settings.posting_external_resolution_concurrency
    if settings.posting_rendered_fallback_provider != "disabled":
        return min(concurrency, settings.posting_rendered_fallback_concurrency)
    return concurrency


def _configured_chrome_cdp_resolver(
    settings: Settings,
    *,
    url_policy: UrlFetchPolicy | None = None,
) -> ChromeCdpRenderedPageResolver:
    url_policy = url_policy or UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls())
    return ChromeCdpRenderedPageResolver(
        endpoint_url=settings.chrome_cdp_endpoint_url,
        auth_wall_wait_seconds=settings.playwright_auth_wall_wait_seconds,
        auth_wall_settle_seconds=settings.playwright_auth_wall_settle_seconds,
        timeout_seconds=settings.posting_rendered_fallback_timeout_seconds,
        retries=settings.posting_rendered_fallback_retries,
        min_start_interval_seconds=settings.posting_rendered_fallback_min_interval_seconds,
        auto_start=settings.chrome_cdp_auto_start,
        executable_path=settings.chrome_cdp_executable_path,
        user_data_dir=settings.chrome_cdp_user_data_dir,
        profile_directory=settings.chrome_cdp_profile_directory,
        headless=settings.playwright_headless,
        url_policy=url_policy,
    )


def default_rendered_page_resolver(
    settings: Settings,
    *,
    url_policy: UrlFetchPolicy | None = None,
    trusted_dom_only: bool = False,
) -> RenderedPageResolver | None:
    url_policy = url_policy or UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls())
    if settings.posting_rendered_fallback_provider == "chrome_cdp_first":
        if trusted_dom_only:
            return _configured_chrome_cdp_resolver(settings, url_policy=url_policy)
        return PrioritizedRenderedPageResolver(
            primary_resolver=_configured_chrome_cdp_resolver(settings, url_policy=url_policy),
            fallback_resolver=CodexCliRenderedPageResolver(
                command=settings.codex_cli_command,
                model=settings.codex_cli_model,
                reasoning_effort=settings.posting_rendered_reasoning_effort,
                timeout_seconds=settings.posting_rendered_fallback_timeout_seconds,
                retries=settings.posting_rendered_fallback_retries,
                min_start_interval_seconds=settings.posting_rendered_fallback_min_interval_seconds,
                workspace=settings.codex_cli_workspace,
                ignore_user_config=settings.codex_cli_rendered_ignore_user_config,
                url_policy=url_policy,
            ),
        )

    if settings.posting_rendered_fallback_provider in {"invisible_playwright", "invisible_playwright_first"}:
        from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver

        primary = InvisiblePlaywrightRenderedPageResolver.from_settings(settings, url_policy=url_policy)
        if settings.posting_rendered_fallback_provider == "invisible_playwright_first":
            return PrioritizedRenderedPageResolver(
                primary_resolver=primary,
                fallback_resolver=_configured_chrome_cdp_resolver(settings, url_policy=url_policy),
                primary_provider="invisible_playwright", fallback_provider="chrome_cdp",
            )
        return TracedRenderedPageResolver(
            inner_resolver=primary,
            provider="invisible_playwright",
        )
    if settings.posting_rendered_fallback_provider == "codex_cli":
        return TracedRenderedPageResolver(
            inner_resolver=CodexCliRenderedPageResolver(
                command=settings.codex_cli_command,
                model=settings.codex_cli_model,
                reasoning_effort=settings.posting_rendered_reasoning_effort,
                timeout_seconds=settings.posting_rendered_fallback_timeout_seconds,
                retries=settings.posting_rendered_fallback_retries,
                min_start_interval_seconds=settings.posting_rendered_fallback_min_interval_seconds,
                workspace=settings.codex_cli_workspace,
                ignore_user_config=settings.codex_cli_rendered_ignore_user_config,
                url_policy=url_policy,
            ),
            provider="codex_rendered",
        )
    if settings.posting_rendered_fallback_provider == "playwright_profile":
        return TracedRenderedPageResolver(
            inner_resolver=PlaywrightProfileRenderedPageResolver(
                user_data_dir=settings.playwright_user_data_dir,
                profile_directory=settings.playwright_profile_directory,
                channel=settings.playwright_channel,
                headless=settings.playwright_headless,
                auth_wall_wait_seconds=settings.playwright_auth_wall_wait_seconds,
                auth_wall_settle_seconds=settings.playwright_auth_wall_settle_seconds,
                timeout_seconds=settings.posting_rendered_fallback_timeout_seconds,
                retries=settings.posting_rendered_fallback_retries,
                min_start_interval_seconds=settings.posting_rendered_fallback_min_interval_seconds,
                url_policy=url_policy,
            ),
            provider="playwright_profile",
        )
    if settings.posting_rendered_fallback_provider == "chrome_cdp":
        return TracedRenderedPageResolver(
            inner_resolver=_configured_chrome_cdp_resolver(settings, url_policy=url_policy),
            provider="chrome_cdp",
        )
    return None


def default_posting_fetcher(settings: Settings) -> PostingFetcher:
    return AtsApplyPageResolver(
        timeout=settings.posting_direct_fetch_timeout_seconds,
        url_policy=UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()),
    )


def _canonical_context(context: PostingLinkContext) -> PostingLinkContext:
    canonical_source_url = canonicalize_posting_url(context.source_url)
    if canonical_source_url == context.source_url:
        return context
    return replace(context, source_url=canonical_source_url)


def _resolve_external_contexts_in_parallel(
    contexts: list[PostingLinkContext],
    *,
    settings: Settings,
    concurrency: int,
) -> dict[int, object]:
    pending_indexes = [
        index
        for index, context in enumerate(contexts)
        if _classification_for_context(context).classification == "requires_external_resolution"
    ]
    if not pending_indexes:
        return {}

    resolutions: dict[int, object] = {}
    with ThreadPoolExecutor(max_workers=min(concurrency, len(pending_indexes))) as executor:
        futures = {
            executor.submit(
                _resolve_external_context_with_new_resolver, contexts[index], settings
            ): index
            for index in pending_indexes
        }
        for future in as_completed(futures):
            resolutions[futures[future]] = future.result()
    return resolutions


def _resolve_external_context_with_new_resolver(
    context: PostingLinkContext, settings: Settings
) -> object:
    resolver = default_posting_resolver(settings)
    if resolver is None:
        return {"status": "needs_review", "error": "posting_resolver_not_configured"}
    hint = build_external_posting_hint(
        source_url=context.source_url,
        link_text=context.link_text,
        surrounding_text=context.surrounding_text,
        raw_text=context.raw_text,
    )
    try:
        return bounded_call("resolve", resolver.resolve, hint)
    except Exception as error:
        return {"status": "needs_review", "error": f"posting_resolver_failed: {error}"}


def _prepare_direct_posting_link(
    context: PostingLinkContext,
    classification: PostingLinkClassification,
    *,
    settings: Settings,
    posting_fetcher: PostingFetcher | None,
    language_filter_profile_data: dict | None,
    skip_language_filter: bool,
    fallback_reason: str | None = None,
) -> PostingLinkProcessingResult:
    policy_result = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
        context.source_url
    )
    if not policy_result.allowed:
        return _blocked_result(context, classification, policy_result.reason)

    board_snapshot = bool(
        context.content_source_url
        and posting_url_identity(context.content_source_url)
        != posting_url_identity(context.source_url)
    )
    try:
        fetched_job = (
            _fetch_direct_posting(context, settings, posting_fetcher)
            if context.fetch_source_archive and (context.source_archive is None or board_snapshot)
            else None
        )
    except AtsProviderError as error:
        policy_block_reason = _url_policy_block_reason(error)
        if policy_block_reason:
            return _blocked_result(context, classification, policy_block_reason)
        if not board_snapshot:
            raise
        fetched_job = None
    if fetched_job and is_verification_url(fetched_job.source_url):
        if not board_snapshot:
            raise PostingVerificationRequired()
        fetched_job = None
    if board_snapshot and fetched_job and (
        not fetched_job.raw_text.strip() or fetched_job.has_posting_body is False
    ):
        fetched_job = None
    ingest_source_url = (
        fetched_job.source_url if fetched_job and fetched_job.source_url else context.source_url
    )
    ingest_raw_text = (
        fetched_job.raw_text if fetched_job and fetched_job.raw_text.strip() else context.raw_text
    )
    title_hint = (
        fetched_job.title if fetched_job and fetched_job.title else context.title_hint
    )
    require_complete_posting(ingest_raw_text, ingest_source_url)
    source_archive = (
        fetched_job.source_archive
        if fetched_job
        else context.source_archive
    )
    portal_source_url = (
        context.portal_source_url
        or context.source_url
    )

    source_resolution = direct_posting_source_resolution(ingest_source_url)
    if (board_snapshot and fetched_job is None) or fallback_reason:
        ingest_source_url = context.content_source_url or context.source_url
        source_resolution = build_aggregator_fallback_source_resolution(
            path="aggregator_structured_fallback",
            reason=fallback_reason or "official_site_request_failed",
            fallback_url=ingest_source_url,
        )
        source_resolution["original_url"] = context.source_url
    elif external_aggregator_kind(ingest_source_url):
        source_resolution = {
            "kind": "aggregator_fallback",
            "path": "aggregator_structured_fallback",
            "official_discovery_status": "not_attempted",
            "reason_code": "source_discovery_pending",
            "fallback_url": ingest_source_url,
            "original_url": context.source_url,
        }
    language_skip_result = _language_filter_skip_result(
        context,
        classification,
        ingest_raw_text,
        ingest_source_url,
        language_filter_profile_data,
        skip_language_filter,
    )
    if language_skip_result:
        return language_skip_result

    ingest_request = PostingIngestPayload(
        source_type=context.source_type,
        raw_text=ingest_raw_text,
        title_hint=title_hint,
        source_url=ingest_source_url,
        source_archive=source_archive,
        portal_source_url=portal_source_url,
        source_resolution=source_resolution,
        skip_language_filter=skip_language_filter,
    )
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="imported",
        original_url=context.source_url,
        resolved_url=ingest_source_url,
        canonical_url=ingest_source_url,
        source_resolution=source_resolution,
        ingest_request=ingest_request,
    )


def _fetch_direct_posting(
    context: PostingLinkContext,
    settings: Settings,
    posting_fetcher: PostingFetcher | None,
) -> AtsJob | None:
    owned_fetcher = posting_fetcher is None
    fetcher = posting_fetcher or default_posting_fetcher(settings)
    try:
        job = bounded_call(
            "fetch", fetcher.resolve,
            context.source_url, context=_posting_context_for_direct_link(context)
        )
        require_complete_posting(job.observed_raw_text or job.raw_text, job.source_url)
        return job
    except AtsProviderError as error:
        if isinstance(error, PostingVerificationRequired):
            raise
        if _url_policy_block_reason(error):
            raise
        rendered_job = _fetch_rendered_direct_posting(context, settings)
        if isinstance(error, IncompletePostingError) and (
            rendered_job is None or not rendered_job.raw_text.strip()
        ):
            raise
        return rendered_job
    finally:
        if owned_fetcher:
            close = getattr(fetcher, "close", None)
            if callable(close):
                close()


def _fetch_rendered_direct_posting(
    context: PostingLinkContext, settings: Settings
) -> AtsJob | None:
    rendered_resolver = default_rendered_page_resolver(settings)
    if rendered_resolver is None:
        return None
    hint = build_external_posting_hint(
        source_url=context.source_url,
        link_text=context.link_text,
        surrounding_text=context.surrounding_text,
        raw_text=context.raw_text,
    )
    resolution = normalize_posting_resolution(bounded_call("resolve", rendered_resolver.resolve, hint))
    if resolution.error in {"playwright_low_confidence", "chrome_cdp_low_confidence"}:
        # A direct posting URL is stronger evidence than old preview-derived
        # title/location hints. Never extend this exception to discovered URLs.
        exact_hint = replace(hint, url_identity="path_and_query")
        for candidate in resolution.candidates:
            if (candidate.evidence_origin == "browser_dom"
                    and not candidate.content_truncated
                    and len((candidate.complete_body_text or "").split()) >= 80
                    and rendered_posting_identity_verdict(candidate, exact_hint).value == "match"):
                require_complete_posting(candidate.raw_text, candidate.source_url)
                resolution = replace(resolution, status="resolved", posting=candidate)
                break
    if resolution.status != "resolved" or resolution.posting is None:
        return None
    policy_result = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
        resolution.posting.source_url
    )
    if not policy_result.allowed:
        return None
    require_complete_posting(resolution.posting.raw_text, resolution.posting.source_url)
    return AtsJob(
        source_url=resolution.posting.source_url,
        raw_text=_raw_text_with_rendered_evidence(resolution),
        source_archive=resolution.posting.source_archive,
    )


def _posting_context_for_direct_link(context: PostingLinkContext) -> AtsPostingContext:
    hint = build_external_posting_hint(
        source_url=context.source_url,
        link_text=context.link_text,
        surrounding_text=context.surrounding_text,
        raw_text=context.raw_text,
    )
    return AtsPostingContext(
        source_label=context.source_type.replace("_", " ").title(),
        source_url=context.source_url,
        company=hint.company,
        role=hint.title,
        location=hint.location,
        apply_url=context.source_url,
    )


def _prepare_external_link(
    context: PostingLinkContext,
    classification: PostingLinkClassification,
    *,
    settings: Settings,
    posting_resolver: PostingResolver | None,
    posting_resolution: object | None,
    language_filter_profile_data: dict | None,
    skip_language_filter: bool,
) -> PostingLinkProcessingResult:
    if not posting_resolver and posting_resolution is None and context.trusted_posting:
        posting_resolution = PostingResolution(
            status="needs_review", error="posting_resolver_not_configured",
        )
    if not posting_resolver and posting_resolution is None:
        outcome = official_discovery_outcome("posting_resolver_not_configured")
        source_resolution = official_discovery_trace_source_resolution(outcome=outcome)
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="needs_external_resolution",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message=outcome.reason_code,
            official_discovery_outcome=outcome,
            source_resolution=source_resolution,
        )

    hint = build_external_posting_hint(
        source_url=context.source_url,
        link_text=context.link_text,
        surrounding_text=context.surrounding_text,
        raw_text=context.raw_text,
    )
    if posting_resolution is None:
        if posting_resolver is None:
            outcome = official_discovery_outcome("posting_resolver_not_configured")
            source_resolution = official_discovery_trace_source_resolution(outcome=outcome)
            return PostingLinkProcessingResult(
                classification=classification,
                ingestion_status="needs_external_resolution",
                original_url=context.source_url,
                resolved_url=context.source_url,
                canonical_url=context.source_url,
                error_message=outcome.reason_code,
                hint=hint,
                official_discovery_outcome=outcome,
                source_resolution=source_resolution,
            )
        try:
            posting_resolution = bounded_call("resolve", posting_resolver.resolve, hint)
        except Exception as error:
            if not context.trusted_posting:
                raise
            if isinstance(error, AtsProviderError) and _url_policy_block_reason(error):
                return _blocked_result(context, classification, _url_policy_block_reason(error))
            posting_resolution = PostingResolution(
                status="needs_review", error="official_discovery_failed",
            )
    resolution = normalize_posting_resolution(posting_resolution)
    if resolution.status != "resolved" or not resolution.posting:
        outcome = official_discovery_outcome_for_resolution(resolution)
        if context.trusted_posting and context.raw_text.strip():
            return _prepare_direct_posting_link(
                replace(context, fetch_source_archive=False), classification,
                settings=settings,
                posting_fetcher=None,
                language_filter_profile_data=language_filter_profile_data,
                skip_language_filter=skip_language_filter,
                fallback_reason=outcome.reason_code,
            )
        source_resolution = source_resolution_trace_for_resolution(resolution)
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="needs_external_resolution",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message=outcome.reason_code,
            hint=hint,
            official_discovery_outcome=outcome,
            source_resolution=source_resolution,
        )

    policy_result = UrlFetchPolicy(allowed_localhosts=settings.allowed_localhost_urls()).check(
        resolution.posting.source_url
    )
    if not policy_result.allowed:
        return replace(
            _blocked_result(context, classification, policy_result.reason, hint=hint),
            official_discovery_outcome=official_discovery_outcome_for_resolution(resolution),
            source_resolution=source_resolution_trace_for_resolution(resolution),
        )

    terminal_status = resolved_external_posting_terminal_status(resolution.posting)
    if terminal_status is not None:
        return PostingLinkProcessingResult(
            classification=classification,
            ingestion_status="terminal_status_detected",
            original_url=context.source_url,
            resolved_url=resolution.posting.source_url,
            canonical_url=resolution.posting.source_url,
            hint=hint,
            source_resolution=source_resolution_trace_for_resolution(resolution),
            detected_status=terminal_status.status,
            status_evidence_code=terminal_status.evidence_code,
        )

    resolution_metadata = source_resolution_for_resolution(context.source_url, resolution)
    require_complete_posting(resolution.posting.raw_text, resolution.posting.source_url)
    outcome = official_discovery_outcome_from_source_resolution(resolution_metadata)
    ingest_raw_text = raw_text_with_resolution_evidence(
        resolution.posting,
        hint,
        source_resolution=resolution_metadata,
    )
    language_skip_result = _language_filter_skip_result(
        context,
        classification,
        ingest_raw_text,
        resolution.posting.source_url,
        language_filter_profile_data,
        skip_language_filter,
        hint=hint,
    )
    if language_skip_result:
        return replace(
            language_skip_result,
            official_discovery_outcome=outcome,
            source_resolution=resolution_metadata,
        )

    ingest_request = PostingIngestPayload(
        source_type=context.source_type,
        raw_text=ingest_raw_text,
        title_hint=context.title_hint or hint.title,
        source_url=resolution.posting.source_url,
        source_archive=resolution.posting.source_archive,
        source_resolution=resolution_metadata,
        skip_language_filter=skip_language_filter,
        portal_source_url=context.portal_source_url or context.source_url,
    )
    fallback_import = is_aggregator_fallback_source_resolution(resolution_metadata)
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="fallback_imported" if fallback_import else "imported",
        original_url=context.source_url,
        resolved_url=resolution.posting.source_url,
        canonical_url=resolution.posting.source_url,
        source_resolution=resolution_metadata,
        ingest_request=ingest_request,
        error_message=outcome.reason_code if fallback_import and outcome is not None else "",
        official_discovery_outcome=outcome,
        hint=hint,
    )


def is_same_aggregator_fallback_resolution(source_url: str, resolution: PostingResolution) -> bool:
    if resolution.status != "resolved" or resolution.posting is None:
        return False
    source_kind = external_aggregator_kind(source_url)
    posting_kind = external_aggregator_kind(resolution.posting.source_url)
    if not source_kind or source_kind != posting_kind:
        return False
    if not resolution.source_path and not resolution.error and not resolution.reason_code:
        return True
    has_fallback_state = resolution.source_path in AGGREGATOR_FALLBACK_SOURCE_PATHS
    has_legacy_fallback_error = bool(resolution.error) and not resolution.source_path
    if not has_fallback_state and not has_legacy_fallback_error:
        return False
    return True


def aggregator_fallback_source_resolution(
    source_url: str, resolution: PostingResolution
) -> SourceResolution | None:
    if (resolution.status == "resolved" and resolution.posting
            and resolution.source_path in AGGREGATOR_FALLBACK_SOURCE_PATHS
            and external_source_kind(source_url) == "indeed"
            and external_source_kind(resolution.posting.source_url) == "linkedin"):
        return {
            **source_resolution_metadata_for_resolution(resolution),
            "original_url": source_url,
        }
    if not is_same_aggregator_fallback_resolution(source_url, resolution):
        return None
    return source_resolution_metadata_for_resolution(resolution)


def source_resolution_metadata_for_resolution(resolution: PostingResolution) -> SourceResolution:
    outcome = (
        official_discovery_outcome("official_posting_not_found")
        if resolution.official_search_outcome is None
        and not resolution.official_discovery_status
        and not resolution.reason_code
        and not resolution.error
        else official_discovery_outcome_for_resolution(resolution)
    )
    return build_aggregator_fallback_source_resolution(
        path=resolution.source_path or "aggregator_structured_fallback",
        reason=outcome.reason_code,
        fallback_url=resolution.posting.source_url if resolution.posting else None,
        official_discovery_status=outcome.status,
        outcome=outcome,
        attempts=tuple(attempt.to_dict() for attempt in resolution.attempts),
        trace_truncated=resolution.trace_truncated,
    )


def source_resolution_for_resolution(
    source_url: str, resolution: PostingResolution
) -> SourceResolution | None:
    fallback_metadata = aggregator_fallback_source_resolution(source_url, resolution)
    if fallback_metadata:
        return fallback_metadata
    if (
        resolution.status == "resolved"
        and resolution.posting
        and external_aggregator_kind(source_url)
        and not external_aggregator_kind(resolution.posting.source_url)
    ):
        return official_discovery_source_resolution(
            resolution.posting.source_url,
            outcome=resolution.official_search_outcome
            or official_discovery_outcome(
                "official_posting_resolved",
                status="resolved",
            ),
            attempts=tuple(attempt.to_dict() for attempt in resolution.attempts),
            trace_truncated=resolution.trace_truncated,
        )
    return None


def direct_posting_source_resolution(source_url: str) -> SourceResolution | None:
    if not source_url or external_aggregator_kind(source_url):
        return None
    return build_direct_posting_source_resolution(source_url)


def _classification_for_context(context: PostingLinkContext) -> PostingLinkClassification:
    classification = classify_posting_link(context.source_url, context.link_text)
    if not context.trusted_posting:
        return classification
    if classification.classification == "irrelevant":
        return PostingLinkClassification(
            classification="probable_job_posting",
            confidence=1.0,
            reason="trusted_posting_source",
        )
    if (
        classification.classification == "requires_external_resolution"
        and not context.fetch_source_archive
        and context.raw_text.strip()
    ):
        return PostingLinkClassification(
            classification="probable_job_posting",
            confidence=1.0,
            reason="trusted_posting_snapshot",
        )
    return classification


def _is_ignored_link_classification(classification: str) -> bool:
    return classification in {
        "unsubscribe_or_preferences",
        "contact_or_support",
        "social_or_marketing",
        "irrelevant",
    }


def _is_footer_management_link(source_url: str, link_text: str) -> bool:
    parsed = urlparse(source_url.strip())
    path_segments = {unquote(segment).casefold() for segment in parsed.path.split("/") if segment}
    text = link_text.casefold()
    query_params = parse_qs(parsed.query)
    query_haystack = " ".join(
        [key for key in query_params]
        + [item for values in query_params.values() for item in values]
    ).casefold()
    exact_path_terms = {
        "unsubscribe",
        "preferences",
        "privacy",
        "privacy-policy",
        "terms",
        "terms-of-service",
        "terms-and-conditions",
    }
    text_phrases = (
        "unsubscribe",
        "manage preferences",
        "email preferences",
        "privacy policy",
        "terms of service",
        "terms and conditions",
        "view in browser",
        "view online",
    )
    return (
        any(segment in exact_path_terms for segment in path_segments)
        or any(phrase in text for phrase in text_phrases)
        or any(
            term in query_haystack
            for term in (
                "unsubscribe",
                "email_preferences",
                "manage_preferences",
                "manage-preferences",
                "optout",
                "opt_out",
            )
        )
    )


def _is_contact_or_support_link(source_url: str, link_text: str) -> bool:
    parsed = urlparse(source_url.strip())
    if parsed.scheme.casefold() == "mailto":
        return True

    haystack = " ".join([parsed.hostname or "", parsed.path, link_text]).casefold()
    has_contact_signal = any(term in haystack for term in ("contact", "support", "help"))
    has_job_signal = any(
        term in haystack for term in ("job", "jobs", "career", "careers", "role", "roles")
    )
    return has_contact_signal and not has_job_signal


def raw_text_with_resolution_evidence(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
    *,
    source_resolution: SourceResolution | None = None,
) -> str:
    context_lines = _trusted_hint_lines_for_fallback(posting, hint, source_resolution)
    context_lines.append(posting.raw_text)
    if hint.location and not _raw_text_has_field("\n".join(context_lines), "Location"):
        context_lines.append(f"Location: {hint.location}")
    context_lines.append(f"{hint.source_kind.title()} Source URL: {hint.source_url}")
    if source_resolution:
        context_lines.append(f"Source Resolution Kind: {source_resolution.get('kind', 'unknown')}")
        discovery_status = source_resolution.get("official_discovery_status")
        if discovery_status:
            context_lines.append(f"Official Discovery Status: {discovery_status}")
        discovery_error = source_resolution.get("official_discovery_error")
        if discovery_error:
            context_lines.append(f"Official Discovery Error: {discovery_error}")
    if posting.evidence:
        context_lines.append(f"Resolution Evidence: {posting.evidence}")
    return "\n".join(line for line in context_lines if line).strip()


def _trusted_hint_lines_for_fallback(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
    source_resolution: SourceResolution | None,
) -> list[str]:
    if not is_aggregator_fallback_source_resolution(source_resolution):
        return []
    lines = []
    if hint.title:
        lines.append(f"Title: {hint.title}")
    if hint.company:
        lines.append(f"Company: {hint.company}")
    return lines


def _raw_text_with_rendered_evidence(resolution: PostingResolution) -> str:
    if resolution.posting is None:
        return ""
    context_lines = [resolution.posting.raw_text]
    if resolution.posting.evidence:
        context_lines.append(f"Resolution Evidence: {resolution.posting.evidence}")
    return "\n".join(line for line in context_lines if line).strip()


def _resolution_error_message(resolution: PostingResolution) -> str:
    return official_discovery_outcome_for_resolution(resolution).reason_code


def official_discovery_outcome_for_resolution(
    resolution: PostingResolution,
) -> OfficialDiscoveryOutcome:
    if resolution.official_search_outcome is not None:
        return resolution.official_search_outcome
    status = resolution.official_discovery_status
    if (
        resolution.status != "resolved"
        and normalized_official_discovery_reason(status) == "resolved"
    ):
        status = ""
    reason = resolution.reason_code or resolution.error or resolution.status
    return official_discovery_outcome(
        reason,
        status=status,
    )


def source_resolution_trace_for_resolution(
    resolution: PostingResolution,
) -> SourceResolution:
    return official_discovery_trace_source_resolution(
        outcome=official_discovery_outcome_for_resolution(resolution),
        attempts=tuple(attempt.to_dict() for attempt in resolution.attempts),
        trace_truncated=resolution.trace_truncated,
    )


def _language_filter_skip_result(
    context: PostingLinkContext,
    classification: PostingLinkClassification,
    raw_text: str,
    resolved_url: str,
    profile_data: dict | None,
    skip_language_filter: bool,
    *,
    hint: ExternalPostingHint | None = None,
) -> PostingLinkProcessingResult | None:
    result = evaluate_language_requirements(
        raw_text,
        profile_data,
        skip_language_filter=skip_language_filter,
    )
    if not result.should_skip:
        return None
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="skipped_by_language_filter",
        original_url=context.source_url,
        resolved_url=resolved_url,
        canonical_url=resolved_url,
        error_message=language_filter_error_message(result),
        payload={
            "language_filter": {
                "reason": result.reason,
                "required_languages": result.required_languages,
                "unmet_languages": result.unmet_languages,
                "evidence": result.evidence,
            }
        },
        hint=hint,
    )


def _blocked_result(
    context: PostingLinkContext,
    classification: PostingLinkClassification,
    reason: str,
    *,
    hint: ExternalPostingHint | None = None,
) -> PostingLinkProcessingResult:
    return PostingLinkProcessingResult(
        classification=classification,
        ingestion_status="blocked_by_url_policy",
        original_url=context.source_url,
        resolved_url=context.source_url,
        canonical_url=context.source_url,
        error_message=reason,
        hint=hint,
    )


def _url_policy_block_reason(error: AtsProviderError) -> str:
    marker = "URL is blocked by policy:"
    if error.status_code != 400 or marker not in error.detail:
        return ""
    return error.detail.split(marker, 1)[1].strip()


def _is_awstrack_host(hostname: str | None) -> bool:
    return hostname_matches_domain(hostname, "awstrack.me")


def _target_url_from_awstrack_path(path: str) -> str:
    for segment in (part for part in path.split("/") if part):
        decoded = unquote(segment)
        if _is_absolute_http_url(decoded):
            return decoded
    return ""


def _is_absolute_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _strip_url_boundary_punctuation(source_url: str) -> str:
    return source_url.rstrip(").,;]}>\"'")


def _raw_text_has_field(raw_text: str, label: str) -> bool:
    prefix = f"{label}:".casefold()
    return any(line.strip().casefold().startswith(prefix) for line in raw_text.splitlines())
