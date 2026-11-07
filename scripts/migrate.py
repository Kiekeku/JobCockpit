from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from tempfile import TemporaryDirectory
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy import UniqueConstraint, create_engine
from sqlalchemy.engine import make_url

from jobcockpit.core.config import Settings
from jobcockpit.db.models import Base
from jobcockpit.ingestion.posting_discovery import capture_source_identity


BASELINE_REVISION = "20260602_0001"
JOB_INGESTION_REVISION = "20260604_0002"
EVIDENCE_SOURCE_ARTIFACT_REVISION = "20260609_0003"
JOB_REPROCESS_REVISION = "20260612_0005"
JOB_REPROCESS_RUN_TYPE_REVISION = "20260612_0006"
DURATION_MONTHS_REVISION = "20260617_0007"
ROLE_INTEREST_REVISION = "20260621_0008"
LANGUAGE_FILTER_REVISION = "20260622_0009"
ACTIVE_JOB_POSTINGS_REVISION = "20260623_0010"
JOB_INGESTION_ITEMS_REVISION = "20260726_0011"
JOB_OPPORTUNITY_STATUS_REVISION = "20260727_0012"
JOB_STATUS_RECONCILIATION_REVISION = "20260727_0013"
GENERATED_DOCUMENT_PROVIDER_REVISION = "20260730_0014"
COMPANY_PROFILES_REVISION = "20260803_0017"
CAPTURE_EXECUTION_MODEL_REVISION = "20260803_0021"
EDITABLE_COVER_LETTER_ARTIFACT_REVISION = "20260809_0022"
COVER_LETTER_GENERATION_QUEUE_REVISION = "20260818_0023"
JOB_AVAILABILITY_EVIDENCE_REVISION = "20260828_0026"
SOURCE_RECONCILIATION_REVISION = "20260905_0027"
ARCHITECTURE_READ_PATHS_REVISION = "20260905_0028"
SQLITE_CONSTRAINT_REPAIR_REVISION = "20260905_0029"
EXTENSION_ASSISTANCE_REVISION = "20260906_0030"
EXTENSION_MEMORY_REVISION = "20260906_0031"
PUBLICATION_REVISION = "20260913_0032"
ACTIVE_JOB_POSTINGS_VIEW = "active_job_postings"
BASELINE_TABLES = {
    "applications",
    "application_package_documents",
    "application_packages",
    "audit_log_entries",
    "candidate_profiles",
    "email_ingestion_runs",
    "evidence_items",
    "fit_analyses",
    "generated_documents",
    "job_alert_email_processing_states",
    "job_alert_emails",
    "job_alert_links",
    "job_alert_sources",
    "job_postings",
    "stored_artifacts",
    "truthfulness_checks",
}
JOB_REPROCESS_TABLES = {"job_reprocess_runs"}
JOB_INGESTION_ITEM_TABLES = {"job_ingestion_items"}
JOB_STATUS_RECONCILIATION_TABLES = {"job_status_scan_runs", "job_status_checks"}
COMPANY_PROFILE_TABLES = {"company_profiles", "company_profile_aliases"}
PAGE_CAPTURE_TABLES = {
    "page_capture_snapshots",
    "page_capture_runs",
    "page_capture_snapshot_scopes",
    "page_capture_subject_heads",
    "page_capture_effects",
    "job_source_identities",
}
BACKGROUND_WORKER_TABLES = {"background_worker_leases"}
COVER_LETTER_GENERATION_QUEUE_TABLES = {"cover_letter_generation_runs"}
EXTENSION_ASSISTANCE_TABLES = {"extension_application_sessions", "extension_approved_answers"}
EXTENSION_MEMORY_TABLES = {"candidate_learned_facts", "extension_learning_runs"}
PUBLICATION_TABLES = {"job_publications"}
JOB_ALERT_LINK_CONTEXT_COLUMNS = {"raw_text", "surrounding_text"}
EVIDENCE_SOURCE_ARTIFACT_COLUMNS = {"source_artifact_id"}
JOB_REPROCESS_RUN_TYPE_COLUMNS = {"type"}
JOB_REPROCESS_RETRY_COLUMNS = {"next_attempt_at", "error_code", "retryable"}
BACKGROUND_WORKER_OWNER_COLUMNS = {"worker_id"}
CAPTURE_CLAIM_COLUMNS = {"claim_token", "supervisor_lease_token"}
FINAL_CAPTURE_ASSOCIATION_COLUMNS = {
    "capture_scope_id",
    "snapshot_id",
    "job_id",
    "scope_hash",
    "scope_kind",
    "association_basis",
    "identity_strength",
    "association_verdict",
    "created_at",
    "updated_at",
}

CAPTURE_EFFECT_COLUMNS = {"last_applied_capture_seq"}
DURATION_MONTHS_COLUMNS = {"duration_months"}
OPPORTUNITY_STATUS_COLUMNS = {"opportunity_status"}
STATUS_RECONCILIATION_COLUMNS = {
    "status_last_checked_at",
    "status_next_check_at",
    "status_check_result",
    "status_check_evidence_code",
    "status_check_failure_count",
    "status_check_not_found_count",
}
JOB_AVAILABILITY_COLUMNS = {
    "status_last_open_at",
    "status_last_open_evidence_code",
    "status_source_repair_required",
}
STATUS_CHECK_DIAGNOSTIC_COLUMNS = {
    "requested_url",
    "final_url",
    "candidate_kind",
    "diagnostics",
}
GENERATED_DOCUMENT_PROVIDER_COLUMNS = {"generation_provider"}
EDITABLE_COVER_LETTER_ARTIFACT_COLUMNS = {"editable_artifact_id"}
ROLE_INTEREST_COLUMNS = {
    "job_alert_sources": {"skip_interest_filter"},
    "job_ingestion_runs": {"jobs_skipped_count"},
}
LANGUAGE_FILTER_COLUMNS = {
    "job_alert_sources": {"skip_language_filter"},
    "job_ingestion_runs": {"jobs_language_skipped_count"},
}
ROLE_AND_LANGUAGE_FILTER_COLUMNS = {
    "job_alert_sources": {"skip_interest_filter", "skip_language_filter"},
    "job_ingestion_runs": {"jobs_skipped_count", "jobs_language_skipped_count"},
}
JOB_ALERT_LINK_CONTEXT_ALLOWED_MISSING_COLUMNS = {
    "job_alert_links": JOB_ALERT_LINK_CONTEXT_COLUMNS,
}
EVIDENCE_SOURCE_ARTIFACT_ALLOWED_MISSING_COLUMNS = {
    "evidence_items": EVIDENCE_SOURCE_ARTIFACT_COLUMNS,
    "job_postings": DURATION_MONTHS_COLUMNS
    | OPPORTUNITY_STATUS_COLUMNS
    | STATUS_RECONCILIATION_COLUMNS,
    **ROLE_AND_LANGUAGE_FILTER_COLUMNS,
}
JOB_REPROCESS_RUN_TYPE_ALLOWED_MISSING_COLUMNS = {
    "job_reprocess_runs": JOB_REPROCESS_RUN_TYPE_COLUMNS,
}
DURATION_MONTHS_ALLOWED_MISSING_COLUMNS = {
    "job_postings": DURATION_MONTHS_COLUMNS
    | OPPORTUNITY_STATUS_COLUMNS
    | STATUS_RECONCILIATION_COLUMNS,
    **ROLE_AND_LANGUAGE_FILTER_COLUMNS,
}
JOB_REPROCESS_ALLOWED_MISSING_COLUMNS = {
    "job_postings": DURATION_MONTHS_COLUMNS
    | OPPORTUNITY_STATUS_COLUMNS
    | STATUS_RECONCILIATION_COLUMNS,
    "job_reprocess_runs": JOB_REPROCESS_RUN_TYPE_COLUMNS,
    **ROLE_AND_LANGUAGE_FILTER_COLUMNS,
}
JOB_INGESTION_ALLOWED_MISSING_COLUMNS = {
    "evidence_items": EVIDENCE_SOURCE_ARTIFACT_COLUMNS,
    "job_alert_links": JOB_ALERT_LINK_CONTEXT_COLUMNS,
    "job_postings": DURATION_MONTHS_COLUMNS
    | OPPORTUNITY_STATUS_COLUMNS
    | STATUS_RECONCILIATION_COLUMNS,
    **ROLE_AND_LANGUAGE_FILTER_COLUMNS,
}


