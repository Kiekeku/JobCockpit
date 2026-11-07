"""Add generic job ingestion run audit records.

Revision ID: 20260604_0002
Revises: 20260602_0001
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260604_0002"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "job_ingestion_runs",
        sa.Column("ingestion_run_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("search_text", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("alert_level", sa.String(length=32), nullable=False),
        sa.Column("jobs_seen_count", sa.Integer(), nullable=False),
        sa.Column("jobs_created_count", sa.Integer(), nullable=False),
        sa.Column("jobs_duplicate_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("ingestion_run_id", name="pk_job_ingestion_runs"),
    )
    op.create_index("ix_job_ingestion_runs_alert_level", "job_ingestion_runs", ["alert_level"])
    op.create_index("ix_job_ingestion_runs_provider", "job_ingestion_runs", ["provider"])
    op.create_index("ix_job_ingestion_runs_source_url", "job_ingestion_runs", ["source_url"])
    op.create_index("ix_job_ingestion_runs_status", "job_ingestion_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_job_ingestion_runs_status", table_name="job_ingestion_runs")
    op.drop_index("ix_job_ingestion_runs_source_url", table_name="job_ingestion_runs")
    op.drop_index("ix_job_ingestion_runs_provider", table_name="job_ingestion_runs")
    op.drop_index("ix_job_ingestion_runs_alert_level", table_name="job_ingestion_runs")
    op.drop_table("job_ingestion_runs")
