"""Add job reprocess run type.

Revision ID: 20260612_0006
Revises: 20260612_0005
Create Date: 2026-06-12
"""

from alembic import op
import sqlalchemy as sa

revision = "20260612_0006"
down_revision = "20260612_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _column_exists("job_reprocess_runs", "type"):
        op.add_column(
            "job_reprocess_runs",
            sa.Column(
                "type",
                sa.String(length=64),
                nullable=False,
                server_default="full_reprocess",
            ),
        )
    if not _index_exists("job_reprocess_runs", "ix_job_reprocess_runs_type"):
        op.create_index("ix_job_reprocess_runs_type", "job_reprocess_runs", ["type"])


def downgrade() -> None:
    if _index_exists("job_reprocess_runs", "ix_job_reprocess_runs_type"):
        op.drop_index("ix_job_reprocess_runs_type", table_name="job_reprocess_runs")
    if _column_exists("job_reprocess_runs", "type"):
        op.drop_column("job_reprocess_runs", "type")


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}
