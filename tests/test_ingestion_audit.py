import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from jobcockpit import cli
from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, EmailIngestionRun, JobAlertSource
from jobcockpit.db.session import get_engine, init_database, session_scope
from jobcockpit.ingestion.audit import IngestionAuditService, empty_ingestion_counts


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )


def test_shared_ingestion_audit_shape_redacts_gmail_source_values(tmp_path):
    settings = _settings(tmp_path)
    init_database(settings)
    audit = IngestionAuditService(settings)
    private_url = "https://mail.example.test/link?email=user%40example.invalid&token=secret"

    with session_scope(settings) as db:
        audit.record(
            db,
            lifecycle="started",
            provider="workday",
            run_id="ingestrun_1",
            source_url="https://example.test/jobs",
            status="running",
            counts=empty_ingestion_counts(),
        )
        audit.record(
            db,
            lifecycle="started",
            provider="gmail",
            run_id="emailrun_1",
            source_id="alertsrc_1",
            source_url=private_url,
            status="running",
            counts=empty_ingestion_counts(),
        )

    with session_scope(settings) as db:
        rows = [
            (row.event_type, row.payload)
            for row in db.execute(
                select(AuditLogEntry).order_by(AuditLogEntry.created_at)
            ).scalars()
        ]

    assert [event_type for event_type, _payload in rows] == [
        "ingestion.run_started",
        "ingestion.run_started",
    ]
    assert (
        set(rows[0][1])
        == set(rows[1][1])
        == {
            "schema_version",
            "operation",
            "provider",
            "run_id",
            "source_id",
            "source_url",
            "status",
            "counts",
            "error_code",
        }
    )
    assert rows[0][1]["source_url"] == "https://example.test/jobs"
    assert rows[1][1]["source_url"] is None
    serialized = json.dumps(rows[1][1])
    assert "user%40example.invalid" not in serialized
    assert "token=secret" not in serialized


def test_gmail_failed_audit_survives_processing_rollback(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_failure_audit",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Broken alerts",
                gmail_query="from:broken.invalid newer_than:7d",
                sender_allowlist=["broken.invalid"],
                enabled=True,
            )
        )

    def fail_after_write(**kwargs):
        kwargs["source"].display_name = "must roll back"
        raise RuntimeError("private token=secret")

    monkeypatch.setattr(cli, "process_gmail_source", fail_after_write)

    exit_code = cli.main(
        ["gmail", "process", "--source-id", "alertsrc_failure_audit", "--json"],
        settings=settings,
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    with session_scope(settings) as db:
        source_display_name = db.get(JobAlertSource, "alertsrc_failure_audit").display_name
        rows = [
            (row.event_type, row.entity_id, row.payload)
            for row in db.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "ingestion_run")
                .order_by(AuditLogEntry.created_at)
            ).scalars()
        ]

    assert source_display_name == "Broken alerts"
    assert [event_type for event_type, _entity_id, _payload in rows] == [
        "ingestion.run_started",
        "ingestion.run_failed",
    ]
    assert rows[0][1] == rows[1][1]
    assert output["runs"][0]["run_id"] == rows[1][1]
    assert rows[1][2]["provider"] == "gmail"
    assert rows[1][2]["source_id"] == "alertsrc_failure_audit"
    assert rows[1][2]["counts"]["runs_failed_count"] == 1
    assert rows[1][2]["error_code"] == "unexpected_error"
    assert "token=secret" not in json.dumps(rows[1][2])


def test_gmail_api_commit_failure_records_durable_failed_audit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_database(settings)
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Synthetic alerts",
            "gmail_query": "from:alerts.invalid newer_than:7d",
            "sender_allowlist": ["alerts.invalid"],
        },
    ).json()
    preview = client.post(
        "/api/job-alerts/gmail/preview",
        json={"source_id": source["job_alert_source_id"]},
    )
    assert preview.status_code == 200

    real_record = IngestionAuditService.record
    completion_session = None

    def track_completion_session(audit, session, **kwargs):
        nonlocal completion_session
        if kwargs["lifecycle"] == "completed":
            completion_session = session
        return real_record(audit, session, **kwargs)

    failed_processing_commit = False
    gmail_import_route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/job-alerts/gmail/import"
    )
    db_dependency = next(
        dependency.call
        for dependency in gmail_import_route.dependant.dependencies
        if dependency.name == "db"
    )
    session_factory = sessionmaker(bind=get_engine(settings), autoflush=False)

    def failing_get_db():
        nonlocal failed_processing_commit
        db = session_factory()
        real_commit = db.commit

        def fail_processing_commit_once():
            nonlocal failed_processing_commit
            if db is completion_session and not failed_processing_commit:
                failed_processing_commit = True
                raise RuntimeError("simulated Gmail transaction commit failure")
            return real_commit()

        db.commit = fail_processing_commit_once
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(IngestionAuditService, "record", track_completion_session)
    app.dependency_overrides[db_dependency] = failing_get_db

    response = client.post(
        "/api/job-alerts/gmail/import",
        json={
            "source_id": source["job_alert_source_id"],
            "approved_message_review_ids": [preview.json()["messages"][0]["message_review_id"]],
        },
    )

    assert failed_processing_commit is True
    assert response.status_code == 500
    with session_scope(settings) as db:
        runs = list(db.execute(select(EmailIngestionRun)).scalars())
        assert len(runs) == 1
        assert runs[0].status == "failed"
        events = [
            (row.event_type, row.entity_id, row.payload)
            for row in db.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.entity_type == "ingestion_run")
                .order_by(AuditLogEntry.created_at)
            ).scalars()
        ]

    assert [event_type for event_type, _entity_id, _payload in events] == [
        "ingestion.run_started",
        "ingestion.run_failed",
    ]
    assert events[0][1] == events[1][1]
    assert events[1][2]["error_code"] == "unexpected_error"
