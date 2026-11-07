"""Key company profiles by domain and persist mapped evidence.

Revision ID: 20260803_0016
Revises: 20260803_0015
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0016"
down_revision: str | None = "20260803_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Company profiles are a disposable research cache. Rebuild the new table
    # instead of carrying forward name-keyed or source-only entries that do not
    # satisfy the stronger identity and evidence contract.
    op.drop_index("ix_company_profiles_normalized_company_name", table_name="company_profiles")
    op.drop_index("ix_company_profiles_company_name", table_name="company_profiles")
    op.drop_table("company_profiles")

    op.create_table(
        "company_profiles",
        sa.Column("company_profile_id", sa.String(length=64), nullable=False),
        sa.Column("identity_key", sa.String(length=512), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("official_website_url", sa.String(length=1024), nullable=False),
        sa.Column("official_website_domain", sa.String(length=255), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("research_provider", sa.String(length=64), nullable=False),
        sa.Column("research_model", sa.String(length=100), nullable=True),
        sa.Column("research_reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("researched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("company_profile_id"),
    )
    op.create_index(
        "ix_company_profiles_identity_key",
        "company_profiles",
        ["identity_key"],
        unique=True,
    )
    op.create_index(
        "ix_company_profiles_normalized_company_name",
        "company_profiles",
        ["normalized_company_name"],
        unique=False,
    )
    op.create_index(
        "ix_company_profiles_company_name",
        "company_profiles",
        ["company_name"],
        unique=False,
    )
    op.create_index(
        "ix_company_profiles_official_website_domain",
        "company_profiles",
        ["official_website_domain"],
        unique=False,
    )

    op.create_table(
        "company_profile_aliases",
        sa.Column("company_profile_alias_id", sa.String(length=64), nullable=False),
        sa.Column("company_profile_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_profile_id"],
            ["company_profiles.company_profile_id"],
        ),
        sa.PrimaryKeyConstraint("company_profile_alias_id"),
        sa.UniqueConstraint(
            "company_profile_id",
            "normalized_company_name",
            name="uq_company_profile_alias_profile_name",
        ),
    )
    op.create_index(
        "ix_company_profile_aliases_company_profile_id",
        "company_profile_aliases",
        ["company_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_profile_aliases_normalized_company_name",
        "company_profile_aliases",
        ["normalized_company_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_profile_aliases_normalized_company_name",
        table_name="company_profile_aliases",
    )
    op.drop_index(
        "ix_company_profile_aliases_company_profile_id",
        table_name="company_profile_aliases",
    )
    op.drop_table("company_profile_aliases")
    op.drop_index("ix_company_profiles_official_website_domain", table_name="company_profiles")
    op.drop_index("ix_company_profiles_company_name", table_name="company_profiles")
    op.drop_index("ix_company_profiles_normalized_company_name", table_name="company_profiles")
    op.drop_index("ix_company_profiles_identity_key", table_name="company_profiles")
    op.drop_table("company_profiles")

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
