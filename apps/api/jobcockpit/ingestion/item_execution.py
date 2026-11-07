"""Bounded preparation and isolated, caller-thread writes for ingestion items."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import closing
from typing import TypeVar

from sqlalchemy.orm import Session

from jobcockpit.core.config import Settings
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.concurrency import ingestion_link_concurrency, prepare_links
from jobcockpit.storage.artifacts import ArtifactStorageService, ArtifactStorageWriteTracker


Work = TypeVar("Work")
Prepared = TypeVar("Prepared")
Result = TypeVar("Result")


def execute_posting_items(
    items: Iterable[Work],
    *,
    settings: Settings,
    storage: ArtifactStorageService,
    prepare: Callable[[Work], Prepared],
    persist: Callable[[Work, Prepared, Session, ArtifactStorageWriteTracker], Result],
    record_failure: Callable[[Work, Exception, Session], None],
    completed: Callable[[Work, Result | Exception], None],
) -> None:
    """Commit each item before reporting it; a later failure cannot undo it.

    Only preparation runs in workers. Failure records get a fresh transaction,
    and progress callbacks run after commit, outside item error handling. Closing
    the preparation iterator drains workers before the source releases resources.
    """
    with closing(prepare_links(
        items, prepare, concurrency=ingestion_link_concurrency(settings),
    )) as prepared_items:
        for work, prepared in prepared_items:
            tracked_storage = ArtifactStorageWriteTracker(storage)
            try:
                if isinstance(prepared, Exception):
                    raise prepared
                with session_scope(settings) as item_db:
                    result = persist(work, prepared, item_db, tracked_storage)
            except Exception as error:
                tracked_storage.rollback()
                with session_scope(settings) as failure_db:
                    record_failure(work, error, failure_db)
                completed(work, error)
            except BaseException:
                tracked_storage.rollback()
                raise
            else:
                tracked_storage.storage_uris.clear()
                completed(work, result)
