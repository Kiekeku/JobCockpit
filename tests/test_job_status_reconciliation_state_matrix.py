from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobPosting, JobStatusCheck, JobStatusScanRun
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion import status_reconciliation as reconciliation
from jobcockpit.ingestion.status_reconciliation import (
    StatusCheckDecision,
    StatusScanAlreadyRunningError,
    StatusScanLeaseLostError,
    classify_http_status,
    retry_delay,
    run_status_reconciliation,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        posting_resolver_provider="disabled",
        posting_search_provider="disabled",
        posting_rendered_fallback_provider="disabled",
        fit_analysis_provider="disabled",
    )


def _ingest(
    client: TestClient,
    *,
    source_url: str,
    company: str,
) -> dict:
    response = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "gmail_alert",
            "raw_text": f"Platform Engineer\nCompany: {company}\nRemote Canada",
            "source_url": source_url,
        },
    )
    assert response.status_code == 201
    return response.json()


def _target(decisions: tuple[StatusCheckDecision, ...]) -> reconciliation.JobStatusTarget:
    candidates = tuple(
        reconciliation.StatusUrlCandidate(
            f"https://careers{index}.example.test/jobs/123",
            "official_ats" if index == 0 else "external_apply",
            "generic",
        )
        for index in range(len(decisions))
    )
    return reconciliation.JobStatusTarget(
        job_id="job_precedence",
        status="active",
        source_type="gmail_alert",
        source_url=candidates[0].url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=candidates,
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )


def test_due_selection_prioritizes_passed_deadlines_then_unverified_and_stale_open(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 8, 20, 18, tzinfo=UTC)
    with TestClient(create_app(settings)) as client:
        passed_deadline = _ingest(
            client,
            source_url="https://example.com/jobs/passed-deadline",
            company="Passed Deadline",
        )
        unverified = _ingest(
            client,
            source_url="https://example.com/jobs/unverified",
            company="Unverified",
        )
        stale_open = _ingest(
            client,
            source_url="https://example.com/jobs/stale-open",
            company="Stale Open",
        )
        fresh_open = _ingest(
            client,
            source_url="https://example.com/jobs/fresh-open",
            company="Fresh Open",
        )

    with session_scope(settings) as db:
        jobs = {
            job.id: job
            for job in db.execute(select(JobPosting)).scalars().all()
        }
        for job in jobs.values():
            job.status_next_check_at = now - timedelta(minutes=1)
        jobs[passed_deadline["id"]].application_deadline = "2026-08-19"
        jobs[passed_deadline["id"]].status_check_result = "open"
        jobs[passed_deadline["id"]].status_last_checked_at = now - timedelta(hours=1)
        jobs[unverified["id"]].status_check_result = "inconclusive"
        jobs[unverified["id"]].status_last_checked_at = now - timedelta(hours=1)
        jobs[stale_open["id"]].status_check_result = "open"
        jobs[stale_open["id"]].status_last_checked_at = now - timedelta(days=8)
        jobs[fresh_open["id"]].status_check_result = "open"
        jobs[fresh_open["id"]].status_last_checked_at = now - timedelta(hours=1)

    targets = reconciliation._select_targets(
        settings,
        now,
        limit=4,
        job_id=None,
        force=False,
    )

    assert [target.job_id for target in targets] == [
        passed_deadline["id"],
        unverified["id"],
        stale_open["id"],
        fresh_open["id"],
    ]


def test_target_selection_skips_closed_expired_and_applied_jobs_even_when_forced(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 8, 20, 18, tzinfo=UTC)
    with TestClient(create_app(settings)) as client:
        active = _ingest(
            client,
            source_url="https://example.com/jobs/active",
            company="Active",
        )
        closed = _ingest(
            client,
            source_url="https://example.com/jobs/closed",
            company="Closed",
        )
        expired = _ingest(
            client,
            source_url="https://example.com/jobs/expired",
            company="Expired",
        )
        applied = _ingest(
            client,
            source_url="https://example.com/jobs/applied",
            company="Applied",
        )
        applied_response = client.post(f"/api/jobs/{applied['id']}/applications/applied")
        assert applied_response.status_code == 200

    with session_scope(settings) as db:
        db.get(JobPosting, closed["id"]).status = "closed"
        db.get(JobPosting, expired["id"]).status = "expired"

    targets = reconciliation._select_targets(
        settings,
        now,
        limit=10,
        job_id=None,
        force=True,
    )

    assert [target.job_id for target in targets] == [active["id"]]


