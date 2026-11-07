from __future__ import annotations

import atexit
import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable, Coroutine, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
from jobcockpit.ingestion.chrome_cdp import validate_local_cdp_endpoint


EmergencyTargetCloser = Callable[[str], bool]
_CloseKind = Literal["context", "page"]

_DEFAULT_CLEANUP_TIMEOUT_SECONDS = 2.0
_TARGET_ID_LIMIT = 512
_TARGET_DISCOVERY_FRACTION = 0.25
_EMERGENCY_DELAY_FRACTION = 0.5


class CloseAttemptState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class BrowserCleanupState(str, Enum):
    PAGE_CLOSED = "page_closed"
    TARGET_CLOSED = "target_closed"
    CONTEXT_DISPOSED = "context_disposed"
    TABS_CLOSED_CONTEXT_UNCONFIRMED = "tabs_closed_context_unconfirmed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class BrowserCleanupResult:
    state: BrowserCleanupState

    @property
    def page_closed(self) -> bool:
        return self.state in {
            BrowserCleanupState.PAGE_CLOSED,
            BrowserCleanupState.TARGET_CLOSED,
        }

    @property
    def all_tabs_closed(self) -> bool:
        return self.page_closed or self.state in {
            BrowserCleanupState.CONTEXT_DISPOSED,
            BrowserCleanupState.TABS_CLOSED_CONTEXT_UNCONFIRMED,
        }

    @property
    def context_disposed(self) -> bool:
        return self.state is BrowserCleanupState.CONTEXT_DISPOSED


@dataclass(frozen=True)
class BrowserPageCleanupLease:
    """Owner-thread-captured token for thread-independent CDP tab cleanup."""

    target_id: str
    _closer: EmergencyTargetCloser
    endpoint_url: str | None = None

    def close(self, *, timeout_seconds: float = 0.5) -> bool:
        return _run_bounded_bool(
            lambda: self._closer(self.target_id),
            timeout_seconds=timeout_seconds,
        )


def capture_browser_page_cleanup_lease(
    browser_context: Any,
    page: Any,
    *,
    cdp_endpoint_url: str | None = None,
    discovery_timeout_seconds: float = 0.5,
    emergency_target_closer: EmergencyTargetCloser | None = None,
) -> BrowserPageCleanupLease | None:
    """Capture a target ID while on its owner thread for later emergency use."""

    closer = emergency_target_closer or _http_target_closer(cdp_endpoint_url)
    if closer is None or discovery_timeout_seconds <= 0:
        return None
    target_id = _target_id_for_page(
        browser_context,
        page,
        timeout_seconds=discovery_timeout_seconds,
    )
    if not target_id:
        return None
    return BrowserPageCleanupLease(target_id, closer, _local_cdp_origin(cdp_endpoint_url) or None)


def browser_page_target_ids(
    cdp_endpoint_url: str | None,
    *,
    timeout_seconds: float = 0.5,
) -> frozenset[str] | None:
    """Return a bounded snapshot of page targets, or None when CDP is unavailable."""

    targets = _browser_page_targets(
        cdp_endpoint_url,
        timeout_seconds=timeout_seconds,
    )
    return frozenset(targets) if targets is not None else None


def _browser_page_targets(
    cdp_endpoint_url: str | None,
    *,
    timeout_seconds: float,
) -> dict[str, str] | None:
    origin = _local_cdp_origin(cdp_endpoint_url)
    if not origin or timeout_seconds <= 0:
        return None
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(f"{origin}/json/list")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    targets: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict) or str(item.get("type") or "").casefold() != "page":
            continue
        target_id = str(item.get("id") or "").strip()
        if target_id and len(target_id) <= _TARGET_ID_LIMIT:
            targets[target_id] = str(item.get("url") or "").strip()
    return targets


