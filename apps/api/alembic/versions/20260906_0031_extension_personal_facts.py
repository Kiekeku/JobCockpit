"""User-confirmed personal facts learned from application answers."""
from alembic import op
import sqlalchemy as sa

revision = "20260906_0031"
down_revision = "20260906_0030"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("candidate_learned_facts",
        sa.Column("fact_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("subject_key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("country", sa.String(100), nullable=False),
        sa.Column("term", sa.String(16), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("job_postings.id"), nullable=True),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_candidate_learned_facts_candidate_id", "candidate_learned_facts", ["candidate_id"])
    op.create_table("extension_learning_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("proposals", sa.JSON(), nullable=False),
        sa.Column("confirmed_observations", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(1000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_extension_learning_runs_candidate_id", "extension_learning_runs", ["candidate_id"])


def downgrade():
    op.drop_table("extension_learning_runs")
    op.drop_table("candidate_learned_facts")
