"""Ingestion-only identities grounded in observed employer posting routes.

This does not expand browser insertion adapters or evidence for closing jobs.
Unknown routes remain weak; numeric IDs are never inferred from arbitrary URLs.
"""
import re
from urllib.parse import parse_qs, unquote, urlsplit

from jobcockpit.ingestion.browser_capture_adapters import capture_source_identity
from jobcockpit.ingestion.ats_adapters.base import AtsProviderError


class PostingVerificationRequired(AtsProviderError):
    def __init__(self):
        super().__init__(502, "Posting requires user verification")


def is_verification_url(url: str | None) -> bool:
    return (urlsplit(url or "").hostname or "").casefold().rstrip(".") == "validate.perfdrive.com"


def authoritative_ingestion_identity(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}):
        return None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = unquote(parsed.path)
    pattern = None
    if host == "amazon.jobs":
        pattern = r"/[a-z]{2}(?:-[A-Z]{2})?/jobs/(\d+)(?:/[^/]+)?/?"
    elif host == "google.com":
        pattern = r"/about/careers/applications/jobs/results/(\d+)(?:-[^/]+)?/?"
    elif host in {"careers.planview.com", "careers.trccompanies.com"}:
        pattern = r"/(?:careers-home/)?jobs/(\d+)(?:/job)?/?"
    elif host == "careers.amd.com":
        pattern = r"/(?:careers-home/|students/)?jobs/(\d+)/?"
    elif host == "jobs.ericsson.com":
        pattern = r"/careers/job/(\d+)/?"
    elif host == "jobs.autodesk.com":
        pattern = r"/careers/job/(\d+)(?:-[^/]+)?/?"
    elif host == "jobs.ea.com":
        pattern = r"/[a-z]{2}_[A-Z]{2}/careers/JobDetail/[^/]+/(\d+)/?"
    elif host == "careers.siemens-healthineers.com":
        pattern = r"/global/[a-z]{2}/job/(R-\d+)(?:/[^/]+)?/?"
    elif host == "stripe.com":
        pattern = r"/(?:careers|jobs)/listing/[^/]+/(\d+)/?"
    elif host == "careers.cisco.com":
        pattern = r"/global/[a-z]{2}/job/(?:CISCISGLOBAL)?(\d+)(?:EXTERNALENGLOBAL)?(?:/[^/]+)?/?"
    elif host in {"jobs.teck.com", "careers.deloitte.ca"}:
        pattern = r"/job/[^/]+/(\d+)-[a-z]{2}_[A-Z]{2}/?"
    elif host in {"jobs.intuit.com", "careers.l3harris.com"}:
        pattern = r"/(?:[a-z]{2}/)?job/[^/]+/[^/]+/(\d+/\d+)/?"
    if pattern and (match := re.fullmatch(pattern, path)):
        return host, match[1]
    if host == "eluta.ca":
        # The same saved posting uses /spl, /direct/i and the sandbox wrapper.
        # Decode only Eluta's own single destination, never an arbitrary redirect.
        locator = parsed
        if path == "/sandbox":
            destinations = parse_qs(parsed.query).get("destination", [])
            if len(destinations) != 1:
                return None
            locator = urlsplit(destinations[0])
            if (locator.scheme not in {"http", "https"} or locator.username or locator.password
                    or (locator.hostname or "").casefold().removeprefix("www.") != host
                    or locator.port not in {None, 80 if locator.scheme == "http" else 443}):
                return None
        if match := re.fullmatch(r"/spl/[^/]+-([a-f0-9]{32})/?", unquote(locator.path)):
            return "eluta", match[1]
        ids = parse_qs(locator.query).get("i", [])
        if (locator.path == "/direct/i" and len(ids) == 1
                and re.fullmatch(r"[a-f0-9]{32}", ids[0])):
            return "eluta", ids[0]
    if host == "cgi.njoyn.com" and path.casefold() == "/corp/xweb/xweb.asp":
        ids = [value for key, values in parse_qs(parsed.query).items()
               if key.casefold() == "jobid" for value in values]
        if len(ids) == 1 and re.fullmatch(r"J\d{4}-\d+", ids[0], re.I):
            return host, ids[0].casefold()
    if host == "tower-research.com" and path.rstrip("/") == "/open-positions":
        ids = parse_qs(parsed.query).get("gh_jid", [])
        if len(ids) == 1 and ids[0].isdigit():
            return host, ids[0]
    identity = capture_source_identity(url)
    if identity and identity.source_url_dedup_safe and identity.strength == "authoritative":
        return identity.provider, identity.identity_hash
    return None


def ingestion_locator_identity(url: str | None) -> tuple[str, str] | None:
    """Keep ingestion's observed route rules separate from browser adapters."""
    if identity := authoritative_ingestion_identity(url):
        return identity
    capture = capture_source_identity(url or "")
    if capture and capture.source_url_dedup_safe:
        return capture.provider, capture.identity_hash
    return None