def test_passed_application_deadline_expires_job_before_accessible_page_can_confirm_open(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 8, 20, 18, tzinfo=UTC)
    with TestClient(create_app(settings)) as client:
        created = _ingest(
            client,
            source_url="https://example.com/jobs/passed-deadline",
            company="Passed Deadline",
        )

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.application_deadline = "2026-08-19"

    checker_called = False

    def accessible_matching_page(_target, _candidate):
        nonlocal checker_called
        checker_called = True
        return StatusCheckDecision("open", "posting_identity_match")

    summary = run_status_reconciliation(
        settings,
        checker=accessible_matching_page,
        now=now,
    )

    assert checker_called is False
    assert summary["expired"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        check = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalar_one()
        assert job.status == "expired"
        assert job.status_check_result == "expired"
        assert job.status_check_evidence_code == "application_deadline_passed"
        assert check.evidence_code == "application_deadline_passed"


def test_application_deadline_waits_until_the_date_has_ended_globally() -> None:
    target = reconciliation.JobStatusTarget(
        job_id="job_global_deadline_boundary",
        status="active",
        source_type="gmail_alert",
        source_url="https://example.com/jobs/global-deadline",
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
        application_deadline="2026-08-19",
    )

    assert (
        reconciliation._passed_application_deadline_decision(
            target,
            datetime(2026, 8, 20, 11, 59, tzinfo=UTC),
        )
        is None
    )
    assert (
        reconciliation._passed_application_deadline_decision(
            target,
            datetime(2026, 8, 20, 12, tzinfo=UTC),
        ).result
        == "expired"
    )


@pytest.mark.parametrize(
    ("decisions", "expected_result", "expected_evidence", "expected_retry_after"),
    [
        pytest.param(
            (
                StatusCheckDecision(
                    "inconclusive",
                    "http_429_rate_limited",
                    retryable=True,
                    retry_after_seconds=14 * 24 * 60 * 60,
                ),
                StatusCheckDecision("unsupported", "url_policy_unsupported_scheme"),
            ),
            "inconclusive",
            "http_429_rate_limited",
            14 * 24 * 60 * 60,
            id="unsupported-does-not-erase-retryable-evidence",
        ),
        pytest.param(
            (
                StatusCheckDecision("unsupported", "url_policy_unsupported_scheme"),
                StatusCheckDecision(
                    "inconclusive",
                    "authentication_wall",
                    retryable=True,
                ),
            ),
            "inconclusive",
            "authentication_wall",
            None,
            id="retryable-evidence-outranks-unsupported",
        ),
        pytest.param(
            (
                StatusCheckDecision(
                    "inconclusive",
                    "generic_http_404_pending_confirmation",
                    http_status=404,
                    retryable=True,
                ),
                StatusCheckDecision("unsupported", "url_policy_unsupported_scheme"),
            ),
            "inconclusive",
            "generic_http_404_pending_confirmation",
            None,
            id="confirmable-404-survives-unsupported-fallback",
        ),
        pytest.param(
            (
                StatusCheckDecision(
                    "inconclusive",
                    "http_429_rate_limited",
                    retryable=True,
                    retry_after_seconds=48 * 60 * 60,
                ),
                StatusCheckDecision("open", "posting_identity_match"),
            ),
            "open",
            "posting_identity_match",
            48 * 60 * 60,
            id="definitive-open-outranks-transient-failure",
        ),
        pytest.param(
            (
                StatusCheckDecision(
                    "inconclusive",
                    "generic_http_404_pending_confirmation",
                    http_status=404,
                    retryable=True,
                ),
                StatusCheckDecision(
                    "closed",
                    "linkedin_no_longer_accepting",
                    detected_status="closed",
                ),
            ),
            "closed",
            "linkedin_no_longer_accepting",
            None,
            id="definitive-closure-outranks-pending-confirmation",
        ),
        pytest.param(
            (
                StatusCheckDecision("unsupported", "url_policy_unsupported_scheme"),
                StatusCheckDecision("unsupported", "url_policy_host_not_allowed"),
            ),
            "unsupported",
            "url_policy_host_not_allowed",
            None,
            id="all-unsupported-remains-unsupported",
        ),
    ],
)
def test_candidate_observation_precedence_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decisions: tuple[StatusCheckDecision, ...],
    expected_result: str,
    expected_evidence: str,
    expected_retry_after: int | None,
) -> None:
    target = _target(decisions)
    by_url = {
        candidate.url: decision
        for candidate, decision in zip(target.candidates, decisions, strict=True)
    }

    def check_candidate(_target, candidate, _settings, **_kwargs):
        decision = by_url[candidate.url]
        return reconciliation.replace(
            decision,
            source_url=candidate.url,
            source_kind=candidate.source_kind,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", check_candidate)

    selected = reconciliation._direct_target_check(
        target,
        _settings(tmp_path),
        {},
        reconciliation.threading.Lock(),
    )

    assert isinstance(selected, StatusCheckDecision)
    assert selected.result == expected_result
    assert selected.evidence_code == expected_evidence
    assert selected.retry_after_seconds == expected_retry_after


@pytest.mark.parametrize(
    ("result", "prior_failures", "expected_delay"),
    [
        ("open", 99, timedelta(hours=24)),
        ("closed", 99, None),
        ("expired", 99, None),
        ("unsupported", 99, timedelta(days=7)),
        ("inconclusive", 0, timedelta(hours=6)),
        ("inconclusive", 1, timedelta(hours=12)),
        ("inconclusive", 2, timedelta(hours=24)),
        ("inconclusive", 20, timedelta(hours=24)),
        ("failed", 0, timedelta(hours=6)),
        ("failed", 1, timedelta(hours=12)),
        ("failed", 2, timedelta(hours=24)),
    ],
)
def test_retry_schedule_matrix(
    result: str,
    prior_failures: int,
    expected_delay: timedelta | None,
) -> None:
    assert retry_delay(result, prior_failures) == expected_delay


def test_generic_404_requires_two_same_source_observations_six_hours_apart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    source_url = "https://example.com/jobs/123"
    created = _ingest(client, source_url=source_url, company="Acme 404")
    first_observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    observed_at = [first_observed_at]

    def generic_404(target, candidate):
        return classify_http_status(
            404,
            source_kind=candidate.source_kind,
            generic_404_count=reconciliation._eligible_generic_404_count(
                target,
                candidate.url,
                observed_at=observed_at[0],
            ),
            source_url=candidate.url,
        )

    first = run_status_reconciliation(
        settings,
        checker=generic_404,
        now=first_observed_at,
    )
    assert first["inconclusive"] == 1

    observed_at[0] = first_observed_at + timedelta(hours=6)
    second = run_status_reconciliation(
        settings,
        checker=generic_404,
        now=observed_at[0],
    )

    assert second["closed"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(
            select(JobStatusCheck)
            .where(JobStatusCheck.job_id == created["id"])
            .order_by(JobStatusCheck.checked_at)
        ).scalars().all()
        assert job.status == "closed"
        assert job.status_next_check_at is None
        assert [check.evidence_code for check in checks] == [
            "generic_http_404_pending_confirmation",
            "generic_http_404_confirmed",
        ]


@pytest.mark.parametrize(
    ("retry_after", "minimum_delay"),
    [
        ("172800", timedelta(hours=48)),
        ("999999999999999999999", timedelta(hours=24)),
    ],
)
def test_retry_after_values_are_bounded_without_failing_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
    minimum_delay: timedelta,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = _ingest(
        client,
        source_url="https://example.com/jobs/429",
        company=f"Acme Retry {retry_after[:8]}",
    )
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def rate_limited(source_url, _settings, **_kwargs):
        request = httpx.Request("GET", source_url)
        return (
            httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                request=request,
            ),
            source_url,
        )

    monkeypatch.setattr(reconciliation, "_get_with_safe_redirects", rate_limited)

    summary = run_status_reconciliation(settings, now=observed_at)

    assert summary["status"] == "completed"
    assert summary["inconclusive"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert _as_utc(job.status_next_check_at) >= observed_at + minimum_delay


def test_completed_observations_survive_budget_interruption_and_remaining_work_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    first = _ingest(
        client,
        source_url="https://example.com/jobs/first",
        company="Acme First",
    )
    second = _ingest(
        client,
        source_url="https://example.com/jobs/second",
        company="Acme Second",
    )
    first_run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    monkeypatch.setattr(reconciliation, "_DIRECT_CONCURRENCY", 1)
    clock_values = iter((0.0, 0.0, 11.0))
    last_clock = [11.0]

    def interrupted_clock() -> float:
        try:
            last_clock[0] = next(clock_values)
        except StopIteration:
            pass
        return last_clock[0]

    monkeypatch.setattr(reconciliation.time, "monotonic", interrupted_clock)

    def close_probe(_target, candidate):
        return StatusCheckDecision(
            "closed",
            "ats_no_longer_available",
            candidate.url,
            candidate.source_kind,
            detected_status="closed",
        )

    interrupted = run_status_reconciliation(
        settings,
        checker=close_probe,
        limit=2,
        max_runtime_seconds=10,
        now=first_run_at,
    )

    assert interrupted["closed"] == 1
    assert interrupted["deferred"] == 1
    with session_scope(settings) as db:
        first_job = db.get(JobPosting, first["id"])
        second_job = db.get(JobPosting, second["id"])
        assert first_job.status == "closed"
        assert second_job.status == "active"
        assert db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == first["id"])
        ).scalar_one()

    monkeypatch.setattr(reconciliation.time, "monotonic", lambda: 0.0)
    resumed = run_status_reconciliation(
        settings,
        checker=close_probe,
        limit=2,
        max_runtime_seconds=10,
        now=first_run_at + timedelta(hours=2),
    )

    assert resumed["selected"] == 1
    assert resumed["closed"] == 1
    with session_scope(settings) as db:
        assert db.get(JobPosting, second["id"]).status == "closed"


