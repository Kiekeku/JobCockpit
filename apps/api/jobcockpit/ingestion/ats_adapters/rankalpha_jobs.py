from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsProviderError,
    AtsSourceExclusion,
    SourceArchive,
)
from jobcockpit.ingestion.ats_helpers import archived_headers, html_to_text, request_headers

RANKALPHA_JOBS_HOST = "jobs.rankalpha.io"
RANKALPHA_JOBS_PATH = "/api/jobs"
RANKALPHA_JOBS_URL = f"https://{RANKALPHA_JOBS_HOST}{RANKALPHA_JOBS_PATH}"
RANKALPHA_JOBS_MAX_PAGE_SIZE = 100
RANKALPHA_CREDENTIAL_QUERY_KEYS = {
    "accesstoken",
    "apikey",
    "apisecret",
    "authorization",
    "authtoken",
    "bearer",
    "bearertoken",
    "clientsecret",
    "idtoken",
    "refreshtoken",
    "token",
}


@dataclass(frozen=True)
class RankAlphaJobsSite:
    source_url: str
    query_pairs: tuple[tuple[str, str], ...]


class RankAlphaJobsAdapter:
    provider = "rankalpha_jobs"

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        bearer_token: str | None = None,
    ) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self._bearer_token = bearer_token.strip() if bearer_token else ""

    def configure_settings(self, settings: Settings) -> None:
        if self._bearer_token or settings.rankalpha_jobs_api_token is None:
            return
        self._bearer_token = settings.rankalpha_jobs_api_token.get_secret_value().strip()

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_rankalpha_jobs_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        if not self._bearer_token:
            raise AtsProviderError(
                503,
                "JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN is not configured",
            )
        site = parse_rankalpha_jobs_url(request.source_url)
        query_pairs = _merged_query_pairs(site.query_pairs, request)
        page = _positive_query_int(site.query_pairs, "page", default=1)
        page_size = min(
            RANKALPHA_JOBS_MAX_PAGE_SIZE,
            request.limit,
            _positive_query_int(site.query_pairs, "limit", default=request.limit),
        )
        jobs: list[AtsJob] = []
        seen_source_urls: set[str] = set()
        already_imported_count = 0

        while request.remaining_link_attempts:
            page_url = _page_url(query_pairs, page=page, limit=page_size)
            response, payload = self._get_page(page_url)
            raw_jobs, pagination = _validated_payload(payload)
            for raw_job in raw_jobs:
                job = _job_from_payload(
                    raw_job,
                    portal_source_url=request.source_url,
                    response=response,
                )
                normalized_source_url = request.source_url_normalizer(job.source_url)
                if normalized_source_url in seen_source_urls:
                    continue
                seen_source_urls.add(normalized_source_url)
                if (request.excludes_provider_job_id(job.provider_job_id)
                        or request.excludes_source_url(job.source_url)):
                    already_imported_count += 1
                    continue
                if not request.allows_listing_candidate(job.source_url, job.title or ""):
                    continue
                if not request.try_begin_link_attempt():
                    break
                jobs.append(job)
            if not request.remaining_link_attempts or not _has_next_page(
                pagination,
                current_page=page,
                returned_count=len(raw_jobs),
                page_size=page_size,
            ):
                break
            page += 1
            if page > 10_000:
                raise AtsProviderError(502, "RankAlpha jobs API pagination was invalid")

        if already_imported_count:
            request.stats.source_exclusions.append(
                AtsSourceExclusion(
                    category="already_imported",
                    count=already_imported_count,
                    reason="RankAlpha jobs already present in JobCockpit",
                )
            )
        return jobs

    def _get_page(self, page_url: str) -> tuple[httpx.Response, dict[str, Any]]:
        headers = request_headers("application/json")
        headers["Authorization"] = f"Bearer {self._bearer_token}"
        try:
            response = self.http_client.get(page_url, headers=headers, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                raise AtsProviderError(
                    502,
                    "RankAlpha jobs API rejected the configured bearer token",
                ) from error
            raise AtsProviderError(
                502,
                f"RankAlpha jobs API request failed with status {error.response.status_code}",
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise AtsProviderError(502, "RankAlpha jobs API request failed") from error
        if not isinstance(payload, dict):
            raise AtsProviderError(502, "RankAlpha jobs API response was invalid")
        return response, payload


def parse_rankalpha_jobs_url(source_url: str) -> RankAlphaJobsSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    try:
        port = parsed.port
    except ValueError as error:
        raise AtsProviderError(
            400, f"Only {RANKALPHA_JOBS_URL} URLs are supported"
        ) from error
    query_pairs = tuple(parse_qsl(parsed.query, keep_blank_values=True))
    has_secret_query = any(_is_credential_query_key(key) for key, _value in query_pairs)
    if (
        parsed.scheme != "https"
        or hostname != RANKALPHA_JOBS_HOST
        or port not in {None, 443}
        or parsed.path.rstrip("/") != RANKALPHA_JOBS_PATH
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
        or has_secret_query
    ):
        raise AtsProviderError(400, f"Only {RANKALPHA_JOBS_URL} URLs are supported")
    clean_url = urlunparse(("https", parsed.netloc, RANKALPHA_JOBS_PATH, "", parsed.query, ""))
    return RankAlphaJobsSite(source_url=clean_url, query_pairs=query_pairs)


def validate_rankalpha_jobs_request_inputs(
    source_url: str,
    applied_facets: dict,
) -> None:
    parse_rankalpha_jobs_url(source_url)
    if any(_is_credential_query_key(str(key)) for key in applied_facets):
        raise AtsProviderError(
            400,
            "RankAlpha jobs credentials must be configured in the backend environment",
        )


def _merged_query_pairs(
    source_pairs: tuple[tuple[str, str], ...], request: AtsFetchRequest
) -> list[tuple[str, str]]:
    pairs = [
        (key, value)
        for key, value in source_pairs
        if key.casefold() not in {"page", "limit"}
        and not (request.search_text and key.casefold() == "q")
    ]
    for raw_key, raw_values in request.applied_facets.items():
        key = str(raw_key).strip()
        if not key or _is_credential_query_key(key) or key.casefold() in {"page", "limit"}:
            raise AtsProviderError(400, "RankAlpha jobs applied facets were invalid")
        for value in _facet_values(raw_values):
            pairs.append((key, value))
    if request.search_text:
        pairs.append(("q", request.search_text))
    return pairs


def _facet_values(value: object) -> Iterable[str]:
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    for item in values:
        if item is None:
            continue
        if isinstance(item, bool):
            yield str(item).lower()
            continue
        if isinstance(item, (str, int, float)):
            yield str(item).strip()
            continue
        raise AtsProviderError(400, "RankAlpha jobs applied facets were invalid")


def _is_credential_query_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    if normalized == "workauthorizationfit":
        return False
    return (
        normalized in RANKALPHA_CREDENTIAL_QUERY_KEYS
        or normalized.endswith(("token", "secret"))
        or normalized.startswith(("bearer", "secret"))
        or any(
            marker in normalized
            for marker in (
                "apikey",
                "apisecret",
                "authorization",
                "clientsecret",
                "credential",
                "password",
            )
        )
    )


def _positive_query_int(
    query_pairs: Iterable[tuple[str, str]], key: str, *, default: int
) -> int:
    values = [value for item_key, value in query_pairs if item_key.casefold() == key]
    if not values:
        return default
    try:
        value = int(values[-1])
    except (TypeError, ValueError) as error:
        raise AtsProviderError(400, f"RankAlpha jobs {key} must be a positive integer") from error
    if value < 1:
        raise AtsProviderError(400, f"RankAlpha jobs {key} must be a positive integer")
    return value


def _page_url(query_pairs: list[tuple[str, str]], *, page: int, limit: int) -> str:
    pairs = [(key, value) for key, value in query_pairs if key.casefold() not in {"page", "limit"}]
    pairs.extend((item for item in (("page", str(page)), ("limit", str(limit)))))
    return f"{RANKALPHA_JOBS_URL}?{urlencode(pairs)}"


def _validated_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_jobs = payload.get("data")
    pagination = payload.get("pagination")
    applied_filters = payload.get("appliedFilters")
    if (
        not isinstance(raw_jobs, list)
        or not isinstance(pagination, dict)
        or not isinstance(applied_filters, dict)
        or any(not isinstance(raw_job, dict) for raw_job in raw_jobs)
    ):
        raise AtsProviderError(502, "RankAlpha jobs API response was invalid")
    return raw_jobs, pagination


def _job_from_payload(
    raw_job: dict[str, Any],
    *,
    portal_source_url: str,
    response: httpx.Response,
) -> AtsJob:
    title = _first_value(raw_job, "title", "jobTitle", "role")
    source_url = _job_url(raw_job)
    if not title or not source_url:
        raise AtsProviderError(502, "RankAlpha jobs API response was invalid")
    provider_job_id = _first_value(raw_job, "id", "jobId", "job_id", "slug") or None
    raw_text = _raw_job_text(raw_job, title=title, source_url=source_url)
    archive_body = json.dumps(
        raw_job,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    archive = SourceArchive(
        source_url=str(response.url),
        final_url=str(response.url),
        status_code=response.status_code,
        content_type=response.headers.get("content-type", "application/json"),
        body=archive_body,
        headers={
            **archived_headers(response.headers),
            "x-jobcockpit-capture-source": "rankalpha_api_job_object",
        },
    )
    return AtsJob(
        source_url=source_url,
        raw_text=raw_text,
        portal_source_url=portal_source_url,
        source_archive=archive,
        content_source_url=portal_source_url,
        title=title,
        provider_job_id=provider_job_id,
    )


def _job_url(raw_job: dict[str, Any]) -> str:
    value = _first_value(
        raw_job,
        "url",
        "applyUrl",
        "apply_url",
        "applyLink",
        "applicationUrl",
        "application_url",
        "applicationLink",
        "sourceUrl",
        "source_url",
        "jobUrl",
        "job_url",
        "externalUrl",
        "external_url",
        "link",
    )
    links = raw_job.get("links")
    if not value and isinstance(links, dict):
        value = _first_value(links, "apply", "application", "source", "job")
    if not value:
        return ""
    value = urljoin(f"https://{RANKALPHA_JOBS_HOST}/", value)
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise AtsProviderError(502, "RankAlpha jobs API response was invalid")
    return value


def _raw_job_text(raw_job: dict[str, Any], *, title: str, source_url: str) -> str:
    fields = [
        title,
        _line("Company", _first_value(raw_job, "company", "companyName", "employer")),
        _line("Location", _first_value(raw_job, "location", "locations")),
        _line("Country", _first_value(raw_job, "country")),
        _line("Work Arrangement", _first_value(raw_job, "workMode", "workArrangement")),
        _line("Status", _first_value(raw_job, "status")),
        _line("Posted", _first_value(raw_job, "postedAt", "postedDate", "datePosted")),
        _line("Deadline", _first_value(raw_job, "deadline", "applicationDeadline")),
        _line("Verified", _first_value(raw_job, "verifiedAt", "lastVerifiedAt")),
        _line(
            "Work Authorization Fit",
            _first_value(raw_job, "workAuthorizationFit", "work_authorization_fit"),
        ),
        _line("Employment Timing", _first_value(raw_job, "employmentTiming", "term")),
        _line("Tags", _first_value(raw_job, "tags", "skills")),
        _line("RankAlpha Job ID", _first_value(raw_job, "id", "jobId", "job_id", "slug")),
        _line("Apply URL", source_url),
        _description(raw_job),
    ]
    return "\n".join(field for field in fields if field).strip()


def _description(raw_job: dict[str, Any]) -> str:
    description = _first_value(
        raw_job,
        "description",
        "descriptionText",
        "descriptionMarkdown",
        "summary",
        "details",
    )
    if "<" in description and ">" in description:
        return html_to_text(description)
    return description


def _first_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _value_text(payload.get(key))
        if text:
            return text
    return ""


def _value_text(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(text for item in value if (text := _value_text(item)))
    if isinstance(value, dict):
        for key in ("name", "label", "value", "title", "city"):
            text = _value_text(value.get(key))
            if text:
                return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _line(label: str, value: object) -> str:
    text = _value_text(value)
    return f"{label}: {text}" if text else ""


def _has_next_page(
    pagination: dict[str, Any],
    *,
    current_page: int,
    returned_count: int,
    page_size: int,
) -> bool:
    for key in ("hasNextPage", "hasNext", "has_next_page", "has_next"):
        value = pagination.get(key)
        if isinstance(value, bool):
            return value
    total_pages = _first_int(pagination, "totalPages", "total_pages", "pageCount")
    if total_pages is not None:
        return current_page < total_pages
    next_page = _first_int(pagination, "nextPage", "next_page")
    if next_page is not None:
        return next_page > current_page
    total = _first_int(pagination, "total", "totalCount", "total_count")
    if total is not None:
        return current_page * page_size < total
    return returned_count >= page_size


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None
