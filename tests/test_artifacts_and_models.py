import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    Application,
    ApplicationPackageDocument,
    Base,
    EmailIngestionRun,
    GeneratedDocument,
    JobAlertEmail,
    JobAlertEmailProcessingState,
    JobAlertLink,
    JobAlertSource,
    JobPosting,
    StoredArtifact,
    TruthfulnessCheck,
)
from jobcockpit.db.session import create_analytics_views, init_database, session_scope
from jobcockpit.ingestion.status_reconciliation import run_status_reconciliation
from jobcockpit.storage.artifacts import ArtifactStorageService
import scripts.migrate as migrate_module
from scripts.migrate import (
    ACTIVE_JOB_POSTINGS_REVISION,
    BASELINE_REVISION,
    CAPTURE_EXECUTION_MODEL_REVISION,
    COMPANY_PROFILES_REVISION,
    PUBLICATION_REVISION as CURRENT_SCHEMA_REVISION,
    JOB_INGESTION_REVISION,
    JOB_INGESTION_ITEMS_REVISION,
    JOB_OPPORTUNITY_STATUS_REVISION,
    JOB_REPROCESS_REVISION,
    JOB_STATUS_RECONCILIATION_REVISION,
    capture_revision_0021_tables,
    config_for_database,
    maybe_convert_provisional_capture_schema,
    maybe_stamp_existing_sqlite_schema,
    recoverable_sqlite_schema_shapes,
    sqlite_schema_mismatches,
    sqlite_tables,
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )


def insert_legacy_job_posting(db, **values) -> None:
    """Insert through the reflected historical table, not the current ORM shape."""
    table = sa.Table("job_postings", sa.MetaData(), autoload_with=db.connection())
    payload = {
        "role_type": "unknown",
        "opportunity_status": "active",
        "status_check_failure_count": 0,
        "status_check_not_found_count": 0,
        "last_applied_capture_seq": 0,
        "source": {},
        "employer": {},
        "role": {},
        "employment_timing": {},
        "location": {},
        "eligibility": {},
        "compensation": {},
        "application": {},
        "fit_analysis": {},
        "data_quality": {},
        "created_at": sa.func.current_timestamp(),
        "updated_at": sa.func.current_timestamp(),
        **values,
    }
    db.execute(table.insert().values(**payload))


def test_artifact_storage_writes_file_and_returns_hash_metadata(tmp_path: Path):
    settings = make_settings(tmp_path)
    storage = ArtifactStorageService(settings.artifact_root)

    artifact = storage.write_bytes(
        artifact_type="job_snapshot",
        content=b"Senior Python Engineer\nRemote Canada",
        original_file_name="posting.txt",
        content_type="text/plain",
        candidate_id="candidate_demo",
        privacy_class="private",
    )

    assert artifact.size_bytes == 36
    assert artifact.sha256 == "52cec0a8d89661dd70c87f6661540a5d5b04664ac9fda8c5ec8b480995372f07"
    assert artifact.storage_uri.startswith("local://job-snapshots/")
    assert (settings.artifact_root / "job-snapshots").exists()
    assert storage.read_bytes(artifact.storage_uri) == b"Senior Python Engineer\nRemote Canada"


def test_database_schema_and_health_endpoint_initialize(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "sqlite",
        "artifact_storage": "local",
    }


def test_sqlite_uses_wal_and_waits_for_short_write_contention(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with session_scope(settings) as db:
        journal_mode = db.execute(sa.text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = db.execute(sa.text("PRAGMA busy_timeout")).scalar_one()

    assert journal_mode.casefold() == "wal"
    assert busy_timeout >= 5_000


def test_sqlite_enforces_foreign_key_constraints(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                Application(
                    application_id="app_orphan",
                    candidate_id="candidate_demo",
                    job_id="job_missing",
                    status="draft",
                    decision="undecided",
                )
            )


def test_sqlite_enforces_job_alert_link_email_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                JobAlertLink(
                    job_alert_link_id="link_orphan",
                    job_alert_email_id="email_missing",
                    original_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    resolved_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    canonical_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    link_text="Software Job",
                    classification="probable_job_posting",
                    confidence=90,
                )
            )


def test_sqlite_enforces_job_alert_email_run_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                JobAlertEmail(
                    job_alert_email_id="email_orphan",
                    email_ingestion_run_id="emailrun_missing",
                    provider_message_id_hash="message_hash_orphan",
                    provider_thread_id_hash="orphan-thread-hash",
                    subject="Orphan alert",
                    sender_email_hash="sender_hash_orphan",
                    sender_domain="alerts.invalid",
                    received_at="2026-06-02T12:00:00Z",
                    body_content_hash="body_hash_orphan",
                )
            )


def test_sqlite_enforces_one_application_per_candidate_job(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    first_application = client.post(f"/api/jobs/{job['id']}/applications").json()

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                Application(
                    application_id="app_duplicate",
                    candidate_id=first_application["candidate_id"],
                    job_id=job["id"],
                    status="draft",
                    decision="undecided",
                )
            )


