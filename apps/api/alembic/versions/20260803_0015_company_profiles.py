"""Add reusable company research profiles.

Revision ID: 20260803_0015
Revises: 20260730_0014
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0015"
down_revision: str | None = "20260730_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column("company_profile_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("official_website_url", sa.String(length=1024), nullable=True),
        sa.Column("source_urls", sa.JSON(), nullable=False),
        sa.Column("research_provider", sa.String(length=64), nullable=False),
        sa.Column("research_model", sa.String(length=100), nullable=True),
        sa.Column("research_reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("researched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("company_profile_id"),
    )
    op.create_index(
        "ix_company_profiles_company_name",
        "company_profiles",
        ["company_name"],
        unique=False,
    )
    op.create_index(
        "ix_company_profiles_normalized_company_name",
        "company_profiles",
        ["normalized_company_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_company_profiles_normalized_company_name", table_name="company_profiles")
    op.drop_index("ix_company_profiles_company_name", table_name="company_profiles")
    op.drop_table("company_profiles")
