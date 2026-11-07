"""Add job reprocess queue table.

Revision ID: 20260612_0005
Revises: 20260611_0004
Create Date: 2026-06-12
"""

from alembic import op
import sqlalchemy as sa

revision = "20260612_0005"
down_revision = "20260611_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _table_exists("job_reprocess_runs"):
        op.create_table(
            "job_reprocess_runs",
            sa.Column("reprocess_run_id", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
            sa.PrimaryKeyConstraint("reprocess_run_id"),
        )
    if not _index_exists("job_reprocess_runs", "ix_job_reprocess_runs_job_id"):
        op.create_index("ix_job_reprocess_runs_job_id", "job_reprocess_runs", ["job_id"])
    if not _index_exists("job_reprocess_runs", "ix_job_reprocess_runs_status"):
        op.create_index("ix_job_reprocess_runs_status", "job_reprocess_runs", ["status"])
    if not _index_exists("job_reprocess_runs", "ix_job_reprocess_runs_job_status"):
        op.create_index("ix_job_reprocess_runs_job_status", "job_reprocess_runs", ["job_id", "status"])
    if not _index_exists("job_reprocess_runs", "uq_job_reprocess_runs_active_job"):
        op.create_index(
            "uq_job_reprocess_runs_active_job",
            "job_reprocess_runs",
            ["job_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
        )


def downgrade() -> None:
    if _table_exists("job_reprocess_runs"):
        op.drop_table("job_reprocess_runs")


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}
