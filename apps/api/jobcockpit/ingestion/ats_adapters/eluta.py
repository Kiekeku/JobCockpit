from __future__ import annotations

import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.ats_adapters.parallel import fetch_details
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest, AtsJob, AtsProviderError, AtsVerificationRequiredError,
)
from jobcockpit.ingestion.ats_helpers import line, select_posting_text, select_text
from jobcockpit.ingestion.pinned_http import PinnedHttpClient, UrlPolicyViolation
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    ExternalPostingHint,
    PrioritizedRenderedPageResolver,
    RenderedPageLoader,
)

ELUTA_HOSTS = {"www.eluta.ca", "eluta.ca"}
ELUTA_MAX_REDIRECTS = 5
HTTP_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ElutaSite:
    origin: str
    path: str
    query: str = ""
    is_detail: bool = False

    @property
    def url(self) -> str:
        suffix = f"?{self.query}" if self.query else ""
        return f"{self.origin}{self.path}{suffix}"


class ElutaAdapter:
    provider = "eluta"
    _request_interval_lock = threading.Lock()
    _next_request_at = 0.0

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        rendered_page_loader: RenderedPageLoader | None = None,
        chrome_cdp_endpoint_url: str | None = None,
        chrome_cdp_launcher: Callable[[str], None] | None = None,
        chrome_cdp_auto_start: bool | None = None,
        chrome_cdp_executable_path: str | Path | None = None,
        apply_http_client: httpx.Client | None = None,
        playwright_timeout_seconds: int | None = None,
    ) -> None:
        settings = Settings()
        self.settings = settings
        self._owns_http_client = http_client is None and rendered_page_loader is None
        self.http_client = (
            PinnedHttpClient(
                timeout=20,
                follow_redirects=False,
                headers=_headers(),
                ssl_context=_eluta_ssl_context(),
            )
            if self._owns_http_client
            else http_client
        )
        self._browser_fallback_enabled = self._owns_http_client or (
            http_client is not None and rendered_page_loader is not None
        )
        self._http_page_fetch_enabled = self.http_client is not None
        self.apply_http_client = apply_http_client
        self.playwright_factory = playwright_factory
        self.chrome_cdp_endpoint_url = chrome_cdp_endpoint_url or settings.chrome_cdp_endpoint_url
        self.chrome_cdp_launcher = chrome_cdp_launcher
        self.chrome_cdp_auto_start = (
            chrome_cdp_auto_start
            if chrome_cdp_auto_start is not None
            else settings.chrome_cdp_auto_start
        )
        configured_chrome_path = chrome_cdp_executable_path or settings.chrome_cdp_executable_path
        self.chrome_cdp_executable_path = (
            Path(configured_chrome_path) if configured_chrome_path else None
        )
        self.chrome_cdp_user_data_dir = settings.chrome_cdp_user_data_dir
        self.chrome_cdp_profile_directory = settings.chrome_cdp_profile_directory
        self.chrome_cdp_headless = settings.playwright_headless
        self.playwright_timeout_seconds = (
            playwright_timeout_seconds or settings.posting_rendered_fallback_timeout_seconds
        )
        self.playwright_auth_wall_wait_seconds = settings.playwright_auth_wall_wait_seconds
        self.playwright_auth_wall_settle_seconds = settings.playwright_auth_wall_settle_seconds
        self.request_min_interval_seconds = (
            settings.posting_rendered_fallback_min_interval_seconds
            if self._owns_http_client
            else 0.0
        )
        self.rendered_page_loader = rendered_page_loader

    def close(self) -> None:
        cancel = getattr(self.rendered_page_loader, "cancel_active_pages", None)
        if callable(cancel):
            cancel()
        if self._owns_http_client and self.http_client is not None:
            self.http_client.close()

    def validate_source_url(self, source_url: str) -> None:
        parse_eluta_url(source_url)

    def resolve_detail(self, source_url: str) -> AtsJob:
        """Read an Eluta detail and resolve its apply redirect, including browser fallback."""
        site = parse_eluta_url(source_url)
        if not site.is_detail:
            raise AtsProviderError(400, "Eluta apply resolution requires a detail URL")
        html = self._get_html(site.url, request_kind="detail")
        return _job_from_detail(self, site.url, html)

    def fetch_jobs(self, request: AtsFetchRequest) -> list[AtsJob]:
        site = parse_eluta_url(request.source_url)
        if site.is_detail:
            if request.excludes_source_url(site.url):
                return []
            if not request.try_begin_link_attempt():
                return []
            try:
                return [self.resolve_detail(site.url)]
            except AtsProviderError as error:
                if not _is_skippable_detail_error(error):
                    raise
                request.stats.record_link_failure(site.url, error)
                return []

        jobs: list[AtsJob] = []
        seen: set[str] = set()
        page = _initial_page(site)
        while len(jobs) < request.limit and request.remaining_link_attempts:
            page_url = _search_page_url(site, page=page, search_text=request.search_text)
            try:
                listing_html = self._get_html(page_url, request_kind="search")
            except AtsProviderError as error:
                if jobs and _is_recoverable_later_search_error(error):
                    request.stats.record_link_failure(page_url, error)
                    break
                raise
            soup = BeautifulSoup(listing_html, "html.parser")
            listing_candidates = _listing_candidates_from_search_page(site, soup)
            if not listing_candidates:
                if page == _initial_page(site):
                    raise AtsProviderError(502, "Eluta search page did not include job results")
                break

            # Eluta shares challenge/session state across pages. Serialize this
            # boundary, but overlap redirect resolution and subsequent imports.
            page_lock = threading.Lock()
            stopped = threading.Event()

            def fetch_detail(url):
                with page_lock:
                    if stopped.is_set():
                        return None
                    try:
                        html = self._get_html(url, request_kind="detail")
                    except AtsProviderError as error:
                        if _should_stop_after_detail_error(error):
                            stopped.set()
                        raise
                return _job_from_detail(self, url, html)

            def candidates():
                for detail_url, title in listing_candidates:
                    if stopped.is_set():
                        break
                    if detail_url in seen:
                        continue
                    seen.add(detail_url)
                    if request.excludes_source_url(detail_url):
                        continue
                    if not request.allows_listing_candidate(detail_url, title):
                        continue
                    yield detail_url, lambda url=detail_url: fetch_detail(url)

            jobs.extend(fetch_details(request, candidates()))
            if stopped.is_set():
                return jobs

            if len(jobs) >= request.limit:
                break
            next_page = _next_page(soup)
            if next_page is None or next_page <= page:
                break
            page = next_page
        return jobs

    def resolve_apply_url(self, direct_url: str) -> str:
        direct_site = parse_eluta_direct_url(direct_url)
        apply_client = self.apply_http_client or self.http_client
        if apply_client is not None:
            if apply_client is self.http_client and self._owns_http_client:
                self._wait_for_request_slot()
            return _resolve_apply_redirect_url(direct_site, apply_client)

        apply_client = PinnedHttpClient(
            timeout=20,
            follow_redirects=False,
            headers=_headers(),
            ssl_context=_eluta_ssl_context(),
        )
        try:
            return _resolve_apply_redirect_url(direct_site, apply_client)
        finally:
            apply_client.close()

    def _get_html(self, url: str, *, request_kind: str) -> str:
        if not self._http_page_fetch_enabled:
            return self._get_html_with_paced_playwright(url, request_kind=request_kind)
        if self._owns_http_client:
            self._wait_for_request_slot()
        try:
            html = self._get_html_with_httpx(url, request_kind=request_kind)
        except AtsProviderError as error:
            if not self._browser_fallback_enabled or not _should_fallback_to_rendered(error):
                raise
            logger.info(
                "Eluta %s HTTP fetch was unavailable; using rendered fallback: %s",
                request_kind,
                error.detail,
            )
            self._http_page_fetch_enabled = False
            return self._get_html_with_paced_playwright(url, request_kind=request_kind)
        if self._browser_fallback_enabled and not _http_html_has_expected_content(
            html, request_kind=request_kind
        ):
            logger.info(
                "Eluta %s HTTP fetch lacked expected content; using rendered fallback",
                request_kind,
            )
            self._http_page_fetch_enabled = False
            return self._get_html_with_paced_playwright(url, request_kind=request_kind)
        return html

    def _get_html_with_paced_playwright(self, url: str, *, request_kind: str) -> str:
        if self._owns_http_client:
            self._wait_for_request_slot()
        return self._get_html_with_playwright(url, request_kind=request_kind)

    def _wait_for_request_slot(self) -> None:
        if self.request_min_interval_seconds <= 0:
            return
        with self._request_interval_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self.__class__._next_request_at - now)
            self.__class__._next_request_at = (
                max(now, self.__class__._next_request_at) + self.request_min_interval_seconds
            )
        if wait_seconds:
            time.sleep(wait_seconds)

    def _get_html_with_httpx(self, url: str, *, request_kind: str) -> str:
        if self.http_client is None:
            raise AtsProviderError(502, f"Eluta {request_kind} request failed")
        current_url = url
        redirects_followed = 0
        try:
            while True:
                response = self.http_client.get(
                    current_url, headers=_headers(), follow_redirects=False
                )
                if response.status_code in HTTP_REDIRECT_STATUS_CODES and response.headers.get(
                    "Location"
                ):
                    if redirects_followed >= ELUTA_MAX_REDIRECTS:
                        raise AtsProviderError(502, f"Eluta {request_kind} redirect limit exceeded")
                    next_url = _validated_html_redirect_url(
                        url,
                        current_url,
                        response.headers["Location"],
                        request_kind=request_kind,
                    )
                    if not next_url:
                        if _is_user_verification_redirect(
                            current_url,
                            response.headers["Location"],
                            request_kind=request_kind,
                        ):
                            raise AtsVerificationRequiredError(
                                f"Eluta {request_kind} page required user verification"
                            )
                        raise AtsProviderError(
                            502, f"Eluta {request_kind} redirect target was not allowed"
                        )
                    current_url = next_url
                    redirects_followed += 1
                    continue
                response.raise_for_status()
                return response.text
        except AtsProviderError:
            raise
        except UrlPolicyViolation as error:
            raise AtsProviderError(
                502, f"Eluta {request_kind} redirect target was not allowed"
            ) from error
        except httpx.HTTPError as error:
            raise AtsProviderError(502, f"Eluta {request_kind} request failed") from error

    def _get_html_with_playwright(self, url: str, *, request_kind: str) -> str:
        loader = self._shared_rendered_page_loader()
        identity_query_groups: tuple[tuple[str, ...], ...] = ()
        if request_kind == "search":
            query = parse_qs(urlparse(url).query, keep_blank_values=True)
            identity_query_groups = tuple(
                (key,) for key in ("q", "pg") if key in query
            )
        resolution = loader.load_page(
            ExternalPostingHint(
                source_url=url,
                source_kind=self.provider,
                url_identity="path_and_query" if request_kind == "search" else "path",
                identity_query_groups=identity_query_groups,
            )
        )
        posting = resolution.posting
        if resolution.status != "resolved" or posting is None:
            logger.warning(
                "Eluta %s rendered load failed: %s",
                request_kind,
                _sanitized_rendered_error(resolution.error),
            )
            raise _rendered_loader_error(resolution.error, request_kind=request_kind)

        final_url = posting.source_url or url
        if not _is_allowed_rendered_html_url(final_url, request_kind=request_kind):
            if _is_user_verification_url(final_url, request_kind=request_kind):
                raise AtsVerificationRequiredError(
                    f"Eluta {request_kind} page required user verification",
                )
            raise AtsProviderError(
                502,
                f"Eluta {request_kind} redirect target was not allowed",
            )
        if not posting.rendered_html:
            raise AtsProviderError(502, f"Eluta {request_kind} request failed")
        return posting.rendered_html

    def _shared_rendered_page_loader(self) -> RenderedPageLoader:
        if self.rendered_page_loader is not None:
            return self.rendered_page_loader

        if self.settings.posting_rendered_fallback_provider == "invisible_playwright":
            from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver

            self.rendered_page_loader = InvisiblePlaywrightRenderedPageResolver.from_settings(
                self.settings,
                timeout_seconds=self.playwright_timeout_seconds,
                retries=0,
                min_start_interval_seconds=0.0,
            )
            return self.rendered_page_loader

        if self.settings.posting_rendered_fallback_provider not in {
            "chrome_cdp", "chrome_cdp_first", "invisible_playwright_first",
        }:
            raise AtsProviderError(503, "Eluta rendered provider is disabled or unsupported")

        chrome_launcher = None
        configured_launcher = self.chrome_cdp_launcher
        if configured_launcher is not None:

            def launch_configured_chrome() -> None:
                configured_launcher(self.chrome_cdp_endpoint_url)

            chrome_launcher = launch_configured_chrome
        try:
            self.rendered_page_loader = ChromeCdpRenderedPageResolver(
                endpoint_url=self.chrome_cdp_endpoint_url,
                auth_wall_wait_seconds=self.playwright_auth_wall_wait_seconds,
                auth_wall_settle_seconds=self.playwright_auth_wall_settle_seconds,
                timeout_seconds=self.playwright_timeout_seconds,
                retries=0,
                min_start_interval_seconds=0.0,
                auto_start=self.chrome_cdp_auto_start,
                executable_path=self.chrome_cdp_executable_path,
                user_data_dir=self.chrome_cdp_user_data_dir,
                profile_directory=self.chrome_cdp_profile_directory,
                headless=self.chrome_cdp_headless,
                persistent_source_session=True,
                chrome_launcher=chrome_launcher,
                playwright_factory=self.playwright_factory,
            )
        except ValueError as error:
            raise _eluta_cdp_configuration_error(error) from error
        if self.settings.posting_rendered_fallback_provider == "invisible_playwright_first":
            from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver

            self.rendered_page_loader = PrioritizedRenderedPageResolver(
                primary_resolver=InvisiblePlaywrightRenderedPageResolver.from_settings(
                    self.settings, timeout_seconds=self.playwright_timeout_seconds,
                    retries=0, min_start_interval_seconds=0.0,
                ),
                fallback_resolver=self.rendered_page_loader,
                primary_provider="invisible_playwright", fallback_provider="chrome_cdp",
            )
        return self.rendered_page_loader