type AllowedMissingColumns = dict[str, set[str]]
type RecoverableSchemaShape = tuple[str, set[str], AllowedMissingColumns, set[str]]


def recoverable_sqlite_schema_shapes(config: Config) -> list[RecoverableSchemaShape]:
    current_revision = ScriptDirectory.from_config(config).get_current_head()
    current_tables = set(Base.metadata.tables)
    pre_publication_tables = current_tables - PUBLICATION_TABLES
    pre_memory_tables = pre_publication_tables - EXTENSION_MEMORY_TABLES
    pre_extension_tables = pre_memory_tables - EXTENSION_ASSISTANCE_TABLES
    pre_source_queue_tables = pre_extension_tables - {"job_source_reconciliations"}
    pre_cover_letter_queue_tables = pre_source_queue_tables - COVER_LETTER_GENERATION_QUEUE_TABLES
    pre_worker_tables = pre_cover_letter_queue_tables - BACKGROUND_WORKER_TABLES
    pre_page_capture_tables = pre_worker_tables - PAGE_CAPTURE_TABLES
    pre_company_profile_tables = pre_page_capture_tables - COMPANY_PROFILE_TABLES
    pre_status_tables = pre_company_profile_tables - JOB_STATUS_RECONCILIATION_TABLES
    pre_ingestion_item_tables = pre_status_tables - JOB_INGESTION_ITEM_TABLES
    pre_reprocess_tables = pre_ingestion_item_tables - JOB_REPROCESS_TABLES
    shapes = [
        (current_revision, current_tables, {}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (EXTENSION_MEMORY_REVISION, pre_publication_tables, {}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (EXTENSION_ASSISTANCE_REVISION, pre_memory_tables, {}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (SQLITE_CONSTRAINT_REPAIR_REVISION, pre_extension_tables, {}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (SOURCE_RECONCILIATION_REVISION, pre_extension_tables, {"cover_letter_generation_runs": {"request_payload"}}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (JOB_AVAILABILITY_EVIDENCE_REVISION, pre_source_queue_tables, {}, {ACTIVE_JOB_POSTINGS_VIEW}),
        (
            EDITABLE_COVER_LETTER_ARTIFACT_REVISION,
            pre_cover_letter_queue_tables,
            {},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            CAPTURE_EXECUTION_MODEL_REVISION,
            pre_cover_letter_queue_tables,
            {"generated_documents": EDITABLE_COVER_LETTER_ARTIFACT_COLUMNS},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            COMPANY_PROFILES_REVISION,
            pre_page_capture_tables,
            {
                "job_reprocess_runs": (
                    JOB_REPROCESS_RETRY_COLUMNS
                    | BACKGROUND_WORKER_OWNER_COLUMNS
                    | CAPTURE_CLAIM_COLUMNS
                )
            },
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            GENERATED_DOCUMENT_PROVIDER_REVISION,
            pre_company_profile_tables,
            {},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            JOB_STATUS_RECONCILIATION_REVISION,
            pre_company_profile_tables,
            {"generated_documents": GENERATED_DOCUMENT_PROVIDER_COLUMNS},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            JOB_OPPORTUNITY_STATUS_REVISION,
            pre_status_tables,
            {"job_postings": STATUS_RECONCILIATION_COLUMNS},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            JOB_INGESTION_ITEMS_REVISION,
            pre_status_tables,
            {"job_postings": OPPORTUNITY_STATUS_COLUMNS | STATUS_RECONCILIATION_COLUMNS},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            ACTIVE_JOB_POSTINGS_REVISION,
            pre_ingestion_item_tables,
            {"job_postings": OPPORTUNITY_STATUS_COLUMNS | STATUS_RECONCILIATION_COLUMNS},
            {ACTIVE_JOB_POSTINGS_VIEW},
        ),
        (
            LANGUAGE_FILTER_REVISION,
            pre_ingestion_item_tables,
            {"job_postings": OPPORTUNITY_STATUS_COLUMNS | STATUS_RECONCILIATION_COLUMNS},
            set(),
        ),
        (
            ROLE_INTEREST_REVISION,
            pre_ingestion_item_tables,
            {
                **LANGUAGE_FILTER_COLUMNS,
                "job_postings": OPPORTUNITY_STATUS_COLUMNS | STATUS_RECONCILIATION_COLUMNS,
            },
            set(),
        ),
        (
            DURATION_MONTHS_REVISION,
            pre_ingestion_item_tables,
            {
                **ROLE_AND_LANGUAGE_FILTER_COLUMNS,
                "job_postings": OPPORTUNITY_STATUS_COLUMNS | STATUS_RECONCILIATION_COLUMNS,
            },
            set(),
        ),
        (
            JOB_REPROCESS_RUN_TYPE_REVISION,
            pre_ingestion_item_tables,
            DURATION_MONTHS_ALLOWED_MISSING_COLUMNS,
            set(),
        ),
        (
            JOB_REPROCESS_REVISION,
            pre_ingestion_item_tables,
            JOB_REPROCESS_ALLOWED_MISSING_COLUMNS,
            set(),
        ),
        (
            EVIDENCE_SOURCE_ARTIFACT_REVISION,
            pre_reprocess_tables,
            EVIDENCE_SOURCE_ARTIFACT_ALLOWED_MISSING_COLUMNS,
            set(),
        ),
        (
            JOB_INGESTION_REVISION,
            pre_reprocess_tables,
            JOB_INGESTION_ALLOWED_MISSING_COLUMNS,
            set(),
        ),
        (BASELINE_REVISION, BASELINE_TABLES, JOB_INGESTION_ALLOWED_MISSING_COLUMNS, set()),
    ]
    for index in range(1, len(shapes)):
        revision, tables, allowed_missing_columns, views = shapes[index]
        allowed_missing_columns = {
            table_name: set(columns) for table_name, columns in allowed_missing_columns.items()
        }
        if "cover_letter_generation_runs" in tables:
            allowed_missing_columns["cover_letter_generation_runs"] = {"request_payload"}
        allowed_missing_columns["generated_documents"] = (
            allowed_missing_columns.get("generated_documents", set())
            | EDITABLE_COVER_LETTER_ARTIFACT_COLUMNS
        )
        allowed_missing_columns["job_postings"] = (
            allowed_missing_columns.get("job_postings", set()) | JOB_AVAILABILITY_COLUMNS
        )
        if "job_status_checks" in tables:
            allowed_missing_columns["job_status_checks"] = (
                allowed_missing_columns.get("job_status_checks", set())
                | STATUS_CHECK_DIAGNOSTIC_COLUMNS
            )
        if revision not in {
            current_revision,
            CAPTURE_EXECUTION_MODEL_REVISION,
            SOURCE_RECONCILIATION_REVISION,
            COMPANY_PROFILES_REVISION,
            GENERATED_DOCUMENT_PROVIDER_REVISION,
        }:
            allowed_missing_columns["generated_documents"] = (
                allowed_missing_columns.get("generated_documents", set())
                | GENERATED_DOCUMENT_PROVIDER_COLUMNS
            )
        if revision not in {current_revision, SOURCE_RECONCILIATION_REVISION, CAPTURE_EXECUTION_MODEL_REVISION}:
            allowed_missing_columns["job_postings"] = (
                allowed_missing_columns.get("job_postings", set()) | CAPTURE_EFFECT_COLUMNS
            )
        if (
            revision
            not in {
                current_revision,
                SOURCE_RECONCILIATION_REVISION,
                CAPTURE_EXECUTION_MODEL_REVISION,
            }
            and "job_reprocess_runs" in tables
        ):
            allowed_missing_columns["job_reprocess_runs"] = (
                allowed_missing_columns.get("job_reprocess_runs", set())
                | JOB_REPROCESS_RETRY_COLUMNS
                | BACKGROUND_WORKER_OWNER_COLUMNS
                | CAPTURE_CLAIM_COLUMNS
            )
        shapes[index] = (revision, tables, allowed_missing_columns, views)
    return shapes


def config_for_database(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    if not url.database or url.database == ":memory:":
        return None
    path = Path(url.database)
    return path if path.is_absolute() else Path.cwd() / path


def sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


def sqlite_views(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
    return {row[0] for row in rows}


def sqlite_alembic_versions(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return [row[0] for row in rows]


PROVISIONAL_CAPTURE_COLUMNS = {
    "capture_id",
    "capture_key",
    "source_url",
    "source_identity_url",
    "page_title",
    "target_job_id",
    "job_id",
    "raw_content_artifact_id",
    "source_archive_artifact_id",
    "content_hash",
    "deduplication_key",
    "skip_language_filter",
    "status",
    "attempts",
    "next_attempt_at",
    "started_at",
    "finished_at",
    "error_code",
    "error_message",
    "retryable",
    "worker_id",
    "claim_token",
    "result",
    "retry_of_capture_id",
    "created_at",
    "updated_at",
}
LEGACY_CAPTURE_IDENTITY_COLUMNS = {
    "job_source_identity_id",
    "job_id",
    "provider",
    "identity_hash",
    "public_source_url",
    "created_at",
    "updated_at",
}
FINAL_CAPTURE_IDENTITY_COLUMNS = LEGACY_CAPTURE_IDENTITY_COLUMNS | {
    "identity_strength",
    "source_kind",
    "match_priority",
}
PRE_STRENGTH_CAPTURE_IDENTITY_COLUMNS = FINAL_CAPTURE_IDENTITY_COLUMNS - {"identity_strength"}


_capture_revision_metadata: sa.MetaData | None = None


def capture_revision_0021_tables() -> dict[str, sa.Table]:
    """Reflect the frozen Alembic revision instead of copying its schema here."""
    global _capture_revision_metadata
    if _capture_revision_metadata is None:
        with TemporaryDirectory(prefix="jobcockpit-capture-schema-") as temp_dir:
            database_path = Path(temp_dir) / "capture-schema.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            command.upgrade(
                config_for_database(database_url),
                CAPTURE_EXECUTION_MODEL_REVISION,
            )
            engine = create_engine(database_url)
            try:
                metadata = sa.MetaData()
                metadata.reflect(bind=engine)
            finally:
                engine.dispose()
        _capture_revision_metadata = metadata
    names = PAGE_CAPTURE_TABLES | BACKGROUND_WORKER_TABLES
    return {name: _capture_revision_metadata.tables[name] for name in names}


def _sqlite_columns(path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            f"PRAGMA table_info({quote_sqlite_identifier(table_name)})"
        ).fetchall()
    return {row[1] for row in rows}


def _migration_datetime(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def _migration_json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _migration_active_job_id(connection, job_id: object) -> str | None:
    current = str(job_id or "").strip()
    if not current:
        return None
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        row = (
            connection.exec_driver_sql(
                "SELECT source FROM job_postings WHERE id = :job_id",
                {"job_id": current},
            )
            .mappings()
            .first()
        )
        if row is None:
            return current
        merged_into = str(_migration_json(row["source"]).get("merged_into_job_id") or "").strip()
        if not merged_into:
            return current
        current = merged_into
    raise RuntimeError(f"Merged job cycle detected while converting {current}")


def backup_sqlite_database(path: Path) -> Path:
    backup_root = path.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_root / (f"{path.stem}.pre-async-capture-{timestamp}{path.suffix or '.db'}")
    with sqlite3.connect(path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
    return backup_path


def maybe_upgrade_capture_identity_registry(database_url: str, path: Path) -> Path | None:
    columns = _sqlite_columns(path, "job_source_identities")
    if frozenset(columns) not in {
        frozenset(LEGACY_CAPTURE_IDENTITY_COLUMNS),
        frozenset(PRE_STRENGTH_CAPTURE_IDENTITY_COLUMNS),
        frozenset(FINAL_CAPTURE_IDENTITY_COLUMNS),
    }:
        raise RuntimeError(
            "Database has an unrecognized job_source_identities schema; "
            "refusing automatic conversion"
        )

    capture_columns = _sqlite_columns(path, "page_capture_runs")
    snapshot_columns = _sqlite_columns(path, "page_capture_snapshots")
    snapshot_scope_columns = _sqlite_columns(path, "page_capture_snapshot_scopes")
    head_columns = _sqlite_columns(path, "page_capture_subject_heads")
    job_columns = _sqlite_columns(path, "job_postings")
    reprocess_columns = _sqlite_columns(path, "job_reprocess_runs")
    lease_columns = _sqlite_columns(path, "background_worker_leases")
    needs_upgrade = (
        "evidence_seq" not in snapshot_columns
        or snapshot_scope_columns != FINAL_CAPTURE_ASSOCIATION_COLUMNS
        or "page_capture_effects" not in sqlite_tables(path)
        or head_columns != {"scope_hash", "snapshot_id", "created_at", "updated_at"}
        or "last_applied_capture_seq" not in job_columns
        or columns != FINAL_CAPTURE_IDENTITY_COLUMNS
        or "identity_strength" not in capture_columns
        or "supervisor_lease_token" not in capture_columns
        or not {
            "page_access_verdict",
            "page_kind",
            "identity_verdict",
            "association_verdict",
            "assessment_evidence_code",
        }.issubset(capture_columns)
        or "identity_accepted" in capture_columns
        or "supervisor_lease_token" not in reprocess_columns
        or "lease_token" not in lease_columns
    )
    if not needs_upgrade:
        return None

    backup_path = backup_sqlite_database(path)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                revision_tables = capture_revision_0021_tables()
                if "last_applied_capture_seq" not in job_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE job_postings ADD COLUMN "
                        "last_applied_capture_seq INTEGER NOT NULL DEFAULT 0"
                    )
                if "evidence_seq" not in snapshot_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_snapshots ADD COLUMN "
                        "evidence_seq INTEGER NOT NULL DEFAULT 0"
                    )
                    snapshot_rows = list(
                        connection.exec_driver_sql(
                            "SELECT snapshot_id FROM page_capture_snapshots "
                            "ORDER BY created_at, snapshot_id"
                        ).mappings()
                    )
                    for evidence_seq, snapshot_row in enumerate(snapshot_rows, start=1):
                        connection.exec_driver_sql(
                            "UPDATE page_capture_snapshots SET evidence_seq = :seq "
                            "WHERE snapshot_id = :snapshot_id",
                            {
                                "seq": evidence_seq,
                                "snapshot_id": str(snapshot_row["snapshot_id"]),
                            },
                        )
                    connection.exec_driver_sql(
                        "CREATE UNIQUE INDEX ix_page_capture_snapshots_evidence_seq "
                        "ON page_capture_snapshots (evidence_seq)"
                    )

                legacy_scope_table = None
                existing_tables = set(sa.inspect(connection).get_table_names())
                if "page_capture_run_scopes" in existing_tables:
                    legacy_scope_table = "page_capture_run_scopes"
                elif (
                    snapshot_scope_columns
                    and snapshot_scope_columns != FINAL_CAPTURE_ASSOCIATION_COLUMNS
                ):
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_snapshot_scopes "
                        "RENAME TO page_capture_snapshot_scopes_legacy"
                    )
                    legacy_scope_table = "page_capture_snapshot_scopes_legacy"
                if legacy_scope_table == "page_capture_run_scopes":
                    revision_tables["page_capture_snapshot_scopes"].create(connection)
                    connection.exec_driver_sql(
                        "INSERT INTO page_capture_snapshot_scopes "
                        "(capture_scope_id, snapshot_id, job_id, scope_hash, scope_kind, "
                        "association_basis, identity_strength, association_verdict, "
                        "created_at, updated_at) "
                        "SELECT 'capture_scope_' || lower(hex(randomblob(16))), "
                        "r.snapshot_id, "
                        "CASE WHEN s.scope_kind = 'job' "
                        "THEN COALESCE(r.target_job_id, r.job_id) ELSE NULL END, "
                        "s.scope_hash, s.scope_kind, "
                        "CASE WHEN s.scope_kind = 'job' "
                        "THEN 'explicit_target' ELSE 'source_identity' END, "
                        "'weak', 'asserted', MIN(s.created_at), MAX(s.updated_at) "
                        "FROM page_capture_run_scopes s "
                        "JOIN page_capture_runs r ON r.capture_id = s.capture_id "
                        "GROUP BY r.snapshot_id, s.scope_hash, s.scope_kind"
                    )
                    connection.exec_driver_sql("DROP TABLE page_capture_run_scopes")
                elif legacy_scope_table == "page_capture_snapshot_scopes_legacy":
                    revision_tables["page_capture_snapshot_scopes"].create(connection)
                    connection.exec_driver_sql(
                        "INSERT INTO page_capture_snapshot_scopes "
                        "(capture_scope_id, snapshot_id, job_id, scope_hash, scope_kind, "
                        "association_basis, identity_strength, association_verdict, "
                        "created_at, updated_at) "
                        "SELECT s.capture_scope_id, s.snapshot_id, "
                        "CASE WHEN s.scope_kind = 'job' THEN ("
                        "SELECT COALESCE(r.target_job_id, r.job_id) "
                        "FROM page_capture_runs r WHERE r.snapshot_id = s.snapshot_id "
                        "ORDER BY r.created_at LIMIT 1) ELSE NULL END, "
                        "s.scope_hash, s.scope_kind, "
                        "CASE WHEN s.scope_kind = 'job' "
                        "THEN 'explicit_target' ELSE 'source_identity' END, "
                        "'weak', 'asserted', s.created_at, s.updated_at "
                        "FROM page_capture_snapshot_scopes_legacy s"
                    )
                    connection.exec_driver_sql("DROP TABLE page_capture_snapshot_scopes_legacy")
                revision_tables["page_capture_effects"].create(
                    connection,
                    checkfirst=True,
                )

                if "capture_id" in head_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_subject_heads "
                        "RENAME TO page_capture_subject_heads_legacy"
                    )
                    revision_tables["page_capture_subject_heads"].create(connection)
                    connection.exec_driver_sql(
                        "INSERT INTO page_capture_subject_heads "
                        "(scope_hash, snapshot_id, created_at, updated_at) "
                        "SELECT h.scope_hash, r.snapshot_id, h.created_at, h.updated_at "
                        "FROM page_capture_subject_heads_legacy h "
                        "JOIN page_capture_runs r ON r.capture_id = h.capture_id"
                    )
                    connection.exec_driver_sql("DROP TABLE page_capture_subject_heads_legacy")

                connection.exec_driver_sql("DELETE FROM page_capture_subject_heads")
                connection.exec_driver_sql(
                    "INSERT INTO page_capture_subject_heads "
                    "(scope_hash, snapshot_id, created_at, updated_at) "
                    "SELECT s.scope_hash, s.snapshot_id, MIN(s.created_at), MAX(s.updated_at) "
                    "FROM page_capture_snapshot_scopes s "
                    "JOIN page_capture_snapshots p ON p.snapshot_id = s.snapshot_id "
                    "WHERE p.evidence_seq = ("
                    "SELECT MAX(p2.evidence_seq) "
                    "FROM page_capture_snapshot_scopes s2 "
                    "JOIN page_capture_snapshots p2 ON p2.snapshot_id = s2.snapshot_id "
                    "WHERE s2.scope_hash = s.scope_hash) "
                    "GROUP BY s.scope_hash, s.snapshot_id"
                )
                connection.exec_driver_sql(
                    "UPDATE page_capture_runs SET status = 'superseded', "
                    "next_attempt_at = NULL, finished_at = CURRENT_TIMESTAMP, "
                    "retryable = 0, worker_id = NULL, claim_token = NULL, "
                    "error_code = 'newer_capture_queued', "
                    "error_message = 'Superseded by newer captured evidence' "
                    "WHERE status IN ('queued', 'running', 'retry_scheduled') "
                    "AND EXISTS ("
                    "SELECT 1 FROM page_capture_snapshot_scopes s "
                    "JOIN page_capture_subject_heads h ON h.scope_hash = s.scope_hash "
                    "WHERE s.snapshot_id = page_capture_runs.snapshot_id "
                    "AND h.snapshot_id <> page_capture_runs.snapshot_id)"
                )

                if columns == LEGACY_CAPTURE_IDENTITY_COLUMNS:
                    old_rows = list(
                        connection.exec_driver_sql(
                            "SELECT * FROM job_source_identities "
                            "ORDER BY created_at, job_source_identity_id"
                        ).mappings()
                    )
                    index_rows = list(
                        connection.exec_driver_sql(
                            "PRAGMA index_list(job_source_identities)"
                        ).mappings()
                    )
                    for index_row in index_rows:
                        if str(index_row["origin"]) == "c":
                            connection.exec_driver_sql(
                                "DROP INDEX IF EXISTS "
                                + quote_sqlite_identifier(str(index_row["name"]))
                            )
                    connection.exec_driver_sql(
                        "ALTER TABLE job_source_identities RENAME TO job_source_identities_legacy"
                    )
                    identities = capture_revision_0021_tables()["job_source_identities"]
                    identities.create(connection)
                    for row in old_rows:
                        created_at = _migration_datetime(row["created_at"], datetime.now(UTC))
                        updated_at = _migration_datetime(row["updated_at"], created_at)
                        connection.execute(
                            identities.insert(),
                            {
                                **dict(row),
                                "identity_strength": "weak",
                                "source_kind": "source_url",
                                "match_priority": 100,
                                "created_at": created_at,
                                "updated_at": updated_at,
                            },
                        )
                    connection.exec_driver_sql("DROP TABLE job_source_identities_legacy")
                elif "identity_strength" not in columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE job_source_identities ADD COLUMN "
                        "identity_strength VARCHAR(32) NOT NULL DEFAULT 'weak'"
                    )
                    connection.exec_driver_sql(
                        "UPDATE job_source_identities SET identity_strength = 'weak'"
                    )
                if "identity_strength" not in capture_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_runs ADD COLUMN "
                        "identity_strength VARCHAR(32) NOT NULL DEFAULT 'weak'"
                    )
                    connection.exec_driver_sql(
                        "UPDATE page_capture_runs SET identity_strength = 'weak'"
                    )
                accepted_expression = (
                    "identity_accepted = 1 OR " if "identity_accepted" in capture_columns else ""
                ) + (
                    "(status IN ('succeeded', 'skipped') AND "
                    "json_extract(result, '$.status') IN "
                    "('created', 'updated', 'terminal_status_updated', "
                    "'terminal_status_already_recorded'))"
                )
                assessment_columns = {
                    "page_access_verdict": "VARCHAR(32) NOT NULL DEFAULT 'unassessed'",
                    "page_kind": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
                    "identity_verdict": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
                    "association_verdict": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
                    "assessment_evidence_code": "VARCHAR(128)",
                }
                for column_name, definition in assessment_columns.items():
                    if column_name not in capture_columns:
                        connection.exec_driver_sql(
                            f"ALTER TABLE page_capture_runs ADD COLUMN {column_name} {definition}"
                        )
                connection.exec_driver_sql(
                    "UPDATE page_capture_runs SET identity_verdict = "
                    f"CASE WHEN {accepted_expression} THEN 'accepted' "
                    "WHEN status IN ('failed', 'skipped', 'superseded') "
                    "THEN 'rejected' ELSE 'pending' END"
                )
                connection.exec_driver_sql(
                    "UPDATE page_capture_runs SET association_verdict = "
                    "CASE WHEN identity_verdict = 'accepted' AND job_id IS NOT NULL "
                    "THEN 'verified' WHEN status = 'skipped' AND job_id IS NULL "
                    "THEN 'unmatched' ELSE 'pending' END"
                )
                connection.exec_driver_sql(
                    "UPDATE job_postings SET last_applied_capture_seq = COALESCE(("
                    "SELECT MAX(p.evidence_seq) FROM page_capture_runs r "
                    "JOIN page_capture_snapshots p ON p.snapshot_id = r.snapshot_id "
                    "WHERE r.job_id = job_postings.id "
                    "AND r.status IN ('succeeded', 'skipped') "
                    "AND json_extract(r.result, '$.status') IN "
                    "('created', 'updated', 'terminal_status_updated', "
                    "'terminal_status_already_recorded')), 0)"
                )
                if "identity_accepted" in capture_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_runs DROP COLUMN identity_accepted"
                    )
                if "supervisor_lease_token" not in capture_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE page_capture_runs ADD COLUMN "
                        "supervisor_lease_token VARCHAR(64)"
                    )
                if "supervisor_lease_token" not in reprocess_columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE job_reprocess_runs ADD COLUMN "
                        "supervisor_lease_token VARCHAR(64)"
                    )
                if "lease_token" not in lease_columns:
                    connection.exec_driver_sql("DELETE FROM background_worker_leases")
                    connection.exec_driver_sql(
                        "ALTER TABLE background_worker_leases ADD COLUMN lease_token VARCHAR(64)"
                    )
                    connection.exec_driver_sql(
                        "CREATE UNIQUE INDEX uq_background_worker_leases_lease_token "
                        "ON background_worker_leases (lease_token)"
                    )
                for statement in (
                    "CREATE INDEX IF NOT EXISTS ix_job_source_identities_identity_strength "
                    "ON job_source_identities (identity_strength)",
                    "CREATE INDEX IF NOT EXISTS ix_page_capture_runs_identity_strength "
                    "ON page_capture_runs (identity_strength)",
                    "CREATE INDEX IF NOT EXISTS ix_page_capture_runs_supervisor_lease_token "
                    "ON page_capture_runs (supervisor_lease_token)",
                    "CREATE INDEX IF NOT EXISTS ix_job_reprocess_runs_supervisor_lease_token "
                    "ON job_reprocess_runs (supervisor_lease_token)",
                ):
                    connection.exec_driver_sql(statement)
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    except Exception as error:
        raise RuntimeError(
            "Failed to upgrade the capture identity registry. "
            f"A recovery backup is available at {backup_path}"
        ) from error
    finally:
        engine.dispose()

    revision_tables = capture_revision_0021_tables()
    mismatches = sqlite_schema_mismatches(
        path,
        {
            "job_reprocess_runs",
            "page_capture_snapshots",
            "page_capture_runs",
            "page_capture_snapshot_scopes",
            "page_capture_effects",
            "page_capture_subject_heads",
            "job_source_identities",
            "background_worker_leases",
        },
        expected_metadata=revision_tables["page_capture_runs"].metadata,
    )
    if mismatches:
        raise schema_mismatch_error(CAPTURE_EXECUTION_MODEL_REVISION, mismatches)
    print(f"Upgraded the async-capture identity registry; backup: {backup_path}")
    return backup_path


