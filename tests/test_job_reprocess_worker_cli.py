import json
from pathlib import Path

from jobcockpit import cli
from jobcockpit.api.background_worker_leases import (
    acquire_background_worker_lease,
    release_background_worker_lease,
)
from jobcockpit.core.config import Settings
from jobcockpit.db.session import init_database


def _assert_ingestion_summary(payload, *, provider, seen, created, reused):
    assert payload["schema_version"] == 1
    assert payload["operation"] == "job_ingestion"
    assert payload["provider"] == provider
    assert payload["totals"]["items_seen_count"] == seen
    assert payload["totals"]["jobs_created_count"] == created
    assert payload["totals"]["jobs_reused_count"] == reused


def test_reprocess_worker_once_command_uses_unified_dispatcher(tmp_path: Path, monkeypatch, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )

    def fake_process_next_background_job(
        worker_settings, storage, *, worker_id, supervisor_lease_token
    ):
        assert worker_settings is settings
        assert storage.root == settings.artifact_root
        assert worker_id.startswith("worker_")
        assert supervisor_lease_token.startswith("lease_")
        return {
            "kind": "page_capture",
            "run": {
                "capture_id": "capture_123",
                "status": "succeeded",
            },
        }

    monkeypatch.setattr(cli, "process_next_background_job", fake_process_next_background_job)

    exit_code = cli.main(["worker", "reprocess", "--once", "--json"], settings=settings)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "processed_count": 1,
        "work": {
            "kind": "page_capture",
            "run": {
                "capture_id": "capture_123",
                "status": "succeeded",
            },
        },
    }


def test_cover_letter_worker_once_uses_dedicated_dispatcher(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )

    def fake_process_next_cover_letter_generation(
        worker_settings, storage, *, worker_id, supervisor_lease_token
    ):
        assert worker_settings is settings
        assert storage.root == settings.artifact_root
        assert worker_id.startswith("cover_worker_")
        assert supervisor_lease_token.startswith("lease_")
        return {
            "generation_run_id": "cover_letter_123",
            "job_id": "job_123",
            "status": "succeeded",
        }

    monkeypatch.setattr(
        cli,
        "process_next_cover_letter_generation",
        fake_process_next_cover_letter_generation,
    )

    exit_code = cli.main(["worker", "cover-letter", "--once", "--json"], settings=settings)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "processed_count": 1,
        "work": {
            "generation_run_id": "cover_letter_123",
            "job_id": "job_123",
            "status": "succeeded",
        },
    }


def test_cover_letter_worker_loop_recovers_iteration_error_and_continues(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    process_calls = 0
    recovery_calls = []

    def fake_process_next_cover_letter_generation(
        _worker_settings, _storage, *, worker_id, supervisor_lease_token
    ):
        nonlocal process_calls
        process_calls += 1
        if process_calls == 1:
            raise RuntimeError("transient iteration failure")
        if process_calls == 2:
            return {
                "generation_run_id": "cover_letter_recovered",
                "job_id": "job_recovered",
                "status": "succeeded",
            }
        raise KeyboardInterrupt

    def fake_recover(_settings, **kwargs):
        recovery_calls.append(kwargs)
        return 1

    monkeypatch.setattr(
        cli,
        "process_next_cover_letter_generation",
        fake_process_next_cover_letter_generation,
    )
    monkeypatch.setattr(cli, "recover_stale_cover_letter_generation_runs", fake_recover)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    exit_code = cli.main(
        ["worker", "cover-letter", "--poll-seconds", "0.01", "--json"],
        settings=settings,
    )

    assert exit_code == 0
    assert process_calls == 3
    assert len(recovery_calls) == 1
    assert recovery_calls[0]["recover_owned_by"].startswith("cover_worker_")
    assert recovery_calls[0]["required_worker_id"] == recovery_calls[0]["recover_owned_by"]
    assert recovery_calls[0]["required_lease_token"].startswith("lease_")
    assert json.loads(capsys.readouterr().out) == {
        "processed_count": 1,
        "work": {
            "generation_run_id": "cover_letter_recovered",
            "job_id": "job_recovered",
            "status": "succeeded",
        },
    }


def test_ats_ingest_hanzilla_command_posts_default_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 5,
                "jobs_created_count": 3,
                "jobs_duplicate_count": 2,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-hanzilla", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "hanzilla",
                "source_url": "https://jobs.hanzilla.co/internships/",
                "search_text": "",
                "limit": 25,
                "skip_interest_filter": False,
                "skip_language_filter": False,
            },
            "timeout": 14400,
        }
    ]
    payload = json.loads(capsys.readouterr().out)
    _assert_ingestion_summary(payload, provider="hanzilla", seen=5, created=3, reused=2)


def test_ats_ingest_eluta_command_posts_default_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 7,
                "jobs_created_count": 4,
                "jobs_duplicate_count": 3,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-eluta", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "eluta",
                "source_url": (
                    "https://www.eluta.ca/search?q=co-op+field%3A%22Computing+-+Software+%26+Web+Development%22&l=&qc="
                ),
                "search_text": "",
                "limit": 25,
                "skip_interest_filter": True,
                "skip_language_filter": False,
            },
            "timeout": 14400,
        }
    ]
    payload = json.loads(capsys.readouterr().out)
    _assert_ingestion_summary(payload, provider="eluta", seen=7, created=4, reused=3)