def _sanitized_rendered_error(error: str) -> str:
    return " ".join(str(error or "unknown_error").split())[:240]


def _rendered_loader_error(error: str, *, request_kind: str) -> AtsProviderError:
    normalized = error.casefold()
    if "timeout" in normalized:
        return AtsProviderError(502, f"Eluta {request_kind} request timed out")
    verification_code = next((code for code in (
        "auth_wall_unresolved", "blocked_rendered_page",
        "invisible_session_preparation_cooldown", "invisible_session_preparation_needs_review",
        "invisible_session_preparation_failed",
    ) if code in normalized), None)
    if verification_code or "auth_wall" in normalized:
        return AtsVerificationRequiredError(
            f"Eluta {request_kind} page required user verification",
            reason_code=verification_code or "authentication_wall",
        )
    if "url_policy_blocked" in normalized or "url_mismatch" in normalized:
        return AtsProviderError(502, f"Eluta {request_kind} redirect target was not allowed")
    return AtsProviderError(502, f"Eluta {request_kind} request failed")


def _resolve_apply_redirect_url(direct_site: ElutaSite, http_client: httpx.Client) -> str:
    try:
        response = http_client.get(
            direct_site.url,
            headers=_headers(),
            follow_redirects=False,
        )
        response.raise_for_status()
    except UrlPolicyViolation as error:
        raise AtsProviderError(502, "Eluta apply redirect target was not allowed") from error
    except httpx.HTTPStatusError as error:
        response = error.response
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise AtsProviderError(502, "Eluta apply redirect request failed") from error
    except httpx.HTTPError as error:
        raise AtsProviderError(502, "Eluta apply redirect request failed") from error

    location = response.headers.get("Location", "").strip()
    return urljoin(direct_site.url, location) if location else ""


