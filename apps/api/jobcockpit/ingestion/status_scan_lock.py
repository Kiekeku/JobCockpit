from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import BinaryIO

from jobcockpit.core.config import Settings


class StatusScanProcessLockUnavailable(RuntimeError):
    pass


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class StatusScanProcessLock:
    """Crash-safe, process-scoped singleton lock for all reconciliation modes."""

    def __init__(self, settings: Settings) -> None:
        database_key = hashlib.sha256(
            settings.database_url.encode("utf-8")
        ).hexdigest()[:16]
        self.path = (
            Path(settings.artifact_root).parent
            / "locks"
            / f"job-status-reconciliation-{database_key}.lock"
        )
        self._handle: BinaryIO | None = None
        self._thread_lock: threading.Lock | None = None
        self._acquired = False

    def __enter__(self) -> StatusScanProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self._acquired:
            return
        lock_key = str(self.path.resolve()).casefold()
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.Lock())
        if not thread_lock.acquire(blocking=False):
            raise StatusScanProcessLockUnavailable(
                "another status reconciliation process is already running"
            )

        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _acquire_os_file_lock(handle)
        except OSError as error:
            if handle is not None:
                handle.close()
            thread_lock.release()
            raise StatusScanProcessLockUnavailable(
                "another status reconciliation process is already running"
            ) from error

        self._handle = handle
        self._thread_lock = thread_lock
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        handle = self._handle
        thread_lock = self._thread_lock
        try:
            if handle is not None:
                _release_os_file_lock(handle)
        finally:
            if handle is not None:
                handle.close()
            if thread_lock is not None:
                thread_lock.release()
            self._handle = None
            self._thread_lock = None
            self._acquired = False


def _acquire_os_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
