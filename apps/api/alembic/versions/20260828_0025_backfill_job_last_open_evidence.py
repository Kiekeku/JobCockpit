"""Backfill the latest historical positive job-status evidence.

Revision ID: 20260828_0025
Revises: 20260828_0024
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0025"
down_revision: str | None = "20260828_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE job_postings
            SET status_last_open_at = (
                    SELECT checks.checked_at
                    FROM job_status_checks AS checks
                    WHERE checks.job_id = job_postings.id
                      AND checks.result = 'open'
                    ORDER BY checks.checked_at DESC,
                             checks.created_at DESC,
                             checks.status_check_id DESC
                    LIMIT 1
                ),
                status_last_open_evidence_code = (
                    SELECT checks.evidence_code
                    FROM job_status_checks AS checks
                    WHERE checks.job_id = job_postings.id
                      AND checks.result = 'open'
                    ORDER BY checks.checked_at DESC,
                             checks.created_at DESC,
                             checks.status_check_id DESC
                    LIMIT 1
                )
            WHERE EXISTS (
                SELECT 1
                FROM job_status_checks AS checks
                WHERE checks.job_id = job_postings.id
                  AND checks.result = 'open'
                  AND (
                      job_postings.status_last_open_at IS NULL
                      OR checks.checked_at > job_postings.status_last_open_at
                  )
            )
            """
        )
    )


def downgrade() -> None:
    # This migration only restores evidence already present in immutable check
    # history. Removing it would discard valid provenance, so downgrade is a no-op.
    pass
