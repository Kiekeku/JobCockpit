from __future__ import annotations

import math
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from enum import Enum
from html import unescape
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Protocol
from urllib.parse import ParseResult, parse_qs, urlparse

from jobcockpit.ingestion.ats_adapters.base import AtsJob, AtsProviderError, SourceArchive
from jobcockpit.ingestion.concurrency import bounded_resource
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.browser_capture_adapters import (
    BrowserCaptureContext,
    extract_browser_capture_fields,
    CaptureAccessVerdict,
    CapturePageKind,
    CaptureProcessingAction,
    ExternalJobBoardTerminalStatus,
    IndeedCaptureProviderAdapter,
    LinkedInCaptureProviderAdapter,
    PageCaptureAssessment,
    capture_source_identity,
    resolve_browser_capture_provider,
)
from jobcockpit.ingestion.browser_cleanup import (
    BrowserPageCleanupLease,
    browser_page_target_ids,
    capture_browser_page_cleanup_lease,
    capture_new_browser_page_cleanup_lease,
    cleanup_owned_browser_context,
    cleanup_owned_page,
    close_owned_browser_context,
    confirm_browser_cleanup,
    register_browser_cleanup_lease,
    request_browser_cleanup,
)
from jobcockpit.ingestion.chrome_cdp import (
    chrome_profile_directory_arg,
    connect_chrome_cdp,
    launch_chrome_cdp,
    validate_local_cdp_endpoint,
)
from jobcockpit.ingestion.codex_cli import (
    codex_cli_failure_message,
    json_object_from_output as codex_json_object_from_output,
    run_codex_prompt,
)
from jobcockpit.ingestion.location_parser import parse_location
from jobcockpit.ingestion.process_tree import run_with_process_tree_cleanup
from jobcockpit.ingestion.posting_resolution import (
    MAX_POSTING_RESOLUTION_ATTEMPTS,
    POSTING_RESOLUTION_ATTEMPT_STAGES,
    POSTING_RESOLUTION_ATTEMPT_STATUSES,
    ExternalPostingHint,
    PostingResolution,
    PostingResolutionAttempt,
    RenderedAccessVerdict,
    RenderedIdentityVerdict,
    RenderedPageObservation,
    RenderedQueryIdentityGroup,
    RenderedStatusClassifier,
    RenderedStatusSignal,
    ResolvedExternalPosting,
    SearchResult,
    _append_resolution_history,
    _bounded_resolution_attempts,  # noqa: F401 - compatibility re-export
    _official_search_outcome_for_resolution,
    _official_search_outcome_from_attempt,
    _resolution_attempt,
    _sanitized_attempt_candidate_url,
    _with_official_search_attempt,
    _with_official_search_outcome,
    _with_resolution_attempts,
    rendered_query_identity_groups,
)
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text
from jobcockpit.ingestion.pinned_browser_proxy import (
    PinnedBrowserProxy,
    PinnedBrowserProxyBlocked,
    PinnedBrowserProxyCapacityError,
)
from jobcockpit.ingestion.source_resolution import (
    OfficialDiscoveryOutcome,
    canonical_official_discovery_reason,
    official_discovery_outcome,
)
from jobcockpit.ingestion.source_authority import job_portal_kind
from jobcockpit.ingestion.status_identity import (
    posting_identity,
    posting_url_supports_open_evidence,
)
from jobcockpit.ingestion.url_policy import UrlFetchPolicy, UrlPolicyResult
from jobcockpit.integrations.official_careers import (
    reviewed_career_domain_for_company,
    reviewed_company_aliases,
)


class SearchClient(Protocol):
    def search(self, query: str, limit: int) -> Sequence[SearchResult]:
        """Return public search results for a job identity query."""


class OfficialCareerSiteSearchClient(Protocol):
    provider_code: str
    last_error: str

    def search(
        self, hint: ExternalPostingHint, limit: int
    ) -> Sequence[SearchResult]:
        """Return results from a reviewed employer-owned careers site."""


class RenderedPostingResolver(Protocol):
    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        """Resolve an external source URL from a rendered browser page."""


class RenderedPageLoader(Protocol):
    """Browser loader that owns page cleanup independently of its caller."""

    def load_page(self, hint: ExternalPostingHint) -> PostingResolution: ...

    def cancel_active_pages(self) -> int: ...

    def pending_cleanup_leases(self) -> tuple[BrowserPageCleanupLease, ...]: ...

    def release_pending_cleanup_leases(
        self,
        leases: Sequence[BrowserPageCleanupLease] | None = None,
    ) -> tuple[BrowserPageCleanupLease, ...]: ...


class _ChromeRenderLifecycleState(str, Enum):
    ACCEPTING_WORK = "accepting_work"
    CANCEL_REQUESTED = "cancel_requested"


# Compatibility exports. Provider behavior is owned by the resolver package.
LinkedInJobBoardAdapter = LinkedInCaptureProviderAdapter
IndeedJobBoardAdapter = IndeedCaptureProviderAdapter


class ExternalPostingUrlPolicy:
    def __init__(self, delegate: UrlFetchPolicy | None = None) -> None:
        self.delegate = delegate or UrlFetchPolicy()

    def check(self, url: str) -> UrlPolicyResult:
        if external_aggregator_kind(url):
            return UrlPolicyResult(False, "external_aggregator_blocked")
        return self.delegate.check(url)


class ExternalAggregatorFallbackUrlPolicy:
    def __init__(self, source_url: str, delegate: UrlFetchPolicy | None = None) -> None:
        self.source_kind = external_aggregator_kind(source_url)
        self.delegate = delegate or UrlFetchPolicy()

    def check(self, url: str) -> UrlPolicyResult:
        result = self.delegate.check(url)
        if not result.allowed:
            return result
        aggregator_kind = external_aggregator_kind(url)
        if aggregator_kind and aggregator_kind != self.source_kind:
            return UrlPolicyResult(False, "external_aggregator_blocked")
        if aggregator_kind == self.source_kind:
            return UrlPolicyResult(True, "external_aggregator_fallback_allowed")
        return result


Runner = Callable[..., subprocess.CompletedProcess]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class CodexCliRenderedPageResolver:
    _throttle_lock = threading.Lock()
    _next_start_at = 0.0

    def __init__(
        self,
        *,
        command: str = "codex",
        runner: Runner | None = None,
        timeout_seconds: int = 60,
        retries: int = 0,
        min_start_interval_seconds: float = 0.0,
        workspace: str | Path | None = None,
        ignore_user_config: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
        url_policy: UrlFetchPolicy | None = None,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.command = command
        self.runner = runner or run_with_process_tree_cleanup
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.min_start_interval_seconds = min_start_interval_seconds
        self.workspace = workspace
        self.ignore_user_config = ignore_user_config
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.url_policy = url_policy or UrlFetchPolicy()
        self.sleeper = sleeper
        self.clock = clock

    @bounded_resource("browser")
    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        policy_result = ExternalAggregatorFallbackUrlPolicy(
            hint.source_url,
            self.url_policy,
        ).check(hint.source_url)
        if not policy_result.allowed:
            return PostingResolution(
                status="needs_review",
                error=f"codex_browser_url_policy_blocked: {policy_result.reason}",
            )

        prompt = _codex_rendered_page_prompt(hint)
        attempts = max(1, self.retries + 1)
        for attempt in range(attempts):
            self._wait_for_rendered_slot()
            result = run_codex_prompt(
                self.command,
                runner=self.runner,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
                workspace=self.workspace,
                ignore_user_config=self.ignore_user_config,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                prompt_as_stdin=False,
                unavailable_error="codex_playwright_unavailable",
                timeout_error="codex_playwright_timeout",
                failure_prefix="codex_playwright_failed",
                operation="posting_rendered_fallback",
            )
            if not result.error:
                break
            if result.error != "codex_playwright_timeout" or attempt == attempts - 1:
                return PostingResolution(status="needs_review", error=result.error)

        posting = _posting_from_codex_rendered_output(result.stdout, hint)
        if posting is None:
            return PostingResolution(status="needs_review", error="codex_playwright_invalid_json")
        observation = observe_rendered_posting(posting, hint)
        if observation.identity is not RenderedIdentityVerdict.MATCH:
            error = (
                "codex_playwright_url_mismatch"
                if observation.identity is RenderedIdentityVerdict.MISMATCH
                else "codex_playwright_identity_ambiguous"
            )
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error=error,
            )
        if observation.access is not RenderedAccessVerdict.USABLE:
            error = (
                "codex_playwright_auth_wall"
                if observation.access is RenderedAccessVerdict.CHALLENGE
                else "codex_playwright_blocked_page"
            )
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error=error,
            )
        if external_job_board_terminal_status(posting.source_url, posting.raw_text) is not None:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="codex_playwright_terminal_evidence_untrusted",
            )
        if posting.confidence < 0.8:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="codex_playwright_low_confidence",
            )
        return PostingResolution(status="resolved", posting=posting)

    def _wait_for_rendered_slot(self) -> None:
        if self.min_start_interval_seconds <= 0:
            return
        with self._throttle_lock:
            now = self.clock()
            wait_seconds = max(0.0, self._next_start_at - now)
            self.__class__._next_start_at = (
                max(now, self._next_start_at) + self.min_start_interval_seconds
            )
        if wait_seconds:
            self.sleeper(wait_seconds)


_CHROME_WEBRTC_PROXY_ONLY_ARG = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
_DISABLE_WEBRTC_INIT_SCRIPT = """
(() => {
  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection"]) {
    Object.defineProperty(globalThis, name, {
      value: undefined,
      writable: false,
      configurable: false,
      enumerable: false
    });
    if (globalThis[name] !== undefined) {
      throw new Error("JobCockpit WebRTC hardening failed");
    }
  }
})();
"""
_VERIFY_WEBRTC_DISABLED_SCRIPT = """
() => ["RTCPeerConnection", "webkitRTCPeerConnection"].every((name) => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
  return (
    globalThis[name] === undefined &&
    descriptor !== undefined &&
    descriptor.value === undefined &&
    descriptor.writable === false &&
    descriptor.configurable === false
  );
})
"""


class PlaywrightProfileRenderedPageResolver:
    _throttle_lock = threading.Lock()
    _next_start_at = 0.0
    capture_html = False
    route_websockets = True
    capture_evidence = "Extracted from rendered page with direct Playwright using a persistent profile"

    def __init__(
        self,
        *,
        user_data_dir: str | Path = Path("var/playwright-profile"),
        profile_directory: str | None = None,
        channel: str | None = None,
        headless: bool = True,
        auth_wall_wait_seconds: float = 120.0,
        auth_wall_settle_seconds: float = 5.0,
        timeout_seconds: int = 60,
        retries: int = 0,
        min_start_interval_seconds: float = 0.0,
        url_policy: UrlFetchPolicy | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.profile_directory = profile_directory
        self.channel = channel
        self.headless = headless
        self.auth_wall_wait_seconds = auth_wall_wait_seconds
        self.auth_wall_settle_seconds = auth_wall_settle_seconds
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.min_start_interval_seconds = min_start_interval_seconds
        self.url_policy = url_policy or UrlFetchPolicy()
        self.playwright_factory = playwright_factory
        self.sleeper = sleeper
        self.clock = clock

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        return self.load_page(hint, _require_resolved_posting=True)

    @bounded_resource("browser")
    def load_page(
        self, hint: ExternalPostingHint, *, _require_resolved_posting: bool = False,
    ) -> PostingResolution:
        policy_result = ExternalAggregatorFallbackUrlPolicy(hint.source_url, self.url_policy).check(
            hint.source_url
        )
        if not policy_result.allowed:
            return PostingResolution(
                status="needs_review",
                error=f"playwright_url_policy_blocked: {policy_result.reason}",
            )

        attempts = max(1, self.retries + 1)
        last_error = ""
        for attempt in range(attempts):
            try:
                self._wait_for_rendered_slot()
                posting = self._render(hint)
                break
            except _PlaywrightRenderedTimeout:
                if attempt == attempts - 1:
                    return PostingResolution(status="needs_review", error="playwright_timeout")
                last_error = "playwright_timeout"
            except _PlaywrightRenderedError as error:
                last_error = str(error)
                if attempt == attempts - 1:
                    return PostingResolution(
                        status="needs_review",
                        error=f"playwright_failed: {_clean_text(last_error)[:240]}",
                    )
        else:
            return PostingResolution(
                status="needs_review",
                error=f"playwright_failed: {_clean_text(last_error)[:240]}",
            )

        if not _rendered_posting_matches_hint(posting, hint):
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="playwright_url_mismatch",
            )
        if not _require_resolved_posting:
            return PostingResolution(status="resolved", posting=posting)
        if resolved_external_posting_terminal_status(posting) is not None:
            return PostingResolution(status="resolved", posting=posting)
        if posting.content_truncated:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="playwright_content_truncated",
            )
        if posting.confidence < 0.8:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="playwright_low_confidence",
            )
        return PostingResolution(status="resolved", posting=posting)

    def _render(self, hint: ExternalPostingHint) -> ResolvedExternalPosting:
        timeout_ms = self.timeout_seconds * 1000
        policy_guard: _PlaywrightUrlPolicyGuard | None = None
        try:
            with ExitStack() as render_stack:
                # Chromium permits only one persistent browser per user-data
                # directory. Keep this OS lock through context cleanup and
                # Playwright shutdown, across both threads and API/CLI processes.
                profile_dir = self._profile_directory(hint)
                render_stack.enter_context(_persistent_storage_state_lock(
                    profile_dir / ".jobcockpit-profile",
                    timeout_seconds=max(
                        30.0, self.timeout_seconds + self.auth_wall_wait_seconds
                        + self.auth_wall_settle_seconds + 10.0,
                    ),
                ))
                pinned_proxy = _enter_pinned_browser_proxy(
                    render_stack, hint.source_url, self.url_policy
                )
                browser_context = self._launch_browser_context(
                    render_stack, profile_dir, pinned_proxy, timeout_ms
                )
                try:
                    _install_rendered_context_hardening(browser_context)
                    policy_guard = _PlaywrightUrlPolicyGuard(
                        source_url=hint.source_url,
                        url_policy=self.url_policy,
                    )
                    policy_guard.install(browser_context, route_websockets=self.route_websockets)
                    page = browser_context.new_page()
                    self._verify_page_hardening(page)
                    try:
                        page.goto(
                            hint.source_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    except Exception:
                        policy_guard.raise_if_blocked()
                        _raise_if_pinned_proxy_blocked(pinned_proxy)
                        raise
                    with suppress(Exception):
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
                    policy_guard.raise_if_blocked()
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                    if self._wait_for_auth_wall_completion(page, hint):
                        self._settle_after_auth_wall_completion(page, timeout_ms)
                    source_url = str(page.url or hint.source_url)
                    policy_guard.ensure_final_url_allowed(source_url)
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                    snapshot, rendered_html = self._capture_rendered_page(page, hint)
                    source_url = str(page.url or hint.source_url)
                    policy_guard.ensure_final_url_allowed(source_url)
                    policy_guard.raise_if_blocked()
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                finally:
                    try:
                        if not close_owned_browser_context(browser_context):
                            raise _PlaywrightRenderedError("owned_context_cleanup_failed")
                    finally:
                        if policy_guard is not None:
                            policy_guard.close()
        except _playwright_timeout_error() as error:
            raise _PlaywrightRenderedTimeout(str(error)) from error
        except OSError as error:
            raise _PlaywrightRenderedError(str(error)) from error
        except Exception as error:
            if (
                "playwright" in error.__class__.__module__
                and "timeout" in error.__class__.__name__.lower()
            ):
                raise _PlaywrightRenderedTimeout(str(error)) from error
            raise _PlaywrightRenderedError(str(error)) from error

        if not snapshot.body_text.strip():
            raise _PlaywrightRenderedError("empty_rendered_page")
        if _looks_like_blocked_page(snapshot.rendered_text):
            raise _PlaywrightRenderedError("blocked_rendered_page")
        if _is_auth_wall_page(source_url, snapshot.rendered_text):
            raise _PlaywrightRenderedError("auth_wall_unresolved")
        confidence = _aggregator_fallback_confidence(
            hint,
            snapshot.rendered_text,
            SearchResult(title="", url=source_url),
        )
        return ResolvedExternalPosting(
            source_url=source_url,
            raw_text=snapshot.raw_text,
            confidence=confidence,
            evidence=self.capture_evidence,
            evidence_origin="browser_dom",
            content_truncated=snapshot.content_truncated,
            complete_body_text=snapshot.body_text,
            rendered_text=snapshot.rendered_text,
            rendered_html=rendered_html,
        )

    def _capture_rendered_page(self, page: Any, hint: ExternalPostingHint):
        snapshot = _settled_workday_page_snapshot(page, hint, self.timeout_seconds * 1000)
        return snapshot, page.content() if self.capture_html else ""

    def _profile_directory(self, hint: ExternalPostingHint) -> Path:
        return self.user_data_dir.resolve()

    def _verify_page_hardening(self, page: Any) -> None:
        _verify_rendered_page_hardening(page)

    def _launch_browser_context(
        self, render_stack: ExitStack, profile_dir: Path,
        pinned_proxy: PinnedBrowserProxy | None, timeout_ms: int,
    ) -> Any:
        playwright = render_stack.enter_context(self._playwright_context_manager())
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": self.headless,
            "timeout": timeout_ms,
            "service_workers": "block",
        }
        if pinned_proxy is not None:
            launch_kwargs["proxy"] = pinned_proxy.playwright_proxy
        if self.channel:
            launch_kwargs["channel"] = self.channel
        profile_arg = chrome_profile_directory_arg(self.profile_directory)
        launch_kwargs["args"] = [_CHROME_WEBRTC_PROXY_ONLY_ARG]
        if profile_arg:
            launch_kwargs["args"].append(profile_arg)
        return playwright.chromium.launch_persistent_context(**launch_kwargs)

    def _playwright_context_manager(self) -> Any:
        if self.playwright_factory is not None:
            return self.playwright_factory()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise _PlaywrightRenderedError(
                "playwright is not installed; run `uv sync` in the selected runtime "
                "(see docs/runtime-operations.md) and `pnpm --silent python -m playwright install chromium`"
            ) from error
        return sync_playwright()

    def _wait_for_auth_wall_completion(self, page: Any, hint: ExternalPostingHint) -> bool:
        if self.auth_wall_wait_seconds <= 0:
            return False
        page_text = _safe_rendered_page_text(page, hint)
        if not _is_auth_wall_page(str(getattr(page, "url", "") or hint.source_url), page_text):
            return False
        if not hasattr(page, "wait_for_timeout"):
            return False

        deadline = self.clock() + self.auth_wall_wait_seconds
        while self.clock() < deadline:
            wait_ms = min(1000, max(1, int((deadline - self.clock()) * 1000)))
            with suppress(Exception):
                page.wait_for_timeout(wait_ms)
            page_text = _safe_rendered_page_text(page, hint)
            if not _is_auth_wall_page(str(getattr(page, "url", "") or hint.source_url), page_text):
                return True
        return False

    def _settle_after_auth_wall_completion(self, page: Any, timeout_ms: int) -> None:
        with suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        if self.auth_wall_settle_seconds <= 0 or not hasattr(page, "wait_for_timeout"):
            return
        with suppress(Exception):
            page.wait_for_timeout(int(self.auth_wall_settle_seconds * 1000))

    def _wait_for_rendered_slot(self) -> None:
        if self.min_start_interval_seconds <= 0:
            return
        with self._throttle_lock:
            now = self.clock()
            wait_seconds = max(0.0, self._next_start_at - now)
            self.__class__._next_start_at = (
                max(now, self._next_start_at) + self.min_start_interval_seconds
            )
        if wait_seconds:
            self.sleeper(wait_seconds)


