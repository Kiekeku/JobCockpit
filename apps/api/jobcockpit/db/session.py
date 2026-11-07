from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from jobcockpit.core.config import Settings
from jobcockpit.db.models import Base

_engines: dict[str, Engine] = {}
_engines_lock = Lock()


def get_engine(settings: Settings) -> Engine:
    with _engines_lock:
        return _get_or_create_engine(settings)


def _get_or_create_engine(settings: Settings) -> Engine:
    engine = _engines.get(settings.database_url)
    if engine is None:
        is_sqlite = settings.database_url.startswith("sqlite")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        engine = create_engine(settings.database_url, connect_args=connect_args)
        if is_sqlite:
            event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        _engines[settings.database_url] = engine
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def init_database(settings: Settings) -> None:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings)
    existing_tables = set(inspect(engine).get_table_names())
    if not existing_tables:
        Base.metadata.create_all(engine)
        existing_tables = set(inspect(engine).get_table_names())
    if "job_postings" in existing_tables:
        create_analytics_views(engine)


def create_analytics_views(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE VIEW IF NOT EXISTS active_job_postings AS
                SELECT *
                FROM job_postings
                WHERE json_extract(source, '$.merged_into_job_id') IS NULL
                """
            )
        )


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    SessionLocal = sessionmaker(bind=get_engine(settings), autoflush=False)
    session = SessionLocal()
    if settings.publication_enabled:
        from jobcockpit.services.publication import enqueue_changed_jobs

        session.info["publication_settings"] = settings
        event.listen(session, "before_flush", enqueue_changed_jobs)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
