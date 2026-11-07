"""Add role interest ingestion filtering fields.

Revision ID: 20260621_0008
Revises: 20260617_0007
Create Date: 2026-06-21
"""

from alembic import op
import sqlalchemy as sa

revision = "20260621_0008"
down_revision = "20260617_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _column_exists("job_alert_sources", "skip_interest_filter"):
        op.add_column(
            "job_alert_sources",
            sa.Column("skip_interest_filter", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _column_exists("job_ingestion_runs", "jobs_skipped_count"):
        op.add_column(
            "job_ingestion_runs",
            sa.Column("jobs_skipped_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _column_exists("job_ingestion_runs", "jobs_skipped_count"):
        op.drop_column("job_ingestion_runs", "jobs_skipped_count")
    if _column_exists("job_alert_sources", "skip_interest_filter"):
        op.drop_column("job_alert_sources", "skip_interest_filter")


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}