class ChromeCdpRenderedPageResolver(PlaywrightProfileRenderedPageResolver):
    _persistent_sessions_lock = threading.Lock()
    _persistent_sessions: dict[tuple[str, ...], _PersistentSourceSession] = {}

    def __init__(
        self,
        *,
        endpoint_url: str = "http://127.0.0.1:9222",
        auth_wall_wait_seconds: float = 120.0,
        auth_wall_settle_seconds: float = 5.0,
        timeout_seconds: int = 60,
        retries: int = 0,
        min_start_interval_seconds: float = 0.0,
        auto_start: bool = False,
        executable_path: str | Path | None = None,
        user_data_dir: str | Path = Path("var/chrome-cdp-profile"),
        persistent_session_state_dir: str | Path = Path("var/chrome-cdp-source-sessions"),
        profile_directory: str | None = None,
        headless: bool = True,
        persistent_source_session: bool = False,
        chrome_launcher: Callable[[], None] | None = None,
        url_policy: UrlFetchPolicy | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(
            auth_wall_wait_seconds=auth_wall_wait_seconds,
            auth_wall_settle_seconds=auth_wall_settle_seconds,
            timeout_seconds=timeout_seconds,
            retries=retries,
            min_start_interval_seconds=min_start_interval_seconds,
            url_policy=url_policy,
            playwright_factory=playwright_factory,
            sleeper=sleeper,
            clock=clock,
        )
        self.endpoint_url = validate_local_cdp_endpoint(endpoint_url).geturl()
        self.auto_start = auto_start
        self.executable_path = Path(executable_path) if executable_path else None
        self.user_data_dir = Path(user_data_dir)
        self.persistent_session_state_dir = Path(persistent_session_state_dir)
        self.profile_directory = profile_directory
        self.headless = headless
        self.persistent_source_session = persistent_source_session
        self.chrome_launcher = chrome_launcher
        self._active_cleanup_lock = threading.Lock()
        self._cleanup_finalization_lock = threading.Lock()
        self._active_cleanup_leases: list[BrowserPageCleanupLease] = []
        self._render_lifecycle_state = _ChromeRenderLifecycleState.ACCEPTING_WORK
        self._active_navigation_claims = 0

    @property
    def cleanup_finalization_lock(self) -> threading.Lock:
        return self._cleanup_finalization_lock

    def cancel_active_pages(self) -> int:
        """Latch cancellation; the shared supervisor retries every failed close."""

        with self._active_cleanup_lock:
            self._render_lifecycle_state = _ChromeRenderLifecycleState.CANCEL_REQUESTED
            leases = tuple(self._active_cleanup_leases)
        closed_count = 0
        for lease in leases:
            if lease.close(timeout_seconds=0.5):
                closed_count += 1
                self._release_cleanup_lease(lease)
            else:
                request_browser_cleanup((lease,))
        return closed_count

    def _register_cleanup_lease(self, lease: BrowserPageCleanupLease) -> bool:
        """Register a target and report whether rendering may continue."""

        try:
            register_browser_cleanup_lease(lease)
        except Exception:
            with self._active_cleanup_lock:
                self._active_cleanup_leases.append(lease)
            raise

        with self._active_cleanup_lock:
            close_immediately = (
                self._render_lifecycle_state is _ChromeRenderLifecycleState.CANCEL_REQUESTED
            )
            self._active_cleanup_leases.append(lease)
        if close_immediately:
            if lease.close(timeout_seconds=0.5):
                self._release_cleanup_lease(lease)
            else:
                request_browser_cleanup((lease,))
            return False
        return True

    def _raise_if_cleanup_cancelled(self) -> None:
        with self._active_cleanup_lock:
            cancelled = self._render_lifecycle_state is _ChromeRenderLifecycleState.CANCEL_REQUESTED
        if cancelled:
            raise _PlaywrightRenderedError("browser_render_cancelled")

    def _begin_navigation(self) -> None:
        """Atomically order navigation before or after terminal cancellation."""
        with self._active_cleanup_lock:
            if self._render_lifecycle_state is _ChromeRenderLifecycleState.CANCEL_REQUESTED:
                raise _PlaywrightRenderedError("browser_render_cancelled")
            self._active_navigation_claims += 1

    def _end_navigation(self) -> None:
        with self._active_cleanup_lock:
            if self._active_navigation_claims <= 0:
                raise RuntimeError("rendered navigation lifecycle is unbalanced")
            self._active_navigation_claims -= 1

    def _release_cleanup_lease(self, lease: BrowserPageCleanupLease | None) -> None:
        if lease is None:
            return
        with self._active_cleanup_lock:
            with suppress(ValueError):
                self._active_cleanup_leases.remove(lease)
        confirm_browser_cleanup(lease)

    def pending_cleanup_leases(self) -> tuple[BrowserPageCleanupLease, ...]:
        with self._active_cleanup_lock:
            return tuple(self._active_cleanup_leases)

    def release_pending_cleanup_leases(
        self,
        leases: Sequence[BrowserPageCleanupLease] | None = None,
    ) -> tuple[BrowserPageCleanupLease, ...]:
        with self._active_cleanup_lock:
            released = tuple(self._active_cleanup_leases if leases is None else leases)
            for lease in released:
                with suppress(ValueError):
                    self._active_cleanup_leases.remove(lease)
        return released

    def _persistent_session_for(self, source_url: str) -> _PersistentSourceSession:
        parsed = urlparse(source_url)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        if scheme not in {"http", "https"} or not hostname:
            raise _PlaywrightRenderedError("persistent_source_session_url_invalid")
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (
            self.endpoint_url,
            str(self.user_data_dir.absolute()).casefold(),
            (self.profile_directory or "").casefold(),
            str(self.persistent_session_state_dir.absolute()).casefold(),
            scheme,
            hostname,
            str(port),
        )
        state_name = hashlib.sha256("\n".join(key).encode("utf-8")).hexdigest()
        state_path = self.persistent_session_state_dir / f"{state_name}.json"
        with self._persistent_sessions_lock:
            session = self._persistent_sessions.get(key)
            if session is None:
                durable_state_present = state_path.is_file()
                session = _PersistentSourceSession(
                    state_path=state_path,
                    storage_state=_load_persistent_storage_state(state_path, source_url),
                    durable_state_present=durable_state_present,
                )
                self._persistent_sessions[key] = session
            return session

    @bounded_resource("browser")
    def load_page(
        self,
        hint: ExternalPostingHint,
        *,
        _require_resolved_posting: bool = False,
    ) -> PostingResolution:
        """Load one page through the shared CDP lifecycle and enforce its URL identity."""

        policy_result = ExternalAggregatorFallbackUrlPolicy(hint.source_url, self.url_policy).check(
            hint.source_url
        )
        if not policy_result.allowed:
            return PostingResolution(
                status="needs_review",
                error=f"chrome_cdp_url_policy_blocked: {policy_result.reason}",
            )

        attempts = max(1, self.retries + 1)
        last_error = ""
        for attempt in range(attempts):
            try:
                self._wait_for_rendered_slot()
                if self.persistent_source_session:
                    persistent_session = self._persistent_session_for(hint.source_url)
                    lock_timeout = max(
                        30.0,
                        self.auth_wall_wait_seconds
                        + self.auth_wall_settle_seconds
                        + self.timeout_seconds
                        + 10.0,
                    )
                    with (
                        persistent_session.lock,
                        _persistent_storage_state_lock(
                            persistent_session.state_path, timeout_seconds=lock_timeout
                        ),
                    ):
                        _refresh_persistent_storage_state(persistent_session, hint.source_url)
                        posting = self._render(
                            hint,
                            persistent_session=persistent_session,
                            require_resolved_posting=_require_resolved_posting,
                        )
                else:
                    posting = self._render(hint, require_resolved_posting=_require_resolved_posting)
                break
            except _PlaywrightRenderedTimeout:
                if attempt == attempts - 1:
                    return PostingResolution(status="needs_review", error="chrome_cdp_timeout")
                last_error = "chrome_cdp_timeout"
            except _PlaywrightRenderedError as error:
                last_error = str(error)
                if attempt == attempts - 1:
                    return PostingResolution(
                        status="needs_review",
                        error=f"chrome_cdp_failed: {_clean_text(last_error)[:240]}",
                    )
        else:
            return PostingResolution(
                status="needs_review",
                error=f"chrome_cdp_failed: {_clean_text(last_error)[:240]}",
            )

        identity_verdict = rendered_posting_identity_verdict(posting, hint)
        if identity_verdict is not RenderedIdentityVerdict.MATCH:
            error = (
                "chrome_cdp_url_mismatch"
                if identity_verdict is RenderedIdentityVerdict.MISMATCH
                else "chrome_cdp_identity_ambiguous"
            )
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error=error,
            )
        return PostingResolution(status="resolved", posting=posting)

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        resolution = self.load_page(hint, _require_resolved_posting=True)
        posting = resolution.posting
        if resolution.status != "resolved" or posting is None:
            return resolution

        if resolved_external_posting_terminal_status(posting) is not None:
            return resolution
        if posting.content_truncated:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="chrome_cdp_content_truncated",
            )
        if posting.confidence < 0.8:
            return PostingResolution(
                status="needs_review",
                candidates=(posting,),
                error="chrome_cdp_low_confidence",
            )
        return PostingResolution(status="resolved", posting=posting)

    def _render(
        self,
        hint: ExternalPostingHint,
        *,
        persistent_session: _PersistentSourceSession | None = None,
        require_resolved_posting: bool = False,
    ) -> ResolvedExternalPosting:
        timeout_ms = self.timeout_seconds * 1000
        policy_guard: _PlaywrightUrlPolicyGuard | None = None
        cleanup_lease: BrowserPageCleanupLease | None = None
        posting: ResolvedExternalPosting | None = None
        accepted_storage_state: dict[str, Any] | None = None
        try:
            self._raise_if_cleanup_cancelled()
            playwright_context_manager = self._playwright_context_manager()
            with ExitStack() as render_stack:
                pinned_proxy = _enter_pinned_browser_proxy(
                    render_stack,
                    hint.source_url,
                    self.url_policy,
                )
                playwright = render_stack.enter_context(playwright_context_manager)
                validation_context = None
                source_contexts: list[Any] = []
                owned_page_leases: dict[int, BrowserPageCleanupLease | None] = {}
                cleanup_registration_errors: list[Exception] = []
                context_page_listener: Callable[[Any], None] | None = None
                primary_popup_listener: Callable[[Any], None] | None = None
                page = None
                known_target_ids: set[str] | None = None

                def register_owned_page(owned_page: Any) -> BrowserPageCleanupLease | None:
                    page_key = id(owned_page)
                    if page_key in owned_page_leases:
                        return owned_page_leases[page_key]
                    lease = capture_browser_page_cleanup_lease(
                        validation_context,
                        owned_page,
                        cdp_endpoint_url=self.endpoint_url,
                        discovery_timeout_seconds=min(0.5, self.timeout_seconds),
                    )
                    if lease is None and known_target_ids is not None:
                        lease = capture_new_browser_page_cleanup_lease(
                            self.endpoint_url,
                            known_target_ids,
                            expected_url=str(getattr(owned_page, "url", "") or ""),
                            discovery_timeout_seconds=min(0.5, self.timeout_seconds),
                        )
                    owned_page_leases[page_key] = lease
                    if lease is None:
                        cleanup_registration_errors.append(
                            _PlaywrightRenderedError("browser_target_registration_unavailable")
                        )
                        try:
                            cleanup_result = cleanup_owned_page(
                                validation_context,
                                owned_page,
                                cdp_endpoint_url=self.endpoint_url,
                                cleanup_timeout_seconds=min(0.5, self.timeout_seconds),
                            )
                        except Exception:
                            cleanup_result = None
                        if cleanup_result is None or not cleanup_result.page_closed:
                            cleanup_registration_errors.append(
                                _PlaywrightRenderedError("browser_target_cleanup_unconfirmed")
                            )
                        return None
                    if known_target_ids is not None:
                        known_target_ids.add(lease.target_id)
                    try:
                        may_continue = self._register_cleanup_lease(lease)
                    except Exception as error:
                        cleanup_registration_errors.append(error)
                        if lease.close(timeout_seconds=0.5):
                            self._release_cleanup_lease(lease)
                    else:
                        if not may_continue:
                            cleanup_registration_errors.append(
                                _PlaywrightRenderedError("browser_render_cancelled")
                            )
                    return lease

                def raise_cleanup_registration_error() -> None:
                    if not cleanup_registration_errors:
                        return
                    error = cleanup_registration_errors[0]
                    if isinstance(error, _PlaywrightRenderedError):
                        raise error
                    raise _PlaywrightRenderedError("browser_target_registration_failed") from error

                try:
                    browser = self._connect_browser(playwright, timeout_ms)
                    target_snapshot = browser_page_target_ids(
                        self.endpoint_url,
                        timeout_seconds=min(0.5, self.timeout_seconds),
                    )
                    known_target_ids = set(target_snapshot) if target_snapshot is not None else None
                    source_contexts = list(getattr(browser, "contexts", []) or [])
                    source_context = source_contexts[0] if source_contexts else None
                    source_cookies: list[dict[str, Any]] = []
                    validation_context_kwargs: dict[str, Any] = {
                        "service_workers": "block",
                        "proxy": pinned_proxy.playwright_proxy,
                    }
                    if persistent_session is not None:
                        storage_state = persistent_session.storage_state
                        if storage_state is None and source_context is not None:
                            storage_state = _source_scoped_browser_storage_state(
                                source_context, hint.source_url
                            )
                        if storage_state and (storage_state["cookies"] or storage_state["origins"]):
                            validation_context_kwargs["storage_state"] = storage_state
                    elif source_context is not None:
                        source_cookies = _source_scoped_browser_cookies(
                            source_context,
                            hint.source_url,
                        )
                    validation_context = browser.new_context(**validation_context_kwargs)
                    _install_rendered_context_hardening(validation_context)
                    if persistent_session is None and source_cookies:
                        validation_context.add_cookies(source_cookies)
                    policy_guard = _PlaywrightUrlPolicyGuard(
                        source_url=hint.source_url,
                        url_policy=self.url_policy,
                    )
                    policy_guard.install(validation_context)
                    context_on = getattr(validation_context, "on", None)
                    context_remove_listener = getattr(validation_context, "remove_listener", None)
                    if not callable(context_remove_listener):
                        context_remove_listener = getattr(validation_context, "off", None)
                    if not callable(context_on) or not callable(context_remove_listener):
                        raise _PlaywrightRenderedError("browser_context_page_tracking_unavailable")
                    context_page_listener = register_owned_page
                    context_on("page", context_page_listener)
                    page = validation_context.new_page()
                    cleanup_lease = register_owned_page(page)
                    if cleanup_lease is None:
                        raise_cleanup_registration_error()
                        raise _PlaywrightRenderedError("browser_target_registration_unavailable")
                    raise_cleanup_registration_error()
                    self._raise_if_cleanup_cancelled()
                    page_on = getattr(page, "on", None)
                    if callable(page_on):
                        primary_popup_listener = register_owned_page
                        page_on("popup", primary_popup_listener)
                    _verify_rendered_page_hardening(page)
                    self._begin_navigation()
                    try:
                        try:
                            page.goto(
                                hint.source_url,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                        except Exception:
                            policy_guard.raise_if_blocked()
                            _raise_if_pinned_proxy_blocked(pinned_proxy)
                            raise
                    finally:
                        self._end_navigation()
                    raise_cleanup_registration_error()
                    with suppress(Exception):
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
                    raise_cleanup_registration_error()
                    policy_guard.raise_if_blocked()
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                    if self._wait_for_auth_wall_completion(page, hint):
                        self._settle_after_auth_wall_completion(page, timeout_ms)
                    source_url = str(page.url or hint.source_url)
                    policy_guard.ensure_final_url_allowed(source_url)
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                    snapshot = _settled_workday_page_snapshot(page, hint, timeout_ms)
                    page_content = getattr(page, "content", None)
                    rendered_html = str(page_content()) if callable(page_content) else ""
                    source_url = str(page.url or hint.source_url)
                    policy_guard.ensure_final_url_allowed(source_url)
                    _raise_if_pinned_proxy_blocked(pinned_proxy)
                    if not snapshot.body_text.strip():
                        blocked_subresource = policy_guard.blocked_subresource_diagnostic()
                        if blocked_subresource:
                            raise _PlaywrightRenderedError(blocked_subresource)
                        raise _PlaywrightRenderedError("empty_rendered_page")
                    confidence = _aggregator_fallback_confidence(
                        hint,
                        snapshot.rendered_text,
                        SearchResult(title="", url=source_url),
                    )
                    posting = ResolvedExternalPosting(
                        source_url=source_url,
                        raw_text=snapshot.raw_text,
                        confidence=confidence,
                        evidence=(
                            "Extracted from rendered page with Chrome CDP attached to an "
                            "existing browser"
                        ),
                        evidence_origin="browser_dom",
                        content_truncated=snapshot.content_truncated,
                        complete_body_text=snapshot.body_text,
                        rendered_text=snapshot.rendered_text,
                        rendered_html=rendered_html,
                    )
                    access_verdict = rendered_posting_access_verdict(posting)
                    if access_verdict is RenderedAccessVerdict.BLOCKED:
                        raise _PlaywrightRenderedError("blocked_rendered_page")
                    if access_verdict is RenderedAccessVerdict.CHALLENGE:
                        raise _PlaywrightRenderedError("auth_wall_unresolved")
                    if persistent_session is not None and _persistent_state_is_accepted(
                        posting, hint, require_resolved_posting=require_resolved_posting
                    ):
                        accepted_storage_state = _source_scoped_browser_storage_state(
                            validation_context, hint.source_url
                        )
                finally:
                    persistence_error: Exception | None = None
                    try:
                        if persistent_session is not None and accepted_storage_state is not None:
                            try:
                                _persist_persistent_storage_state(
                                    persistent_session.state_path, accepted_storage_state
                                )
                                persistent_session.storage_state = accepted_storage_state
                                persistent_session.durable_state_present = True
                            except Exception as error:
                                persistence_error = error
                        if page is not None and primary_popup_listener is not None:
                            remove_listener = getattr(page, "remove_listener", None)
                            if not callable(remove_listener):
                                remove_listener = getattr(page, "off", None)
                            if callable(remove_listener):
                                with suppress(Exception):
                                    remove_listener("popup", primary_popup_listener)
                        if validation_context is not None and context_page_listener is not None:
                            remove_listener = getattr(
                                validation_context,
                                "remove_listener",
                                None,
                            )
                            if not callable(remove_listener):
                                remove_listener = getattr(validation_context, "off", None)
                            if callable(remove_listener):
                                with suppress(Exception):
                                    remove_listener("page", context_page_listener)

                        context_disposed = validation_context is None
                        tabs_closed = False
                        if validation_context is not None:
                            try:
                                cleanup_result = cleanup_owned_browser_context(
                                    validation_context,
                                    cdp_endpoint_url=self.endpoint_url,
                                )
                            except Exception:
                                cleanup_result = None
                            if cleanup_result is not None:
                                context_disposed = cleanup_result.context_disposed
                                tabs_closed = cleanup_result.all_tabs_closed

                        cleanup_leases = tuple(
                            lease for lease in owned_page_leases.values() if lease is not None
                        )
                        if context_disposed or tabs_closed:
                            for lease in cleanup_leases:
                                self._release_cleanup_lease(lease)
                        else:
                            for lease in cleanup_leases:
                                if lease.close(timeout_seconds=0.5):
                                    self._release_cleanup_lease(lease)
                                else:
                                    request_browser_cleanup((lease,))
                        if not context_disposed:
                            raise _PlaywrightRenderedError("owned_context_cleanup_failed")
                        if persistence_error is not None:
                            raise persistence_error
                    finally:
                        if policy_guard is not None:
                            policy_guard.close()
        except _playwright_timeout_error() as error:
            raise _PlaywrightRenderedTimeout(str(error)) from error
        except OSError as error:
            raise _PlaywrightRenderedError(str(error)) from error
        except Exception as error:
            if (
                "playwright" in error.__class__.__module__
                and "timeout" in error.__class__.__name__.lower()
            ):
                raise _PlaywrightRenderedTimeout(str(error)) from error
            raise _PlaywrightRenderedError(str(error)) from error

        if posting is None:
            raise _PlaywrightRenderedError("rendered_posting_unavailable")
        return posting

    def _connect_browser(self, playwright: Any, timeout_ms: int) -> Any:
        return connect_chrome_cdp(
            playwright.chromium,
            endpoint_url=self.endpoint_url,
            timeout_seconds=timeout_ms / 1000.0,
            auto_start=self.auto_start,
            launcher=self._launch_chrome,
            clock=self.clock,
            sleeper=self.sleeper,
        )

    def _launch_chrome(self) -> None:
        if self.chrome_launcher is not None:
            self.chrome_launcher()
            return
        launch_chrome_cdp(
            endpoint_url=self.endpoint_url,
            executable_path=self.executable_path,
            user_data_dir=self.user_data_dir,
            profile_directory=self.profile_directory,
            headless=self.headless,
            extra_args=(_CHROME_WEBRTC_PROXY_ONLY_ARG,),
        )


class TracedRenderedPageResolver:
    def __init__(
        self,
        *,
        inner_resolver: RenderedPostingResolver,
        provider: str,
    ) -> None:
        self.inner_resolver = inner_resolver
        self.provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner_resolver, name)

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        resolution = normalize_posting_resolution(self.inner_resolver.resolve(hint))
        return _trace_rendered_resolution(
            resolution,
            hint=hint,
            provider=self.provider,
        )


