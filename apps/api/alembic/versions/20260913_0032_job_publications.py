"""Add an optional publication outbox and persistent delivery identities."""

from alembic import op
import sqlalchemy as sa

revision = "20260913_0032"
down_revision = "20260906_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_publications",
        sa.Column("publication_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64),
                  sa.ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("destination", sa.String(1024), nullable=False),
        sa.Column("remote_id", sa.String(120), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("assessment_hash", sa.String(64), nullable=True),
        sa.Column("assessment", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("job_id", "status", "next_attempt_at"):
        op.create_index(f"ix_job_publications_{column}", "job_publications", [column])


def downgrade() -> None:
    op.drop_table("job_publications")
