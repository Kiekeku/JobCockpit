from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
    parse_oracle_candidate_experience_url,
)
from jobcockpit.ingestion.ats_adapters.workday import parse_workday_url
from jobcockpit.ingestion.source_authority import job_portal_kind
from jobcockpit.ingestion.url_hosts import (
    hostname_matches_domain,
    hostname_matches_indeed,
)


@dataclass(frozen=True)
class PostingIdentity:
    source_url: str
    source_kind: str
    is_trusted_provider: bool
    is_detail: bool
    posting_id: str | None = None
    posting_scope: str | None = None


ATS_CLOSED_SOURCE_KINDS = frozenset(
    {
        "adp_workforcenow",
        "applytojob",
        "ashby",
        "bamboohr",
        "breezy",
        "dayforce",
        "greenhouse",
        "icims",
        "jobvite",
        "lever",
        "oracle",
        "recruitee",
        "smartrecruiters",
        "ultipro",
        "workable",
        "workday",
    }
)
INDEED_POSTING_ID = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)
RESERVED_ROUTE_SEGMENTS = frozenset(
    {
        "apply",
        "application",
        "browse",
        "candidatehome",
        "home",
        "jobs",
        "list",
        "login",
        "openings",
        "portal",
        "preview",
        "register",
        "result",
        "results",
        "search",
        "search-results",
        "signin",
        "sign-in",
        "view",
    }
)

_UUID_POSTING_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NUMERIC_POSTING_ID = re.compile(r"^\d+$")
_INDEED_PRETTY_POSTING_ID = re.compile(r"(?:^|-)([0-9a-f]{16})$", re.IGNORECASE)
_SMARTRECRUITERS_POSTING_ID = re.compile(
    r"^(?P<posting_id>(?:sr)?\d{12,}[a-z]?)(?:-[a-z0-9-]+)?$",
    re.IGNORECASE,
)
_WORKABLE_POSTING_ID = re.compile(r"^[a-z0-9_-]{6,64}$", re.IGNORECASE)
_EXACT_RECRUITING_HOSTS = {
    "greenhouse": frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"}),
    "lever": frozenset({"jobs.lever.co"}),
    "ashby": frozenset({"jobs.ashbyhq.com"}),
    "smartrecruiters": frozenset(
        {"jobs.smartrecruiters.com", "smartrecruiters.com", "www.smartrecruiters.com"}
    ),
    "workable": frozenset({"apply.workable.com", "jobs.workable.com"}),
}
_RESERVED_PROVIDER_SUBDOMAINS = frozenset(
    {"api", "community", "developer", "developers", "docs", "help", "status", "support", "www"}
)
_WORKDAY_NON_DETAIL_ROUTE_SEGMENTS = frozenset(
    {
        "apply",
        "application",
        "browse",
        "candidatehome",
        "home",
        "jobs",
        "list",
        "login",
        "preview",
        "result",
        "results",
        "search",
        "search-results",
        "signin",
        "sign-in",
        "view",
    }
)


