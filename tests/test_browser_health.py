from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobcockpit.ingestion import browser_health
from jobcockpit.ingestion.browser_health import observe, summarize, safe_posting_url
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, PostingResolution


def hint():
    return ExternalPostingHint(source_url="https://example.com/job?gh_jid=42&token=secret", source_kind="generic", title="Engineer")


@pytest.mark.parametrize("error, status, reason", [
    ("", "succeeded", "Page resolved"),
    ("playwright_timeout", "failed", "Timed out"),
    ("playwright_failed: empty_rendered_page", "failed", "Page body was empty"),
    ("playwright_low_confidence", "needs_review", "Page could not be confidently matched"),
    ("playwright_failed: blocked_rendered_page", "failed", "Site blocked access or needs verification"),
    ("playwright_failed: invisible_session_sign_in_required", "failed", "Site requires sign-in"),
    ("playwright_failed: rendered_http_error", "failed", "Site returned an HTTP error page"),
    ("playwright_failed: browser_proxy_capacity_exhausted", "failed", "Browser proxy connection capacity exhausted"),
])
def test_records_one_attempt_and_classifies_results(tmp_path, error, status, reason):
    resolution = PostingResolution(status="needs_review" if error else "resolved", error=error)
    result = observe(tmp_path, hint(), "ingestion", 30, lambda: resolution)
    assert result is resolution
    health = summarize(tmp_path)
    assert health["total"] == 1  # Start/completion are not double counted.
    assert health["counts"][status] == 1
    if error:
        assert health["recent_issues"][0]["reason"] == reason
        if "browser_proxy_capacity_exhausted" in error:
            assert health["recent_issues"][0]["error_code"] == "browser_proxy_capacity_exhausted"
    else:
        assert health["last_success_at"]
    assert "secret" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_running_attempt_visible_before_completion(tmp_path):
    def render():
        assert summarize(tmp_path)["counts"]["running"] == 1
        return PostingResolution(status="resolved")
    observe(tmp_path, hint(), "ingestion", 30, render)
    assert summarize(tmp_path)["counts"]["running"] == 0


def test_records_exception_without_changing_it_or_exposing_details(tmp_path):
    def fail():
        raise RuntimeError("private secret credential")
    with pytest.raises(RuntimeError, match="private secret"):
        observe(tmp_path, hint(), "availability_scan", 30, fail)
    health = summarize(tmp_path)
    assert health["counts"]["failed"] == 1
    assert "private secret" not in str(health)


def test_failure_records_allowlisted_code_without_raw_error(tmp_path):
    error = "playwright_failed: rendered_http_error; token=private-secret"
    observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
        status="needs_review", error=error,
    ))
    issue = summarize(tmp_path)["recent_issues"][0]
    assert issue["error_code"] == "rendered_http_error"
    assert "private-secret" not in str(issue)
    assert "private-secret" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_unknown_failure_does_not_expose_exception_text(tmp_path):
    observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
        status="needs_review", error="playwright_failed: private account secret",
    ))
    issue = summarize(tmp_path)["recent_issues"][0]
    assert issue["error_code"] == "playwright_failed"
    assert "private account secret" not in str(issue)


def test_nested_policy_code_is_preserved_without_policy_detail(tmp_path):
    observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
        status="needs_review", error="playwright_failed: rendered_url_policy_blocked: secret destination",
    ))
    issue = summarize(tmp_path)["recent_issues"][0]
    assert issue["error_code"] == "rendered_url_policy_blocked"
    assert "secret destination" not in str(issue)


def test_page_failure_context_excludes_session_identifiers(tmp_path):
    def render():
        browser_health.record_page_failure(
            "https://secure.indeed.com/auth;jsessionid=private-session?token=secret",
            "Error 400 Bad Request", "rendered_http_error",
        )
        return PostingResolution(status="needs_review", error="playwright_failed: rendered_http_error")

    observe(tmp_path, hint(), "ingestion", 30, render)
    issue = summarize(tmp_path)["recent_issues"][0]
    assert issue["signals"] == ["HTTP 400 at secure.indeed.com/auth"]
    assert "private-session" not in str(issue)
    assert "token=secret" not in str(issue)


