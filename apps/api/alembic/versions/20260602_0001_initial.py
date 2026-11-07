"""Initial SQLite-first schema.

Revision ID: 20260602_0001
Revises:
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260602_0001"
down_revision = None
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "audit_log_entries",
        sa.Column("audit_log_entry_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=128), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("audit_log_entry_id", name="pk_audit_log_entries"),
    )
    op.create_index("ix_audit_log_entries_event_type", "audit_log_entries", ["event_type"])

    op.create_table(
        "candidate_profiles",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("profile_name", sa.String(length=255), nullable=False),
        sa.Column("profile_data", sa.JSON(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_candidate_profiles"),
    )

    op.create_table(
        "evidence_items",
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("allowed_for_resume", sa.Boolean(), nullable=False),
        sa.Column("allowed_for_cover_letter", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_evidence_items"),
    )
    op.create_index("ix_evidence_items_candidate_id", "evidence_items", ["candidate_id"])

    op.create_table(
        "job_alert_sources",
        sa.Column("job_alert_source_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("gmail_query", sa.Text(), nullable=False),
        sa.Column("sender_allowlist", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("job_alert_source_id", name="pk_job_alert_sources"),
    )
    op.create_index("ix_job_alert_sources_candidate_id", "job_alert_sources", ["candidate_id"])
    op.create_index("ix_job_alert_sources_enabled", "job_alert_sources", ["enabled"])
    op.create_index("ix_job_alert_sources_provider", "job_alert_sources", ["provider"])

    op.create_table(
        "email_ingestion_runs",
        sa.Column("email_ingestion_run_id", sa.String(length=64), nullable=False),
        sa.Column("job_alert_source_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("query_used", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("messages_imported_count", sa.Integer(), nullable=False),
        sa.Column("jobs_created_count", sa.Integer(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["job_alert_source_id"],
            ["job_alert_sources.job_alert_source_id"],
            name="fk_email_ingestion_runs_job_alert_source_id_job_alert_sources",
        ),
        sa.PrimaryKeyConstraint("email_ingestion_run_id", name="pk_email_ingestion_runs"),
    )

    op.create_table(
        "job_alert_email_processing_states",
        sa.Column("processing_state_id", sa.String(length=64), nullable=False),
        sa.Column("job_alert_source_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id_hash", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=64), nullable=False),
        sa.Column("last_ingestion_run_id", sa.String(length=64), nullable=True),
        sa.Column("linked_job_count", sa.Integer(), nullable=False),
        sa.Column("skipped_link_count", sa.Integer(), nullable=False),
        sa.Column("needs_review_count", sa.Integer(), nullable=False),
        sa.Column("local_notes", sa.Text(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["job_alert_source_id"],
            ["job_alert_sources.job_alert_source_id"],
            name="fk_email_processing_states_job_alert_source_id_job_alert_sources",
        ),
        sa.ForeignKeyConstraint(
            ["last_ingestion_run_id"],
            ["email_ingestion_runs.email_ingestion_run_id"],
            name="fk_email_processing_states_last_ingestion_run_id_email_ingestion_runs",
        ),
        sa.PrimaryKeyConstraint("processing_state_id", name="pk_job_alert_email_processing_states"),
        sa.UniqueConstraint(
            "job_alert_source_id",
            "provider_message_id_hash",
            name="uq_email_processing_state_source_message",
        ),
    )
    op.create_index(
        "ix_job_alert_email_processing_states_job_alert_source_id",
        "job_alert_email_processing_states",
        ["job_alert_source_id"],
    )
    op.create_index(
        "ix_job_alert_email_processing_states_provider_message_id_hash",
        "job_alert_email_processing_states",
        ["provider_message_id_hash"],
    )

    op.create_table(
        "job_alert_emails",
        sa.Column("job_alert_email_id", sa.String(length=64), nullable=False),
        sa.Column("email_ingestion_run_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_message_id_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_thread_id_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("sender_email_hash", sa.String(length=64), nullable=False),
        sa.Column("sender_domain", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.String(length=64), nullable=False),
        sa.Column("body_content_hash", sa.String(length=64), nullable=False),
        sa.Column("extraction_status", sa.String(length=64), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["email_ingestion_run_id"],
            ["email_ingestion_runs.email_ingestion_run_id"],
            name="fk_job_alert_emails_email_ingestion_run_id_email_ingestion_runs",
        ),
        sa.PrimaryKeyConstraint("job_alert_email_id", name="pk_job_alert_emails"),
    )
    op.create_index("ix_job_alert_emails_email_ingestion_run_id", "job_alert_emails", ["email_ingestion_run_id"])
    op.create_index(
        "ix_job_alert_emails_provider_message_id_hash",
        "job_alert_emails",
        ["provider_message_id_hash"],
    )

    op.create_table(
        "stored_artifacts",
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=True),
        sa.Column("application_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("storage_uri", sa.String(length=1024), nullable=False),
        sa.Column("original_file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("privacy_class", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_stored_artifacts"),
    )
    op.create_index("ix_stored_artifacts_artifact_type", "stored_artifacts", ["artifact_type"])
    op.create_index("ix_stored_artifacts_candidate_id", "stored_artifacts", ["candidate_id"])
    op.create_index("ix_stored_artifacts_privacy_class", "stored_artifacts", ["privacy_class"])
    op.create_index("ix_stored_artifacts_sha256", "stored_artifacts", ["sha256"])

    op.create_table(
        "job_postings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("posting_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_content_artifact_id", sa.String(length=64), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("normalized_title", sa.String(length=255), nullable=True),
        sa.Column("role_type", sa.String(length=64), nullable=False),
        sa.Column("application_deadline", sa.String(length=64), nullable=True),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("employer", sa.JSON(), nullable=False),
        sa.Column("role", sa.JSON(), nullable=False),
        sa.Column("employment_timing", sa.JSON(), nullable=False),
        sa.Column("location", sa.JSON(), nullable=False),
        sa.Column("eligibility", sa.JSON(), nullable=False),
        sa.Column("compensation", sa.JSON(), nullable=False),
        sa.Column("application", sa.JSON(), nullable=False),
        sa.Column("fit_analysis", sa.JSON(), nullable=False),
        sa.Column("data_quality", sa.JSON(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["raw_content_artifact_id"],
            ["stored_artifacts.artifact_id"],
            name="fk_job_postings_raw_content_artifact_id_stored_artifacts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_postings"),
        sa.UniqueConstraint("posting_snapshot_hash", name="uq_job_postings_posting_snapshot_hash"),
    )
    op.create_index("ix_job_company_title", "job_postings", ["company_name", "normalized_title"])
    op.create_index("ix_job_postings_application_deadline", "job_postings", ["application_deadline"])
    op.create_index("ix_job_postings_company_name", "job_postings", ["company_name"])
    op.create_index("ix_job_postings_normalized_title", "job_postings", ["normalized_title"])
    op.create_index("ix_job_postings_posting_snapshot_hash", "job_postings", ["posting_snapshot_hash"])
    op.create_index("ix_job_postings_role_type", "job_postings", ["role_type"])
    op.create_index("ix_job_postings_source_type", "job_postings", ["source_type"])
    op.create_index("ix_job_postings_source_url", "job_postings", ["source_url"])

    op.create_table(
        "job_alert_links",
        sa.Column("job_alert_link_id", sa.String(length=64), nullable=False),
        sa.Column("job_alert_email_id", sa.String(length=64), nullable=False),
        sa.Column("original_url", sa.String(length=2048), nullable=False),
        sa.Column("resolved_url", sa.String(length=2048), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("link_text", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("ingestion_status", sa.String(length=64), nullable=False),
        sa.Column("linked_job_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["job_alert_email_id"],
            ["job_alert_emails.job_alert_email_id"],
            name="fk_job_alert_links_email_id_job_alert_emails",
        ),
        sa.ForeignKeyConstraint(
            ["linked_job_id"],
            ["job_postings.id"],
            name="fk_job_alert_links_linked_job_id_job_postings",
        ),
        sa.PrimaryKeyConstraint("job_alert_link_id", name="pk_job_alert_links"),
    )
    op.create_index("ix_job_alert_links_canonical_url", "job_alert_links", ["canonical_url"])
    op.create_index("ix_job_alert_links_classification", "job_alert_links", ["classification"])
    op.create_index("ix_job_alert_links_ingestion_status", "job_alert_links", ["ingestion_status"])
    op.create_index("ix_job_alert_links_job_alert_email_id", "job_alert_links", ["job_alert_email_id"])
    op.create_index("ix_job_alert_links_linked_job_id", "job_alert_links", ["linked_job_id"])

    op.create_table(
        "applications",
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("would_accept_if_offered", sa.Boolean(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_date", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], name="fk_applications_job_id_job_postings"),
        sa.PrimaryKeyConstraint("application_id", name="pk_applications"),
        sa.UniqueConstraint("candidate_id", "job_id", name="uq_applications_candidate_id_job_id"),
    )
    op.create_index("ix_applications_candidate_id", "applications", ["candidate_id"])
    op.create_index("ix_applications_decision", "applications", ["decision"])
    op.create_index("ix_applications_follow_up_date", "applications", ["follow_up_date"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_status", "applications", ["status"])

    op.create_table(
        "fit_analyses",
        sa.Column("fit_analysis_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], name="fk_fit_analyses_job_id_job_postings"),
        sa.PrimaryKeyConstraint("fit_analysis_id", name="pk_fit_analyses"),
    )
    op.create_index("ix_fit_analyses_job_id", "fit_analyses", ["job_id"])

    op.create_table(
        "application_packages",
        sa.Column("application_package_id", sa.String(length=64), nullable=False),
        sa.Column("application_id", sa.String(length=64), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("approved_for_extension", sa.Boolean(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.application_id"],
            name="fk_application_packages_application_id_applications",
        ),
        sa.PrimaryKeyConstraint("application_package_id", name="pk_application_packages"),
    )
    op.create_index("ix_application_packages_application_id", "application_packages", ["application_id"])

    op.create_table(
        "truthfulness_checks",
        sa.Column("truthfulness_check_id", sa.String(length=64), nullable=False),
        sa.Column("application_package_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("blocking_errors", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["application_package_id"],
            ["application_packages.application_package_id"],
            name="fk_truthfulness_checks_application_package_id_application_packages",
        ),
        sa.PrimaryKeyConstraint("truthfulness_check_id", name="pk_truthfulness_checks"),
    )
    op.create_index(
        "ix_truthfulness_checks_application_package_id",
        "truthfulness_checks",
        ["application_package_id"],
    )

    op.create_table(
        "generated_documents",
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("application_package_id", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("source_format", sa.String(length=32), nullable=False),
        sa.Column("rendered_format", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_preview", sa.Text(), nullable=False),
        sa.Column("claim_mapping_count", sa.Integer(), nullable=False),
        sa.Column("truthfulness_check_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by_user", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["application_package_id"],
            ["application_packages.application_package_id"],
            name="fk_generated_documents_application_package_id_application_packages",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["stored_artifacts.artifact_id"],
            name="fk_generated_documents_artifact_id_stored_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["truthfulness_check_id"],
            ["truthfulness_checks.truthfulness_check_id"],
            name="fk_generated_documents_truthfulness_check_id_truthfulness_checks",
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_generated_documents"),
    )
    op.create_index("ix_generated_documents_application_package_id", "generated_documents", ["application_package_id"])
    op.create_index("ix_generated_documents_document_type", "generated_documents", ["document_type"])

    op.create_table(
        "application_package_documents",
        sa.Column("application_package_document_id", sa.String(length=64), nullable=False),
        sa.Column("application_package_id", sa.String(length=64), nullable=False),
        sa.Column("generated_document_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("approved_for_upload", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["application_package_id"],
            ["application_packages.application_package_id"],
            name="fk_application_package_documents_package_id_application_packages",
        ),
        sa.ForeignKeyConstraint(
            ["generated_document_id"],
            ["generated_documents.document_id"],
            name="fk_application_package_documents_generated_document_id_generated_documents",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["stored_artifacts.artifact_id"],
            name="fk_application_package_documents_artifact_id_stored_artifacts",
        ),
        sa.PrimaryKeyConstraint("application_package_document_id", name="pk_application_package_documents"),
    )
    op.create_index(
        "ix_application_package_documents_application_package_id",
        "application_package_documents",
        ["application_package_id"],
    )
    op.create_index(
        "ix_application_package_documents_document_type",
        "application_package_documents",
        ["document_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_package_documents_document_type", table_name="application_package_documents")
    op.drop_index(
        "ix_application_package_documents_application_package_id",
        table_name="application_package_documents",
    )
    op.drop_table("application_package_documents")

    op.drop_index("ix_generated_documents_document_type", table_name="generated_documents")
    op.drop_index("ix_generated_documents_application_package_id", table_name="generated_documents")
    op.drop_table("generated_documents")

    op.drop_index("ix_truthfulness_checks_application_package_id", table_name="truthfulness_checks")
    op.drop_table("truthfulness_checks")

    op.drop_index("ix_application_packages_application_id", table_name="application_packages")
    op.drop_table("application_packages")

    op.drop_index("ix_fit_analyses_job_id", table_name="fit_analyses")
    op.drop_table("fit_analyses")

    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_index("ix_applications_follow_up_date", table_name="applications")
    op.drop_index("ix_applications_decision", table_name="applications")
    op.drop_index("ix_applications_candidate_id", table_name="applications")
    op.drop_table("applications")

    op.drop_index("ix_job_alert_links_linked_job_id", table_name="job_alert_links")
    op.drop_index("ix_job_alert_links_job_alert_email_id", table_name="job_alert_links")
    op.drop_index("ix_job_alert_links_ingestion_status", table_name="job_alert_links")
    op.drop_index("ix_job_alert_links_classification", table_name="job_alert_links")
    op.drop_index("ix_job_alert_links_canonical_url", table_name="job_alert_links")
    op.drop_table("job_alert_links")

    op.drop_index("ix_job_postings_source_url", table_name="job_postings")
    op.drop_index("ix_job_postings_source_type", table_name="job_postings")
    op.drop_index("ix_job_postings_role_type", table_name="job_postings")
    op.drop_index("ix_job_postings_posting_snapshot_hash", table_name="job_postings")
    op.drop_index("ix_job_postings_normalized_title", table_name="job_postings")
    op.drop_index("ix_job_postings_company_name", table_name="job_postings")
    op.drop_index("ix_job_postings_application_deadline", table_name="job_postings")
    op.drop_index("ix_job_company_title", table_name="job_postings")
    op.drop_table("job_postings")

    op.drop_index("ix_stored_artifacts_sha256", table_name="stored_artifacts")
    op.drop_index("ix_stored_artifacts_privacy_class", table_name="stored_artifacts")
    op.drop_index("ix_stored_artifacts_candidate_id", table_name="stored_artifacts")
    op.drop_index("ix_stored_artifacts_artifact_type", table_name="stored_artifacts")
    op.drop_table("stored_artifacts")

    op.drop_index("ix_job_alert_emails_provider_message_id_hash", table_name="job_alert_emails")
    op.drop_index("ix_job_alert_emails_email_ingestion_run_id", table_name="job_alert_emails")
    op.drop_table("job_alert_emails")

    op.drop_index(
        "ix_job_alert_email_processing_states_provider_message_id_hash",
        table_name="job_alert_email_processing_states",
    )
    op.drop_index(
        "ix_job_alert_email_processing_states_job_alert_source_id",
        table_name="job_alert_email_processing_states",
    )
    op.drop_table("job_alert_email_processing_states")

    op.drop_table("email_ingestion_runs")

    op.drop_index("ix_job_alert_sources_provider", table_name="job_alert_sources")
    op.drop_index("ix_job_alert_sources_enabled", table_name="job_alert_sources")
    op.drop_index("ix_job_alert_sources_candidate_id", table_name="job_alert_sources")
    op.drop_table("job_alert_sources")

    op.drop_index("ix_evidence_items_candidate_id", table_name="evidence_items")
    op.drop_table("evidence_items")

    op.drop_table("candidate_profiles")

    op.drop_index("ix_audit_log_entries_event_type", table_name="audit_log_entries")
    op.drop_table("audit_log_entries")