class PrioritizedRenderedPageResolver:
    def __init__(
        self,
        *,
        primary_resolver: RenderedPostingResolver,
        fallback_resolver: RenderedPostingResolver,
        primary_provider: str = "",
        fallback_provider: str = "",
    ) -> None:
        self.primary_resolver = primary_resolver
        self.fallback_resolver = fallback_resolver
        self.primary_provider = primary_provider or _rendered_provider_code(primary_resolver)
        self.fallback_provider = fallback_provider or _rendered_provider_code(fallback_resolver)

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        return self._resolve_using(hint, "resolve")

    def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
        return self._resolve_using(hint, "load_page")

    def cancel_active_pages(self) -> None:
        for resolver in (self.primary_resolver, self.fallback_resolver):
            cancel = getattr(resolver, "cancel_active_pages", None)
            if callable(cancel):
                cancel()

    def _resolve_using(self, hint: ExternalPostingHint, method: str) -> PostingResolution:
        primary = normalize_posting_resolution(getattr(self.primary_resolver, method)(hint))
        primary = _trace_rendered_resolution(
            primary,
            hint=hint,
            provider=self.primary_provider,
        )
        if primary.status == "resolved" or _rendered_url_policy_blocked(primary):
            return primary

        fallback = normalize_posting_resolution(getattr(self.fallback_resolver, method)(hint))
        fallback = _trace_rendered_resolution(
            fallback,
            hint=hint,
            provider=self.fallback_provider,
        )
        if fallback.status == "resolved":
            traced = replace(fallback, attempts=(), trace_truncated=False)
            return _with_resolution_attempts(traced, primary, fallback)

        candidates = (*primary.candidates, *fallback.candidates)
        errors = [error for error in (primary.error, fallback.error) if error]
        if fallback.status != "not_found":
            combined = replace(
                fallback,
                candidates=candidates,
                error=" | ".join(dict.fromkeys(errors)),
                attempts=(),
                trace_truncated=False,
            )
            return _with_resolution_attempts(combined, primary, fallback)
        if primary.status != "not_found":
            combined = replace(
                primary,
                candidates=candidates,
                error=" | ".join(dict.fromkeys(errors)),
                attempts=(),
                trace_truncated=False,
            )
            return _with_resolution_attempts(combined, primary, fallback)
        combined = PostingResolution(
            status="not_found",
            candidates=candidates,
            error=" | ".join(dict.fromkeys(errors)),
        )
        return _with_resolution_attempts(combined, primary, fallback)


def _trace_rendered_resolution(
    resolution: PostingResolution,
    *,
    hint: object,
    provider: str,
) -> PostingResolution:
    stage = (
        "official_candidate_render"
        if getattr(hint, "source_kind", "") == "official_candidate"
        else "source_render"
    )
    candidate_url = (
        str(getattr(hint, "source_url", "")) if stage == "official_candidate_render" else ""
    )
    return _with_resolution_attempts(
        resolution,
        extra=(
            _resolution_attempt(
                resolution,
                stage=stage,
                provider=provider,
                candidate_url=candidate_url,
            ),
        ),
    )


def _rendered_provider_code(resolver: object) -> str:
    class_name = resolver.__class__.__name__.casefold()
    if "invisibleplaywright" in class_name:
        return "invisible_playwright"
    if "chromecdp" in class_name or "chrome_cdp" in class_name:
        return "chrome_cdp"
    if "codex" in class_name:
        return "codex_rendered"
    if "playwright" in class_name:
        return "playwright_profile"
    return "rendered"


def _source_scoped_browser_cookies(
    source_context: Any,
    source_url: str,
) -> list[dict[str, Any]]:
    """Copy only unexpired cookies that the browser would send to the stored URL."""
    cookies = getattr(source_context, "cookies", None)
    if not callable(cookies):
        raise _PlaywrightRenderedError("source_cookie_read_unavailable")
    try:
        candidates = cookies([source_url])
    except Exception as error:
        raise _PlaywrightRenderedError("source_cookie_read_failed") from error
    if not isinstance(candidates, list):
        raise _PlaywrightRenderedError("source_cookie_read_invalid")

    return _source_scoped_cookie_candidates(candidates, source_url)


def _source_origin_browser_cookies(
    source_context: Any,
    source_url: str,
) -> list[dict[str, Any]]:
    cookies = getattr(source_context, "cookies", None)
    if not callable(cookies):
        raise _PlaywrightRenderedError("source_cookie_read_unavailable")
    try:
        candidates = cookies()
    except Exception as error:
        raise _PlaywrightRenderedError("source_cookie_read_failed") from error
    if not isinstance(candidates, list):
        raise _PlaywrightRenderedError("source_cookie_read_invalid")
    return _source_scoped_cookie_candidates(candidates, source_url, require_request_match=False)


def _source_scoped_cookie_candidates(
    candidates: Sequence[object],
    source_url: str,
    *,
    require_request_match: bool = True,
) -> list[dict[str, Any]]:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    request_path = parsed.path or "/"
    secure_request = parsed.scheme.casefold() == "https"
    now = time.time()
    allowed_keys = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
    scoped: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = candidate.get("name")
        value = candidate.get("value")
        domain = str(candidate.get("domain") or "")
        cookie_domain = domain.lstrip(".").rstrip(".").casefold()
        cookie_path = str(candidate.get("path") or "/")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        if not cookie_domain or not (
            hostname == cookie_domain or hostname.endswith(f".{cookie_domain}")
        ):
            continue
        if require_request_match and candidate.get("secure") is True and not secure_request:
            continue
        if require_request_match and not _cookie_path_matches(request_path, cookie_path):
            continue
        expires = candidate.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        scoped.append({key: candidate[key] for key in allowed_keys if key in candidate})
    return scoped


def _source_scoped_browser_storage_state(
    source_context: Any,
    source_url: str,
) -> dict[str, Any]:
    storage_state_reader = getattr(source_context, "storage_state", None)
    raw_state: object = {}
    if callable(storage_state_reader):
        try:
            raw_state = storage_state_reader(indexed_db=True)
        except TypeError:
            # Playwright before 1.51 and simple test doubles do not accept this option.
            raw_state = storage_state_reader()
        except Exception as error:
            raise _PlaywrightRenderedError("source_storage_state_read_failed") from error
    sanitized_state = _sanitize_persistent_storage_state(raw_state, source_url)
    return {
        "cookies": _source_origin_browser_cookies(source_context, source_url),
        "origins": sanitized_state["origins"] if sanitized_state is not None else [],
    }


def _persistent_state_is_accepted(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
    *,
    require_resolved_posting: bool,
) -> bool:
    observation = observe_rendered_posting(posting, hint)
    if (
        observation.identity is not RenderedIdentityVerdict.MATCH
        or observation.access is not RenderedAccessVerdict.USABLE
    ):
        return False
    if posting.content_truncated:
        return False
    return not require_resolved_posting or posting.confidence >= 0.8


def _sanitize_persistent_storage_state(
    raw_state: object,
    source_url: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_state, dict):
        return None
    raw_cookies = raw_state.get("cookies", [])
    raw_origins = raw_state.get("origins", [])
    if not isinstance(raw_cookies, list) or not isinstance(raw_origins, list):
        return None
    origins: list[dict[str, Any]] = []
    for origin in raw_origins:
        if not isinstance(origin, dict):
            continue
        origin_url = str(origin.get("origin") or "")
        local_storage = origin.get("localStorage", [])
        if not _same_origin(origin_url, source_url) or not isinstance(local_storage, list):
            continue
        sanitized_items = [
            {"name": item["name"], "value": item["value"]}
            for item in local_storage
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        ]
        sanitized_origin = {"origin": origin_url, "localStorage": sanitized_items}
        indexed_db = origin.get("indexedDB")
        if isinstance(indexed_db, list):
            sanitized_origin["indexedDB"] = indexed_db
        origins.append(sanitized_origin)
    return {
        "cookies": _source_scoped_cookie_candidates(
            raw_cookies, source_url, require_request_match=False
        ),
        "origins": origins,
    }


def _load_persistent_storage_state(
    state_path: Path,
    source_url: str,
) -> dict[str, Any] | None:
    try:
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return _sanitize_persistent_storage_state(raw_state, source_url)


def _refresh_persistent_storage_state(
    session: _PersistentSourceSession,
    source_url: str,
) -> None:
    if session.state_path.is_file():
        session.storage_state = _load_persistent_storage_state(session.state_path, source_url)
        session.durable_state_present = True
    elif session.durable_state_present:
        session.storage_state = None
        session.durable_state_present = False