def _eluta_cdp_configuration_error(error: Exception) -> AtsProviderError:
    message = str(error)
    if "valid port" in message:
        detail = "Eluta Chrome CDP endpoint must include a valid port"
    elif "nonzero port" in message:
        detail = "Eluta Chrome CDP endpoint must include a port"
    elif "localhost" in message:
        detail = "Eluta Chrome CDP endpoint must be localhost"
    elif "executable was not found" in message:
        detail = "Chrome executable was not found for Eluta CDP auto-start"
    elif "profile directory" in message:
        detail = "Invalid Chrome profile directory"
    else:
        detail = f"Eluta Chrome CDP configuration failed: {message}"
    return AtsProviderError(502, detail)


def _is_allowed_rendered_html_url(url: str, *, request_kind: str) -> bool:
    try:
        site = parse_eluta_url(url)
    except AtsProviderError:
        return False
    if request_kind == "search":
        return site.path == "/search"
    if request_kind == "detail":
        return site.is_detail
    return False


def _is_user_verification_url(url: str, *, request_kind: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in ELUTA_HOSTS
        or parsed.path != "/sandbox"
    ):
        return False
    for destination in parse_qs(parsed.query).get("destination", []):
        try:
            destination_site = parse_eluta_url(destination)
        except AtsProviderError:
            continue
        if request_kind == "detail" and destination_site.is_detail:
            return True
        if request_kind == "search" and destination_site.path == "/search":
            return True
    return False


