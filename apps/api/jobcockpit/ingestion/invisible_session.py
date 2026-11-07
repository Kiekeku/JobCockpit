"""Site-specific verification controls observed in the browser comparison.

This is browser session preparation, not an application-form adapter. Unknown
controls stay untouched and every result is reloaded through the guarded loader.
"""

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobcockpit.ingestion.url_hosts import INDEED_PROVIDER_DOMAINS
from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult


def verification_site(url: str) -> str:
    parsed = urlsplit(url)
    try:
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username:
            return ""
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if any(host == domain or host.endswith("." + domain) for domain in INDEED_PROVIDER_DOMAINS):
        return "indeed"
    return "eluta" if host in {"eluta.ca", "www.eluta.ca"} else ""


def session_preparation_url(source_url: str) -> str:
    """Use a simple search to avoid Eluta corrupting encoded destinations."""
    parsed = urlsplit(source_url)
    if verification_site(source_url) == "eluta" and parsed.path.rstrip("/") == "/search":
        # Saved search challenges decoded and truncated a quoted field query
        # at its embedded ampersand. Bootstrap the same site's session without
        # that expression; the guarded retry still loads the original URL.
        return parsed._replace(query="q=co-op", fragment="").geturl()
    return source_url


def session_page_error(url: str, title: str) -> str:
    """Reject observed authentication transitions/errors as posting content."""
    heading = title.casefold().strip().rstrip(" .!?:…")
    if heading == "authenticating":
        return "auth_wall_unresolved"
    # Let the existing bounded verification flow handle challenge pages even
    # when Indeed has already redirected to /account/login.
    if heading in {"just a moment", "additional verification required"}:
        return ""
    if re.match(r"^(?:http\s+)?(?:error\s+)?[45]\d\d\b", heading):
        return "rendered_http_error"
    if verification_site(url) == "indeed":
        path = urlsplit(url).path.split(";", 1)[0].rstrip("/")
        if path in {"/auth", "/account/login"}:
            return "invisible_session_sign_in_required"
    return ""


class PreparationUrlPolicy:
    """Validate public connections and restrict accepted preparation pages."""

    def __init__(self, source_url: str, delegate: UrlFetchPolicy):
        self.source_url = source_url
        self.delegate = delegate
        site = verification_site(source_url)
        self.domains = (
            (*INDEED_PROVIDER_DOMAINS, "indeedcdn.com", "challenges.cloudflare.com")
            if site == "indeed" else
            ("eluta.ca", "www.recaptcha.net", "www.google.com", "www.gstatic.com")
            if site == "eluta" else ()
        )

    def check(self, url: str) -> UrlPolicyResult:
        return self.delegate.check(url)

    def check_browser_final_url(self, source_url: str, request_url: str) -> UrlPolicyResult:
        check = getattr(self.delegate, "check_browser_final_url", None)
        if callable(check):
            delegated = check(source_url, request_url)
            if not delegated.allowed:
                return delegated
        source = urlsplit(self.source_url)
        request = urlsplit(request_url)
        try:
            source_origin = (source.scheme, source.hostname, source.port or (443 if source.scheme == "https" else 80))
            request_origin = (request.scheme, request.hostname, request.port or (443 if request.scheme == "https" else 80))
        except ValueError:
            return UrlPolicyResult(False, "invalid_port")
        if request_origin == source_origin and request.scheme in {"http", "https"}:
            return UrlPolicyResult(True, "preparation_source_origin")
        host = (request.hostname or "").lower()
        if request.scheme == "https" and request.port in (None, 443) and any(
            host == domain or host.endswith("." + domain) for domain in self.domains
        ):
            return UrlPolicyResult(True, "preparation_widget_origin")
        return UrlPolicyResult(False, "preparation_cross_origin_blocked")


@dataclass
class VerificationProgress:
    checkbox_clicked: bool = False
    continue_clicked: bool = False


def advance_verification(
    page: Any, source_url: str, progress: VerificationProgress, *, timeout_ms: int,
) -> None:
    """At most one checkbox click and one narrowly scoped Eluta continuation."""
    site = verification_site(source_url)
    if not site or verification_site(page.url) != site:
        return
    title = page.title()
    if site == "indeed":
        if progress.checkbox_clicked or title not in {"Just a moment...", "Additional Verification Required"}:
            return
        frames = [frame for frame in page.frames if (
            urlsplit(frame.url).scheme == "https"
            and urlsplit(frame.url).hostname == "challenges.cloudflare.com"
        )]
        if len(frames) == 1:
            checkbox = frames[0].get_by_role("checkbox")
            if checkbox.count() == 1 and checkbox.is_visible():
                progress.checkbox_clicked = True
                checkbox.click(timeout=timeout_ms)
        return

    if title != "User Verification" or progress.continue_clicked:
        return
    form = page.locator('form[action="/sandbox"]')
    if form.count() != 1:
        return
    destination = form.locator('input[name="destination"]')
    if destination.count() != 1 or destination.input_value(timeout=timeout_ms) != source_url:
        return
    frames = []
    for frame in page.frames:
        parsed = urlsplit(frame.url)
        if (parsed.scheme == "https" and parsed.hostname in {"www.recaptcha.net", "www.google.com"}
                and parsed.path == "/recaptcha/api2/anchor"
                and parse_qs(parsed.query).get("size") == ["normal"]):
            frames.append(frame)
    if len(frames) != 1:
        return
    checkbox = frames[0].get_by_role("checkbox")
    if checkbox.count() != 1 or not checkbox.is_visible():
        return
    if not progress.checkbox_clicked:
        progress.checkbox_clicked = True
        checkbox.click(timeout=timeout_ms)
        return
    if checkbox.get_attribute("aria-checked", timeout=timeout_ms) != "true":
        return
    continuation = form.get_by_role("button", name="Continue", exact=True)
    if continuation.count() == 1 and continuation.is_visible():
        progress.continue_clicked = True
        continuation.click(timeout=timeout_ms)
