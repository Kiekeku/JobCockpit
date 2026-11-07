"""Make company profile aliases job- and context-aware.

Revision ID: 20260803_0017
Revises: 20260803_0016
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0017"
down_revision: str | None = "20260803_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Aliases are disposable cache metadata. Existing name-only aliases cannot
    # be safely promoted because they have no job or employer context.
    op.drop_index(
        "ix_company_profile_aliases_normalized_company_name",
        table_name="company_profile_aliases",
    )
    op.drop_index(
        "ix_company_profile_aliases_company_profile_id",
        table_name="company_profile_aliases",
    )
    op.drop_table("company_profile_aliases")
    _create_contextual_alias_table()


def downgrade() -> None:
    op.drop_index(
        "ix_company_profile_aliases_normalized_company_name",
        table_name="company_profile_aliases",
    )
    op.drop_index(
        "ix_company_profile_aliases_alias_key",
        table_name="company_profile_aliases",
    )
    op.drop_index(
        "ix_company_profile_aliases_company_profile_id",
        table_name="company_profile_aliases",
    )
    op.drop_table("company_profile_aliases")

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


def _create_contextual_alias_table() -> None:
    op.create_table(
        "company_profile_aliases",
        sa.Column("company_profile_alias_id", sa.String(length=64), nullable=False),
        sa.Column("company_profile_id", sa.String(length=64), nullable=False),
        sa.Column("alias_key", sa.String(length=128), nullable=False),
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
            "alias_key",
            name="uq_company_profile_alias_key",
        ),
    )
    op.create_index(
        "ix_company_profile_aliases_company_profile_id",
        "company_profile_aliases",
        ["company_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_company_profile_aliases_alias_key",
        "company_profile_aliases",
        ["alias_key"],
        unique=True,
    )
    op.create_index(
        "ix_company_profile_aliases_normalized_company_name",
        "company_profile_aliases",
        ["normalized_company_name"],
        unique=False,
    )