def parse_eluta_url(source_url: str) -> ElutaSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path or ""
    if parsed.scheme not in {"http", "https"} or hostname not in ELUTA_HOSTS:
        raise AtsProviderError(400, "Only eluta.ca search/detail URLs are supported")
    if path == "/search":
        return ElutaSite(origin=f"{parsed.scheme}://{parsed.netloc}", path=path, query=parsed.query)
    if path.startswith("/spl/"):
        return ElutaSite(
            origin=f"{parsed.scheme}://{parsed.netloc}",
            path=path,
            query=parsed.query,
            is_detail=True,
        )
    raise AtsProviderError(400, "Only eluta.ca search/detail URLs are supported")


def parse_eluta_direct_url(source_url: str) -> ElutaSite:
    parsed = urlparse(source_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in ELUTA_HOSTS
        or parsed.path != "/direct/i"
    ):
        raise AtsProviderError(400, "Only eluta.ca apply redirect URLs are supported")
    return ElutaSite(
        origin=f"{parsed.scheme}://{parsed.netloc}", path=parsed.path, query=parsed.query
    )


def _search_page_url(site: ElutaSite, *, page: int, search_text: str) -> str:
    params = parse_qsl(site.query, keep_blank_values=True)
    updated: dict[str, list[str]] = {}
    for key, value in params:
        if key in {"pg", "q"} and key in updated:
            continue
        updated.setdefault(key, []).append(value)
    if search_text:
        updated["q"] = [search_text]
    updated["pg"] = [str(page)]
    return f"{site.origin}{site.path}?{urlencode(updated, doseq=True)}"


