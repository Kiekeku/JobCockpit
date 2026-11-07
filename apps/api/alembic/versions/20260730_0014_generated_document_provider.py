"""Record generated document provider provenance.

Revision ID: 20260730_0014
Revises: 20260727_0013
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0014"
down_revision: str | None = "20260727_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_documents",
        sa.Column(
            "generation_provider",
            sa.String(length=64),
            nullable=False,
            server_default="heuristic",
        ),
    )


def downgrade() -> None:
    op.drop_column("generated_documents", "generation_provider")
