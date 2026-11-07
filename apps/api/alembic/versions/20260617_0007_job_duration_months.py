"""Add denormalized job duration months.

Revision ID: 20260617_0007
Revises: 20260612_0006
Create Date: 2026-06-17
"""

from alembic import op
import sqlalchemy as sa

revision = "20260617_0007"
down_revision = "20260612_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _column_exists("job_postings", "duration_months"):
        op.add_column("job_postings", sa.Column("duration_months", sa.Integer(), nullable=True))
    if not _index_exists("job_postings", "ix_job_postings_duration_months"):
        op.create_index("ix_job_postings_duration_months", "job_postings", ["duration_months"])
    _backfill_duration_months()


def downgrade() -> None:
    if _index_exists("job_postings", "ix_job_postings_duration_months"):
        op.drop_index("ix_job_postings_duration_months", table_name="job_postings")
    if _column_exists("job_postings", "duration_months"):
        op.drop_column("job_postings", "duration_months")


def _backfill_duration_months() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    bind.execute(
        sa.text(
            """
            UPDATE job_postings
            SET duration_months = CAST(json_extract(employment_timing, '$.duration_months') AS INTEGER)
            WHERE duration_months IS NULL
              AND json_extract(employment_timing, '$.duration_months') IS NOT NULL
            """
        )
    )


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}