def test_recovered_page_failure_does_not_mark_success_for_review(tmp_path):
    def render():
        browser_health.record_page_failure(
            "https://secure.indeed.com/auth;jsessionid=private-session",
            "Error 400 Bad Request", "rendered_http_error",
        )
        return PostingResolution(status="resolved")

    observe(tmp_path, hint(), "ingestion", 30, render)
    health = summarize(tmp_path)
    assert health["counts"]["succeeded"] == 1
    assert health["recent_issues"] == []


def test_sign_in_page_context_keeps_only_known_path(tmp_path):
    def render():
        browser_health.record_page_failure(
            "https://www.indeed.com/account/login;jsessionid=private-session?token=secret",
            "Sign in to a private account", "invisible_session_sign_in_required",
        )
        return PostingResolution(status="needs_review", error="playwright_failed: invisible_session_sign_in_required")

    observe(tmp_path, hint(), "ingestion", 30, render)
    issue = summarize(tmp_path)["recent_issues"][0]
    assert issue["error_code"] == "invisible_session_sign_in_required"
    assert issue["signals"] == ["Sign-in page at www.indeed.com/account/login"]
    assert "private-session" not in str(issue)
    assert "token=secret" not in str(issue)


def test_legacy_missing_completion_is_reconciled_and_history_window_is_bounded(tmp_path):
    now = datetime.now(UTC)
    event = dict(attempt_id="lost", provider="invisible_playwright", workflow="availability_scan",
                 source_url="https://example.com/job", title="Engineer", url_key="key", status="running",
                 reason="", started_at=(now-timedelta(minutes=5)).isoformat(), finished_at=None,
                 expected_by=(now-timedelta(minutes=1)).isoformat(), duration_seconds=None)
    browser_health._write(tmp_path, event)
    health = summarize(tmp_path, now=now)
    assert health["counts"]["overdue"] == 0
    assert health["counts"]["failed"] == 1
    issue = health["recent_issues"][0]
    assert issue["error_code"] == "browser_completion_missing"
    assert "ownership unknown" in issue["reason"]
    assert issue["finished_at"] == event["expected_by"]
    count = len(list(tmp_path.rglob("*.json")))
    assert summarize(tmp_path, now=now) == health
    assert len(list(tmp_path.rglob("*.json"))) == count
    assert summarize(tmp_path, now=now+timedelta(days=2))["total"] == 0


def test_live_attempt_is_not_reconciled_even_after_budget(tmp_path):
    def render():
        health = summarize(tmp_path, now=datetime.now(UTC) + timedelta(hours=1))
        assert health["counts"]["overdue"] == 1
        assert health["counts"]["failed"] == 0
        return PostingResolution(status="resolved")

    observe(tmp_path, hint(), "ingestion", 30, render)
    assert summarize(tmp_path)["counts"]["succeeded"] == 1


def test_dead_owner_is_reconciled_before_budget_and_late_completion_wins(tmp_path):
    event = {}

    def render():
        rows, _, _ = browser_health._recent_attempts(tmp_path, datetime.now(UTC))
        event.update(rows[0])
        return PostingResolution(status="resolved")

    observe(tmp_path, hint(), "ingestion", 1800, render)
    # Simulate a killed worker: only its start record and released lease survive.
    for path in tmp_path.rglob("*.json"):
        if '"status": "succeeded"' in path.read_text(encoding="utf-8"):
            path.unlink()
    health = summarize(tmp_path)
    assert health["counts"]["failed"] == 1
    assert health["counts"]["overdue"] == 0
    assert health["recent_issues"][0]["error_code"] == "browser_attempt_interrupted"
    event.update(status="succeeded", reason="Page resolved", finished_at=datetime.now(UTC).isoformat())
    browser_health._write(tmp_path, event)
    health = summarize(tmp_path)
    assert health["counts"]["succeeded"] == 1
    assert health["counts"]["failed"] == 0
    assert health["total"] == 1


