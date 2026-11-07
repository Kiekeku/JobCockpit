from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_helpers import posting_html_text, select_posting_text

HANZILLA_HOST = "jobs.hanzilla.co"


@dataclass(frozen=True)
class HanzillaSite:
    origin: str
    path: str
    is_detail: bool = False

    @property
    def url(self) -> str:
        return f"{self.origin}{self.path}"


class HanzillaAdapter:
    provider = "hanzilla"

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=20)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_hanzilla_url(source_url)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_hanzilla_url(request.source_url)
        if site.is_detail:
            if request.excludes_source_url(site.url):
                return []
            if not request.try_begin_link_attempt():
                return []
            try:
                detail_html = self._get_html(site.url, request_kind="detail")
                return [_job_from_detail(site.url, detail_html)]
            except AtsProviderError as error:
                request.stats.record_link_failure(site.url, error)
                return []

        listing_html = self._get_html(site.url, request_kind="listing")
        seen_detail_urls: set[str] = set()
        detail_urls: list[str] = []
        if site.path == "/internships/":
            for category_url in _category_urls_from_internships(site.url, listing_html):
                try:
                    category_html = self._get_html(category_url, request_kind="category")
                except AtsProviderError as error:
                    if error.status_code == 404:
                        request.stats.record_link_failure(category_url, error)
                        continue
                    raise
                detail_urls.extend(
                    _detail_urls_from_listing(
                        category_url,
                        category_html,
                        request,
                        seen=seen_detail_urls,
                        internship_only=True,
                    )
                )
        detail_urls.extend(
            _detail_urls_from_listing(
                site.url,
                listing_html,
                request,
                seen=seen_detail_urls,
            )
        )
        def fetch_detail(url):
            return _job_from_detail(url, self._get_html(url, request_kind="detail"))

        return fetch_details(request, (
            (url, lambda url=url: fetch_detail(url)) for url in detail_urls
        ))

    def _get_html(self, url: str, *, request_kind: str) -> str:
        try:
            response = self.http_client.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if request_kind == "category" and error.response.status_code == 404:
                raise AtsProviderError(404, "Hanzilla category page was not found") from error
            raise AtsProviderError(502, f"Hanzilla {request_kind} request failed") from error
        except httpx.HTTPError as error:
            raise AtsProviderError(502, f"Hanzilla {request_kind} request failed") from error
        return response.text


def parse_hanzilla_url(source_url: str) -> HanzillaSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.scheme not in {"http", "https"} or hostname != HANZILLA_HOST:
        raise AtsProviderError(400, "Only jobs.hanzilla.co URLs are supported")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"
    return HanzillaSite(
        origin=f"{parsed.scheme}://{parsed.netloc}",
        path=path,
        is_detail=path.startswith("/jobs/"),
    )


def _detail_urls_from_listing(
    listing_url: str,
    html: str,
    request: AtsFetchRequest,
    *,
    seen: set[str] | None = None,
    internship_only: bool = False,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".job-card")
    if not cards:
        raise AtsProviderError(502, "Hanzilla listing page did not include job cards")

    listing_site = parse_hanzilla_url(listing_url)
    terms = _search_terms(request.search_text)
    urls: list[str] = []
    seen = seen if seen is not None else set()
    for card in cards:
        if internship_only and str(card.get("data-stage") or "").casefold() != "internship":
            continue
        if terms and not _card_matches(card, terms):
            continue
        link = card.select_one("a.job-link[href]")
        if not link:
            continue
        detail_url = _validated_detail_url(listing_site, link.get("href"))
        if not detail_url:
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        if request.excludes_source_url(detail_url):
            continue
        title = _select_text(card, ".job-title") or str(card.get("data-title") or "").strip()
        if not request.allows_listing_candidate(detail_url, title):
            continue
        urls.append(detail_url)
    return urls