def _initial_page(site: ElutaSite) -> int:
    value = (parse_qs(site.query).get("pg") or ["1"])[0]
    try:
        page = int(value)
    except ValueError:
        return 1
    return max(page, 1)


def _next_page(soup: BeautifulSoup) -> int | None:
    for link in soup.select("a[href]"):
        if link.get_text(" ", strip=True) != "›":
            continue
        page = (parse_qs(urlparse(str(link.get("href") or "")).query).get("pg") or [""])[0]
        try:
            return int(page)
        except ValueError:
            return None
    return None


def _listing_candidates_from_search_page(
    site: ElutaSite, soup: BeautifulSoup
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for card in soup.select(".organic-job"):
        detail_url = _validated_detail_url(site, card.get("data-url"))
        if not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)
        candidates.append((detail_url, select_text(card, ".lk-job-title")))
    return candidates


def _validated_detail_url(search_site: ElutaSite, href: object | None) -> str:
    detail_url = urljoin(f"{search_site.origin}/search", str(href or ""))
    try:
        detail_site = parse_eluta_url(detail_url)
    except AtsProviderError:
        return ""
    if not detail_site.is_detail or detail_site.origin != search_site.origin:
        return ""
    return detail_site.url


def _is_skippable_detail_error(error: AtsProviderError) -> bool:
    return (
        error.status_code == 502 and "redirect target was not allowed" not in error.detail.lower()
    )