def test_unreadable_owner_does_not_claim_interruption(tmp_path, monkeypatch):
    def render():
        def unavailable(*args):
            raise OSError("unavailable")
        monkeypatch.setattr(browser_health, "_owner_has_exited", unavailable)
        health = summarize(tmp_path)
        assert health["counts"]["running"] == 1
        assert health["incomplete"]
        return PostingResolution(status="resolved")
    observe(tmp_path, hint(), "ingestion", 30, render)


def test_killed_browser_owner_is_reconciled_across_processes(tmp_path):
    import json
    import os
    import subprocess
    import sys

    program = f"import sys; sys.path[:] = {json.dumps(sys.path)}\n" + """
import sys
from pathlib import Path
from jobcockpit.ingestion.browser_health import observe
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint
def wait():
    print('ready', flush=True)
    sys.stdin.read(1)
observe(Path(sys.argv[1]), ExternalPostingHint(
    source_url='https://example.com/job', source_kind='generic', title='Engineer'
), 'ingestion', 1800, wait)
"""
    child = subprocess.Popen(
        # Windows venv python.exe is a launcher; kill the actual owner, rather
        # than just that launcher while its interpreter child keeps running.
        [sys._base_executable, "-c", program, str(tmp_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "apps/api")},
    )
    try:
        # No stdin is sent: the attempt stays live until killed below.
        import threading
        ready = []
        reader = threading.Thread(target=lambda: ready.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=20)
        assert ready == [b"ready\r\n"] or ready == [b"ready\n"]
        assert summarize(tmp_path)["counts"]["running"] == 1
        child.kill()
        child.wait(timeout=20)
        health = summarize(tmp_path)
        assert health["counts"]["failed"] == 1
        assert health["counts"]["overdue"] == 0
        assert health["recent_issues"][0]["error_code"] == "browser_attempt_interrupted"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)


def test_successful_retry_clears_reconciled_interruption_before_old_deadline(tmp_path):
    now = datetime.now(UTC)
    browser_health._write(tmp_path, dict(
        attempt_id="old", provider="invisible_playwright", workflow="ingestion",
        source_url=hint().source_url, title="Engineer", url_key=browser_health.url_key(hint().source_url),
        status="failed", reason="Interrupted", error_code="browser_attempt_interrupted",
        started_at=(now-timedelta(minutes=20)).isoformat(),
        finished_at=(now-timedelta(minutes=10)).isoformat(), reconciled_at=now.isoformat(),
        expected_by=(now-timedelta(minutes=10)).isoformat(), duration_seconds=None,
    ))
    _timed_attempt(tmp_path, now, "retry", "succeeded", minutes_ago=15)
    health = summarize(tmp_path, now=now)
    assert health["active_issue_counts"]["failed"] == 0
    assert health["recovered_failure_count"] == 1


