from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlparse
import unicodedata

import httpx
from bs4 import BeautifulSoup

from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsProviderError,
    AtsRecordedLinkFailureError,
    SourceArchive,
)
from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
    OracleCandidateExperienceAdapter,
    parse_oracle_candidate_experience_url,
)
from jobcockpit.ingestion.ats_adapters.workday import WorkdayAdapter, WorkdaySite, parse_workday_url
from jobcockpit.ingestion.ats_helpers import (
    clean_text,
    enforce_url_policy,
    first_text,
    line,
    posting_html_text,
    request_headers,
    select_text,
    source_archive_from_response,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy
from jobcockpit.ingestion.posting_completeness import IncompletePostingError, require_complete_posting
from jobcockpit.ingestion.ingestion_identity import PostingVerificationRequired, is_verification_url

MAX_APPLY_REDIRECTS = 5
HTTP_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class MetadataOnlyPostingError(IncompletePostingError):
    """The HTTP page exposes a preview, but no posting body."""


@dataclass(frozen=True)
class AtsPostingContext:
    source_label: str = "Source"
    source_url: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    date_posted: str = ""
    apply_url: str = ""


class AtsApplyPageResolver:
    def __init__(
        self,
        http_client: httpx.Client | None = None,
        url_policy: UrlFetchPolicy | None = None,
        timeout: float = 20,
        *,
        eluta_adapter_factory=None,
    ) -> None:
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.url_policy = url_policy or UrlFetchPolicy()
        self.eluta_adapter_factory = eluta_adapter_factory

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()

    def resolve(self, apply_url: str, context: AtsPostingContext | None = None) -> AtsJob:
        context = context or AtsPostingContext()
        apply_url = apply_url.strip()
        context = context if context.apply_url else _context_with_apply_url(context, apply_url)
        parsed_url = urlparse(apply_url)
        if parsed_url.hostname in {"eluta.ca", "www.eluta.ca"} and parsed_url.path.startswith("/spl/"):
            return self._resolve_eluta_apply(apply_url, context)
        final_url, html, source_archive, workday_job = self._get_apply_page(apply_url)
        if workday_job:
            return _with_context(workday_job, context)
        job = _job_from_generic_apply_page(final_url, html, context, source_archive)
        if job.observed_raw_text is not None and not job.observed_raw_text.strip():
            raise IncompletePostingError(502, "Apply page contains no posting content; full-page retrieval required")
        require_complete_posting(job.observed_raw_text or job.raw_text, final_url)
        if job.has_posting_body is False and _has_description_metadata(BeautifulSoup(html, "html.parser")):
            raise MetadataOnlyPostingError(502, "Apply page contains only a metadata preview; full-page retrieval required")
        return job

    def _resolve_eluta_apply(self, source_url: str, context: AtsPostingContext) -> AtsJob:
        # Lazy imports avoid the Eluta browser-loader / discovery import cycle.
        from jobcockpit.ingestion.ats_adapters.eluta import ElutaAdapter
        from jobcockpit.ingestion.posting_discovery import (
            ExternalPostingHint,
            _official_candidate_url_matches_hint,
            _official_posting_confidence,
        )

        enforce_url_policy(self.url_policy, source_url, label="Eluta")
        adapter = (self.eluta_adapter_factory or ElutaAdapter)()
        try:
            detail = adapter.resolve_detail(source_url)
        finally:
            adapter.close()
        destination = detail.source_url
        if not destination or urlparse(destination).hostname in {"eluta.ca", "www.eluta.ca"}:
            raise AtsProviderError(502, "Eluta employer apply link unavailable")
        # Never inject the listing's identity into the evidence used to validate
        # the destination. Context is appended only after independent validation.
        final_url, html, archive, job = self._get_apply_page(destination)
        job = job or _job_from_generic_apply_page(final_url, html, AtsPostingContext(), archive)
        observed = job.observed_raw_text if job.observed_raw_text is not None else job.raw_text
        # A brand in an Apply URL is not an employer declaration in the body.
        observed = re.sub(r"https?://\S+", "", observed)
        def field(label):
            match = re.search(rf"(?im)^{label}:\s*(.+)$", detail.raw_text)
            return match.group(1).strip() if match else ""

        def unaccent(value):
            return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))

        company = context.company or field("Company")
        # Eluta explicitly displays legal-name / brand aliases in its header.
        aliases = [company, *re.split(r"\s+/\s+", company)]
        valid = False
        for alias in dict.fromkeys(aliases):
            hint = ExternalPostingHint(
                source_url=source_url, source_kind="eluta", company=unaccent(alias),
                title=unaccent(context.role or detail.title or ""),
                location=unaccent(context.location or field("Location")),
            )
            if (_official_candidate_url_matches_hint(job.source_url, hint)
                    and job.has_posting_body is not False
                    and len(observed.split()) >= 80
                    and _official_posting_confidence(hint, unaccent(observed)) >= 0.8):
                valid = True
                break
        if not valid:
            raise AtsProviderError(502, "Eluta employer posting identity could not be verified")
        return replace(_with_context(job, context), portal_source_url=source_url,
                       content_source_url=job.source_url)

    def _get_apply_page(self, apply_url: str) -> tuple[str, str, SourceArchive | None, AtsJob | None]:
        current_url = apply_url
        redirects_followed = 0
        try:
            while True:
                if is_verification_url(current_url):
                    # A 200 response here is a challenge, never a posting. Do
                    # not fetch or persist session-specific verification URLs.
                    raise PostingVerificationRequired()
                enforce_url_policy(self.url_policy, current_url, label="Apply")
                workday_job = _workday_job_from_url(self.http_client, current_url)
                if workday_job:
                    return current_url, "", workday_job.source_archive, workday_job
                oracle_candidate_experience_job = _oracle_candidate_experience_job_from_url(
                    self.http_client, current_url
                )
                if oracle_candidate_experience_job:
                    return (
                        current_url,
                        "",
                        oracle_candidate_experience_job.source_archive,
                        oracle_candidate_experience_job,
                    )
                response = self.http_client.get(current_url, headers=request_headers("text/html"), follow_redirects=False)
                if response.status_code in HTTP_REDIRECT_STATUS_CODES and response.headers.get("Location"):
                    if redirects_followed >= MAX_APPLY_REDIRECTS:
                        raise AtsProviderError(502, "Apply redirect limit exceeded")
                    current_url = urljoin(current_url, response.headers["Location"])
                    redirects_followed += 1
                    continue
                response.raise_for_status()
                return current_url, response.text, source_archive_from_response(apply_url, response, final_url=current_url, default_content_type="application/octet-stream"), None
        except AtsProviderError:
            raise
        except httpx.HTTPError as error:
            raise AtsProviderError(502, "Apply page request failed") from error