def _category_urls_from_internships(listing_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    listing_site = parse_hanzilla_url(listing_url)
    urls: list[str] = []
    seen: set[str] = set()
    for link in soup.select(".category-view-all a[href]"):
        category_url = urljoin(listing_site.url, str(link.get("href") or ""))
        try:
            category_site = parse_hanzilla_url(category_url)
        except AtsProviderError:
            continue
        if (
            category_site.origin != listing_site.origin
            or not category_site.path.startswith("/categories/")
            or category_site.url in seen
        ):
            continue
        seen.add(category_site.url)
        urls.append(category_site.url)
    return urls


def _validated_detail_url(listing_site: HanzillaSite, href: object | None) -> str:
    detail_url = urljoin(listing_site.url, str(href or ""))
    try:
        detail_site = parse_hanzilla_url(detail_url)
    except AtsProviderError:
        return ""
    if not detail_site.is_detail or detail_site.origin != listing_site.origin:
        return ""
    return detail_site.url


def _job_from_detail(detail_url: str, html: str) -> AtsJob:
    soup = BeautifulSoup(html, "html.parser")
    job_posting = _job_posting_json(soup)
    apply_url = _apply_url(soup)
    title = _first_text(job_posting.get("title"), _select_text(soup, ".job-header h1"))
    if not title:
        raise AtsProviderError(502, "Hanzilla detail page did not include a job title")

    company = _company_name(job_posting) or _clean_company(_select_text(soup, ".company-name"))
    location = _location(job_posting) or _meta_value(soup, "Location")
    country = _country(job_posting)
    category = _select_text(soup, ".badge-category")
    stage = _select_text(soup, ".badge-intern") or str(job_posting.get("employmentType") or "").strip()
    details = _meta_value(soup, "Details")
    posted = _meta_value(soup, "Posted") or str(job_posting.get("datePosted") or "").strip()
    description = select_posting_text(soup, ".description-content") or posting_html_text(str(job_posting.get("description") or ""))
    summaries = [_clean_text(block.get_text(" ", strip=True)) for block in soup.select(".summary-block")]

    fields = [
        title,
        _line("Company", company),
        _line("Location", location),
        _line("Country", country),
        _line("Category", category),
        _line("Stage", stage),
        _line("Details", details),
        _line("Posted", posted),
        _line("Hanzilla URL", detail_url),
        _line("Apply URL", apply_url),
        *summaries,
        description,
    ]
    raw_text = "\n".join(field for field in fields if field).strip()
    return AtsJob(
        source_url=apply_url or detail_url,
        raw_text=raw_text,
        portal_source_url=detail_url,
        content_source_url=detail_url,
    )


def _job_posting_json(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        content = script.string or script.get_text() or ""
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        for item in _json_items(payload):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _json_items(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            return graph
        return [payload]
    return []


def _apply_url(soup: BeautifulSoup) -> str:
    apply_link = soup.select_one("a.btn-apply[href]")
    if not apply_link:
        return ""
    return str(apply_link.get("href") or "").strip()


def _company_name(job_posting: dict) -> str:
    organization = job_posting.get("hiringOrganization")
    if isinstance(organization, dict):
        return str(organization.get("name") or "").strip()
    return ""


def _location(job_posting: dict) -> str:
    places = _location_places(job_posting)
    countries = [_country_from_place(place) for place in places]
    include_country = bool(any(countries) and not _shared_country(countries))
    locations = [
        _location_from_place(place, include_country=include_country)
        for place in places
    ]
    return "; ".join(location for location in locations if location)


def _location_places(job_posting: dict) -> list[dict]:
    location = job_posting.get("jobLocation")
    if isinstance(location, list):
        return [place for place in location if isinstance(place, dict)]
    return [location] if isinstance(location, dict) else []


def _location_from_place(place: dict, *, include_country: bool = False) -> str:
    address = place.get("address")
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
    ]
    if include_country:
        parts.append(_country_from_place(place))
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _country(job_posting: dict) -> str:
    countries = [_country_from_place(place) for place in _location_places(job_posting)]
    return _shared_country(countries)


def _shared_country(countries: list[str]) -> str:
    return countries[0] if countries and countries[0] and len(set(countries)) == 1 else ""


def _country_from_place(place: dict) -> str:
    address = place.get("address")
    if not isinstance(address, dict):
        return ""
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id")
    return str(country or "").strip()


def _meta_value(soup: BeautifulSoup, label: str) -> str:
    for meta in soup.select(".meta-item"):
        text = _clean_text(meta.get_text(" ", strip=True))
        if text.lower().startswith(label.lower()):
            return text[len(label) :].strip(" :-")
    return ""


def _card_matches(card, terms: list[str]) -> bool:
    haystack = " ".join(
        [
            str(card.get("data-search-text") or ""),
            str(card.get("data-title") or ""),
            str(card.get("data-company") or ""),
            str(card.get("data-location") or ""),
            _clean_text(card.get_text(" ", strip=True)),
        ]
    ).lower()
    return all(term in haystack for term in terms)


def _search_terms(search_text: str) -> list[str]:
    return [term for term in search_text.lower().split() if term]


def _select_text(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    if not element:
        return ""
    return _clean_text(element.get_text(" ", strip=True))


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _clean_company(text: str) -> str:
    text = text.strip()
    return text[3:].strip() if text.lower().startswith("at ") else text


def _clean_text(text: str) -> str:
    return " ".join(text.replace("\U0001f4cd", "").split())


def _line(label: str, value: object | None) -> str:
    text = str(value or "").strip()
    return f"{label}: {text}" if text else ""
