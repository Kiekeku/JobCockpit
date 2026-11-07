import argparse
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy import select

from jobcockpit import cli
from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, JobAlertSource, JobPosting
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.integrations.gmail_live import (
    GmailProviderConfigurationError,
    GmailProviderOperationError,
    GoogleGmailMessageProvider,
)
from jobcockpit.integrations.gmail_processing import _live_gmail_exception_to_processing_error


@pytest.fixture
def gmail_read_clock(monkeypatch):
    from jobcockpit.integrations import gmail_live

    now = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(gmail_live.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gmail_live.time, "sleep", sleep)
    monkeypatch.setattr(gmail_live.random, "random", lambda: 0.5)
    monkeypatch.setattr(gmail_live, "_next_gmail_request_at", 0.0)
    return now, waits


@pytest.mark.parametrize("status,reason", [(403, "rateLimitExceeded"),
                                         (403, "userRateLimitExceeded"), (429, "quota"),
                                         (503, "backendError")])
def test_gmail_reads_retry_transient_errors_without_restarting_batch(gmail_read_clock,
                                                                   status, reason):
    from jobcockpit.integrations.gmail_live import _execute_gmail_read

    now, waits = gmail_read_clock
    attempts = []

    class Request:
        def execute(self):
            attempts.append(now[0])
            if len(attempts) <= 2:
                raise _provider_http_error(status, reason)
            return {"ok": True}

    assert _execute_gmail_read(Request(), Settings()) == {"ok": True}
    assert attempts == [0.0, 1.5, 4.0]
    assert waits == [1.5, 2.5]


@pytest.mark.parametrize("status,reason", [(401, "authError"), (403, "forbidden"),
                                         (403, "dailyLimitExceeded"), (400, "badRequest")])
def test_gmail_reads_do_not_retry_permanent_errors(gmail_read_clock, status, reason):
    from jobcockpit.integrations.gmail_live import _execute_gmail_read

    error = _provider_http_error(status, reason)

    class Request:
        def execute(self):
            raise error

    with pytest.raises(HttpError) as caught:
        _execute_gmail_read(Request(), Settings())
    assert caught.value is error
    assert gmail_read_clock[1] == []


def test_gmail_read_retries_are_bounded_and_respect_retry_after(gmail_read_clock):
    from jobcockpit.integrations.gmail_live import _execute_gmail_read

    error = _provider_http_error(403, "rateLimitExceeded")
    error.resp["retry-after"] = "10"
    attempts = []

    class Request:
        def execute(self):
            attempts.append(gmail_read_clock[0][0])
            raise error

    with pytest.raises(HttpError) as caught:
        _execute_gmail_read(Request(), Settings(gmail_read_max_retries=2))
    assert caught.value is error
    assert attempts == [0.0, 10.0, 20.0]


def test_gmail_read_does_not_retry_before_a_long_retry_after(gmail_read_clock):
    from jobcockpit.integrations.gmail_live import _execute_gmail_read

    error = _provider_http_error(429, "quota")
    error.resp["retry-after"] = "3600"

    class Request:
        def execute(self):
            raise error

    with pytest.raises(HttpError):
        _execute_gmail_read(Request(), Settings())
    assert gmail_read_clock[1] == []


