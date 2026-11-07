from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import ParseResult, urlparse

from jobcockpit.ingestion.browser_capture_adapters.base import (
    BrowserCaptureAdapter,
    BrowserCaptureContext,
    BrowserCaptureExtractedFields,
    BrowserCaptureProviderAdapter,
    CaptureSourceIdentity,
)
from jobcockpit.ingestion.browser_capture_adapters.indeed import IndeedBrowserCaptureAdapter
from jobcockpit.ingestion.browser_capture_adapters.providers import (
    GenericCaptureProviderAdapter,
    IndeedCaptureProviderAdapter,
    LinkedInCaptureProviderAdapter,
    WorkdayCaptureProviderAdapter,
)

BROWSER_CAPTURE_ADAPTERS: tuple[BrowserCaptureAdapter, ...] = (IndeedBrowserCaptureAdapter(),)


class BrowserCaptureResolverFactory:
    """Resolve one DNS-owned provider adapter, with an explicit generic fallback."""

    def __init__(
        self,
        adapters: Iterable[BrowserCaptureProviderAdapter],
        fallback: BrowserCaptureProviderAdapter,
    ) -> None:
        self._adapters = tuple(adapters)
        self._fallback = fallback

    @property
    def adapters(self) -> tuple[BrowserCaptureProviderAdapter, ...]:
        return self._adapters

    def resolve_parsed(self, parsed: ParseResult) -> BrowserCaptureProviderAdapter:
        return next(
            (adapter for adapter in self._adapters if adapter.matches_provider_url(parsed)),
            self._fallback,
        )

    def resolve(self, source_url: str) -> BrowserCaptureProviderAdapter | None:
        try:
            parsed = urlparse(source_url.strip())
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                return None
            explicit_port = parsed.port
            if explicit_port is not None and explicit_port < 1:
                return None
        except ValueError:
            return None
        return self.resolve_parsed(parsed)


BROWSER_CAPTURE_PROVIDER_FACTORY = BrowserCaptureResolverFactory(
    (
        LinkedInCaptureProviderAdapter(),
        IndeedCaptureProviderAdapter(),
        WorkdayCaptureProviderAdapter(),
    ),
    GenericCaptureProviderAdapter(),
)


def resolve_browser_capture_provider(
    source_url: str,
) -> BrowserCaptureProviderAdapter | None:
    return BROWSER_CAPTURE_PROVIDER_FACTORY.resolve(source_url)


def capture_source_identity(source_url: str) -> CaptureSourceIdentity | None:
    adapter = resolve_browser_capture_provider(source_url)
    if adapter is None:
        return None
    return adapter.capture_identity(urlparse(source_url.strip()))


def extract_browser_capture_fields(context: BrowserCaptureContext) -> BrowserCaptureExtractedFields:
    for adapter in BROWSER_CAPTURE_ADAPTERS:
        if not adapter.matches(context):
            continue
        fields = adapter.extract(context)
        if fields.has_values():
            return fields
    return BrowserCaptureExtractedFields()
