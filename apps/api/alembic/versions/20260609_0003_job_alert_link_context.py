"""Store job alert link extraction context.

Revision ID: 20260609_0003
Revises: 20260604_0002
Create Date: 2026-06-09
"""

from alembic import op
import sqlalchemy as sa

revision = "20260609_0003"
down_revision = "20260604_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _column_exists("job_alert_links", "raw_text"):
        op.add_column(
            "job_alert_links",
            sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        )
    if not _column_exists("job_alert_links", "surrounding_text"):
        op.add_column(
            "job_alert_links",
            sa.Column("surrounding_text", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _column_exists("job_alert_links", "surrounding_text"):
        op.drop_column("job_alert_links", "surrounding_text")
    if _column_exists("job_alert_links", "raw_text"):
        op.drop_column("job_alert_links", "raw_text")


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}
