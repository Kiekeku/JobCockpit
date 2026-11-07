"""Add a source-only reconciliation queue; leave existing postings untouched."""

from alembic import op
import sqlalchemy as sa

revision = "20260905_0027"
down_revision = "20260828_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_source_reconciliations",
        sa.Column("job_id", sa.String(64), sa.ForeignKey("job_postings.id"), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("input_guard", sa.String(64), nullable=True),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("status", "next_attempt_at"):
        op.create_index(f"ix_job_source_reconciliations_{column}", "job_source_reconciliations", [column])


def downgrade() -> None:
    op.drop_table("job_source_reconciliations")
