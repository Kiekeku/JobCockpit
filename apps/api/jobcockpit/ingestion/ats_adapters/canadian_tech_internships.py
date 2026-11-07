from __future__ import annotations

import re
from dataclasses import dataclass, replace
from html import unescape
from typing import Literal
from urllib.parse import unquote, urlparse

import httpx

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsLinkFailure,
    AtsProviderError,
    AtsRecordedLinkFailureError,
    classify_link_failure,
    normalize_source_url,
)
from jobcockpit.ingestion.ats_adapters.workday import canonicalize_workday_job_url
from jobcockpit.ingestion.ats_helpers import clean_text, enforce_url_policy, request_headers
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.posting_completeness import IncompletePostingError, require_complete_posting
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    CodexCliRenderedPageResolver,
    ExternalPostingHint,
    PrioritizedRenderedPageResolver,
    RenderedPostingResolver,
    RenderedStatusSignal,
    ResolvedExternalPosting,
    normalize_posting_resolution,
    observe_rendered_posting,
    rendered_query_identity_groups,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy

DEFAULT_REF = "main"
DEFAULT_README_PATH = "README.md"
GITHUB_RAW_ORIGIN = "https://raw.githubusercontent.com"
WORKDAY_EXPIRED_PAGE_MARKERS = (
    "page you are looking for doesn't exist",
    "page you are looking for does not exist",
    "job is no longer available",
    "job posting is no longer available",
    "position is no longer available",
)
WORKDAY_TEMPORARY_PAGE_MARKERS = (
    "workday is currently unavailable",
    "service interruption",
    "service will be restored",
)


@dataclass(frozen=True)
class CanadianTechInternshipsReadme:
    owner: str
    repo: str
    ref: str
    path: str

    @property
    def raw_url(self) -> str:
        return f"{GITHUB_RAW_ORIGIN}/{self.owner}/{self.repo}/{self.ref}/{self.path}"

    @property
    def github_url(self) -> str:
        repository_url = f"https://github.com/{self.owner}/{self.repo}"
        if self.ref == DEFAULT_REF and self.path == DEFAULT_README_PATH:
            return repository_url
        return f"{repository_url}/blob/{self.ref}/{self.path}"


@dataclass(frozen=True)
class InternshipListing:
    company: str
    role: str
    location: str
    apply_url: str
    date_posted: str
    readme_url: str