def test_ats_ingest_csjobs_command_posts_full_public_preview_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 75,
                "jobs_created_count": 70,
                "jobs_duplicate_count": 5,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-csjobs", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "csjobs",
                "source_url": "https://csjobs.ca/jobs",
                "search_text": "",
                "limit": 75,
                "skip_interest_filter": True,
                "skip_language_filter": True,
            },
            "timeout": 14400,
        }
    ]
    _assert_ingestion_summary(
        json.loads(capsys.readouterr().out),
        provider="csjobs",
        seen=75,
        created=70,
        reused=5,
    )


def test_ats_ingest_canadian_tech_command_posts_default_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 9,
                "jobs_created_count": 6,
                "jobs_duplicate_count": 3,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-canadian-tech", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "canadian_tech_internships",
                "source_url": "https://github.com/negarprh/Canadian-Tech-Internships-2026?tab=readme-ov-file",
                "search_text": "software",
                "limit": 30,
                "skip_interest_filter": False,
                "skip_language_filter": False,
            },
            "timeout": 14400,
        }
    ]
    payload = json.loads(capsys.readouterr().out)
    _assert_ingestion_summary(
        payload, provider="canadian_tech_internships", seen=9, created=6, reused=3
    )


def test_ats_ingest_canada_tech_2027_command_posts_default_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 1,
                "jobs_created_count": 1,
                "jobs_duplicate_count": 0,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-canada-tech-2027", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "canada_tech_internships_summer_2027",
                "source_url": "https://github.com/michelleokolie/canada-tech-internships-summer-2027",
                "search_text": "software",
                "limit": 30,
                "skip_interest_filter": False,
                "skip_language_filter": False,
            },
            "timeout": 14400,
        }
    ]


def test_ats_ingest_canadian_tech_2027_command_posts_default_payload(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 2,
                "jobs_created_count": 2,
                "jobs_duplicate_count": 0,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-canadian-tech-2027", "--json"])

    assert exit_code == 0
    assert calls == [
        {
            "url": "http://127.0.0.1:8000/api/jobs/ingest/ats",
            "json": {
                "provider": "canadian_tech_internships_2027",
                "source_url": (
                    "https://github.com/negarprh/"
                    "Canadian-Tech-Internships-2027?tab=readme-ov-file"
                ),
                "search_text": "software",
                "limit": 30,
                "skip_interest_filter": False,
                "skip_language_filter": False,
            },
            "timeout": 14400,
        }
    ]


def test_ats_ingest_eluta_command_accepts_timeout_seconds(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jobs_seen_count": 1, "jobs_created_count": 1, "jobs_duplicate_count": 0}

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["ats", "ingest-eluta", "--timeout-seconds", "1200"])

    assert exit_code == 0
    assert calls[0]["timeout"] == 1200


def test_ats_ingest_eluta_command_rejects_nonpositive_timeout(capsys):
    exit_code = cli.main(["ats", "ingest-eluta", "--timeout-seconds", "0"])

    assert exit_code == 2
    assert "--timeout-seconds must be greater than zero" in capsys.readouterr().err


def test_ats_ingest_eluta_command_prints_eluta_summary(monkeypatch, capsys):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 2,
                "jobs_created_count": 1,
                "jobs_duplicate_count": 1,
                "link_failures": [
                    {"category": "permanent"},
                    {"category": "retryable"},
                    {"category": "technical"},
                ],
            }

    monkeypatch.setattr(cli.httpx, "post", lambda *args, **kwargs: FakeResponse())

    exit_code = cli.main(["ats", "ingest-eluta"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "Eluta import complete: 2 item(s) seen, 1 job(s) created, 1 job(s) reused, "
        "0 item(s) skipped, 0 item(s) needing review, 3 item(s) failed.\n"
    )


def test_ats_ingest_hanzilla_command_accepts_filter_options(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jobs_seen_count": 1, "jobs_created_count": 1, "jobs_duplicate_count": 0}

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(
        [
            "ats",
            "ingest-hanzilla",
            "--api-base-url",
            "http://localhost:8123/",
            "--search-text",
            "python",
            "--limit",
            "5",
            "--skip-interest-filter",
            "--skip-language-filter",
        ]
    )

    assert exit_code == 0
    assert calls[0]["url"] == "http://localhost:8123/api/jobs/ingest/ats"
    assert calls[0]["json"] == {
        "provider": "hanzilla",
        "source_url": "https://jobs.hanzilla.co/internships/",
        "search_text": "python",
        "limit": 5,
        "skip_interest_filter": True,
        "skip_language_filter": True,
    }
def test_reprocess_worker_once_respects_api_supervisor_lease(
    tmp_path: Path, monkeypatch, capsys
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
    )
    init_database(settings)
    api_worker_id = "worker_api_owner"
    assert acquire_background_worker_lease(settings, api_worker_id)

    def must_not_process(*_args, **_kwargs):
        raise AssertionError("CLI worker must not bypass the API supervisor lease")

    monkeypatch.setattr(cli, "process_next_background_job", must_not_process)
    try:
        exit_code = cli.main(
            ["worker", "reprocess", "--once", "--json"],
            settings=settings,
        )
    finally:
        release_background_worker_lease(settings, api_worker_id)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "processed_count": 0,
        "work": None,
        "worker_busy": True,
    }
