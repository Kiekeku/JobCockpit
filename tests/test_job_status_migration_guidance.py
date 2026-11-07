from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from jobcockpit.core.config import Settings
from jobcockpit.db.session import get_engine
from jobcockpit.ingestion.status_reconciliation import status_report


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RECOVERY_AWARE_MIGRATION_COMMAND = "pnpm --silent python scripts/migrate.py"
RAW_ALEMBIC_COMMAND = "uv run alembic upgrade head"


def test_status_reconciliation_readme_uses_recovery_aware_migration_wrapper():
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Scheduled active-job status reconciliation", maxsplit=1)[1]
    section = section.split("### 6. Chrome Extension Setup", maxsplit=1)[0]

    assert RECOVERY_AWARE_MIGRATION_COMMAND in section
    assert RAW_ALEMBIC_COMMAND not in section


def test_status_reconciliation_schema_error_uses_recovery_aware_migration_wrapper(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'missing-status-schema.db'}",
        artifact_root=tmp_path / "private" / "storage",
    )

    try:
        with pytest.raises(RuntimeError) as error:
            status_report(settings)
    finally:
        get_engine(settings).dispose()

    message = str(error.value)
    assert RECOVERY_AWARE_MIGRATION_COMMAND in message
    assert RAW_ALEMBIC_COMMAND not in message


def test_status_reconciliation_rejects_pre_availability_schema_with_guidance(
    tmp_path: Path,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'pre-availability-schema.db'}",
        artifact_root=tmp_path / "private" / "storage",
    )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "20260818_0023")

    try:
        with pytest.raises(RuntimeError) as error:
            status_report(settings)
    finally:
        get_engine(settings).dispose()

    message = str(error.value)
    assert "status_last_open_at" in message
    assert "requested_url" in message
    assert RECOVERY_AWARE_MIGRATION_COMMAND in message
