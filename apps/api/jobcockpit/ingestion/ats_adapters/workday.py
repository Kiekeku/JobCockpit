from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.ats_helpers import html_to_text, line, source_archive_from_response

WORKDAY_PAGE_SIZE = 20
WORKDAY_LOCALE_PATH_SEGMENT = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)


@dataclass(frozen=True)
class WorkdaySite:
    origin: str
    tenant: str
    site: str
    external_path: str | None = None


class WorkdayAdapter:
    provider = "workday"

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_workday_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_workday_url(request.source_url)
        if site.external_path:
            direct_source_url = _public_job_url(site, site.external_path)
            if request.excludes_source_url(request.source_url) or request.excludes_source_url(direct_source_url):
                return []
            if not request.try_begin_link_attempt():
                return []
            try:
                detail_payload, source_archive = self._get_json_with_archive(
                    _detail_endpoint(site, site.external_path)
                )
                return [_job_from_detail(site, detail_payload, site.external_path, source_archive)]
            except AtsProviderError as error:
                request.stats.record_link_failure(direct_source_url, error)
                return []

        jobs: list[AtsJob] = []
        offset = 0
        listing_url = _listing_endpoint(site)
        while len(jobs) < request.limit and request.remaining_link_attempts:
            page_limit = min(WORKDAY_PAGE_SIZE, request.remaining_link_attempts)
            listing_payload = self._post_json(
                listing_url,
                {
                    "appliedFacets": request.applied_facets,
                    "limit": page_limit,
                    "offset": offset,
                    "searchText": request.search_text,
                },
            )
            postings = _job_postings(listing_payload)
            if not postings:
                break
            def fetch_detail(path):
                payload, archive = self._get_json_with_archive(_detail_endpoint(site, path))
                return _job_from_detail(site, payload, path, archive)

            def candidates():
                for posting in postings[:page_limit]:
                    external_path = str(posting.get("externalPath") or "")
                    if not external_path:
                        continue
                    source_url = _public_job_url(site, external_path, _external_url(posting))
                    if request.excludes_source_url(source_url):
                        continue
                    if not request.allows_listing_candidate(source_url, str(posting.get("title") or "")):
                        continue
                    yield source_url, lambda path=external_path: fetch_detail(path)

            jobs.extend(fetch_details(request, candidates()))
            offset += page_limit
            total = _optional_int(listing_payload.get("total"))
            if total is not None and offset >= total:
                break
            if len(postings) < page_limit:
                break
        return jobs

    def _post_json(self, url: str, payload: dict) -> dict:
        try:
            response = self.http_client.post(url, json=payload, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AtsProviderError(502, "Workday listing request failed") from error
        return _json_object(response)

    def _get_json(self, url: str) -> dict:
        payload, _source_archive = self._get_json_with_archive(url)
        return payload

    def _get_json_with_archive(self, url: str) -> tuple[dict, SourceArchive]:
        try:
            response = self.http_client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AtsProviderError(502, "Workday detail request failed") from error
        return _json_object(response), source_archive_from_response(url, response)


def fetch_workday_jobs(
    source_url: str,
    *,
    limit: int = 10,
    search_text: str = "",
    applied_facets: dict | None = None,
) -> list[AtsJob]:
    with httpx.Client(timeout=20) as http_client:
        return WorkdayAdapter(http_client=http_client).fetch_jobs(
            AtsFetchRequest(
                source_url=source_url,
                limit=limit,
                search_text=search_text,
                applied_facets=applied_facets or {},
            )
        )


def parse_workday_url(source_url: str) -> WorkdaySite:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AtsProviderError(400, "Only Workday public URLs are supported")

    hostname = parsed.hostname.lower()
    if (
        hostname != "myworkdayjobs.com"
        and not hostname.endswith(".myworkdayjobs.com")
        and not hostname.endswith(".myworkdaysite.com")
    ):
        raise AtsProviderError(400, "Only Workday public URLs are supported")

    tenant = hostname.split(".", 1)[0]
    path_parts = [part for part in parsed.path.split("/") if part]
    if _has_workday_locale_prefix(hostname, path_parts):
        path_parts = path_parts[1:]
    if len(path_parts) >= 4 and path_parts[0] == "wday" and path_parts[1] == "cxs":
        tenant = path_parts[2]
        site = path_parts[3]
        external_path = "/" + "/".join(path_parts[4:]) if len(path_parts) > 4 else None
    elif (
        hostname.endswith(".myworkdaysite.com")
        and len(path_parts) > 4
        and path_parts[0] == "recruiting"
        and path_parts[3] == "job"
    ):
        tenant = path_parts[1]
        site = path_parts[2]
        external_path = "/" + "/".join(path_parts[3:])
    elif hostname.endswith(".myworkdaysite.com"):
        raise AtsProviderError(400, "Workday myworkdaysite URL must include a recruiting job path")
    elif path_parts:
        site = path_parts[0]
        external_path = "/" + "/".join(path_parts[1:]) if len(path_parts) > 1 else None
    else:
        raise AtsProviderError(400, "Workday URL must include a career site path")

    # Selected search results use /details/; CXS serves the same posting at /job/.
    if external_path and external_path.startswith("/details/"):
        external_path = "/job/" + external_path.removeprefix("/details/")
    if external_path and not external_path.startswith("/job/"):
        external_path = None
    # Public search results also link to the application's entry route. CXS
    # serves the posting at the parent path, not at its /apply child.
    if external_path and external_path.endswith('/apply') and external_path.count('/') >= 3:
        external_path = external_path.removesuffix('/apply')

    return WorkdaySite(origin=f"{parsed.scheme}://{parsed.netloc}", tenant=tenant, site=site, external_path=external_path)


def _has_workday_locale_prefix(hostname: str, path_parts: list[str]) -> bool:
    if not path_parts or not WORKDAY_LOCALE_PATH_SEGMENT.fullmatch(path_parts[0]):
        return False
    if hostname.endswith(".myworkdaysite.com"):
        return len(path_parts) > 1 and path_parts[1] == "recruiting"
    if len(path_parts) == 2:
        return True
    return (
        len(path_parts) >= 3
        and path_parts[1] not in {"job", "details"}
        and path_parts[2] in {"job", "details"}
    )


def canonicalize_workday_job_url(source_url: str) -> str:
    try:
        site = parse_workday_url(source_url)
    except AtsProviderError:
        return ""
    if not site.external_path:
        return ""
    hostname = (urlparse(site.origin).hostname or "").casefold()
    if not hostname:
        return ""
    canonical_site = WorkdaySite(
        origin=f"https://{hostname}",
        tenant=site.tenant,
        site=site.site,
        external_path=site.external_path,
    )
    return _public_job_url(canonical_site, site.external_path)


def _listing_endpoint(site: WorkdaySite) -> str:
    return f"{site.origin}/wday/cxs/{site.tenant}/{site.site}/jobs"


def _detail_endpoint(site: WorkdaySite, external_path: str) -> str:
    path = external_path if external_path.startswith("/") else f"/{external_path}"
    return f"{site.origin}/wday/cxs/{site.tenant}/{site.site}{path}"


def _public_job_url(site: WorkdaySite, external_path: str, external_url: str | None = None) -> str:
    if external_url:
        return external_url
    path = external_path if external_path.startswith("/") else f"/{external_path}"
    hostname = urlparse(site.origin).hostname or ""
    if hostname.endswith(".myworkdaysite.com"):
        return f"{site.origin}/recruiting/{site.tenant}/{site.site}{path}"
    return f"{site.origin}/{site.site}{path}"


def _job_from_detail(
    site: WorkdaySite,
    detail_payload: dict,
    external_path: str,
    source_archive: SourceArchive | None = None,
) -> AtsJob:
    info = detail_payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise AtsProviderError(502, "Workday detail response did not include jobPostingInfo")

    description = html_to_text(str(info.get("jobDescription") or ""))
    fields = [
        str(info.get("title") or "").strip(),
        line("Req ID", info.get("jobReqId") or info.get("jobPostingId")),
        line("Location", info.get("location")),
        line("Time Type", info.get("timeType")),
        line("Posted", info.get("postedOn") or info.get("posted")),
        line("Apply URL", info.get("externalUrl")),
        description,
    ]
    raw_text = "\n".join(field for field in fields if field).strip()
    return AtsJob(
        source_url=_public_job_url(site, external_path, info.get("externalUrl")),
        raw_text=raw_text,
        source_archive=source_archive,
        title=str(info.get("title") or "").strip() or None,
        provider_job_id=(
            str(info.get("jobReqId") or info.get("jobPostingId") or "").strip() or None
        ),
    )










def _external_url(posting: dict) -> str | None:
    external_url = posting.get("externalUrl")
    return str(external_url).strip() if external_url else None


def _job_postings(payload: dict) -> list[dict]:
    postings = payload.get("jobPostings") or []
    if not isinstance(postings, list):
        raise AtsProviderError(502, "Workday listing response did not include jobPostings")
    return [posting for posting in postings if isinstance(posting, dict)]


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise AtsProviderError(502, "Workday response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise AtsProviderError(502, "Workday response was not a JSON object")
    return payload