def _context_with_apply_url(context: AtsPostingContext, apply_url: str) -> AtsPostingContext:
    return AtsPostingContext(
        source_label=context.source_label,
        source_url=context.source_url,
        company=context.company,
        role=context.role,
        location=context.location,
        date_posted=context.date_posted,
        apply_url=apply_url,
    )


def _workday_site(url: str) -> WorkdaySite | None:
    try:
        return parse_workday_url(url)
    except AtsProviderError:
        return None


def _workday_job_from_url(http_client: httpx.Client, url: str) -> AtsJob | None:
    workday_site = _workday_site(url)
    if not workday_site:
        return None
    if not workday_site.external_path:
        raise AtsProviderError(400, "Workday apply URL must be a job detail URL")
    request = AtsFetchRequest(source_url=url, limit=1)
    jobs = WorkdayAdapter(http_client=http_client).fetch_jobs(request)
    if jobs:
        return jobs[0]
    if request.stats.link_failures:
        raise AtsRecordedLinkFailureError(request.stats.link_failures[0])
    return None


def _oracle_candidate_experience_job_from_url(http_client: httpx.Client, url: str) -> AtsJob | None:
    try:
        parse_oracle_candidate_experience_url(url)
    except AtsProviderError:
        return None
    try:
        jobs = OracleCandidateExperienceAdapter(http_client=http_client).fetch_jobs(AtsFetchRequest(source_url=url, limit=1))
        return jobs[0] if jobs else None
    except AtsProviderError:
        return None


def _with_context(job: AtsJob, context: AtsPostingContext) -> AtsJob:
    context_text = _context_text(context)
    raw_text = "\n".join(part for part in [job.raw_text, context_text] if part).strip()
    return AtsJob(
        source_url=job.source_url or context.apply_url,
        raw_text=raw_text,
        portal_source_url=job.portal_source_url,
        source_archive=job.source_archive,
        title=job.title,
        provider_job_id=job.provider_job_id,
        observed_raw_text=job.observed_raw_text if job.observed_raw_text is not None else job.raw_text,
        has_posting_body=job.has_posting_body,
        content_source_url=job.content_source_url,
    )


