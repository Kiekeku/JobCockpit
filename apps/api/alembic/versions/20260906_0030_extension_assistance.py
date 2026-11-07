"""Persist application question reviews and scoped answer reuse."""

from alembic import op
import sqlalchemy as sa

revision = "20260906_0030"
down_revision = "20260905_0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "extension_application_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("page_url", sa.String(2048), nullable=False),
        sa.Column("page_title", sa.String(500), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("draft_status", sa.String(32), nullable=False),
        sa.Column("draft_request_id", sa.String(64), nullable=True),
        sa.Column("draft_model", sa.String(100), nullable=True),
        sa.Column("draft_error", sa.String(1000), nullable=False),
        sa.Column("draft_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipts", sa.JSON(), nullable=False),
        sa.Column("document_approval", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extension_application_sessions_job_id", "extension_application_sessions", ["job_id"])
    op.create_table(
        "extension_approved_answers",
        sa.Column("answer_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("question_signature", sa.String(64), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", "job_id", "question_signature", name="uq_extension_answer_scope"),
    )
    op.create_index("ix_extension_approved_answers_job_id", "extension_approved_answers", ["job_id"])


def downgrade():
    op.drop_table("extension_approved_answers")
    op.drop_table("extension_application_sessions")
