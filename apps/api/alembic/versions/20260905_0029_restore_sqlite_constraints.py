"""Restore constraints omitted by legacy local SQLite table rebuilds.

Definitions are frozen here rather than imported from evolving ORM metadata.
Healthy installations already have these constraints and need no rebuild.
"""

from alembic import context, op
import sqlalchemy as sa

revision = "20260905_0029"
down_revision = "20260905_0028"
branch_labels = None
depends_on = None

FOREIGN_KEYS = {
    "application_package_documents": [
        ("generated_document_id", "generated_documents", "document_id")
    ],
    "generated_documents": [
        ("truthfulness_check_id", "truthfulness_checks", "truthfulness_check_id")
    ],
    "truthfulness_checks": [
        ("application_package_id", "application_packages", "application_package_id")
    ],
    "job_alert_emails": [
        ("email_ingestion_run_id", "email_ingestion_runs", "email_ingestion_run_id")
    ],
    "job_alert_email_processing_states": [
        ("job_alert_source_id", "job_alert_sources", "job_alert_source_id"),
        ("last_ingestion_run_id", "email_ingestion_runs", "email_ingestion_run_id"),
    ],
    "job_alert_links": [
        ("job_alert_email_id", "job_alert_emails", "job_alert_email_id"),
        ("linked_job_id", "job_postings", "id"),
    ],
}
UNIQUES = {
    "job_postings": ("uq_job_postings_posting_snapshot_hash", ("posting_snapshot_hash",)),
    "applications": ("uq_applications_candidate_id_job_id", ("candidate_id", "job_id")),
    "job_alert_email_processing_states": (
        "uq_email_processing_state_source_message",
        ("job_alert_source_id", "provider_message_id_hash"),
    ),
}
CAPTURE_INDEX = "ix_job_postings_last_applied_capture_seq"


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("SQLite constraint repair requires an online schema inspection")
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    # SQLite's legacy driver does not begin a transaction for DDL. Explicitly
    # protect preflight, table replacement, views, and Alembic version together.
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("SQLite constraint repair stopped: existing foreign-key violations")
    for table, specs in FOREIGN_KEYS.items():
        for column, target, target_column in specs:
            orphan = bind.exec_driver_sql(
                f"SELECT 1 FROM {_quote(table)} a LEFT JOIN {_quote(target)} b "
                f"ON a.{_quote(column)} = b.{_quote(target_column)} "
                f"WHERE a.{_quote(column)} IS NOT NULL AND b.{_quote(target_column)} IS NULL LIMIT 1"
            ).first()
            if orphan:
                raise RuntimeError(
                    f"SQLite constraint repair stopped: orphan references in {table}.{column}"
                )
    for table, (_, columns) in UNIQUES.items():
        names = ", ".join(_quote(column) for column in columns)
        nonnull = " AND ".join(f"{_quote(column)} IS NOT NULL" for column in columns)
        if bind.exec_driver_sql(
            f"SELECT 1 FROM {_quote(table)} WHERE {nonnull} GROUP BY {names} HAVING COUNT(*) > 1 LIMIT 1"
        ).first():
            raise RuntimeError(f"SQLite constraint repair stopped: duplicate keys in {table}")

    inspector = sa.inspect(bind)
    repairs = {}
    for table in sorted(set(FOREIGN_KEYS) | set(UNIQUES)):
        existing_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table)
        }
        missing_fks = [
            spec
            for spec in FOREIGN_KEYS.get(table, [])
            if ((spec[0],), spec[1], (spec[2],)) not in existing_fks
        ]
        unique = UNIQUES.get(table)
        existing_uniques = {
            tuple(item["column_names"]) for item in inspector.get_unique_constraints(table)
        }
        existing_uniques.update(
            tuple(item["column_names"])
            for item in inspector.get_indexes(table)
            if item["unique"] and item.get("dialect_options", {}).get("sqlite_where") is None
        )
        missing_unique = unique if unique and unique[1] not in existing_uniques else None
        if missing_fks or missing_unique:
            repairs[table] = (missing_fks, missing_unique)

    if repairs:
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
            raise RuntimeError(
                "SQLite constraint repair requires the migration connection's foreign_keys=OFF"
            )
        # SQLite validates views during batch renames. Preserve their exact SQL,
        # as well as triggers that would otherwise disappear with rebuilt tables.
        views = list(
            bind.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"
            )
        )
        triggers = [
            (name, sql)
            for name, table, sql in bind.exec_driver_sql(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
            )
            if table in repairs
        ]
        for name, _ in views:
            bind.exec_driver_sql(f"DROP VIEW {_quote(name)}")
        for table, (foreign_keys, unique) in repairs.items():
            with op.batch_alter_table(table, recreate="always") as batch:
                for column, target, target_column in foreign_keys:
                    batch.create_foreign_key(
                        f"fk_{table}_{column}", target, [column], [target_column]
                    )
                if unique:
                    batch.create_unique_constraint(unique[0], list(unique[1]))
        for _, sql in views + triggers:
            bind.exec_driver_sql(sql)

    indexes = {item["name"]: item for item in sa.inspect(bind).get_indexes("job_postings")}
    if CAPTURE_INDEX not in indexes:
        op.create_index(CAPTURE_INDEX, "job_postings", ["last_applied_capture_seq"])
    elif (
        indexes[CAPTURE_INDEX]["column_names"] != ["last_applied_capture_seq"]
        or indexes[CAPTURE_INDEX]["unique"]
    ):
        raise RuntimeError("SQLite constraint repair stopped: incompatible capture index")
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("SQLite constraint repair stopped: post-rebuild foreign-key violations")


def downgrade():
    # These constraints are already part of the 0028 schema contract. Reversing
    # a repair would break healthy installations rather than restore that schema.
    pass
