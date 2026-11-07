"""Add durable job-availability evidence and source-repair state.

Revision ID: 20260828_0024
Revises: 20260818_0023
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0024"
down_revision: str | None = "20260818_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column("status_last_open_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "job_postings",
        sa.Column("status_last_open_evidence_code", sa.String(128)),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "status_source_repair_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        )
    )
    op.create_index(
        "ix_job_postings_status_source_repair_required",
        "job_postings",
        ["status_source_repair_required"],
    )

    op.add_column("job_status_checks", sa.Column("requested_url", sa.String(2048)))
    op.add_column("job_status_checks", sa.Column("final_url", sa.String(2048)))
    op.add_column("job_status_checks", sa.Column("candidate_kind", sa.String(64)))
    op.add_column("job_status_checks", sa.Column("diagnostics", sa.JSON()))

    op.execute(
        sa.text(
            """
            UPDATE job_postings
            SET status_last_open_at = status_last_checked_at,
                status_last_open_evidence_code = status_check_evidence_code
            WHERE status_check_result = 'open'
              AND status_last_checked_at IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE job_postings
            SET status_source_repair_required = 1,
                status_next_check_at = NULL
            WHERE opportunity_status = 'active'
              AND status_check_failure_count >= 3
              AND status_check_evidence_code IN (
                  'job_identity_mismatch',
                  'redirect_identity_mismatch',
                  'workday_job_identity_mismatch'
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("job_status_checks", "diagnostics")
    op.drop_column("job_status_checks", "candidate_kind")
    op.drop_column("job_status_checks", "final_url")
    op.drop_column("job_status_checks", "requested_url")
    op.drop_index(
        "ix_job_postings_status_source_repair_required",
        table_name="job_postings",
    )
    op.drop_column("job_postings", "status_source_repair_required")
    op.drop_column("job_postings", "status_last_open_evidence_code")
    op.drop_column("job_postings", "status_last_open_at")
