from __future__ import annotations

import re
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
from urllib.parse import quote, urlparse

import httpx

from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, SearchResult


_MAX_RESULTS = 20


@contextmanager
def _http_client_scope(
    http_client: httpx.Client | None,
    timeout_seconds: float,
) -> Iterator[httpx.Client]:
    if http_client is not None:
        yield http_client
        return
    with httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as owned_client:
        yield owned_client


class SearchClient(Protocol):
    provider_code: str
    last_error: str

    def search(self, query: str, limit: int) -> Sequence[SearchResult]: ...


@dataclass(frozen=True)
class JibeCareerSite:
    company_aliases: tuple[str, ...]
    origin: str
    public_jobs_path: str

    @property
    def search_url(self) -> str:
        return f"{self.origin}/api/jobs"


JIBE_CAREER_SITES: tuple[JibeCareerSite, ...] = (
    JibeCareerSite(
        company_aliases=("amd", "amd canada", "advanced micro devices", "advanced micro devices inc"),
        origin="https://careers.amd.com",
        public_jobs_path="/careers-home/jobs",
    ),
)


@dataclass(frozen=True)
class RecruiteeCareerSite:
    company_aliases: tuple[str, ...]
    origin: str

    @property
    def offers_url(self) -> str:
        return f"{self.origin}/api/offers/"


RECRUITEE_CAREER_SITES: tuple[RecruiteeCareerSite, ...] = (
    RecruiteeCareerSite(
        company_aliases=(
            "huawei canada",
            "huawei technologies canada",
            "huawei technologies canada co",
        ),
        origin="https://huaweicanada.recruitee.com",
    ),
)


@dataclass(frozen=True)
class GreenhouseCareerSite:
    company_aliases: tuple[str, ...]
    board_token: str

    @property
    def jobs_url(self) -> str:
        return (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{self.board_token}/jobs?content=true"
        )


GREENHOUSE_CAREER_SITES: tuple[GreenhouseCareerSite, ...] = (
    GreenhouseCareerSite(
        company_aliases=(
            "riverside natural foods ltd home of madegood",
            "riverside natural foods home of madegood",
            "riverside natural foods",
        ),
        board_token="riversidenaturalfoodsltd",
    ),
)


@dataclass(frozen=True)
class WorkdayCareerSite:
    company_aliases: tuple[str, ...]
    origin: str
    tenant: str
    site: str

    @property
    def jobs_url(self) -> str:
        return f"{self.origin}/wday/cxs/{self.tenant}/{self.site}/jobs"


WORKDAY_CAREER_SITES: tuple[WorkdayCareerSite, ...] = (
    WorkdayCareerSite(
        company_aliases=("bdo", "bdo canada"),
        origin="https://bdo.wd3.myworkdayjobs.com",
        tenant="bdo",
        site="BDO",
    ),
    WorkdayCareerSite(
        company_aliases=("pratt whitney", "pratt and whitney"),
        origin="https://globalhr.wd5.myworkdayjobs.com",
        tenant="globalhr",
        site="REC_RTX_Ext_Gateway",
    ),
    WorkdayCareerSite(
        company_aliases=("suncor", "suncor energy"),
        origin="https://suncor.wd1.myworkdayjobs.com",
        tenant="suncor",
        site="Suncor_External",
    ),
    WorkdayCareerSite(
        company_aliases=("rbc", "royal bank of canada"),
        origin="https://rbc.wd3.myworkdayjobs.com",
        tenant="rbc",
        site="RBCEARLYTALENT1",
    ),
)


@dataclass(frozen=True)
class ReviewedCareerDomain:
    company_aliases: tuple[str, ...]
    domains: tuple[str, ...]
    detail_path_pattern: str


