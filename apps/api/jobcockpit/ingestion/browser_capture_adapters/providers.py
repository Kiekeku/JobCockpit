from __future__ import annotations

import hashlib
import re
from urllib.parse import ParseResult, parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from jobcockpit.ingestion.ats_adapters.workday import parse_workday_url
from jobcockpit.ingestion.browser_capture_adapters.base import (
    CaptureSourceIdentity,
    ExternalJobBoardTerminalStatus,
)
from jobcockpit.ingestion.url_hosts import INDEED_PROVIDER_DOMAINS
from jobcockpit.ingestion.status_identity import posting_identity
from jobcockpit.utils.public_urls import sanitize_public_source_url


def capture_identity_hash(provider: str, identity_key: str) -> str:
    return hashlib.sha256(f"{provider}\0{identity_key}".encode("utf-8")).hexdigest()


def hostname_matches(hostname: str | None, *domains: str) -> bool:
    normalized = (hostname or "").casefold().strip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def normalized_capture_url(parsed: ParseResult) -> str:
    hostname = (parsed.hostname or "").casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            "",
            urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
            parsed.fragment,
        )
    )


def weak_capture_identity(parsed: ParseResult, provider: str) -> CaptureSourceIdentity:
    normalized = normalized_capture_url(parsed)
    canonical_source_url = sanitize_public_source_url(normalized)
    hostname = (parsed.hostname or "").casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    public_source_url = urlunparse(
        (parsed.scheme.casefold(), netloc, parsed.path or "/", "", "", "")
    )
    source_url_dedup_safe = not parsed.fragment and (
        not parsed.query or bool(urlparse(canonical_source_url).query)
    )
    identity_key = canonical_source_url if source_url_dedup_safe else normalized
    return CaptureSourceIdentity(
        provider=provider,
        identity_hash=capture_identity_hash(provider, identity_key),
        public_source_url=public_source_url,
        canonical_source_url=canonical_source_url,
        strength="weak",
        source_url_dedup_safe=source_url_dedup_safe,
    )


class LinkedInCaptureProviderAdapter:
    provider = "linkedin"

    def matches_provider_url(self, parsed: ParseResult) -> bool:
        return hostname_matches(parsed.hostname, "linkedin.com")

    def matches_posting_route(self, parsed: ParseResult) -> bool:
        return self._posting_id(parsed) is not None

    def matches_source_route(self, parsed: ParseResult) -> bool:
        segments = tuple(segment.casefold() for segment in parsed.path.split("/") if segment)
        return "jobs" in segments

    def matches_terminal_route(self, parsed: ParseResult) -> bool:
        return self.matches_posting_route(parsed)

    def capture_identity(self, parsed: ParseResult) -> CaptureSourceIdentity:
        job_id = self._posting_id(parsed)
        if not job_id:
            return weak_capture_identity(parsed, self.provider)
        source_url = f"https://www.linkedin.com/jobs/view/{job_id}"
        return CaptureSourceIdentity(
            provider=self.provider,
            identity_hash=capture_identity_hash(self.provider, job_id),
            public_source_url=source_url,
            canonical_source_url=source_url,
            strength="authoritative",
            source_url_dedup_safe=True,
        )

    def terminal_status(self, page_text: str) -> ExternalJobBoardTerminalStatus | None:
        normalized = " ".join(re.findall(r"[a-z0-9]+", page_text[:32_000].casefold()))
        if "no longer accepting applications" in normalized:
            return ExternalJobBoardTerminalStatus(
                status="closed",
                evidence_code="linkedin_no_longer_accepting",
            )
        if any(
            marker in normalized
            for marker in (
                "job may have been removed",
                "job has been removed",
                "job posting has been removed",
                "job was removed",
                "job id is incorrect",
                "job id is invalid",
                "job id is not valid",
            )
        ):
            return ExternalJobBoardTerminalStatus(
                status="closed",
                evidence_code="linkedin_job_removed_or_invalid",
            )
        return None

    @staticmethod
    def _posting_id(parsed: ParseResult) -> str | None:
        match = re.fullmatch(
            r"/(?:comm/)?jobs/view/(?:[^/]*-)?(?P<job_id>\d+)/?",
            parsed.path,
            re.IGNORECASE,
        )
        if match:
            return match.group("job_id")
        path_segments = tuple(segment.casefold() for segment in parsed.path.split("/") if segment)
        if len(path_segments) >= 2 and path_segments[:2] in {
            ("jobs", "collections"),
            ("jobs", "search"),
        }:
            for key, value in parse_qsl(parsed.query, keep_blank_values=False):
                if key.casefold() == "currentjobid" and value.isdigit():
                    return value
        return None


