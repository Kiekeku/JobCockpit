from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


from jobcockpit.ingestion.url_hosts import INDEED_PROVIDER_DOMAINS


@dataclass(frozen=True)
class UrlPolicyResult:
    allowed: bool
    reason: str
    connect_host: str | None = None
    connect_hosts: tuple[str, ...] = ()


class UrlFetchPolicy:
    def __init__(self, allowed_localhosts: set[str] | None = None) -> None:
        self.allowed_localhosts = allowed_localhosts or set()

    def check(self, url: str) -> UrlPolicyResult:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return UrlPolicyResult(False, "unsupported_protocol")
        hostname = parsed.hostname
        if not hostname:
            return UrlPolicyResult(False, "missing_host")
        try:
            explicit_port = parsed.port
        except ValueError:
            return UrlPolicyResult(False, "invalid_port")
        if explicit_port is not None and explicit_port < 1:
            return UrlPolicyResult(False, "invalid_port")
        port = (
            explicit_port
            if explicit_port is not None
            else (443 if parsed.scheme == "https" else 80)
        )
        hostport = f"{hostname}:{port}"
        if hostname in {"localhost", "127.0.0.1"}:
            allowed = hostport in self.allowed_localhosts
            return UrlPolicyResult(
                allowed,
                "localhost_allowed" if allowed else "localhost_not_allowlisted",
                "127.0.0.1" if allowed else None,
                ("127.0.0.1",) if allowed else (),
            )
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return self._check_resolved_hostname(hostname, port)
        if _is_blocked_ip(ip):
            return UrlPolicyResult(False, "private_network_blocked")
        address = str(ip)
        return UrlPolicyResult(True, "public_ip", address, (address,))

    def _check_resolved_hostname(self, hostname: str, port: int) -> UrlPolicyResult:
        try:
            resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return UrlPolicyResult(False, "dns_resolution_failed")

        resolved_ips = []
        for result in resolved:
            address = result[4][0]
            try:
                resolved_ips.append(ipaddress.ip_address(address))
            except ValueError:
                return UrlPolicyResult(False, "dns_resolution_failed")
        if not resolved_ips:
            return UrlPolicyResult(False, "dns_resolution_failed")
        if any(_is_blocked_ip(ip) for ip in resolved_ips):
            return UrlPolicyResult(False, "private_network_blocked")
        addresses = tuple(dict.fromkeys(str(ip) for ip in resolved_ips))
        return UrlPolicyResult(
            True,
            "public_hostname",
            addresses[0],
            addresses,
        )


_RENDERED_PROVIDER_DOMAIN_GROUPS = (
    (("linkedin.com",), ("linkedin.com", "licdn.com")),
    (INDEED_PROVIDER_DOMAINS, (*INDEED_PROVIDER_DOMAINS, "indeedcdn.com")),
    (
        ("myworkdayjobs.com", "myworkdaysite.com"),
        (
            "myworkdayjobs.com",
            "myworkdaysite.com",
            "myworkday.com",
            "myworkdaycdn.com",
            "workday.com",
            "workdaycdn.com",
        ),
    ),
    (("greenhouse.io",), ("greenhouse.io",)),
    (("lever.co",), ("lever.co",)),
    (("ashbyhq.com",), ("ashbyhq.com",)),
    (("oraclecloud.com",), ("oraclecloud.com",)),
    (("smartrecruiters.com",), ("smartrecruiters.com",)),
    (("workable.com",), ("workable.com",)),
    (("icims.com",), ("icims.com",)),
    (("ultipro.com",), ("ultipro.com",)),
    (("recruitee.com",), ("recruitee.com",)),
    (("jobvite.com",), ("jobvite.com",)),
    (("breezy.hr",), ("breezy.hr",)),
    (("applytojob.com",), ("applytojob.com",)),
    (("bamboohr.com",), ("bamboohr.com",)),
)