def test_sqlite_enforces_unique_job_posting_snapshot_hash(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            original = db.get(JobPosting, job["id"])
            db.add(
                JobPosting(
                    id="job_duplicate",
                    source_type=original.source_type,
                    source_url=original.source_url,
                    posting_snapshot_hash=original.posting_snapshot_hash,
                    raw_content_artifact_id=original.raw_content_artifact_id,
                    company_name=original.company_name,
                    normalized_title=original.normalized_title,
                    role_type=original.role_type,
                    application_deadline=original.application_deadline,
                    duration_months=original.duration_months,
                    source=original.source,
                    employer=original.employer,
                    role=original.role,
                    employment_timing=original.employment_timing,
                    location=original.location,
                    eligibility=original.eligibility,
                    compensation=original.compensation,
                    application=original.application,
                    fit_analysis=original.fit_analysis,
                    data_quality=original.data_quality,
                )
            )


def test_sqlite_enforces_one_email_processing_state_per_source_message_hash(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="source_one",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Unique source",
                gmail_query="from:alerts.invalid",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add_all(
                [
                    JobAlertEmailProcessingState(
                        processing_state_id="emailstate_one",
                        job_alert_source_id="source_one",
                        provider_message_id_hash="message_hash_one",
                    ),
                    JobAlertEmailProcessingState(
                        processing_state_id="emailstate_two",
                        job_alert_source_id="source_one",
                        provider_message_id_hash="message_hash_one",
                    ),
                ]
            )


def test_sqlite_enforces_email_processing_state_source_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                JobAlertEmailProcessingState(
                    processing_state_id="emailstate_orphan_source",
                    job_alert_source_id="alertsrc_missing",
                    provider_message_id_hash="message_hash_orphan_source",
                )
            )


def test_sqlite_enforces_email_processing_state_last_run_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_existing",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Existing source",
                gmail_query="from:alerts.invalid",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                JobAlertEmailProcessingState(
                    processing_state_id="emailstate_orphan_run",
                    job_alert_source_id="alertsrc_existing",
                    provider_message_id_hash="message_hash_orphan_run",
                    last_ingestion_run_id="emailrun_missing",
                )
            )


def test_sqlite_enforces_truthfulness_check_package_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                TruthfulnessCheck(
                    truthfulness_check_id="truth_orphan_package",
                    application_package_id="pkg_missing",
                    status="passed",
                    blocking_errors=[],
                    warnings=[],
                )
            )


def test_sqlite_enforces_generated_document_truthfulness_check_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = client.post(f"/api/applications/{application['application_id']}/packages").json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            document_row = db.get(GeneratedDocument, document["document_id"])
            document_row.truthfulness_check_id = "truth_missing"


def test_sqlite_enforces_application_package_document_generated_document_foreign_key(
    tmp_path: Path,
):
    settings = make_settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Developer\nPython APIs",
            "source_url": None,
        },
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = client.post(f"/api/applications/{application['application_id']}/packages").json()
    document = client.post(
        f"/api/application-packages/{package['application_package_id']}/resume-draft"
    ).json()

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            document_row = db.get(GeneratedDocument, document["document_id"])
            db.add(
                ApplicationPackageDocument(
                    application_package_document_id="pkgdoc_orphan_generated_document",
                    application_package_id=package["application_package_id"],
                    generated_document_id="doc_missing",
                    artifact_id=document_row.artifact_id,
                    document_type="resume",
                    required=True,
                    approved_for_upload=False,
                )
            )


