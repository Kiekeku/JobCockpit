"""Regression for the constraint omissions observed in the local SQLite database."""

import sqlite3

import pytest
import sqlalchemy as sa
from alembic import command

from jobcockpit.db.models import Base
from scripts.migrate import (
    EXTENSION_ASSISTANCE_TABLES, EXTENSION_MEMORY_TABLES, PUBLICATION_TABLES,
    SQLITE_CONSTRAINT_REPAIR_REVISION,
    config_for_database, sqlite_schema_mismatches,
)


MISSING_FKS = {
    "application_package_documents": {"generated_document_id"},
    "generated_documents": {"truthfulness_check_id"},
    "truthfulness_checks": {"application_package_id"},
    "job_alert_emails": {"email_ingestion_run_id"},
    "job_alert_email_processing_states": {"job_alert_source_id", "last_ingestion_run_id"},
    "job_alert_links": {"job_alert_email_id", "linked_job_id"},
}


def drifted_database(tmp_path):
    path = tmp_path / "repair.db"
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    for table in metadata.tables.values():
        for constraint in list(table.constraints):
            if isinstance(constraint, sa.ForeignKeyConstraint) and any(
                col.name in MISSING_FKS.get(table.name, set()) for col in constraint.columns
            ):
                table.constraints.remove(constraint)
            if isinstance(constraint, sa.UniqueConstraint) and table.name in {
                "job_postings",
                "applications",
                "job_alert_email_processing_states",
            }:
                table.constraints.remove(constraint)
        for index in list(table.indexes):
            if index.name == "ix_job_postings_last_applied_capture_seq":
                table.indexes.remove(index)
    engine = sa.create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(
            metadata.tables["stored_artifacts"]
            .insert()
            .values(
                artifact_id="artifact",
                artifact_type="job_snapshot",
                storage_backend="local",
                storage_uri="local://fixture.txt",
                original_file_name="fixture.txt",
                content_type="text/plain",
                size_bytes=0,
                sha256="hash",
                privacy_class="private",
            )
        )
        db.execute(
            metadata.tables["job_postings"]
            .insert()
            .values(
                id="job",
                source_type="paste",
                raw_content_artifact_id="artifact",
                posting_snapshot_hash="snapshot",
                company_name="Example",
                normalized_title="Engineer",
            )
        )
        db.exec_driver_sql("CREATE VIEW active_job_postings AS SELECT * FROM job_postings")
        db.exec_driver_sql(
            "CREATE TRIGGER preserve_job_id BEFORE UPDATE OF id ON job_postings "
            "BEGIN SELECT RAISE(ABORT, 'immutable fixture id'); END"
        )
    engine.dispose()
    config = config_for_database(f"sqlite:///{path}")
    command.stamp(config, "20260905_0028")
    assert len(sqlite_schema_mismatches(path)) == 10
    return path, config


def snapshot(path):
    with sqlite3.connect(path) as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        return {
            table: sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for table in tables
        }


def test_repairs_observed_constraints_without_changing_rows_or_views(tmp_path):
    path, config = drifted_database(tmp_path)
    before = snapshot(path)
    command.upgrade(config, SQLITE_CONSTRAINT_REPAIR_REVISION)
    assert sqlite_schema_mismatches(path) == []
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM active_job_postings").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='preserve_job_id'"
        ).fetchone() == (1,)
        columns = [row[1] for row in db.execute("PRAGMA table_info(job_postings)")]
        copied = ["'duplicate'" if col == "id" else f'"{col}"' for col in columns]
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            db.execute(f"INSERT INTO job_postings SELECT {', '.join(copied)} FROM job_postings")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO truthfulness_checks VALUES "
                "('check', 'missing', 'passed', '[]', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    command.upgrade(config, SQLITE_CONSTRAINT_REPAIR_REVISION)
    assert snapshot(path) == before


@pytest.mark.parametrize(
    "invalid", ["duplicate", "orphan", "missing_fk_orphan", "late_index_failure"]
)
def test_invalid_existing_data_aborts_before_schema_changes(tmp_path, invalid):
    path, config = drifted_database(tmp_path)
    with sqlite3.connect(path) as db:
        if invalid == "duplicate":
            columns = [row[1] for row in db.execute("PRAGMA table_info(job_postings)")]
            copied = ["'duplicate'" if col == "id" else f'"{col}"' for col in columns]
            db.execute(f"INSERT INTO job_postings SELECT {', '.join(copied)} FROM job_postings")
        elif invalid == "orphan":
            db.execute("UPDATE job_postings SET raw_content_artifact_id='missing'")
        elif invalid == "missing_fk_orphan":
            db.execute(
                "INSERT INTO truthfulness_checks VALUES "
                "('check', 'missing', 'passed', '[]', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        else:
            # Fail after table rebuilds to exercise transactional DDL rollback.
            db.execute("CREATE INDEX ix_job_postings_last_applied_capture_seq ON job_postings(id)")
        schema = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    before = snapshot(path)
    with pytest.raises(RuntimeError, match="constraint repair"):
        command.upgrade(config, SQLITE_CONSTRAINT_REPAIR_REVISION)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260905_0028",
        )
    assert snapshot(path) == before


def test_healthy_schema_is_not_rebuilt_and_downgrade_preserves_contract(tmp_path):
    path = tmp_path / "healthy.db"
    config = config_for_database(f"sqlite:///{path}")
    command.upgrade(config, "20260905_0028")
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    command.upgrade(config, SQLITE_CONSTRAINT_REPAIR_REVISION)
    assert sqlite_schema_mismatches(path, set(Base.metadata.tables) - EXTENSION_ASSISTANCE_TABLES - EXTENSION_MEMORY_TABLES - PUBLICATION_TABLES) == []
    command.downgrade(config, "20260905_0028")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before
