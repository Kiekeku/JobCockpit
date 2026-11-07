import json
from types import SimpleNamespace

import httpx

from jobcockpit import cli
from jobcockpit.core.config import Settings
from jobcockpit.ingestion.output import normalize_ats_ingestion, normalize_gmail_ingestion
from jobcockpit.integrations.gmail_processing import _gmail_link_item


def test_fallback_import_is_successful_in_standard_output():
    output = normalize_gmail_ingestion(
        [
            {
                "source_id": "alertsrc_1",
                "email_ingestion_run_id": "emailrun_1",
                "jobs_created_count": 1,
                "jobs_reused_count": 0,
                "items": [
                    {
                        "item_id": "link_1",
                        "status": "fallback_imported",
                        "linked_job_id": "job_1",
                    }
                ],
            }
        ],
        [],
    )

    assert output["status"] == "completed"
    assert output["totals"]["items_failed_count"] == 0


def test_terminal_status_is_counted_as_a_skipped_ingestion_item():
    output = normalize_gmail_ingestion(
        [
            {
                "source_id": "alertsrc_terminal",
                "email_ingestion_run_id": "emailrun_terminal",
                "jobs_created_count": 0,
                "jobs_reused_count": 0,
                "items": [
                    {
                        "item_id": "link_terminal",
                        "status": "terminal_status_detected",
                        "linked_job_id": None,
                    }
                ],
            }
        ],
        [],
    )

    assert output["status"] == "completed"
    assert output["totals"]["items_skipped_count"] == 1
    assert output["totals"]["items_failed_count"] == 0


def test_ats_item_count_comes_from_emitted_items_not_provider_discovery_count():
    output = normalize_ats_ingestion(
        {
            "provider": "hanzilla",
            "source_url": "https://jobs.hanzilla.co/internships/",
            "jobs_seen_count": 3,
            "jobs_created_count": 0,
            "jobs_duplicate_count": 0,
            "ingestion_run": {"ingestion_run_id": "run_1", "status": "completed"},
            "items": [
                {
                    "ingestion_item_id": "item_1",
                    "source_url": "https://example.test/job/1",
                    "status": "ignored",
                }
            ],
        }
    )

    assert output["totals"]["items_seen_count"] == 1
    assert output["totals"]["items_skipped_count"] == 1
    assert output["runs"][0]["metadata"]["provider_jobs_seen_count"] == 3


def test_source_failure_does_not_invent_a_failed_item():
    output = normalize_gmail_ingestion(
        [],
        [{"source_id": "alertsrc_failed", "error": "provider unavailable"}],
    )

    assert output["status"] == "failed"
    assert output["totals"]["items_seen_count"] == 0
    assert output["totals"]["items_failed_count"] == 0
    assert output["totals"]["runs_failed_count"] == 1


def test_gmail_standard_items_redact_email_derived_values():
    private_url = (
        "https://example.test/preferences/manage?email=user%40example.invalid&token=secret"
    )
    output = normalize_gmail_ingestion(
        [
            {
                "source_id": "alertsrc_1",
                "email_ingestion_run_id": "emailrun_1",
                "jobs_created_count": 0,
                "jobs_reused_count": 0,
                "items": [
                    {
                        "item_id": "link_1",
                        "source_url": private_url,
                        "resolved_url": private_url,
                        "canonical_url": private_url,
                        "status": "ignored",
                        "error_message": f"ignored private URL {private_url}",
                        "metadata": {"message_id": "email_1", "raw_value": private_url},
                    }
                ],
            }
        ],
        [],
    )

    serialized = json.dumps(output)
    assert "user%40example.invalid" not in serialized
    assert "token=secret" not in serialized
    assert output["runs"][0]["items"][0] == {
        "item_id": "link_1",
        "source_url": None,
        "resolved_url": None,
        "canonical_url": None,
        "status": "ignored",
        "linked_job_id": None,
        "error_message": None,
        "retryable": False,
        "metadata": {},
    }


def test_gmail_processing_result_redacts_email_derived_values_at_source():
    private_url = "https://example.test/apply?email=user%40example.invalid&token=secret"
    item = _gmail_link_item(
        SimpleNamespace(
            job_alert_link_id="link_1",
            original_url=private_url,
            resolved_url=private_url,
            canonical_url=private_url,
            ingestion_status="needs_external_resolution",
            linked_job_id=None,
            error_message=f"failed URL {private_url}",
        )
    )

    assert item == {
        "item_id": "link_1",
        "source_url": None,
        "resolved_url": None,
        "canonical_url": None,
        "status": "needs_external_resolution",
        "linked_job_id": None,
        "error_message": None,
        "retryable": True,
        "metadata": {},
    }


def test_ats_http_failure_emits_standard_json(monkeypatch, capsys):
    request = httpx.Request("POST", "http://127.0.0.1:8000/api/jobs/ingest/ats")
    response = httpx.Response(503, request=request, json={"detail": "provider unavailable"})

    def fail_post(*args, **kwargs):
        raise httpx.HTTPStatusError("failed", request=request, response=response)

    monkeypatch.setattr(cli.httpx, "post", fail_post)

    exit_code = cli.main(["ats", "ingest-hanzilla", "--json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["provider"] == "hanzilla"
    assert payload["totals"]["runs_failed_count"] == 1
    assert payload["totals"]["items_failed_count"] == 0


def test_ats_invalid_json_response_emits_standard_json(monkeypatch, capsys):
    request = httpx.Request("POST", "http://127.0.0.1:8000/api/jobs/ingest/ats")
    response = httpx.Response(200, request=request, content=b"<html>not json</html>")
    monkeypatch.setattr(cli.httpx, "post", lambda *args, **kwargs: response)

    exit_code = cli.main(["ats", "ingest-hanzilla", "--json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["provider"] == "hanzilla"
    assert payload["totals"]["runs_failed_count"] == 1
    assert payload["runs"][0]["alerts"] == ["ATS API returned invalid JSON"]


def test_ats_non_object_json_response_emits_standard_json(monkeypatch, capsys):
    request = httpx.Request("POST", "http://127.0.0.1:8000/api/jobs/ingest/ats")
    response = httpx.Response(200, request=request, json=[])
    monkeypatch.setattr(cli.httpx, "post", lambda *args, **kwargs: response)

    exit_code = cli.main(["ats", "ingest-hanzilla", "--json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["provider"] == "hanzilla"
    assert payload["totals"]["runs_failed_count"] == 1
    assert payload["runs"][0]["alerts"] == ["ATS API returned a non-object JSON response"]


def test_gmail_unknown_source_emits_standard_json(tmp_path, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="synthetic",
    )

    exit_code = cli.main(
        ["gmail", "process", "--source-id", "alertsrc_missing", "--json"],
        settings=settings,
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["provider"] == "gmail"
    assert payload["totals"]["runs_failed_count"] == 1
    assert payload["runs"][0]["source_id"] == "alertsrc_missing"
    assert payload["runs"][0]["alerts"] == ["Job alert source not found: alertsrc_missing"]