def test_dry_run_respects_a_live_singleton_without_mutating_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = _ingest(
        client,
        source_url="https://example.com/jobs/dry-run",
        company="Acme Dry Run",
    )
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_live",
                status="running",
                started_at=now,
                request_payload={"max_runtime_seconds": 5400},
            )
        )

    with pytest.raises(StatusScanAlreadyRunningError):
        run_status_reconciliation(
            settings,
            dry_run=True,
            checker=lambda _target, candidate: StatusCheckDecision(
                "open",
                "posting_identity_match",
                candidate.url,
                candidate.source_kind,
            ),
            now=now,
        )

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        runs = db.execute(select(JobStatusScanRun)).scalars().all()
        assert job.status == "active"
        assert job.status_last_checked_at is None
        assert len(runs) == 1
        assert runs[0].status == "running"
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_dry_run_fences_active_input_edit_before_counting_terminal_result(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = _ingest(
        client,
        source_url="https://www.linkedin.com/jobs/view/2234567801",
        company="Acme Dry Edit",
    )
    run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    edit_at = run_at + timedelta(minutes=1)
    replacement_url = "https://www.linkedin.com/jobs/view/2234567802"

    def close_after_input_edit(target, candidate):
        with session_scope(settings) as db:
            job = db.get(JobPosting, target.job_id)
            source = dict(job.source)
            source["source_url"] = replacement_url
            job.source_url = replacement_url
            job.source = source
            job.updated_at = edit_at
        return StatusCheckDecision(
            "closed",
            "linkedin_no_longer_accepting",
            candidate.url,
            candidate.source_kind,
            detected_status="closed",
            http_status=200,
        )

    summary = run_status_reconciliation(
        settings,
        dry_run=True,
        checker=close_after_input_edit,
        now=run_at,
    )

    assert summary["selected"] == 1
    assert summary["checked"] == 1
    assert summary["closed"] == 0
    assert summary["skipped"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert job.source_url == replacement_url
        assert db.execute(select(JobStatusCheck)).scalars().all() == []
        assert db.execute(select(JobStatusScanRun)).scalars().all() == []


def test_dry_run_fences_manual_nonactive_transition_before_deferral(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = _ingest(
        client,
        source_url="https://www.linkedin.com/jobs/view/2234567803",
        company="Acme Dry Manual",
    )
    run_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    manual_at = run_at + timedelta(minutes=1)

    def defer_after_manual_close(target, candidate):
        with session_scope(settings) as db:
            job = db.get(JobPosting, target.job_id)
            job.status = "closed"
            job.status_next_check_at = None
            job.updated_at = manual_at
        return reconciliation.DeferredRequired(
            "runtime_budget_exhausted",
            candidate.url,
        )

    summary = run_status_reconciliation(
        settings,
        dry_run=True,
        checker=defer_after_manual_close,
        now=run_at,
    )

    assert summary["selected"] == 1
    assert summary["checked"] == 0
    assert summary["deferred"] == 0
    assert summary["skipped"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "closed"
        assert db.execute(select(JobStatusCheck)).scalars().all() == []
        assert db.execute(select(JobStatusScanRun)).scalars().all() == []


def test_dry_run_never_recovers_or_mutates_a_stale_persisted_run(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    stale_started_at = now - timedelta(hours=4)
    with session_scope(settings) as db:
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_stale_before_dry_run",
                status="running",
                started_at=stale_started_at,
                request_payload={"max_runtime_seconds": 60},
            )
        )
    with session_scope(settings) as db:
        original_updated_at = _as_utc(
            db.get(
                JobStatusScanRun,
                "status_scan_stale_before_dry_run",
            ).updated_at
        )

    with pytest.raises(StatusScanAlreadyRunningError):
        run_status_reconciliation(
            settings,
            dry_run=True,
            now=now,
            max_runtime_seconds=60,
        )

    with session_scope(settings) as db:
        run = db.get(
            JobStatusScanRun,
            "status_scan_stale_before_dry_run",
        )
        assert run.status == "running"
        assert _as_utc(run.started_at) == stale_started_at
        assert run.finished_at is None
        assert run.error_message is None
        assert _as_utc(run.updated_at) == original_updated_at
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_dry_run_holds_the_singleton_lease_for_its_full_scan(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    _ingest(
        client,
        source_url="https://example.com/jobs/dry-run-lease",
        company="Acme Dry Lease",
    )
    checker_started = threading.Event()
    release_checker = threading.Event()

    def blocked_checker(_target, candidate):
        checker_started.set()
        assert release_checker.wait(timeout=10)
        return StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        dry_future = executor.submit(
            run_status_reconciliation,
            settings,
            dry_run=True,
            checker=blocked_checker,
        )
        assert checker_started.wait(timeout=10)
        try:
            with pytest.raises(StatusScanAlreadyRunningError):
                run_status_reconciliation(
                    settings,
                    checker=lambda _target, candidate: StatusCheckDecision(
                        "open",
                        "posting_identity_match",
                        candidate.url,
                        candidate.source_kind,
                    ),
                )
        finally:
            release_checker.set()
        assert dry_future.result(timeout=10)["status"] == "dry_run"

    with session_scope(settings) as db:
        assert db.execute(select(JobStatusScanRun)).scalars().all() == []
        assert db.execute(select(JobStatusCheck)).scalars().all() == []


def test_stale_takeover_fences_old_worker_and_preserves_partial_counts(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    client = TestClient(create_app(settings))
    created = _ingest(
        client,
        source_url="https://example.com/jobs/fenced",
        company="Acme Fenced",
    )
    started_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    request = {
        "max_runtime_seconds": 1,
        "dry_run": False,
    }
    old_run_id = reconciliation._start_run(
        settings,
        request,
        started_at,
        max_runtime_seconds=1,
    )
    target = reconciliation._select_targets(
        settings,
        started_at,
        limit=1,
        job_id=created["id"],
        force=True,
    )[0]
    reconciliation._record_run_selection(settings, old_run_id, 1)
    assert reconciliation._persist_decision(
        settings,
        old_run_id,
        target,
        reconciliation.StatusObservation(
            StatusCheckDecision(
                "open",
                "posting_identity_match",
                target.candidates[0].url,
                target.candidates[0].source_kind,
            ),
            started_at,
        ),
    )

    new_run_id = reconciliation._start_run(
        settings,
        request,
        started_at + timedelta(seconds=602),
        max_runtime_seconds=1,
    )

    with pytest.raises(StatusScanLeaseLostError):
        reconciliation._persist_decision(
            settings,
            old_run_id,
            target,
            reconciliation.StatusObservation(
                StatusCheckDecision(
                    "closed",
                    "ats_no_longer_available",
                    target.candidates[0].url,
                    target.candidates[0].source_kind,
                    detected_status="closed",
                ),
                started_at + timedelta(seconds=603),
            ),
        )
    with pytest.raises(StatusScanLeaseLostError):
        reconciliation._finish_run(
            settings,
            old_run_id,
            {
                "status": "completed",
                "selected": 1,
                "checked": 1,
                "open": 1,
                "closed": 0,
                "expired": 0,
                "inconclusive": 0,
                "failed": 0,
                "deferred": 0,
                "skipped": 0,
            },
            started_at + timedelta(seconds=603),
        )

    with session_scope(settings) as db:
        old_run = db.get(JobStatusScanRun, old_run_id)
        assert old_run.status == "abandoned"
        assert old_run.selected_count == 1
        assert old_run.checked_count == 1
        assert old_run.open_count == 1
        assert db.get(JobPosting, created["id"]).status == "active"
        assert db.get(JobStatusScanRun, new_run_id).status == "running"
def test_runtime_limit_is_bounded_before_a_run_is_created(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)

    with pytest.raises(ValueError, match="invalid reconciliation limits"):
        run_status_reconciliation(
            settings,
            max_runtime_seconds=7201,
        )

    with session_scope(settings) as db:
        assert db.execute(select(JobStatusScanRun)).scalars().all() == []


def test_stale_recovery_caps_an_oversized_legacy_runtime(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_bad_legacy_runtime",
                status="running",
                started_at=now - timedelta(hours=3),
                request_payload={"max_runtime_seconds": 54_000_000},
            )
        )

    summary = run_status_reconciliation(
        settings,
        now=now,
        max_runtime_seconds=60,
    )

    assert summary["status"] == "completed"
    with session_scope(settings) as db:
        stale = db.get(
            JobStatusScanRun,
            "status_scan_bad_legacy_runtime",
        )
        current = db.get(JobStatusScanRun, summary["run_id"])
        assert stale.status == "abandoned"
        assert stale.error_message == "stale_run_recovered"
        assert current.status == "completed"




def test_redirect_hops_share_destination_host_concurrency_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    active_final_requests = 0
    maximum_final_requests = 0
    counter_lock = threading.Lock()

    class AllowAllPolicy:
        def __init__(self, **_kwargs):
            pass

        def check(self, _url):
            return SimpleNamespace(allowed=True, reason="allowed")

    class FakeClient:
        def __init__(self, *, timeout, **_kwargs):
            self.timeout = timeout
            self.url_policy = AllowAllPolicy()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **_kwargs):
            nonlocal active_final_requests, maximum_final_requests
            request = httpx.Request("GET", url)
            if urlparse(url).hostname != "final.example.test":
                return httpx.Response(
                    302,
                    headers={"Location": "https://final.example.test/jobs/123"},
                    request=request,
                )
            with counter_lock:
                active_final_requests += 1
                maximum_final_requests = max(
                    maximum_final_requests,
                    active_final_requests,
                )
            time.sleep(0.05)
            with counter_lock:
                active_final_requests -= 1
            return httpx.Response(200, request=request)

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "UrlFetchPolicy", AllowAllPolicy)
    monkeypatch.setattr(reconciliation, "PinnedHttpClient", FakeClient)
    host_locks: dict[str, threading.Lock] = {}
    host_locks_guard = threading.Lock()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda index: reconciliation._get_with_safe_redirects(
                    f"https://source{index}.example.test/short",
                    settings,
                    host_locks=host_locks,
                    host_locks_guard=host_locks_guard,
                ),
                range(4),
            )
        )

    assert all(response.status_code == 200 for response, _url in results)
    assert maximum_final_requests == 1


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

