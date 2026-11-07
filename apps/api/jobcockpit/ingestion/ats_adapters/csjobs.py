from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsProviderError,
    AtsVerificationRequiredError,
    AtsSourceExclusion,
)
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import (
    configured_cdp_resolver,
)
from jobcockpit.ingestion.ats_helpers import enforce_url_policy, request_headers
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.posting_discovery import (
    ExternalPostingHint,
    RenderedPostingResolver,
    normalize_posting_resolution,
    rendered_query_identity_groups,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy

CSJOBS_HOST = "csjobs.ca"
CSJOBS_PATH = "/jobs"
CSJOBS_API_PATH = "/api/v1/jobs"
CSJOBS_PUBLIC_PREVIEW_SIZE = 90
CSJOBS_PREMIUM_EXCLUSION_REASON = (
    "CS Jobs reports additional internship postings behind Premium without public links"
)


@dataclass(frozen=True)
class CsJobsSite:
    origin: str

    @property
    def url(self) -> str:
        return f"{self.origin}{CSJOBS_PATH}"

    @property
    def api_url(self) -> str:
        return f"{self.origin}{CSJOBS_API_PATH}"


@dataclass(frozen=True)
class CsJobsListing:
    provider_job_id: str
    title: str
    company: str
    location: str
    date_posted: str
    apply_url: str


class CsJobsAdapter:
    provider = "csjobs"

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        apply_resolver: AtsApplyPageResolver | None = None,
        rendered_page_resolver: RenderedPostingResolver | None = None,
        url_policy: UrlFetchPolicy | None = None,
    ) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self.url_policy = url_policy or UrlFetchPolicy()
        self.apply_resolver = apply_resolver or AtsApplyPageResolver(
            http_client=self.http_client,
            url_policy=self.url_policy,
        )
        self.rendered_page_resolver = rendered_page_resolver

    def configure_settings(self, settings: Settings) -> None:
        if self.rendered_page_resolver is None:
            self.rendered_page_resolver = configured_cdp_resolver(
                settings,
                self.url_policy,
            )

    def close(self) -> None:
        cancel = getattr(self.rendered_page_resolver, "cancel_active_pages", None)
        if callable(cancel):
            cancel()
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_csjobs_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_csjobs_url(request.source_url)
        payload = self._get_public_internships(site, request.search_text)
        listings = _listings_from_payload(payload)
        _record_premium_exclusion(request, payload, returned_count=len(listings))

        already_imported_count = 0

        def candidates():
            nonlocal already_imported_count
            for listing in listings:
                if request.excludes_source_url(listing.apply_url):
                    already_imported_count += 1
                    continue
                if not request.allows_listing_candidate(listing.apply_url, listing.title):
                    continue
                yield listing.apply_url, lambda listing=listing: self._job_from_listing(listing, site)

        jobs = fetch_details(request, candidates())
        if already_imported_count:
            request.stats.source_exclusions.append(
                AtsSourceExclusion(
                    category="already_imported",
                    count=already_imported_count,
                    reason="CS Jobs public internship links already present in JobCockpit",
                )
            )
        return jobs

    def _job_from_listing(self, listing: CsJobsListing, site: CsJobsSite) -> AtsJob:
        context = AtsPostingContext(
            source_label="CS Jobs", source_url=site.url, company=listing.company,
            role=listing.title, location=listing.location, date_posted=listing.date_posted,
            apply_url=listing.apply_url,
        )
        try:
            resolved = self.apply_resolver.resolve(listing.apply_url, context)
        except AtsVerificationRequiredError:
            # The Eluta adapter has already attempted guarded session recovery.
            # Another render would reuse the same blocked/cooling-down profile.
            raise
        except AtsProviderError as error:
            if "blocked by policy" in error.detail.casefold():
                raise
            resolved = self._job_from_rendered_listing(listing)
        return replace(resolved, portal_source_url=listing.apply_url,
                       title=listing.title, provider_job_id=listing.provider_job_id)

    def _job_from_rendered_listing(self, listing: CsJobsListing) -> AtsJob:
        if self.rendered_page_resolver is None:
            raise AtsProviderError(502, "Apply page request failed")
        identity_query_groups = rendered_query_identity_groups(listing.apply_url)
        hint = ExternalPostingHint(
            source_url=listing.apply_url,
            source_kind="generic",
            company=listing.company,
            title=listing.title,
            location=listing.location,
            link_text=listing.apply_url,
            evidence_text=f"{listing.company} {listing.title} {listing.location}",
            url_identity="path_and_query" if identity_query_groups else "path",
            identity_query_groups=identity_query_groups,
        )
        try:
            resolution = normalize_posting_resolution(self.rendered_page_resolver.resolve(hint))
        except Exception as error:
            detail = str(error) or error.__class__.__name__
            raise AtsProviderError(
                502,
                f"Rendered CS Jobs apply-page fallback failed: {detail}",
            ) from error
        if resolution.status != "resolved" or resolution.posting is None:
            detail = resolution.error or "rendered posting was not resolved"
            raise AtsProviderError(
                502,
                f"Rendered CS Jobs apply-page fallback failed: {detail}",
            )
        posting = resolution.posting
        context_text = "\n".join(
            (
                f"CS Jobs Company: {listing.company}",
                f"CS Jobs Role: {listing.title}",
                f"CS Jobs Location: {listing.location}",
                f"CS Jobs Date Posted: {listing.date_posted}",
                "CS Jobs URL: https://csjobs.ca/jobs",
                f"CS Jobs Apply URL: {listing.apply_url}",
            )
        )
        return AtsJob(
            source_url=posting.source_url or listing.apply_url,
            raw_text=f"{posting.raw_text.strip()}\n{context_text}".strip(),
            source_archive=posting.source_archive,
        )

    def _get_public_internships(self, site: CsJobsSite, search_text: str) -> dict[str, Any]:
        query = [
            ("page", "1"),
            ("per_page", str(CSJOBS_PUBLIC_PREVIEW_SIZE)),
            ("job_type", "internship"),
        ]
        if search_text.strip():
            query.append(("search", search_text.strip()))
        api_url = f"{site.api_url}?{urlencode(query)}"
        enforce_url_policy(self.url_policy, api_url, label="CS Jobs listing API")
        try:
            response = self.http_client.get(
                api_url,
                headers=request_headers("application/json"),
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AtsProviderError(502, "CS Jobs internship listing request failed") from error
        if not isinstance(payload, dict):
            raise AtsProviderError(502, "CS Jobs internship listing response was invalid")
        return payload


def parse_csjobs_url(source_url: str) -> CsJobsSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    path = parsed.path.rstrip("/") or "/"
    if (
        parsed.scheme not in {"http", "https"}
        or hostname != CSJOBS_HOST
        or path != CSJOBS_PATH
        or parsed.params
    ):
        raise AtsProviderError(400, "Only csjobs.ca/jobs is supported")
    return CsJobsSite(origin=f"{parsed.scheme}://{parsed.netloc}")


def _listings_from_payload(payload: dict[str, Any]) -> list[CsJobsListing]:
    raw_jobs = payload.get("jobs")
    meta = payload.get("meta")
    if not isinstance(raw_jobs, list) or not isinstance(meta, dict):
        raise AtsProviderError(502, "CS Jobs internship listing response was invalid")

    listings: list[CsJobsListing] = []
    seen_urls: set[str] = set()
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict) or raw_job.get("job_type") != "internship":
            raise AtsProviderError(502, "CS Jobs internship listing response included a non-intern job")
        apply_url = str(raw_job.get("link") or "").strip()
        title = str(raw_job.get("title") or "").strip()
        provider_job_id = str(raw_job.get("id") or "").strip()
        if not apply_url or not title or not provider_job_id:
            raise AtsProviderError(502, "CS Jobs internship listing response omitted a public job link")
        if apply_url in seen_urls:
            continue
        seen_urls.add(apply_url)
        listings.append(
            CsJobsListing(
                provider_job_id=provider_job_id,
                title=title,
                company=str(raw_job.get("company") or "").strip(),
                location=str(raw_job.get("location") or "").strip(),
                date_posted=str(raw_job.get("date_posted") or "").strip(),
                apply_url=apply_url,
            )
        )
    return listings


def _record_premium_exclusion(
    request: AtsFetchRequest,
    payload: dict[str, Any],
    *,
    returned_count: int,
) -> None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return
    try:
        total = int(meta.get("total") or 0)
    except (TypeError, ValueError):
        return
    excluded_count = max(0, total - returned_count)
    if excluded_count:
        request.stats.source_exclusions.append(
            AtsSourceExclusion(
                category="premium_locked",
                count=excluded_count,
                reason=CSJOBS_PREMIUM_EXCLUSION_REASON,
            )
        )
