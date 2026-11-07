from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread, current_thread

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.models import BackgroundWorkerLease
from jobcockpit.db.session import session_scope
from jobcockpit.utils.ids import new_id


API_BACKGROUND_WORKER_LEASE = "api_background_queue"


logger = logging.getLogger(__name__)


def _is_transient_sqlite_lock(error: OperationalError) -> bool:
    normalized = str(error.orig or error).strip().casefold()
    return normalized.startswith("database") and "locked" in normalized


def begin_background_worker_lease_transaction(
    db: Session,
    worker_id: str | None,
    lease_token: str | None,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    """Fence a lease-owned write for the lifetime of its database transaction.

    SQLite serializes writers after BEGIN IMMEDIATE. Validating the generation
    after taking that lock prevents another supervisor from replacing the lease
    between this check and the queue mutation's commit.
    """
    if lease_token is None:
        return True
    if db.get_bind().dialect.name == "sqlite" and not db.in_transaction():
        db.execute(text("BEGIN IMMEDIATE"))
    return background_worker_lease_is_valid(
        db,
        worker_id or "",
        lease_token,
        lease_name=lease_name,
    )


def background_worker_lease_is_valid(
    db,
    worker_id: str,
    lease_token: str | None = None,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    lease = db.get(BackgroundWorkerLease, lease_name)
    if lease is None:
        return False
    predicates = [
        BackgroundWorkerLease.lease_name == lease_name,
        BackgroundWorkerLease.owner_id == worker_id,
        BackgroundWorkerLease.expires_at > datetime.now(UTC),
    ]
    if lease_token is not None:
        predicates.append(BackgroundWorkerLease.lease_token == lease_token)
    return (
        db.execute(select(BackgroundWorkerLease.lease_name).where(*predicates)).scalar_one_or_none()
        is not None
    )


def current_background_worker_lease_token(
    settings: Settings,
    worker_id: str,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> str | None:
    with session_scope(settings) as db:
        return db.execute(
            select(BackgroundWorkerLease.lease_token).where(
                BackgroundWorkerLease.lease_name == lease_name,
                BackgroundWorkerLease.owner_id == worker_id,
                BackgroundWorkerLease.expires_at > datetime.now(UTC),
            )
        ).scalar_one_or_none()


def background_worker_lease_is_current(
    settings: Settings,
    worker_id: str,
    lease_token: str,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    with session_scope(settings) as db:
        return background_worker_lease_is_valid(
            db,
            worker_id,
            lease_token,
            lease_name=lease_name,
        )


def acquire_background_worker_lease(
    settings: Settings,
    worker_id: str,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.background_worker_lease_seconds)
    lease_token = new_id("lease")
    with session_scope(settings) as db:
        claimed = db.execute(
            update(BackgroundWorkerLease)
            .where(
                BackgroundWorkerLease.lease_name == lease_name,
                or_(
                    BackgroundWorkerLease.owner_id == worker_id,
                    BackgroundWorkerLease.expires_at <= now,
                ),
            )
            .values(
                owner_id=worker_id,
                lease_token=lease_token,
                expires_at=expires_at,
                updated_at=now,
            )
        )
        if claimed.rowcount == 1:
            return True
        try:
            with db.begin_nested():
                db.add(
                    BackgroundWorkerLease(
                        lease_name=lease_name,
                        owner_id=worker_id,
                        lease_token=lease_token,
                        expires_at=expires_at,
                    )
                )
                db.flush()
            return True
        except IntegrityError:
            return False


def renew_background_worker_lease(
    settings: Settings,
    worker_id: str,
    lease_token: str | None = None,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.background_worker_lease_seconds)
    with session_scope(settings) as db:
        predicates = [
            BackgroundWorkerLease.lease_name == lease_name,
            BackgroundWorkerLease.owner_id == worker_id,
        ]
        if lease_token is not None:
            predicates.append(BackgroundWorkerLease.lease_token == lease_token)
        renewed = db.execute(
            update(BackgroundWorkerLease)
            .where(*predicates)
            .values(expires_at=expires_at, updated_at=now)
        )
        return renewed.rowcount == 1


def release_background_worker_lease(
    settings: Settings,
    worker_id: str,
    lease_token: str | None = None,
    *,
    lease_name: str = API_BACKGROUND_WORKER_LEASE,
) -> bool:
    with session_scope(settings) as db:
        predicates = [
            BackgroundWorkerLease.lease_name == lease_name,
            BackgroundWorkerLease.owner_id == worker_id,
        ]
        if lease_token is not None:
            predicates.append(BackgroundWorkerLease.lease_token == lease_token)
        released = db.execute(delete(BackgroundWorkerLease).where(*predicates))
        return released.rowcount == 1


class BackgroundWorkerLeaseGuard:
    """Hold the shared queue lease for non-API worker entry points."""

    def __init__(
        self,
        settings: Settings,
        worker_id: str,
        *,
        lease_name: str = API_BACKGROUND_WORKER_LEASE,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id
        self.lease_name = lease_name
        self.acquired = False
        self.lease_token: str | None = None
        self.lost_event = Event()
        self.stop_event = Event()
        self.heartbeat_thread: Thread | None = None
        self._state_lock = Lock()

    @property
    def lost(self) -> bool:
        return self.lost_event.is_set()

    def __enter__(self) -> "BackgroundWorkerLeaseGuard":
        with self._state_lock:
            if self.stop_event.is_set():
                return self
            try:
                self.acquired = acquire_background_worker_lease(
                    self.settings,
                    self.worker_id,
                    lease_name=self.lease_name,
                )
                if self.acquired:
                    self.lease_token = current_background_worker_lease_token(
                        self.settings,
                        self.worker_id,
                        lease_name=self.lease_name,
                    )
            except OperationalError as error:
                if not _is_transient_sqlite_lock(error):
                    raise
                logger.warning(
                    "Background worker lease acquisition deferred for %s; "
                    "error_type=sqlite_locked",
                    self.worker_id,
                )
                self.acquired = False
                self.lease_token = None
                return self
            if not self.acquired:
                return self
            if self.lease_token is None or self.stop_event.is_set():
                if self.lease_token is not None:
                    release_background_worker_lease(
                        self.settings,
                        self.worker_id,
                        self.lease_token,
                        lease_name=self.lease_name,
                    )
                self.acquired = False
                self.lost_event.set()
                return self
            self.heartbeat_thread = Thread(
                target=self._heartbeat,
                name=f"jobcockpit-cli-worker-heartbeat-{self.worker_id}",
                daemon=True,
            )
            self.heartbeat_thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._release(mark_lost=False)

    def relinquish(self) -> bool:
        """Fence this worker immediately and release its supervisor lease."""
        return self._release(mark_lost=True)

    def _release(self, *, mark_lost: bool) -> bool:
        self.stop_event.set()
        if mark_lost:
            self.lost_event.set()
        heartbeat_thread = self.heartbeat_thread
        if heartbeat_thread and heartbeat_thread is not current_thread():
            heartbeat_thread.join(self.settings.background_worker_shutdown_timeout_seconds)
        with self._state_lock:
            if not self.acquired:
                return False
            try:
                return release_background_worker_lease(
                    self.settings,
                    self.worker_id,
                    self.lease_token,
                    lease_name=self.lease_name,
                )
            except OperationalError as error:
                if not _is_transient_sqlite_lock(error):
                    raise
                self.lost_event.set()
                logger.warning(
                    "Background worker lease release deferred to expiry for %s; "
                    "error_type=sqlite_locked",
                    self.worker_id,
                )
                return False
            finally:
                self.acquired = False

    def _heartbeat(self) -> None:
        interval = max(1.0, self.settings.background_worker_lease_seconds / 3)
        while not self.stop_event.wait(interval):
            try:
                renewed = renew_background_worker_lease(
                    self.settings,
                    self.worker_id,
                    self.lease_token,
                    lease_name=self.lease_name,
                )
            except Exception:
                logger.error(
                    "Background worker lease heartbeat failed for %s; error_type=%s",
                    self.worker_id,
                    "heartbeat_error",
                )
                self.lost_event.set()
                return
            if not renewed:
                self.lost_event.set()
                return


def bootstrap_background_worker(
    settings: Settings,
    *,
    worker_id: str,
    lease_token: str,
) -> tuple[int, int, int]:
    """Run the identical lease-owned startup sequence for API and CLI workers."""
    with session_scope(settings) as db:
        if not begin_background_worker_lease_transaction(db, worker_id, lease_token):
            raise RuntimeError("background_worker_lease_lost")
        from jobcockpit.api.page_capture_runs import backfill_job_source_identities

        identities_backfilled = backfill_job_source_identities(db)

    from jobcockpit.api.job_reprocess_runs import recover_stale_job_reprocess_runs
    from jobcockpit.api.page_capture_runs import recover_stale_page_capture_runs

    recovered_captures = recover_stale_page_capture_runs(
        settings,
        active_worker_id=worker_id,
        active_lease_token=lease_token,
        recover_orphaned=True,
        required_worker_id=worker_id,
        required_lease_token=lease_token,
    )
    recovered_reprocesses = recover_stale_job_reprocess_runs(
        settings,
        active_worker_id=worker_id,
        active_lease_token=lease_token,
        recover_orphaned=True,
        required_worker_id=worker_id,
        required_lease_token=lease_token,
    )
    return identities_backfilled, recovered_captures, recovered_reprocesses