def posting_identity(source_url: str) -> PostingIdentity:
    parsed = urlparse(source_url.strip())
    hostname = (parsed.hostname or "").casefold()
    path_parts = path_parts_for_url(parsed.path)
    if parsed.scheme not in {"http", "https"} or not hostname:
        return PostingIdentity(source_url, "generic", False, False)

    if hostname_matches_domain(hostname, "linkedin.com"):
        posting_id = _linkedin_posting_id(path_parts)
        return PostingIdentity(
            source_url,
            "linkedin",
            True,
            posting_id is not None,
            posting_id,
            "global" if posting_id else None,
        )
    if hostname_matches_indeed(hostname):
        posting_id = _indeed_posting_id(path_parts, parsed.query)
        return PostingIdentity(
            source_url,
            "indeed",
            True,
            posting_id is not None,
            posting_id,
            "global" if posting_id else None,
        )

    try:
        site = parse_workday_url(source_url)
    except Exception:
        site = None
    if site is not None:
        posting_id = _workday_posting_id(site.external_path)
        return PostingIdentity(
            source_url,
            "workday",
            True,
            posting_id is not None,
            posting_id,
            (
                f"{_normalized_origin_scope(site.origin)}|{site.tenant}|{site.site}"
                if posting_id
                else None
            ),
        )

    try:
        oracle_site = parse_oracle_candidate_experience_url(source_url)
    except Exception:
        oracle_site = None
    if oracle_site is not None:
        posting_id = oracle_site.job_id
        if not _is_non_reserved_posting_id(posting_id or ""):
            posting_id = None
        return PostingIdentity(
            source_url,
            "oracle",
            True,
            posting_id is not None,
            posting_id,
            (
                f"{_normalized_origin_scope(oracle_site.origin)}|{oracle_site.site}"
                if posting_id
                else None
            ),
        )

    if hostname in _EXACT_RECRUITING_HOSTS["greenhouse"]:
        posting_id, posting_scope = _greenhouse_posting_identity(path_parts, parsed.query)
        return PostingIdentity(
            source_url,
            "greenhouse",
            True,
            posting_id is not None,
            posting_id,
            posting_scope,
        )
    if hostname in _EXACT_RECRUITING_HOSTS["lever"]:
        posting_id = (
            path_parts[1] if len(path_parts) >= 2 and _is_uuid_posting_id(path_parts[1]) else None
        )
        posting_id = posting_id.casefold() if posting_id else None
        return PostingIdentity(
            source_url,
            "lever",
            True,
            posting_id is not None,
            posting_id,
            path_parts[0] if posting_id else None,
        )
    if hostname in _EXACT_RECRUITING_HOSTS["ashby"]:
        posting_id = (
            path_parts[1] if len(path_parts) >= 2 and _is_uuid_posting_id(path_parts[1]) else None
        )
        posting_id = posting_id.casefold() if posting_id else None
        return PostingIdentity(
            source_url,
            "ashby",
            True,
            posting_id is not None,
            posting_id,
            path_parts[0] if posting_id else None,
        )
    if hostname in _EXACT_RECRUITING_HOSTS["smartrecruiters"]:
        posting_id = _smartrecruiters_posting_id(path_parts)
        return PostingIdentity(
            source_url,
            "smartrecruiters",
            True,
            posting_id is not None,
            posting_id,
            path_parts[0] if posting_id else None,
        )
    if hostname in _EXACT_RECRUITING_HOSTS["workable"]:
        posting_id = _workable_posting_id(hostname, path_parts)
        return PostingIdentity(
            source_url,
            "workable",
            True,
            posting_id is not None,
            posting_id,
            "global" if posting_id else None,
        )

    source_kind = _routed_ats_source_kind(hostname, path_parts)
    if source_kind is not None:
        posting_id = _other_ats_posting_id(source_kind, path_parts, parsed.query)
        return PostingIdentity(
            source_url,
            source_kind,
            True,
            posting_id is not None,
            posting_id,
            _other_ats_posting_scope(source_kind, hostname, path_parts, parsed.query)
            if posting_id
            else None,
        )

    aggregator = job_portal_kind(source_url)
    if aggregator:
        return PostingIdentity(source_url, aggregator, True, False)
    return PostingIdentity(source_url, "generic", False, False)


def source_kind(source_url: str) -> str:
    return posting_identity(source_url).source_kind


def posting_url_supports_open_evidence(source_url: str) -> bool:
    """Shared URL-detail gate for discovery and status reconciliation."""

    identity = posting_identity(source_url)
    return not identity.is_trusted_provider or identity.is_detail


def supports_definitive_ats_404(identity: PostingIdentity) -> bool:
    if (
        identity.source_kind not in ATS_CLOSED_SOURCE_KINDS
        or not identity.is_trusted_provider
        or not identity.is_detail
        or not identity.posting_id
    ):
        return False
    if identity.source_kind in {
        "ashby",
        "greenhouse",
        "icims",
        "lever",
        "smartrecruiters",
        "workable",
        "workday",
    }:
        return True
    if identity.source_kind in {"bamboohr", "oracle"}:
        return _is_numeric_posting_id(identity.posting_id)
    if identity.source_kind == "ultipro":
        return _is_numeric_posting_id(identity.posting_id) or _is_uuid_posting_id(
            identity.posting_id
        )
    return False


def is_recognized_ats_detail_url(source_kind: str, source_url: str) -> bool:
    identity = posting_identity(source_url)
    return (
        identity.source_kind == source_kind and identity.is_trusted_provider and identity.is_detail
    )


def path_parts_for_url(path: str) -> tuple[str, ...]:
    return tuple(unquote(part) for part in path.split("/") if part)


def _linkedin_posting_id(path_parts: tuple[str, ...]) -> str | None:
    if path_parts and path_parts[0].casefold() == "comm":
        path_parts = path_parts[1:]
    if len(path_parts) < 3 or tuple(part.casefold() for part in path_parts[0:2]) != (
        "jobs",
        "view",
    ):
        return None
    match = re.search(r"(\d+)$", path_parts[2])
    return match.group(1) if match else None


