"""Add durable cover-letter generation requests.

Revision ID: 20260818_0023
Revises: 20260809_0022
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260818_0023"
down_revision: str | None = "20260809_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cover_letter_generation_runs",
        sa.Column("generation_run_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("posting_snapshot_hash", sa.String(length=64), nullable=False),
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.PrimaryKeyConstraint("generation_run_id"),
    )
    op.create_index(
        "ix_cover_letter_generation_runs_job_id",
        "cover_letter_generation_runs",
        ["job_id"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_posting_snapshot_hash",
        "cover_letter_generation_runs",
        ["posting_snapshot_hash"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_status",
        "cover_letter_generation_runs",
        ["status"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_next_attempt_at",
        "cover_letter_generation_runs",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_worker_id",
        "cover_letter_generation_runs",
        ["worker_id"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_claim_token",
        "cover_letter_generation_runs",
        ["claim_token"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_supervisor_lease_token",
        "cover_letter_generation_runs",
        ["supervisor_lease_token"],
    )
    op.create_index(
        "ix_cover_letter_generation_runs_job_status",
        "cover_letter_generation_runs",
        ["job_id", "status"],
    )
    op.create_index(
        "uq_cover_letter_generation_runs_active_job",
        "cover_letter_generation_runs",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('queued', 'running', 'retry_scheduled', 'waiting_for_profile')"
        ),
    )


def downgrade() -> None:
    op.drop_table("cover_letter_generation_runs")
