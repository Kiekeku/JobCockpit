"""Shared lifecycle for independent, lease-fenced SQLite work queues."""

from contextlib import contextmanager
import logging
from threading import Event, Lock, Thread
from time import monotonic

from jobcockpit.api.background_worker_leases import BackgroundWorkerLeaseGuard
from jobcockpit.utils.ids import new_id

logger = logging.getLogger(__name__)


class LeasedWorker:
    def __init__(self, settings, *, lease_name, prefix):
        self.settings = settings
        self.lease_name = lease_name
        self.worker_id = new_id(prefix)
        self.stop_event = Event()
        self._lease_guard_lock = Lock()
        self._active_lease_guard = None
        self.thread = Thread(target=self._run, name=self.worker_id, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(self.settings.background_worker_shutdown_timeout_seconds)
        if self.thread.is_alive():
            with self._lease_guard_lock:
                lease = self._active_lease_guard
            if lease is not None:
                lease.relinquish()
            logger.warning("Worker %s relinquished its lease while work unwinds", self.worker_id)

    @contextmanager
    def _registered_lease_guard(self):
        guard = BackgroundWorkerLeaseGuard(
            self.settings,
            self.worker_id,
            lease_name=self.lease_name,
        )
        with self._lease_guard_lock:
            stopping = self.stop_event.is_set()
            if not stopping:
                self._active_lease_guard = guard
        if stopping:
            yield guard
            return
        try:
            with guard as lease:
                yield lease
        finally:
            with self._lease_guard_lock:
                if self._active_lease_guard is guard:
                    self._active_lease_guard = None

    def _bootstrap(self, token):
        pass

    def _process_once(self, token):
        raise NotImplementedError

    def _recover_stale_work(self, token, *, include_owned=False):
        pass

    def _run(self):
        while not self.stop_event.is_set():
            try:
                with self._registered_lease_guard() as lease:
                    if not lease.acquired or lease.lease_token is None:
                        self.stop_event.wait(self.settings.background_worker_poll_seconds)
                        continue
                    self._bootstrap(lease.lease_token)
                    next_recovery = 0.0
                    while not self.stop_event.is_set() and not lease.lost:
                        try:
                            result = self._process_once(lease.lease_token)
                            if self.stop_event.is_set() or lease.lost:
                                break
                            if result:
                                continue
                            if monotonic() >= next_recovery:
                                self._recover_stale_work(lease.lease_token)
                                next_recovery = monotonic() + 30
                        except Exception as error:
                            if self.stop_event.is_set() or lease.lost:
                                break
                            logger.error(
                                "Worker %s iteration failed; error_type=%s",
                                self.worker_id,
                                type(error).__name__,
                            )
                            self._recover_stale_work(lease.lease_token, include_owned=True)
                        self.stop_event.wait(self.settings.background_worker_poll_seconds)
            except Exception as error:
                logger.error(
                    "Worker %s startup failed; error_type=%s", self.worker_id, type(error).__name__
                )
                self.stop_event.wait(self.settings.background_worker_poll_seconds)
