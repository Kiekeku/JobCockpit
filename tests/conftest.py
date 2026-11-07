import os
import re
import shutil
import socket
from pathlib import Path
from uuid import uuid4

import _pytest.tmpdir
import pytest
from fastapi.testclient import TestClient

from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.session import init_database


def finish_generation(client, response):
    """Drive queued test work explicitly and read its real HTTP result endpoint."""
    if response.status_code != 202 or "run" not in response.json():
        return response
    from datetime import UTC, datetime
    from jobcockpit.db.models import CoverLetterGenerationRun
    from jobcockpit.db.session import session_scope
    from jobcockpit.services.workflows import process_next_cover_letter_generation

    payload = response.json()
    settings = client.app.state.settings
    for _ in range(12):
        with session_scope(settings) as db:
            run = db.get(CoverLetterGenerationRun, payload["run"]["generation_run_id"])
            if run.status in {"succeeded", "failed", "cancelled"}:
                break
            if run.status == "retry_scheduled":
                run.next_attempt_at = datetime.now(UTC)
        process_next_cover_letter_generation(settings, client.app.state.storage)
    return client.get(payload["result_url"])


_cleanup_dead_symlinks = _pytest.tmpdir.cleanup_dead_symlinks

PYTEST_ENVIRONMENT_OVERRIDES = {
    "JOBCOCKPIT_BROWSER_HEALTH_ENABLED": "false",
    "JOBCOCKPIT_PUBLICATION_ENABLED": "false",
    "JOBCOCKPIT_PUBLICATION_POLICY_PATH": "",
    "JOBCOCKPIT_CHROME_CDP_AUTO_START": "false",
    "JOBCOCKPIT_COVER_LETTER_GENERATOR_PROVIDER": "heuristic",
    "JOBCOCKPIT_FIT_ANALYSIS_PROVIDER": "disabled",
    "JOBCOCKPIT_GMAIL_PROVIDER": "synthetic",
    "JOBCOCKPIT_PLAYWRIGHT_AUTH_WALL_SETTLE_SECONDS": "0",
    "JOBCOCKPIT_PLAYWRIGHT_AUTH_WALL_WAIT_SECONDS": "0",
    "JOBCOCKPIT_POSTING_DIRECT_FETCH_TIMEOUT_SECONDS": "0.1",
    "JOBCOCKPIT_POSTING_PARSER_PROVIDER": "heuristic",
    "JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER": "disabled",
    "JOBCOCKPIT_POSTING_RESOLVER_PROVIDER": "disabled",
    "JOBCOCKPIT_POSTING_SEARCH_PROVIDER": "disabled",
}


def pytest_configure(config):
    os.environ.update(PYTEST_ENVIRONMENT_OVERRIDES)

    def cleanup_dead_symlinks_ignoring_windows_permission_error(root):
        try:
            _cleanup_dead_symlinks(root)
        except PermissionError:
            if os.name != "nt":
                raise

    _pytest.tmpdir.cleanup_dead_symlinks = cleanup_dead_symlinks_ignoring_windows_permission_error


@pytest.fixture
def tmp_path(request) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:80]
    path = (Path(".pytest-tmp") / f"{safe_name}-{uuid4().hex}").resolve()
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def jobcockpit_settings_factory(tmp_path):
    def factory(**overrides):
        values = {
            "publication_policy_path": None,
            "publication_write_token": None,
            "database_url": f"sqlite:///{tmp_path / 'jobcockpit.db'}",
            "artifact_root": tmp_path / "private" / "storage",
            "extension_token_secret": "test-secret",
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def jobcockpit_client_factory():
    def factory(settings: Settings) -> TestClient:
        init_database(settings)
        return TestClient(create_app(settings))

    return factory


@pytest.fixture
def allow_public_dns(monkeypatch):
    def allow(expected_hosts: str | set[str], *, ip: str = "20.12.34.56") -> None:
        hosts = {expected_hosts} if isinstance(expected_hosts, str) else set(expected_hosts)

        def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
            assert host in hosts
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    return allow