def _persist_persistent_storage_state(
    state_path: Path,
    storage_state: dict[str, Any],
) -> None:
    temporary_path = state_path.with_name(
        f".{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(storage_state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(state_path)
    except Exception as error:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise _PlaywrightRenderedError("persistent_storage_state_write_failed") from error


def _cookie_path_matches(request_path: str, cookie_path: str) -> bool:
    normalized_path = cookie_path if cookie_path.startswith("/") else "/"
    if request_path == normalized_path:
        return True
    if not request_path.startswith(normalized_path):
        return False
    return normalized_path.endswith("/") or request_path[len(normalized_path)] == "/"


def _rendered_url_policy_blocked(resolution: PostingResolution) -> bool:
    return "url_policy_blocked:" in resolution.error


def _enter_pinned_browser_proxy(
    render_stack: ExitStack,
    source_url: str,
    url_policy: UrlFetchPolicy,
) -> PinnedBrowserProxy:
    return render_stack.enter_context(
        PinnedBrowserProxy(
            source_url=source_url,
            url_policy=url_policy,
        )
    )


def _install_rendered_context_hardening(browser_context: Any) -> None:
    add_init_script = getattr(browser_context, "add_init_script", None)
    if not callable(add_init_script):
        raise _PlaywrightRenderedError("rendered_context_hardening_unavailable")
    try:
        add_init_script(script=_DISABLE_WEBRTC_INIT_SCRIPT)
    except Exception as error:
        raise _PlaywrightRenderedError("rendered_context_hardening_install_failed") from error


def _verify_rendered_page_hardening(page: Any) -> None:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise _PlaywrightRenderedError("rendered_context_hardening_verification_unavailable")
    try:
        verified = evaluate(_VERIFY_WEBRTC_DISABLED_SCRIPT)
    except Exception as error:
        raise _PlaywrightRenderedError("rendered_context_hardening_verification_failed") from error
    if verified is not True:
        raise _PlaywrightRenderedError("rendered_context_hardening_verification_failed")


def _raise_if_pinned_proxy_blocked(
    pinned_proxy: PinnedBrowserProxy | None,
) -> None:
    if pinned_proxy is None:
        return
    try:
        pinned_proxy.raise_if_blocked()
    except (PinnedBrowserProxyBlocked, PinnedBrowserProxyCapacityError) as error:
        raise _PlaywrightRenderedError(str(error)) from error


class _PlaywrightRenderedError(RuntimeError):
    pass


@dataclass
class _PersistentSourceSession:
    state_path: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    storage_state: dict[str, Any] | None = None
    durable_state_present: bool = False


@contextmanager
def _persistent_storage_state_lock(
    state_path: Path,
    *,
    timeout_seconds: float,
) -> Iterator[None]:
    lock_path = state_path.with_suffix(f"{state_path.suffix}.lock")
    handle: BinaryIO | None = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                _acquire_persistent_storage_file_lock(handle)
                acquired = True
                break
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _PlaywrightRenderedError(
                        "persistent_storage_state_lock_timeout"
                    ) from error
                time.sleep(min(0.1, remaining))
        yield
    except _PlaywrightRenderedError:
        raise
    except OSError as error:
        raise _PlaywrightRenderedError("persistent_storage_state_lock_failed") from error
    finally:
        if handle is not None:
            if acquired:
                with suppress(OSError):
                    _release_persistent_storage_file_lock(handle)
            handle.close()


def _acquire_persistent_storage_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_persistent_storage_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _PlaywrightRenderedTimeout(_PlaywrightRenderedError):
    pass


_RENDERED_TITLE_LIMIT = 1_000
_RENDERED_BODY_LIMIT = 20_000
_RENDERED_NODE_LIMIT = 50_000
_RENDERED_SOURCE_URL_LIMIT = 4_096
_SAFE_BROWSER_LOCAL_SCHEMES = frozenset({"about", "blob", "data"})


@dataclass(frozen=True)
class _PlaywrightPageSnapshot:
    raw_text: str
    rendered_text: str
    body_text: str
    content_truncated: bool


class _PlaywrightUrlPolicyGuard:
    """Reject unsafe requests early and validate the final page before capture.

    The proxy enforces public IPs at connection time, including redirect hops
    that bypass routing. Public navigation and assets need no shared lifecycle.
    """

    def __init__(
        self,
        *,
        source_url: str,
        url_policy: UrlFetchPolicy,
    ) -> None:
        self.source_url = source_url
        self.url_policy = url_policy
        self.policy = ExternalAggregatorFallbackUrlPolicy(source_url, url_policy)
        self.blocked_reason = ""
        self.blocked_subresource_reasons: set[str] = set()
        self.blocked_subresource_requests: set[tuple[str, str]] = set()
        self._context_routes: list[tuple[Any, Callable[..., None]]] = []

    def install(self, browser_context: Any, *, route_websockets: bool = True) -> None:
        route = getattr(browser_context, "route", None)
        unroute = getattr(browser_context, "unroute", None)
        route_web_socket = getattr(browser_context, "route_web_socket", None)
        if not callable(route) or not callable(unroute) or (
            route_websockets and not callable(route_web_socket)
        ):
            raise _PlaywrightRenderedError("rendered_url_policy_guard_unavailable")

        request_handler = self._handle_request
        try:
            route("**/*", request_handler)
        except Exception as error:
            raise _PlaywrightRenderedError("rendered_url_policy_guard_install_failed") from error
        self._context_routes.append((browser_context, request_handler))
        if not route_websockets:
            return
        try:
            route_web_socket("**/*", self._handle_web_socket)
        except Exception as error:
            with suppress(Exception):
                unroute("**/*", request_handler)
            self._context_routes.clear()
            raise _PlaywrightRenderedError(
                "rendered_websocket_policy_guard_install_failed"
            ) from error

    def ensure_final_url_allowed(self, url: str) -> None:
        self.raise_if_blocked()
        final_check = getattr(self.url_policy, "check_browser_final_url", None)
        result = final_check(self.source_url, url) if callable(final_check) else None
        if result is None or result.allowed:
            result = self.policy.check(url)
        if not result.allowed:
            self._record_block(result.reason)
            self.raise_if_blocked()

    def raise_if_blocked(self) -> None:
        if self.blocked_reason:
            raise _PlaywrightRenderedError(f"rendered_url_policy_blocked: {self.blocked_reason}")

    def close(self) -> None:
        for browser_context, handler in reversed(self._context_routes):
            unroute = getattr(browser_context, "unroute", None)
            if callable(unroute):
                with suppress(Exception):
                    unroute("**/*", handler)
        self._context_routes.clear()
        self.blocked_subresource_reasons.clear()
        self.blocked_subresource_requests.clear()

    def blocked_subresource_diagnostic(self) -> str:
        if not self.blocked_subresource_requests:
            return ""
        reason, hostname = sorted(self.blocked_subresource_requests)[0]
        diagnostic = f"rendered_subresource_blocked: {reason}"
        if hostname:
            diagnostic = f"{diagnostic} host={hostname}"
        return diagnostic

    def _handle_request(self, route: Any, request: Any) -> None:
        request_url = _playwright_request_url(request)
        fatal_navigation = _is_top_level_navigation_request(request)
        try:
            result = self._check_network(request_url, allow_browser_local=True)
        except Exception:
            self._record_request_block(
                "url_policy_check_failed",
                fatal=fatal_navigation,
                request_url=request_url,
            )
            with suppress(Exception):
                route.abort("blockedbyclient")
            return
        if not result.allowed:
            self._record_request_block(
                result.reason,
                fatal=fatal_navigation,
                request_url=request_url,
            )
            with suppress(Exception):
                route.abort("blockedbyclient")
            return
        self._continue_request(route)

    @staticmethod
    def _continue_request(route: Any) -> None:
        try:
            route.continue_()
        except Exception as error:
            # Firefox can cancel an intercepted channel during navigation
            # or teardown before this callback resumes it. There is no live
            # request left to continue; do not retry or relax the policy.
            message = str(error)
            if not (
                "NS_BINDING_ABORTED" in message
                or "Target page, context or browser has been closed" in message
            ):
                raise

    def _handle_web_socket(self, route: Any) -> None:
        request_url = _playwright_request_url(route)
        try:
            result = self._check_network(request_url, allow_browser_local=False)
        except Exception:
            self._record_request_block(
                "url_policy_check_failed",
                fatal=False,
                request_url=request_url,
            )
            with suppress(Exception):
                route.close(code=1008, reason="Blocked by JobCockpit URL policy")
            return
        if not result.allowed:
            self._record_request_block(
                result.reason,
                fatal=False,
                request_url=request_url,
            )
            with suppress(Exception):
                route.close(code=1008, reason="Blocked by JobCockpit URL policy")
            return
        route.connect_to_server()

    def _check_network(
        self, url: str, *, allow_browser_local: bool,
    ) -> UrlPolicyResult:
        parsed = urlparse(url)
        scheme = parsed.scheme.casefold()
        if allow_browser_local and scheme in _SAFE_BROWSER_LOCAL_SCHEMES:
            return UrlPolicyResult(True, "browser_local_resource")
        if scheme in {"ws", "wss"}:
            url = parsed._replace(scheme="https" if scheme == "wss" else "http").geturl()
        return self.url_policy.check(url)

    def _record_request_block(
        self,
        reason: str,
        *,
        fatal: bool,
        request_url: str = "",
    ) -> None:
        if fatal:
            self._record_block(reason)
            return
        clean_reason = reason or "unknown"
        hostname = (urlparse(request_url).hostname or "").rstrip(".").casefold()
        self.blocked_subresource_reasons.add(clean_reason)
        self.blocked_subresource_requests.add((clean_reason, hostname))

    def _record_block(self, reason: str) -> None:
        if not self.blocked_reason:
            self.blocked_reason = reason or "unknown"


def _is_top_level_navigation_request(request: Any) -> bool:
    is_navigation_request = getattr(request, "is_navigation_request", None)
    try:
        # These classification headers are present in the request event. Calling
        # all_headers() adds a browser round trip that can be cancelled during
        # teardown, leaking CancelledError from the synchronous event listener.
        headers = getattr(request, "headers", {})
        destination = headers.get("sec-fetch-dest", "").casefold()
        resource_type = str(getattr(request, "resource_type", "")).casefold()
        is_navigation = destination == "document" or resource_type == "document" or (
            bool(is_navigation_request())
            if callable(is_navigation_request)
            else False
        )
        if not destination and resource_type == "other" and "text/html" in headers.get("accept", ""):
            # Classify ambiguous Firefox document failures for diagnostics only;
            # all requests use the same network policy.
            is_navigation = True
    except Exception:
        return True
    if destination == "document":
        return True
    if not is_navigation:
        return False
    try:
        frame = getattr(request, "frame", None)
        if frame is None:
            return True
        parent_frame = getattr(frame, "parent_frame", None)
        if callable(parent_frame):
            parent_frame = parent_frame()
        return parent_frame is None
    except Exception:
        return True


def _rendered_origin_cache_key(
    parsed: ParseResult,
) -> tuple[str, str, int] | None:
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


def _playwright_request_url(value: Any) -> str:
    url = getattr(value, "url", "")
    if callable(url):
        with suppress(Exception):
            url = url()
    return str(url or "")


def _safe_rendered_page_text(page: Any, hint: ExternalPostingHint) -> str:
    with suppress(Exception):
        return _playwright_page_snapshot(page, hint).rendered_text
    return ""


def _is_auth_wall_page(url: str, text: str) -> bool:
    normalized_url = url.casefold()
    normalized_text = text.casefold()
    parsed = urlparse(url)
    if (parsed.hostname or "").rstrip(".").casefold() in {
        "eluta.ca",
        "www.eluta.ca",
    } and parsed.path.rstrip("/").casefold() == "/sandbox":
        return True
    if any(
        token in normalized_url for token in ("/authwall", "/login", "/uas/login", "/checkpoint/")
    ):
        return True
    first_line = next((line.strip() for line in normalized_text.splitlines() if line.strip()), "")
    if _auth_wall_heading_matches(url, first_line, normalized_text):
        return True
    # A credential label is a wall signal; interview-process prose such as
    # "communicate outcomes ... by email or phone call" is ordinary job text.
    if re.search(r"(?m)^\s*email or phone\s*$|\bemail or phone\s+password\b", normalized_text):
        return True
    return any(
        marker in normalized_text
        for marker in (
            "sign up | linkedin",
            "sign in | linkedin",
            "authwall",
            "sign in to continue",
            "join linkedin",
            "linkedin app challenge",
            "check your linkedin app",
            "verify using authenticator app",
            "request blocked",
            "you have been blocked",
            "blocked - indeed",
        )
    )


def _auth_wall_heading_matches(url: str, first_line: str, page_text: str) -> bool:
    headings = (
        "just a moment",
        "checking your browser",
        "security verification",
        "are you human",
        "are you a human",
        "verify you are human",
        "verify that you are a human",
        "verifying you are human",
        "confirm you are human",
        "human verification",
    )
    normalized_heading = first_line.casefold().strip().rstrip(" .!?:…")
    if normalized_heading in headings:
        return True

    hostname = (urlparse(url).hostname or "").casefold()
    hostname_words = {
        word
        for word in re.findall(r"[a-z0-9]+", hostname)
        if len(word) >= 3 and word not in {"www", "com", "net", "org"}
    }
    if not hostname_words:
        return False
    challenge_text_present = any(
        marker in page_text
        for marker in (
            "complete the challenge",
            "please verify",
            "verify that you",
            "confirm you are",
            "checking your browser",
            "to continue",
        )
    )
    for heading in headings:
        if not normalized_heading.startswith(heading):
            continue
        suffix = normalized_heading[len(heading) :].lstrip(" .!?:…")
        if not suffix or suffix[0] not in "|-–—·•":
            continue
        brand_words = set(re.findall(r"[a-z0-9]+", suffix[1:]))
        if challenge_text_present and hostname_words & brand_words:
            return True
    return False


class PostingDiscoveryResolver:
    def __init__(
        self,
        *,
        search_client: SearchClient | None = None,
        official_career_site_search_client: OfficialCareerSiteSearchClient | None = None,
        apply_resolver: AtsApplyPageResolver | None = None,
        aggregator_fallback_resolver: AtsApplyPageResolver | None = None,
        rendered_fallback_resolver: RenderedPostingResolver | None = None,
        max_results: int = 5,
        allow_aggregator_fallback: bool = True,
        allow_linkedin_fallback: bool = False,
    ) -> None:
        self.search_client = search_client
        self.official_career_site_search_client = official_career_site_search_client
        self.apply_resolver = apply_resolver
        self.aggregator_fallback_resolver = aggregator_fallback_resolver
        self.rendered_fallback_resolver = rendered_fallback_resolver
        self.max_results = max_results
        self.allow_aggregator_fallback = allow_aggregator_fallback
        self.allow_linkedin_fallback = allow_linkedin_fallback

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        rendered_resolution: PostingResolution | None = None
        allow_source_fallback = self._can_try_aggregator_fallback(hint)
        if self._should_render_aggregator_before_search(hint):
            rendered_resolution = self._resolve_rendered_aggregator_fallback(hint)
            if rendered_resolution.status == "resolved" and rendered_resolution.posting is not None:
                if (
                    resolved_external_posting_terminal_status(rendered_resolution.posting)
                    is not None
                ):
                    if allow_source_fallback:
                        return rendered_resolution
                else:
                    hint = _hint_with_rendered_fallback(hint, rendered_resolution.posting)
                    follow_apply = getattr(self.rendered_fallback_resolver, "resolve_company_apply", None)
                    if callable(follow_apply):
                        direct = follow_apply(hint, rendered_resolution.posting)
                        if direct.status == "resolved":
                            return _append_resolution_history(direct, rendered_resolution)
                        rendered_resolution = _append_resolution_history(rendered_resolution, direct)

        if not hint.title or not hint.company:
            primary_resolution = PostingResolution(
                status="needs_review", error="missing_company_or_title"
            )
            primary_resolution = _with_official_search_attempt(
                primary_resolution,
                rendered_resolution,
                provider=_search_provider_code(self.search_client),
            )
            if (allow_source_fallback and rendered_resolution is not None
                    and rendered_resolution.status == "resolved"):
                return _aggregator_fallback_resolution(rendered_resolution, primary_resolution)
            fallback_resolution = self._resolve_aggregator_fallback(
                hint,
                primary_resolution,
                rendered_resolution=rendered_resolution,
            )
            if fallback_resolution.status != "not_found":
                return fallback_resolution
            return primary_resolution
        if not self.search_client and not self._saved_candidate_urls(hint):
            primary_resolution = PostingResolution(
                status="needs_review", error="posting_search_not_configured"
            )
            primary_resolution = _with_official_search_attempt(
                primary_resolution,
                rendered_resolution,
                provider="search",
            )
            if (allow_source_fallback and rendered_resolution is not None
                    and rendered_resolution.status == "resolved"):
                return _aggregator_fallback_resolution(rendered_resolution, primary_resolution)
            return primary_resolution

        fallback_hint = hint
        candidates: list[ResolvedExternalPosting] = []
        search_error = ""
        search_outcome = official_discovery_outcome("official_posting_not_found")
        candidate_attempts: list[PostingResolutionAttempt] = []
        candidate_failure_reasons: set[str] = set()
        candidate_validation_failed = False
        seen_urls: set[str] = set()
        candidate_budget = min(self.max_results * 3, 20)
        render_budget = 2
        apply_resolver = self.apply_resolver or AtsApplyPageResolver(
            url_policy=ExternalPostingUrlPolicy()
        )
        owns_apply_resolver = self.apply_resolver is None
        try:
            for search_provider, search_results, batch_error in self._search_batches(hint):
                if batch_error:
                    search_error = search_error or batch_error
                fallback_hint = _preferred_aggregator_fallback_hint(
                    fallback_hint, search_results,
                )
                official_results = [
                    (result, result.url.strip()) for result in search_results
                    if result.url.strip() and (
                        not external_aggregator_kind(result.url.strip())
                        or (search_provider == "saved_link" and _is_eluta_detail_url(result.url.strip()))
                    )
                ][:self.max_results]
                search_attempt = _official_search_attempt(
                    provider=search_provider,
                    status="needs_review" if batch_error else ("resolved" if official_results else "not_found"),
                    reason=batch_error or ("official_candidates_found" if official_results else "official_posting_not_found"),
                    candidate_count=len(official_results),
                )
                candidate_attempts.append(search_attempt)
                if official_results or search_outcome.status != "candidates_found":
                    search_outcome = _official_search_outcome_from_attempt(search_attempt)
                for result, candidate_url in official_results:
                    if candidate_url in seen_urls or len(seen_urls) >= candidate_budget:
                        continue
                    seen_urls.add(candidate_url)
                    try:
                        job = apply_resolver.resolve(
                            candidate_url,
                            AtsPostingContext(
                                source_label=f"{hint.source_kind.title()} Hint",
                                source_url=hint.source_url,
                                apply_url=candidate_url,
                            ),
                        )
                    except AtsProviderError as error:
                        candidate_validation_failed = True
                        failure_reason = _official_candidate_failure_reason(error)
                        candidate_failure_reasons.add(failure_reason)
                        candidate_attempts.append(
                            _official_candidate_attempt(
                                candidate_url=(
                                    ""
                                    if failure_reason == "official_candidate_url_policy_blocked"
                                    else candidate_url
                                ),
                                status="needs_review",
                                reason=failure_reason,
                            )
                        )
                        if (render_budget <= 0 or error.status_code in {404, 410}
                                or failure_reason == "official_candidate_url_policy_blocked"):
                            continue
                        render_budget -= 1
                        job = self._render_official_candidate(candidate_url, hint, candidate_attempts)
                        if job is None:
                            continue
                    else:
                        if job.observed_raw_text is not None and (
                            job.has_posting_body is False or not job.title
                            or not _posting_titles_match(hint.title, job.title)
                        ):
                            if render_budget > 0:
                                render_budget -= 1
                                rendered_job = self._render_official_candidate(candidate_url, hint, candidate_attempts)
                                if rendered_job is not None:
                                    job = rendered_job
                    if not posting_url_supports_open_evidence(job.source_url):
                        candidate_validation_failed = True
                        failure_reason = "official_candidate_not_job_detail"
                        candidate_failure_reasons.add(failure_reason)
                        candidate_attempts.append(
                            _official_candidate_attempt(
                                candidate_url=job.source_url,
                                status="needs_review",
                                reason=failure_reason,
                            )
                        )
                        continue
                    if not _official_candidate_url_matches_hint(job.source_url, hint):
                        candidate_validation_failed = True
                        failure_reason = "official_candidate_not_employer_authoritative"
                        candidate_failure_reasons.add(failure_reason)
                        candidate_attempts.append(
                            _official_candidate_attempt(
                                candidate_url=job.source_url,
                                status="needs_review",
                                reason=failure_reason,
                            )
                        )
                        continue
                    if job.title and not _posting_titles_match(hint.title, job.title):
                        candidate_validation_failed = True
                        failure_reason = "official_candidate_title_mismatch"
                        candidate_failure_reasons.add(failure_reason)
                        candidate_attempts.append(
                            _official_candidate_attempt(
                                candidate_url=job.source_url,
                                status="needs_review",
                                reason=failure_reason,
                            )
                        )
                        continue
                    observed_text = (
                        job.observed_raw_text if job.observed_raw_text is not None else job.raw_text
                    )
                    confidence = _official_posting_confidence(hint, observed_text)
                    if job.has_posting_body is False:
                        confidence = 0.0
                    resolved = ResolvedExternalPosting(
                        source_url=job.source_url,
                        raw_text=job.raw_text,
                        confidence=confidence,
                        evidence=result.snippet or result.title,
                        source_archive=job.source_archive,
                    )
                    candidate_attempts.append(
                        _official_candidate_attempt(
                            candidate_url=candidate_url,
                            status="resolved" if confidence >= 0.8 else "needs_review",
                            reason=(
                                "official_posting_resolved"
                                if confidence >= 0.8
                                else "official_candidates_low_confidence"
                            ),
                            confidence=confidence,
                        )
                    )
                    if confidence >= 0.8:
                        resolved_resolution = _with_official_search_outcome(
                            PostingResolution(status="resolved", posting=resolved),
                            search_outcome,
                        )
                        return _with_resolution_attempts(
                            resolved_resolution,
                            rendered_resolution,
                            extra=tuple(candidate_attempts),
                        )
                    candidates.append(resolved)
        finally:
            if owns_apply_resolver:
                apply_resolver.close()
        candidate_failure_only = candidate_validation_failed and not candidates
        governing_search_error = search_error
        if search_error:
            search_outcome = official_discovery_outcome(search_error)
        official_resolution = PostingResolution(
            status=(
                "needs_review"
                if governing_search_error or candidates or candidate_failure_only
                else "not_found"
            ),
            error=(
                next(iter(candidate_failure_reasons))
                if candidate_failure_only and len(candidate_failure_reasons) == 1
                else (
                    "official_candidate_render_needs_review"
                    if candidate_failure_only
                    else _official_discovery_fallback_reason(
                        search_error=governing_search_error,
                        candidates=tuple(candidates),
                    )
                )
            ),
        )
        official_resolution = _with_official_search_outcome(
            official_resolution,
            search_outcome,
        )
        official_resolution = _with_resolution_attempts(
            official_resolution,
            rendered_resolution,
            extra=tuple(candidate_attempts),
        )
        # LinkedIn is an explicit second source, not just a lucky generic result.
        if (self.search_client and not search_error and (self.allow_aggregator_fallback or self.allow_linkedin_fallback)
                and external_source_kind(hint.source_url) == "indeed"
                and external_source_kind(fallback_hint.source_url) != "linkedin"):
            company = reviewed_company_aliases(hint.company)[0]
            results = list(self.search_client.search(
                f'site:linkedin.com/jobs/view "{company}" "{hint.title}"',
                min(self.max_results * 3, 20),
            ))
            linkedin_error = str(getattr(self.search_client, "last_error", "") or "")
            fallback_hint = _preferred_aggregator_fallback_hint(hint, results)
            official_resolution = _with_resolution_attempts(official_resolution, extra=(
                PostingResolutionAttempt(
                    stage="alternative_search", provider=_search_provider_code(self.search_client),
                    status="needs_review" if linkedin_error else (
                        "resolved" if fallback_hint.source_url != hint.source_url else "not_found"),
                    reason_code=linkedin_error or "linkedin_search_completed",
                ),
            ))
        if fallback_hint.source_url != hint.source_url:
            alternative = self._resolve_aggregator_fallback(
                fallback_hint, official_resolution, permit_structured=False,
            )
            posting = alternative.posting
            if (alternative.status == "resolved" and posting is not None
                    and posting_identity(posting.source_url).is_detail
                    and external_source_kind(posting.source_url) == "linkedin"
                    and rendered_posting_identity_verdict(posting, fallback_hint) is RenderedIdentityVerdict.MATCH
                    and posting_has_description(posting, hint)
                    and _official_posting_confidence(hint, posting.raw_text) >= 0.8):
                return alternative
            official_resolution = _append_resolution_history(official_resolution, alternative)
        fallback_resolution = self._resolve_aggregator_fallback(
            hint,
            official_resolution,
            rendered_resolution=rendered_resolution,
        )
        if fallback_resolution.status == "resolved":
            return fallback_resolution
        if candidates:
            return replace(official_resolution, candidates=tuple(candidates))
        if fallback_resolution.status == "needs_review":
            return fallback_resolution
        if search_error:
            return official_resolution
        return official_resolution

    def _saved_candidate_urls(self, hint: ExternalPostingHint) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            url.strip() for url in (*hint.candidate_urls, hint.source_url)
            if url.strip() and (
                _is_eluta_detail_url(url.strip())
                or (url in hint.candidate_urls and not external_aggregator_kind(url.strip()))
            )
        ))[:3]

    def _search_batches(self, hint: ExternalPostingHint):
        """Reviewed ATS, exact/relaxed search, then at most two employer domains."""
        saved_urls = self._saved_candidate_urls(hint)
        if saved_urls:
            yield "saved_link", [SearchResult(title="", url=url) for url in saved_urls], ""
        if self.search_client is None:
            return
        if self.official_career_site_search_client is not None:
            client = self.official_career_site_search_client
            results = list(client.search(hint, self.max_results))
            error = _reviewed_official_search_error(client)
            if results or error:
                yield _search_provider_code(client), results, error
            if error.startswith("serper_"):
                return
        relaxed_company = reviewed_company_aliases(hint.company)[0]
        queries = (build_search_query(hint), f'"{relaxed_company}" "{hint.title}" careers job')
        seen_queries: set[str] = set()
        pool_size = min(max(self.max_results * 3, self.max_results), 20)
        domain_candidates: list[str] = []
        for query in queries:
            if query.casefold() in seen_queries:
                continue
            seen_queries.add(query.casefold())
            results = list(self.search_client.search(query, pool_size))
            domain_candidates.extend(_employer_search_domains(hint, results))
            error = str(getattr(self.search_client, "last_error", "") or "")
            yield _search_provider_code(self.search_client), results, error
            if error:
                return  # A provider outage is not helped by another immediate query.
        if not domain_candidates:
            results = list(self.search_client.search(f'"{relaxed_company}" careers', pool_size))
            error = str(getattr(self.search_client, "last_error", "") or "")
            domain_candidates.extend(_employer_search_domains(hint, results))
            yield _search_provider_code(self.search_client), results, error
            if error:
                return
        for domain in list(dict.fromkeys(domain_candidates))[:2]:
            # Unquoted title improves recall; fetched evidence still verifies exact identity.
            results = list(self.search_client.search(
                f'site:{domain} {hint.title}', pool_size,
            ))
            error = str(getattr(self.search_client, "last_error", "") or "")
            yield _search_provider_code(self.search_client), results, error
            if error:
                return

    def _render_official_candidate(self, url, hint, attempts):
        if self.rendered_fallback_resolver is None or not _official_candidate_url_matches_hint(url, hint):
            return None
        rendered_hint = replace(
            hint, source_url=url, url_identity="source_identity",
            identity_query_groups=rendered_query_identity_groups(url),
            evidence_text="", source_text="",
        )
        try:
            result = normalize_posting_resolution(self.rendered_fallback_resolver.resolve(rendered_hint))
        except Exception:
            attempts.append(_official_candidate_attempt(
                candidate_url=url, status="needs_review", reason="official_candidate_render_needs_review",
            ))
            return None
        attempts.extend(result.attempts)
        attempts.append(_official_candidate_attempt(
            candidate_url=url, status=result.status,
            reason=result.error or "official_candidate_render_" + result.status,
        ))
        posting = result.posting
        if result.status != "resolved" or posting is None or posting.content_truncated:
            return None
        # The existing renderer enforces URL policy. Independently enforce identity,
        # employer authority and page fields again before trusting its output.
        if (posting.source_url != url
                or posting.evidence_origin != "browser_dom"
                or not _official_candidate_url_matches_hint(posting.source_url, hint)):
            return None
        if posting.rendered_html:
            from jobcockpit.ingestion.ats_handlers.apply import _job_from_generic_apply_page
            job = _job_from_generic_apply_page(
                posting.source_url, posting.rendered_html, AtsPostingContext(), posting.source_archive,
            )
            observed = job.observed_raw_text or ""
            if not job.has_posting_body or _official_posting_confidence(hint, observed) < 0.8:
                return None
            return replace(job, raw_text=observed)
        if _official_posting_confidence(hint, posting.raw_text) < 0.8:
            return None
        title = _field_value(_candidate_lines(posting.raw_text), "title", "job title", "role")
        body_lines = [line for line in posting.raw_text.splitlines() if not _field_label(line)]
        if not title or len(" ".join(body_lines).split()) < 5:
            return None
        return AtsJob(
            source_url=posting.source_url, raw_text=posting.raw_text, title=title,
            source_archive=posting.source_archive, observed_raw_text=posting.raw_text,
            has_posting_body=True,
        )

    def _can_try_aggregator_fallback(self, hint: ExternalPostingHint) -> bool:
        return bool(external_aggregator_kind(hint.source_url)) and (
            self.allow_aggregator_fallback
            or (self.allow_linkedin_fallback and external_source_kind(hint.source_url) == "linkedin")
        )

    def _should_render_aggregator_before_search(self, hint: ExternalPostingHint) -> bool:
        if self.rendered_fallback_resolver is None:
            return False
        # Reading Indeed to discover an employer link is independent of whether
        # the caller permits an aggregator posting as the final result.
        if (external_aggregator_kind(hint.source_url) == "indeed"
                and callable(getattr(self.rendered_fallback_resolver, "resolve_company_apply", None))):
            return True
        return (
            self.aggregator_fallback_resolver is None
            and self._can_try_aggregator_fallback(hint)
            and (self.search_client is None or not hint.title or not hint.company)
        )

    def _resolve_aggregator_fallback(
        self,
        hint: ExternalPostingHint,
        primary_resolution: PostingResolution,
        *,
        rendered_resolution: PostingResolution | None = None,
        permit_structured: bool = True,
    ) -> PostingResolution:
        if not self._can_try_aggregator_fallback(hint):
            return PostingResolution(status="not_found")
        if not external_aggregator_kind(hint.source_url):
            return PostingResolution(status="not_found")

        fallback_resolver = self.aggregator_fallback_resolver
        owns_fallback_resolver = fallback_resolver is None
        if fallback_resolver is None:
            if rendered_resolution is None:
                rendered_resolution = self._resolve_rendered_aggregator_fallback(hint)
                primary_resolution = _append_resolution_history(
                    primary_resolution, rendered_resolution
                )
            if rendered_resolution.status == "resolved" and rendered_resolution.posting is not None:
                return _aggregator_fallback_resolution(
                    rendered_resolution,
                    primary_resolution,
                )
            structured_resolution = _structured_source_hint_resolution(
                hint,
                reason=primary_resolution.error or primary_resolution.status,
                official_search_outcome=_official_search_outcome_for_resolution(primary_resolution),
                attempts=primary_resolution.attempts,
                trace_truncated=primary_resolution.trace_truncated,
            )
            if permit_structured and structured_resolution is not None:
                return structured_resolution
            if rendered_resolution.status != "not_found":
                return _with_primary_resolution_state(rendered_resolution, primary_resolution)
            return _with_primary_resolution_state(
                PostingResolution(
                    status="needs_review", error="rendered_aggregator_fallback_not_configured"
                ),
                primary_resolution,
            )
        try:
            job = fallback_resolver.resolve(hint.source_url, _aggregator_fallback_context(hint))
        except AtsProviderError as error:
            if rendered_resolution is None:
                rendered_resolution = self._resolve_rendered_aggregator_fallback(hint)
                primary_resolution = _append_resolution_history(
                    primary_resolution, rendered_resolution
                )
            if rendered_resolution.status == "resolved" and rendered_resolution.posting is not None:
                return _aggregator_fallback_resolution(
                    rendered_resolution,
                    primary_resolution,
                )
            structured_resolution = _structured_source_hint_resolution(
                hint,
                reason=primary_resolution.error or primary_resolution.status,
                official_search_outcome=_official_search_outcome_for_resolution(primary_resolution),
                attempts=primary_resolution.attempts,
                trace_truncated=primary_resolution.trace_truncated,
            )
            if permit_structured and structured_resolution is not None:
                return structured_resolution
            if rendered_resolution.status != "not_found":
                return _with_primary_resolution_state(rendered_resolution, primary_resolution)
            return _with_primary_resolution_state(
                PostingResolution(
                    status="needs_review", error=f"aggregator_fallback_failed: {error.detail}"
                ),
                primary_resolution,
            )
        finally:
            if owns_fallback_resolver:
                fallback_resolver.close()

        resolved = ResolvedExternalPosting(
            source_url=job.source_url,
            raw_text=job.raw_text,
            confidence=_aggregator_fallback_confidence(
                hint, job.raw_text, SearchResult(title="", url=hint.source_url)
            ),
            evidence=(
                f"Imported from public {hint.source_kind.title()} posting after official posting search found no match"
            ),
            source_archive=job.source_archive,
        )
        if resolved.confidence < 0.8:
            if rendered_resolution is None:
                rendered_resolution = self._resolve_rendered_aggregator_fallback(hint)
                primary_resolution = _append_resolution_history(
                    primary_resolution, rendered_resolution
                )
            if rendered_resolution.status == "resolved" and rendered_resolution.posting is not None:
                return _aggregator_fallback_resolution(
                    rendered_resolution,
                    primary_resolution,
                )
            structured_resolution = _structured_source_hint_resolution(
                hint,
                reason=primary_resolution.error or primary_resolution.status,
                official_search_outcome=_official_search_outcome_for_resolution(primary_resolution),
                attempts=primary_resolution.attempts,
                trace_truncated=primary_resolution.trace_truncated,
            )
            if permit_structured and structured_resolution is not None:
                return structured_resolution
            if rendered_resolution.status != "not_found":
                return _with_primary_resolution_state(rendered_resolution, primary_resolution)
            return _with_primary_resolution_state(
                PostingResolution(
                    status="needs_review",
                    candidates=(resolved,),
                    error="aggregator_fallback_low_confidence",
                ),
                primary_resolution,
            )
        fallback = _with_aggregator_fallback_state(
            PostingResolution(status="resolved", posting=resolved),
            source_path="aggregator_direct_fallback",
            reason=primary_resolution.error or primary_resolution.status,
            official_search_outcome=_official_search_outcome_for_resolution(primary_resolution),
        )
        return _with_primary_resolution_state(fallback, primary_resolution)

    def _resolve_rendered_aggregator_fallback(self, hint: ExternalPostingHint) -> PostingResolution:
        if self.rendered_fallback_resolver is None:
            return PostingResolution(status="not_found")
        return self.rendered_fallback_resolver.resolve(hint)


