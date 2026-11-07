import sqlite3
from pathlib import Path

from sqlalchemy import select

from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobAlertSource
from jobcockpit.db.session import init_database, session_scope
from scripts import setup_gmail_ingestion
from scripts.setup_gmail_ingestion import (
    ensure_client_secret_placeholder,
    main,
    update_env_file,
    upsert_gmail_source,
)


def test_update_env_file_adds_gmail_settings_without_removing_ports(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOBCOCKPIT_API_PORT=8010\n"
        "JOBCOCKPIT_WEB_PORT=3010\n",
        encoding="utf-8",
    )

    update_env_file(
        env_file,
        {
            "JOBCOCKPIT_GMAIL_PROVIDER": "google",
            "JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH": "var/private/gmail/client_secret.json",
        },
    )

    content = env_file.read_text(encoding="utf-8")
    assert "JOBCOCKPIT_API_PORT=8010" in content
    assert "JOBCOCKPIT_WEB_PORT=3010" in content
    assert "JOBCOCKPIT_GMAIL_PROVIDER=google" in content
    assert (
        "JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH=var/private/gmail/client_secret.json"
        in content
    )


def test_update_env_file_replaces_existing_gmail_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOBCOCKPIT_GMAIL_PROVIDER=synthetic\n"
        "JOBCOCKPIT_API_PORT=8010\n",
        encoding="utf-8",
    )

    update_env_file(env_file, {"JOBCOCKPIT_GMAIL_PROVIDER": "google"})

    content = env_file.read_text(encoding="utf-8")
    assert "JOBCOCKPIT_GMAIL_PROVIDER=google" in content
    assert "JOBCOCKPIT_GMAIL_PROVIDER=synthetic" not in content
    assert "JOBCOCKPIT_API_PORT=8010" in content


def test_ensure_client_secret_placeholder_does_not_overwrite_existing_file(tmp_path: Path):
    secret_path = tmp_path / "var" / "private" / "gmail" / "client_secret.json"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text('{"installed": {"client_secret": "real-ish"}}\n', encoding="utf-8")

    created = ensure_client_secret_placeholder(secret_path)

    assert created is False
    assert "real-ish" in secret_path.read_text(encoding="utf-8")


def test_upsert_gmail_source_creates_then_updates_existing_source(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    init_database(settings)

    created = upsert_gmail_source(
        settings=settings,
        display_name="MapleRadar jobs",
        gmail_query="from:jobs@mapleradar.com newer_than:30d",
        sender_allowlist=["mapleradar.com"],
    )
    updated = upsert_gmail_source(
        settings=settings,
        display_name="MapleRadar alerts",
        gmail_query="from:jobs@mapleradar.com newer_than:30d",
        sender_allowlist=["mapleradar.com"],
    )

    assert created["created"] is True
    assert updated["created"] is False
    assert created["job_alert_source_id"] == updated["job_alert_source_id"]
    assert updated["display_name"] == "MapleRadar alerts"

    with session_scope(settings) as db:
        sources = [
            (source.job_alert_source_id, source.enabled)
            for source in db.execute(select(JobAlertSource)).scalars()
        ]
    assert sources == [(created["job_alert_source_id"], True)]


def test_upsert_gmail_source_reports_duplicate_existing_sources(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add_all(
            [
                JobAlertSource(
                    job_alert_source_id="alertsrc_a",
                    candidate_id="candidate_local",
                    provider="gmail",
                    display_name="Old MapleRadar",
                    gmail_query="from:jobs@mapleradar.com newer_than:30d",
                    sender_allowlist=["old.example"],
                    enabled=False,
                ),
                JobAlertSource(
                    job_alert_source_id="alertsrc_b",
                    candidate_id="candidate_local",
                    provider="gmail",
                    display_name="Duplicate MapleRadar",
                    gmail_query="from:jobs@mapleradar.com newer_than:30d",
                    sender_allowlist=["mapleradar.com"],
                    enabled=True,
                ),
            ]
        )

    result = upsert_gmail_source(
        settings=settings,
        display_name="MapleRadar jobs",
        gmail_query="from:jobs@mapleradar.com newer_than:30d",
        sender_allowlist=["mapleradar.com"],
    )

    assert result["created"] is False
    assert result["job_alert_source_id"] == "alertsrc_a"
    assert result["duplicate_source_ids"] == ["alertsrc_b"]
    assert result["display_name"] == "MapleRadar jobs"


def test_main_uses_selected_env_file_for_database(tmp_path: Path, monkeypatch, capsys):
    default_db = tmp_path / "default.db"
    selected_db = tmp_path / "selected.db"
    default_env = tmp_path / ".env"
    selected_env = tmp_path / "selected.env"
    default_env.write_text(f"JOBCOCKPIT_DATABASE_URL=sqlite:///{default_db}\n", encoding="utf-8")
    selected_env.write_text(f"JOBCOCKPIT_DATABASE_URL=sqlite:///{selected_db}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        setup_gmail_ingestion.Settings,
        "model_config",
        {**setup_gmail_ingestion.Settings.model_config, "env_file": str(default_env)},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "setup_gmail_ingestion.py",
            "--sender",
            "jobs@mapleradar.com",
            "--env-file",
            str(selected_env),
        ],
    )

    exit_code = main()

    assert exit_code == 0
    assert "configured" in capsys.readouterr().out
    with session_scope(Settings(database_url=f"sqlite:///{selected_db}")) as db:
        selected_queries = [
            source.gmail_query for source in db.execute(select(JobAlertSource)).scalars()
        ]
    assert not sqlite_table_exists(default_db, "job_alert_sources")
    assert selected_queries == ["from:jobs@mapleradar.com newer_than:30d"]


def sqlite_table_exists(path: Path, table_name: str) -> bool:
    if not path.exists():
        return False
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    return row is not None