def test_telemetry_write_failure_does_not_fail_ingestion(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(browser_health, "_write", fail)
    resolution = PostingResolution(status="resolved")
    assert observe(tmp_path, hint(), "ingestion", 30, lambda: resolution) is resolution


def test_recovery_diagnostics_are_allowlisted_and_do_not_turn_success_into_an_issue(tmp_path):
    def recovered():
        browser_health.record_session_recovery("preparation_granted")
        browser_health.record_session_recovery("recheck_resolved")
        browser_health.record_session_recovery("secret https://user:password@example.test/")
        return PostingResolution(status="resolved")

    observe(tmp_path, hint(), "availability_scan", 120, recovered)
    health = summarize(tmp_path)
    assert health["counts"]["succeeded"] == 1
    assert health["recent_issues"] == []
    rows, _, incomplete = browser_health._recent_attempts(tmp_path, datetime.now(UTC))
    assert not incomplete
    assert rows[0]["session_recovery"] == ["preparation_granted", "recheck_resolved"]
    assert "password" not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    observe(tmp_path, hint(), "availability_scan", 120, lambda: PostingResolution(status="resolved"))
    rows, _, _ = browser_health._recent_attempts(tmp_path, datetime.now(UTC))
    assert sum("session_recovery" in row for row in rows) == 1


def test_safe_posting_links_exclude_credentials_and_unsafe_schemes():
    assert safe_posting_url("javascript:alert(1)") == ""
    assert safe_posting_url("https://user:secret@example.test/job?gh_jid=42&token=secret#x") == "https://example.test/job?gh_jid=42"


@pytest.mark.parametrize("url, expected", [
    ("https://careers.ibm.com/careers?jobId=42&source=tracking", "https://careers.ibm.com/careers?jobId=42"),
    ("https://recruiting.ultipro.ca/example/OpportunityDetail?opportunityId=example-id&postingId=other-id&token=secret", "https://recruiting.ultipro.ca/example/OpportunityDetail?opportunityId=example-id&postingId=other-id"),
    ("https://job-boards.greenhouse.io/embed/job_app?for=example&token=42&validityToken=secret", "https://job-boards.greenhouse.io/embed/job_app?for=example&token=42"),
    ("https://greenhouse.io.example.test/embed/job_app?for=example&token=secret", "https://greenhouse.io.example.test/embed/job_app"),
])
def test_health_links_preserve_provider_posting_identifiers(url, expected):
    assert safe_posting_url(url) == expected


@pytest.mark.parametrize("legacy_link", [False, True])
def test_health_job_link_uses_original_identity_after_url_sanitization(jobcockpit_settings_factory, jobcockpit_client_factory, monkeypatch, legacy_link):
    settings = jobcockpit_settings_factory(browser_health_enabled=True)
    url = "https://careers.ibm.com/careers?jobId=42&source=tracking"
    with jobcockpit_client_factory(settings) as client:
        response = client.post("/api/jobs/ingest", json={
            "source_type": "paste", "source_url": url,
            "raw_text": "Software Engineer\nCompany: Example\nLocation: Montreal\nBuild Python software.",
        })
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        with monkeypatch.context() as patch:
            if legacy_link:
                patch.setattr(browser_health, "safe_posting_url", lambda value: value.split("?")[0])
            observe(settings.artifact_root, ExternalPostingHint(source_url=url, source_kind="generic"),
                    "availability_scan", 20, lambda: PostingResolution(status="needs_review", error="playwright_timeout"))
        issue = client.get("/api/browser-health").json()["recent_issues"][0]
    assert issue["job_id"] == job_id
    assert issue["source_url"] == "https://careers.ibm.com/careers?jobId=42"
    assert "url_key" not in issue


def test_health_endpoint_reads_durable_results(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(browser_health_enabled=True)
    observe(settings.artifact_root, hint(), "availability_scan", 20, lambda: PostingResolution(
        status="needs_review", error="playwright_timeout",
    ))
    with jobcockpit_client_factory(settings) as client:
        response = client.get("/api/browser-health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["counts"]["failed"] == 1
    assert payload["recent_issues"][0]["workflow"] == "availability_scan"
    assert payload["recent_issues"][0]["error_code"] == "playwright_timeout"
    assert "url_key" not in payload["recent_issues"][0]


def _timed_attempt(root, now, attempt_id, status, *, url=None, minutes_ago=3,
                   finished_minutes_ago=None, workflow="ingestion", include_key=True):
    url = url or hint().source_url
    started = now - timedelta(minutes=minutes_ago)
    finished = now - timedelta(minutes=(
        minutes_ago - .5 if finished_minutes_ago is None else finished_minutes_ago
    ))
    event = dict(
        attempt_id=attempt_id, provider="invisible_playwright", workflow=workflow,
        source_url=safe_posting_url(url), title="Engineer", status=status,
        reason="Timed out" if status == "failed" else "Page resolved",
        started_at=started.isoformat(), finished_at=finished.isoformat(),
        expected_by=(started + timedelta(seconds=30)).isoformat(), duration_seconds=30,
    )
    if include_key:
        event["url_key"] = browser_health.url_key(url)
    browser_health._write(root, event)


def test_later_success_recovers_failures_across_workflows_without_erasing_records(tmp_path):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "failed-1", "failed", minutes_ago=5)
    _timed_attempt(tmp_path, now, "failed-2", "failed", minutes_ago=3)
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=1,
                   workflow="availability_scan")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    health = summarize(tmp_path, now=now)
    assert health["counts"]["failed"] == 2
    assert health["active_issue_counts"]["failed"] == 0
    assert health["recovered_failure_count"] == 2
    assert health["dismissed_issue_count"] == 0
    assert health["recent_issues"] == []
    assert health["failure_reasons"] == []
    assert health["total"] == 3
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.json")} == before


@pytest.mark.parametrize("success_minutes_ago,success_finished_minutes_ago", [(5, 4.5), (5, 1)])
def test_earlier_or_overlapping_success_does_not_hide_new_failure(
    tmp_path, success_minutes_ago, success_finished_minutes_ago,
):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=success_minutes_ago,
                   finished_minutes_ago=success_finished_minutes_ago)
    _timed_attempt(tmp_path, now, "failed", "failed", minutes_ago=3)
    assert summarize(tmp_path, now=now)["active_issue_counts"]["failed"] == 1


