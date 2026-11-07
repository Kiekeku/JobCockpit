from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_helpers import html_to_text, line

PHENOM_ORACLE_PAGE_SIZE = 10
SUPPORTED_PHENOM_ORACLE_HOSTS = {"careers.atco.com"}
PROTECTED_LISTING_REQUEST_KEYS = {
    "counts",
    "country",
    "ddoKey",
    "deviceType",
    "from",
    "jobs",
    "keywords",
    "lang",
    "pageName",
    "size",
}


@dataclass(frozen=True)
class PhenomOracleSite:
    origin: str
    base_path: str
    country: str
    language: str
    locale: str
    job_lookup_id: str | None = None
    query_keywords: str = ""

    @property
    def widget_endpoint(self) -> str:
        return f"{self.origin}/widgets"

    @property
    def search_url(self) -> str:
        return f"{self.origin}{self.base_path}/search-results"

    @property
    def detail_referer(self) -> str:
        if self.job_lookup_id:
            return f"{self.origin}{self.base_path}/job/{quote(self.job_lookup_id, safe='')}"
        return self.search_url


class PhenomOracleAdapter:
    provider = "phenom_oracle"

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_phenom_oracle_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_phenom_oracle_url(request.source_url)
        if site.job_lookup_id:
            if request.excludes_source_url(request.source_url):
                return []
            if not request.try_begin_link_attempt():
                return []
            try:
                detail_payload = self._post_json(
                    site,
                    _detail_request(site, site.job_lookup_id),
                    referer=site.detail_referer,
                )
                return [_job_from_detail(site, _job_detail(detail_payload))]
            except AtsProviderError as error:
                request.stats.record_link_failure(request.source_url, error)
                return []

        jobs: list[AtsJob] = []
        offset = 0
        search_text = request.search_text or site.query_keywords
        while len(jobs) < request.limit and request.remaining_link_attempts:
            page_limit = min(PHENOM_ORACLE_PAGE_SIZE, request.remaining_link_attempts)
            listing_payload = self._post_json(
                site,
                _listing_request(
                    site,
                    offset=offset,
                    limit=page_limit,
                    search_text=search_text,
                    applied_facets=request.applied_facets,
                ),
                referer=site.search_url,
            )
            postings = _job_postings(listing_payload)
            if not postings:
                break
            def fetch_detail(identity):
                payload = self._post_json(site, _detail_request(site, identity), referer=site.search_url)
                return _job_from_detail(site, _job_detail(payload))

            def candidates():
                for posting in postings[:page_limit]:
                    lookup_id = _lookup_id(posting)
                    if not lookup_id:
                        continue
                    source_url = _public_job_url(site, posting)
                    if request.excludes_source_url(source_url):
                        continue
                    if not request.allows_listing_candidate(source_url, str(posting.get("title") or "")):
                        continue
                    yield source_url, lambda identity=lookup_id: fetch_detail(identity)

            jobs.extend(fetch_details(request, candidates()))
            offset += page_limit
            total = _refine_total(listing_payload)
            if total is not None and offset >= total:
                break
            if len(postings) < page_limit:
                break
        return jobs

    def _post_json(self, site: PhenomOracleSite, payload: dict, *, referer: str) -> dict:
        try:
            response = self.http_client.post(
                site.widget_endpoint,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": site.origin,
                    "Referer": referer,
                    "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            ddo_key = payload.get("ddoKey")
            request_kind = "detail" if ddo_key == "jobDetail" else "listing"
            raise AtsProviderError(502, f"Phenom/Oracle {request_kind} request failed") from error
        return _json_object(response)


def parse_phenom_oracle_url(source_url: str) -> PhenomOracleSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.scheme not in {"http", "https"} or hostname not in SUPPORTED_PHENOM_ORACLE_HOSTS:
        raise AtsProviderError(400, "Only supported Phenom/Oracle careers URLs are accepted")

    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(path_parts) < 3:
        raise AtsProviderError(400, "Phenom/Oracle URL must be a search results or job detail URL")

    country, language = path_parts[0], path_parts[1]
    page = path_parts[2]
    if not country or not language:
        raise AtsProviderError(400, "Phenom/Oracle URL must include country and language path segments")

    job_lookup_id = None
    if page == "job":
        if len(path_parts) < 4 or not path_parts[3]:
            raise AtsProviderError(400, "Phenom/Oracle job URL must include a job identifier")
        job_lookup_id = path_parts[3]
    elif page != "search-results":
        raise AtsProviderError(400, "Phenom/Oracle URL must be a search results or job detail URL")

    query_keywords = (parse_qs(parsed.query).get("keywords") or [""])[0].strip()
    return PhenomOracleSite(
        origin=f"{parsed.scheme}://{parsed.netloc}",
        base_path=f"/{country}/{language}",
        country=country,
        language=language,
        locale=f"{language}_{country}",
        job_lookup_id=job_lookup_id,
        query_keywords=query_keywords,
    )


def phenom_oracle_job_identity(source_url: str) -> str:
    try:
        site = parse_phenom_oracle_url(source_url)
    except AtsProviderError:
        return ""
    if not site.job_lookup_id:
        return ""
    hostname = (urlparse(site.origin).hostname or "").casefold()
    if not hostname:
        return ""
    return (
        f"phenom_oracle:{hostname}:{site.country.casefold()}:"
        f"{site.language.casefold()}:{site.job_lookup_id}"
    )


def _listing_request(
    site: PhenomOracleSite,
    *,
    offset: int,
    limit: int,
    search_text: str,
    applied_facets: dict,
) -> dict:
    payload = {
        **_common_request(site, "search-results", "refineSearch"),
        "from": offset,
        "size": limit,
        "jobs": True,
        "counts": True,
        "sortBy": "",
    }
    if search_text:
        payload["keywords"] = search_text
    payload.update(
        {
            key: value
            for key, value in (applied_facets or {}).items()
            if key not in PROTECTED_LISTING_REQUEST_KEYS
        }
    )
    return payload


def _detail_request(site: PhenomOracleSite, lookup_id: str) -> dict:
    payload = _common_request(site, "job", "jobDetail")
    if lookup_id.startswith("AGZ"):
        payload["jobSeqNo"] = lookup_id
    else:
        payload["jobId"] = lookup_id
    return payload


def _common_request(site: PhenomOracleSite, page_name: str, ddo_key: str) -> dict:
    return {
        "lang": site.locale,
        "deviceType": "desktop",
        "country": site.country,
        "pageName": page_name,
        "ddoKey": ddo_key,
    }


def _job_from_detail(site: PhenomOracleSite, job: dict) -> AtsJob:
    description = html_to_text(str(job.get("description") or ""))
    if not description:
        description = str(job.get("ml_Description") or job.get("descriptionTeaser") or "").strip()

    skills = job.get("ml_skills")
    skills_text = ", ".join(str(skill).strip() for skill in skills if str(skill).strip()) if isinstance(skills, list) else ""
    fields = [
        str(job.get("title") or "").strip(),
        line("Company", job.get("companyName") or job.get("company") or "ATCO Group"),
        line("Req ID", job.get("reqId") or job.get("jobId")),
        line("Location", _locations(job)),
        line("Business Unit", job.get("businessUnit")),
        line("Category", job.get("category") or job.get("jobCategory")),
        line("Employment Type", job.get("type") or job.get("jobType")),
        line("Posted", job.get("postedDate")),
        line("Status", job.get("jobStatus") or job.get("publishedJobStatus")),
        line("Apply URL", job.get("applyUrl")),
        line("Skills", skills_text),
        description,
    ]
    raw_text = "\n".join(field for field in fields if field).strip()
    return AtsJob(source_url=_public_job_url(site, job), raw_text=raw_text)


def _job_postings(payload: dict) -> list[dict]:
    refine_search = payload.get("refineSearch")
    if not isinstance(refine_search, dict):
        raise AtsProviderError(502, "Phenom/Oracle listing response did not include refineSearch")
    jobs = (refine_search.get("data") or {}).get("jobs") or []
    if not isinstance(jobs, list):
        raise AtsProviderError(502, "Phenom/Oracle listing response did not include jobs")
    return [job for job in jobs if isinstance(job, dict)]


def _job_detail(payload: dict) -> dict:
    job_detail = payload.get("jobDetail")
    if not isinstance(job_detail, dict):
        raise AtsProviderError(502, "Phenom/Oracle detail response did not include jobDetail")
    job = (job_detail.get("data") or {}).get("job")
    if not isinstance(job, dict):
        raise AtsProviderError(502, "Phenom/Oracle detail response did not include a job")
    return job


def _refine_total(payload: dict) -> int | None:
    refine_search = payload.get("refineSearch")
    if not isinstance(refine_search, dict):
        return None
    return _optional_int(refine_search.get("totalHits"))


def _lookup_id(job: dict) -> str:
    return str(job.get("jobSeqNo") or job.get("jobId") or job.get("reqId") or "").strip()


def _locations(job: dict) -> str:
    multi_location = job.get("multi_location")
    if isinstance(multi_location, list):
        locations = [str(location).strip() for location in multi_location if str(location).strip()]
        if locations:
            return "; ".join(dict.fromkeys(locations))
    return str(job.get("location") or job.get("cityStateCountry") or job.get("address") or "").strip()


def _public_job_url(site: PhenomOracleSite, job: dict) -> str:
    lookup_id = str(job.get("jobId") or job.get("reqId") or job.get("jobSeqNo") or "").strip()
    title = str(job.get("title") or "job").strip()
    slug = _slugify(title)
    if not lookup_id:
        return site.search_url
    return f"{site.origin}{site.base_path}/job/{quote(lookup_id, safe='')}/{slug}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return slug or "job"






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
        raise AtsProviderError(502, "Phenom/Oracle response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise AtsProviderError(502, "Phenom/Oracle response was not a JSON object")
    return payload