class IndeedCaptureProviderAdapter:
    provider = "indeed"

    def matches_provider_url(self, parsed: ParseResult) -> bool:
        return hostname_matches(parsed.hostname, *INDEED_PROVIDER_DOMAINS)

    def matches_posting_route(self, parsed: ParseResult) -> bool:
        return self._posting_id(parsed) is not None

    def matches_source_route(self, parsed: ParseResult) -> bool:
        segments = tuple(segment.casefold() for segment in parsed.path.split("/") if segment)
        if "viewjob" in segments or segments[:2] in {
            ("rc", "clk"),
            ("pagead", "clk"),
        }:
            return True
        if re.fullmatch(r"/job/(?:[^/]*-)?[A-Za-z0-9]{8,128}/?", parsed.path):
            return True
        params = parse_qs(parsed.query)
        has_posting_id = bool(params.get("jk") or params.get("vjk"))
        return has_posting_id and (
            any(segment in {"job", "jobs"} for segment in segments)
            or (
                len(segments) == 1
                and segments[0].startswith("q-")
                and segments[0].endswith("-jobs.html")
            )
        )

    def matches_terminal_route(self, parsed: ParseResult) -> bool:
        segments = tuple(segment.casefold() for segment in parsed.path.split("/") if segment)
        return "viewjob" in segments or bool(
            re.fullmatch(r"/job/(?:[^/]*-)?[A-Za-z0-9]{8,128}/?", parsed.path)
        )

    def capture_identity(self, parsed: ParseResult) -> CaptureSourceIdentity:
        job_id = self._posting_id(parsed)
        if not job_id:
            return weak_capture_identity(parsed, self.provider)
        hostname = (parsed.hostname or "www.indeed.com").casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        source_url = urlunparse(
            (parsed.scheme.casefold(), netloc, "/viewjob", "", f"jk={job_id}", "")
        )
        return CaptureSourceIdentity(
            provider=self.provider,
            identity_hash=capture_identity_hash(self.provider, job_id.casefold()),
            public_source_url=source_url,
            canonical_source_url=source_url,
            strength="authoritative",
            source_url_dedup_safe=True,
        )

    def terminal_status(self, page_text: str) -> ExternalJobBoardTerminalStatus | None:
        normalized = " ".join(page_text[:32_000].casefold().split())
        if any(
            marker in normalized
            for marker in (
                "this job has expired",
                "this job is no longer available",
                "job is no longer available",
            )
        ):
            return ExternalJobBoardTerminalStatus(
                status="expired",
                evidence_code="indeed_job_expired",
            )
        return None

    @staticmethod
    def _posting_id(parsed: ParseResult) -> str | None:
        pretty_match = re.fullmatch(
            r"/job/(?:[^/]*-)?(?P<job_id>[A-Za-z0-9]{8,128})/?",
            parsed.path,
            re.IGNORECASE,
        )
        if pretty_match:
            return pretty_match.group("job_id")
        params = parse_qs(parsed.query)
        value = next(
            (
                candidate
                for key in ("jk", "vjk")
                for candidate in params.get(key, [])
                if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", candidate)
            ),
            None,
        )
        if not value:
            return None
        segments = tuple(segment.casefold() for segment in parsed.path.split("/") if segment)
        if "viewjob" in segments:
            return value
        if segments[:2] in {("rc", "clk"), ("pagead", "clk")}:
            return value
        if any(segment in {"job", "jobs"} for segment in segments):
            return value
        if (
            len(segments) == 1
            and segments[0].startswith("q-")
            and segments[0].endswith("-jobs.html")
        ):
            return value
        return None


