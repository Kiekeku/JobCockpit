"""Add the durable asynchronous capture execution model.

Revision ID: 20260803_0021
Revises: 20260803_0017
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision = "20260803_0021"
down_revision = "20260803_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACTIVE_REPROCESS_PREDICATE = "status IN ('queued', 'running', 'retry_scheduled')"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(
    name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    sqlite_where: sa.TextClause | None = None,
) -> None:
    if name in _index_names(table_name):
        return
    op.create_index(
        name,
        table_name,
        columns,
        unique=unique,
        sqlite_where=sqlite_where,
    )


def _create_job_source_identity_table() -> None:
    op.create_table(
        "job_source_identities",
        sa.Column("job_source_identity_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("identity_strength", sa.String(length=32), nullable=False),
        sa.Column("public_source_url", sa.String(length=2048), nullable=True),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("match_priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("job_source_identity_id"),
        sa.UniqueConstraint(
            "job_id",
            "provider",
            "identity_hash",
            name="uq_job_source_identities_job_provider_identity",
        ),
    )


def _upgrade_partial_job_source_identity_table() -> None:
    if "job_source_identities" not in _table_names():
        _create_job_source_identity_table()
        return
    columns = _column_names("job_source_identities")
    if {"source_kind", "match_priority", "identity_strength"} <= columns:
        return
    if {"source_kind", "match_priority"} <= columns:
        op.add_column(
            "job_source_identities",
            sa.Column(
                "identity_strength",
                sa.String(length=32),
                nullable=False,
                server_default="weak",
            ),
        )
        op.execute(sa.text("UPDATE job_source_identities SET identity_strength = 'weak'"))
        return
    legacy_columns = {
        "job_source_identity_id",
        "job_id",
        "provider",
        "identity_hash",
        "public_source_url",
        "created_at",
        "updated_at",
    }
    if columns != legacy_columns:
        raise RuntimeError(
            "Partial job_source_identities schema is not recognized; refusing an unsafe migration"
        )
    inspector = sa.inspect(op.get_bind())
    for index in inspector.get_indexes("job_source_identities"):
        if index.get("name"):
            op.drop_index(str(index["name"]), table_name="job_source_identities")
    op.rename_table("job_source_identities", "job_source_identities_partial")
    _create_job_source_identity_table()
    op.execute(
        sa.text(
            """
            INSERT INTO job_source_identities (
                job_source_identity_id, job_id, provider, identity_hash,
                public_source_url, identity_strength, source_kind, match_priority,
                created_at, updated_at
            )
            SELECT
                job_source_identity_id, job_id, provider, identity_hash,
                public_source_url,
                'weak',
                'source_url', 100,
                created_at, updated_at
            FROM job_source_identities_partial
            """
        )
    )
    op.drop_table("job_source_identities_partial")


def upgrade() -> None:
    reprocess_columns = _column_names("job_reprocess_runs")
    if "next_attempt_at" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "error_code" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("error_code", sa.String(length=128), nullable=True),
        )
    if "retryable" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "worker_id" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("worker_id", sa.String(length=64), nullable=True),
        )
    if "claim_token" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("claim_token", sa.String(length=64), nullable=True),
        )
    if "supervisor_lease_token" not in reprocess_columns:
        op.add_column(
            "job_reprocess_runs",
            sa.Column("supervisor_lease_token", sa.String(length=64), nullable=True),
        )
    _create_index_if_missing(
        "ix_job_reprocess_runs_next_attempt_at",
        "job_reprocess_runs",
        ["next_attempt_at"],
    )
    _create_index_if_missing(
        "ix_job_reprocess_runs_worker_id",
        "job_reprocess_runs",
        ["worker_id"],
    )
    _create_index_if_missing(
        "ix_job_reprocess_runs_claim_token",
        "job_reprocess_runs",
        ["claim_token"],
    )
    _create_index_if_missing(
        "ix_job_reprocess_runs_supervisor_lease_token",
        "job_reprocess_runs",
        ["supervisor_lease_token"],
    )
    op.execute(sa.text("DROP INDEX IF EXISTS uq_job_reprocess_runs_active_job"))
    _create_index_if_missing(
        "uq_job_reprocess_runs_active_job",
        "job_reprocess_runs",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text(ACTIVE_REPROCESS_PREDICATE),
    )

    job_columns = _column_names("job_postings")
    if "last_applied_capture_seq" not in job_columns:
        op.add_column(
            "job_postings",
            sa.Column(
                "last_applied_capture_seq",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    _create_index_if_missing(
        "ix_job_postings_last_applied_capture_seq",
        "job_postings",
        ["last_applied_capture_seq"],
    )

    if "page_capture_snapshots" not in _table_names():
        op.create_table(
            "page_capture_snapshots",
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("source_url", sa.String(length=2048), nullable=False),
            sa.Column("page_title", sa.String(length=512), nullable=False),
            sa.Column("raw_content_artifact_id", sa.String(length=64), nullable=False),
            sa.Column("source_archive_artifact_id", sa.String(length=64), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("evidence_seq", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["raw_content_artifact_id"], ["stored_artifacts.artifact_id"]),
            sa.ForeignKeyConstraint(
                ["source_archive_artifact_id"], ["stored_artifacts.artifact_id"]
            ),
            sa.PrimaryKeyConstraint("snapshot_id"),
            sa.UniqueConstraint(
                "evidence_seq",
                name="uq_page_capture_snapshots_evidence_seq",
            ),
        )
    _create_index_if_missing(
        "ix_page_capture_snapshots_content_hash",
        "page_capture_snapshots",
        ["content_hash"],
    )

    if "page_capture_runs" not in _table_names():
        op.create_table(
            "page_capture_runs",
            sa.Column("capture_id", sa.String(length=64), nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("identity_hash", sa.String(length=64), nullable=False),
            sa.Column("identity_strength", sa.String(length=32), nullable=False),
            sa.Column("public_source_url", sa.String(length=2048), nullable=True),
            sa.Column("target_job_id", sa.String(length=64), nullable=True),
            sa.Column("job_id", sa.String(length=64), nullable=True),
            sa.Column("skip_language_filter", sa.Boolean(), nullable=False),
            sa.Column("page_access_verdict", sa.String(length=32), nullable=False),
            sa.Column("page_kind", sa.String(length=32), nullable=False),
            sa.Column("identity_verdict", sa.String(length=32), nullable=False),
            sa.Column("association_verdict", sa.String(length=32), nullable=False),
            sa.Column("assessment_evidence_code", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_code", sa.String(length=128), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("retryable", sa.Boolean(), nullable=False),
            sa.Column("worker_id", sa.String(length=64), nullable=True),
            sa.Column("claim_token", sa.String(length=64), nullable=True),
            sa.Column("supervisor_lease_token", sa.String(length=64), nullable=True),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("retry_of_capture_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["snapshot_id"], ["page_capture_snapshots.snapshot_id"]),
            sa.ForeignKeyConstraint(["target_job_id"], ["job_postings.id"]),
            sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
            sa.ForeignKeyConstraint(["retry_of_capture_id"], ["page_capture_runs.capture_id"]),
            sa.PrimaryKeyConstraint("capture_id"),
        )
    capture_columns = _column_names("page_capture_runs")
    if "identity_strength" not in capture_columns:
        op.add_column(
            "page_capture_runs",
            sa.Column(
                "identity_strength",
                sa.String(length=32),
                nullable=False,
                server_default="weak",
            ),
        )
        op.execute(sa.text("UPDATE page_capture_runs SET identity_strength = 'weak'"))
    assessment_columns = (
        ("page_access_verdict", sa.String(length=32), "unassessed"),
        ("page_kind", sa.String(length=32), "unknown"),
        ("identity_verdict", sa.String(length=32), "pending"),
        ("association_verdict", sa.String(length=32), "pending"),
    )
    for column_name, column_type, default in assessment_columns:
        if column_name not in capture_columns:
            op.add_column(
                "page_capture_runs",
                sa.Column(
                    column_name,
                    column_type,
                    nullable=False,
                    server_default=default,
                ),
            )
    if "assessment_evidence_code" not in capture_columns:
        op.add_column(
            "page_capture_runs",
            sa.Column(
                "assessment_evidence_code",
                sa.String(length=128),
                nullable=True,
            ),
        )
    accepted_expression = (
        "identity_accepted = 1 OR " if "identity_accepted" in capture_columns else ""
    ) + (
        "(status IN ('succeeded', 'skipped') AND "
        "json_extract(result, '$.status') IN "
        "('created', 'updated', 'terminal_status_updated', "
        "'terminal_status_already_recorded'))"
    )
    op.execute(
        sa.text(
            "UPDATE page_capture_runs SET identity_verdict = "
            f"CASE WHEN {accepted_expression} THEN 'accepted' "
            "WHEN status IN ('failed', 'skipped', 'superseded') "
            "THEN 'rejected' ELSE 'pending' END"
        )
    )
    op.execute(
        sa.text(
            "UPDATE page_capture_runs SET association_verdict = "
            "CASE WHEN identity_verdict = 'accepted' AND job_id IS NOT NULL "
            "THEN 'verified' WHEN status = 'skipped' AND job_id IS NULL "
            "THEN 'unmatched' ELSE 'pending' END"
        )
    )
    if "identity_accepted" in capture_columns:
        op.drop_column("page_capture_runs", "identity_accepted")
    if "supervisor_lease_token" not in capture_columns:
        op.add_column(
            "page_capture_runs",
            sa.Column("supervisor_lease_token", sa.String(length=64), nullable=True),
        )
    for name, columns in (
        ("ix_page_capture_runs_snapshot_id", ["snapshot_id"]),
        ("ix_page_capture_runs_provider", ["provider"]),
        ("ix_page_capture_runs_identity_hash", ["identity_hash"]),
        ("ix_page_capture_runs_identity_strength", ["identity_strength"]),
        ("ix_page_capture_runs_target_job_id", ["target_job_id"]),
        ("ix_page_capture_runs_job_id", ["job_id"]),
        ("ix_page_capture_runs_status", ["status"]),
        ("ix_page_capture_runs_next_attempt_at", ["next_attempt_at"]),
        ("ix_page_capture_runs_worker_id", ["worker_id"]),
        ("ix_page_capture_runs_claim_token", ["claim_token"]),
        (
            "ix_page_capture_runs_supervisor_lease_token",
            ["supervisor_lease_token"],
        ),
        ("ix_page_capture_runs_retry_of_capture_id", ["retry_of_capture_id"]),
        (
            "ix_page_capture_runs_status_due",
            ["status", "next_attempt_at", "created_at"],
        ),
        (
            "ix_page_capture_runs_identity_status",
            ["identity_hash", "status"],
        ),
    ):
        _create_index_if_missing(name, "page_capture_runs", columns)

    if "page_capture_snapshot_scopes" not in _table_names():
        op.create_table(
            "page_capture_snapshot_scopes",
            sa.Column("capture_scope_id", sa.String(length=64), nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=True),
            sa.Column("scope_hash", sa.String(length=64), nullable=False),
            sa.Column("scope_kind", sa.String(length=32), nullable=False),
            sa.Column("association_basis", sa.String(length=32), nullable=False),
            sa.Column("identity_strength", sa.String(length=32), nullable=False),
            sa.Column("association_verdict", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["snapshot_id"], ["page_capture_snapshots.snapshot_id"]),
            sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
            sa.PrimaryKeyConstraint("capture_scope_id"),
            sa.UniqueConstraint(
                "snapshot_id",
                "scope_hash",
                name="uq_page_capture_snapshot_scopes_snapshot_scope",
            ),
        )
    scope_columns = _column_names("page_capture_snapshot_scopes")
    if "job_id" not in scope_columns:
        op.add_column(
            "page_capture_snapshot_scopes",
            sa.Column("job_id", sa.String(length=64), nullable=True),
        )
        op.execute(
            sa.text(
                "UPDATE page_capture_snapshot_scopes SET job_id = ("
                "SELECT COALESCE(r.target_job_id, r.job_id) FROM page_capture_runs r "
                "WHERE r.snapshot_id = page_capture_snapshot_scopes.snapshot_id "
                "ORDER BY r.created_at LIMIT 1) WHERE scope_kind = 'job'"
            )
        )
    for column_name, default in (
        ("association_basis", "source_identity"),
        ("identity_strength", "weak"),
        ("association_verdict", "asserted"),
    ):
        if column_name not in scope_columns:
            op.add_column(
                "page_capture_snapshot_scopes",
                sa.Column(
                    column_name,
                    sa.String(length=32),
                    nullable=False,
                    server_default=default,
                ),
            )
    for name, columns in (
        ("ix_page_capture_snapshot_scopes_snapshot_id", ["snapshot_id"]),
        ("ix_page_capture_snapshot_scopes_job_id", ["job_id"]),
        ("ix_page_capture_snapshot_scopes_scope_hash", ["scope_hash"]),
        ("ix_page_capture_snapshot_scopes_association_basis", ["association_basis"]),
        ("ix_page_capture_snapshot_scopes_identity_strength", ["identity_strength"]),
        ("ix_page_capture_snapshot_scopes_association_verdict", ["association_verdict"]),
        (
            "ix_page_capture_snapshot_scopes_scope_snapshot",
            ["scope_hash", "snapshot_id"],
        ),
    ):
        _create_index_if_missing(name, "page_capture_snapshot_scopes", columns)

    if "page_capture_effects" not in _table_names():
        op.create_table(
            "page_capture_effects",
            sa.Column("effect_id", sa.String(length=64), nullable=False),
            sa.Column("capture_id", sa.String(length=64), nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("effect_kind", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("outcome_code", sa.String(length=128), nullable=True),
            sa.Column("error_code", sa.String(length=128), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["capture_id"], ["page_capture_runs.capture_id"]),
            sa.ForeignKeyConstraint(["snapshot_id"], ["page_capture_snapshots.snapshot_id"]),
            sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
            sa.PrimaryKeyConstraint("effect_id"),
            sa.UniqueConstraint(
                "capture_id",
                "job_id",
                "effect_kind",
                name="uq_page_capture_effects_run_job_kind",
            ),
        )
    for name, columns in (
        ("ix_page_capture_effects_capture_id", ["capture_id"]),
        ("ix_page_capture_effects_snapshot_id", ["snapshot_id"]),
        ("ix_page_capture_effects_job_id", ["job_id"]),
        ("ix_page_capture_effects_status", ["status"]),
    ):
        _create_index_if_missing(name, "page_capture_effects", columns)

    if "page_capture_subject_heads" not in _table_names():
        op.create_table(
            "page_capture_subject_heads",
            sa.Column("scope_hash", sa.String(length=64), nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["snapshot_id"], ["page_capture_snapshots.snapshot_id"]),
            sa.PrimaryKeyConstraint("scope_hash"),
        )
    _create_index_if_missing(
        "ix_page_capture_subject_heads_snapshot_id",
        "page_capture_subject_heads",
        ["snapshot_id"],
    )

    _upgrade_partial_job_source_identity_table()
    _create_index_if_missing(
        "ix_job_source_identities_job_id",
        "job_source_identities",
        ["job_id"],
    )
    _create_index_if_missing(
        "ix_job_source_identities_provider",
        "job_source_identities",
        ["provider"],
    )
    _create_index_if_missing(
        "ix_job_source_identities_identity_hash",
        "job_source_identities",
        ["identity_hash"],
    )
    _create_index_if_missing(
        "ix_job_source_identities_identity_strength",
        "job_source_identities",
        ["identity_strength"],
    )
    _create_index_if_missing(
        "ix_job_source_identities_provider_identity",
        "job_source_identities",
        ["provider", "identity_hash"],
    )

    if "background_worker_leases" not in _table_names():
        op.create_table(
            "background_worker_leases",
            sa.Column("lease_name", sa.String(length=64), nullable=False),
            sa.Column("owner_id", sa.String(length=64), nullable=False),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("lease_name"),
            sa.UniqueConstraint(
                "lease_token",
                name="uq_background_worker_leases_lease_token",
            ),
        )
    lease_columns = _column_names("background_worker_leases")
    if "lease_token" not in lease_columns:
        op.add_column(
            "background_worker_leases",
            sa.Column("lease_token", sa.String(length=64), nullable=True),
        )
        op.execute(
            sa.text(
                "UPDATE background_worker_leases "
                "SET lease_token = 'lease_' || lower(hex(randomblob(16)))"
            )
        )
    _create_index_if_missing(
        "ix_background_worker_leases_owner_id",
        "background_worker_leases",
        ["owner_id"],
    )
    _create_index_if_missing(
        "ix_background_worker_leases_expires_at",
        "background_worker_leases",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("background_worker_leases")
    op.drop_table("job_source_identities")
    op.drop_table("page_capture_subject_heads")
    op.drop_table("page_capture_effects")
    op.drop_table("page_capture_snapshot_scopes")
    op.drop_table("page_capture_runs")
    op.drop_table("page_capture_snapshots")
    op.drop_index("ix_job_postings_last_applied_capture_seq", table_name="job_postings")
    op.drop_column("job_postings", "last_applied_capture_seq")

    op.drop_index("uq_job_reprocess_runs_active_job", table_name="job_reprocess_runs")
    op.create_index(
        "uq_job_reprocess_runs_active_job",
        "job_reprocess_runs",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_job_reprocess_runs_claim_token", table_name="job_reprocess_runs")
    op.drop_index(
        "ix_job_reprocess_runs_supervisor_lease_token",
        table_name="job_reprocess_runs",
    )
    op.drop_index("ix_job_reprocess_runs_worker_id", table_name="job_reprocess_runs")
    op.drop_index("ix_job_reprocess_runs_next_attempt_at", table_name="job_reprocess_runs")
    op.drop_column("job_reprocess_runs", "claim_token")
    op.drop_column("job_reprocess_runs", "supervisor_lease_token")
    op.drop_column("job_reprocess_runs", "worker_id")
    op.drop_column("job_reprocess_runs", "retryable")
    op.drop_column("job_reprocess_runs", "error_code")
    op.drop_column("job_reprocess_runs", "next_attempt_at")