def test_failure_after_recovery_stays_visible(tmp_path):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "old", "failed", minutes_ago=5)
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=3)
    _timed_attempt(tmp_path, now, "new", "failed", minutes_ago=1)
    health = summarize(tmp_path, now=now)
    assert health["counts"]["failed"] == 2
    assert health["active_issue_counts"]["failed"] == 1
    assert health["recovered_failure_count"] == 1
    assert [row["attempt_id"] for row in health["recent_issues"]] == ["new"]


@pytest.mark.parametrize("include_key", [True, False])
def test_recovery_does_not_merge_different_links_that_sanitize_to_the_same_url(tmp_path, include_key):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "failed", "failed", url="https://example.com/job?id=1",
                   include_key=include_key)
    _timed_attempt(tmp_path, now, "success", "succeeded", url="https://example.com/job?id=2",
                   minutes_ago=1, include_key=include_key)
    assert summarize(tmp_path, now=now)["active_issue_counts"]["failed"] == 1


def test_review_result_does_not_recover_failure(tmp_path):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "failed", "failed")
    _timed_attempt(tmp_path, now, "review", "needs_review", minutes_ago=1)
    assert summarize(tmp_path, now=now)["active_issue_counts"]["failed"] == 1


def test_dismissed_failure_and_recovered_failure_are_counted_separately(tmp_path):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "dismissed", "failed", minutes_ago=5)
    assert browser_health.dismiss_issues(tmp_path, ["dismissed"], now=now) == 1
    _timed_attempt(tmp_path, now, "recovered", "failed", minutes_ago=3)
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=1)
    health = summarize(tmp_path, now=now)
    assert health["active_issue_counts"]["failed"] == 0
    assert health["dismissed_issue_count"] == 1
    assert health["recovered_failure_count"] == 1


def test_dismiss_all_excludes_already_recovered_failures(tmp_path):
    now = datetime.now(UTC)
    _timed_attempt(tmp_path, now, "failed", "failed")
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=1)
    assert browser_health.dismiss_issues(tmp_path, None, now=now) == 0
    health = summarize(tmp_path, now=now)
    assert health["recovered_failure_count"] == 1
    assert health["dismissed_issue_count"] == 0