def maybe_convert_provisional_capture_schema(database_url: str) -> Path | None:
    path = sqlite_path(database_url)
    if path is None or not path.exists():
        return None
    tables = sqlite_tables(path)
    if "alembic_version" not in tables:
        return None
    versions = sqlite_alembic_versions(path)
    if versions != [CAPTURE_EXECUTION_MODEL_REVISION]:
        return None
    if "page_capture_snapshots" in tables:
        if "job_source_identities" not in tables:
            raise RuntimeError(
                "Database is stamped 20260803_0021 but has no job_source_identities table"
            )
        return maybe_upgrade_capture_identity_registry(database_url, path)
    if "page_capture_runs" not in tables:
        raise RuntimeError("Database is stamped 20260803_0021 but has no page_capture_runs table")
    existing_columns = _sqlite_columns(path, "page_capture_runs")
    if existing_columns != PROVISIONAL_CAPTURE_COLUMNS:
        raise RuntimeError(
            "Database is stamped 20260803_0021 but does not match the known "
            "provisional capture schema; refusing automatic conversion"
        )

    backup_path = backup_sqlite_database(path)
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    final_table_names = (
        "page_capture_snapshots",
        "page_capture_runs",
        "page_capture_snapshot_scopes",
        "page_capture_subject_heads",
        "page_capture_effects",
        "job_source_identities",
    )
    capture_tables = capture_revision_0021_tables()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                old_rows = list(
                    connection.exec_driver_sql(
                        "SELECT * FROM page_capture_runs ORDER BY created_at, capture_id"
                    ).mappings()
                )
                index_rows = list(
                    connection.exec_driver_sql("PRAGMA index_list(page_capture_runs)").mappings()
                )
                for index_row in index_rows:
                    if str(index_row["origin"]) == "c":
                        connection.exec_driver_sql(
                            f"DROP INDEX IF EXISTS "
                            f"{quote_sqlite_identifier(str(index_row['name']))}"
                        )
                connection.exec_driver_sql(
                    "ALTER TABLE page_capture_runs RENAME TO page_capture_runs_provisional"
                )
                if "page_capture_effects" in tables:
                    connection.exec_driver_sql("DROP TABLE page_capture_effects")
                for table_name in final_table_names:
                    capture_tables[table_name].create(connection)

                snapshots = capture_tables["page_capture_snapshots"]
                runs = capture_tables["page_capture_runs"]
                scopes = capture_tables["page_capture_snapshot_scopes"]
                heads = capture_tables["page_capture_subject_heads"]
                effects = capture_tables["page_capture_effects"]
                identities = capture_tables["job_source_identities"]
                if "last_applied_capture_seq" not in _sqlite_columns(path, "job_postings"):
                    connection.exec_driver_sql(
                        "ALTER TABLE job_postings ADD COLUMN "
                        "last_applied_capture_seq INTEGER NOT NULL DEFAULT 0"
                    )

                rows_by_id = {str(row["capture_id"]): row for row in old_rows}
                root_by_capture: dict[str, str] = {}

                def capture_root_id(capture_id: str) -> str:
                    if capture_id in root_by_capture:
                        return root_by_capture[capture_id]
                    chain: list[str] = []
                    current = capture_id
                    while True:
                        if current in chain:
                            raise RuntimeError(
                                f"Provisional capture retry cycle includes {current}"
                            )
                        chain.append(current)
                        row = rows_by_id.get(current)
                        if row is None:
                            raise RuntimeError(
                                f"Provisional capture retry ancestor {current} is missing"
                            )
                        parent = str(row["retry_of_capture_id"] or "").strip()
                        if not parent:
                            root = current
                            break
                        current = parent
                    for member in chain:
                        root_by_capture[member] = root
                    return root

                for capture_id in rows_by_id:
                    capture_root_id(capture_id)

                grouped_rows: dict[str, list] = {}
                for capture_id, row in rows_by_id.items():
                    grouped_rows.setdefault(root_by_capture[capture_id], []).append(row)

                ordered_roots = sorted(
                    grouped_rows,
                    key=lambda root: (
                        _migration_datetime(rows_by_id[root]["created_at"], now),
                        root,
                    ),
                )
                snapshot_by_capture: dict[str, str] = {}
                snapshot_scopes: dict[str, list[tuple[str, str]]] = {}
                evidence_seq_by_snapshot: dict[str, int] = {}
                snapshot_job_ids: dict[str, set[str]] = {}
                job_id_by_scope_hash: dict[str, str] = {}
                identity_by_snapshot: dict[str, object] = {}

                for evidence_seq, root_id in enumerate(ordered_roots, start=1):
                    root_row = rows_by_id[root_id]
                    immutable_keys = (
                        "source_url",
                        "page_title",
                        "raw_content_artifact_id",
                        "source_archive_artifact_id",
                        "content_hash",
                    )
                    for member in grouped_rows[root_id]:
                        if any(member[key] != root_row[key] for key in immutable_keys):
                            raise RuntimeError(
                                f"Retry {member['capture_id']} changed immutable capture evidence"
                            )
                    identity = capture_source_identity(str(root_row["source_url"]))
                    if identity is None:
                        raise RuntimeError(
                            f"Provisional capture {root_id} has an invalid source URL"
                        )
                    canonical_targets = {
                        target_id
                        for member in grouped_rows[root_id]
                        if (
                            target_id := _migration_active_job_id(
                                connection, member["target_job_id"]
                            )
                        )
                    }
                    if len(canonical_targets) > 1:
                        raise RuntimeError(f"Retry chain {root_id} targets multiple active jobs")
                    target_job_id = next(iter(canonical_targets), None)
                    associated_job_ids = set(canonical_targets)
                    associated_job_ids.update(
                        canonical_job_id
                        for member in grouped_rows[root_id]
                        if (
                            canonical_job_id := _migration_active_job_id(
                                connection, member["job_id"]
                            )
                        )
                    )

                    snapshot_id = (
                        "capture_snapshot_" + sha256(root_id.encode("utf-8")).hexdigest()[:32]
                    )
                    created_at = _migration_datetime(root_row["created_at"], now)
                    updated_at = max(
                        _migration_datetime(member["updated_at"], created_at)
                        for member in grouped_rows[root_id]
                    )
                    connection.execute(
                        snapshots.insert(),
                        {
                            "snapshot_id": snapshot_id,
                            "source_url": str(root_row["source_url"]),
                            "page_title": str(root_row["page_title"] or ""),
                            "raw_content_artifact_id": str(root_row["raw_content_artifact_id"]),
                            "source_archive_artifact_id": str(
                                root_row["source_archive_artifact_id"]
                            ),
                            "content_hash": str(root_row["content_hash"]),
                            "evidence_seq": evidence_seq,
                            "created_at": created_at,
                            "updated_at": updated_at,
                        },
                    )
                    source_scope = sha256(
                        f"source\0{identity.identity_hash}".encode("utf-8")
                    ).hexdigest()
                    capture_scopes = [("source", source_scope)]
                    for associated_job_id in sorted(associated_job_ids):
                        job_scope = sha256(f"job\0{associated_job_id}".encode("utf-8")).hexdigest()
                        capture_scopes.append(("job", job_scope))
                        job_id_by_scope_hash[job_scope] = associated_job_id
                    snapshot_scopes[snapshot_id] = capture_scopes
                    snapshot_job_ids[snapshot_id] = associated_job_ids
                    evidence_seq_by_snapshot[snapshot_id] = evidence_seq
                    identity_by_snapshot[snapshot_id] = identity
                    for member in grouped_rows[root_id]:
                        snapshot_by_capture[str(member["capture_id"])] = snapshot_id
                    for kind, scope_hash in capture_scopes:
                        connection.execute(
                            scopes.insert(),
                            {
                                "capture_scope_id": (
                                    "capture_scope_"
                                    + sha256(
                                        f"{snapshot_id}\0{scope_hash}".encode("utf-8")
                                    ).hexdigest()[:32]
                                ),
                                "snapshot_id": snapshot_id,
                                "scope_hash": scope_hash,
                                "job_id": job_id_by_scope_hash.get(scope_hash),
                                "association_basis": (
                                    "explicit_target"
                                    if job_id_by_scope_hash.get(scope_hash) == target_job_id
                                    else "run_result"
                                    if kind == "job"
                                    else "source_identity"
                                ),
                                "identity_strength": identity.strength,
                                "association_verdict": "asserted",
                                "scope_kind": kind,
                                "created_at": created_at,
                                "updated_at": updated_at,
                            },
                        )

                for row in old_rows:
                    capture_id = str(row["capture_id"])
                    snapshot_id = snapshot_by_capture[capture_id]
                    identity = identity_by_snapshot[snapshot_id]
                    created_at = _migration_datetime(row["created_at"], now)
                    updated_at = _migration_datetime(row["updated_at"], created_at)
                    status = str(row["status"])
                    attempts = int(row["attempts"] or 0)
                    error_code = row["error_code"]
                    error_message = row["error_message"]
                    retryable = bool(row["retryable"])
                    next_attempt_at = (
                        _migration_datetime(row["next_attempt_at"], now)
                        if row["next_attempt_at"]
                        else None
                    )
                    worker_id = row["worker_id"]
                    claim_token = row["claim_token"]
                    result = _migration_json(row["result"])
                    recovered_running_to_terminal = False
                    if status == "running":
                        if attempts >= 3:
                            status = "failed"
                            recovered_running_to_terminal = True
                            retryable = True
                            next_attempt_at = None
                        else:
                            status = "retry_scheduled"
                            retryable = True
                            next_attempt_at = now
                        error_code = "page_capture_worker_stale"
                        error_message = "Capture worker ownership expired"
                        worker_id = None
                        claim_token = None
                    canonical_target_job_id = _migration_active_job_id(
                        connection, row["target_job_id"]
                    )
                    canonical_job_id = _migration_active_job_id(connection, row["job_id"])
                    accepted = status in {"succeeded", "skipped"} and str(
                        result.get("status") or ""
                    ) in {
                        "created",
                        "updated",
                        "terminal_status_updated",
                        "terminal_status_already_recorded",
                    }
                    connection.execute(
                        runs.insert(),
                        {
                            "capture_id": capture_id,
                            "snapshot_id": snapshot_id,
                            "provider": identity.provider,
                            "identity_hash": identity.identity_hash,
                            "identity_strength": identity.strength,
                            "public_source_url": identity.public_source_url,
                            "target_job_id": canonical_target_job_id,
                            "job_id": canonical_job_id,
                            "skip_language_filter": bool(row["skip_language_filter"]),
                            "page_access_verdict": "unassessed",
                            "page_kind": "unknown",
                            "identity_verdict": "accepted" if accepted else "pending",
                            "association_verdict": (
                                "verified" if canonical_job_id and accepted else "pending"
                            ),
                            "assessment_evidence_code": None,
                            "status": status,
                            "attempts": attempts,
                            "next_attempt_at": next_attempt_at,
                            "started_at": (
                                _migration_datetime(row["started_at"], now)
                                if row["started_at"]
                                else None
                            ),
                            "finished_at": (
                                now
                                if recovered_running_to_terminal
                                else _migration_datetime(row["finished_at"], now)
                                if row["finished_at"]
                                else None
                            ),
                            "error_code": error_code,
                            "error_message": error_message,
                            "retryable": retryable,
                            "worker_id": worker_id,
                            "claim_token": claim_token,
                            "supervisor_lease_token": None,
                            "result": result,
                            "retry_of_capture_id": row["retry_of_capture_id"],
                            "created_at": created_at,
                            "updated_at": updated_at,
                        },
                    )

                    for affected_job_id in sorted(snapshot_job_ids[snapshot_id]):
                        connection.execute(
                            effects.insert(),
                            {
                                "effect_id": (
                                    "capture_effect_"
                                    + sha256(
                                        f"{capture_id}\0{affected_job_id}".encode("utf-8")
                                    ).hexdigest()[:32]
                                ),
                                "capture_id": capture_id,
                                "snapshot_id": snapshot_id,
                                "job_id": affected_job_id,
                                "effect_kind": "processing",
                                "status": status,
                                "outcome_code": str(result.get("status") or error_code or status),
                                "error_code": error_code,
                                "error_message": error_message,
                                "result": result,
                                "applied_at": (updated_at if status == "succeeded" else None),
                                "created_at": created_at,
                                "updated_at": updated_at,
                            },
                        )

                head_snapshot_by_scope: dict[str, str] = {}
                for snapshot_id, capture_scopes in snapshot_scopes.items():
                    for _kind, scope_hash in capture_scopes:
                        current = head_snapshot_by_scope.get(scope_hash)
                        if current is None or (
                            evidence_seq_by_snapshot[snapshot_id]
                            > evidence_seq_by_snapshot[current]
                        ):
                            head_snapshot_by_scope[scope_hash] = snapshot_id
                for scope_hash, snapshot_id in head_snapshot_by_scope.items():
                    connection.execute(
                        heads.insert(),
                        {
                            "scope_hash": scope_hash,
                            "snapshot_id": snapshot_id,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )

                active_rows = list(
                    connection.execute(
                        runs.select()
                        .where(runs.c.status.in_(("queued", "running", "retry_scheduled")))
                        .order_by(runs.c.created_at.desc(), runs.c.capture_id.desc())
                    ).mappings()
                )
                active_snapshot_ids: set[str] = set()
                for row in active_rows:
                    snapshot_id = str(row["snapshot_id"])
                    is_current = all(
                        head_snapshot_by_scope.get(scope_hash) == snapshot_id
                        for _kind, scope_hash in snapshot_scopes[snapshot_id]
                    )
                    duplicate_active = snapshot_id in active_snapshot_ids
                    if is_current and not duplicate_active:
                        active_snapshot_ids.add(snapshot_id)
                        continue
                    connection.execute(
                        runs.update()
                        .where(runs.c.capture_id == row["capture_id"])
                        .values(
                            status="superseded",
                            next_attempt_at=None,
                            finished_at=now,
                            retryable=False,
                            worker_id=None,
                            claim_token=None,
                            error_code="newer_capture_queued",
                            error_message="Superseded by newer captured evidence",
                            updated_at=now,
                        )
                    )

                applied_rows = connection.execute(
                    sa.select(
                        runs.c.job_id,
                        sa.func.max(snapshots.c.evidence_seq),
                    )
                    .join(snapshots, snapshots.c.snapshot_id == runs.c.snapshot_id)
                    .where(
                        runs.c.job_id.is_not(None),
                        runs.c.identity_verdict == "accepted",
                    )
                    .group_by(runs.c.job_id)
                ).all()
                for job_id, evidence_seq in applied_rows:
                    connection.exec_driver_sql(
                        "UPDATE job_postings SET last_applied_capture_seq = :seq "
                        "WHERE id = :job_id",
                        {"seq": int(evidence_seq), "job_id": str(job_id)},
                    )

                job_rows = connection.exec_driver_sql(
                    "SELECT id, source_url, created_at, updated_at "
                    "FROM job_postings WHERE source_url IS NOT NULL "
                    "ORDER BY created_at, id"
                ).mappings()
                for job_row in job_rows:
                    identity = capture_source_identity(str(job_row["source_url"]))
                    if identity is None:
                        continue
                    connection.execute(
                        identities.insert(),
                        {
                            "job_source_identity_id": (
                                "job_identity_"
                                + sha256(
                                    (
                                        f"{job_row['id']}\0"
                                        f"{identity.provider}\0"
                                        f"{identity.identity_hash}"
                                    ).encode("utf-8")
                                ).hexdigest()[:32]
                            ),
                            "job_id": str(job_row["id"]),
                            "provider": identity.provider,
                            "identity_hash": identity.identity_hash,
                            "identity_strength": identity.strength,
                            "public_source_url": identity.public_source_url,
                            "source_kind": "source_url",
                            "match_priority": 100,
                            "created_at": _migration_datetime(job_row["created_at"], now),
                            "updated_at": _migration_datetime(job_row["updated_at"], now),
                        },
                    )
                connection.exec_driver_sql("DROP TABLE page_capture_runs_provisional")
                if "background_worker_leases" in tables:
                    connection.exec_driver_sql("DELETE FROM background_worker_leases")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
    except Exception as error:
        raise RuntimeError(
            "Failed to convert provisional async-capture schema. "
            f"A recovery backup is available at {backup_path}"
        ) from error
    finally:
        engine.dispose()

    mismatches = sqlite_schema_mismatches(
        path,
        {
            "job_reprocess_runs",
            "page_capture_snapshots",
            "page_capture_runs",
            "page_capture_snapshot_scopes",
            "page_capture_effects",
            "page_capture_subject_heads",
            "job_source_identities",
            "background_worker_leases",
        },
        expected_metadata=capture_tables["page_capture_runs"].metadata,
    )
    if mismatches:
        raise schema_mismatch_error(CAPTURE_EXECUTION_MODEL_REVISION, mismatches)
    print(
        f"Converted provisional async-capture schema to the final 0021 model; backup: {backup_path}"
    )
    return backup_path


def quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def normalize_sql_predicate(value: object) -> str:
    normalized = " ".join(str(value).casefold().split())
    normalized = normalized.replace('"', "").replace("`", "").replace("[", "").replace("]", "")
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized


def sqlite_schema_mismatches(
    path: Path,
    table_names: set[str] | None = None,
    allowed_missing_columns: dict[str, set[str]] | None = None,
    *,
    expected_metadata: sa.MetaData | None = None,
) -> list[str]:
    allowed_missing_columns = allowed_missing_columns or {}
    mismatches: list[str] = []
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        metadata = expected_metadata or Base.metadata
        for table_name, table in metadata.tables.items():
            if table_names is not None and table_name not in table_names:
                continue
            quoted_table = quote_sqlite_identifier(table_name)
            column_rows = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            existing_columns = {row["name"] for row in column_rows}
            expected_columns = {column.name for column in table.columns}
            allowed_missing = allowed_missing_columns.get(table_name, set())
            missing_columns = (expected_columns - existing_columns) - allowed_missing
            extra_columns = existing_columns - expected_columns
            if missing_columns or extra_columns:
                missing = sorted(missing_columns)
                extra = sorted(existing_columns - expected_columns)
                details = []
                if missing:
                    details.append(f"missing columns: {', '.join(missing)}")
                if extra:
                    details.append(f"extra columns: {', '.join(extra)}")
                mismatches.append(f"{table_name} ({'; '.join(details)})")
                continue

            comparable_columns = expected_columns - allowed_missing
            expected_pk = tuple(
                column.name
                for column in table.primary_key.columns
                if column.name in comparable_columns
            )
            existing_pk = tuple(
                row["name"] for row in sorted(column_rows, key=lambda row: row["pk"]) if row["pk"]
            )
            if existing_pk != expected_pk:
                mismatches.append(
                    f"{table_name} primary key mismatch: expected {expected_pk}, found {existing_pk}"
                )

            fk_rows = connection.execute(f"PRAGMA foreign_key_list({quoted_table})").fetchall()
            existing_fks = {(row["from"], row["table"], row["to"]) for row in fk_rows}
            expected_fks = {
                (foreign_key.parent.name, foreign_key.column.table.name, foreign_key.column.name)
                for foreign_key in table.foreign_keys
                if foreign_key.parent.name in comparable_columns
            }
            if existing_fks != expected_fks:
                mismatches.append(
                    f"{table_name} foreign key mismatch: expected {expected_fks}, found {existing_fks}"
                )

            index_rows = connection.execute(f"PRAGMA index_list({quoted_table})").fetchall()
            index_rows_by_name = {row["name"]: row for row in index_rows}
            for index in table.indexes:
                if (index.name == "ix_job_reprocess_runs_job_created"
                    and ("request_payload" in allowed_missing_columns.get("cover_letter_generation_runs", set())
                         or table_names is not None and "cover_letter_generation_runs" not in table_names)):
                    continue
                if not index.name:
                    continue
                expected_index_columns = tuple(
                    column.name for column in index.columns if column.name in comparable_columns
                )
                if not expected_index_columns or any(
                    column.name not in comparable_columns for column in index.columns
                ):
                    continue
                if index.name not in index_rows_by_name:
                    mismatches.append(f"{table_name} missing index: {index.name}")
                    continue
                quoted_index = quote_sqlite_identifier(index.name)
                index_info_rows = connection.execute(
                    f"PRAGMA index_info({quoted_index})"
                ).fetchall()
                existing_index_columns = tuple(row["name"] for row in index_info_rows)
                if existing_index_columns != expected_index_columns:
                    mismatches.append(
                        f"{table_name} index {index.name} column mismatch: "
                        f"expected {expected_index_columns}, found {existing_index_columns}"
                    )
                expected_where = index.dialect_options["sqlite"].get("where")
                if expected_where is not None:
                    sql_row = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (index.name,),
                    ).fetchone()
                    index_sql = normalize_sql_predicate(sql_row["sql"] if sql_row else "")
                    actual_where = (
                        index_sql.partition(" where ")[2] if " where " in index_sql else ""
                    )
                    expected_where_text = normalize_sql_predicate(expected_where)
                    accepts_pre_capture_predicate = (
                        table_name == "job_reprocess_runs"
                        and index.name == "uq_job_reprocess_runs_active_job"
                        and JOB_REPROCESS_RETRY_COLUMNS <= allowed_missing
                        and actual_where
                        == normalize_sql_predicate("status IN ('queued', 'running')")
                    )
                    if actual_where != expected_where_text and not accepts_pre_capture_predicate:
                        mismatches.append(
                            f"{table_name} index {index.name} WHERE predicate mismatch: "
                            f"expected {expected_where_text!r}, found {actual_where!r}"
                        )

            expected_unique_sets = {
                tuple(column.name for column in constraint.columns)
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
                and all(column.name in comparable_columns for column in constraint.columns)
            }
            expected_unique_sets.update(
                tuple(column.name for column in index.columns)
                for index in table.indexes
                if index.unique
                and all(column.name in comparable_columns for column in index.columns)
            )
            existing_unique_sets = set()
            for row in index_rows:
                if not row["unique"] or row["origin"] == "pk":
                    continue
                quoted_index = quote_sqlite_identifier(row["name"])
                index_info_rows = connection.execute(
                    f"PRAGMA index_info({quoted_index})"
                ).fetchall()
                existing_unique_sets.add(tuple(index_row["name"] for index_row in index_info_rows))
            if existing_unique_sets != expected_unique_sets:
                mismatches.append(
                    f"{table_name} unique constraint mismatch: "
                    f"expected {expected_unique_sets}, found {existing_unique_sets}"
                )

    return mismatches


