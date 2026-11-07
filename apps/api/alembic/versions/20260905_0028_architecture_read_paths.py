"""Persist manual generation inputs and index latest reprocess lookups."""

from alembic import op
import sqlalchemy as sa

revision = "20260905_0028"
down_revision = "20260905_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cover_letter_generation_runs",
        sa.Column(
            "request_payload",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_index(
        "ix_job_reprocess_runs_job_created",
        "job_reprocess_runs",
        ["job_id", "created_at", "reprocess_run_id"],
    )


def downgrade():
    op.drop_index("ix_job_reprocess_runs_job_created", table_name="job_reprocess_runs")
    with op.batch_alter_table("cover_letter_generation_runs") as batch:
        batch.drop_column("request_payload")