REVIEWED_CAREER_DOMAINS: tuple[ReviewedCareerDomain, ...] = (
    ReviewedCareerDomain(
        # Observed employer page and Eluta -> Intact Workday handoff.
        company_aliases=("intact", "intact financial", "intact financial corporation"),
        domains=("careers.intactfc.com", "intactfc.wd3.myworkdayjobs.com"),
        detail_path_pattern=r"/job/",
    ),
    ReviewedCareerDomain(
        company_aliases=("atco", "atco energy systems", "atco electric", "atco gas and pipelines"),
        domains=("careers.atco.com",),
        detail_path_pattern=r"/global/en/job/\d+/",
    ),
    ReviewedCareerDomain(
        company_aliases=JIBE_CAREER_SITES[0].company_aliases,
        domains=("careers.amd.com",),
        detail_path_pattern=r"/careers-home/jobs/[^/]+",
    ),
    ReviewedCareerDomain(
        company_aliases=("hexagon", "hexagon autonomous solutions", "novatel", "novatel inc"),
        domains=("careers-hexagonpositioning.icims.com",),
        detail_path_pattern=r"/jobs/\d+/[^/]+/job",
    ),
    ReviewedCareerDomain(
        company_aliases=("amgen",),
        domains=("careers.amgen.com",),
        detail_path_pattern=r"/(?:[a-z]{2}/)?(?:job|trabajo)/",
    ),
    ReviewedCareerDomain(
        company_aliases=("bdo", "bdo canada"),
        domains=("bdo.wd3.myworkdayjobs.com",),
        detail_path_pattern=r"/job/",
    ),
    ReviewedCareerDomain(
        company_aliases=("pratt whitney", "pratt and whitney"),
        domains=("globalhr.wd5.myworkdayjobs.com",),
        detail_path_pattern=r"/job/",
    ),
    ReviewedCareerDomain(
        company_aliases=("walmart", "walmart canada"),
        domains=("careers.walmart.ca", "carrieres.walmart.ca"),
        detail_path_pattern=r"/(?:job|emploi)/",
    ),
    ReviewedCareerDomain(
        company_aliases=("suncor", "suncor energy"),
        domains=("suncor.wd1.myworkdayjobs.com",),
        detail_path_pattern=r"/job/",
    ),
    ReviewedCareerDomain(
        # The observed Workday JobPosting names the legal entity; its visible
        # description and Indeed posting use the Foresters Financial brand.
        company_aliases=("foresters financial", "foresters financial services",
                         "the independent order of foresters canada"),
        domains=("foresters.wd3.myworkdayjobs.com",),
        detail_path_pattern=r"/ForestersFinancialCareers/job/",
    ),
    ReviewedCareerDomain(
        # The observed Engineering Co-Op Indeed -> Workday handoff names the
        # IKO brand in its description and this plant in its JobPosting JSON-LD.
        company_aliases=("iko industries", "ig machine fibers"),
        domains=("iko.wd3.myworkdayjobs.com",),
        detail_path_pattern=r"/IKO_Careers/job/",
    ),
    ReviewedCareerDomain(
        # Observed Indeed -> Workday handoff: the Dotdash posting's JSON-LD
        # names Investopedia Canada and its description names People Inc.
        # Rebrand: https://news.people.inc/2025-07-31-Dotdash-Meredith-is-now-People-Inc
        company_aliases=("dotdash meredith", "people", "investopedia canada"),
        domains=("meredith.wd5.myworkdayjobs.com",),
        detail_path_pattern=r"/EXT/job/",
    ),
)