def schema_mismatch_error(revision_label: str, mismatches: list[str]) -> RuntimeError:
    preview = "; ".join(mismatches[:5])
    if len(mismatches) > 5:
        preview += f"; ... {len(mismatches) - 5} more"
    return RuntimeError(
        "Existing SQLite database has JobCockpit tables but does not match "
        f"the {revision_label} schema, so it will not be stamped: {preview}"
    )


def maybe_stamp_existing_sqlite_schema(database_url: str, config: Config) -> None:
    path = sqlite_path(database_url)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return

    tables = sqlite_tables(path)
    views = sqlite_views(path)
    if "alembic_version" in tables and sqlite_alembic_versions(path):
        return
    if not tables.intersection(BASELINE_TABLES):
        return
    if not BASELINE_TABLES <= tables:
        missing = ", ".join(sorted(BASELINE_TABLES - tables))
        raise RuntimeError(
            "Existing SQLite database contains JobCockpit tables but is missing "
            f"Alembic version state and baseline tables: {missing}"
        )

    checked_mismatches: list[tuple[str, list[str]]] = []
    current_tables = set(Base.metadata.tables)
    for (
        revision,
        required_tables,
        allowed_missing_columns,
        required_views,
    ) in recoverable_sqlite_schema_shapes(config):
        if not required_tables <= tables:
            continue
        if not required_views <= views:
            continue
        newer_known_tables = current_tables - required_tables
        if newer_known_tables & tables:
            continue
        mismatches = sqlite_schema_mismatches(path, required_tables, allowed_missing_columns)
        if not mismatches:
            print(
                f"Detected existing JobCockpit SQLite schema without Alembic state; stamping {revision}."
            )
            command.stamp(config, revision)
            return
        checked_mismatches.append((revision, mismatches))

    if checked_mismatches:
        revision, mismatches = min(checked_mismatches, key=lambda item: len(item[1]))
        raise schema_mismatch_error(revision, mismatches)
    missing = ", ".join(sorted(BASELINE_TABLES - tables))
    raise RuntimeError(
        "Existing SQLite database contains JobCockpit tables but is missing "
        f"Alembic version state and baseline tables: {missing}"
    )


def run_migrations() -> None:
    settings = Settings()
    config = config_for_database(settings.database_url)
    maybe_convert_provisional_capture_schema(settings.database_url)
    maybe_stamp_existing_sqlite_schema(settings.database_url, config)
    command.upgrade(config, "head")


def main() -> int:
    run_migrations()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
