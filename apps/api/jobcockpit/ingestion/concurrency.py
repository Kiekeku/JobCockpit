"""Coordination for concurrent ingestion in API and CLI processes."""
from __future__ import annotations

from contextvars import ContextVar
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from collections.abc import Callable
from functools import wraps
from hashlib import sha256
from inspect import signature
from threading import BoundedSemaphore, Lock
import time
import logging
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    background_worker_lease_is_valid,
)
from jobcockpit.utils.ids import new_id


_active_scope: ContextVar[tuple | None] = ContextVar("ingestion_scope", default=None)
_failure_handlers: ContextVar[list[Callable[[BaseException], None]] | None] = ContextVar(
    "ingestion_failure_handlers", default=None
)
logger = logging.getLogger(__name__)
_resource_slots = {"source": BoundedSemaphore(4), "fetch": BoundedSemaphore(4), "parse": BoundedSemaphore(4),
                   "resolve": BoundedSemaphore(2), "browser": BoundedSemaphore(2)}
# Source orchestration may wait for link workers, so it must never hold their
# fetch permits. Acquisition order: source -> host/link -> fetch/resolve -> browser.
_host_slots = {}
_host_slots_lock = Lock()
_link_slots = BoundedSemaphore(4)


def ingestion_link_concurrency(settings):
    concurrency = min(settings.ingestion_link_concurrency,
                      settings.posting_external_resolution_concurrency)
    if settings.posting_rendered_fallback_provider != "disabled":
        concurrency = min(concurrency, settings.posting_rendered_fallback_concurrency)
    return concurrency


@contextmanager
def link_request_slot(url):
    """Share preparation limits across board and mail runs in this process."""
    host = (urlsplit(url).hostname or "").lower()
    with _host_slots_lock:
        slot, users = _host_slots.get(host, (BoundedSemaphore(2), 0))
        _host_slots[host] = (slot, users + 1)
    try:
        with slot, _link_slots:
            yield
    finally:
        with _host_slots_lock:
            _, users = _host_slots[host]
            if users == 1:
                del _host_slots[host]
            else:
                _host_slots[host] = (slot, users - 1)


def prepare_links(items, prepare, *, concurrency=4):
    """Yield (input, result-or-error) as completed, with bounded read-ahead.

    Workers receive only their input, never a database session. The caller owns
    all counters, writes, and per-item recovery. Drain workers before closing
    adapter resources on cancellation. No context carrying DB ownership is copied.
    """
    iterator = iter(items)
    if concurrency <= 1:
        for item in iterator:
            try:
                result = prepare(item)
            except Exception as error:
                result = error
            yield item, result
        return
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {}

        def submit_next():
            try:
                item = next(iterator)
            except StopIteration:
                return
            pending[executor.submit(prepare, item)] = item

        for _ in range(concurrency):
            submit_next()
        try:
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        result = error
                    yield item, result
                    submit_next()
        finally:
            for future in pending:
                future.cancel()


def bounded_call(resource, function, *args, **kwargs):
    """Bound expensive preparation across simultaneous runs within this process."""
    with _resource_slots[resource]:
        return function(*args, **kwargs)


def bounded_resource(resource):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            return bounded_call(resource, function, *args, **kwargs)
        return call
    return decorate


def begin_ingestion_write(db: Session) -> None:
    """Acquire the SQLite writer before reading identity or changing progress.

    sqlite3's legacy mode does not start a DB transaction for SELECT, even when
    SQLAlchemy has autobegun a Session transaction. Retry only BEGIN IMMEDIATE:
    no write or DB snapshot exists yet, so no preparation or caller work needs
    replaying. An existing transaction belongs to the caller (e.g. a capture
    fence); never commit or roll it back here.
    """
    if db.get_bind().dialect.name == "sqlite":
        connection = db.connection()
        raw = connection.connection.driver_connection
        if not raw.in_transaction:
            for attempt in range(4):
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    break
                except OperationalError as error:
                    code = getattr(error.orig, "sqlite_errorcode", None)
                    if code is None or (code & 0xFF) not in {5, 6} or attempt == 3:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
    scope = _active_scope.get()
    if scope:
        guard, error_factory = scope
        if guard.lost or not background_worker_lease_is_valid(
            db, guard.worker_id, guard.lease_token, lease_name=guard.lease_name
        ):
            raise error_factory(409, "Ingestion ownership expired; retry the source")


def ats_scope(arguments):
    request = arguments["request"]
    url = urlsplit(request.source_url)
    # Search/facet variants still operate on the same source.
    source = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path.rstrip("/"), "", ""))
    return f"ats:{request.provider}:{source}"


def gmail_scope(arguments):
    source = arguments["source"]
    return f"gmail:{source.candidate_id}:{source.job_alert_source_id}"


def coordinated_ingestion(scope_key, error_factory):
    """Keep a renewable source lease through the final progress commit."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def run(*args, **kwargs):
            arguments = parameters.bind(*args, **kwargs).arguments
            db = arguments["db"]
            scope_hash = sha256(scope_key(arguments).encode("utf-8")).hexdigest()
            # These run entry points already own commit boundaries. Publish any
            # caller-created source/preview state before a separate lease writer.
            db.commit()
            with BackgroundWorkerLeaseGuard(
                arguments["settings"], new_id("ingest_owner"),
                lease_name=f"ingestion:{scope_hash}",
            ) as guard:
                if not guard.acquired:
                    raise error_factory(409, "This source is already being ingested; retry later")
                token = _active_scope.set((guard, error_factory))
                handlers = []
                handlers_token = _failure_handlers.set(handlers)
                try:
                    try:
                        result = function(*args, **kwargs)
                        db.commit()
                        return result
                    finally:
                        _active_scope.reset(token)
                        _failure_handlers.reset(handlers_token)
                except BaseException as error:
                    db.rollback()
                    # Terminal bookkeeping uses a fresh guarded transaction,
                    # after the expired scope has stopped fencing its writes.
                    for handler in handlers:
                        try:
                            handler(error)
                        except Exception:
                            logger.exception("Failed to record interrupted ingestion")
                    raise

        return run
    return decorate


def on_ingestion_failure(handler: Callable[[BaseException], None]) -> None:
    handlers = _failure_handlers.get()
    if handlers is not None:
        handlers.append(handler)


@event.listens_for(Session, "before_flush")
def _fence_scope_flush(db, _context, _instances):
    if _active_scope.get():
        begin_ingestion_write(db)


@event.listens_for(Session, "do_orm_execute")
def _fence_scope_bulk_write(state):
    if _active_scope.get() and (state.is_insert or state.is_update or state.is_delete):
        begin_ingestion_write(state.session)


@event.listens_for(Session, "before_commit")
def _fence_scope_commit(db):
    if _active_scope.get():
        begin_ingestion_write(db)


def track_ingestion_artifact(db, storage, storage_uri):
    db.info.setdefault("ingestion_artifacts", []).append((storage, storage_uri))


@event.listens_for(Session, "after_commit")
def _keep_committed_artifacts(db):
    if not db.in_nested_transaction():
        db.info.pop("ingestion_artifacts", None)


@event.listens_for(Session, "after_transaction_end")
def _remove_rolled_back_artifacts(db, transaction):
    if transaction.parent is None:
        for storage, uri in reversed(db.info.pop("ingestion_artifacts", [])):
            storage.delete(uri)
