"""Deadline-bounded Firefox rendering in an owned, cancellable process tree."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from collections.abc import Callable

from jobcockpit.core.config import Settings
from jobcockpit.core.tls import install_windows_certifi_workaround
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, PostingResolution
from jobcockpit.ingestion.process_tree import kill_process_tree


class ScheduledSessionRecovery:
    """At most two Indeed preparations, with no overlap and a cooldown."""

    def __init__(self, *, cooldown_seconds: float = 300, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._cooldown_seconds = cooldown_seconds
        self._attempts = 0
        self._in_progress = False
        self._next_allowed_at = float('-inf')

    def claim(self, site: str, deadline: float) -> bool:
        return self.request(site, deadline) == 'granted'

    def request(self, site: str, deadline: float) -> str:
        with self._lock:
            now = self._clock()
            if site != 'indeed':
                return 'denied_site'
            # Reserve the capped 60s preparation plus a guarded reload/startup.
            if deadline - now < 90:
                return 'denied_deadline'
            if self._in_progress:
                return 'denied_active'
            if self._attempts >= 2:
                return 'denied_limit'
            if now < self._next_allowed_at:
                return 'denied_cooldown'
            self._attempts += 1
            self._in_progress = True
            return 'granted'

    def complete(self) -> None:
        with self._lock:
            if self._in_progress:
                self._in_progress = False
                self._next_allowed_at = self._clock() + self._cooldown_seconds


def _render_in_worker(connection, settings: Settings, hint: ExternalPostingHint, method: str) -> None:
    # A separate POSIX session lets cancellation kill the driver and Firefox too.
    if os.name != "nt":
        os.setsid()
    connection.recv()  # Parent establishes process ownership before browser launch.
    from jobcockpit.ingestion.browser_health import collect_signals

    signals = []
    try:
        # Windows spawn starts a fresh interpreter; the parent's TLS wrapper
        # is not inherited by this browser worker.
        install_windows_certifi_workaround()
        from jobcockpit.ingestion.invisible_browser import InvisiblePlaywrightRenderedPageResolver
        from jobcockpit.ingestion.invisible_session import verification_site
        from jobcockpit.ingestion.posting_discovery import _PlaywrightRenderedError
        from jobcockpit.ingestion.url_policy import ScheduledRenderedUrlPolicy

        resolver = InvisiblePlaywrightRenderedPageResolver.from_settings(
            settings,
            url_policy=ScheduledRenderedUrlPolicy(
                extra_domain_groups=settings.scheduled_rendered_extra_domain_groups,
            ),
            # Recovery is explicitly granted by the scan coordinator below;
            # never let each blocked posting start its own preparation loop.
            auto_prepare=False,
            # Allow brief passive recovery, but never spend the interactive
            # sign-in wait on every blocked posting in an unattended scan.
            auth_wall_wait_seconds=min(settings.playwright_auth_wall_wait_seconds, 10.0),
            health_root=None,  # The coordinator records once, including worker death.
        )
        with collect_signals() as signals:
            result = getattr(resolver, method)(hint)
            if (settings.invisible_playwright_auto_prepare
                    and verification_site(hint.source_url) == 'indeed'
                    and any(code in result.error for code in ('blocked_rendered_page', 'auth_wall_unresolved'))):
                connection.send({'prepare_site': 'indeed'})
                if connection.recv() is True:
                    # Preparation retains the scheduled DNS/origin guard and
                    # the same owned process tree / hard scan deadline.
                    resolver.prepare_timeout_seconds = min(resolver.prepare_timeout_seconds, 60)
                    try:
                        resolver._prepare_session(hint, started_at=time.time())
                    except _PlaywrightRenderedError as error:
                        result = PostingResolution(status='needs_review', error=str(error))
                    else:
                        result = getattr(resolver, method)(hint)
    except Exception:
        result = PostingResolution(status="needs_review", error="invisible_playwright_worker_failed")
    connection.send({"resolution": result, "signals": signals})
    # Stay alive until the parent removes the entire tree, even if browser
    # cleanup failed. Otherwise descendants could outlive their identifiable root.
    connection.recv()


class ScheduledInvisiblePlaywrightResolver:
    provider = "invisible_playwright"

    def __init__(self, settings: Settings, deadline: float) -> None:
        self.settings = settings
        self.deadline = deadline
        self.cleanup_finalization_lock = threading.RLock()
        self._cancelled = False
        self._process = None
        self._job = None
        self._cleanup_complete = False
        self.session_recovery = ScheduledSessionRecovery(
            cooldown_seconds=settings.invisible_playwright_prepare_cooldown_seconds,
        )

    def resolve(self, hint: ExternalPostingHint) -> PostingResolution:
        return self._run(hint, "resolve")

    def load_page(self, hint: ExternalPostingHint) -> PostingResolution:
        return self._run(hint, "load_page")

    def _run(self, hint: ExternalPostingHint, method: str) -> PostingResolution:
        from jobcockpit.ingestion.browser_health import observe

        return observe(
            self.settings.artifact_root if self.settings.browser_health_enabled else None,
            hint, "availability_scan", max(0, self.deadline - time.monotonic()),
            lambda: self._run_unobserved(hint, method),
        )

    def _run_unobserved(self, hint: ExternalPostingHint, method: str) -> PostingResolution:
        from jobcockpit.ingestion.browser_health import record_session_recovery

        with self.cleanup_finalization_lock:
            if self._cancelled or time.monotonic() >= self.deadline:
                return self._cancelled_result()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        recovery_granted = False
        recovery_finished = False
        try:
            with self.cleanup_finalization_lock:
                if self._cancelled or time.monotonic() >= self.deadline:
                    return self._cancelled_result()
                if self._process is not None:
                    raise RuntimeError("scheduled browser resolver is single-use")
                process = context.Process(
                    target=_render_in_worker, args=(child, self.settings, hint, method), daemon=True,
                )
                process.start()
                self._process = process
                if os.name == "nt":
                    self._job = _WindowsWorkerJob(process.sentinel)
                parent.send("start")
            child.close()
            while time.monotonic() < self.deadline:
                if self._cancelled:
                    return self._cancelled_result()
                if parent.poll(min(0.05, max(0, self.deadline - time.monotonic()))):
                    result = parent.recv()
                    if isinstance(result, dict):
                        if 'prepare_site' in result:
                            decision = ('denied_cancelled' if self._cancelled else
                                        self.session_recovery.request(result['prepare_site'], self.deadline))
                            allowed = decision == 'granted'
                            recovery_granted = recovery_granted or allowed
                            record_session_recovery(f'preparation_{decision}')
                            parent.send(allowed)
                            continue
                        from jobcockpit.ingestion.browser_health import record_signal

                        for signal in result.get("signals", []):
                            record_signal(signal)
                        result = result["resolution"]
                    if self._cancelled or time.monotonic() >= self.deadline:
                        return self._cancelled_result()
                    if not isinstance(result, PostingResolution):
                        raise ValueError("invalid browser worker result")
                    if recovery_granted:
                        record_session_recovery(
                            'recheck_resolved' if result.status == 'resolved' else 'recheck_inconclusive',
                        )
                        recovery_finished = True
                    return result
                if not process.is_alive():
                    return PostingResolution(status="needs_review", error="invisible_playwright_worker_failed")
            return PostingResolution(status="needs_review", error="playwright_timeout")
        except (EOFError, OSError):
            return self._cancelled_result() if self._cancelled else PostingResolution(
                status="needs_review", error="invisible_playwright_worker_failed",
            )
        finally:
            if recovery_granted and not recovery_finished:
                record_session_recovery('recovery_interrupted')
            try:
                self.cancel_active_pages()
                # Only release recovery after the entire owned tree is gone.
                if recovery_granted:
                    self.session_recovery.complete()
            finally:
                parent.close()
                child.close()

    def cancel_active_pages(self) -> None:
        # Serialize process creation with cancellation: no worker may start
        # after the coordinator has finalized this resolver.
        with self.cleanup_finalization_lock:
            self._cancelled = True
            if self._cleanup_complete:
                return
            process = self._process
            if self._job is not None:
                self._job.close()
                self._job = None
            elif process is not None:
                kill_process_tree(process.pid)
            if process is not None:
                process.join(timeout=3)
                if process.is_alive():
                    raise RuntimeError("scheduled browser process cleanup failed")
            self._cleanup_complete = True

    @staticmethod
    def _cancelled_result() -> PostingResolution:
        return PostingResolution(status="needs_review", error="playwright_failed: browser_render_cancelled")


class _WindowsWorkerJob:
    """Kill-on-close ownership includes descendants even after worker failure."""

    def __init__(self, process_handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle:
            if not self._kernel.CloseHandle(self._handle):
                import ctypes

                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
