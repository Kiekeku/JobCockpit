from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_PUBLIC_JOB_ID_QUERY_KEYS = {
    "currentjobid",
    "gh_jid",
    "id",
    "jid",
    "jk",
    "job",
    "job_id",
    "job_number",
    "job_req_id",
    "job_seq_no",
    "jobcard",
    "jobid",
    "jobnumber",
    "jobreqid",
    "jobseqno",
    "opening_id",
    "openingid",
    "opportunity_id",
    "opportunityid",
    "position_id",
    "positionid",
    "posting",
    "posting_id",
    "postingid",
    "req_id",
    "reqid",
    "requisition_id",
    "requisitionid",
    "rid",
    "vacancy",
    "vacancy_id",
    "vacancyid",
    "vjk",
}
_PUBLIC_QUERY_VALUE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_LINKEDIN_JOB_PATH = re.compile(
    r"^/(?:comm/)?jobs/view/(?:[^/]*-)?(?P<job_id>\d+)/?$",
    re.IGNORECASE,
)


def _hostname_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def capture_source_identity_url(source_url: str) -> str:
    """Compatibility projection; internal queue identity is never this URL."""
    from jobcockpit.ingestion.browser_capture_adapters import capture_source_identity

    identity = capture_source_identity(source_url)
    return identity.public_source_url if identity else ""


def sanitize_public_source_url(source_url: str) -> str:
    """Keep posting identity while removing credentials and tracking data."""
    try:
        parsed = urlsplit(source_url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        return ""

    public_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.casefold() in _PUBLIC_JOB_ID_QUERY_KEYS
        and _PUBLIC_QUERY_VALUE.fullmatch(value)
    ]
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path,
            urlencode(public_query),
            "",
        )
    )