class ScheduledRenderedUrlPolicy(UrlFetchPolicy):
    """Allow public connections; scope the final pages accepted by the renderer."""

    def __init__(
        self,
        allowed_localhosts: set[str] | None = None,
        *,
        extra_domain_groups: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        super().__init__(allowed_localhosts=allowed_localhosts)
        self.extra_domain_groups = _normalize_extra_domain_groups(extra_domain_groups)

    def check_browser_final_url(
        self,
        source_url: str,
        request_url: str,
    ) -> UrlPolicyResult:
        source = urlparse(source_url)
        request = urlparse(request_url)
        source_host = (source.hostname or "").rstrip(".").casefold()
        request_host = (request.hostname or "").rstrip(".").casefold()
        if not source_host or not request_host:
            return UrlPolicyResult(False, "rendered_cross_origin_blocked")
        if _url_origin(source) == _url_origin(request):
            return UrlPolicyResult(True, "rendered_source_origin")

        # Employer-hosted Greenhouse pages (identified by gh_jid) embed their
        # description and assets on Greenhouse. Public-IP validation still runs
        # separately in the request guard and pinned proxy.
        job_ids = parse_qs(source.query).get("gh_jid", [])
        if len(job_ids) == 1 and job_ids[0].isdigit():
            if request.scheme == "https" and _hostname_matches(request_host, "greenhouse.io"):
                return UrlPolicyResult(True, "rendered_embedded_provider_origin")
        source_origin, request_origin = _url_origin(source), _url_origin(request)
        if (
            source_origin and request_origin
            and source_origin[0] == request_origin[0] == "https"
            and source_origin[2] == request_origin[2]
            and source_host.removeprefix("www.") == request_host.removeprefix("www.")
        ):
            return UrlPolicyResult(True, "rendered_canonical_source_origin")

        # Indeed's observed verification page needs these hosts even for
        # passive completion. Keep the exception on standard HTTPS and the
        # exact observed hosts; the guard/proxy still validate their public IPs.
        if (
            source_origin and source_origin[0] == "https" and source_origin[2] == 443
            and any(_hostname_matches(source_host, domain) for domain in INDEED_PROVIDER_DOMAINS)
            and request_origin in {
                ("https", "challenges.cloudflare.com", 443),
                ("https", "hagen.challenges.cloudflare.com", 443),
            }
        ):
            return UrlPolicyResult(True, "rendered_verification_origin")

        allowed_domains: tuple[str, ...] = ()
        for source_domains, provider_domains in _RENDERED_PROVIDER_DOMAIN_GROUPS:
            if any(_hostname_matches(source_host, domain) for domain in source_domains):
                allowed_domains = provider_domains
                break
        if allowed_domains and any(
            _hostname_matches(request_host, domain) for domain in allowed_domains
        ):
            return UrlPolicyResult(True, "rendered_provider_origin")
        for source_domain, provider_domains in self.extra_domain_groups:
            if _hostname_matches(source_host, source_domain) and any(
                _hostname_matches(request_host, domain) for domain in provider_domains
            ):
                return UrlPolicyResult(True, "rendered_provider_origin")
        return UrlPolicyResult(False, "rendered_cross_origin_blocked")


def _normalize_extra_domain_groups(
    groups: Mapping[str, Sequence[str]] | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    normalized: list[tuple[str, tuple[str, ...]]] = []
    for raw_source_domain, raw_provider_domains in (groups or {}).items():
        source_domain = _validated_domain(raw_source_domain)
        if isinstance(raw_provider_domains, str):
            raise ValueError("extra rendered domains must be a list of valid hostnames")
        provider_domains = tuple(
            dict.fromkeys(_validated_domain(domain) for domain in raw_provider_domains)
        )
        if not provider_domains:
            raise ValueError("extra rendered domain groups cannot be empty")
        normalized.append((source_domain, provider_domains))
    return tuple(sorted(normalized))


def _validated_domain(value: object) -> str:
    domain = str(value or "").strip().rstrip(".").casefold()
    if (
        len(domain) > 253
        or "." not in domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise ValueError(f"{value!r} is not a valid hostname suffix")
    return domain


def _url_origin(parsed) -> tuple[str, str, int] | None:
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if scheme not in {"http", "https"} or not hostname:
        return None
    try:
        explicit_port = parsed.port
    except ValueError:
        return None
    if explicit_port is not None and explicit_port < 1:
        return None
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _hostname_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast
