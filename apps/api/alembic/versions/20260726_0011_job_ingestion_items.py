"""Add durable per-link ATS ingestion items.

Revision ID: 20260726_0011
Revises: 20260623_0010
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa

revision = "20260726_0011"
down_revision = "20260623_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_ingestion_items",
        sa.Column("ingestion_item_id", sa.String(length=64), nullable=False),
        sa.Column("ingestion_run_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("resolved_url", sa.String(length=2048), nullable=True),
        sa.Column("canonical_url", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("linked_job_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["job_ingestion_runs.ingestion_run_id"],
            name="fk_job_ingestion_items_ingestion_run_id_job_ingestion_runs",
        ),
        sa.ForeignKeyConstraint(
            ["linked_job_id"],
            ["job_postings.id"],
            name="fk_job_ingestion_items_linked_job_id_job_postings",
        ),
        sa.PrimaryKeyConstraint("ingestion_item_id", name="pk_job_ingestion_items"),
    )
    op.create_index(
        "ix_job_ingestion_items_canonical_url",
        "job_ingestion_items",
        ["canonical_url"],
    )
    op.create_index(
        "ix_job_ingestion_items_ingestion_run_id",
        "job_ingestion_items",
        ["ingestion_run_id"],
    )
    op.create_index(
        "ix_job_ingestion_items_linked_job_id",
        "job_ingestion_items",
        ["linked_job_id"],
    )
    op.create_index("ix_job_ingestion_items_status", "job_ingestion_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_job_ingestion_items_status", table_name="job_ingestion_items")
    op.drop_index("ix_job_ingestion_items_linked_job_id", table_name="job_ingestion_items")
    op.drop_index("ix_job_ingestion_items_ingestion_run_id", table_name="job_ingestion_items")
    op.drop_index("ix_job_ingestion_items_canonical_url", table_name="job_ingestion_items")
    op.drop_table("job_ingestion_items")