def test_gmail_read_pacing_covers_pagination_and_each_message(gmail_read_clock):
    now, waits = gmail_read_clock
    calls = []
    error = _provider_http_error(403, "rateLimitExceeded")

    class Request:
        def __init__(self, stage, payload, fail_once=False):
            self.stage, self.payload, self.fail_once = stage, payload, fail_once

        def execute(self):
            calls.append((self.stage, now[0]))
            if self.fail_once:
                self.fail_once = False
                raise error
            return self.payload

    class Service:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            second = "pageToken" in kwargs
            payload = {"messages": [{"id": "second" if second else "first"}]}
            if not second:
                payload["nextPageToken"] = "page-2"
            return Request("list", payload)

        def get(self, *, id, **kwargs):
            return Request(id, _raw_gmail_message(id, "thread", id), id == "second")

    provider = GoogleGmailMessageProvider(credentials_loader=lambda _: object(),
                                         service_factory=lambda _: Service())
    from types import SimpleNamespace
    result = provider.fetch_messages(source=SimpleNamespace(gmail_query="from:example.invalid"),
                                     settings=Settings(gmail_max_results=1), max_results=2)
    assert [message["subject"] for message in result] == ["first", "second"]
    assert calls == [("list", 0.0), ("first", 1.0), ("list", 2.0),
                     ("second", 3.0), ("second", 4.5)]


def _provider_http_error(
    status: int,
    reason: str,
    *,
    provider_status: str = "PERMISSION_DENIED",
) -> HttpError:
    response = Response({"status": str(status), "reason": "provider failure"})
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": "failed for gmail-message-raw-id with access-token-secret",
                "status": provider_status,
                "errors": [
                    {
                        "reason": reason,
                        "message": "gmail-message-raw-id access-token-secret",
                    }
                ],
            }
        }
    ).encode("utf-8")
    return HttpError(
        response,
        content,
        uri="https://gmail.googleapis.invalid/message/gmail-message-raw-id?token=access-token-secret",
    )


def test_google_gmail_provider_fetches_messages_without_exposing_raw_ids(tmp_path):
    sender_email = "alerts" + "@jobs.example.invalid"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_max_results=1,
    )
    source = JobAlertSource(
        job_alert_source_id="alertsrc_live_provider",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Live alerts",
        gmail_query="from:jobs.example.invalid newer_than:7d",
        sender_allowlist=["jobs.example.invalid"],
        enabled=True,
    )
    provider = GoogleGmailMessageProvider(
        credentials_loader=lambda _settings: object(),
        service_factory=lambda _credentials: FakeGmailService(
            [
                {
                    "id": "gmail-message-raw-id",
                    "threadId": "gmail-thread-raw-id",
                    "internalDate": "1764720000000",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Python role"},
                            {"name": "From", "value": f"Jobs <{sender_email}>"},
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {
                                    "data": "UHl0aG9uIHJvbGUgaHR0cDovL2xvY2FsaG9zdDo4MDAxL2F0cy9nZW5lcmljL2FjbWUvam9icy8xMjM="
                                },
                            },
                            {
                                "mimeType": "text/html",
                                "body": {
                                    "data": "PGEgaHJlZj0iaHR0cDovL2xvY2FsaG9zdDo4MDAxL2F0cy9nZW5lcmljL2FjbWUvam9icy8xMjMiPkFwcGx5PC9hPg=="
                                },
                            },
                        ],
                    },
                    "snippet": "Python role",
                }
            ]
        ),
    )

    messages = provider.fetch_messages(source=source, settings=settings)

    assert len(messages) == 1
    message = messages[0]
    assert message["id_hash"] == hashlib.sha256("gmail-message-raw-id".encode("utf-8")).hexdigest()
    assert (
        message["thread_hash"] == hashlib.sha256("gmail-thread-raw-id".encode("utf-8")).hexdigest()
    )
    assert message["id"] == message["id_hash"]
    assert message["subject"] == "Python role"
    assert message["sender_domain"] == "jobs.example.invalid"
    assert message["sender_hash"] == hashlib.sha256(sender_email.encode("utf-8")).hexdigest()
    assert "localhost:8001" in message["plain_text"]
    assert "<a href=" in message["html"]
    assert "gmail-message-raw-id" not in json.dumps(message)