def _official_search_attempt(
    *,
    provider: str,
    status: str,
    reason: str,
    candidate_count: int,
) -> PostingResolutionAttempt:
    return PostingResolutionAttempt(
        stage="official_search",
        provider=provider,
        status=status,
        reason_code=canonical_official_discovery_reason(reason),
        candidate_count=candidate_count,
    )


def _official_candidate_failure_reason(error: AtsProviderError) -> str:
    if error.status_code == 400 and "URL is blocked by policy:" in error.detail:
        return "official_candidate_url_policy_blocked"
    return "official_candidate_render_needs_review"


def _official_candidate_attempt(
    *,
    candidate_url: str,
    status: str,
    reason: str,
    confidence: float | None = None,
) -> PostingResolutionAttempt:
    return PostingResolutionAttempt(
        stage="official_candidate_render",
        provider="ats_apply",
        status=status,
        reason_code=canonical_official_discovery_reason(reason),
        candidate_url=candidate_url,
        confidence=confidence,
    )


def _search_provider_code(search_client: object) -> str:
    return str(getattr(search_client, "provider_code", "search"))


def _hint_with_rendered_fallback(
    hint: ExternalPostingHint,
    posting: ResolvedExternalPosting,
) -> ExternalPostingHint:
    rendered_company, rendered_title = _company_title_from_texts([posting.raw_text])
    rendered_location = _location_from_text(posting.raw_text)
    if external_aggregator_kind(posting.source_url) == "indeed":
        fields = extract_browser_capture_fields(BrowserCaptureContext(
            raw_text=posting.raw_text, lines=posting.raw_text.splitlines(),
            source_url=posting.source_url,
        ))
        rendered_company = fields.company or rendered_company
        rendered_title = fields.title or rendered_title
        rendered_location = fields.location_label or rendered_location
    rendered_term = posting_search_term_from_text(posting.raw_text)
    evidence_text = _clean_text(
        "\n".join(part for part in (hint.evidence_text, posting.raw_text) if part)
    )[:2000]
    return replace(
        hint,
        company=hint.company or rendered_company,
        title=hint.title or rendered_title,
        location=hint.location or rendered_location,
        term=hint.term or rendered_term,
        evidence_text=evidence_text or hint.evidence_text,
    )


def _official_discovery_fallback_reason(
    *,
    search_error: str,
    candidates: tuple[ResolvedExternalPosting, ...],
) -> str:
    if search_error:
        return search_error
    if candidates:
        return "official_candidates_low_confidence"
    return "official_posting_not_found"


def _aggregator_fallback_resolution(
    fallback_resolution: PostingResolution,
    primary_resolution: PostingResolution,
    *,
    source_path: str = "aggregator_rendered_fallback",
) -> PostingResolution:
    reason = primary_resolution.error or primary_resolution.status
    fallback = _with_aggregator_fallback_state(
        fallback_resolution,
        source_path=source_path,
        reason=reason,
        official_search_outcome=_official_search_outcome_for_resolution(primary_resolution),
    )
    return _with_primary_resolution_state(fallback, primary_resolution)


def _with_aggregator_fallback_state(
    resolution: PostingResolution,
    *,
    source_path: str,
    reason: str,
    official_search_outcome: OfficialDiscoveryOutcome,
) -> PostingResolution:
    fallback = replace(
        resolution,
        error=canonical_official_discovery_reason(reason),
        source_path=resolution.source_path or source_path,
    )
    return _with_official_search_outcome(fallback, official_search_outcome)


def _with_primary_resolution_state(
    resolution: PostingResolution,
    primary_resolution: PostingResolution,
) -> PostingResolution:
    """Compose fallback payload with the primary cumulative search state."""
    composed = replace(
        resolution,
        official_discovery_status="",
        reason_code="",
        official_search_outcome=None,
        attempts=(),
        trace_truncated=False,
    )
    composed = _with_official_search_outcome(
        composed,
        _official_search_outcome_for_resolution(primary_resolution),
    )
    return _with_resolution_attempts(composed, primary_resolution)


def external_source_kind(url: str) -> str | None:
    adapter = resolve_browser_capture_provider(url)
    if (
        adapter is None
        or adapter.provider not in {"linkedin", "indeed"}
        or not adapter.matches_source_route(urlparse(url.strip()))
    ):
        return None
    return adapter.provider


