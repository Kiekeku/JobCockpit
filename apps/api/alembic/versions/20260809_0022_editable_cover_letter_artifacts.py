"""Link generated documents to optional editable artifacts.

Revision ID: 20260809_0022
Revises: 20260803_0021
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260809_0022"
down_revision: str | None = "20260803_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.add_column(
            sa.Column("editable_artifact_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_generated_documents_editable_artifact_id_stored_artifacts",
            "stored_artifacts",
            ["editable_artifact_id"],
            ["artifact_id"],
        )
        batch_op.create_index(
            "ix_generated_documents_editable_artifact_id",
            ["editable_artifact_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.drop_index("ix_generated_documents_editable_artifact_id")
        batch_op.drop_constraint(
            "fk_generated_documents_editable_artifact_id_stored_artifacts",
            type_="foreignkey",
        )
        batch_op.drop_column("editable_artifact_id")