def _indeed_posting_id(path_parts: tuple[str, ...], query: str) -> str | None:
    query_params = httpx.QueryParams(query)
    jk_id = str(query_params.get("jk") or "").strip()
    query_id = jk_id or str(query_params.get("vjk") or "").strip()
    folded_parts = tuple(part.casefold() for part in path_parts)
    if INDEED_POSTING_ID.fullmatch(jk_id) and folded_parts in {
        ("rc", "clk"),
        ("rc", "clk", "dl"),
        ("pagead", "clk"),
        ("pagead", "clk", "dl"),
    }:
        return jk_id.casefold()
    if folded_parts and folded_parts[-1] == "viewjob" and INDEED_POSTING_ID.fullmatch(query_id):
        return query_id.casefold()
    if len(path_parts) == 2 and folded_parts[0] == "job":
        match = _INDEED_PRETTY_POSTING_ID.search(path_parts[1])
        return match.group(1).casefold() if match else None
    if (
        INDEED_POSTING_ID.fullmatch(query_id)
        and len(folded_parts) == 1
        and folded_parts[0].startswith("q-")
        and folded_parts[0].endswith("-jobs.html")
    ):
        return query_id.casefold()
    if INDEED_POSTING_ID.fullmatch(query_id) and any(
        part in {"job", "jobs"} for part in folded_parts
    ):
        return query_id.casefold()
    return None


def _workday_posting_id(external_path: str | None) -> str | None:
    if not external_path:
        return None
    path_parts = path_parts_for_url(external_path)
    folded_parts = tuple(part.casefold() for part in path_parts)
    try:
        marker_index = folded_parts.index("job")
    except ValueError:
        return None
    remaining = path_parts[marker_index + 1 :]
    if not remaining:
        return None
    first_segment = remaining[0]
    if first_segment.casefold() in _WORKDAY_NON_DETAIL_ROUTE_SEGMENTS:
        return None
    if first_segment.casefold() == "page" and (
        len(remaining) != 2 or _is_numeric_posting_id(remaining[-1])
    ):
        return None
    posting_segment = remaining[-1]
    # Some real Workday requisitions include an underscore themselves. Keep
    # the complete ID rather than confusing its final number with the ID.
    compound_requisition = re.search(r"(?:^|_)(R\d*_\d+(?:-\d+)?)$", posting_segment, re.I)
    if compound_requisition:
        return compound_requisition.group(1)
    prefix, separator, suffix = posting_segment.rpartition("_")
    posting_id = (
        suffix
        if prefix and separator and suffix and any(char.isdigit() for char in suffix)
        else posting_segment
    )
    if not _is_non_reserved_posting_id(posting_id) or not any(
        char.isdigit() for char in posting_id
    ):
        return None
    return posting_id


def _posting_id_after_marker(
    path_parts: tuple[str, ...],
    marker: str,
    validator: Callable[[str], bool],
) -> str | None:
    try:
        marker_index = tuple(part.casefold() for part in path_parts).index(marker.casefold())
    except ValueError:
        return None
    if marker_index + 1 >= len(path_parts):
        return None
    posting_id = path_parts[marker_index + 1]
    return posting_id if validator(posting_id) else None


def _greenhouse_posting_identity(
    path_parts: tuple[str, ...],
    query: str,
) -> tuple[str | None, str | None]:
    folded_parts = tuple(part.casefold() for part in path_parts)
    try:
        jobs_index = folded_parts.index("jobs")
    except ValueError:
        jobs_index = -1
    if jobs_index > 0 and jobs_index + 1 < len(path_parts):
        posting_id = path_parts[jobs_index + 1]
        if _is_numeric_posting_id(posting_id):
            return posting_id, path_parts[jobs_index - 1]
    if folded_parts == ("embed", "job_app"):
        query_params = httpx.QueryParams(query)
        posting_id = str(query_params.get("token") or "").strip()
        posting_scope = str(query_params.get("for") or "").strip()
        if _is_numeric_posting_id(posting_id) and _is_non_reserved_posting_id(posting_scope):
            return posting_id, posting_scope
    return None, None


def _smartrecruiters_posting_id(path_parts: tuple[str, ...]) -> str | None:
    if len(path_parts) != 2:
        return None
    match = _SMARTRECRUITERS_POSTING_ID.fullmatch(path_parts[1])
    return match.group("posting_id") if match else None


def _workable_posting_id(hostname: str, path_parts: tuple[str, ...]) -> str | None:
    posting_id: str | None = None
    folded_parts = tuple(part.casefold() for part in path_parts)
    if hostname == "jobs.workable.com" and len(path_parts) >= 2 and folded_parts[0] == "view":
        posting_id = path_parts[1]
    elif hostname == "apply.workable.com":
        if len(path_parts) >= 2 and folded_parts[0] == "j":
            posting_id = path_parts[1]
        elif len(path_parts) >= 3 and folded_parts[1] == "j":
            posting_id = path_parts[2]
        elif len(path_parts) >= 4 and folded_parts[1:3] == ("jobs", "view"):
            posting_id = path_parts[3].removesuffix(".md")
    if (
        posting_id
        and _WORKABLE_POSTING_ID.fullmatch(posting_id)
        and _is_non_reserved_posting_id(posting_id)
    ):
        return posting_id
    return None


