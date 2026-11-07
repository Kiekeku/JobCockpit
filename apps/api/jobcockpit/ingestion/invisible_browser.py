"""Opt-in Firefox transport using the existing rendered-ingestion contract."""

from __future__ import annotations

from contextlib import ExitStack, suppress
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.concurrency import bounded_resource
from jobcockpit.ingestion.pinned_browser_proxy import PinnedBrowserProxy
from jobcockpit.ingestion.browser_cleanup import close_owned_browser_context
from jobcockpit.ingestion.invisible_session import (
    PreparationUrlPolicy,
    VerificationProgress,
    advance_verification,
    session_preparation_url,
    session_page_error,
    verification_site,
)
from jobcockpit.ingestion.posting_discovery import (
    ExternalPostingHint,
    PostingResolution,
    SearchResult,
    AtsPostingContext,
    capture_source_identity,
    external_aggregator_kind,
    _aggregator_fallback_confidence,
    _hint_with_rendered_fallback,
    _official_candidate_attempt,
    _official_candidate_url_matches_hint,
    _official_posting_confidence,
    _with_official_search_outcome,
    official_discovery_outcome,
    PlaywrightProfileRenderedPageResolver,
    _PlaywrightUrlPolicyGuard,
    _PlaywrightRenderedError,
    _enter_pinned_browser_proxy,
    _install_rendered_context_hardening,
    _is_auth_wall_page,
    _looks_like_blocked_page,
    _persistent_storage_state_lock,
    _playwright_page_snapshot,
    _raise_if_pinned_proxy_blocked,
    _safe_rendered_page_text,
    _verify_rendered_page_hardening,
)