def test_google_gmail_provider_includes_spam_for_explicit_inbox_and_spam_query(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    source = JobAlertSource(
        job_alert_source_id="alertsrc_live_provider_spam",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="LinkedIn Job Alerts and Recommendations",
        gmail_query="from:jobalerts-noreply@linkedin.com {in:inbox in:spam}",
        sender_allowlist=["linkedin.com"],
        enabled=True,
    )
    service = FakeGmailService([])
    provider = GoogleGmailMessageProvider(
        credentials_loader=lambda _settings: object(),
        service_factory=lambda _credentials: service,
    )

    assert provider.fetch_messages(source=source, settings=settings) == []
    assert service.list_kwargs["q"] == source.gmail_query
    assert service.list_kwargs["includeSpamTrash"] is True


def test_google_gmail_provider_excludes_spam_without_explicit_spam_query(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    source = JobAlertSource(
        job_alert_source_id="alertsrc_live_provider_inbox_only",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Inbox alerts",
        gmail_query="from:jobs.example.invalid in:inbox",
        sender_allowlist=["jobs.example.invalid"],
        enabled=True,
    )
    service = FakeGmailService([])
    provider = GoogleGmailMessageProvider(
        credentials_loader=lambda _settings: object(),
        service_factory=lambda _credentials: service,
    )

    assert provider.fetch_messages(source=source, settings=settings) == []
    assert service.list_kwargs["includeSpamTrash"] is False


def test_google_gmail_provider_fetches_paginated_messages(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_max_results=1,
    )
    source = JobAlertSource(
        job_alert_source_id="alertsrc_live_provider_pages",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Live alerts",
        gmail_query="from:jobs.example.invalid newer_than:7d",
        sender_allowlist=["jobs.example.invalid"],
        enabled=True,
    )
    service = FakeGmailService(
        [
            _raw_gmail_message("gmail-page-1-message", "gmail-thread-1", "First role"),
            _raw_gmail_message("gmail-page-2-message", "gmail-thread-2", "Second role"),
        ],
        pages=[
            {"messages": [{"id": "gmail-page-1-message"}], "nextPageToken": "page-2"},
            {"messages": [{"id": "gmail-page-2-message"}]},
        ],
    )
    provider = GoogleGmailMessageProvider(
        credentials_loader=lambda _settings: object(),
        service_factory=lambda _credentials: service,
    )

    messages = provider.fetch_messages(source=source, settings=settings, max_results=2)

    assert [message["subject"] for message in messages] == ["First role", "Second role"]
    assert service.list_kwargs_by_call[0]["maxResults"] == 1
    assert service.list_kwargs_by_call[1]["pageToken"] == "page-2"


def test_google_gmail_provider_rejects_blank_query_before_mailbox_request(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    source = JobAlertSource(
        job_alert_source_id="alertsrc_live_provider_blank_query",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Live alerts",
        gmail_query="   ",
        sender_allowlist=["jobs.example.invalid"],
        enabled=True,
    )
    credentials_loaded = False

    def credentials_loader(_settings):
        nonlocal credentials_loaded
        credentials_loaded = True
        return object()

    provider = GoogleGmailMessageProvider(
        credentials_loader=credentials_loader,
        service_factory=lambda _credentials: FakeGmailService([]),
    )

    with pytest.raises(GmailProviderConfigurationError, match="query cannot be blank"):
        provider.fetch_messages(source=source, settings=settings)

    assert credentials_loaded is False


def test_cli_gmail_process_uses_google_provider_when_configured(tmp_path, monkeypatch, capsys):
    sender_email = "alerts" + "@jobs.example.invalid"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_google_cli",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Live alerts",
                gmail_query="from:jobs.example.invalid newer_than:7d",
                sender_allowlist=["jobs.example.invalid"],
                enabled=True,
            )
        )

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        lambda self, source, settings, max_results=None: [
            {
                "id": "live-message-hash",
                "id_hash": "live-message-hash",
                "thread_hash": "live-thread-hash",
                "subject": "Live Python role",
                "sender_hash": hashlib.sha256(sender_email.encode("utf-8")).hexdigest(),
                "sender_domain": "jobs.example.invalid",
                "received_at": "2026-06-03T12:00:00+00:00",
                "body_hash": hashlib.sha256("Live Python role".encode("utf-8")).hexdigest(),
                "plain_text": "Live Python role\nPython SQL",
                "html": '<a href="http://localhost:8001/ats/generic/acme/jobs/live">Apply</a>',
            }
        ],
    )

    exit_code = cli.main(["gmail", "process", "--provider", "google", "--json"], settings=settings)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["totals"]["jobs_created_count"] == 1
    assert payload["totals"]["items_seen_count"] == 1
    assert len(payload["runs"]) == 1
    assert len(payload["runs"][0]["metadata"]["processed_messages"]) == 1
    assert payload["runs"][0]["metadata"]["messages_needing_review_count"] == 0
    assert payload["runs"][0]["metadata"]["mailbox_mutations"] == []
    with session_scope(settings) as db:
        assert db.execute(select(JobPosting)).scalar_one().source_type == "gmail_alert"


