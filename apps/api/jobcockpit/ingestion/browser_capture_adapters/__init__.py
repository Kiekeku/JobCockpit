from __future__ import annotations

from jobcockpit.ingestion.browser_capture_adapters.base import (
    BrowserCaptureAdapter,
    BrowserCaptureContext,
    BrowserCaptureExtractedFields,
    BrowserCaptureProviderAdapter,
    CaptureAccessVerdict,
    CapturePageKind,
    CaptureProcessingAction,
    CaptureSourceIdentity,
    ExternalJobBoardTerminalStatus,
    PageCaptureAssessment,
)
from jobcockpit.ingestion.browser_capture_adapters.providers import (
    GenericCaptureProviderAdapter,
    IndeedCaptureProviderAdapter,
    LinkedInCaptureProviderAdapter,
    WorkdayCaptureProviderAdapter,
)
from jobcockpit.ingestion.browser_capture_adapters.registry import (
    BROWSER_CAPTURE_PROVIDER_FACTORY,
    BrowserCaptureResolverFactory,
    capture_source_identity,
    extract_browser_capture_fields,
    resolve_browser_capture_provider,
)

__all__ = [
    "BROWSER_CAPTURE_PROVIDER_FACTORY",
    "BrowserCaptureAdapter",
    "BrowserCaptureContext",
    "BrowserCaptureExtractedFields",
    "BrowserCaptureProviderAdapter",
    "BrowserCaptureResolverFactory",
    "CaptureAccessVerdict",
    "CapturePageKind",
    "CaptureProcessingAction",
    "CaptureSourceIdentity",
    "ExternalJobBoardTerminalStatus",
    "GenericCaptureProviderAdapter",
    "IndeedCaptureProviderAdapter",
    "LinkedInCaptureProviderAdapter",
    "PageCaptureAssessment",
    "WorkdayCaptureProviderAdapter",
    "capture_source_identity",
    "extract_browser_capture_fields",
    "resolve_browser_capture_provider",
]