def test_health_endpoint_reports_recovery_without_exposing_original_url_hash(
    jobcockpit_settings_factory, jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(browser_health_enabled=True)
    now = datetime.now(UTC)
    _timed_attempt(settings.artifact_root, now, "failed", "failed")
    _timed_attempt(settings.artifact_root, now, "success", "succeeded", minutes_ago=1)
    with jobcockpit_client_factory(settings) as client:
        payload = client.get("/api/browser-health").json()
    assert payload["counts"]["failed"] == 1
    assert payload["active_issue_counts"]["failed"] == 0
    assert payload["recovered_failure_count"] == 1
    assert payload["recent_issues"] == []
    assert "url_key" not in payload


@pytest.mark.parametrize("status", ["running", "needs_review"])
def test_later_success_does_not_hide_review_or_missing_completion(tmp_path, status):
    now = datetime.now(UTC)
    event = dict(
        attempt_id="attention", provider="invisible_playwright", workflow="ingestion",
        source_url=safe_posting_url(hint().source_url), title="Engineer", status=status,
        url_key=browser_health.url_key(hint().source_url), reason="Needs review",
        started_at=(now - timedelta(minutes=5)).isoformat(),
        expected_by=(now - timedelta(minutes=4)).isoformat(),
        finished_at=None if status == "running" else (now - timedelta(minutes=4)).isoformat(),
        duration_seconds=None, ownership_version=1,
    )
    browser_health._write(tmp_path, event)
    _timed_attempt(tmp_path, now, "success", "succeeded", minutes_ago=1)
    health = summarize(tmp_path, now=now)
    assert health["active_issue_counts"]["overdue" if status == "running" else status] == 1
    assert health["recovered_failure_count"] == 0


def test_dismissal_hides_one_issue_without_erasing_attempts(tmp_path):
    for _ in range(2):
        observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
            status="needs_review", error="playwright_timeout",
        ))
    before = summarize(tmp_path)
    target = before["recent_issues"][0]["attempt_id"]
    assert browser_health.dismiss_issues(tmp_path, [target]) == 1
    assert browser_health.dismiss_issues(tmp_path, [target]) == 0
    after = summarize(tmp_path)
    assert after["counts"]["failed"] == 2
    assert after["active_issue_counts"]["failed"] == 1
    assert [row["attempt_id"] for row in after["recent_issues"]] == [before["recent_issues"][1]["attempt_id"]]
    assert after["dismissed_issue_count"] == 1
    assert len(list((tmp_path / "browser-health").rglob("*.json"))) == 4
    observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
        status="needs_review", error="playwright_timeout",
    ))
    assert summarize(tmp_path)["active_issue_counts"]["failed"] == 2


def test_dismiss_all_covers_issues_beyond_recent_list_limit(tmp_path):
    for _ in range(25):
        observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
            status="needs_review", error="playwright_timeout",
        ))
    assert len(summarize(tmp_path)["recent_issues"]) == 20
    assert browser_health.dismiss_issues(tmp_path, None) == 25
    result = summarize(tmp_path)
    assert result["recent_issues"] == []
    assert result["counts"]["failed"] == 25
    assert result["active_issue_counts"]["failed"] == 0


def test_dismissed_overdue_attempt_reappears_if_it_fails(tmp_path):
    now = datetime.now(UTC)
    event = dict(attempt_id="a" * 32, provider="invisible_playwright", workflow="availability_scan",
                 source_url="https://example.com/job", title="Engineer", url_key="key", status="running",
                 reason="", started_at=(now-timedelta(minutes=5)).isoformat(), finished_at=None,
                 expected_by=(now-timedelta(minutes=1)).isoformat(), duration_seconds=None)
    browser_health._write(tmp_path, event)
    assert browser_health.dismiss_issues(tmp_path, [event["attempt_id"]], now=now) == 1
    assert summarize(tmp_path, now=now)["active_issue_counts"]["overdue"] == 0
    browser_health._write(tmp_path, {**event, "status": "failed", "reason": "Timed out", "finished_at": now.isoformat()})
    assert summarize(tmp_path, now=now)["active_issue_counts"]["failed"] == 1