def _job_from_generic_apply_page(
    final_url: str,
    html: str,
    context: AtsPostingContext,
    source_archive: SourceArchive | None = None,
) -> AtsJob:
    if is_verification_url(final_url):
        raise PostingVerificationRequired()
    soup = BeautifulSoup(html, "html.parser")
    job_posting = _job_posting_json(soup)
    observed_title = first_text(
        job_posting.get("title"),
        select_text(soup, "h1"),
        _meta_content(soup, "property", "og:title"),
        _title_text(soup),
    )
    observed_company = first_text(
        _company_name(job_posting),
        _meta_content(soup, "property", "og:site_name"),
        _visible_labeled_value(soup, "Company"),
    )
    observed_location = first_text(_location(job_posting), _visible_labeled_value(soup, "Location"))
    # Workday's JSON-LD can expose only one (remote) location while the actual
    # posting lists its city too. Keep that independent DOM evidence for source
    # verification without changing the ingestion text's existing precedence.
    hostname = (urlparse(final_url).hostname or '').lower()
    if hostname.endswith(('.myworkdayjobs.com', '.myworkdaysite.com')):
        visible_locations = [clean_text(node.get_text(' ', strip=True)) for node in soup.select(
            '[data-automation-id="job-posting-details"] [data-automation-id="locations"] dd'
        )]
        observed_location = '; '.join(dict.fromkeys(filter(None, [observed_location, *visible_locations])))
    # Preserve ingestion text precedence; independent evidence is separate below.
    title = first_text(job_posting.get("title"), _meta_content(soup, "property", "og:title"),
                       select_text(soup, "h1"), _title_text(soup), context.role)
    company = first_text(observed_company, context.company)
    location = first_text(_location(job_posting), context.location)
    posted = first_text(job_posting.get("datePosted"), context.date_posted)
    description = _description_text(soup, job_posting)
    fields = [
        title,
        line("Company", company),
        line("Location", location),
        line("Posted", posted),
        line("Apply URL", final_url),
        _context_text(context),
        description,
    ]
    return AtsJob(
        source_url=final_url,
        raw_text="\n".join(field for field in fields if field).strip(),
        source_archive=source_archive,
        title=observed_title or None,
        observed_raw_text="\n".join(filter(None, [
            line("Title", observed_title), line("Company", observed_company),
            line("Location", observed_location), description,
        ])),
        has_posting_body=bool(
            posting_html_text(str(job_posting.get("description") or "")).strip()
            or len(_main_text(soup).split()) >= 5
        ),
    )


def _job_posting_json(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        content = script.string or script.get_text() or ""
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        for item in _json_items(payload):
            if isinstance(item, dict) and _is_job_posting_type(item.get("@type")):
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


def _is_job_posting_type(value: object) -> bool:
    if isinstance(value, list):
        return any(str(item) == "JobPosting" for item in value)
    return str(value) == "JobPosting"


def _company_name(job_posting: dict) -> str:
    organization = job_posting.get("hiringOrganization")
    if isinstance(organization, dict):
        return str(organization.get("name") or "").strip()
    return str(organization or "").strip()


def _visible_labeled_value(soup: BeautifulSoup, label: str) -> str:
    lines = soup.get_text("\n", strip=True).splitlines()
    for index, text in enumerate(lines):
        match = re.fullmatch(rf"\s*{re.escape(label)}\s*:[ \t]*(.*)", text, re.IGNORECASE)
        if not match:
            continue
        value = match[1].strip() or (lines[index + 1].strip() if index + 1 < len(lines) else "")
        # Empty template labels may precede another field before the real job.
        if value and not re.match(r"^[\w /&-]+\s*:", value):
            return clean_text(value)
    return ""


def _location(job_posting: dict) -> str:
    location = job_posting.get("jobLocation")
    if isinstance(location, list):
        locations = [_location_from_place(place) for place in location if isinstance(place, dict)]
        return "; ".join(location for location in locations if location)
    if isinstance(location, dict):
        return _location_from_place(location)
    return str(location or "").strip()


def _location_from_place(place: dict) -> str:
    address = place.get("address")
    if not isinstance(address, dict):
        return str(place.get("name") or "").strip()
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id")
    region = address.get("addressRegion")
    # Some providers include the country code in the region, e.g. "ON,CA".
    if isinstance(region, str) and isinstance(country, str) and "," in region:
        if region.rsplit(",", 1)[-1].strip().casefold() == country.strip().casefold():
            country = None
    parts = [
        address.get("addressLocality"),
        region,
        country,
    ]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _context_text(context: AtsPostingContext) -> str:
    label = context.source_label.strip() or "Source"
    fields = [
        line(f"{label} Company", context.company),
        line(f"{label} Role", context.role),
        line(f"{label} Location", context.location),
        line(f"{label} Date Posted", context.date_posted),
        line(f"{label} URL", context.source_url),
        line(f"{label} Apply URL", context.apply_url),
    ]
    return "\n".join(field for field in fields if field)












def _title_text(soup: BeautifulSoup) -> str:
    return clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""


def _description_text(soup: BeautifulSoup, job_posting: dict) -> str:
    parts = [
        posting_html_text(str(job_posting.get("description") or "")),
        _meta_content(soup, "property", "og:description"),
        _meta_content(soup, "name", "description"),
        _main_text(soup),
    ]
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_parts.append(part)
    return "\n".join(unique_parts)


def _has_description_metadata(soup: BeautifulSoup) -> bool:
    return bool(
        _meta_content(soup, "property", "og:description")
        or _meta_content(soup, "name", "description")
    )


def _meta_content(soup: BeautifulSoup, attribute: str, value: str) -> str:
    element = soup.find("meta", attrs={attribute: value})
    if not element:
        return ""
    return clean_text(str(element.get("content") or ""))


def _main_text(soup: BeautifulSoup) -> str:
    element = soup.select_one("main") or soup.body
    return posting_html_text(str(element)) if element else ""