def capture_new_browser_page_cleanup_lease(
    cdp_endpoint_url: str | None,
    known_target_ids: Iterable[str],
    *,
    expected_url: str,
    discovery_timeout_seconds: float = 0.5,
) -> BrowserPageCleanupLease | None:
    """Capture one new matching target without guessing among unrelated tabs."""

    closer = _http_target_closer(cdp_endpoint_url)
    normalized_expected_url = expected_url.strip()
    if closer is None or normalized_expected_url.casefold() in {"", "about:blank"}:
        return None
    current_targets = _browser_page_targets(
        cdp_endpoint_url,
        timeout_seconds=discovery_timeout_seconds,
    )
    if current_targets is None:
        return None
    known = {str(target_id).strip() for target_id in known_target_ids}
    candidates = {
        target_id
        for target_id, target_url in current_targets.items()
        if target_id not in known and target_url == normalized_expected_url
    }
    if len(candidates) != 1:
        return None
    target_id = next(iter(candidates))
    return BrowserPageCleanupLease(
        target_id,
        closer,
        _local_cdp_origin(cdp_endpoint_url) or None,
    )


@dataclass(frozen=True)
class BrowserCleanupDescriptor:
    endpoint_url: str
    target_id: str


class BrowserCleanupHandoffError(RuntimeError):
    pass


