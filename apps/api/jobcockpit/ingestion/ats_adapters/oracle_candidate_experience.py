from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.ats_helpers import html_to_text, line, source_archive_from_response


@dataclass(frozen=True)
class OracleCandidateExperienceSite:
    origin: str
    site: str
    job_id: str | None = None


class OracleCandidateExperienceAdapter:
    provider = "oracle_candidate_experience"

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_oracle_candidate_experience_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_oracle_candidate_experience_url(request.source_url)
        if not site.job_id:
            raise AtsProviderError(400, "Oracle Candidate Experience URL must include a job detail id")
        if request.excludes_source_url(request.source_url):
            return []
        if not request.try_begin_link_attempt():
            return []
        try:
            payload, source_archive = self._get_json_with_archive(_detail_endpoint(site, site.job_id))
            return [_job_from_detail(site, payload, request.source_url, source_archive)]
        except AtsProviderError as error:
            request.stats.record_link_failure(request.source_url, error)
            return []

    def _get_json_with_archive(self, url: str) -> tuple[dict, SourceArchive]:
        try:
            response = self.http_client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AtsProviderError(502, "Oracle Candidate Experience detail request failed") from error
        return _json_object(response), source_archive_from_response(url, response)


def parse_oracle_candidate_experience_url(source_url: str) -> OracleCandidateExperienceSite:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AtsProviderError(400, "Only Oracle Candidate Experience public URLs are supported")

    hostname = parsed.hostname.lower()
    if hostname != "oraclecloud.com" and not hostname.endswith(".oraclecloud.com"):
        raise AtsProviderError(400, "Only Oracle Candidate Experience public URLs are supported")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        candidate_index = parts.index("CandidateExperience")
        sites_index = parts.index("sites", candidate_index)
    except ValueError as error:
        raise AtsProviderError(400, "Oracle Candidate Experience URL must include a CandidateExperience site") from error

    if len(parts) <= sites_index + 1:
        raise AtsProviderError(400, "Oracle Candidate Experience URL must include a site code")
    site = parts[sites_index + 1]

    job_id = _job_id_from_path(parts[sites_index + 2 :])
    return OracleCandidateExperienceSite(origin=f"{parsed.scheme}://{parsed.netloc}", site=site, job_id=job_id)


def oracle_candidate_experience_job_identity(source_url: str) -> str:
    try:
        site = parse_oracle_candidate_experience_url(source_url)
    except AtsProviderError:
        return ""
    if not site.job_id:
        return ""
    hostname = (urlparse(site.origin).hostname or "").casefold()
    if not hostname:
        return ""
    job_id = site.job_id
    if job_id[:1].casefold() == "j" and job_id[1:].isascii() and job_id[1:].isdigit():
        job_id = job_id[1:]
    return f"oracle_candidate_experience:{hostname}:{job_id}"


def oracle_candidate_experience_detail_urls(source_url: str) -> tuple[str, ...]:
    site = parse_oracle_candidate_experience_url(source_url)
    if not site.job_id:
        return ()
    lookup_ids = [site.job_id]
    if site.job_id.isascii() and site.job_id.isdigit():
        lookup_ids.append(f"J{site.job_id}")
    return tuple(_detail_endpoint(site, lookup_id) for lookup_id in lookup_ids)


def _job_id_from_path(parts: list[str]) -> str | None:
    if len(parts) >= 2 and parts[0] == "job":
        return parts[1]
    if len(parts) >= 3 and parts[0] == "jobs" and parts[1] == "preview":
        return parts[2]
    if (
        len(parts) >= 3
        and parts[0] == "requisitions"
        and parts[1] in {"job", "preview"}
    ):
        return parts[2]
    return None


def _detail_endpoint(site: OracleCandidateExperienceSite, job_id: str) -> str:
    return (
        f"{site.origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?expand=all&onlyData=true&finder=ById;Id=\"{job_id}\",siteNumber={site.site}"
    )


def _job_from_detail(
    site: OracleCandidateExperienceSite,
    detail_payload: dict,
    public_url: str,
    source_archive: SourceArchive,
) -> AtsJob:
    item = _first_item(detail_payload)
    title = _text(item.get("Title"))
    provider_job_id = _text(
        item.get("RequisitionNumber") or item.get("Id") or item.get("RequisitionId")
    )
    description = html_to_text(str(item.get("ExternalDescriptionStr") or item.get("Description") or ""))
    fields = [
        title,
        line("Req ID", provider_job_id),
        line("Category", item.get("Category")),
        line("Location", _location(item)),
        line("Schedule", item.get("JobSchedule")),
        line("Posted", item.get("ExternalPostedStartDate")),
        line("Posting End", item.get("ExternalPostedEndDate")),
        line("Apply URL", public_url),
        description,
        html_to_text(str(item.get("ExternalQualificationsStr") or "")),
        html_to_text(str(item.get("ExternalResponsibilitiesStr") or "")),
    ]
    return AtsJob(
        source_url=public_url,
        raw_text="\n".join(field for field in fields if field).strip(),
        source_archive=source_archive,
        title=title or None,
        provider_job_id=provider_job_id or None,
    )


def _first_item(payload: dict) -> dict:
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise AtsProviderError(502, "Oracle Candidate Experience response did not include a job detail item")
    return items[0]


def _location(item: dict) -> str:
    for key in ("PrimaryLocation", "Location", "WorkLocation"):
        value = _text(item.get(key))
        if value:
            return value
    locations = item.get("secondaryLocations") or item.get("OtherWorkLocations")
    if isinstance(locations, list):
        values = [_text(location.get("Name") if isinstance(location, dict) else location) for location in locations]
        return "; ".join(value for value in values if value)
    return ""






def _text(value: object | None) -> str:
    return str(value or "").strip()


def _json_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as error:
        raise AtsProviderError(502, "Oracle Candidate Experience response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise AtsProviderError(502, "Oracle Candidate Experience response was not a JSON object")
    return payload