def assess_page_capture(
    source_url: str,
    page_text: str,
) -> PageCaptureAssessment:
    """Classify immutable capture evidence before any parser or job mutation."""
    source_identity = capture_source_identity(source_url)
    try:
        parsed = urlparse(source_url.strip())
    except ValueError:
        parsed = urlparse("")

    adapter = resolve_browser_capture_provider(source_url)
    is_specific_provider = bool(adapter and adapter.provider != "generic")
    is_provider_posting = bool(
        is_specific_provider and adapter and adapter.matches_posting_route(parsed)
    )
    page_kind = (
        CapturePageKind.POSTING
        if is_provider_posting
        else CapturePageKind.NON_JOB
        if is_specific_provider
        else CapturePageKind.UNKNOWN
    )
    normalized_text = " ".join(page_text.casefold().split())
    if not normalized_text:
        return PageCaptureAssessment(
            source_identity,
            CaptureAccessVerdict.INCOMPLETE,
            page_kind,
            "capture_page_incomplete",
        )
    challenge_markers = (
        "complete the challenge",
        "captcha",
        "checking your browser",
        "verify you are human",
        "verify that you are a human",
        "request blocked",
        "you have been blocked",
    )
    if _looks_like_blocked_page(page_text) or any(
        marker in normalized_text for marker in challenge_markers
    ):
        return PageCaptureAssessment(
            source_identity,
            CaptureAccessVerdict.CHALLENGE,
            page_kind,
            "capture_page_challenge",
        )
    if _is_auth_wall_page(source_url, page_text):
        return PageCaptureAssessment(
            source_identity,
            CaptureAccessVerdict.AUTHENTICATION_WALL,
            page_kind,
            "capture_authentication_wall",
        )
    if is_specific_provider and not is_provider_posting:
        return PageCaptureAssessment(
            source_identity,
            CaptureAccessVerdict.USABLE,
            CapturePageKind.NON_JOB,
            "capture_non_job_page",
        )

    terminal_status = (
        adapter.terminal_status(page_text)
        if adapter is not None and is_provider_posting and adapter.matches_terminal_route(parsed)
        else None
    )
    if terminal_status is not None:
        return PageCaptureAssessment(
            source_identity,
            CaptureAccessVerdict.USABLE,
            CapturePageKind.TERMINAL_POSTING,
            terminal_status.evidence_code,
            terminal_status,
        )
    return PageCaptureAssessment(
        source_identity,
        CaptureAccessVerdict.USABLE,
        page_kind,
        ("capture_provider_posting_page" if is_provider_posting else "capture_page_unclassified"),
    )


def capture_processing_action(
    assessment: PageCaptureAssessment,
) -> CaptureProcessingAction:
    if assessment.access_verdict is not CaptureAccessVerdict.USABLE:
        return CaptureProcessingAction.REJECT
    if assessment.page_kind is CapturePageKind.NON_JOB:
        return CaptureProcessingAction.REJECT
    if assessment.terminal_status is not None:
        return CaptureProcessingAction.APPLY_TERMINAL
    return CaptureProcessingAction.PARSE


def external_job_board_terminal_status(
    source_url: str,
    page_text: str,
) -> ExternalJobBoardTerminalStatus | None:
    return assess_page_capture(source_url, page_text).terminal_status


def resolved_external_posting_terminal_status(
    posting: ResolvedExternalPosting,
) -> ExternalJobBoardTerminalStatus | None:
    if posting.evidence_origin != "browser_dom":
        return None
    page_text = posting.complete_body_text or posting.rendered_text or posting.raw_text
    return external_job_board_terminal_status(posting.source_url, page_text)


def external_aggregator_kind(url: str) -> str | None:
    return job_portal_kind(url)


def build_external_posting_hint(
    *,
    source_url: str,
    link_text: str = "",
    surrounding_text: str = "",
    raw_text: str = "",
) -> ExternalPostingHint:
    source_kind = external_aggregator_kind(source_url) or "external_job_board"
    company, title = _company_title_from_texts([raw_text, surrounding_text, link_text])
    return ExternalPostingHint(
        source_url=source_url.strip(),
        source_kind=source_kind,
        company=company,
        title=title,
        location=_location_from_text(raw_text),
        term=posting_search_term_from_text(raw_text),
        link_text=_clean_text(link_text),
        evidence_text=_clean_text(
            "\n".join(part for part in [link_text, surrounding_text, raw_text] if part)
        )[:2000],
        source_text=raw_text.strip(),
    )


def normalize_posting_resolution(value: object) -> PostingResolution:
    if isinstance(value, PostingResolution):
        attempts, trace_truncated = _normalized_resolution_attempts(
            value.attempts,
            trace_truncated=value.trace_truncated,
        )
        normalized = _downgrade_truncated_resolution(
            replace(
                value,
                attempts=attempts,
                trace_truncated=trace_truncated,
            )
        )
        outcome = value.official_search_outcome
        if outcome is None and value.official_discovery_status and value.reason_code:
            outcome = official_discovery_outcome(
                value.reason_code,
                status=value.official_discovery_status,
            )
        return (
            _with_official_search_outcome(normalized, outcome)
            if outcome is not None
            else normalized
        )
    if not isinstance(value, dict):
        return PostingResolution(status="needs_review", error="invalid_posting_resolution")
    posting = value.get("posting")
    candidates = value.get("candidates") or ()
    source_resolution = (
        value.get("source_resolution") if isinstance(value.get("source_resolution"), dict) else {}
    )
    source_path = str(
        value.get("source_path")
        or source_resolution.get("path")
        or source_resolution.get("source_path")
        or ""
    )
    if not source_path and source_resolution.get("kind") == "aggregator_fallback":
        source_path = "aggregator_structured_fallback"
    parsed_attempts, trace_truncated = _normalized_resolution_attempts(
        value.get("attempts") or source_resolution.get("attempts") or (),
        trace_truncated=bool(
            value.get("trace_truncated") or source_resolution.get("trace_truncated")
        ),
    )
    official_status = str(
        value.get("official_discovery_status")
        or source_resolution.get("official_discovery_status")
        or ""
    )
    official_reason = str(
        value.get("reason_code")
        or source_resolution.get("reason_code")
        or source_resolution.get("official_discovery_error")
        or ""
    )
    normalized = _downgrade_truncated_resolution(
        PostingResolution(
            status=str(value.get("status") or "needs_review"),
            posting=_posting_from_value(posting),
            candidates=tuple(
                candidate
                for candidate in (_posting_from_value(item) for item in candidates)
                if candidate is not None
            ),
            error=str(value.get("error") or ""),
            source_path=source_path,
            official_discovery_status=official_status,
            reason_code=official_reason,
            attempts=parsed_attempts,
            trace_truncated=trace_truncated,
        )
    )
    if official_status and official_reason:
        return _with_official_search_outcome(
            normalized,
            official_discovery_outcome(official_reason, status=official_status),
        )
    return normalized


def _normalized_resolution_attempts(
    raw_attempts: object,
    *,
    trace_truncated: bool,
) -> tuple[tuple[PostingResolutionAttempt, ...], bool]:
    attempts_value = (
        raw_attempts[: MAX_POSTING_RESOLUTION_ATTEMPTS + 1]
        if isinstance(raw_attempts, Sequence)
        and not isinstance(raw_attempts, (str, bytes, bytearray))
        else ()
    )
    parsed_attempts = tuple(
        attempt
        for attempt in (_posting_resolution_attempt_from_value(item) for item in attempts_value)
        if attempt is not None
    )
    return (
        parsed_attempts[:MAX_POSTING_RESOLUTION_ATTEMPTS],
        trace_truncated or len(attempts_value) > MAX_POSTING_RESOLUTION_ATTEMPTS,
    )


def _posting_resolution_attempt_from_value(value: object) -> PostingResolutionAttempt | None:
    if isinstance(value, PostingResolutionAttempt):
        value = value.to_dict()
    if not isinstance(value, dict):
        return None
    stage = str(value.get("stage") or "").strip().casefold()
    status = str(value.get("status") or "").strip().casefold()
    provider = str(value.get("provider") or "").strip().casefold()
    if stage not in POSTING_RESOLUTION_ATTEMPT_STAGES:
        return None
    if status not in POSTING_RESOLUTION_ATTEMPT_STATUSES:
        return None
    if not re.fullmatch(r"[a-z0-9_]{1,64}", provider):
        return None
    reason_code = canonical_official_discovery_reason(str(value.get("reason_code") or status))[:160]
    candidate_url = _sanitized_attempt_candidate_url(str(value.get("candidate_url") or ""))
    confidence_value = value.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        and not isinstance(confidence_value, bool)
        and math.isfinite(float(confidence_value))
        and 0 <= float(confidence_value) <= 1
        else None
    )
    candidate_count_value = value.get("candidate_count")
    candidate_count = (
        candidate_count_value
        if isinstance(candidate_count_value, int)
        and not isinstance(candidate_count_value, bool)
        and 0 <= candidate_count_value <= 10_000
        else None
    )
    return PostingResolutionAttempt(
        stage=stage,
        provider=provider,
        status=status,
        reason_code=reason_code,
        candidate_url=candidate_url,
        confidence=confidence,
        candidate_count=candidate_count,
    )


def _downgrade_truncated_resolution(
    resolution: PostingResolution,
) -> PostingResolution:
    posting = resolution.posting
    if resolution.status != "resolved" or posting is None or not posting.content_truncated:
        return resolution
    candidates = (posting, *resolution.candidates)
    return replace(
        resolution,
        status="needs_review",
        posting=None,
        candidates=candidates,
        error=resolution.error or "rendered_content_truncated",
    )


_SEARCH_ACADEMIC_TERM_PATTERN = re.compile(
    r"\b(?:winter|spring|summer|fall|autumn)(?:\s*[/&-]\s*(?:winter|spring|summer|fall|autumn))?\s+20\d{2}\b",
    re.IGNORECASE,
)
_SEARCH_DURATION_TERM_PATTERN = re.compile(
    r"\b(?:4|8|12|16)[\s-]+months?\s+(?:co-op|internship|work\s+term|term|placement)\b"
    r"(?!\s+(?:work\s+)?experience\b)",
    re.IGNORECASE,
)
_SEARCH_LABELED_DURATION_TERM_PATTERN = re.compile(
    r"(?im)^\s*(?:employment\s+|work\s+)?(?:term(?:\s+length)?|duration)\s*:\s*"
    r"(?P<term>(?:4|8|12|16)[\s-]+months?)\b"
)
_SEARCH_ACADEMIC_EMPLOYMENT_CONTEXT_PATTERN = re.compile(
    r"\b(?:title|job\s+title|role|position|term|co-op|intern(?:ship)?|placement|"
    r"student|start(?:ing)?|commenc(?:e|ing)|contract)\b",
    re.IGNORECASE,
)
_SEARCH_ACADEMIC_NON_EMPLOYMENT_CONTEXT_PATTERN = re.compile(
    r"\b(?:graduat(?:e|es|ed|ing|ion)|degree|education|school|semester|coursework)\b",
    re.IGNORECASE,
)


def posting_search_term_from_text(text: str) -> str:
    labeled_duration = _SEARCH_LABELED_DURATION_TERM_PATTERN.search(text)
    if labeled_duration:
        return _clean_text(labeled_duration.group("term"))

    duration = _SEARCH_DURATION_TERM_PATTERN.search(text)
    if duration:
        return _clean_text(duration.group(0))

    for academic in _SEARCH_ACADEMIC_TERM_PATTERN.finditer(text):
        line = _line_containing_match(text, academic)
        if _SEARCH_ACADEMIC_NON_EMPLOYMENT_CONTEXT_PATTERN.search(line):
            continue
        if (
            _clean_text(line).casefold() == _clean_text(academic.group(0)).casefold()
            or _SEARCH_ACADEMIC_EMPLOYMENT_CONTEXT_PATTERN.search(line)
        ):
            return _clean_text(academic.group(0))
    return ""


def _line_containing_match(text: str, match: re.Match[str]) -> str:
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    return text[line_start : line_end if line_end >= 0 else len(text)]


def _employer_search_domains(hint: ExternalPostingHint, results: Sequence[SearchResult]) -> list[str]:
    """Search leads only; this does not authorize a destination or fetch a URL."""
    domains = []
    for result in results:
        try:
            parsed = urlparse(result.url)
            host = (parsed.hostname or "").casefold()
        except ValueError:
            continue
        if (parsed.scheme != "https" or not host or job_portal_kind(result.url)
                or not re.fullmatch(r"[a-z0-9.-]+", host)):
            continue
        if any(_company_matches_url_surface(alias, host) for alias in reviewed_company_aliases(hint.company)):
            domains.append(host)
    return domains


def posting_has_description(posting: ResolvedExternalPosting, hint: ExternalPostingHint) -> bool:
    """Identity/provenance alone is not a recovered description."""
    from jobcockpit.ingestion.posting_content import PostingContentState, posting_content_state
    return posting_content_state(posting.raw_text, hint) is PostingContentState.DESCRIPTION


def build_search_query(hint: ExternalPostingHint) -> str:
    parts = [f'"{hint.company}"', f'"{hint.title}"', "careers", "job"]
    if hint.term:
        parts.append(f'"{_search_term_query_value(hint.term)}"')
    if hint.location:
        parts.append(hint.location)
    return " ".join(part for part in parts if part)


def _preferred_aggregator_fallback_hint(
    hint: ExternalPostingHint,
    search_results: Sequence[SearchResult],
) -> ExternalPostingHint:
    candidates: list[tuple[int, int, str, str]] = []
    original_kind = external_aggregator_kind(hint.source_url)
    if original_kind:
        candidates.append((_aggregator_preference(original_kind), -1, hint.source_url, original_kind))

    for index, result in enumerate(search_results):
        candidate_url = result.url.strip()
        candidate_kind = external_aggregator_kind(candidate_url)
        if (candidate_kind != "linkedin" or not posting_identity(candidate_url).is_detail
                or external_source_kind(candidate_url) != "linkedin"):
            continue
        result_text = "\n".join(part for part in (result.title, result.snippet) if part)
        if _posting_confidence(hint, result_text, result) < 0.8:
            continue
        if not _search_result_disambiguators_match(hint, result_text):
            continue
        candidates.append(
            (_aggregator_preference(candidate_kind), index, candidate_url, candidate_kind)
        )

    if not candidates:
        return hint
    _, _, preferred_url, preferred_kind = min(candidates)
    if preferred_url == hint.source_url:
        return hint
    return replace(
        hint,
        source_url=preferred_url,
        source_kind=preferred_kind,
        url_identity="source_identity",
        identity_query_groups=(),
    )


def _search_result_disambiguators_match(hint: ExternalPostingHint, result_text: str) -> bool:
    if hint.term:
        result_term = posting_search_term_from_text(result_text)
        if not result_term or _canonical_search_term(result_term) != _canonical_search_term(
            hint.term
        ):
            return False
    if hint.location and not _search_result_location_matches(hint.location, result_text):
        return False
    return True


def _search_result_location_matches(location: str, result_text: str) -> bool:
    parsed = parse_location(location, location.casefold())
    required_location = parsed.city or parsed.province_state or parsed.country
    if not required_location:
        required_location = location.split(",", 1)[0].strip()
    normalized_required = _normalized_search_context(required_location or "")
    normalized_result = _normalized_search_context(result_text)
    return bool(normalized_required and normalized_required in normalized_result)


def _canonical_search_term(value: str) -> str:
    duration = re.search(r"\b(?P<months>4|8|12|16)[\s-]+months?\b", value, re.IGNORECASE)
    if duration:
        return f"duration:{duration.group('months')}"
    academic = _SEARCH_ACADEMIC_TERM_PATTERN.search(value)
    if academic:
        normalized = _normalized_search_context(academic.group(0)).replace("autumn", "fall")
        return f"academic:{normalized}"
    return _normalized_search_context(value)


def _search_term_query_value(value: str) -> str:
    canonical = _canonical_search_term(value)
    if canonical.startswith("duration:"):
        return f"{canonical.removeprefix('duration:')} month"
    return value


