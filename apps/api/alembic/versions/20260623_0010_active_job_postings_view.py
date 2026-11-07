"""Add active job postings analytics view.

Revision ID: 20260623_0010
Revises: 20260622_0009
Create Date: 2026-06-23
"""

from alembic import op

revision = "20260623_0010"
down_revision = "20260622_0009"
branch_labels = None
depends_on = None


ACTIVE_JOB_POSTINGS_VIEW_SQL = """
CREATE VIEW active_job_postings AS
SELECT *
FROM job_postings
WHERE json_extract(source, '$.merged_into_job_id') IS NULL
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS active_job_postings")
    op.execute(ACTIVE_JOB_POSTINGS_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS active_job_postings")