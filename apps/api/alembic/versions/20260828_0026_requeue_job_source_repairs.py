"""Requeue source repairs created from approximate failure counts.

Revision ID: 20260828_0026
Revises: 20260828_0025
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0026"
down_revision: str | None = "20260828_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Revision 0024 inferred repeated identity mismatches from the generic
    # failure counter. Requeue those rows once so the corrected reconciler can
    # validate them using consecutive, same-source check history.
    op.execute(
        sa.text(
            """
            UPDATE job_postings
            SET status_source_repair_required = 0,
                status_next_check_at = CURRENT_TIMESTAMP
            WHERE opportunity_status = 'active'
              AND status_source_repair_required = 1
            """
        )
    )


def downgrade() -> None:
    # Re-applying an approximate quarantine would discard the correction.
    pass