class InvisiblePlaywrightRenderedPageResolver(PlaywrightProfileRenderedPageResolver):
    capture_html = True
    # The pinned engine lacks WebSocket interception. Native workers/sockets
    # use the validating proxy, including loopback, with direct failover off.
    # Removing these APIs breaks Indeed's verification even after preparation.
    route_websockets = False
    capture_evidence = (
        "Extracted from rendered page with invisible_playwright using a persistent Firefox profile"
    )

    def __init__(
        self, *, seed: int = 42, timezone: str = "America/Denver", locale: str = "en-US",
        invisible_factory: Callable[..., Any] | None = None,
        user_data_dir: str | Path = Path("var/invisible-playwright-profile"),
        auto_prepare: bool = True, prepare_timeout_seconds: int = 60,
        prepare_cooldown_seconds: int = 300,
        health_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(user_data_dir=user_data_dir, **kwargs)
        self.seed = seed
        self.timezone = timezone
        self.locale = locale
        self.invisible_factory = invisible_factory
        self.auto_prepare = auto_prepare
        self.prepare_timeout_seconds = prepare_timeout_seconds
        self.prepare_cooldown_seconds = prepare_cooldown_seconds
        self.health_root = health_root

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any):
        options = {
            "health_root": settings.artifact_root if settings.browser_health_enabled else None,
            "user_data_dir": settings.invisible_playwright_user_data_dir,
            "seed": settings.invisible_playwright_seed,
            "timezone": settings.invisible_playwright_timezone,
            "locale": settings.invisible_playwright_locale,
            "auto_prepare": settings.invisible_playwright_auto_prepare,
            "prepare_timeout_seconds": settings.invisible_playwright_prepare_timeout_seconds,
            "prepare_cooldown_seconds": settings.invisible_playwright_prepare_cooldown_seconds,
            "headless": settings.playwright_headless,
            "auth_wall_wait_seconds": settings.playwright_auth_wall_wait_seconds,
            "auth_wall_settle_seconds": settings.playwright_auth_wall_settle_seconds,
            "timeout_seconds": settings.posting_rendered_fallback_timeout_seconds,
            "retries": settings.posting_rendered_fallback_retries,
            "min_start_interval_seconds": settings.posting_rendered_fallback_min_interval_seconds,
        }
        options.update(overrides)
        return cls(**options)

    def load_page(self, hint: ExternalPostingHint, *, _require_resolved_posting: bool = False):
        from jobcockpit.ingestion.browser_health import observe

        budget = (self.retries + 1) * (
            2 * (self.timeout_seconds + self.auth_wall_wait_seconds + 40)
            + self.prepare_timeout_seconds + self.min_start_interval_seconds + 30
        )
        return observe(
            self.health_root, hint, "ingestion", budget,
            lambda: super(InvisiblePlaywrightRenderedPageResolver, self).load_page(
                hint, _require_resolved_posting=_require_resolved_posting,
            ),
        )

    def _profile_directory(self, hint: ExternalPostingHint) -> Path:
        site = verification_site(hint.source_url)
        root = self.user_data_dir.resolve()
        return root / site / f"seed-{self.seed}" if site else root

    def _wait_for_auth_wall_completion(self, page: Any, hint: ExternalPostingHint) -> bool:
        if self.auto_prepare and verification_site(hint.source_url):
            # Let _render close the guarded browser before opening preparation.
            return False
        return super()._wait_for_auth_wall_completion(page, hint)

    def _capture_rendered_page(self, page: Any, hint: ExternalPostingHint):
        from jobcockpit.ingestion.browser_health import inspect_embedded_job_content, record_page_failure

        title = page.title()
        if error := session_page_error(page.url, title):
            record_page_failure(page.url, title, error)
            raise _PlaywrightRenderedError(error)
        # Tower and other employer-hosted Greenhouse postings put the job in a
        # cross-origin frame. Only select the exact job requested by the caller.
        expected = parse_qs(urlsplit(hint.source_url).query).get("gh_jid", [])
        current = parse_qs(urlsplit(page.url).query).get("gh_jid", [])
        matches = []
        if len(expected) == 1 and expected[0].isdigit() and current == expected:
            for frame in getattr(page, "frames", ()):
                if frame is getattr(page, "main_frame", None):
                    continue
                parsed = urlsplit(frame.url)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"}
                    and parsed.path.rstrip("/") == "/embed/job_app"
                    and parse_qs(parsed.query).get("token") == expected
                ):
                    matches.append(frame)
        if len(matches) != 1:
            inspect_embedded_job_content(page)
            return super()._capture_rendered_page(page, hint)
        frame = matches[0]
        snapshot = _playwright_page_snapshot(frame, hint, source_url=page.url, in_frame=True)
        if not snapshot.body_text.strip():
            raise _PlaywrightRenderedError("empty_rendered_page")
        # The embed defaults to 150px if its CDN resize script fails. Make the
        # loaded description visible without touching application controls.
        with suppress(Exception):
            height = frame.evaluate("Math.min(20000, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
            # Invisible Playwright can omit parent_frame for an out-of-process
            # embed. The top document is the fallback for a direct child frame.
            (frame.parent_frame or page).evaluate(r"""({jobId, board, height}) => {
                for (const element of document.querySelectorAll('iframe')) {
                    const url = new URL(element.src || 'about:blank', document.baseURI);
                    if (url.protocol === 'https:'
                        && ['boards.greenhouse.io', 'job-boards.greenhouse.io'].includes(url.hostname)
                        && url.pathname.replace(/\/$/, '') === '/embed/job_app'
                        && url.searchParams.getAll('token').length === 1
                        && url.searchParams.get('token') === jobId
                        && url.searchParams.get('for') === board
                        && element.getBoundingClientRect().height < height)
                        element.style.height = height + 'px';
                }
            }""", {"jobId": expected[0], "board": parse_qs(urlsplit(frame.url).query).get("for", [None])[0], "height": height})
        return snapshot, frame.content() if self.capture_html else ""

    def _render(self, hint: ExternalPostingHint):
        started_at = time.time()
        try:
            return _with_observed_indeed_identity(hint, super()._render(hint))
        except _PlaywrightRenderedError as error:
            if not (self.auto_prepare and verification_site(hint.source_url) and str(error) in {
                "auth_wall_unresolved", "blocked_rendered_page",
            }):
                raise
        self._prepare_session(hint, started_at=started_at)
        try:
            posting = super()._render(hint)
        except Exception:
            profile = self._profile_directory(hint)
            with _persistent_storage_state_lock(profile / ".jobcockpit-profile", timeout_seconds=30):
                self._write_preparation_state(profile, "failed", "guarded_recheck_failed")
            raise
        return _with_observed_indeed_identity(hint, replace(
            posting, evidence=posting.evidence + "; automatic session preparation completed",
        ))

    @bounded_resource("browser")
    def resolve_company_apply(self, hint, posting):
        """Follow only the observed external-posting link; never operate a form."""
        from jobcockpit.ingestion.ats_handlers.apply import _job_from_generic_apply_page
        from jobcockpit.ingestion.browser_health import observe

        url = _indeed_company_apply_url(hint, posting)
        if not url:
            return PostingResolution(status="not_found")
        apply_hint = replace(hint, source_url=url, evidence_text="", source_text="")
        attempts = 1 + min(1, max(0, self.retries))

        def operation():
            if not self.url_policy.check(url).allowed:
                return PostingResolution(status="needs_review", error="url_policy_blocked")
            for attempt in range(attempts):
                try:
                    candidate = self._render(apply_hint)
                    break
                except _PlaywrightRenderedError as error:
                    # Observed Workday handoffs can briefly yield an empty
                    # document. Retry once when enabled, only after _render's
                    # owned-context cleanup. Never retry an unconfirmed cleanup.
                    if str(error) != "empty_rendered_page" or attempt == attempts - 1:
                        return PostingResolution(status="needs_review", error=str(error))
                except Exception:
                    return PostingResolution(status="needs_review", error="official_candidate_render_needs_review")
            if (candidate.content_truncated or candidate.evidence_origin != "browser_dom"
                    or external_aggregator_kind(candidate.source_url)
                    or not _official_candidate_url_matches_hint(candidate.source_url, hint)):
                return PostingResolution(status="needs_review", error="official_candidate_identity_mismatch")
            job = _job_from_generic_apply_page(
                candidate.source_url, candidate.rendered_html, AtsPostingContext(), candidate.source_archive,
            )
            observed = job.observed_raw_text or ""
            confidence = _official_posting_confidence(hint, observed)
            if not job.has_posting_body or confidence < .8:
                return PostingResolution(status="needs_review", error="official_candidates_low_confidence")
            return _with_official_search_outcome(PostingResolution(
                status="resolved", posting=replace(candidate, raw_text=observed, confidence=confidence),
            ), official_discovery_outcome("official_posting_resolved"))

        result = observe(self.health_root, apply_hint, "ingestion",
                         attempts * (2 * (self.timeout_seconds + self.auth_wall_wait_seconds + 40)
                                     + self.prepare_timeout_seconds), operation)
        return replace(result, attempts=result.attempts + (replace(_official_candidate_attempt(
            candidate_url=result.posting.source_url if result.posting else url,
            status=result.status, reason=result.error or "official_posting_resolved",
        ), provider="invisible_playwright", reason_code=result.error or "official_posting_resolved"),))

    def _write_preparation_state(self, profile: Path, status: str, reason: str = "") -> None:
        path = profile / ".jobcockpit-preparation.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "status": status, "attempted_at": time.time(), "reason": reason,
        }), encoding="utf-8")
        temporary.replace(path)

    def _prepare_session(self, hint: ExternalPostingHint, *, started_at: float) -> None:
        profile = self._profile_directory(hint)
        with _persistent_storage_state_lock(
            profile / ".jobcockpit-profile", timeout_seconds=self.prepare_timeout_seconds + 30,
        ):
            try:
                previous = json.loads((profile / ".jobcockpit-preparation.json").read_text(encoding="utf-8"))
                attempted_at = float(previous["attempted_at"])
                if previous["status"] == "ready" and attempted_at >= started_at:
                    return  # Another importer prepared this profile while we waited.
                if previous["status"] in {"failed", "preparing"} and (
                    time.time() - attempted_at < self.prepare_cooldown_seconds
                ):
                    raise _PlaywrightRenderedError("invisible_session_preparation_cooldown")
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._write_preparation_state(profile, "preparing")
            try:
                self._run_preparation(hint, profile)
            except Exception as error:
                self._write_preparation_state(profile, "failed", "verification_incomplete")
                if isinstance(error, _PlaywrightRenderedError):
                    raise
                raise _PlaywrightRenderedError("invisible_session_preparation_failed") from error
            self._write_preparation_state(profile, "ready")

    def _run_preparation(self, hint: ExternalPostingHint, profile: Path) -> None:
        from jobcockpit.ingestion.browser_health import record_page_failure

        preparation_url = session_preparation_url(hint.source_url)
        policy = PreparationUrlPolicy(hint.source_url, self.url_policy)
        with ExitStack() as stack:
            proxy = _enter_pinned_browser_proxy(stack, hint.source_url, policy)
            context = self._launch_context(
                stack, profile, proxy, min(self.timeout_seconds, self.prepare_timeout_seconds) * 1000,
                preparation=True,
            )
            guard = _PlaywrightUrlPolicyGuard(source_url=hint.source_url, url_policy=policy)
            try:
                _install_rendered_context_hardening(context)
                guard.install(context, route_websockets=False)
                page = context.new_page()
                _verify_rendered_page_hardening(page)
                deadline = self.clock() + self.prepare_timeout_seconds
                guard.ensure_final_url_allowed(preparation_url)
                page.goto(preparation_url, wait_until="domcontentloaded")
                progress = VerificationProgress()
                while self.clock() < deadline:
                    guard.ensure_final_url_allowed(page.url)
                    _raise_if_pinned_proxy_blocked(proxy)
                    text = _safe_rendered_page_text(page, hint)
                    title = page.title()
                    error = session_page_error(page.url, title)
                    if error in {"invisible_session_sign_in_required", "rendered_http_error"}:
                        record_page_failure(page.url, title, error)
                        raise _PlaywrightRenderedError(error)
                    if text.strip() and not error and not (
                        _is_auth_wall_page(page.url, text) or _looks_like_blocked_page(text)
                    ):
                        return  # The guarded retry checks posting identity and content.
                    remaining_ms = max(1, int((deadline - self.clock()) * 1000))
                    try:
                        advance_verification(page, preparation_url, progress, timeout_ms=min(3000, remaining_ms))
                    except Exception:
                        # A widget may reload between inspection and interaction.
                        # Progress marks clicks before dispatch, so none are repeated.
                        guard.raise_if_blocked()
                        _raise_if_pinned_proxy_blocked(proxy)
                    page.wait_for_timeout(min(500, max(1, int((deadline - self.clock()) * 1000))))
                raise _PlaywrightRenderedError("invisible_session_preparation_needs_review")
            finally:
                try:
                    if not close_owned_browser_context(context):
                        raise _PlaywrightRenderedError("owned_context_cleanup_failed")
                finally:
                    guard.close()

    def _launch_browser_context(
        self, render_stack: ExitStack, profile_dir: Path,
        pinned_proxy: PinnedBrowserProxy, timeout_ms: int,
    ) -> Any:
        return self._launch_context(render_stack, profile_dir, pinned_proxy, timeout_ms)

    def _launch_context(
        self, render_stack: ExitStack, profile_dir: Path,
        pinned_proxy: PinnedBrowserProxy, timeout_ms: int, *, preparation: bool = False,
    ) -> Any:
        factory = self.invisible_factory
        os.environ.setdefault(
            "INVISIBLE_PLAYWRIGHT_CACHE_DIR",
            str(Path("var/invisible-playwright-cache").resolve()),
        )
        if factory is None:
            try:
                from invisible_playwright import InvisiblePlaywright
                from jobcockpit.ingestion.invisible_compat import install_invisible_playwright_compatibility
                install_invisible_playwright_compatibility()
            except ImportError as error:
                raise _PlaywrightRenderedError(
                    "invisible_playwright is not installed; run `uv sync --extra invisible-browser` "
                    "in the selected runtime (see docs/runtime-operations.md) "
                    "and `pnpm --silent python -m invisible_playwright fetch`"
                ) from error
            factory = InvisiblePlaywright

        context = render_stack.enter_context(factory(
            profile_dir=str(profile_dir),
            seed=self.seed,
            headless=self.headless,
            proxy=pinned_proxy.playwright_proxy,
            # Explicit values let launch proceed if upstream's best-effort
            # egress lookup fails. No external proxy service is involved.
            timezone=self.timezone,
            locale=self.locale,
            extra_prefs={
                "media.peerconnection.enabled": False,
                # Firefox's interception controller shares this engine switch.
                # Disabling it also silently disables every Playwright route.
                "dom.serviceWorkers.enabled": True,
                "network.webtransport.enabled": False,
                # Firefox otherwise bypasses proxies for loopback, allowing a
                # redirect to connect before the wrapper reports the request.
                "network.proxy.allow_hijacking_localhost": True,
                "network.proxy.no_proxies_on": "",
                "network.proxy.failover_direct": False,
            },
        ))
        # Block page registration without disabling the interception controller.
        # Every document/frame gets this before any site script can retain the API.
        context.add_init_script(script="""
        (() => {
          Object.defineProperty(Navigator.prototype, "serviceWorker", {
            get: () => undefined, configurable: false
          });
        })();
        """)
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)
        return context


