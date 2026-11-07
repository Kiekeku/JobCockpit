"""One fit-analysis scheduling policy for all posting ingestion paths."""
from enum import Enum

from jobcockpit.core.config import Settings


class FitAnalysisMode(Enum):
    DISABLED = "disabled"
    INLINE = "inline"
    QUEUED = "queued"


def ingestion_fit_mode(settings: Settings | None, *, run_inline: bool | None = None) -> FitAnalysisMode:
    """Use cheap local fitting inline; defer providers and guarded capture writes.

    An explicit inline request is reserved for callers already running in a
    worker. False requests deferred fitting. Disabled providers never queue work.
    """
    provider = str(getattr(settings, "fit_analysis_provider", "disabled") or "").strip().lower()
    if provider in {"", "disabled"}:
        return FitAnalysisMode.DISABLED
    if run_inline is True or (run_inline is None and provider == "heuristic"):
        return FitAnalysisMode.INLINE
    return FitAnalysisMode.QUEUED