def test_browser_health_dismiss_endpoints_persist_and_reject_unknown_ids(
    jobcockpit_settings_factory, jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(browser_health_enabled=True)
    for _ in range(2):
        observe(settings.artifact_root, hint(), "ingestion", 30, lambda: PostingResolution(
            status="needs_review", error="playwright_timeout",
        ))
    with jobcockpit_client_factory(settings) as client:
        first = client.get("/api/browser-health").json()["recent_issues"][0]["attempt_id"]
        assert client.post("/api/browser-health/issues/not-an-id/dismiss").status_code == 422
        assert client.post(f"/api/browser-health/issues/{'f' * 32}/dismiss").status_code == 404
        assert client.post(f"/api/browser-health/issues/{first}/dismiss").json() == {"dismissed": 1}
        assert client.post(f"/api/browser-health/issues/{first}/dismiss").json() == {"dismissed": 0}
        assert client.get("/api/browser-health").json()["active_issue_counts"]["failed"] == 1
        assert client.post("/api/browser-health/issues/dismiss-all").json() == {"dismissed": 1}
        result = client.get("/api/browser-health").json()
    assert result["recent_issues"] == []
    assert result["counts"]["failed"] == 2
    assert result["active_issue_counts"]["failed"] == 0


def test_dismissal_still_works_when_unrelated_monitoring_record_is_corrupt(tmp_path):
    observe(tmp_path, hint(), "ingestion", 30, lambda: PostingResolution(
        status="needs_review", error="playwright_timeout",
    ))
    issue_id = summarize(tmp_path)["recent_issues"][0]["attempt_id"]
    directory = tmp_path / "browser-health" / datetime.now(UTC).date().isoformat() / "misc"
    (directory / "corrupt.json").write_text("{", encoding="utf-8")
    assert browser_health.dismiss_issues(tmp_path, [issue_id]) == 1
    result = summarize(tmp_path)
    assert result["incomplete"] is True
    assert result["active_issue_counts"]["failed"] == 0


def test_dashboard_polls_and_exposes_monitoring_gaps():
    panel = Path("apps/web/app/browser-health.tsx").read_text(encoding="utf-8")
    assert '"/api/browser-health"' in panel
    assert "refetchInterval: 30_000" in panel
    assert "No completion reported" in panel
    assert "counts.needs_review" in panel
    assert "query.isError" in panel
    assert "data.incomplete" in panel
    assert "Dismiss all issues" in panel
    assert "onClick={() => dismissal.mutate(attempt.attempt_id)}" in panel
    assert 'queryClient.invalidateQueries({ queryKey: ["browser-health"] })' in panel


def test_embedded_job_content_is_reported_without_changing_resolution(tmp_path):
    from types import SimpleNamespace

    # Grounded in the supplied Tower posting: outer page chrome and a Greenhouse
    # job iframe, whose text is absent from the top-document capture.
    frame = SimpleNamespace(url="https://job-boards.greenhouse.io/embed/job_app", evaluate=lambda script: True)
    def render():
        browser_health.inspect_embedded_job_content(SimpleNamespace(frames=[frame], main_frame=object()))
        return PostingResolution(status="resolved")
    result = observe(tmp_path, hint(), "ingestion", 30, render)
    assert result.status == "resolved"
    health = summarize(tmp_path)
    assert health["counts"]["needs_review"] == 1
    assert health["last_success_at"] is None
    assert "embedded in a frame" in health["recent_issues"][0]["reason"]


def test_corrupt_monitoring_data_is_reported_as_incomplete(tmp_path):
    storage = browser_health.ArtifactStorageService(tmp_path / "browser-health" / datetime.now(UTC).date().isoformat())
    storage.write_bytes(artifact_type="browser_health", content=b'{"status":"running"}',
                        original_file_name="invalid.json", content_type="application/json", privacy_class="private")
    result = summarize(tmp_path)
    assert result["incomplete"] is True
    assert result["total"] == 0


def test_scheduled_attempt_is_recorded_once_even_when_worker_is_used(tmp_path):
    import time
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion.scheduled_browser import ScheduledInvisiblePlaywrightResolver

    resolver = ScheduledInvisiblePlaywrightResolver(Settings(
        browser_health_enabled=True, artifact_root=tmp_path,
    ), time.monotonic()+20)
    resolver.load_page(ExternalPostingHint(source_url="http://127.0.0.1:12345/job", source_kind="generic"))
    result = summarize(tmp_path)
    assert result["total"] == 1
    assert result["counts"]["failed"] == 1
    assert result["recent_issues"][0]["workflow"] == "availability_scan"
