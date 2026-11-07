"""Add scheduled job status reconciliation state.

Revision ID: 20260727_0013
Revises: 20260727_0012
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa

revision = "20260727_0013"
down_revision = "20260727_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("status_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("status_next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("status_check_result", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("status_check_evidence_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "status_check_failure_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "status_check_not_found_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_job_postings_status_due",
        "job_postings",
        ["opportunity_status", "status_next_check_at", "created_at"],
    )
    op.create_table(
        "job_status_scan_runs",
        sa.Column("status_scan_run_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("checked_count", sa.Integer(), nullable=False),
        sa.Column("open_count", sa.Integer(), nullable=False),
        sa.Column("closed_count", sa.Integer(), nullable=False),
        sa.Column("expired_count", sa.Integer(), nullable=False),
        sa.Column("inconclusive_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("deferred_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_job_status_scan_runs_status",
        "job_status_scan_runs",
        ["status"],
    )
    op.create_index(
        "uq_job_status_scan_runs_running",
        "job_status_scan_runs",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "job_status_checks",
        sa.Column("status_check_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "status_scan_run_id",
            sa.String(length=64),
            sa.ForeignKey("job_status_scan_runs.status_scan_run_id"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.String(length=64),
            sa.ForeignKey("job_postings.id"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("source_kind", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=64), nullable=False),
        sa.Column("detected_status", sa.String(length=64), nullable=True),
        sa.Column("evidence_code", sa.String(length=128), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("status_before", sa.String(length=64), nullable=False),
        sa.Column("status_after", sa.String(length=64), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_job_status_checks_status_scan_run_id",
        "job_status_checks",
        ["status_scan_run_id"],
    )
    op.create_index("ix_job_status_checks_job_id", "job_status_checks", ["job_id"])
    op.create_index("ix_job_status_checks_result", "job_status_checks", ["result"])
    op.create_index(
        "ix_job_status_checks_job_checked",
        "job_status_checks",
        ["job_id", "checked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_status_checks_job_checked", table_name="job_status_checks")
    op.drop_index("ix_job_status_checks_result", table_name="job_status_checks")
    op.drop_index("ix_job_status_checks_job_id", table_name="job_status_checks")
    op.drop_index(
        "ix_job_status_checks_status_scan_run_id",
        table_name="job_status_checks",
    )
    op.drop_table("job_status_checks")
    op.drop_index(
        "uq_job_status_scan_runs_running",
        table_name="job_status_scan_runs",
    )
    op.drop_index("ix_job_status_scan_runs_status", table_name="job_status_scan_runs")
    op.drop_table("job_status_scan_runs")
    op.drop_index("ix_job_postings_status_due", table_name="job_postings")
    op.drop_column("job_postings", "status_check_not_found_count")
    op.drop_column("job_postings", "status_check_failure_count")
    op.drop_column("job_postings", "status_check_evidence_code")
    op.drop_column("job_postings", "status_check_result")
    op.drop_column("job_postings", "status_next_check_at")
    op.drop_column("job_postings", "status_last_checked_at")