class GitHubInternshipCollectionAdapter:
    provider: str
    display_name: str
    supported_owner: str
    supported_repo: str
    apply_column_names = ("Apply",)
    source_label = "GitHub List"

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        url_policy: UrlFetchPolicy | None = None,
        rendered_page_resolver: RenderedPostingResolver | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self.url_policy = url_policy or UrlFetchPolicy()
        self.rendered_page_resolver = rendered_page_resolver
        if settings is not None:
            self.configure_settings(settings)

    def configure_settings(self, settings: Settings) -> None:
        if self.rendered_page_resolver is None:
            self.rendered_page_resolver = configured_cdp_resolver(
                settings,
                self.url_policy,
            )

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        self._parse_source_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        readme = self._parse_source_url(request.source_url)
        readme_text = self._get_readme(readme.raw_url)
        listings = _matching_listings(
            readme_text,
            readme.github_url,
            request.search_text,
            apply_column_names=self.apply_column_names,
        )
        if not listings:
            raise AtsProviderError(
                502, f"{self.display_name} README did not include matching open job links"
            )

        def candidates():
            for listing in listings:
                if request.excludes_source_url(listing.apply_url):
                    continue
                if not request.allows_listing_candidate(listing.apply_url, listing.role):
                    continue
                yield listing.apply_url, lambda listing=listing: self._job_from_listing(listing)

        return fetch_details(request, candidates())


    def _parse_source_url(self, source_url: str) -> CanadianTechInternshipsReadme:
        return _parse_github_internships_url(
            source_url,
            supported_owner=self.supported_owner,
            supported_repo=self.supported_repo,
        )

    def _get_readme(self, raw_url: str) -> str:
        enforce_url_policy(self.url_policy, raw_url, label="GitHub README")
        try:
            response = self.http_client.get(
                raw_url, headers=request_headers("text/plain"), follow_redirects=False
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AtsProviderError(502, f"{self.display_name} README request failed") from error
        return response.text

    def _job_from_listing(self, listing: InternshipListing) -> AtsJob:
        context = AtsPostingContext(
            source_label=self.source_label,
            source_url=listing.readme_url,
            company=listing.company,
            role=listing.role,
            location=listing.location,
            date_posted=listing.date_posted,
            apply_url=listing.apply_url,
        )
        try:
            job = AtsApplyPageResolver(
                http_client=self.http_client,
                url_policy=self.url_policy,
            ).resolve(listing.apply_url, context)
        except AtsProviderError as error:
            if not self.rendered_page_resolver or not (
                isinstance(error, IncompletePostingError)
                or _is_apply_page_forbidden(listing.apply_url, error)
            ):
                raise
            job = self._job_from_rendered_listing(listing)
        return replace(job, portal_source_url=listing.apply_url)

    def _job_from_rendered_listing(self, listing: InternshipListing) -> AtsJob:
        is_workday = bool(canonicalize_workday_job_url(listing.apply_url))
        identity_query_groups = (
            () if is_workday else rendered_query_identity_groups(listing.apply_url)
        )
        hint = ExternalPostingHint(
            source_url=listing.apply_url,
            source_kind="workday" if is_workday else "generic",
            company=listing.company,
            title=listing.role,
            location=listing.location,
            link_text=listing.apply_url,
            evidence_text=f"{listing.company} {listing.role} {listing.location}",
            url_identity=("path_and_query" if identity_query_groups else "path"),
            identity_query_groups=identity_query_groups,
        )
        try:
            resolution = normalize_posting_resolution(self.rendered_page_resolver.resolve(hint))
        except Exception as error:
            raise _rendered_apply_failure(
                listing.apply_url,
                "retryable",
                f"Rendered apply-page fallback failed: {str(error) or error.__class__.__name__}",
            ) from error

        rendered_candidates = tuple(
            candidate
            for candidate in (resolution.posting, *resolution.candidates)
            if candidate is not None
        )
        workday_observations = (
            tuple(
                observe_rendered_posting(
                    candidate,
                    hint,
                    status_classifier=_workday_rendered_status_signal,
                )
                for candidate in rendered_candidates
            )
            if is_workday
            else ()
        )
        if any(
            observation.permits_status_action
            and observation.status is RenderedStatusSignal.CLOSED
            for observation in workday_observations
        ):
            raise _rendered_apply_failure(
                listing.apply_url,
                "permanent",
                "Workday posting expired or was removed",
                upstream_status_code=410,
            )
        if any(
            observation.has_trusted_status_signal
            and observation.status is RenderedStatusSignal.TEMPORARY
            for observation in workday_observations
        ):
            raise _rendered_apply_failure(
                listing.apply_url,
                "retryable",
                "Workday rendered page is temporarily unavailable",
                upstream_status_code=503,
            )
        if resolution.status != "resolved" or resolution.posting is None:
            reason = resolution.error or "Rendered fallback did not resolve the posting"
            raise _rendered_apply_failure(
                listing.apply_url,
                _rendered_apply_failure_category(reason),
                reason,
            )
        require_complete_posting(resolution.posting.raw_text, resolution.posting.source_url)
        return _job_from_rendered_posting(listing, resolution.posting, self.source_label)


class CanadianTechInternshipsAdapter(GitHubInternshipCollectionAdapter):
    provider = "canadian_tech_internships"
    display_name = "Canadian Tech Internships"
    supported_owner = "negarprh"
    supported_repo = "Canadian-Tech-Internships-2026"

    def _parse_source_url(self, source_url: str) -> CanadianTechInternshipsReadme:
        return parse_canadian_tech_internships_url(source_url)


def parse_canadian_tech_internships_url(source_url: str) -> CanadianTechInternshipsReadme:
    readme = _parse_github_internships_url(
        source_url,
        supported_owner=CanadianTechInternshipsAdapter.supported_owner,
        supported_repo=CanadianTechInternshipsAdapter.supported_repo,
    )
    if readme.path != DEFAULT_README_PATH:
        raise AtsProviderError(
            400,
            f"Only {CanadianTechInternshipsAdapter.supported_owner}/"
            f"{CanadianTechInternshipsAdapter.supported_repo}/{DEFAULT_README_PATH} is supported",
        )
    return readme


def _parse_github_internships_url(
    source_url: str,
    *,
    supported_owner: str,
    supported_repo: str,
) -> CanadianTechInternshipsReadme:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]

    if parsed.scheme not in {"http", "https"}:
        raise _unsupported_url_error(supported_owner, supported_repo)

    if hostname == "github.com":
        if (
            len(path_parts) < 2
            or path_parts[0] != supported_owner
            or path_parts[1] != supported_repo
        ):
            raise _unsupported_url_error(supported_owner, supported_repo)
        if len(path_parts) >= 5 and path_parts[2] == "blob":
            return CanadianTechInternshipsReadme(
                path_parts[0], path_parts[1], path_parts[3], "/".join(path_parts[4:])
            )
        return CanadianTechInternshipsReadme(
            supported_owner, supported_repo, DEFAULT_REF, DEFAULT_README_PATH
        )

    if hostname == "raw.githubusercontent.com":
        if (
            len(path_parts) < 4
            or path_parts[0] != supported_owner
            or path_parts[1] != supported_repo
        ):
            raise _unsupported_url_error(supported_owner, supported_repo)
        return CanadianTechInternshipsReadme(
            path_parts[0], path_parts[1], path_parts[2], "/".join(path_parts[3:])
        )

    raise _unsupported_url_error(supported_owner, supported_repo)