class WorkdayCaptureProviderAdapter:
    provider = "workday"

    def matches_provider_url(self, parsed: ParseResult) -> bool:
        return hostname_matches(
            parsed.hostname,
            "myworkdayjobs.com",
            "myworkdaysite.com",
        )

    def matches_posting_route(self, parsed: ParseResult) -> bool:
        return self._posting_identity(parsed) is not None

    def matches_terminal_route(self, parsed: ParseResult) -> bool:
        return self.matches_posting_route(parsed)

    def matches_source_route(self, parsed: ParseResult) -> bool:
        return self.matches_posting_route(parsed)

    def capture_identity(self, parsed: ParseResult) -> CaptureSourceIdentity:
        posting_identity = self._posting_identity(parsed)
        if posting_identity is None:
            return weak_capture_identity(parsed, self.provider)
        identity_key, source_url = posting_identity
        return CaptureSourceIdentity(
            provider=self.provider,
            identity_hash=capture_identity_hash(self.provider, identity_key),
            public_source_url=source_url,
            canonical_source_url=source_url,
            strength="authoritative",
            source_url_dedup_safe=True,
        )

    def terminal_status(self, page_text: str) -> ExternalJobBoardTerminalStatus | None:
        normalized = " ".join(page_text[:32_000].casefold().split())
        if any(
            marker in normalized
            for marker in (
                "job requisition is no longer available",
                "job posting is no longer available",
                "this job is no longer available",
            )
        ):
            return ExternalJobBoardTerminalStatus(
                status="closed",
                evidence_code="ats_no_longer_available",
            )
        return None

    @staticmethod
    def _posting_identity(parsed: ParseResult) -> tuple[str, str] | None:
        try:
            site = parse_workday_url(parsed.geturl())
        except Exception:
            return None
        if not site.external_path:
            return None
        path_parts = [part for part in site.external_path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0].casefold() != "job":
            return None
        posting_segment = path_parts[-1]
        prefix, separator, suffix = posting_segment.rpartition("_")
        posting_id = (
            suffix
            if prefix and separator and suffix and any(char.isdigit() for char in suffix)
            else posting_segment
        )
        if not posting_id or not any(char.isdigit() for char in posting_id):
            return None
        origin = urlparse(site.origin)
        hostname = (origin.hostname or "").casefold()
        scope = f"{hostname}|{site.tenant}|{site.site}"
        public_url = sanitize_public_source_url(
            urlunparse((parsed.scheme.casefold(), parsed.netloc, parsed.path, "", "", ""))
        )
        return f"{scope}|{posting_id}", public_url


class GenericCaptureProviderAdapter:
    provider = "generic"

    def matches_provider_url(self, parsed: ParseResult) -> bool:
        return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)

    def matches_posting_route(self, parsed: ParseResult) -> bool:
        return False

    def matches_terminal_route(self, parsed: ParseResult) -> bool:
        return False

    def matches_source_route(self, parsed: ParseResult) -> bool:
        return False

    def capture_identity(self, parsed: ParseResult) -> CaptureSourceIdentity:
        ats_identity = posting_identity(parsed.geturl())
        if (
            ats_identity.is_trusted_provider
            and ats_identity.is_detail
            and ats_identity.posting_id
        ):
            identity_key = (
                f"{ats_identity.posting_scope or 'global'}|"
                f"{ats_identity.posting_id.casefold()}"
            )
            public_source_url = urlunparse(
                (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", "", "")
            )
            canonical_source_url = sanitize_public_source_url(normalized_capture_url(parsed))
            return CaptureSourceIdentity(
                provider=ats_identity.source_kind,
                identity_hash=capture_identity_hash(ats_identity.source_kind, identity_key),
                public_source_url=public_source_url,
                canonical_source_url=canonical_source_url,
                strength="authoritative",
                source_url_dedup_safe=True,
            )
        return weak_capture_identity(parsed, self.provider)

    def terminal_status(self, page_text: str) -> ExternalJobBoardTerminalStatus | None:
        return None