class BrowserCleanupJournal:
    """Multi-process-safe directory of independently recoverable CDP targets."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def persist(self, lease: BrowserPageCleanupLease) -> str:
        endpoint_url = _local_cdp_origin(getattr(lease, "endpoint_url", None))
        target_id = str(getattr(lease, "target_id", "") or "").strip()
        if not endpoint_url or not target_id or len(target_id) > _TARGET_ID_LIMIT:
            raise BrowserCleanupHandoffError("browser cleanup lease is not durably recoverable")
        key = hashlib.sha256(f"{endpoint_url}\0{target_id}".encode("utf-8")).hexdigest()
        payload = json.dumps(
            {"endpoint_url": endpoint_url, "target_id": target_id},
            separators=(",", ":"),
            sort_keys=True,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{key}.{uuid4().hex}.tmp"
        destination = self.root / f"{key}.json"
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(destination)
        finally:
            with suppress(OSError):
                temporary.unlink()
        return key

    def discard(self, key: str) -> None:
        with suppress(OSError):
            (self.root / f"{key}.json").unlink()

    def load_entries(self) -> tuple[tuple[str, BrowserCleanupDescriptor], ...]:
        if not self.root.is_dir():
            return ()
        entries: list[tuple[str, BrowserCleanupDescriptor]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                if path.stat().st_size > 4096:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                endpoint_url = _local_cdp_origin(payload.get("endpoint_url"))
                target_id = str(payload.get("target_id") or "").strip()
                if not endpoint_url or not target_id or len(target_id) > _TARGET_ID_LIMIT:
                    continue
                expected_key = hashlib.sha256(
                    f"{endpoint_url}\0{target_id}".encode("utf-8")
                ).hexdigest()
                if path.stem != expected_key:
                    continue
                entries.append(
                    (
                        expected_key,
                        BrowserCleanupDescriptor(endpoint_url, target_id),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(entries)


class BrowserPageCleanupSupervisor:
    """Own retryable CDP target leases, durably when a journal is configured."""

    def __init__(
        self,
        *,
        retry_interval_seconds: float = 0.25,
        journal: BrowserCleanupJournal | None = None,
        recovery_closer_factory: Callable[[str], EmergencyTargetCloser | None] | None = None,
    ) -> None:
        if retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds must be positive")
        self._retry_interval_seconds = retry_interval_seconds
        self._journal = journal
        self._recovery_closer_factory = recovery_closer_factory
        self._condition = threading.Condition()
        self._registered: dict[str, BrowserPageCleanupLease] = {}
        self._lease_keys: dict[int, str] = {}
        self._pending: dict[str, BrowserPageCleanupLease] = {}
        self._worker: threading.Thread | None = None

    def register(self, lease: BrowserPageCleanupLease) -> str:
        """Take durable ownership without requesting that the live tab close."""

        with self._condition:
            existing_key = self._lease_keys.get(id(lease))
            if existing_key is not None:
                return existing_key
        key = (
            f"memory:{id(lease)}"
            if self._journal is None
            else f"durable:{self._journal.persist(lease)}"
        )
        with self._condition:
            existing_key = self._lease_keys.get(id(lease))
            if existing_key is not None:
                return existing_key
            self._registered[key] = lease
            self._lease_keys[id(lease)] = key
            return key

    def request_close(self, leases: Iterable[BrowserPageCleanupLease]) -> int:
        requested_list: list[BrowserPageCleanupLease] = []
        seen_ids: set[int] = set()
        for lease in leases:
            if id(lease) in seen_ids:
                continue
            seen_ids.add(id(lease))
            requested_list.append(lease)
        requested = tuple(requested_list)
        if not requested:
            return 0
        prepared = [(self.register(lease), lease) for lease in requested]
        queued = 0
        with self._condition:
            for key, lease in prepared:
                if self._lease_keys.get(id(lease)) != key:
                    continue
                self._pending[key] = lease
                queued += 1
            if queued:
                self._start_worker_locked()
                self._condition.notify_all()
        return queued

    def request_all_close(self) -> int:
        with self._condition:
            leases = tuple(self._registered.values())
        return self.request_close(leases)

    def confirm_closed(self, lease: BrowserPageCleanupLease) -> bool:
        with self._condition:
            key = self._lease_keys.get(id(lease))
            if key is None:
                return False
            if self._journal is not None and key.startswith("durable:"):
                self._journal.discard(key.removeprefix("durable:"))
            self._lease_keys.pop(id(lease), None)
            self._registered.pop(key, None)
            self._pending.pop(key, None)
            self._condition.notify_all()
        return True

    def adopt(self, leases: Iterable[BrowserPageCleanupLease]) -> int:
        """Compatibility alias: take ownership and request closure."""

        return self.request_close(leases)

    def recover_pending(self) -> int:
        if self._journal is None:
            return 0
        closer_factory = self._recovery_closer_factory or _http_target_closer
        recovered: list[tuple[str, BrowserPageCleanupLease]] = []
        for journal_key, descriptor in self._journal.load_entries():
            closer = closer_factory(descriptor.endpoint_url)
            if closer is None:
                continue
            recovered.append(
                (
                    f"durable:{journal_key}",
                    BrowserPageCleanupLease(
                        descriptor.target_id,
                        closer,
                        descriptor.endpoint_url,
                    ),
                )
            )
        if not recovered:
            return 0
        with self._condition:
            for key, lease in recovered:
                self._registered[key] = lease
                self._lease_keys[id(lease)] = key
                self._pending[key] = lease
            self._start_worker_locked()
            self._condition.notify_all()
        return len(recovered)

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def wait_until_idle(self, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _start_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run,
                name="jobcockpit-browser-cleanup-supervisor",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                pending = tuple(self._pending.items())
            for _lease_key, lease in pending:
                if lease.close(timeout_seconds=0.5):
                    self.confirm_closed(lease)
            with self._condition:
                if self._pending:
                    self._condition.wait(self._retry_interval_seconds)


_BROWSER_CLEANUP_JOURNAL = BrowserCleanupJournal(Path("var/browser-cleanup-pending"))
_BROWSER_PAGE_CLEANUP_SUPERVISOR = BrowserPageCleanupSupervisor(journal=_BROWSER_CLEANUP_JOURNAL)


def register_browser_cleanup_lease(lease: BrowserPageCleanupLease) -> str:
    return _BROWSER_PAGE_CLEANUP_SUPERVISOR.register(lease)


def request_browser_cleanup(leases: Iterable[BrowserPageCleanupLease]) -> int:
    return _BROWSER_PAGE_CLEANUP_SUPERVISOR.request_close(leases)


def confirm_browser_cleanup(lease: BrowserPageCleanupLease) -> bool:
    return _BROWSER_PAGE_CLEANUP_SUPERVISOR.confirm_closed(lease)


def persist_browser_cleanup_lease(lease: BrowserPageCleanupLease) -> str:
    """Compatibility helper for callers that only need a durable record."""

    return _BROWSER_CLEANUP_JOURNAL.persist(lease)


def discard_browser_cleanup_lease(journal_key: str) -> None:
    _BROWSER_CLEANUP_JOURNAL.discard(journal_key)


def _drain_browser_cleanup_at_process_exit() -> None:
    # The scheduled runner reserves five seconds beyond the scan deadline.
    _BROWSER_PAGE_CLEANUP_SUPERVISOR.request_all_close()
    _BROWSER_PAGE_CLEANUP_SUPERVISOR.wait_until_idle(timeout_seconds=4.0)


atexit.register(_drain_browser_cleanup_at_process_exit)


def retain_pending_browser_cleanup(
    owner: object | None,
    *,
    supervisor: BrowserPageCleanupSupervisor | None = None,
) -> int:
    """Transfer unresolved independent leases before an owner is discarded."""

    pending = getattr(owner, "pending_cleanup_leases", None)
    release = getattr(owner, "release_pending_cleanup_leases", None)
    if not callable(pending) or not callable(release):
        return 0
    leases = tuple(pending())
    if not leases:
        return 0
    target_supervisor = supervisor or _BROWSER_PAGE_CLEANUP_SUPERVISOR
    adopted_count = target_supervisor.adopt(leases)
    released = tuple(release(leases))
    if len(released) != len(leases) or {id(lease) for lease in released} != {
        id(lease) for lease in leases
    }:
        raise BrowserCleanupHandoffError(
            "browser cleanup owner did not confirm the durable handoff"
        )
    return adopted_count


@dataclass(frozen=True)
class _CloseWatchdogResult:
    close_state: CloseAttemptState
    all_targets_closed: bool = False


def close_owned_browser_context(
    browser_context: Any,
    *,
    cdp_endpoint_url: str | None = None,
    cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    emergency_target_closer: EmergencyTargetCloser | None = None,
    skip_page_cleanup: bool = False,
) -> bool:
    """Compatibility wrapper reporting only confirmed context disposal."""

    return cleanup_owned_browser_context(
        browser_context,
        cdp_endpoint_url=cdp_endpoint_url,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
        emergency_target_closer=emergency_target_closer,
        skip_page_cleanup=skip_page_cleanup,
    ).context_disposed


def cleanup_owned_browser_context(
    browser_context: Any,
    *,
    cdp_endpoint_url: str | None = None,
    cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    emergency_target_closer: EmergencyTargetCloser | None = None,
    skip_page_cleanup: bool = False,
) -> BrowserCleanupResult:
    """Attempt bounded context cleanup without conflating tabs with disposal."""

    deadline = _cleanup_deadline(cleanup_timeout_seconds)
    closer = emergency_target_closer or _http_target_closer(cdp_endpoint_url)
    owned_pages = [] if skip_page_cleanup else list(getattr(browser_context, "pages", []) or [])
    target_ids: tuple[str, ...] = ()
    if closer is not None:
        target_ids = _target_ids_for_pages(
            browser_context,
            owned_pages,
            timeout_seconds=cleanup_timeout_seconds * _TARGET_DISCOVERY_FRACTION,
        )

    page_results: list[BrowserCleanupResult] = []
    for index, page in enumerate(owned_pages):
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            return BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED)
        pages_left = len(owned_pages) - index
        page_results.append(
            cleanup_owned_page(
                browser_context,
                page,
                cdp_endpoint_url=cdp_endpoint_url,
                cleanup_timeout_seconds=(remaining * _EMERGENCY_DELAY_FRACTION) / pages_left,
                emergency_target_closer=closer,
            )
        )

    tabs_confirmed_closed = bool(owned_pages) and all(result.page_closed for result in page_results)
    is_playwright_context = _is_sync_playwright_owner(browser_context)
    maximum_attempts = 1 if is_playwright_context else 3
    targets_confirmed_closed = False
    for attempts_left in range(maximum_attempts, 0, -1):
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            break
        outcome = _call_close_with_watchdog(
            browser_context.close,
            target_ids,
            closer,
            remaining / attempts_left,
            playwright_owner=browser_context,
            close_kind="context",
        )
        targets_confirmed_closed = targets_confirmed_closed or outcome.all_targets_closed
        if outcome.close_state is CloseAttemptState.SUCCEEDED:
            return BrowserCleanupResult(BrowserCleanupState.CONTEXT_DISPOSED)
        if outcome.close_state is CloseAttemptState.TIMED_OUT:
            break

    if tabs_confirmed_closed or targets_confirmed_closed:
        return BrowserCleanupResult(BrowserCleanupState.TABS_CLOSED_CONTEXT_UNCONFIRMED)
    return BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED)


def close_owned_page(
    browser_context: Any,
    page: Any,
    *,
    cdp_endpoint_url: str | None = None,
    cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    emergency_target_closer: EmergencyTargetCloser | None = None,
) -> bool:
    """Compatibility wrapper reporting confirmed page or target closure."""

    return cleanup_owned_page(
        browser_context,
        page,
        cdp_endpoint_url=cdp_endpoint_url,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
        emergency_target_closer=emergency_target_closer,
    ).page_closed


def cleanup_owned_page(
    browser_context: Any,
    page: Any,
    *,
    cdp_endpoint_url: str | None = None,
    cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    emergency_target_closer: EmergencyTargetCloser | None = None,
) -> BrowserCleanupResult:
    """Close one caller-created page and report what was actually confirmed."""

    deadline = _cleanup_deadline(cleanup_timeout_seconds)
    closer = emergency_target_closer or _http_target_closer(cdp_endpoint_url)
    target_id = ""
    if closer is not None or callable(getattr(browser_context, "new_cdp_session", None)):
        target_id = _target_id_for_page(
            browser_context,
            page,
            timeout_seconds=cleanup_timeout_seconds * _TARGET_DISCOVERY_FRACTION,
        )

    def close_page() -> None:
        try:
            page.close(run_before_unload=False)
        except TypeError:
            page.close()

    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        return BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED)
    close_state = _run_bounded_close(
        close_page,
        timeout_seconds=remaining * _EMERGENCY_DELAY_FRACTION,
        playwright_owner=page,
        close_kind="page",
    )
    if close_state is CloseAttemptState.SUCCEEDED:
        return BrowserCleanupResult(BrowserCleanupState.PAGE_CLOSED)

    # A timed-out Playwright operation may have poisoned its owner object. Do
    # not issue another command through that object; only the independent HTTP
    # target closer is safe after that transition.
    remaining = _remaining_seconds(deadline)
    if (
        close_state is CloseAttemptState.FAILED
        and target_id
        and remaining > 0
        and _close_target_via_context(
            browser_context,
            page,
            target_id,
            timeout_seconds=remaining * _EMERGENCY_DELAY_FRACTION,
        )
    ):
        return BrowserCleanupResult(BrowserCleanupState.TARGET_CLOSED)

    remaining = _remaining_seconds(deadline)
    if (
        target_id
        and closer is not None
        and remaining > 0
        and _run_bounded_bool(lambda: closer(target_id), timeout_seconds=remaining)
    ):
        return BrowserCleanupResult(BrowserCleanupState.TARGET_CLOSED)
    return BrowserCleanupResult(BrowserCleanupState.UNCONFIRMED)


def _call_close_with_watchdog(
    close: Callable[[], None],
    target_ids: Iterable[str],
    emergency_target_closer: EmergencyTargetCloser | None,
    cleanup_timeout_seconds: float,
    *,
    playwright_owner: Any | None = None,
    close_kind: _CloseKind = "context",
) -> _CloseWatchdogResult:
    """Run close with a hard bound and independently track target cleanup."""

    deadline = _cleanup_deadline(cleanup_timeout_seconds)
    unique_target_ids = tuple(dict.fromkeys(target_ids))
    completed = threading.Event()
    target_results: list[bool] = []
    watchdog: threading.Thread | None = None
    if emergency_target_closer is not None and unique_target_ids:

        def emergency_cleanup() -> None:
            delay = cleanup_timeout_seconds * _EMERGENCY_DELAY_FRACTION
            if completed.wait(delay):
                return
            for target_id in unique_target_ids:
                try:
                    target_results.append(bool(emergency_target_closer(target_id)))
                except Exception:
                    target_results.append(False)

        watchdog = threading.Thread(
            target=emergency_cleanup,
            name="jobcockpit-browser-cleanup-watchdog",
            daemon=True,
        )
        watchdog.start()

    close_state = _run_bounded_close(
        close,
        timeout_seconds=_remaining_seconds(deadline),
        playwright_owner=playwright_owner,
        close_kind=close_kind,
    )
    completed.set()
    if watchdog is not None:
        watchdog.join(timeout=_remaining_seconds(deadline))
    return _CloseWatchdogResult(
        close_state=close_state,
        all_targets_closed=bool(unique_target_ids)
        and len(target_results) == len(unique_target_ids)
        and all(target_results),
    )


def _run_bounded_close(
    close: Callable[[], None],
    *,
    timeout_seconds: float,
    playwright_owner: Any | None,
    close_kind: _CloseKind,
) -> CloseAttemptState:
    if timeout_seconds <= 0:
        return CloseAttemptState.TIMED_OUT
    if _is_sync_playwright_owner(playwright_owner):
        return _run_playwright_close_on_owner_loop(
            playwright_owner,
            close_kind=close_kind,
            timeout_seconds=timeout_seconds,
        )

    finished = threading.Event()
    succeeded = threading.Event()

    def invoke_close() -> None:
        try:
            close()
            succeeded.set()
        except Exception:
            pass
        finally:
            finished.set()

    worker = threading.Thread(
        target=invoke_close,
        name="jobcockpit-browser-close",
        daemon=True,
    )
    worker.start()
    if not finished.wait(timeout_seconds):
        return CloseAttemptState.TIMED_OUT
    if succeeded.is_set():
        return CloseAttemptState.SUCCEEDED
    return CloseAttemptState.FAILED


def _is_sync_playwright_owner(owner: Any | None) -> bool:
    return bool(
        owner is not None
        and callable(getattr(owner, "_sync", None))
        and getattr(owner, "_impl_obj", None) is not None
    )


def _run_playwright_close_on_owner_loop(
    owner: Any,
    *,
    close_kind: _CloseKind,
    timeout_seconds: float,
) -> CloseAttemptState:
    """Bound Playwright close without moving its greenlet across threads."""

    try:
        owner._sync(
            _bounded_playwright_close(
                owner._impl_obj,
                close_kind=close_kind,
                timeout_seconds=timeout_seconds,
            )
        )
        return CloseAttemptState.SUCCEEDED
    except TimeoutError:
        return CloseAttemptState.TIMED_OUT
    except Exception:
        return CloseAttemptState.FAILED


async def _await_with_hard_timeout(
    operation: Coroutine[Any, Any, Any],
    *,
    timeout_seconds: float,
) -> Any:
    """Cancel a Playwright operation at the deadline without awaiting teardown."""

    task = asyncio.ensure_future(operation)
    done, _pending = await asyncio.wait((task,), timeout=timeout_seconds)
    if not done:
        task.cancel()
        raise TimeoutError
    return task.result()


async def _bounded_playwright_close(
    impl: Any,
    *,
    close_kind: _CloseKind,
    timeout_seconds: float,
) -> None:
    close_coro: Coroutine[Any, Any, Any]
    if close_kind == "page":
        close_coro = impl.close(
            runBeforeUnload=False,
            reason="JobCockpit owned-page cleanup",
        )
    else:
        close_coro = impl.close(reason="JobCockpit owned-context cleanup")
    await _await_with_hard_timeout(close_coro, timeout_seconds=timeout_seconds)


def _target_ids_for_pages(
    browser_context: Any,
    pages: Iterable[Any],
    *,
    timeout_seconds: float,
) -> tuple[str, ...]:
    deadline = _cleanup_deadline(timeout_seconds)
    target_ids: list[str] = []
    for page in pages:
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            break
        target_id = _target_id_for_page(
            browser_context,
            page,
            timeout_seconds=remaining,
        )
        if target_id and target_id not in target_ids:
            target_ids.append(target_id)
    return tuple(target_ids)


def _target_id_for_page(
    browser_context: Any,
    page: Any,
    *,
    timeout_seconds: float,
) -> str:
    if timeout_seconds <= 0:
        return ""
    if _is_sync_playwright_owner(browser_context) and getattr(page, "_impl_obj", None) is not None:
        try:
            target_id = browser_context._sync(
                _await_with_hard_timeout(
                    _playwright_target_id(
                        browser_context._impl_obj,
                        page._impl_obj,
                    ),
                    timeout_seconds=timeout_seconds,
                )
            )
            return str(target_id or "").strip()[:_TARGET_ID_LIMIT]
        except Exception:
            return ""

    result: list[str] = []

    def discover_target() -> None:
        result.append(_target_id_for_page_sync(browser_context, page))

    finished = threading.Event()

    def invoke_discovery() -> None:
        try:
            discover_target()
        finally:
            finished.set()

    worker = threading.Thread(
        target=invoke_discovery,
        name="jobcockpit-browser-target-discovery",
        daemon=True,
    )
    worker.start()
    if not finished.wait(timeout_seconds):
        return ""
    return result[0] if result else ""


async def _playwright_target_id(context_impl: Any, page_impl: Any) -> str:
    session = await context_impl.new_cdp_session(page_impl)
    try:
        target_info = await session.send("Target.getTargetInfo")
        return str((target_info.get("targetInfo") or {}).get("targetId") or "").strip()
    finally:
        with suppress(Exception):
            await session.detach()


def _target_id_for_page_sync(browser_context: Any, page: Any) -> str:
    new_cdp_session = getattr(browser_context, "new_cdp_session", None)
    if not callable(new_cdp_session):
        return ""
    session = None
    try:
        session = new_cdp_session(page)
        target_info = session.send("Target.getTargetInfo")
        target_id = str((target_info.get("targetInfo") or {}).get("targetId") or "").strip()
        return target_id[:_TARGET_ID_LIMIT]
    except Exception:
        return ""
    finally:
        if session is not None:
            with suppress(Exception):
                session.detach()


def _close_target_via_context(
    browser_context: Any,
    page: Any,
    target_id: str,
    *,
    timeout_seconds: float,
) -> bool:
    if timeout_seconds <= 0:
        return False
    if _is_sync_playwright_owner(browser_context) and getattr(page, "_impl_obj", None) is not None:
        try:
            result = browser_context._sync(
                _await_with_hard_timeout(
                    _playwright_close_target(
                        browser_context._impl_obj,
                        page._impl_obj,
                        target_id,
                    ),
                    timeout_seconds=timeout_seconds,
                )
            )
            return not isinstance(result, dict) or result.get("success") is not False
        except Exception:
            return False

    def close_target() -> bool:
        new_cdp_session = getattr(browser_context, "new_cdp_session", None)
        if not callable(new_cdp_session):
            return False
        session = None
        try:
            session = new_cdp_session(page)
            result = session.send("Target.closeTarget", {"targetId": target_id})
            return not isinstance(result, dict) or result.get("success") is not False
        except Exception:
            return False
        finally:
            if session is not None:
                with suppress(Exception):
                    session.detach()

    return _run_bounded_bool(close_target, timeout_seconds=timeout_seconds)


async def _playwright_close_target(
    context_impl: Any,
    page_impl: Any,
    target_id: str,
) -> Any:
    session = await context_impl.new_cdp_session(page_impl)
    try:
        return await session.send("Target.closeTarget", {"targetId": target_id})
    finally:
        with suppress(Exception):
            await session.detach()


def _run_bounded_bool(
    operation: Callable[[], bool],
    *,
    timeout_seconds: float,
) -> bool:
    if timeout_seconds <= 0:
        return False
    finished = threading.Event()
    succeeded = threading.Event()

    def invoke() -> None:
        try:
            if operation():
                succeeded.set()
        except Exception:
            pass
        finally:
            finished.set()

    worker = threading.Thread(
        target=invoke,
        name="jobcockpit-browser-fallback",
        daemon=True,
    )
    worker.start()
    if not finished.wait(timeout_seconds):
        return False
    return succeeded.is_set()


def _http_target_closer(
    cdp_endpoint_url: str | None,
) -> EmergencyTargetCloser | None:
    origin = _local_cdp_origin(cdp_endpoint_url)
    if not origin:
        return None

    def close_target(target_id: str) -> bool:
        target_url = f"{origin}/json/close/{quote(target_id, safe='')}"
        try:
            with httpx.Client(
                timeout=1.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get(target_url)
            return response.status_code in {200, 404}
        except httpx.HTTPError:
            return False

    return close_target


def _cleanup_deadline(cleanup_timeout_seconds: float) -> float:
    if cleanup_timeout_seconds <= 0:
        raise ValueError("cleanup_timeout_seconds must be positive")
    return time.monotonic() + cleanup_timeout_seconds


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _local_cdp_origin(endpoint_url: str | None) -> str:
    try:
        parsed = validate_local_cdp_endpoint(str(endpoint_url or ""))
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return f"{parsed.scheme}://{authority}:{parsed.port}"


_BROWSER_PAGE_CLEANUP_SUPERVISOR.recover_pending()