def test_rendered_checks_serialize_same_host_but_allow_different_hosts():
    def target_for(index: int, hostname: str):
        candidate = reconciliation.StatusUrlCandidate(
            f"https://{hostname}/jobs/{index}",
            "official_ats",
            "generic",
        )
        target = reconciliation.JobStatusTarget(
            job_id=f"job_render_host_{index}",
            status="active",
            source_type="gmail_alert",
            source_url=candidate.url,
            company="Acme",
            title="Platform Engineer",
            location="Canada",
            candidates=(candidate,),
            failure_count=0,
            not_found_count=0,
            last_checked_at=None,
        )
        return target, candidate

    active = 0
    maximum_active = 0
    counter_lock = threading.Lock()

    class MeasuringResolver:
        def resolve(self, hint):
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with counter_lock:
                active -= 1
            return {
                "status": "resolved",
                "posting": {
                    "source_url": hint.source_url,
                    "raw_text": "Platform Engineer\nAcme\nCanada",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    host_locks: dict[str, threading.Lock] = {}
    host_locks_guard = threading.Lock()
    same_host_items = [
        target_for(1, "jobs.example.test"),
        target_for(2, "jobs.example.test"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(
            executor.map(
                lambda item: reconciliation._rendered_check_with_host_lock(
                    item[0],
                    item[1],
                    MeasuringResolver(),
                    host_locks,
                    host_locks_guard,
                ),
                same_host_items,
            )
        )

    assert all(decision.result == "open" for decision in decisions)
    assert maximum_active == 1

    overlap_barrier = threading.Barrier(2)

    class OverlapResolver:
        def resolve(self, hint):
            overlap_barrier.wait(timeout=1)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": hint.source_url,
                    "raw_text": "Platform Engineer\nAcme\nCanada",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    different_host_items = [
        target_for(3, "jobs-a.example.test"),
        target_for(4, "jobs-b.example.test"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(
            executor.map(
                lambda item: reconciliation._rendered_check_with_host_lock(
                    item[0],
                    item[1],
                    OverlapResolver(),
                    host_locks,
                    host_locks_guard,
                ),
                different_host_items,
            )
        )

    assert all(decision.result == "open" for decision in decisions)


def test_rendered_check_does_not_start_after_waiting_past_deadline(monkeypatch):
    candidate = reconciliation.StatusUrlCandidate(
        "https://jobs.example.test/jobs/1",
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_render_deadline",
        status="active",
        source_type="gmail_alert",
        source_url=candidate.url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    clock = [0.0]

    class DeadlineAdvancingLock:
        def __init__(self):
            self.released = False

        def acquire(self, *, timeout):
            assert timeout == pytest.approx(10.0)
            clock[0] = 10.0
            return True

        def release(self):
            self.released = True

    host_lock = DeadlineAdvancingLock()
    resolver_created = False

    def unexpected_resolver_factory():
        nonlocal resolver_created
        resolver_created = True
        pytest.fail("a rendered resolver must not start after the deadline")

    monkeypatch.setattr(
        reconciliation.time,
        "monotonic",
        lambda: clock[0],
    )

    resolution = reconciliation._rendered_check_with_host_lock(
        target,
        candidate,
        None,
        {"jobs.example.test": host_lock},
        threading.Lock(),
        deadline=10.0,
        resolver_factory=unexpected_resolver_factory,
    )

    assert isinstance(resolution, reconciliation.DeferredRequired)
    assert resolution.evidence_code == "runtime_budget_exhausted"
    assert resolver_created is False
    assert host_lock.released is True
def test_rendered_check_requires_enough_budget_for_its_minimum_phases(
    monkeypatch,
):
    candidate = reconciliation.StatusUrlCandidate(
        "https://jobs.example.test/jobs/2",
        "official_ats",
        "generic",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_render_subsecond_budget",
        status="active",
        source_type="gmail_alert",
        source_url=candidate.url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    resolver_created = False

    def unexpected_resolver_factory():
        nonlocal resolver_created
        resolver_created = True
        pytest.fail("resolver cannot fit inside the remaining budget")

    monkeypatch.setattr(reconciliation.time, "monotonic", lambda: 0.0)

    resolution = reconciliation._rendered_check_with_host_lock(
        target,
        candidate,
        None,
        {},
        threading.Lock(),
        deadline=0.5,
        resolver_factory=unexpected_resolver_factory,
        minimum_start_seconds=1,
    )

    assert isinstance(resolution, reconciliation.DeferredRequired)
    assert resolution.evidence_code == "runtime_budget_exhausted"
    assert resolver_created is False


def test_equal_observation_timestamps_use_persisted_check_order_for_404_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    old_url = "https://www.linkedin.com/jobs/view/1234567890"
    new_url = "https://www.linkedin.com/jobs/view/1234567891"
    with TestClient(create_app(settings)) as client:
        created = _ingest(
            client,
            source_url=old_url,
            company="Acme Equal Timestamp",
        )
    checked_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    older_created_at = checked_at + timedelta(seconds=1)
    newer_created_at = checked_at + timedelta(seconds=2)

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.source_url = new_url
        job.status_last_checked_at = checked_at
        job.status_next_check_at = None
        job.status_check_result = "inconclusive"
        job.status_check_evidence_code = (
            "generic_http_404_pending_confirmation"
        )
        job.status_check_failure_count = 1
        job.status_check_not_found_count = 1
        db.add(
            JobStatusScanRun(
                status_scan_run_id="status_scan_equal_timestamp",
                status="completed",
                started_at=checked_at,
                finished_at=checked_at,
                request_payload={},
            )
        )
        db.flush()
        db.add_all(
            [
                JobStatusCheck(
                    status_check_id="status_check_z_older",
                    status_scan_run_id="status_scan_equal_timestamp",
                    job_id=job.id,
                    source_url=old_url,
                    source_kind="generic",
                    result="inconclusive",
                    detected_status=None,
                    evidence_code="generic_http_404_pending_confirmation",
                    http_status=404,
                    retryable=True,
                    status_before="active",
                    status_after="active",
                    checked_at=checked_at,
                    created_at=older_created_at,
                    updated_at=older_created_at,
                ),
                JobStatusCheck(
                    status_check_id="status_check_a_newer",
                    status_scan_run_id="status_scan_equal_timestamp",
                    job_id=job.id,
                    source_url=new_url,
                    source_kind="generic",
                    result="inconclusive",
                    detected_status=None,
                    evidence_code="generic_http_404_pending_confirmation",
                    http_status=404,
                    retryable=True,
                    status_before="active",
                    status_after="active",
                    checked_at=checked_at,
                    created_at=newer_created_at,
                    updated_at=newer_created_at,
                ),
            ]
        )

    targets = reconciliation._select_targets(
        settings,
        checked_at + timedelta(hours=7),
        10,
        created["id"],
        True,
    )

    assert len(targets) == 1
    assert targets[0].last_check_source_url == new_url
    assert reconciliation._eligible_generic_404_count(
        targets[0],
        new_url,
        observed_at=checked_at + timedelta(hours=7),
    ) == 1
    report = reconciliation.status_report(settings, job_id=created["id"])
    assert [check["source_url"] for check in report["checks"]] == [
        new_url,
        old_url,
    ]