def _unsupported_url_error(owner: str, repo: str) -> AtsProviderError:
    return AtsProviderError(400, f"Only the {owner}/{repo} GitHub README is supported")


def configured_cdp_resolver(
    settings: Settings,
    url_policy: UrlFetchPolicy,
) -> RenderedPostingResolver | None:
    if settings.posting_rendered_fallback_provider in {"invisible_playwright", "invisible_playwright_first"}:
        from jobcockpit.ingestion.posting_links import default_rendered_page_resolver

        return default_rendered_page_resolver(settings, url_policy=url_policy)
    if settings.posting_rendered_fallback_provider not in {
        "chrome_cdp",
        "chrome_cdp_first",
    }:
        return None
    cdp_resolver = ChromeCdpRenderedPageResolver(
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
    if settings.posting_rendered_fallback_provider == "chrome_cdp":
        return cdp_resolver
    return PrioritizedRenderedPageResolver(
        primary_resolver=cdp_resolver,
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


def _is_apply_page_forbidden(source_url: str, error: AtsProviderError) -> bool:
    return classify_link_failure(source_url, error).upstream_status_code == 403


def _job_from_rendered_posting(
    listing: InternshipListing,
    posting: ResolvedExternalPosting,
    source_label: str,
) -> AtsJob:
    context_text = "\n".join(
        (
            f"{source_label} Company: {listing.company}",
            f"{source_label} Role: {listing.role}",
            f"{source_label} Location: {listing.location}",
            f"{source_label} Date Posted: {listing.date_posted}",
            f"{source_label} URL: {listing.readme_url}",
            f"{source_label} Apply URL: {listing.apply_url}",
        )
    )
    return AtsJob(
        source_url=posting.source_url or listing.apply_url,
        raw_text=f"{posting.raw_text.strip()}\n{context_text}".strip(),
        source_archive=posting.source_archive,
    )


def _rendered_apply_failure(
    source_url: str,
    category: Literal["permanent", "retryable", "technical"],
    reason: str,
    *,
    upstream_status_code: int | None = None,
) -> AtsRecordedLinkFailureError:
    return AtsRecordedLinkFailureError(
        AtsLinkFailure(
            source_url=source_url,
            category=category,
            reason=reason,
            upstream_status_code=upstream_status_code,
        )
    )


def _workday_rendered_status_signal(raw_text: str) -> RenderedStatusSignal:
    if _is_expired_workday_page(raw_text):
        return RenderedStatusSignal.CLOSED
    if _is_temporary_workday_page(raw_text):
        return RenderedStatusSignal.TEMPORARY
    return RenderedStatusSignal.UNKNOWN


def _is_expired_workday_page(raw_text: str) -> bool:
    normalized = raw_text.casefold().replace("’", "'")
    return any(marker in normalized for marker in WORKDAY_EXPIRED_PAGE_MARKERS)


def _is_temporary_workday_page(raw_text: str) -> bool:
    normalized = raw_text.casefold()
    return any(marker in normalized for marker in WORKDAY_TEMPORARY_PAGE_MARKERS)


def _rendered_apply_failure_category(
    reason: str,
) -> Literal["retryable", "technical"]:
    normalized = reason.casefold()
    retryable_markers = (
        "chrome_cdp_timeout",
        "chrome_cdp_failed:",
        "codex_playwright_timeout",
        "codex_playwright_failed:",
    )
    if any(marker in normalized for marker in retryable_markers):
        return "retryable"
    return "technical"


def _matching_listings(
    markdown: str,
    readme_url: str,
    search_text: str,
    *,
    apply_column_names: tuple[str, ...] = ("Apply",),
) -> list[InternshipListing]:
    terms = [term for term in search_text.lower().split() if term]
    listings: list[InternshipListing] = []
    current_company = ""
    for cells in _internship_table_rows(markdown, apply_column_names=apply_column_names):
        company = _clean_markdown_text(cells[0])
        role = _clean_markdown_text(cells[1])
        location = _clean_markdown_text(cells[2])
        apply_url = _apply_url(cells[3])
        date_posted = _clean_markdown_text(cells[4])
        if company == "\u21b3":
            company = current_company
        elif company:
            current_company = company
        if not apply_url:
            continue
        listing = InternshipListing(
            company=company,
            role=role,
            location=location,
            apply_url=apply_url,
            date_posted=date_posted,
            readme_url=readme_url,
        )
        if _listing_matches(listing, terms):
            listings.append(listing)
    return listings


def _internship_table_rows(
    markdown: str, *, apply_column_names: tuple[str, ...]
) -> list[list[str]]:
    rows: list[list[str]] = []
    in_table = False
    normalized_apply_columns = {name.lower() for name in apply_column_names}
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!-- END:INTERNSHIPS_TABLE"):
            break
        if not stripped.startswith("|"):
            if in_table and rows:
                break
            continue
        cells = _split_markdown_table_row(stripped)
        normalized_header = [_clean_markdown_text(cell).lower() for cell in cells[:5]]
        if (
            len(normalized_header) == 5
            and normalized_header[:3] == ["company", "role", "location"]
            and normalized_header[3] in normalized_apply_columns
            and normalized_header[4] == "date posted"
        ):
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) < 5 or set(cells[0].strip()) <= {"-"}:
            continue
        rows.append(cells[:5])
    return rows


def _split_markdown_table_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    bracket_depth = 0
    paren_depth = 0
    for character in body:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == "[":
            bracket_depth += 1
        elif character == "]" and bracket_depth:
            bracket_depth -= 1
        elif character == "(" and bracket_depth == 0:
            paren_depth += 1
        elif character == ")" and paren_depth:
            paren_depth -= 1
        if character == "|" and bracket_depth == 0 and paren_depth == 0:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    cells.append("".join(current).strip())
    return cells


def _apply_url(cell: str) -> str:
    urls = re.findall(r"\]\((https?://[^)\s]+)\)", cell)
    for url in reversed(urls):
        parsed = urlparse(unescape(url))
        if parsed.hostname != "img.shields.io":
            return normalize_source_url(unescape(url))
    link_match = re.search(r'href=["\'](https?://[^"\']+)["\']', cell)
    return normalize_source_url(unescape(link_match.group(1))) if link_match else ""


def _listing_matches(listing: InternshipListing, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = " ".join(
        [
            listing.company,
            listing.role,
            listing.location,
            listing.apply_url,
            listing.date_posted,
        ]
    ).lower()
    return all(term in haystack for term in terms)


def _clean_markdown_text(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return clean_text(unescape(text))