def _should_stop_after_detail_error(error: AtsProviderError) -> bool:
    return (isinstance(error, AtsVerificationRequiredError)
            or "required user verification" in error.detail.casefold())


def _should_fallback_to_rendered(error: AtsProviderError) -> bool:
    return error.status_code == 502 and "redirect target was not allowed" not in error.detail.casefold()


def _http_html_has_expected_content(html: str, *, request_kind: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if request_kind == "search":
        return soup.select_one(".organic-job") is not None
    if request_kind == "detail":
        return soup.select_one(".job-title") is not None
    return bool(html.strip())


def _is_recoverable_later_search_error(error: AtsProviderError) -> bool:
    return (
        error.status_code == 502 and "redirect target was not allowed" not in error.detail.lower()
    )


def _validated_html_redirect_url(
    requested_url: str,
    current_url: str,
    location: str,
    *,
    request_kind: str,
) -> str:
    redirect_url = urljoin(current_url, location)
    try:
        requested_site = parse_eluta_url(requested_url)
        redirect_site = parse_eluta_url(redirect_url)
    except AtsProviderError:
        return ""
    if request_kind == "search":
        if redirect_site.path != requested_site.path:
            return ""
        requested_query = parse_qs(requested_site.query, keep_blank_values=True)
        redirect_query = parse_qs(redirect_site.query, keep_blank_values=True)
        for key in ("q", "pg"):
            if key in requested_query and redirect_query.get(key) != requested_query[key]:
                return ""
    if request_kind == "detail" and (
        not redirect_site.is_detail or redirect_site.path != requested_site.path
    ):
        return ""
    return redirect_site.url


def _is_user_verification_redirect(current_url: str, location: str, *, request_kind: str) -> bool:
    redirect_url = urljoin(current_url, location)
    parsed = urlparse(redirect_url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in ELUTA_HOSTS
        or parsed.path != "/sandbox"
    ):
        return False
    for destination in parse_qs(parsed.query).get("destination", []):
        try:
            destination_site = parse_eluta_url(destination)
        except AtsProviderError:
            continue
        if request_kind == "detail" and destination_site.is_detail:
            return True
        if request_kind == "search" and destination_site.path == "/search":
            return True
    return False


def _job_from_detail(adapter: ElutaAdapter, detail_url: str, html: str) -> AtsJob:
    soup = BeautifulSoup(html, "html.parser")
    title = select_text(soup, ".job-title")
    if not title:
        raise AtsProviderError(502, "Eluta detail page did not include a job title")

    company = select_text(soup, ".employer-name")
    location = select_text(soup, '[itemprop="jobLocation"]')
    summary = select_text(soup, ".summary")
    posted = _meta_content(soup, '[itemprop="datePosted"]')
    description = select_posting_text(soup, '[itemprop="description"]') or select_posting_text(soup, ".description")
    direct_url = _direct_url(detail_url, soup)
    apply_url = adapter.resolve_apply_url(direct_url) if direct_url else ""

    fields = [
        title,
        line("Company", company),
        line("Location", location),
        line("Summary", summary),
        line("Posted", posted),
        line("Eluta URL", detail_url),
        line("Direct URL", direct_url),
        line("Apply URL", apply_url),
        description,
    ]
    raw_text = "\n".join(field for field in fields if field).strip()
    return AtsJob(
        source_url=apply_url or detail_url,
        raw_text=raw_text,
        portal_source_url=detail_url,
        content_source_url=detail_url,
        title=title,
    )


def _direct_url(detail_url: str, soup: BeautifulSoup) -> str:
    for element in soup.select(".apply-btn[onclick], .job-title a[onclick]"):
        match = re.search(r"enavOpenNew\(['\"]([^'\"]+)['\"]\)", str(element.get("onclick") or ""))
        if match:
            return urljoin(detail_url, unescape(match.group(1)))
    return ""


def _headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0 JobCockpit/0.1",
    }


def _eluta_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    return context


def _meta_content(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    if not element:
        return ""
    return str(element.get("content") or "").strip()
