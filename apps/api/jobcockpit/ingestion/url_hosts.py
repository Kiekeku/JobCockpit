from __future__ import annotations

from urllib.parse import urlparse

INDEED_PROVIDER_DOMAINS = ("indeed.com", "indeed.ca")


def hostname_matches_domain(hostname: str | None, domain: str) -> bool:
    normalized_hostname = (hostname or "").strip(".").casefold()
    normalized_domain = domain.strip(".").casefold()
    return bool(
        normalized_hostname
        and normalized_domain
        and (
            normalized_hostname == normalized_domain
            or normalized_hostname.endswith(f".{normalized_domain}")
        )
    )


def hostname_matches_any_domain(hostname: str | None, domains: tuple[str, ...]) -> bool:
    return any(hostname_matches_domain(hostname, domain) for domain in domains)


def hostname_matches_indeed(hostname: str | None) -> bool:
    return hostname_matches_any_domain(hostname, INDEED_PROVIDER_DOMAINS)


def url_matches_domain(url: str, domain: str) -> bool:
    return hostname_matches_domain(urlparse(url).hostname, domain)