def _normalized_search_context(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _aggregator_preference(source_kind: str) -> int:
    return 0 if source_kind == "linkedin" else 1


def _aggregator_fallback_context(hint: ExternalPostingHint) -> AtsPostingContext:
    return AtsPostingContext(
        source_label=f"{hint.source_kind.title()} Fallback",
        source_url=hint.source_url,
        apply_url=hint.source_url,
    )


def _structured_source_hint_resolution(
    hint: ExternalPostingHint,
    *,
    reason: str,
    official_search_outcome: OfficialDiscoveryOutcome | None = None,
    attempts: Sequence[PostingResolutionAttempt] = (),
    trace_truncated: bool = False,
) -> PostingResolution | None:
    if not external_aggregator_kind(hint.source_url):
        return None
    if not hint.title.strip() or not hint.company.strip():
        return None

    raw_text = "\n".join(
        field
        for field in (
            f"Title: {hint.title.strip()}",
            f"Company: {hint.company.strip()}",
            f"Location: {hint.location.strip()}" if hint.location.strip() else "",
            f"Apply URL: {hint.source_url.strip()}",
            hint.source_text.strip(),
            f"Source Evidence: {hint.evidence_text.strip()[:1000]}"
            if hint.evidence_text.strip() and not hint.source_text.strip()
            else "",
        )
        if field
    )
    fallback_reason_code = canonical_official_discovery_reason(reason)
    search_outcome = official_search_outcome or official_discovery_outcome(reason)
    evidence = (
        f"Imported from {hint.source_kind.title()} source metadata because the public posting page "
        f"could not be resolved automatically ({fallback_reason_code})."
    )
    confidence = 0.86 if hint.location.strip() else 0.82
    resolution = PostingResolution(
        status="resolved",
        posting=ResolvedExternalPosting(
            source_url=hint.source_url,
            raw_text=raw_text,
            confidence=confidence,
            evidence=evidence,
        ),
        source_path="aggregator_structured_fallback",
        official_discovery_status=search_outcome.status,
        reason_code=search_outcome.reason_code,
        official_search_outcome=search_outcome,
        error=fallback_reason_code,
        attempts=tuple(attempts),
        trace_truncated=trace_truncated,
    )
    return _with_resolution_attempts(
        replace(resolution, attempts=(), trace_truncated=False),
        resolution,
        extra=(
            PostingResolutionAttempt(
                stage="structured_fallback",
                provider="structured_hint",
                status="resolved",
                reason_code="structured_metadata_imported",
                candidate_url=hint.source_url,
                confidence=confidence,
            ),
        ),
    )


def _short_resolution_reason(reason: str) -> str:
    normalized = " ".join(reason.split())
    return normalized[:240] if normalized else "external resolution unavailable"


def _posting_from_value(value: object) -> ResolvedExternalPosting | None:
    if isinstance(value, ResolvedExternalPosting):
        return value
    if not isinstance(value, dict):
        return None
    source_url = str(value.get("source_url") or "").strip()
    raw_text = str(value.get("raw_text") or "").strip()
    if not source_url or not raw_text:
        return None
    return ResolvedExternalPosting(
        source_url=source_url,
        raw_text=raw_text,
        confidence=_confidence_from_value(value.get("confidence")),
        evidence=str(value.get("evidence") or ""),
        evidence_origin=(
            str(value.get("evidence_origin"))
            if value.get("evidence_origin") in {"browser_dom", "model_output"}
            else "unknown"
        ),
        source_archive=_source_archive_from_value(value.get("source_archive")),
        content_truncated=value.get("content_truncated") is True,
        complete_body_text=str(value.get("complete_body_text") or ""),
    )


def _confidence_from_value(value: object) -> float:
    try:
        confidence = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return confidence if math.isfinite(confidence) else 0.0


def _source_archive_from_value(value: object) -> SourceArchive | None:
    if isinstance(value, SourceArchive):
        return value
    if not isinstance(value, dict):
        return None
    body = value.get("body")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        return None
    return SourceArchive(
        source_url=str(value.get("source_url") or ""),
        final_url=str(value.get("final_url") or value.get("source_url") or ""),
        status_code=int(value.get("status_code") or 0),
        content_type=str(value.get("content_type") or "application/octet-stream"),
        body=body,
        headers={str(key): str(item) for key, item in dict(value.get("headers") or {}).items()},
    )


def _company_title_from_texts(texts: Sequence[str]) -> tuple[str, str]:
    for text in texts:
        company, title = _company_title_from_fielded_text(text)
        if company and title:
            return company, title
        for line in _candidate_lines(text):
            company, title = _company_title_from_separated_row(line)
            if company and title:
                return company, title
            match = re.search(
                r"(?P<title>.+?)\s+(?:at|@)\s+(?P<company>.+)", line, flags=re.IGNORECASE
            )
            if not match:
                continue
            title = _clean_title(match.group("title"))
            company = _clean_company(match.group("company"))
            if title and company:
                return company, title
    return "", ""


def _company_title_from_separated_row(line: str) -> tuple[str, str]:
    parts = [_clean_text(part) for part in re.split(r"\s*(?:\u00b7|•|\ufffd)\s*", line)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return "", ""
    title = _clean_title(parts[0])
    company = _clean_company(parts[1])
    if title and company and not _field_label(company):
        return company, title
    return "", ""


def _company_title_from_fielded_text(text: str) -> tuple[str, str]:
    lines = _candidate_lines(text)
    company = _field_value(lines, "company")
    title = _field_value(lines, "title", "job title", "role", "position")
    if not title:
        for line in lines:
            if _field_label(line):
                continue
            title = line
            break
    title = _clean_title(title)
    company = _clean_company(company)
    if title and company:
        return company, title
    return "", ""


def _candidate_lines(text: str) -> list[str]:
    lines = [_clean_text(line) for line in text.splitlines()]
    return [line for line in lines if line][:8]


def _location_from_text(text: str) -> str:
    explicit_location = _field_value(_candidate_lines(text), "location")
    if explicit_location:
        return explicit_location
    for line in _candidate_lines(text)[1:6]:
        if _field_label(line):
            continue
        lowered = line.lower()
        if any(
            term in lowered for term in ("remote", "canada", "united states", "hybrid", "onsite")
        ):
            return line
        if "," in line and len(line) <= 120:
            return line
    return ""


def _clean_title(value: str) -> str:
    value = re.sub(r"^(job|role|position)\s*:\s*", "", value.strip(), flags=re.IGNORECASE)
    return _clean_text(value.strip(" -|"))


def _clean_company(value: str) -> str:
    value = re.split(
        r"\s+[|-]\s+|\s+on\s+(?:LinkedIn|Indeed)\b|\s+\|\s+(?:LinkedIn|Indeed)\b",
        value,
        maxsplit=1,
    )[0]
    return _clean_text(value.strip(" -|"))


def _company_matches_text(company: str, text: str) -> bool:
    normalized_text = _normalize_company_name(text)
    return any(
        normalized and f" {normalized} " in f" {normalized_text} "
        for alias in reviewed_company_aliases(company)
        if (normalized := _normalize_company_name(alias))
    )


def _company_names_match(left: str, right: str) -> bool:
    normalized_left = _normalize_company_name(left)
    normalized_right = _normalize_company_name(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    if {_normalize_company_name(alias) for alias in reviewed_company_aliases(left)} & {
        _normalize_company_name(alias) for alias in reviewed_company_aliases(right)
    }:
        return True
    if len(normalized_left) <= 4 or len(normalized_right) <= 4:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def _reviewed_official_search_error(search_client: object) -> str:
    error = str(getattr(search_client, "last_error", "") or "")
    return "" if error in {"", "missing_title", "unsupported_company"} else error


def _normalize_company_name(value: str) -> str:
    # Workday's observed hiringOrganization can contain HTML entities.
    normalized = re.sub(r"[^a-z0-9]+", " ", unescape(value).casefold())
    suffixes = {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "limited",
        "ltd",
        "llc",
        "technologies",
        "technology",
    }
    tokens = [token for token in normalized.split() if token not in suffixes]
    return " ".join(tokens)


def _posting_titles_match(expected: str, observed: str) -> bool:
    # Workday JSON-LD can retain entities that the visible board title has decoded.
    expected_title = " ".join(re.findall(r"[a-z0-9]+", unescape(expected).casefold()))
    observed_title = " ".join(re.findall(r"[a-z0-9]+", unescape(observed).casefold()))
    return bool(expected_title and expected_title == observed_title)


_GENERIC_ATS_TENANT_DOMAINS = frozenset(
    {
        "adp.com",
        "applytojobs.ca",
        "crelate.com",
        "easyapply.co",
    }
)
_GENERIC_NON_DETAIL_PATH_PARTS = frozenset(
    {
        "all-of-our-positions",
        "career-opportunities",
        "careers",
        "departments",
        "errorpage",
        "invalidjob",
        "job-listings",
        "jobs",
        "openings",
        "positions",
        "search-results",
        "studentportal",
        "view-all-jobs",
        "work-with-us",
    }
)


def _official_candidate_url_matches_hint(
    source_url: str,
    hint: ExternalPostingHint,
) -> bool:
    """Require employer/tenant ownership and a detail-shaped destination URL."""

    if job_portal_kind(source_url):
        return False
    reviewed = reviewed_career_domain_for_company(hint.company)
    if reviewed is not None:
        parsed = urlparse(source_url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        return bool(
            parsed.scheme in {"http", "https"}
            and hostname in reviewed.domains
            and re.search(reviewed.detail_path_pattern, parsed.path)
        )
    identity = posting_identity(source_url)
    if identity.source_kind == "eightfold":
        return identity.is_detail and _company_matches_url_surface(
            hint.company, urlparse(source_url).hostname or "",
        )
    if identity.is_trusted_provider:
        return identity.is_detail
    try:
        parsed = urlparse(source_url.strip())
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False

    is_tenant_provider = any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in _GENERIC_ATS_TENANT_DOMAINS
    )
    company_surface = f"{hostname} {parsed.path}" if is_tenant_provider else hostname
    if not _company_matches_url_surface(hint.company, company_surface):
        return False
    return _generic_candidate_path_is_detail(
        parsed.path,
        parsed.query,
        is_tenant_provider=is_tenant_provider,
    )


def _company_matches_url_surface(company: str, surface: str) -> bool:
    normalized_company = _normalize_company_name(company)
    normalized_surface = re.sub(r"[^a-z0-9]+", " ", surface.casefold()).strip()
    if not normalized_company or not normalized_surface:
        return False
    company_compact = normalized_company.replace(" ", "")
    surface_compact = normalized_surface.replace(" ", "")
    if len(company_compact) >= 3 and company_compact in surface_compact:
        return True
    surface_tokens = set(normalized_surface.split())
    meaningful_tokens = [
        token
        for token in normalized_company.split()
        if token
        not in {
            "agriculture",
            "america",
            "canada",
            "consumer",
            "goods",
            "group",
            "north",
            "solutions",
            "staffing",
            "systems",
        }
    ]
    return (
        len(meaningful_tokens) == 1
        and len(meaningful_tokens[0]) >= 3
        and meaningful_tokens[0] in surface_tokens
    )


def _generic_candidate_path_is_detail(
    path: str,
    query: str,
    *,
    is_tenant_provider: bool,
) -> bool:
    parts = [part.casefold() for part in path.split("/") if part]
    if not parts:
        return False
    if any(part in {"errorpage", "invalidjob", "search-results"} for part in parts):
        return False
    if len(parts) == 1 and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?(?:\.html)?", parts[0]):
        return False
    if parts[-1] in _GENERIC_NON_DETAIL_PATH_PARTS:
        return False
    if parts[0] in {"departments", "go"}:
        return False
    if any(re.fullmatch(r"\d{3,}", part) for part in parts):
        return True
    if any(re.fullmatch(r"[a-z0-9-]{8,}", part) and any(char.isdigit() for char in part) for part in parts):
        return True
    if any(part in {"job", "jobs", "position", "positions"} for part in parts[:-1]):
        return True
    params = parse_qs(query)
    if any(params.get(key) for key in ("jobId", "job_id", "reqId", "requisitionId")):
        return True
    return (
        not is_tenant_provider
        and len(parts) == 1
        and parts[0] not in _GENERIC_NON_DETAIL_PATH_PARTS
    )


def _field_value(lines: Sequence[str], *labels: str) -> str:
    wanted = {label.casefold() for label in labels}
    for line in lines:
        label = _field_label(line)
        if label and label in wanted:
            return _clean_text(line.split(":", 1)[1])
    return ""


def _field_label(line: str) -> str:
    if ":" not in line:
        return ""
    label = line.split(":", 1)[0].strip().casefold()
    return label if re.fullmatch(r"[a-z][a-z\s_-]{0,40}", label) else ""


def _official_posting_confidence(hint: ExternalPostingHint, raw_text: str) -> float:
    # Hints and search snippets locate candidates; only the fetched page verifies them.
    observed = "\n".join(
        _clean_text(line) for line in raw_text.splitlines()
        if not re.match(r"^\s*[^:]*\b(?:hint|source)\b[^:]*:", line, re.IGNORECASE)
    )
    # Browser captures retain the independently observed document title. LinkedIn
    # uses both of these title formats; neither requires identity from the hint.
    page_title = _field_value(_candidate_lines(observed), "page title")
    branded_title = re.fullmatch(r"(.+?)\s+\|\s+(.+?)\s+\|\s+LinkedIn", page_title)
    hiring_title = re.fullmatch(r"(.+?) hiring (.+) in (.+?)\s+\|\s+LinkedIn", page_title)
    if branded_title:
        title, company = branded_title.groups()
        observed = f"Title: {title}\nCompany: {company}\n{observed}"
    elif hiring_title:
        company, title, location = hiring_title.groups()
        observed = f"Title: {title}\nCompany: {company}\nLocation: {location}\n{observed}"
    lines = _candidate_lines(observed)
    title = _field_value(lines, "title", "job title", "role", "position") or (lines[0] if lines else "")
    if not title or not _posting_titles_match(hint.title, title):
        return 0.0
    if hint.location:
        location = _field_value(lines, "location", "locations", "primary location")
        expected = parse_location(hint.location, hint.location.casefold())
        actual = parse_location(location, location.casefold())
        city = expected.city or expected.province_state or expected.country or hint.location
        # A province-only source can match a city inside that province. A known
        # source city still has to appear in the independently observed location.
        if not expected.city and expected.province_state:
            location = actual.province_state or location
        surface = re.sub(r"[^a-z0-9]+", " ", (location or observed).casefold())
        normalized_city = re.sub(r"[^a-z0-9]+", " ", city.casefold()).strip()
        if normalized_city and f" {normalized_city} " not in f" {surface} ":
            return 0.0
    if hint.term:
        years = set(re.findall(r"\b20\d{2}\b", hint.term))
        if years and not years.issubset(set(re.findall(r"\b20\d{2}\b", observed))):
            return 0.0
        from jobcockpit.ingestion.employment_timing import _parse_employment_timing_resolution
        timing_text = hint.term if hint.term.startswith(("Start Date:", "Duration:")) else f"Work term: {hint.term}"
        expected_timing = _parse_employment_timing_resolution(timing_text)
        actual_timing = _parse_employment_timing_resolution(observed)
        if expected_timing.timing.start_date:
            if "start_date" in actual_timing.ambiguous_fields or not actual_timing.timing.start_date:
                return 0.0
            expected_start, actual_start = expected_timing.timing.start_date, actual_timing.timing.start_date
            if (expected_start[:4], (int(expected_start[5:7]) - 1) // 4) != (
                actual_start[:4], (int(actual_start[5:7]) - 1) // 4
            ):
                return 0.0
        if re.search(r"\b\d+[ -]+(?:months?|years?)\b", hint.term, re.IGNORECASE):
            expected_range = _verification_duration_range(expected_timing.timing.duration_label)
            if expected_range:
                if _verification_duration_range(actual_timing.timing.duration_label) != expected_range:
                    return 0.0
            elif ("duration_months" in actual_timing.ambiguous_fields
                  or not expected_timing.timing.duration_months
                  or actual_timing.timing.duration_months != expected_timing.timing.duration_months):
                return 0.0
    return _posting_confidence(replace(hint, title=title), observed, SearchResult(title="", url=""))


def _verification_duration_range(label: str | None) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)[ -]*months?", label or "", re.IGNORECASE)
    return (int(match[1]), int(match[2])) if match else None


def _is_eluta_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"eluta.ca", "www.eluta.ca"} and parsed.path.startswith("/spl/")


def _posting_confidence(
    hint: ExternalPostingHint,
    raw_text: str,
    result: SearchResult,
) -> float:
    if _looks_like_blocked_page(raw_text):
        return 0.0
    lines = _candidate_lines(raw_text)
    explicit_company = _field_value(lines, "company")
    explicit_title = _field_value(lines, "title", "job title", "role", "position")
    haystack = " ".join([raw_text, result.title, result.snippet]).casefold()
    score = 0.0
    company_conflicts = bool(
        explicit_company
        and hint.company
        and not _company_names_match(hint.company, explicit_company)
    )
    if hint.company and not company_conflicts and _company_matches_text(hint.company, haystack):
        score += 0.45
    title_haystack = (explicit_title or haystack).casefold()
    if hint.title and hint.title.casefold() in title_haystack:
        score += 0.45
    if hint.location and hint.location.casefold() in haystack:
        score += 0.1
    return min(score, 1.0)


def _aggregator_fallback_confidence(
    hint: ExternalPostingHint,
    raw_text: str,
    result: SearchResult,
) -> float:
    if _looks_like_blocked_page(raw_text):
        return 0.0
    score = _posting_confidence(hint, raw_text, result)
    if not external_aggregator_kind(hint.source_url):
        return score

    lines = _candidate_lines(raw_text)
    company = _field_value(lines, "company")
    title = _field_value(lines, "title", "job title", "role", "position")
    title_haystack = " ".join([title, raw_text, result.title, result.snippet]).casefold()
    title_matches = bool(hint.title and hint.title.casefold() in title_haystack)
    company_matches = bool(
        company and (not hint.company or _company_names_match(hint.company, company))
    )
    if title_matches and company_matches:
        score = max(score, 0.85)
    return min(score, 1.0)


def _looks_like_blocked_page(raw_text: str) -> bool:
    lower = raw_text.casefold()
    return any(
        marker in lower
        for marker in (
            "blocked - indeed.com",
            "request blocked",
            "you have been blocked",
            "troubleshooting cloudflare errors",
            "403 forbidden",
            "http error 403",
            "access to this page has been denied",
            "you don't have permission to access",
            "you do not have permission to access",
        )
    )


def _codex_rendered_page_prompt(hint: ExternalPostingHint) -> str:
    return (
        "Use the Chrome browser-control skill to open this public job posting URL in the user's existing "
        "Chrome session and reuse the user's already-running Chrome process and existing session state. "
        "Treat this as an explicit request for Chrome. Do not use standalone Playwright, the Playwright MCP "
        "server, Chrome CDP, Computer Use, or any other separate-browser control path. Do not launch any "
        "browser process. The Chrome skill's tab.playwright API is allowed after binding to the existing "
        "Chrome session. If existing Chrome control is unavailable, do not substitute another browser. "
        "Open a new tab for this validation rather than reusing a pre-existing tab. Before returning, close "
        "every tab created by this validation, including on an error or timeout when browser control remains "
        "available. Never close or navigate any pre-existing tab. "
        "Do not sign in, do not solve bot checks, do not click Apply, Submit, Continue, or e-signature controls. "
        "Do not perform general browsing or web searching. Only inspect the rendered page content. "
        "Extract the job title, company, location, description, "
        "and the current page URL. Return only one compact JSON object with string keys "
        '"source_url", "raw_text", and "evidence", plus numeric key "confidence". '
        "The raw_text must be plain text and include Title, Company, Location, and Apply URL lines when visible. "
        "Use confidence 0.0 to 1.0 based on whether the page matches the provided hint. "
        f"URL: {hint.source_url}\n"
        f"Expected company: {hint.company}\n"
        f"Expected title: {hint.title}\n"
        f"Expected location: {hint.location}\n"
        f"Source evidence: {hint.evidence_text[:1000]}"
    )


def _posting_from_codex_rendered_output(
    output: str, hint: ExternalPostingHint
) -> ResolvedExternalPosting | None:
    payload = _json_object_from_output(output)
    if not payload:
        return None
    source_url = str(payload.get("source_url") or hint.source_url).strip()
    raw_text = str(payload.get("raw_text") or "").strip()
    if not raw_text:
        raw_text = _raw_text_from_rendered_fields(payload, hint)
    raw_text = clean_aggregator_posting_text(raw_text, source_url=source_url)
    if not source_url or not raw_text:
        return None
    confidence_value = payload.get("confidence")
    model_confidence = float(confidence_value) if isinstance(confidence_value, int | float) else 1.0
    local_confidence = _aggregator_fallback_confidence(
        hint,
        raw_text,
        SearchResult(title="", url=source_url),
    )
    return ResolvedExternalPosting(
        source_url=source_url,
        raw_text=raw_text,
        confidence=max(0.0, min(model_confidence, local_confidence, 1.0)),
        evidence=str(
            payload.get("evidence") or "Extracted from the user's existing Chrome session"
        ),
        evidence_origin="model_output",
    )


def _raw_text_from_playwright_page(page: Any, hint: ExternalPostingHint) -> str:
    return _playwright_page_snapshot(page, hint).raw_text


_WORKDAY_RENDER_SETTLE_TIMEOUT_MS = 5_000
_WORKDAY_RENDER_POLL_INTERVAL_MS = 250
_WORKDAY_RENDER_TERMINAL_MARKERS = (
    "the page you are looking for does not exist",
    "the page you are looking for doesn't exist",
    "job is no longer available",
    "job posting is no longer available",
    "position is no longer available",
)


def _settled_workday_page_snapshot(
    page: Any,
    hint: ExternalPostingHint,
    timeout_ms: int,
) -> _PlaywrightPageSnapshot:
    snapshot = _playwright_page_snapshot(page, hint)
    if not _is_workday_render_url(str(page.url or hint.source_url)) or _workday_snapshot_ready(snapshot, hint):
        return snapshot

    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        return snapshot

    remaining_ms = min(max(timeout_ms, 0), _WORKDAY_RENDER_SETTLE_TIMEOUT_MS)
    while remaining_ms > 0:
        delay_ms = min(_WORKDAY_RENDER_POLL_INTERVAL_MS, remaining_ms)
        wait_for_timeout(delay_ms)
        remaining_ms -= delay_ms
        snapshot = _playwright_page_snapshot(page, hint)
        if _workday_snapshot_ready(snapshot, hint):
            break
    return snapshot


def _is_workday_render_url(source_url: str) -> bool:
    return _hostname_matches(
        urlparse(source_url).hostname,
        ("myworkdayjobs.com", "myworkdaysite.com"),
    )


def _workday_snapshot_ready(
    snapshot: _PlaywrightPageSnapshot,
    hint: ExternalPostingHint,
) -> bool:
    if not snapshot.body_text.strip():
        return False
    normalized_text = _normalized_search_context(snapshot.rendered_text)
    if any(
        _normalized_search_context(marker) in normalized_text
        for marker in _WORKDAY_RENDER_TERMINAL_MARKERS
    ):
        return True
    return _posting_confidence(
        hint,
        snapshot.rendered_text,
        SearchResult(title="", url=hint.source_url),
    ) >= 0.8


def _playwright_page_snapshot(
    page: Any,
    hint: ExternalPostingHint,
    *, source_url: str | None = None, in_frame: bool = False,
) -> _PlaywrightPageSnapshot:
    page_title, title_truncated = _bounded_playwright_title(page)
    stop_after_id = ""
    expected = capture_source_identity(hint.source_url)
    current = capture_source_identity(str(getattr(page, "url", "") or ""))
    if (expected and current and expected.provider == current.provider == "linkedin"
            and expected.strength == current.strength == "authoritative"
            and expected.identity_hash == current.identity_hash):
        job_id = expected.canonical_source_url.rstrip("/").rsplit("/", 1)[-1]
        if job_id.isdigit():
            stop_after_id = f"JobDetails_AboutTheJob_{job_id}"
    body_text, body_truncated = _bounded_playwright_body(
        page, in_frame=in_frame, stop_after_id=stop_after_id,
    )
    body_text = clean_aggregator_posting_text(
        body_text,
        source_url=str(getattr(page, "url", "") or hint.source_url),
    )

    source_url = str(source_url if source_url is not None else getattr(page, "url", "") or "")[:_RENDERED_SOURCE_URL_LIMIT]
    fields = [
        f"Page Title: {page_title}" if page_title else "",
        f"Source URL: {source_url}" if source_url else "",
        f"Source Evidence: {hint.evidence_text[:1000]}" if hint.evidence_text else "",
        body_text,
    ]
    raw_text = "\n".join(field for field in fields if field).strip()
    rendered_text = "\n".join(field for field in (page_title, body_text) if field).strip()
    return _PlaywrightPageSnapshot(
        raw_text=raw_text,
        rendered_text=rendered_text,
        body_text=body_text,
        content_truncated=title_truncated or body_truncated,
    )


def _bounded_playwright_title(page: Any) -> tuple[str, bool]:
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            value = evaluate(
                """
                limit => {
                  const text = String(document.title || "");
                  return {text: text.slice(0, limit), truncated: text.length > limit};
                }
                """,
                _RENDERED_TITLE_LIMIT,
            )
        except Exception as error:
            raise _PlaywrightRenderedError("rendered_title_snapshot_failed") from error
        result = _bounded_playwright_value(value, _RENDERED_TITLE_LIMIT)
        if result is None:
            raise _PlaywrightRenderedError("rendered_title_snapshot_invalid")
        return result
    with suppress(Exception):
        value = str(page.title() or "")
        return value[:_RENDERED_TITLE_LIMIT].strip(), len(value) > _RENDERED_TITLE_LIMIT
    return "", False


def _bounded_playwright_body(
    page: Any, *, in_frame: bool = False, stop_after_id: str = "",
) -> tuple[str, bool]:
    locator = page.locator("body")
    evaluate = getattr(locator, "evaluate", None)
    if in_frame:
        # Invisible Playwright can lose cross-origin element handles. Evaluate
        # in the frame realm directly, retaining the same bounded DOM walker.
        def evaluate(expression, limits):
            return page.evaluate("limits => (" + expression + ")(document.body, limits)", limits)
    if callable(evaluate):
        try:
            value = evaluate(
                """
                (element, limits) => {
                  const charLimit = limits.charLimit;
                  const nodeLimit = limits.nodeLimit;
                  // LinkedIn appends large recommendation lists after the
                  // requested job. Bound capture at that job's complete body.
                  const candidateEnd = limits.stopAfterId
                    ? document.getElementById(limits.stopAfterId) : null;
                  const endElement = candidateEnd && element.contains(candidateEnd) ? candidateEnd : null;
                  let enteredEnd = false;
                  let endStartLength = 0;
                  const blockedTags = new Set([
                    "SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "SVG",
                    "SELECT", "OPTION", "OPTGROUP", "DATALIST", "TEXTAREA"
                  ]);
                  const blockTags = new Set([
                    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DIV", "DL",
                    "DT", "DD", "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER",
                    "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER",
                    "HR", "LI", "MAIN", "NAV", "OL", "P", "PRE", "SECTION",
                    "TABLE", "TBODY", "TD", "TFOOT", "TH", "THEAD", "TR", "UL"
                  ]);
                  let committed = "";
                  let currentLine = "";
                  let truncated = false;
                  let visited = 0;
                  const commitLine = () => {
                    const line = currentLine.replace(/\\s+/g, " ").trim();
                    currentLine = "";
                    if (!line) return true;
                    const separator = committed ? "\\n" : "";
                    if (committed.length + separator.length + line.length > charLimit) {
                      truncated = true;
                      return false;
                    }
                    committed += separator + line;
                    return true;
                  };
                  const appendText = value => {
                    if (!value || truncated) return;
                    const remaining = charLimit + 1
                      - committed.length
                      - currentLine.length;
                    if (remaining <= 0) {
                      truncated = true;
                      currentLine = "";
                      return;
                    }
                    const raw = String(value);
                    const bounded = raw.slice(0, remaining);
                    currentLine += bounded.replace(/\\s+/g, " ");
                    if (
                      raw.length > remaining
                      || committed.length + currentLine.length > charLimit
                    ) {
                      truncated = true;
                      currentLine = "";
                    }
                  };
                  const visible = node => {
                    if (node === element) return true;
                    if (blockedTags.has(node.tagName) || node.hidden) return false;
                    if (node.tagName === "DIALOG" && !node.open) return false;
                    if (node.tagName === "DETAILS" && !node.open) return false;
                    if (node.hasAttribute("popover")) {
                      try {
                        if (!node.matches(":popover-open")) return false;
                      } catch {
                        return false;
                      }
                    }
                    if (node.getAttribute("aria-hidden") === "true") return false;
                    const style = window.getComputedStyle(node);
                    return style.display !== "none"
                      && style.visibility !== "hidden"
                      && style.opacity !== "0"
                      && style.contentVisibility !== "hidden";
                  };
                  const walker = document.createTreeWalker(
                    element,
                    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
                    {
                      acceptNode(node) {
                        if (node.nodeType === Node.ELEMENT_NODE && !visible(node)) {
                          return NodeFilter.FILTER_REJECT;
                        }
                        return NodeFilter.FILTER_ACCEPT;
                      }
                    }
                  );
                  let node;
                  while (!truncated && (node = walker.nextNode())) {
                    if (enteredEnd && !endElement.contains(node)) {
                      if (committed.length + currentLine.length - endStartLength > 200) break;
                      enteredEnd = false; // A loading shell is not a complete description.
                    }
                    if (node === endElement) {
                      enteredEnd = true;
                      endStartLength = committed.length + currentLine.length;
                    }
                    visited += 1;
                    if (visited > nodeLimit) {
                      truncated = true;
                      currentLine = "";
                      break;
                    }
                    if (node.nodeType === Node.TEXT_NODE) {
                      appendText(node.nodeValue || "");
                    } else if (node.tagName === "BR" || blockTags.has(node.tagName)) {
                      commitLine();
                    }
                  }
                  if (!truncated) commitLine();
                  return {
                    text: committed,
                    truncated
                  };
                }
                """,
                {
                    "charLimit": _RENDERED_BODY_LIMIT,
                    "nodeLimit": _RENDERED_NODE_LIMIT,
                    "stopAfterId": stop_after_id,
                },
            )
        except Exception as error:
            raise _PlaywrightRenderedError("rendered_body_snapshot_failed") from error
        result = _bounded_playwright_value(value, _RENDERED_BODY_LIMIT)
        if result is None:
            raise _PlaywrightRenderedError("rendered_body_snapshot_invalid")
        return result
    with suppress(Exception):
        value = str(locator.inner_text(timeout=5000) or "")
        return value[:_RENDERED_BODY_LIMIT].strip(), len(value) > _RENDERED_BODY_LIMIT
    with suppress(Exception):
        value = str(page.text_content("body", timeout=5000) or "")
        return value[:_RENDERED_BODY_LIMIT].strip(), len(value) > _RENDERED_BODY_LIMIT
    return "", False


def _bounded_playwright_value(
    value: object,
    limit: int,
) -> tuple[str, bool] | None:
    if not isinstance(value, dict):
        return None
    text = str(value.get("text") or "")
    return (
        text[:limit].strip(),
        value.get("truncated") is True or len(text) > limit,
    )


def _playwright_timeout_error() -> type[Exception]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return TimeoutError
    return PlaywrightTimeoutError


def observe_rendered_posting(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
    *,
    status_classifier: RenderedStatusClassifier | None = None,
) -> RenderedPageObservation:
    identity = rendered_posting_identity_verdict(posting, hint)
    access = rendered_posting_access_verdict(posting)
    status = RenderedStatusSignal.UNKNOWN
    if (
        identity is RenderedIdentityVerdict.MATCH
        and access is RenderedAccessVerdict.USABLE
        and status_classifier is not None
    ):
        status = status_classifier(
            posting.rendered_text or posting.complete_body_text or posting.raw_text
        )
    return RenderedPageObservation(
        posting=posting,
        identity=identity,
        access=access,
        status=status,
    )


def rendered_posting_access_verdict(
    posting: ResolvedExternalPosting,
) -> RenderedAccessVerdict:
    rendered_text = posting.rendered_text or posting.complete_body_text or posting.raw_text
    if _looks_like_blocked_page(rendered_text):
        return RenderedAccessVerdict.BLOCKED
    if _is_auth_wall_page(posting.source_url, rendered_text):
        return RenderedAccessVerdict.CHALLENGE
    return RenderedAccessVerdict.USABLE


def rendered_posting_identity_verdict(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
) -> RenderedIdentityVerdict:
    try:
        if not _rendered_page_url_matches_hint(posting.source_url, hint.source_url):
            return RenderedIdentityVerdict.MISMATCH
        if hint.url_identity == "origin":
            return RenderedIdentityVerdict.MATCH
        if hint.url_identity == "source_identity":
            expected_identity = capture_source_identity(hint.source_url)
            rendered_identity = capture_source_identity(posting.source_url)
            if (
                expected_identity is None
                or rendered_identity is None
                or expected_identity.strength != "authoritative"
                or rendered_identity.strength != "authoritative"
            ):
                return RenderedIdentityVerdict.AMBIGUOUS
            return (
                RenderedIdentityVerdict.MATCH
                if expected_identity.provider == rendered_identity.provider
                and expected_identity.identity_hash == rendered_identity.identity_hash
                else RenderedIdentityVerdict.MISMATCH
            )
        rendered_url = urlparse(posting.source_url)
        expected_url = urlparse(hint.source_url)
        rendered_path = rendered_url.path.rstrip("/") or "/"
        expected_path = expected_url.path.rstrip("/") or "/"
        if rendered_path != expected_path:
            return RenderedIdentityVerdict.MISMATCH
        if hint.url_identity == "path":
            return RenderedIdentityVerdict.MATCH
        expected_query = parse_qs(expected_url.query, keep_blank_values=True)
        rendered_query = parse_qs(rendered_url.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return RenderedIdentityVerdict.AMBIGUOUS

    identity_groups = hint.identity_query_groups or rendered_query_identity_groups(hint.source_url)
    verdict = RenderedIdentityVerdict.MATCH
    for group in identity_groups:
        group_verdict = _rendered_query_identity_group_verdict(
            group,
            expected_query=expected_query,
            rendered_query=rendered_query,
        )
        if group_verdict is RenderedIdentityVerdict.MISMATCH:
            return group_verdict
        if group_verdict is RenderedIdentityVerdict.AMBIGUOUS:
            verdict = group_verdict
    return verdict


def _rendered_posting_matches_hint(
    posting: ResolvedExternalPosting,
    hint: ExternalPostingHint,
) -> bool:
    return rendered_posting_identity_verdict(posting, hint) is RenderedIdentityVerdict.MATCH


def _rendered_query_identity_group_verdict(
    group: RenderedQueryIdentityGroup,
    *,
    expected_query: dict[str, list[str]],
    rendered_query: dict[str, list[str]],
) -> RenderedIdentityVerdict:
    expected_values = [expected_query[key] for key in group if key in expected_query]
    if not expected_values:
        return RenderedIdentityVerdict.AMBIGUOUS
    canonical_values = expected_values[0]
    if any(values != canonical_values for values in expected_values[1:]):
        return RenderedIdentityVerdict.AMBIGUOUS
    rendered_values = [rendered_query[key] for key in group if key in rendered_query]
    if not rendered_values:
        return RenderedIdentityVerdict.AMBIGUOUS
    if any(values != canonical_values for values in rendered_values):
        return RenderedIdentityVerdict.MISMATCH
    return RenderedIdentityVerdict.MATCH


def _rendered_page_url_matches_hint(rendered_url: str, hint_url: str) -> bool:
    rendered_url = rendered_url.strip()
    hint_url = hint_url.strip()
    if rendered_url == hint_url:
        return True
    hint_aggregator_kind = external_aggregator_kind(hint_url)
    if not hint_aggregator_kind:
        if _same_origin(rendered_url, hint_url):
            return True
        # Canonical www redirects must preserve the entire posting path/query,
        # protocol, and port. Network admission still belongs to the URL guard.
        left, right = urlparse(rendered_url), urlparse(hint_url)
        return (
            left.scheme == right.scheme == "https"
            and left.port == right.port
            and bool(left.hostname and right.hostname)
            and left.hostname.removeprefix("www.") == right.hostname.removeprefix("www.")
            and left.path == right.path and left.query == right.query
        )
    policy_result = ExternalAggregatorFallbackUrlPolicy(hint_url).check(rendered_url)
    return policy_result.allowed and external_aggregator_kind(rendered_url) == hint_aggregator_kind


def _same_origin(left_url: str, right_url: str) -> bool:
    try:
        left_origin = _rendered_origin_cache_key(urlparse(left_url))
        right_origin = _rendered_origin_cache_key(urlparse(right_url))
    except (TypeError, ValueError):
        return False
    return left_origin is not None and left_origin == right_origin


def _raw_text_from_rendered_fields(payload: dict[str, object], hint: ExternalPostingHint) -> str:
    title = str(payload.get("title") or hint.title).strip()
    company = str(payload.get("company") or hint.company).strip()
    location = str(payload.get("location") or hint.location).strip()
    apply_url = str(
        payload.get("apply_url") or payload.get("source_url") or hint.source_url
    ).strip()
    description = clean_aggregator_posting_text(
        str(payload.get("description") or "").strip(),
        source_url=apply_url,
    )
    fields = [
        title,
        f"Company: {company}" if company else "",
        f"Location: {location}" if location else "",
        f"Apply URL: {apply_url}" if apply_url else "",
        description,
    ]
    return "\n".join(field for field in fields if field).strip()


def _json_object_from_output(output: str) -> dict[str, object]:
    return codex_json_object_from_output(output) or {}


def _codex_cli_failure_message(completed: subprocess.CompletedProcess) -> str:
    return codex_cli_failure_message(completed)


def _codex_playwright_failure_message(completed: subprocess.CompletedProcess) -> str:
    return codex_cli_failure_message(completed, prefix="codex_playwright_failed")


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _hostname_matches(hostname: str | None, suffixes: Sequence[str]) -> bool:
    if not hostname:
        return False
    normalized = hostname.lower()
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in suffixes)


def _clean_text(text: str) -> str:
    return " ".join(str(text or "").split())
