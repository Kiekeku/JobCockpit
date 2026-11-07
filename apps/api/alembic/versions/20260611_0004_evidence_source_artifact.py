"""Track source artifact for profile evidence.

Revision ID: 20260611_0004
Revises: 20260609_0003
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa

revision = "20260611_0004"
down_revision = "20260609_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _column_exists("evidence_items", "source_artifact_id"):
        op.add_column("evidence_items", sa.Column("source_artifact_id", sa.String(length=64), nullable=True))
    if not _index_exists("ix_evidence_items_source_artifact_id"):
        op.create_index("ix_evidence_items_source_artifact_id", "evidence_items", ["source_artifact_id"])


def downgrade() -> None:
    if _index_exists("ix_evidence_items_source_artifact_id"):
        op.drop_index("ix_evidence_items_source_artifact_id", table_name="evidence_items")
    if _column_exists("evidence_items", "source_artifact_id"):
        op.drop_column("evidence_items", "source_artifact_id")


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {index["name"] for index in inspector.get_indexes("evidence_items")}
