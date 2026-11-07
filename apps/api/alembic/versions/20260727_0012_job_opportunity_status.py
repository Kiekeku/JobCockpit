"""Add job opportunity lifecycle status.

Revision ID: 20260727_0012
Revises: 20260726_0011
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa

revision = "20260727_0012"
down_revision = "20260726_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column(
            "opportunity_status",
            sa.String(length=64),
            server_default="active",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_job_postings_opportunity_status",
        "job_postings",
        ["opportunity_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_postings_opportunity_status", table_name="job_postings")
    op.drop_column("job_postings", "opportunity_status")