def test_sqlite_enforces_job_alert_link_linked_job_foreign_key(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_linked_job_fk",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Linked job FK source",
                gmail_query="from:alerts.invalid",
                sender_allowlist=["alerts.invalid"],
                enabled=True,
            )
        )
        db.flush()
        db.add(
            EmailIngestionRun(
                email_ingestion_run_id="emailrun_linked_job_fk",
                job_alert_source_id="alertsrc_linked_job_fk",
                provider="gmail",
                query_used="from:alerts.invalid",
                status="completed",
            )
        )
        db.flush()
        db.add(
            JobAlertEmail(
                job_alert_email_id="email_linked_job_fk",
                email_ingestion_run_id="emailrun_linked_job_fk",
                provider_message_id_hash="message_hash_linked_job_fk",
                provider_thread_id_hash="threadhashlinkedjobfk",
                subject="Linked job alert",
                sender_email_hash="sender_hash_linked_job_fk",
                sender_domain="alerts.invalid",
                received_at="2026-06-02T12:00:00Z",
                body_content_hash="body_hash_linked_job_fk",
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(settings) as db:
            db.add(
                JobAlertLink(
                    job_alert_link_id="link_orphan_linked_job",
                    job_alert_email_id="email_linked_job_fk",
                    original_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    resolved_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    canonical_url="http://localhost:8001/ats/generic/acme/jobs/123",
                    link_text="Software Job",
                    classification="probable_job_posting",
                    confidence=90,
                    linked_job_id="job_missing",
                )
            )


def test_email_processing_state_get_or_create_handles_unique_race(tmp_path: Path):
    settings = make_settings(tmp_path)
    init_database(settings)
    inserted = False
    competing_state_id = "emailstate_competing"

    def insert_competing_state(_session, _flush_context, _instances):
        nonlocal inserted
        if inserted:
            return
        inserted = True
        with session_scope(settings) as competing_db:
            competing_db.add(
                JobAlertSource(
                    job_alert_source_id="source_one",
                    candidate_id="candidate_local",
                    provider="gmail",
                    display_name="Race source",
                    gmail_query="from:alerts.invalid",
                    sender_allowlist=["alerts.invalid"],
                    enabled=True,
                )
            )
            competing_db.flush()
            competing_db.add(
                JobAlertEmailProcessingState(
                    processing_state_id=competing_state_id,
                    job_alert_source_id="source_one",
                    provider_message_id_hash="message_hash_race",
                )
            )

    with session_scope(settings) as db:
        event.listen(db, "before_flush", insert_competing_state)
        state = JobAlertEmailProcessingState._get_or_create(
            db,
            "source_one",
            {"id_hash": "message_hash_race"},
        )
        assert state.processing_state_id == competing_state_id

    with session_scope(settings) as db:
        states = list(db.execute(select(JobAlertEmailProcessingState)).scalars())
        assert len(states) == 1
        assert states[0].processing_state_id == competing_state_id


def test_app_startup_does_not_create_schema_without_migrations(tmp_path: Path):
    settings = make_settings(tmp_path)
    create_app(settings)

    assert not (tmp_path / "jobcockpit.db").exists()


def test_create_analytics_views_does_not_replace_existing_view(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'view-idempotency.db'}"
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE VIEW active_job_postings AS SELECT * FROM job_postings WHERE 0"
            )

        create_analytics_views(engine)

        with engine.connect() as connection:
            view_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'active_job_postings'"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "WHERE 0" in view_sql


def test_alembic_upgrade_head_creates_sqlite_schema(tmp_path: Path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'alembic-jobcockpit.db'}"
    monkeypatch.setenv("JOBCOCKPIT_DATABASE_URL", database_url)
    application_logger = logging.getLogger("jobcockpit.cli")
    monkeypatch.setattr(application_logger, "disabled", False)
    config = Config("alembic.ini")

    command.upgrade(config, "head")

    assert application_logger.disabled is False
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        views = set(inspector.get_view_names())
        job_alert_link_fks = inspector.get_foreign_keys("job_alert_links")
        job_ingestion_item_fks = inspector.get_foreign_keys("job_ingestion_items")
        job_ingestion_item_columns = {
            column["name"] for column in inspector.get_columns("job_ingestion_items")
        }
        job_posting_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        job_posting_indexes = {index["name"] for index in inspector.get_indexes("job_postings")}
        job_alert_email_fks = inspector.get_foreign_keys("job_alert_emails")
        generated_document_fks = inspector.get_foreign_keys("generated_documents")
        generated_document_columns = {
            column["name"] for column in inspector.get_columns("generated_documents")
        }
        application_package_document_fks = inspector.get_foreign_keys(
            "application_package_documents"
        )
        truthfulness_check_fks = inspector.get_foreign_keys("truthfulness_checks")
        application_unique_constraints = inspector.get_unique_constraints("applications")
        job_posting_unique_constraints = inspector.get_unique_constraints("job_postings")
        email_state_unique_constraints = inspector.get_unique_constraints(
            "job_alert_email_processing_states"
        )
        email_state_fks = inspector.get_foreign_keys("job_alert_email_processing_states")
    finally:
        engine.dispose()
    assert "active_job_postings" in views
    assert {
        "job_postings",
        "job_ingestion_runs",
        "job_reprocess_runs",
        "cover_letter_generation_runs",
        "job_ingestion_items",
        "applications",
        "generated_documents",
        "stored_artifacts",
        "job_status_scan_runs",
        "job_status_checks",
    } <= tables
    assert {
        "ingestion_item_id",
        "ingestion_run_id",
        "source_url",
        "resolved_url",
        "canonical_url",
        "status",
        "linked_job_id",
        "error_message",
        "retryable",
    } <= job_ingestion_item_columns
    assert "opportunity_status" in job_posting_columns
    assert {
        "status_last_checked_at",
        "status_next_check_at",
        "status_check_result",
        "status_check_evidence_code",
        "status_check_failure_count",
        "status_check_not_found_count",
    } <= job_posting_columns
    assert "ix_job_postings_opportunity_status" in job_posting_indexes
    assert "ix_job_postings_status_due" in job_posting_indexes
    item_fk_targets = {
        (
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            tuple(fk["referred_columns"]),
        )
        for fk in job_ingestion_item_fks
    }
    assert (
        ("ingestion_run_id",),
        "job_ingestion_runs",
        ("ingestion_run_id",),
    ) in item_fk_targets
    assert (("linked_job_id",), "job_postings", ("id",)) in item_fk_targets
    assert {
        "constrained_columns": ["job_alert_email_id"],
        "referred_table": "job_alert_emails",
        "referred_columns": ["job_alert_email_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in job_alert_link_fks
    ]
    assert {
        "constrained_columns": ["email_ingestion_run_id"],
        "referred_table": "email_ingestion_runs",
        "referred_columns": ["email_ingestion_run_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in job_alert_email_fks
    ]
    assert {
        "constrained_columns": ["linked_job_id"],
        "referred_table": "job_postings",
        "referred_columns": ["id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in job_alert_link_fks
    ]
    assert {
        "constrained_columns": ["truthfulness_check_id"],
        "referred_table": "truthfulness_checks",
        "referred_columns": ["truthfulness_check_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in generated_document_fks
    ]
    assert "editable_artifact_id" in generated_document_columns
    assert {
        "constrained_columns": ["editable_artifact_id"],
        "referred_table": "stored_artifacts",
        "referred_columns": ["artifact_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in generated_document_fks
    ]
    assert {
        "constrained_columns": ["generated_document_id"],
        "referred_table": "generated_documents",
        "referred_columns": ["document_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in application_package_document_fks
    ]
    assert {
        "constrained_columns": ["application_package_id"],
        "referred_table": "application_packages",
        "referred_columns": ["application_package_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in truthfulness_check_fks
    ]
    assert {
        "name": "uq_applications_candidate_id_job_id",
        "column_names": ["candidate_id", "job_id"],
    } in [
        {
            "name": constraint["name"],
            "column_names": constraint["column_names"],
        }
        for constraint in application_unique_constraints
    ]
    assert {
        "name": "uq_job_postings_posting_snapshot_hash",
        "column_names": ["posting_snapshot_hash"],
    } in [
        {
            "name": constraint["name"],
            "column_names": constraint["column_names"],
        }
        for constraint in job_posting_unique_constraints
    ]
    assert {
        "name": "uq_email_processing_state_source_message",
        "column_names": ["job_alert_source_id", "provider_message_id_hash"],
    } in [
        {
            "name": constraint["name"],
            "column_names": constraint["column_names"],
        }
        for constraint in email_state_unique_constraints
    ]
    assert {
        "constrained_columns": ["job_alert_source_id"],
        "referred_table": "job_alert_sources",
        "referred_columns": ["job_alert_source_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in email_state_fks
    ]
    assert {
        "constrained_columns": ["last_ingestion_run_id"],
        "referred_table": "email_ingestion_runs",
        "referred_columns": ["email_ingestion_run_id"],
    } in [
        {
            "constrained_columns": fk["constrained_columns"],
            "referred_table": fk["referred_table"],
            "referred_columns": fk["referred_columns"],
        }
        for fk in email_state_fks
    ]


def create_sqlite_schema_at_revision(
    database_url: str,
    revision: str,
    *,
    keep_empty_alembic_version: bool = False,
) -> None:
    config = config_for_database(database_url)
    command.upgrade(config, revision)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            if keep_empty_alembic_version:
                connection.exec_driver_sql("DELETE FROM alembic_version")
            else:
                connection.exec_driver_sql("DROP TABLE alembic_version")
    finally:
        engine.dispose()


def create_baseline_sqlite_schema(
    database_url: str, *, keep_empty_alembic_version: bool = False
) -> None:
    create_sqlite_schema_at_revision(
        database_url,
        BASELINE_REVISION,
        keep_empty_alembic_version=keep_empty_alembic_version,
    )


def test_company_profile_evidence_identity_migration_rebuilds_disposable_cache(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'company-profile-v1.db'}"
    create_sqlite_schema_at_revision(database_url, "20260803_0015")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO company_profiles (
                    company_profile_id,
                    normalized_company_name,
                    company_name,
                    summary,
                    official_website_url,
                    source_urls,
                    research_provider,
                    research_model,
                    research_reasoning_effort,
                    researched_at,
                    created_at,
                    updated_at
                ) VALUES (
                    'company_legacy',
                    'acme',
                    'Acme',
                    'Unmapped legacy summary',
                    'https://acme.example/',
                    '[]',
                    'codex_cli',
                    'gpt-old',
                    'max',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
    finally:
        engine.dispose()

    config = config_for_database(database_url)
    command.stamp(config, "20260803_0015")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        profile_columns = {column["name"] for column in inspector.get_columns("company_profiles")}
        alias_columns = {
            column["name"] for column in inspector.get_columns("company_profile_aliases")
        }
        with engine.connect() as connection:
            profile_count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM company_profiles"
            ).scalar_one()
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "company_profile_aliases" in table_names
    assert {"identity_key", "official_website_domain", "evidence"} <= profile_columns
    assert "alias_key" in alias_columns
    assert profile_count == 0
    assert version == CURRENT_SCHEMA_REVISION


def test_collapsed_capture_migration_upgrades_directly_from_0017(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'page-capture-queue.db'}"
    create_sqlite_schema_at_revision(database_url, COMPANY_PROFILES_REVISION)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        capture_columns = {column["name"] for column in inspector.get_columns("page_capture_runs")}
        reprocess_columns = {
            column["name"] for column in inspector.get_columns("job_reprocess_runs")
        }
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "background_worker_leases" in tables
    assert {"snapshot_id", "identity_hash", "claim_token"} <= capture_columns
    assert "worker_id" in reprocess_columns
    assert version == CURRENT_SCHEMA_REVISION
    assert (
        sqlite_schema_mismatches(
            tmp_path / "page-capture-queue.db",
            {
                "page_capture_snapshots",
                "page_capture_runs",
                "page_capture_snapshot_scopes",
                "page_capture_subject_heads",
                "job_source_identities",
            },
        )
        == []
    )


def test_capture_compatibility_schema_is_frozen_to_revision_0021():
    tables = capture_revision_0021_tables()

    assert tables["page_capture_runs"] is not Base.metadata.tables["page_capture_runs"]
    assert {column.name for column in tables["page_capture_runs"].columns} == {
        "capture_id",
        "snapshot_id",
        "provider",
        "identity_hash",
        "identity_strength",
        "public_source_url",
        "target_job_id",
        "job_id",
        "skip_language_filter",
        "page_access_verdict",
        "page_kind",
        "identity_verdict",
        "association_verdict",
        "assessment_evidence_code",
        "status",
        "attempts",
        "next_attempt_at",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
        "retryable",
        "worker_id",
        "claim_token",
        "supervisor_lease_token",
        "result",
        "retry_of_capture_id",
        "created_at",
        "updated_at",
    }
    assert {index.name for index in tables["job_source_identities"].indexes} == {
        "ix_job_source_identities_job_id",
        "ix_job_source_identities_provider",
        "ix_job_source_identities_identity_hash",
        "ix_job_source_identities_identity_strength",
        "ix_job_source_identities_provider_identity",
    }


def test_collapsed_capture_migration_resumes_partial_sqlite_ddl(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'partial-capture-migration.db'}"
    create_sqlite_schema_at_revision(database_url, COMPANY_PROFILES_REVISION)
    maybe_stamp_existing_sqlite_schema(database_url, config_for_database(database_url))
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX IF EXISTS uq_job_reprocess_runs_active_job")
            connection.exec_driver_sql(
                "ALTER TABLE job_reprocess_runs ADD COLUMN next_attempt_at DATETIME"
            )
    finally:
        engine.dispose()

    command.upgrade(config_for_database(database_url), "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {
            "next_attempt_at",
            "error_code",
            "retryable",
            "worker_id",
            "claim_token",
        } <= {column["name"] for column in inspector.get_columns("job_reprocess_runs")}
        assert "uq_job_reprocess_runs_active_job" in {
            index["name"] for index in inspector.get_indexes("job_reprocess_runs")
        }
        assert "page_capture_snapshots" in inspector.get_table_names()
    finally:
        engine.dispose()


def test_unversioned_schema_shapes_have_no_provisional_capture_revisions():
    config = config_for_database("sqlite:///unused.db")
    shapes = {
        revision: allowed_missing
        for revision, _tables, allowed_missing, _views in recoverable_sqlite_schema_shapes(config)
    }

    assert "20260803_0018" not in shapes
    assert "20260803_0019" not in shapes
    assert "20260803_0020" not in shapes
    assert shapes[CAPTURE_EXECUTION_MODEL_REVISION] == {
        "generated_documents": {"editable_artifact_id"},
        "job_postings": {
            "status_last_open_at",
            "status_last_open_evidence_code",
            "status_source_repair_required",
        },
        "job_status_checks": {
            "candidate_kind",
            "diagnostics",
            "final_url",
            "requested_url",
        },
    }


def install_future_capture_orm_metadata(monkeypatch) -> None:
    future_metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(future_metadata)
    future_metadata.tables["page_capture_runs"].append_column(
        sa.Column("future_capture_column", sa.String(length=64), nullable=True)
    )
    monkeypatch.setattr(
        migrate_module,
        "Base",
        SimpleNamespace(metadata=future_metadata),
    )


def test_local_provisional_0021_schema_is_backed_up_and_converted(
    tmp_path: Path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'provisional-capture.db'}"
    config = config_for_database(database_url)
    command.upgrade(config, CAPTURE_EXECUTION_MODEL_REVISION)
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    with session_scope(settings) as db:
        for artifact_id in ("artifact_raw", "artifact_archive"):
            db.add(
                StoredArtifact(
                    artifact_id=artifact_id,
                    artifact_type="job_snapshot",
                    storage_backend="local",
                    storage_uri=f"local://test/{artifact_id}",
                    original_file_name=f"{artifact_id}.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    sha256=(artifact_id[-1] * 64),
                    privacy_class="private",
                )
            )
        db.flush()
        for suffix in ("a", "b"):
            artifact_id = f"artifact_duplicate_identity_{suffix}"
            db.add(
                StoredArtifact(
                    artifact_id=artifact_id,
                    artifact_type="job_snapshot",
                    storage_backend="local",
                    storage_uri=f"local://test/{suffix}",
                    original_file_name=f"{suffix}.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    sha256=(suffix * 64),
                    privacy_class="private",
                )
            )
            db.flush()
            insert_legacy_job_posting(
                db,
                id=f"job_duplicate_identity_{suffix}",
                source_type="paste",
                source_url="https://www.linkedin.com/jobs/view/1234567890",
                posting_snapshot_hash=(suffix * 64),
                raw_content_artifact_id=artifact_id,
            )
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                for table_name in (
                    "page_capture_subject_heads",
                    "page_capture_snapshot_scopes",
                    "page_capture_runs",
                    "page_capture_snapshots",
                    "job_source_identities",
                ):
                    connection.exec_driver_sql(f"DROP TABLE {table_name}")
                connection.exec_driver_sql(
                    """
                    CREATE TABLE page_capture_runs (
                        capture_id VARCHAR(64) PRIMARY KEY,
                        capture_key VARCHAR(2110) NOT NULL,
                        source_url VARCHAR(2048) NOT NULL,
                        source_identity_url VARCHAR(2048),
                        page_title VARCHAR(512) NOT NULL,
                        target_job_id VARCHAR(64),
                        job_id VARCHAR(64),
                        raw_content_artifact_id VARCHAR(64) NOT NULL,
                        source_archive_artifact_id VARCHAR(64) NOT NULL,
                        content_hash VARCHAR(64) NOT NULL,
                        deduplication_key VARCHAR(160) NOT NULL,
                        skip_language_filter BOOLEAN NOT NULL,
                        status VARCHAR(64) NOT NULL,
                        attempts INTEGER NOT NULL,
                        next_attempt_at DATETIME,
                        started_at DATETIME,
                        finished_at DATETIME,
                        error_code VARCHAR(128),
                        error_message TEXT,
                        retryable BOOLEAN NOT NULL,
                        worker_id VARCHAR(64),
                        claim_token VARCHAR(64),
                        result JSON NOT NULL,
                        retry_of_capture_id VARCHAR(64),
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_provisional_capture_status ON page_capture_runs (status)"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO page_capture_runs (
                        capture_id, capture_key, source_url, source_identity_url,
                        page_title, raw_content_artifact_id,
                        source_archive_artifact_id, content_hash,
                        deduplication_key, skip_language_filter, status, attempts,
                        retryable, result, created_at, updated_at
                    ) VALUES (
                        'capture_provisional', 'url:linkedin',
                        'https://www.linkedin.com/jobs/view/1234567890',
                        'https://www.linkedin.com/jobs/view/1234567890',
                        'Intern', 'artifact_raw', 'artifact_archive',
                        'content_hash', 'content_hash', 0, 'queued', 0, 0, '{}',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO page_capture_runs (
                        capture_id, capture_key, source_url, source_identity_url,
                        page_title, raw_content_artifact_id,
                        source_archive_artifact_id, content_hash,
                        deduplication_key, skip_language_filter, status, attempts,
                        started_at, retryable, result, created_at, updated_at
                    ) VALUES (
                        'capture_exhausted_running', 'url:linkedin:exhausted',
                        'https://www.linkedin.com/jobs/view/1234567891',
                        'https://www.linkedin.com/jobs/view/1234567891',
                        'Expired worker', 'artifact_raw', 'artifact_archive',
                        'exhausted_content_hash', 'exhausted_content_hash', 0,
                        'running', 3, CURRENT_TIMESTAMP, 1, '{}',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO page_capture_runs (
                        capture_id, capture_key, source_url, source_identity_url,
                        page_title, raw_content_artifact_id,
                        source_archive_artifact_id, content_hash,
                        deduplication_key, skip_language_filter, status, attempts,
                        finished_at, retryable, result, created_at, updated_at
                    ) VALUES (
                        'capture_retry_root', 'url:retry-root',
                        'https://www.linkedin.com/jobs/view/2234567890',
                        'https://www.linkedin.com/jobs/view/2234567890',
                        'Retry root', 'artifact_raw', 'artifact_archive',
                        'retry_content_hash', 'retry_content_hash', 0, 'failed', 1,
                        CURRENT_TIMESTAMP, 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO page_capture_runs (
                        capture_id, capture_key, source_url, source_identity_url,
                        page_title, raw_content_artifact_id,
                        source_archive_artifact_id, content_hash,
                        deduplication_key, skip_language_filter, status, attempts,
                        retryable, result, retry_of_capture_id, created_at, updated_at
                    ) VALUES (
                        'capture_retry_run', 'url:retry-root',
                        'https://www.linkedin.com/jobs/view/2234567890',
                        'https://www.linkedin.com/jobs/view/2234567890',
                        'Retry root', 'artifact_raw', 'artifact_archive',
                        'retry_content_hash', 'retry_content_hash', 0, 'queued', 0,
                        0, '{}', 'capture_retry_root', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
    finally:
        engine.dispose()

    install_future_capture_orm_metadata(monkeypatch)
    backup_path = maybe_convert_provisional_capture_schema(database_url)

    assert backup_path is not None and backup_path.exists()
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {
            "page_capture_snapshots",
            "page_capture_runs",
            "page_capture_snapshot_scopes",
            "page_capture_subject_heads",
            "job_source_identities",
        } <= set(inspector.get_table_names())
        assert "snapshot_id" in {
            column["name"] for column in inspector.get_columns("page_capture_runs")
        }
        with engine.connect() as connection:
            converted = connection.exec_driver_sql(
                "SELECT capture_id, status, page_access_verdict, page_kind, "
                "identity_verdict, association_verdict FROM page_capture_runs "
                "WHERE capture_id = 'capture_provisional'"
            ).one()
            assert tuple(converted) == (
                "capture_provisional",
                "queued",
                "unassessed",
                "unknown",
                "pending",
                "pending",
            )
            exhausted = connection.exec_driver_sql(
                "SELECT status, attempts, finished_at, error_code "
                "FROM page_capture_runs "
                "WHERE capture_id = 'capture_exhausted_running'"
            ).one()
            assert exhausted.status == "failed"
            assert exhausted.attempts == 3
            assert exhausted.finished_at is not None
            assert exhausted.error_code == "page_capture_worker_stale"
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM page_capture_subject_heads"
                ).scalar_one()
                == 3
            )
            retry_snapshots = connection.exec_driver_sql(
                "SELECT capture_id, snapshot_id FROM page_capture_runs "
                "WHERE capture_id IN ('capture_retry_root', 'capture_retry_run') "
                "ORDER BY capture_id"
            ).all()
            assert retry_snapshots[0].snapshot_id == retry_snapshots[1].snapshot_id

            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM job_source_identities WHERE provider = 'linkedin'"
                ).scalar_one()
                == 2
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()


def test_local_boolean_capture_verdict_is_replaced_by_explicit_verdicts(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'boolean-capture-verdict.db'}"
    command.upgrade(
        config_for_database(database_url),
        CAPTURE_EXECUTION_MODEL_REVISION,
    )
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE page_capture_runs ADD COLUMN "
                "identity_accepted BOOLEAN NOT NULL DEFAULT 0"
            )
    finally:
        engine.dispose()

    backup_path = maybe_convert_provisional_capture_schema(database_url)

    assert backup_path is not None and backup_path.exists()
    engine = create_engine(database_url)
    try:
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("page_capture_runs")
        }
        assert "identity_accepted" not in columns
        assert {
            "page_access_verdict",
            "page_kind",
            "identity_verdict",
            "association_verdict",
            "assessment_evidence_code",
        } <= columns
    finally:
        engine.dispose()


def test_local_legacy_capture_identity_registry_is_backed_up_and_converted(
    tmp_path: Path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'legacy-capture-identities.db'}"
    command.upgrade(
        config_for_database(database_url),
        CAPTURE_EXECUTION_MODEL_REVISION,
    )
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    with session_scope(settings) as db:
        db.add(
            StoredArtifact(
                artifact_id="artifact_legacy_identity",
                artifact_type="job_snapshot",
                storage_backend="local",
                storage_uri="local://job-snapshots/legacy.txt",
                original_file_name="legacy.txt",
                content_type="text/plain",
                size_bytes=1,
                sha256="0" * 64,
                privacy_class="private",
            )
        )
        db.flush()
        insert_legacy_job_posting(
            db,
            id="job_legacy_identity",
            source_type="paste",
            source_url="https://www.linkedin.com/feed/",
            posting_snapshot_hash="1" * 64,
            raw_content_artifact_id="artifact_legacy_identity",
            company_name="Acme",
            normalized_title="backend_intern",
        )
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                indexes = list(
                    connection.exec_driver_sql(
                        "PRAGMA index_list(job_source_identities)"
                    ).mappings()
                )
                for index in indexes:
                    if str(index["origin"]) == "c":
                        connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index["name"]}"')
                connection.exec_driver_sql("DROP TABLE job_source_identities")
                connection.exec_driver_sql(
                    """
                    CREATE TABLE job_source_identities (
                        job_source_identity_id VARCHAR(64) PRIMARY KEY,
                        job_id VARCHAR(64) NOT NULL,
                        provider VARCHAR(64) NOT NULL,
                        identity_hash VARCHAR(64) NOT NULL,
                        public_source_url VARCHAR(2048),
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        UNIQUE (provider, identity_hash),
                        FOREIGN KEY(job_id) REFERENCES job_postings(id)
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO job_source_identities (
                        job_source_identity_id, job_id, provider, identity_hash,
                        public_source_url, created_at, updated_at
                    ) VALUES (
                        'identity_legacy_non_job', 'job_legacy_identity',
                        'linkedin', 'legacy-provider-only-hash',
                        'https://www.linkedin.com/feed/',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
    finally:
        engine.dispose()

    install_future_capture_orm_metadata(monkeypatch)
    backup_path = maybe_convert_provisional_capture_schema(database_url)

    assert backup_path is not None and backup_path.exists()
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("job_source_identities")}
        assert {"source_kind", "match_priority"} <= columns
        unique_columns = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("job_source_identities")
        }
        assert ("job_id", "provider", "identity_hash") in unique_columns
        assert ("provider", "identity_hash") not in unique_columns
        with engine.connect() as connection:
            strength = connection.exec_driver_sql(
                "SELECT identity_strength FROM job_source_identities "
                "WHERE job_source_identity_id = 'identity_legacy_non_job'"
            ).scalar_one()
            assert strength == "weak"
    finally:
        engine.dispose()


def test_migration_wrapper_upgrades_existing_baseline_sqlite_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'baseline-jobcockpit.db'}"
    create_baseline_sqlite_schema(database_url)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {"job_ingestion_runs", "job_reprocess_runs"} <= set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("job_alert_links")}
        assert {"raw_text", "surrounding_text"} <= columns
        reprocess_columns = {
            column["name"] for column in inspector.get_columns("job_reprocess_runs")
        }
        assert "type" in reprocess_columns
        views = set(inspector.get_view_names())
        assert "active_job_postings" in views
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert version == CURRENT_SCHEMA_REVISION
    assert "active_job_postings" in views


def test_migration_wrapper_upgrades_existing_job_ingestion_sqlite_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'job-ingestion-jobcockpit.db'}"
    create_sqlite_schema_at_revision(database_url, JOB_INGESTION_REVISION)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "job_reprocess_runs" in set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("job_alert_links")}
        assert {"raw_text", "surrounding_text"} <= columns
        reprocess_columns = {
            column["name"] for column in inspector.get_columns("job_reprocess_runs")
        }
        assert "type" in reprocess_columns
        views = set(inspector.get_view_names())
        assert "active_job_postings" in views
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert version == CURRENT_SCHEMA_REVISION
    assert "active_job_postings" in views


def test_migration_wrapper_upgrades_existing_job_reprocess_sqlite_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'job-reprocess-jobcockpit.db'}"
    create_sqlite_schema_at_revision(database_url, JOB_REPROCESS_REVISION)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        reprocess_columns = {
            column["name"] for column in inspector.get_columns("job_reprocess_runs")
        }
        assert "type" in reprocess_columns
        views = set(inspector.get_view_names())
        assert "active_job_postings" in views
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert version == CURRENT_SCHEMA_REVISION
    assert "active_job_postings" in views


def test_migration_wrapper_upgrades_unversioned_active_view_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'active-view-jobcockpit.db'}"
    create_sqlite_schema_at_revision(database_url, ACTIVE_JOB_POSTINGS_REVISION)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "job_ingestion_items" in set(inspector.get_table_names())
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()

    assert version == CURRENT_SCHEMA_REVISION


def test_migration_wrapper_upgrades_unversioned_ingestion_items_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'ingestion-items-jobcockpit.db'}"
    create_sqlite_schema_at_revision(database_url, JOB_INGESTION_ITEMS_REVISION)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "opportunity_status" in job_columns
    assert version == CURRENT_SCHEMA_REVISION


@pytest.mark.parametrize("keep_empty_alembic_version", [False, True])
def test_migration_wrapper_upgrades_unversioned_status_reconciliation_schema(
    tmp_path: Path,
    keep_empty_alembic_version: bool,
):
    database_url = f"sqlite:///{tmp_path / 'status-reconciliation-jobcockpit.db'}"
    create_sqlite_schema_at_revision(
        database_url,
        JOB_STATUS_RECONCILIATION_REVISION,
        keep_empty_alembic_version=keep_empty_alembic_version,
    )

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        generated_document_columns = {
            column["name"] for column in inspector.get_columns("generated_documents")
        }
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()

    assert "generation_provider" in generated_document_columns
    assert version == CURRENT_SCHEMA_REVISION


def test_migration_wrapper_rejects_baseline_sqlite_schema_drift(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'drifted-baseline-jobcockpit.db'}"
    create_baseline_sqlite_schema(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE audit_log_entries RENAME TO audit_log_entries_old"
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE audit_log_entries (
                    audit_log_entry_id VARCHAR(64) NOT NULL,
                    event_type VARCHAR(128) NOT NULL,
                    entity_type VARCHAR(128) NOT NULL,
                    entity_id VARCHAR(64) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT pk_audit_log_entries PRIMARY KEY (audit_log_entry_id)
                )
                """
            )
            connection.exec_driver_sql("DROP TABLE audit_log_entries_old")
    finally:
        engine.dispose()

    config = config_for_database(database_url)
    with pytest.raises(RuntimeError, match="payload"):
        maybe_stamp_existing_sqlite_schema(database_url, config)


def test_migration_wrapper_recovers_empty_alembic_version_table(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'empty-version-jobcockpit.db'}"
    create_baseline_sqlite_schema(database_url, keep_empty_alembic_version=True)

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "job_reprocess_runs" in set(inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("job_alert_links")}
        assert {"raw_text", "surrounding_text"} <= columns
        reprocess_columns = {
            column["name"] for column in inspector.get_columns("job_reprocess_runs")
        }
        assert "type" in reprocess_columns
        assert "active_job_postings" in set(inspector.get_view_names())
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert version == CURRENT_SCHEMA_REVISION


def test_migration_wrapper_stamps_current_sqlite_schema(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'current-jobcockpit.db'}"
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        create_analytics_views(engine)
    finally:
        engine.dispose()

    config = config_for_database(database_url)
    maybe_stamp_existing_sqlite_schema(database_url, config)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        views = set(inspector.get_view_names())
        with engine.connect() as connection:
            version = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
    finally:
        engine.dispose()
    assert version == CURRENT_SCHEMA_REVISION
    assert "active_job_postings" in views


def test_reconciliation_requires_migration_without_materializing_future_tables(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'pre-status-reconciliation.db'}"
    create_sqlite_schema_at_revision(
        database_url,
        JOB_OPPORTUNITY_STATUS_REVISION,
    )
    config = config_for_database(database_url)
    command.stamp(config, JOB_OPPORTUNITY_STATUS_REVISION)
    settings = Settings(
        database_url=database_url,
        artifact_root=tmp_path / "private" / "storage",
        posting_rendered_fallback_provider="disabled",
    )

    with pytest.raises(RuntimeError, match=r"python scripts/migrate\.py"):
        run_status_reconciliation(settings, dry_run=True)

    assert not {
        "job_status_scan_runs",
        "job_status_checks",
    }.intersection(sqlite_tables(tmp_path / "pre-status-reconciliation.db"))

    command.upgrade(config, "head")
    with create_engine(database_url).connect() as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == CURRENT_SCHEMA_REVISION


def test_migration_wrapper_rejects_current_sqlite_schema_drift(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'drifted-current-jobcockpit.db'}"
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        create_analytics_views(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE job_ingestion_runs RENAME TO job_ingestion_runs_old"
            )
            connection.exec_driver_sql(
                """
                CREATE TABLE job_ingestion_runs (
                    ingestion_run_id VARCHAR(64) NOT NULL,
                    provider VARCHAR(64) NOT NULL,
                    source_url VARCHAR(2048),
                    search_text VARCHAR(255) NOT NULL,
                    status VARCHAR(64) NOT NULL,
                    alert_level VARCHAR(32) NOT NULL,
                    jobs_seen_count INTEGER NOT NULL,
                    jobs_created_count INTEGER NOT NULL,
                    jobs_duplicate_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    error_message TEXT,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT pk_job_ingestion_runs PRIMARY KEY (ingestion_run_id)
                )
                """
            )
            connection.exec_driver_sql("DROP TABLE job_ingestion_runs_old")
    finally:
        engine.dispose()

    config = config_for_database(database_url)
    with pytest.raises(RuntimeError, match="request_payload"):
        maybe_stamp_existing_sqlite_schema(database_url, config)


def test_migration_wrapper_rejects_full_running_status_unique_index(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'invalid-status-run-index.db'}"
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        create_analytics_views(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX uq_job_status_scan_runs_running")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_job_status_scan_runs_running "
                "ON job_status_scan_runs (status)"
            )
    finally:
        engine.dispose()

    config = config_for_database(database_url)
    with pytest.raises(RuntimeError, match="WHERE predicate mismatch"):
        maybe_stamp_existing_sqlite_schema(database_url, config)


def test_initial_migration_drops_email_processing_states_before_referenced_tables():
    source = Path("apps/api/alembic/versions/20260602_0001_initial.py").read_text(encoding="utf-8")

    processing_state_drop = source.index('op.drop_table("job_alert_email_processing_states")')
    email_run_drop = source.index('op.drop_table("email_ingestion_runs")')
    source_drop = source.index('op.drop_table("job_alert_sources")')

    assert processing_state_drop < email_run_drop
    assert processing_state_drop < source_drop


def test_default_extension_token_secret_is_generated_and_persisted(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        ats_mock_port=8001,
    )

    secret = settings.get_extension_token_secret()
    reloaded = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        ats_mock_port=8001,
    ).get_extension_token_secret()

    assert secret
    assert secret != "local-dev-extension-token"
    assert reloaded == secret
    assert (tmp_path / "private" / "extension-token-secret").read_text(encoding="utf-8") == secret


def test_models_use_portable_json_type():
    source = Path("apps/api/jobcockpit/db/models.py").read_text(encoding="utf-8")

    assert "sqlalchemy.dialects.sqlite import JSON" not in source
    assert "from sqlalchemy import" in source
    assert " JSON," in source


def test_readme_quick_start_runs_alembic_before_api_server():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "mkdir -p var" in readme
    assert readme.index("pnpm --silent python scripts/migrate.py") < readme.index(
        "pnpm --silent python scripts/run_uvicorn_dev.py jobcockpit.api.main:app"
    )


def test_readme_shell_examples_match_declared_fences():
    readme = Path("README.md").read_text(encoding="utf-8")

    for fence in readme.split("```bash")[1:]:
        block = fence.split("```", 1)[0]
        assert "curl.exe" not in block
        assert " ^" not in block
    assert "```powershell\nInvoke-RestMethod" in readme


def test_ci_runs_alembic_upgrade_before_backend_tests():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run alembic upgrade head" in workflow
    assert workflow.index("uv run alembic upgrade head") < workflow.index("uv run pytest")