def _same_indeed_posting(hint, posting):
    expected = capture_source_identity(hint.source_url)
    actual = capture_source_identity(posting.source_url)
    return bool(expected and actual and expected.provider == "indeed"
                and expected.strength == actual.strength == "authoritative"
                and expected.identity_hash == actual.identity_hash)


def _with_observed_indeed_identity(hint, posting):
    if (hint.title and hint.company) or not _same_indeed_posting(hint, posting):
        return posting
    if posting.content_truncated or posting.evidence_origin != "browser_dom":
        return posting
    # Require the actual description, not just the search/navigation shell.
    body = posting.complete_body_text or posting.rendered_text
    _, marker, description = body.casefold().partition("full job description")
    if not marker or len(description.split()) < 50:
        return posting
    observed = replace(posting, raw_text="\n".join(
        line for line in posting.raw_text.splitlines()
        if not line.startswith("Source Evidence:")
    ))
    enriched = _hint_with_rendered_fallback(hint, observed)
    if not enriched.title or not enriched.company:
        return posting
    confidence = _aggregator_fallback_confidence(
        enriched, body, SearchResult(title="", url=posting.source_url),
    )
    return replace(posting, confidence=max(posting.confidence, confidence))


def _indeed_company_apply_url(hint, posting):
    if not _same_indeed_posting(hint, posting):
        return ""
    soup = BeautifulSoup(posting.rendered_html, "html.parser")
    links = {
        urljoin(posting.source_url, str(anchor.get("href", "")))
        for anchor in soup.select('a[data-testid="viewjob-apply"][href]')
        if anchor.get_text(" ", strip=True).casefold() == "apply on company site"
    }
    if len(links) != 1:
        return ""
    url = links.pop()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if external_aggregator_kind(url):
        expected = parse_qs(urlsplit(hint.source_url).query).get("jk")
        if (external_aggregator_kind(url) != "indeed" or parsed.path != "/applystart"
                or not expected or len(expected) != 1
                or parse_qs(parsed.query).get("jk") != expected):
            return ""
    return url