def _routed_ats_source_kind(
    hostname: str,
    path_parts: tuple[str, ...],
) -> str | None:
    folded_parts = tuple(part.casefold() for part in path_parts)
    if any(label in _RESERVED_PROVIDER_SUBDOMAINS for label in hostname.split(".")):
        return None
    if hostname.endswith(".eightfold.ai") and len(hostname.split(".")) == 3:
        return "eightfold"
    if hostname.endswith(".icims.com") and hostname.split(".", 1)[0].startswith(
        ("jobs-", "careers-", "careersen-", "external-")
    ):
        return "icims"
    if hostname in {"recruiting.ultipro.com", "recruiting.ultipro.ca"}:
        return "ultipro"
    if hostname == "workforcenow.adp.com":
        return "adp_workforcenow"
    if hostname == "jobs.dayforcehcm.com":
        return "dayforce"
    if hostname.endswith(".recruitee.com") and "o" in folded_parts:
        return "recruitee"
    if hostname == "jobs.jobvite.com":
        return "jobvite"
    if hostname.endswith(".breezy.hr") and "p" in folded_parts:
        return "breezy"
    if hostname.endswith(".applytojob.com") and "apply" in folded_parts:
        return "applytojob"
    if hostname.endswith(".bamboohr.com") and "careers" in folded_parts:
        return "bamboohr"
    return None


def _other_ats_posting_id(
    source_kind: str,
    path_parts: tuple[str, ...],
    query: str,
) -> str | None:
    query_params = httpx.QueryParams(query)
    if source_kind == "eightfold":
        posting_ids = query_params.get_list("pid")
        return (
            posting_ids[0]
            if tuple(part.casefold() for part in path_parts) == ("careers",)
            and len(posting_ids) == 1
            and _is_numeric_posting_id(posting_ids[0])
            else None
        )
    if source_kind == "icims":
        return _posting_id_after_marker(path_parts, "jobs", _is_numeric_posting_id)
    if source_kind == "ultipro":
        posting_id = str(
            query_params.get("jobId")
            or query_params.get("jobid")
            or query_params.get("opportunityId")
            or query_params.get("opportunityid")
            or query_params.get("job")
            or ""
        ).strip()
        return posting_id if _is_non_reserved_posting_id(posting_id) else None
    if source_kind == "adp_workforcenow":
        posting_id = str(query_params.get("jobId") or query_params.get("jobid") or "").strip()
        return posting_id if re.fullmatch(r"[0-9]+(?:_[0-9]+)?", posting_id) else None
    if source_kind == "dayforce":
        return _posting_id_after_marker(path_parts, "jobs", _is_numeric_posting_id)
    marker_by_source = {
        "applytojob": "apply",
        "bamboohr": "careers",
        "breezy": "p",
        "jobvite": "job",
        "recruitee": "o",
    }
    marker = marker_by_source.get(source_kind)
    if not marker:
        return None
    return _posting_id_after_marker(path_parts, marker, _is_non_reserved_posting_id)


def _other_ats_posting_scope(
    source_kind: str,
    hostname: str,
    path_parts: tuple[str, ...],
    query: str,
) -> str:
    query_params = httpx.QueryParams(query)
    if source_kind == "adp_workforcenow":
        return "|".join(
            (
                hostname,
                str(query_params.get("cid") or "").casefold(),
                str(query_params.get("ccId") or query_params.get("ccid") or "").casefold(),
            )
        )
    if source_kind == "dayforce":
        folded = tuple(part.casefold() for part in path_parts)
        try:
            jobs_index = folded.index("jobs")
        except ValueError:
            jobs_index = len(path_parts)
        return "|".join((hostname, *folded[:jobs_index]))
    if source_kind == "ultipro":
        folded = tuple(part.casefold() for part in path_parts)
        try:
            board_index = folded.index("jobboard")
        except ValueError:
            board_index = min(len(path_parts), 1)
        return "|".join((hostname, *folded[: board_index + 2]))
    return hostname


def _is_uuid_posting_id(value: str) -> bool:
    return bool(_UUID_POSTING_ID.fullmatch(value))


def _is_numeric_posting_id(value: str) -> bool:
    return bool(_NUMERIC_POSTING_ID.fullmatch(value))


def _is_non_reserved_posting_id(value: str) -> bool:
    return bool(value and value.casefold() not in RESERVED_ROUTE_SEGMENTS)


def _normalized_origin_scope(source_url: str) -> str:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    scheme = parsed.scheme.casefold()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{hostname}{port_suffix}"