class ReviewedOfficialCareerSearchClient:
    """Try reviewed employer APIs and domain-constrained search before open search."""

    def __init__(
        self,
        *,
        search_client: SearchClient,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.search_client = search_client
        self.jibe_client = JibeCareerSiteSearchClient(
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
        self.recruitee_client = RecruiteeCareerSiteSearchClient(
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
        self.greenhouse_client = GreenhouseCareerSiteSearchClient(
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
        self.workday_client = WorkdayCareerSiteSearchClient(
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
        self.provider_code = "reviewed_official"
        self.last_error = ""

    def close(self) -> None:
        self.jibe_client.close()
        self.recruitee_client.close()
        self.greenhouse_client.close()
        self.workday_client.close()

    def search(self, hint: ExternalPostingHint, limit: int) -> list[SearchResult]:
        provider_error = ""
        provider_error_code = ""
        for client in (
            self.jibe_client,
            self.recruitee_client,
            self.greenhouse_client,
            self.workday_client,
        ):
            results = client.search(hint, limit)
            if results:
                self.provider_code = client.provider_code
                self.last_error = ""
                return results
            client_error = _actionable_site_error(client.last_error)
            if client_error and not provider_error:
                provider_error = client_error
                provider_error_code = client.provider_code

        site = reviewed_career_domain_for_company(hint.company)
        if site is None:
            self.last_error = provider_error or "unsupported_company"
            if provider_error:
                self.provider_code = provider_error_code
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        self.provider_code = f"{self.search_client.provider_code}_official_domain"
        search_limit = max(bounded_limit, 10)
        for query in _reviewed_domain_queries(site, hint):
            results = list(self.search_client.search(query, search_limit))
            self.last_error = str(getattr(self.search_client, "last_error", "") or "")
            matched = [
                result
                for result in results
                if any(_hostname_matches(result.url, domain) for domain in site.domains)
                and re.search(
                    site.detail_path_pattern,
                    urlparse(result.url).path,
                    flags=re.IGNORECASE,
                )
            ]
            if matched:
                self.last_error = ""
                return matched[:bounded_limit]
        if provider_error:
            self.provider_code = provider_error_code
            self.last_error = provider_error
        return []


class JibeCareerSiteSearchClient:
    """Search reviewed employer-owned Jibe/iCIMS career sites before web search."""

    provider_code = "jibe"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.last_error = ""

    def close(self) -> None:
        return None

    def search(self, hint: ExternalPostingHint, limit: int) -> list[SearchResult]:
        self.last_error = ""
        site = jibe_career_site_for_company(hint.company)
        if site is None:
            self.last_error = "unsupported_company"
            return []
        title = " ".join(hint.title.split())
        if not title:
            self.last_error = "missing_title"
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        try:
            with _http_client_scope(self.http_client, self.timeout_seconds) as http_client:
                response = http_client.get(
                    site.search_url,
                    params={
                        "keywords": title,
                        "page": 1,
                        "limit": bounded_limit,
                        "internal": "false",
                    },
                    headers={
                        "Accept": "application/json",
                        "Referer": f"{site.origin}{site.public_jobs_path}",
                        "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self.last_error = "official_site_request_failed"
            return []
        try:
            payload = response.json()
        except (TypeError, ValueError):
            self.last_error = "official_site_invalid_response"
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            self.last_error = "official_site_invalid_response"
            return []
        results = _jibe_search_results(site, payload["jobs"], hint)
        return results[:bounded_limit]


class RecruiteeCareerSiteSearchClient:
    """Search reviewed employer Recruitee feeds by exact normalized title."""

    provider_code = "recruitee"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.last_error = ""

    def close(self) -> None:
        return None

    def search(self, hint: ExternalPostingHint, limit: int) -> list[SearchResult]:
        self.last_error = ""
        site = recruitee_career_site_for_company(hint.company)
        if site is None:
            self.last_error = "unsupported_company"
            return []
        if not _normalized_title(hint.title):
            self.last_error = "missing_title"
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        try:
            with _http_client_scope(self.http_client, self.timeout_seconds) as http_client:
                response = http_client.get(
                    site.offers_url,
                    headers={
                        "Accept": "application/json",
                        "Referer": site.origin,
                        "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self.last_error = "official_site_request_failed"
            return []
        try:
            payload = response.json()
        except (TypeError, ValueError):
            self.last_error = "official_site_invalid_response"
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
            self.last_error = "official_site_invalid_response"
            return []
        return _recruitee_search_results(site, payload["offers"], hint)[:bounded_limit]


class GreenhouseCareerSiteSearchClient:
    """Search reviewed employer Greenhouse boards by exact normalized title."""

    provider_code = "greenhouse"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.last_error = ""

    def close(self) -> None:
        return None

    def search(self, hint: ExternalPostingHint, limit: int) -> list[SearchResult]:
        self.last_error = ""
        site = greenhouse_career_site_for_company(hint.company)
        if site is None:
            self.last_error = "unsupported_company"
            return []
        if not _normalized_title(hint.title):
            self.last_error = "missing_title"
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        try:
            with _http_client_scope(self.http_client, self.timeout_seconds) as http_client:
                response = http_client.get(
                    site.jobs_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self.last_error = "official_site_request_failed"
            return []
        try:
            payload = response.json()
        except (TypeError, ValueError):
            self.last_error = "official_site_invalid_response"
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            self.last_error = "official_site_invalid_response"
            return []
        return _greenhouse_search_results(site, payload["jobs"], hint)[:bounded_limit]


class WorkdayCareerSiteSearchClient:
    """Search reviewed employer Workday sites by exact normalized title."""

    provider_code = "workday"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.last_error = ""

    def close(self) -> None:
        return None

    def search(self, hint: ExternalPostingHint, limit: int) -> list[SearchResult]:
        self.last_error = ""
        site = workday_career_site_for_company(hint.company)
        if site is None:
            self.last_error = "unsupported_company"
            return []
        title = " ".join(hint.title.split())
        if not title:
            self.last_error = "missing_title"
            return []
        bounded_limit = max(1, min(int(limit), _MAX_RESULTS))
        try:
            with _http_client_scope(self.http_client, self.timeout_seconds) as http_client:
                response = http_client.post(
                    site.jobs_url,
                    json={
                        "appliedFacets": {},
                        "limit": _MAX_RESULTS,
                        "offset": 0,
                        "searchText": title,
                    },
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self.last_error = "official_site_request_failed"
            return []
        try:
            payload = response.json()
        except (TypeError, ValueError):
            self.last_error = "official_site_invalid_response"
            return []
        if not isinstance(payload, dict) or not isinstance(
            payload.get("jobPostings"), list
        ):
            self.last_error = "official_site_invalid_response"
            return []
        return _workday_search_results(site, payload["jobPostings"], hint)[:bounded_limit]


def jibe_career_site_for_company(company: str) -> JibeCareerSite | None:
    normalized = _normalized_company(company)
    if not normalized:
        return None
    for site in JIBE_CAREER_SITES:
        if normalized in site.company_aliases:
            return site
    return None


def recruitee_career_site_for_company(company: str) -> RecruiteeCareerSite | None:
    normalized = _normalized_company(company)
    if not normalized:
        return None
    for site in RECRUITEE_CAREER_SITES:
        if normalized in site.company_aliases:
            return site
    return None


def greenhouse_career_site_for_company(company: str) -> GreenhouseCareerSite | None:
    normalized = _normalized_company(company)
    if not normalized:
        return None
    for site in GREENHOUSE_CAREER_SITES:
        if normalized in site.company_aliases:
            return site
    return None


def workday_career_site_for_company(company: str) -> WorkdayCareerSite | None:
    normalized = _normalized_company(company)
    if not normalized:
        return None
    for site in WORKDAY_CAREER_SITES:
        if normalized in site.company_aliases:
            return site
    return None


def reviewed_career_domain_for_company(company: str) -> ReviewedCareerDomain | None:
    normalized = _normalized_company(company)
    if not normalized:
        return None
    for site in REVIEWED_CAREER_DOMAINS:
        if normalized in site.company_aliases:
            return site
    return None


def reviewed_company_aliases(company: str) -> tuple[str, ...]:
    """Explicit reviewed names only; never infer an employer from its initials."""
    normalized = _normalized_company(company)
    for site in (*JIBE_CAREER_SITES, *RECRUITEE_CAREER_SITES,
                 *GREENHOUSE_CAREER_SITES, *WORKDAY_CAREER_SITES, *REVIEWED_CAREER_DOMAINS):
        if normalized in site.company_aliases:
            return site.company_aliases
    return (company,)


def _hostname_matches(url: str, domain: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    normalized_domain = domain.casefold().rstrip(".")
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def _reviewed_domain_queries(
    site: ReviewedCareerDomain,
    hint: ExternalPostingHint,
) -> list[str]:
    if len(site.domains) == 1:
        domain_query = f"site:{site.domains[0]}"
    else:
        domain_query = f"({' OR '.join(f'site:{domain}' for domain in site.domains)})"
    title = " ".join(hint.title.split())
    title_variants = [title]
    title_variants.extend(
        part.strip(" -–—")
        for part in re.split(r"\s+\|\s+", title)
        if part.strip(" -–—")
    )
    queries: list[str] = []
    for variant in dict.fromkeys(title_variants):
        location = _location_city(hint.location)
        suffix = f" {location}" if location else ""
        queries.append(f'{domain_query} "{variant}"{suffix}')
    return queries


def _jibe_search_results(
    site: JibeCareerSite,
    rows: list[object],
    hint: ExternalPostingHint,
) -> list[SearchResult]:
    ranked: list[tuple[int, int, SearchResult]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
            continue
        data: dict[str, Any] = row["data"]
        title = str(data.get("title") or "").strip()
        job_id = str(data.get("slug") or data.get("req_id") or "").strip()
        if not title or not job_id:
            continue
        if _normalized_title(title) != _normalized_title(hint.title):
            continue
        company = str(data.get("hiring_organization") or hint.company).strip()
        location = str(
            data.get("full_location")
            or data.get("short_location")
            or data.get("location_name")
            or ""
        ).strip()
        result = SearchResult(
            title=title[:500],
            url=(
                f"{site.origin}{site.public_jobs_path}/"
                f"{quote(job_id, safe='')}"
            )[:2048],
            snippet=" | ".join(part for part in (company, location) if part)[:2000],
        )
        rank = 100
        if hint.location and _location_city(hint.location) in _normalized_title(location):
            rank += 10
        ranked.append((-rank, index, result))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [result for _rank, _index, result in ranked]


def _recruitee_search_results(
    site: RecruiteeCareerSite,
    rows: list[object],
    hint: ExternalPostingHint,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("careers_url") or "").strip()
        if _normalized_title(title) != _normalized_title(hint.title):
            continue
        if not _hostname_matches(url, urlparse(site.origin).hostname or ""):
            continue
        company = str(row.get("company_name") or hint.company).strip()
        location = str(row.get("location") or "").strip()
        results.append(
            SearchResult(
                title=title[:500],
                url=url[:2048],
                snippet=" | ".join(part for part in (company, location) if part)[:2000],
            )
        )
    return results


def _greenhouse_search_results(
    site: GreenhouseCareerSite,
    rows: list[object],
    hint: ExternalPostingHint,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    expected_host = "job-boards.greenhouse.io"
    expected_path_prefix = f"/{site.board_token}/jobs/"
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("absolute_url") or "").strip()
        if _normalized_title(title) != _normalized_title(hint.title):
            continue
        parsed = urlparse(url)
        if not _hostname_matches(url, expected_host) or not parsed.path.startswith(
            expected_path_prefix
        ):
            continue
        location_value = row.get("location")
        location = (
            str(location_value.get("name") or "").strip()
            if isinstance(location_value, dict)
            else ""
        )
        results.append(
            SearchResult(
                title=title[:500],
                url=url[:2048],
                snippet=" | ".join(part for part in (hint.company, location) if part)[:2000],
            )
        )
    return results


def _workday_search_results(
    site: WorkdayCareerSite,
    rows: list[object],
    hint: ExternalPostingHint,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        external_path = str(row.get("externalPath") or "").strip()
        if _normalized_title(title) != _normalized_title(hint.title):
            continue
        if not external_path.startswith("/job/"):
            continue
        location = str(row.get("locationsText") or "").strip()
        results.append(
            SearchResult(
                title=title[:500],
                url=f"{site.origin}/{site.site}{external_path}"[:2048],
                snippet=" | ".join(part for part in (hint.company, location) if part)[:2000],
            )
        )
    return results


def _normalized_company(value: str) -> str:
    normalized = " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
    return re.sub(r"\s+(?:inc|incorporated|corp|corporation|ltd|limited)$", "", normalized)


def _actionable_site_error(error: str) -> str:
    return "" if error in {"", "missing_title", "unsupported_company"} else error


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _location_city(value: str) -> str:
    return _normalized_title(value.split(",", 1)[0])