def test_cli_gmail_process_reports_google_configuration_errors(tmp_path, capsys):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_oauth_token_path=tmp_path / "missing-token.json",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_google_cli_missing_token",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Live alerts",
                gmail_query="from:jobs.example.invalid newer_than:7d",
                sender_allowlist=["jobs.example.invalid"],
                enabled=True,
            )
        )

    exit_code = cli.main(["gmail", "process", "--provider", "google", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Gmail OAuth token not found" in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["source_id"] == "alertsrc_google_cli_missing_token"
    assert "Gmail OAuth token not found" in payload["runs"][0]["alerts"][0]


def test_cli_gmail_process_summary_reports_discovered_and_review_counts(capsys):
    payload = cli.normalize_gmail_ingestion(
        [
            {
                "jobs_created_count": 2,
                "jobs_discovered_count": 5,
                "jobs_reused_count": 3,
                "processed_messages": [{"processing_status": "processed"}],
                "messages_needing_review_count": 0,
                "unresolved_links_count": 3,
                "mailbox_mutations": [],
            },
            {
                "jobs_created_count": 1,
                "jobs_discovered_count": 2,
                "jobs_reused_count": 1,
                "processed_messages": [{"processing_status": "needs_review"}],
                "messages_needing_review_count": 1,
                "unresolved_links_count": 4,
                "mailbox_mutations": [],
            },
        ],
        [],
    )

    assert payload["totals"]["items_seen_count"] == 14
    assert payload["totals"]["jobs_reused_count"] == 4
    assert payload["totals"]["items_needing_review_count"] == 7
    cli._emit(argparse.Namespace(json=False), payload)

    captured = capsys.readouterr()
    assert "14 item(s) seen" in captured.out
    assert "3 job(s) created" in captured.out
    assert "4 job(s) reused" in captured.out
    assert "7 item(s) needing review" in captured.out


def test_cli_gmail_auth_reports_malformed_client_secret(tmp_path, capsys):
    client_secret_path = tmp_path / "client_secret.json"
    client_secret_path.write_text("{bad json", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_oauth_client_secret_path=client_secret_path,
    )

    exit_code = cli.main(["gmail", "auth", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Gmail OAuth client secret" in captured.err
    assert "unreadable or malformed" in captured.err
    assert "{bad json" not in captured.err
    assert captured.out == ""


def test_cli_gmail_process_reports_malformed_google_token(tmp_path, capsys):
    token_path = tmp_path / "token.json"
    token_path.write_text("{bad json", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_oauth_token_path=token_path,
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_google_cli_bad_token",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Live alerts",
                gmail_query="from:jobs.example.invalid newer_than:7d",
                sender_allowlist=["jobs.example.invalid"],
                enabled=True,
            )
        )

    exit_code = cli.main(["gmail", "process", "--provider", "google", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Gmail OAuth token" in captured.err
    assert "unreadable or malformed" in captured.err
    assert "{bad json" not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["source_id"] == "alertsrc_google_cli_bad_token"
    assert "unreadable or malformed" in payload["runs"][0]["alerts"][0]
    assert "{bad json" not in captured.out


def test_cli_gmail_process_reports_live_provider_errors_without_leaking_details(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    init_database(settings)
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id="alertsrc_google_cli_refresh_error",
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Live alerts",
                gmail_query="from:jobs.example.invalid newer_than:7d",
                sender_allowlist=["jobs.example.invalid"],
                enabled=True,
            )
        )

    def raise_refresh_error(self, source, settings, max_results=None):
        raise RefreshError("refresh failed for gmail-message-raw-id and access-token-secret")

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        raise_refresh_error,
    )

    exit_code = cli.main(["gmail", "process", "--provider", "google", "--json"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Gmail provider authentication failed" in captured.err
    assert "gmail-message-raw-id" not in captured.err
    assert "access-token-secret" not in captured.err
    assert "gmail-message-raw-id" not in captured.out
    assert "access-token-secret" not in captured.out
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["runs"][0]["source_id"] == "alertsrc_google_cli_refresh_error"
    assert payload["runs"][0]["alerts"] == [
        "Gmail provider authentication failed; run `pnpm --silent cli gmail auth` again."
    ]
    assert payload["runs"][0]["metadata"]["provider_diagnostics"] == {
        "error_code": "gmail_authentication_failed",
        "provider_stage": "provider_request",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("status", "reason", "provider_status", "expected_status", "expected_error_code", "retryable"),
    [
        (403, "userRateLimitExceeded", "RESOURCE_EXHAUSTED", 429, "gmail_rate_limited", True),
        (403, "accessNotConfigured", "PERMISSION_DENIED", 502, "gmail_api_unavailable", False),
        (403, "insufficientPermissions", "PERMISSION_DENIED", 502, "gmail_forbidden", False),
        (403, "futureProviderReason", "PERMISSION_DENIED", 502, "gmail_http_403", False),
        (401, "authError", "UNAUTHENTICATED", 502, "gmail_unauthorized", False),
    ],
)
def test_google_http_errors_emit_only_structured_sanitized_diagnostics(
    status,
    reason,
    provider_status,
    expected_status,
    expected_error_code,
    retryable,
):
    provider_error = GmailProviderOperationError(
        "messages_list",
        _provider_http_error(status, reason, provider_status=provider_status),
    )

    processing_error = _live_gmail_exception_to_processing_error(provider_error)

    assert processing_error is not None
    assert processing_error.status_code == expected_status
    assert processing_error.provider_diagnostics == {
        "error_code": expected_error_code,
        "provider_stage": "messages_list",
        "retryable": retryable,
        "http_status": status,
        "provider_reason": reason,
        "provider_status": provider_status,
    }
    serialized = json.dumps(
        {
            "detail": processing_error.detail,
            "diagnostics": processing_error.provider_diagnostics,
        }
    )
    assert "gmail-message-raw-id" not in serialized
    assert "access-token-secret" not in serialized


def test_cli_gmail_process_logs_provider_stage_and_reason_without_raw_error_details(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    init_database(settings)
    source_id = "alertsrc_google_cli_rate_limit"
    with session_scope(settings) as db:
        db.add(
            JobAlertSource(
                job_alert_source_id=source_id,
                candidate_id="candidate_local",
                provider="gmail",
                display_name="Live alerts",
                gmail_query="from:jobs.example.invalid newer_than:7d",
                sender_allowlist=["jobs.example.invalid"],
                enabled=True,
            )
        )

    def raise_rate_limit(self, source, settings, max_results=None):
        raise GmailProviderOperationError(
            "message_get",
            _provider_http_error(
                403,
                "userRateLimitExceeded",
                provider_status="RESOURCE_EXHAUSTED",
            ),
        )

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        raise_rate_limit,
    )

    exit_code = cli.main(
        ["gmail", "process", "--source-id", source_id, "--provider", "google", "--json"],
        settings=settings,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    diagnostics = payload["runs"][0]["metadata"]["provider_diagnostics"]
    assert exit_code == 1
    assert diagnostics == {
        "error_code": "gmail_rate_limited",
        "provider_stage": "message_get",
        "retryable": True,
        "http_status": 403,
        "provider_reason": "userRateLimitExceeded",
        "provider_status": "RESOURCE_EXHAUSTED",
    }
    assert 'diagnostics={"error_code": "gmail_rate_limited"' in captured.err
    assert "gmail-message-raw-id" not in captured.out
    assert "access-token-secret" not in captured.out
    assert "gmail-message-raw-id" not in captured.err
    assert "access-token-secret" not in captured.err
    with session_scope(settings) as db:
        failed_audit = db.execute(
            select(AuditLogEntry)
            .where(AuditLogEntry.event_type == "ingestion.run_failed")
            .order_by(AuditLogEntry.created_at.desc())
        ).scalar_one()
        failed_audit_payload = dict(failed_audit.payload)
    assert failed_audit_payload["error_code"] == "gmail_rate_limited"


def test_google_gmail_preview_returns_configuration_error_without_token(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_oauth_token_path=tmp_path / "missing-token.json",
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Live alerts",
            "gmail_query": "from:jobs.example.invalid newer_than:7d",
            "sender_allowlist": ["jobs.example.invalid"],
        },
    ).json()

    response = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )

    assert response.status_code == 400
    assert "Gmail OAuth token not found" in response.json()["detail"]


def test_google_gmail_preview_returns_configuration_error_for_malformed_token(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{bad json", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
        gmail_oauth_token_path=token_path,
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Live alerts",
            "gmail_query": "from:jobs.example.invalid newer_than:7d",
            "sender_allowlist": ["jobs.example.invalid"],
        },
    ).json()

    response = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Gmail OAuth token" in detail
    assert "unreadable or malformed" in detail
    assert "{bad json" not in detail


def test_google_gmail_preview_returns_sanitized_provider_error(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
        gmail_provider="google",
    )
    init_database(settings)
    client = TestClient(create_app(settings))
    source = client.post(
        "/api/job-alert-sources",
        json={
            "display_name": "Live alerts",
            "gmail_query": "from:jobs.example.invalid newer_than:7d",
            "sender_allowlist": ["jobs.example.invalid"],
        },
    ).json()

    def raise_refresh_error(self, source, settings, max_results=None):
        raise RefreshError("refresh failed for gmail-message-raw-id and access-token-secret")

    monkeypatch.setattr(
        "jobcockpit.integrations.gmail_live.GoogleGmailMessageProvider.fetch_messages",
        raise_refresh_error,
    )

    response = client.post(
        "/api/job-alerts/gmail/preview", json={"source_id": source["job_alert_source_id"]}
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "Gmail provider authentication failed" in detail
    assert "gmail-message-raw-id" not in detail
    assert "access-token-secret" not in detail


class FakeGmailService:
    def __init__(self, messages, pages=None):
        self._messages = {message["id"]: message for message in messages}
        self._pages = pages
        self.list_kwargs = None
        self.list_kwargs_by_call = []
        self._list_call_count = 0

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        self.list_kwargs_by_call.append(kwargs)
        if self._pages is not None:
            page = self._pages[self._list_call_count]
            self._list_call_count += 1
            return FakeRequest(page)
        return FakeRequest({"messages": [{"id": message_id} for message_id in self._messages]})

    def get(self, *, id, **kwargs):
        return FakeRequest(self._messages[id])


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def _raw_gmail_message(message_id: str, thread_id: str, subject: str) -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": "1764720000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Jobs <alerts" + "@jobs.example.invalid>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": "UHl0aG9uIHJvbGU="},
                },
            ],
        },
        "snippet": subject,
    }
