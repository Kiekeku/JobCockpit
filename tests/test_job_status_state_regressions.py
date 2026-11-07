from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobcockpit.services import workflows as api_main
from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobPosting, JobStatusCheck, JobStatusScanRun
from jobcockpit.db.session import get_engine, init_database, session_scope
from jobcockpit.ingestion import status_reconciliation as reconciliation
from jobcockpit.ingestion.status_reconciliation import (
    DeferredRequired,
    RenderRequired,
    StatusCheckDecision,
    classify_http_status,
    ordered_status_urls,
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


def _ingest_job(
    settings: Settings,
    *,
    source_url: str,
    company: str = "Acme",
) -> dict:
    with TestClient(create_app(settings)) as client:
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


def _seed_historical_job(settings, *, source_url):
    """Seed the historical posting schema without invoking current queue services."""
    from jobcockpit.db.models import StoredArtifact
    from jobcockpit.storage.artifacts import ArtifactStorageService
    from jobcockpit.utils.ids import new_id
    job_id = new_id("job")
    record = ArtifactStorageService(settings.artifact_root).write_bytes(
        artifact_type="job_snapshot", content=b"Platform Engineer\nCompany: Acme\nRemote Canada",
        original_file_name="posting.txt", content_type="text/plain", privacy_class="private",
        job_id=job_id,
    )
    with session_scope(settings) as db:
        db.add(StoredArtifact.from_record(record))
        db.flush()
        db.execute(JobPosting.__table__.insert().values(
            id=job_id, source_type="gmail_alert", source_url=source_url,
            raw_content_artifact_id=record.artifact_id, posting_snapshot_hash=record.sha256,
            company_name="Acme", normalized_title="Platform Engineer",
            source={"source_url": source_url, "source_url_dedup_safe": True},
            employer={"company_name": "Acme"}, role={"job_title": "Platform Engineer"},
        ))
    return {"id": job_id}


def _set_official_url(
    settings: Settings,
    job_id: str,
    official_url: str,
) -> None:
    with session_scope(settings) as db:
        job = db.get(JobPosting, job_id)
        source = dict(job.source)
        source["source_resolution"] = {
            **(
                source.get("source_resolution")
                if isinstance(source.get("source_resolution"), dict)
                else {}
            ),
            "official_url": official_url,
        }
        job.source = source


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_repeated_identity_mismatch_requires_source_repair_and_leaves_last_open_evidence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    opened_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    run_status_reconciliation(
        settings,
        job_id=created["id"],
        checker=lambda _target, candidate: StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        ),
        now=opened_at,
    )
    for day in range(1, 4):
        run_status_reconciliation(
            settings,
            job_id=created["id"],
            force=True,
            checker=lambda _target, candidate: StatusCheckDecision(
                "inconclusive",
                "redirect_identity_mismatch",
                candidate.url,
                candidate.source_kind,
                retryable=True,
                requested_url=candidate.url,
                final_url="https://job-boards.greenhouse.io/acme?error=true",
                diagnostics={"identity_match": False},
            ),
            now=opened_at + timedelta(days=day),
        )

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status_source_repair_required is True
        assert _as_utc(job.status_last_open_at) == opened_at
        assert job.status_last_open_evidence_code == "posting_identity_match"

    with TestClient(create_app(settings)) as client:
        detail = client.get(f"/api/jobs/{created['id']}").json()
        assert detail["availability"]["state"] == "source_repair_required"
        assert detail["availability"]["last_open_at"] == opened_at.isoformat()
        history = client.get(f"/api/jobs/{created['id']}/status-checks").json()
        assert history[0]["requested_url"] == created["source"]["source_url"]
        assert history[0]["final_url"].endswith("?error=true")
        assert history[0]["diagnostics"] == {"identity_match": False}
        health = client.get("/api/job-status/health").json()
        assert health["source_repair_required"] == 1
        assert health["active_total"] == 1
        assert health["latest_run"]["inconclusive"] == 1


def test_source_repair_requires_consecutive_same_source_identity_mismatches(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    decisions = (
        "authentication_wall",
        "authentication_wall",
        "redirect_identity_mismatch",
        "authentication_wall",
        "redirect_identity_mismatch",
        "redirect_identity_mismatch",
        "redirect_identity_mismatch",
    )
    for offset, evidence_code in enumerate(decisions):
        run_status_reconciliation(
            settings,
            job_id=created["id"],
            force=True,
            checker=lambda _target, candidate, evidence_code=evidence_code: StatusCheckDecision(
                "inconclusive",
                evidence_code,
                candidate.url,
                candidate.source_kind,
                retryable=True,
                requested_url=candidate.url,
                final_url="https://job-boards.greenhouse.io/acme?error=true",
            ),
            now=observed_at + timedelta(days=offset),
        )
        with session_scope(settings) as db:
            job = db.get(JobPosting, created["id"])
            assert job.status_source_repair_required is (offset == len(decisions) - 1)


def test_known_job_portal_source_is_quarantined_for_repair_on_first_check(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(settings, source_url="https://internhunt.eu/")

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        force=True,
        checker=lambda _target, candidate: StatusCheckDecision(
            "unsupported",
            "stored_url_not_job_detail",
            candidate.url,
            candidate.source_kind,
        ),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert summary["checked"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status_check_evidence_code == "stored_url_not_job_detail"
        assert job.status_source_repair_required is True


def test_source_repair_mismatch_history_does_not_cross_an_input_reset(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)

    def record_mismatch(offset: int) -> None:
        run_status_reconciliation(
            settings,
            job_id=created["id"],
            force=True,
            checker=lambda _target, candidate: StatusCheckDecision(
                "inconclusive",
                "redirect_identity_mismatch",
                candidate.url,
                candidate.source_kind,
                retryable=True,
                requested_url=candidate.url,
                final_url="https://job-boards.greenhouse.io/acme?error=true",
            ),
            now=observed_at + timedelta(days=offset),
        )

    record_mismatch(0)
    record_mismatch(1)
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        previous = api_main.job_status_scan_input_fingerprint(job)
        job.company_name = "Acme Systems"
        assert api_main.invalidate_job_status_scan_if_inputs_changed(job, previous)
        assert job.status_check_failure_count == 0

    record_mismatch(2)
    with session_scope(settings) as db:
        assert db.get(JobPosting, created["id"]).status_source_repair_required is False

    record_mismatch(3)
    record_mismatch(4)
    with session_scope(settings) as db:
        assert db.get(JobPosting, created["id"]).status_source_repair_required is True


def test_source_repair_jobs_are_not_selected_until_inputs_change(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status_source_repair_required = True
        job.status_next_check_at = datetime(2026, 8, 1, tzinfo=UTC)

    summary = run_status_reconciliation(
        settings,
        now=datetime(2026, 8, 28, tzinfo=UTC),
    )
    assert summary["selected"] == 0

    with TestClient(create_app(settings)) as client:
        response = client.patch(
            f"/api/jobs/{created['id']}/source",
            json={"source_url": "https://job-boards.greenhouse.io/acme/jobs/7654321"},
        )
        assert response.status_code == 200
        assert response.json()["availability"]["source_repair_required"] is False


def test_source_repair_correction_migration_requeues_approximate_quarantines(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "20260828_0025")
    created = _seed_historical_job(
        settings,
        source_url=(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
            "recruitment.html?cid=acme&jobId=599336"
        ),
    )
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status_source_repair_required = True
        job.status_next_check_at = None

    get_engine(settings).dispose()
    command.upgrade(config, "head")

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status_source_repair_required is False
        assert job.status_next_check_at is not None


def test_last_open_backfill_recovers_historical_positive_before_latest_inconclusive(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "20260828_0024")
    created = _seed_historical_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    opened_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    run_status_reconciliation(
        settings,
        job_id=created["id"],
        checker=lambda _target, candidate: StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        ),
        now=opened_at,
    )
    run_status_reconciliation(
        settings,
        job_id=created["id"],
        force=True,
        checker=lambda _target, candidate: StatusCheckDecision(
            "inconclusive",
            "authentication_wall",
            candidate.url,
            candidate.source_kind,
            retryable=True,
        ),
        now=opened_at + timedelta(days=1),
    )
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status_last_open_at = None
        job.status_last_open_evidence_code = None

    get_engine(settings).dispose()
    command.upgrade(config, "head")

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert _as_utc(job.status_last_open_at) == opened_at
        assert job.status_last_open_evidence_code == "posting_identity_match"


def test_full_update_invalidates_active_backoff_without_erasing_latest_observation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/jobs/ingest",
            json={
                "source_type": "paste",
                "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
                "source_url": None,
            },
        ).json()
        summary = run_status_reconciliation(
            settings,
            job_id=created["id"],
            now=observed_at,
        )
        assert summary["skipped"] == 1
        with session_scope(settings) as db:
            job = db.get(JobPosting, created["id"])
            job.status_check_failure_count = 2
            job.status_check_not_found_count = 1

        update_started_at = datetime.now(UTC)
        response = client.post(
            "/api/extension/page-captures",
            json={
                "target_job_id": created["id"],
                "source_url": "https://www.linkedin.com/jobs/view/9876543210",
                "page_title": "Platform Engineer | Acme",
                "visible_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            },
        )
        assert response.status_code == 202
        processed = api_main.process_page_capture_run(
            response.json()["capture_id"], settings, client.app.state.storage
        )
        assert processed["status"] == "succeeded"
        update_finished_at = datetime.now(UTC)

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        assert job.status == "active"
        assert _as_utc(job.status_last_checked_at) == observed_at
        assert job.status_check_result == "unsupported"
        assert job.status_check_evidence_code == "no_usable_stored_url"
        assert update_started_at <= _as_utc(job.status_next_check_at) <= update_finished_at
        assert job.status_check_failure_count == 0
        assert job.status_check_not_found_count == 0
        assert [candidate.url for candidate in ordered_status_urls(job)] == [
            "https://www.linkedin.com/jobs/view/9876543210"
        ]


def test_closed_job_input_update_stays_closed_and_reactivation_preserves_observation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = "https://www.linkedin.com/jobs/view/1234509876"
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    run_status_reconciliation(
        settings,
        job_id=created["id"],
        checker=lambda _target, candidate: StatusCheckDecision(
            "closed",
            "linkedin_no_longer_accepting",
            candidate.url,
            candidate.source_kind,
            detected_status="closed",
            http_status=200,
        ),
        now=observed_at,
    )

    with TestClient(create_app(settings)) as client:
        updated = client.post(
            "/api/extension/page-captures",
            json={
                "target_job_id": created["id"],
                "source_url": "https://ca.indeed.com/viewjob?jk=1234509876",
                "page_title": "Platform Engineer | Acme",
                "visible_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
            },
        )
        assert updated.status_code == 202
        processed = api_main.process_page_capture_run(
            updated.json()["capture_id"], settings, client.app.state.storage
        )
        assert processed["status"] == "succeeded"
        updated_job = client.get(f"/api/jobs/{created['id']}")
        assert updated_job.status_code == 200
        assert updated_job.json()["status"] == "closed"
        assert updated_job.json()["status_check"]["next_check_at"] is None
        assert updated_job.json()["status_check"]["result"] == "closed"
        assert updated_job.json()["status_check"]["evidence_code"] == (
            "linkedin_no_longer_accepting"
        )

        reactivation_started_at = datetime.now(UTC)
        reactivated = client.patch(
            f"/api/jobs/{created['id']}/status",
            json={"status": "active"},
        )
        reactivation_finished_at = datetime.now(UTC)

    assert reactivated.status_code == 200
    status_check = reactivated.json()["status_check"]
    assert reactivated.json()["status"] == "active"
    assert status_check["last_checked_at"] == observed_at.isoformat()
    assert status_check["result"] == "closed"
    assert status_check["evidence_code"] == "linkedin_no_longer_accepting"
    assert (
        reactivation_started_at
        <= datetime.fromisoformat(status_check["next_check_at"])
        <= reactivation_finished_at
    )
    assert status_check["failure_count"] == 0


def test_manual_reactivation_clears_current_deadline_attribution(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    created = _ingest_job(
        settings,
        source_url="https://job-boards.greenhouse.io/acme/jobs/1234567",
    )
    observed_at = datetime(2026, 8, 28, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status = "expired"
        job.status_last_checked_at = observed_at
        job.status_check_result = "expired"
        job.status_check_evidence_code = "application_deadline_passed"
        job.status_next_check_at = None

    with TestClient(create_app(settings)) as client:
        reactivated = client.patch(
            f"/api/jobs/{created['id']}/status",
            json={"status": "active"},
        )

    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == "active"
    assert reactivated.json()["status_check"]["evidence_code"] == (
        "application_deadline_passed"
    )
    assert reactivated.json()["availability"]["deadline_based"] is False


def test_aggregator_fallback_candidate_invalidates_weekly_unsupported_backoff(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    fallback_url = "https://www.linkedin.com/jobs/view/1122334455"
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/jobs/ingest",
            json={
                "source_type": "paste",
                "raw_text": "Platform Engineer\nCompany: Acme\nRemote Canada",
                "source_url": None,
            },
        ).json()

    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status_last_checked_at = observed_at
        job.status_next_check_at = observed_at + timedelta(days=7)
        job.status_check_result = "unsupported"
        job.status_check_evidence_code = "no_usable_stored_url"
        job.status_check_failure_count = 2
        job.status_check_not_found_count = 1
        mutation_started_at = datetime.now(UTC)
        api_main.mark_aggregator_fallback_duplicate_for_review(
            job,
            api_main.JobIngestPayload(
                source_type="url",
                source_url=fallback_url,
                raw_text="Platform Engineer\nCompany: Acme\nRemote Canada",
                source_resolution={
                    "kind": "aggregator_fallback",
                    "fallback_url": fallback_url,
                },
            ),
        )
        mutation_finished_at = datetime.now(UTC)

        assert _as_utc(job.status_last_checked_at) == observed_at
        assert job.status_check_result == "unsupported"
        assert job.status_check_evidence_code == "no_usable_stored_url"
        assert mutation_started_at <= _as_utc(job.status_next_check_at) <= mutation_finished_at
        assert job.status_check_failure_count == 0
        assert job.status_check_not_found_count == 0
        assert [candidate.url for candidate in ordered_status_urls(job)] == [
            fallback_url
        ]


def test_snapshot_reparse_invalidates_only_when_status_identity_changes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    changed_text = "Staff Platform Engineer\nCompany: Acme\nRemote Canada"

    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        job.status_last_checked_at = observed_at
        job.status_next_check_at = observed_at + timedelta(days=7)
        job.status_check_result = "inconclusive"
        job.status_check_evidence_code = "generic_http_404_pending_confirmation"
        job.status_check_failure_count = 2
        job.status_check_not_found_count = 1

        mutation_started_at = datetime.now(UTC)
        api_main.apply_parsed_job_to_existing_posting(
            job,
            changed_text,
            settings,
            db=db,
        )
        mutation_finished_at = datetime.now(UTC)

        assert mutation_started_at <= _as_utc(job.status_next_check_at) <= mutation_finished_at
        assert _as_utc(job.status_last_checked_at) == observed_at
        assert job.status_check_result == "inconclusive"
        assert job.status_check_evidence_code == "generic_http_404_pending_confirmation"
        assert job.status_check_failure_count == 0
        assert job.status_check_not_found_count == 0

        unchanged_next_check_at = observed_at + timedelta(days=3)
        job.status_next_check_at = unchanged_next_check_at
        job.status_check_failure_count = 2
        job.status_check_not_found_count = 1
        api_main.apply_parsed_job_to_existing_posting(
            job,
            changed_text,
            settings,
            db=db,
        )

        assert _as_utc(job.status_next_check_at) == unchanged_next_check_at
        assert job.status_check_failure_count == 2
        assert job.status_check_not_found_count == 1


def test_inconclusive_render_continues_to_later_terminal_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    workday_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/Platform-Engineer_R123"
    )
    linkedin_url = "https://www.linkedin.com/jobs/view/1234567890"
    created = _ingest_job(settings, source_url=linkedin_url)
    _set_official_url(settings, created["id"], workday_url)

    def workday_requires_render(_target, candidate, _settings, **_kwargs):
        assert candidate.url == workday_url
        return RenderRequired(
            "workday_403_render_required",
            candidate.url,
            candidate.source_kind,
            http_status=403,
        )

    class Resolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def resolve(self, hint):
            self.calls.append(hint.source_url)
            if hint.source_url == workday_url:
                return {
                    "status": "needs_review",
                    "error": "auth_wall_unresolved",
                }
            assert hint.source_url == linkedin_url
            return {
                "status": "resolved",
                "posting": {
                    "source_url": linkedin_url,
                    "raw_text": "No longer accepting applications",
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    resolver = Resolver()
    monkeypatch.setattr(reconciliation, "_direct_check", workday_requires_render)
    monkeypatch.setattr(
        reconciliation,
        "_rendered_resolver_for_remaining_budget",
        lambda *_args, **_kwargs: resolver,
    )

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=2,
    )

    assert resolver.calls == [workday_url, linkedin_url]
    assert summary["closed"] == 1
    assert summary["inconclusive"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalars().all()
        assert job.status == "closed"
        assert len(checks) == 1
        assert checks[0].evidence_code == "linkedin_no_longer_accepting"
        assert checks[0].source_url == linkedin_url


def test_indeed_selected_result_url_renders_and_expires_the_matching_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = (
        "https://www.indeed.com/q-Check-the-Status-of-Your-jobs.html"
        "?vjk=c01efd207eb8d10a"
    )
    created = _ingest_job(settings, source_url=source_url)

    class Resolver:
        def resolve(self, hint):
            assert hint.source_url == source_url
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "Other matching cards\n"
                        "Job Post Details\n"
                        "This job has expired on Indeed\n"
                        "Reasons could include: the employer is not accepting applications\n"
                        "Platform Engineer - job post\n"
                        "Image\n"
                        "Acme\n"
                        "Return to Search"
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    monkeypatch.setattr(
        reconciliation,
        "_rendered_resolver_for_remaining_budget",
        lambda *_args, **_kwargs: Resolver(),
    )

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=1,
    )

    assert summary["expired"] == 1
    assert summary["deferred"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        check = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalar_one()
        assert job.status == "expired"
        assert check.source_url == source_url
        assert check.evidence_code == "indeed_job_expired"


@pytest.mark.parametrize(
    "source_url",
    (
        "https://www.indeed.com/q-Platform-Engineer-jobs.html?vjk=c01efd207eb8d10a",
        "https://www.indeed.com/jobs?q=Platform+Engineer&vjk=c01efd207eb8d10a",
        "https://www.indeed.com/jobs/search?vjk=c01efd207eb8d10a",
        "https://www.indeed.com/company/acme/jobs?vjk=c01efd207eb8d10a",
    ),
)
def test_indeed_selected_result_page_cannot_prove_open_from_card_identity(
    source_url: str,
):
    identity = reconciliation.posting_identity(source_url)

    assert identity.is_detail is True
    assert reconciliation._posting_url_supports_open_evidence(source_url) is False


def test_indeed_selected_result_page_cannot_expire_a_different_rendered_panel():
    source_url = (
        "https://www.indeed.com/q-Software-Engineer-jobs.html"
        "?vjk=c01efd207eb8d10a"
    )
    candidate = reconciliation.StatusUrlCandidate(
        source_url,
        "canonical_source",
        "indeed",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_indeed_unbound_panel",
        status="active",
        source_type="gmail_alert",
        source_url=source_url,
        company="Acme",
        title="Software Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class Resolver:
        def resolve(self, _hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": source_url,
                    "raw_text": (
                        "Matching result card\n"
                        "Software Engineer\n"
                        "Acme\n"
                        "Job Post Details\n"
                        "Registered Nurse\n"
                        "Hospital X\n"
                        "This job has expired on Indeed\n"
                        "Return to Search"
                    ),
                    "confidence": 1.0,
                    "evidence_origin": "browser_dom",
                },
            }

    decision = reconciliation._rendered_check(target, candidate, Resolver())

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "rendered_identity_inconclusive"


@pytest.mark.parametrize(
    "page_text",
    (
        (
            "Software Engineer\nAcme\n"
            "This job has expired on Indeed\nReturn to Search"
        ),
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Software Engineer\nAcme"
        ),
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Registered Nurse\nHospital X\n"
            "Full job description\n"
            "Work with Acme as a Software Engineer\nReturn to Search"
        ),
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Software Engineer\nJob details"
        ),
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Acme\nJob details"
        ),
    ),
)
def test_indeed_selected_result_requires_bound_matching_header(
    page_text: str,
):
    source_url = (
        "https://www.indeed.com/q-Software-Engineer-jobs.html"
        "?vjk=c01efd207eb8d10a"
    )

    decision = reconciliation.infer_status_from_page(
        source_url,
        page_text,
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is None


def test_indeed_selected_header_rejects_short_token_collision():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=AI&vjk=c01efd207eb8d10a",
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Paid Media Manager\nAcme\nJob details"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="AI",
    )

    assert decision is None
def test_indeed_selected_header_does_not_treat_location_as_company():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=program&vjk=c01efd207eb8d10a",
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Program Manager - job post\nAcme Corp\nRemote\nFull-time\n"
            "Job details"
        ),
        evidence_scope="rendered_page",
        expected_company="Remote",
        expected_title="Program Manager",
    )

    assert decision is None




@pytest.mark.parametrize(
    "selected_title",
    (
        "Senior Software Engineer",
        "Software Engineer II",
    ),
)
def test_indeed_selected_header_rejects_similar_title_for_same_company(
    selected_title: str,
):
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            f"{selected_title}\nAcme\nJob details"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is None


def test_indeed_selected_header_requires_company_on_a_distinct_line():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Acme Software Engineer\nJob details"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Acme Software Engineer",
    )

    assert decision is None


def test_indeed_selected_header_allows_only_trailing_job_post_title_label():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
        (
            "Job Post Details\nThis job has expired on Indeed\n"
            "Software Engineer - job post\nAcme\nJob details"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is not None
    assert decision.result == "expired"


def test_indeed_selected_result_matching_header_is_expired():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
        (
            "Other search cards\nJob Post Details\n"
            "This job has expired on Indeed\nSoftware Engineer - job post\nAcme\n"
            "Job details\nOther content"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is not None
    assert decision.result == "expired"
    assert decision.evidence_code == "indeed_job_expired"


def test_indeed_documented_expired_pane_shape_binds_exact_job():
    decision = reconciliation.infer_status_from_page(
        "https://www.indeed.com/jobs?q=engineer&vjk=c01efd207eb8d10a",
        (
            "Other result cards\n"
            "Job Post Details\n"
            "This job has expired on Indeed\n"
            "Reasons could include: the employer is not accepting applications, "
            "is not actively hiring, or is reviewing applications\n"
            "Software Engineer - job post\n"
            "Image\n"
            "Acme\n"
            "Remote\n"
            "Full-time\n"
            "Job details"
        ),
        evidence_scope="rendered_page",
        expected_company="Acme",
        expected_title="Software Engineer",
    )

    assert decision is not None
    assert decision.result == "expired"
    assert decision.evidence_code == "indeed_job_expired"

def test_render_quota_defers_preferred_candidate_before_lower_terminal_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    preferred_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    lower_priority_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    created = _ingest_job(settings, source_url=lower_priority_url)
    _set_official_url(settings, created["id"], preferred_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    checked_urls: list[str] = []

    def check(_target, candidate, _settings, **_kwargs):
        checked_urls.append(candidate.url)
        if candidate.url == preferred_url:
            return RenderRequired(
                "workday_403_render_required",
                candidate.url,
                candidate.source_kind,
                http_status=403,
            )
        pytest.fail(
            "a lower-priority URL cannot supersede an unobserved preferred render"
        )

    monkeypatch.setattr(reconciliation, "_direct_check", check)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=0,
        now=observed_at,
    )

    assert checked_urls == [preferred_url]
    assert summary["checked"] == 0
    assert summary["closed"] == 0
    assert summary["expired"] == 0
    assert summary["inconclusive"] == 0
    assert summary["failed"] == 0
    assert summary["deferred"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert job.status == "active"
        assert job.status_last_checked_at is None
        assert job.status_check_result is None
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(hours=2)
        assert db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalars().all() == []
        assert run.checked_count == 0
        assert run.deferred_count == 1


def test_render_deferral_persists_prior_completed_404_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    official_url = "https://careers.acme.test/jobs/123"
    linkedin_url = "https://www.linkedin.com/jobs/view/2345678901"
    created = _ingest_job(settings, source_url=linkedin_url)
    _set_official_url(settings, created["id"], official_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def pending_404(_target, candidate, _settings, **_kwargs):
        assert candidate.url == official_url
        return StatusCheckDecision(
            "inconclusive",
            "generic_http_404_pending_confirmation",
            candidate.url,
            candidate.source_kind,
            http_status=404,
            retryable=True,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", pending_404)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=0,
        now=observed_at,
    )

    assert summary["deferred"] == 1
    assert summary["failed"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalars().all()
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert len(checks) == 1
        assert checks[0].result == "inconclusive"
        assert checks[0].evidence_code == "generic_http_404_pending_confirmation"
        assert checks[0].source_url == official_url
        assert job.status == "active"
        assert job.status_check_not_found_count == 1
        assert _as_utc(job.status_last_checked_at) == observed_at
        assert _as_utc(job.status_next_check_at) >= observed_at + timedelta(hours=6)
        assert run.failed_count == 0
        assert run.deferred_count == 1
        assert run.checked_count == 1
        assert run.inconclusive_count == 1


def test_later_open_result_uses_open_schedule_not_prior_rate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    preferred_url = "https://boards.greenhouse.io/acme/jobs/8888888"
    fallback_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    created = _ingest_job(settings, source_url=fallback_url)
    _set_official_url(settings, created["id"], preferred_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def check(_target, candidate, _settings, **_kwargs):
        if candidate.url == preferred_url:
            return StatusCheckDecision(
                "inconclusive",
                "http_429_rate_limited",
                candidate.url,
                candidate.source_kind,
                http_status=429,
                retryable=True,
                retry_after_seconds=10 * 24 * 60 * 60,
            )
        assert candidate.url == fallback_url
        return StatusCheckDecision(
            "open",
            "posting_identity_match",
            candidate.url,
            candidate.source_kind,
            http_status=200,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", check)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        now=observed_at,
    )

    assert summary["open"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        check_row = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalar_one()
        assert job.status == "active"
        assert job.status_check_result == "open"
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(hours=24)
        assert check_row.source_url == fallback_url


def test_unavailable_workday_renderer_uses_unsupported_weekly_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    monkeypatch.setattr(
        reconciliation,
        "_direct_check",
        lambda _target, candidate, _settings, **_kwargs: RenderRequired(
            "workday_403_render_required",
            candidate.url,
            candidate.source_kind,
            retry_after_seconds=10 * 24 * 60 * 60,
            http_status=403,
        ),
    )

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=1,
        now=observed_at,
    )

    assert summary["skipped"] == 1
    assert summary["deferred"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        check_row = db.execute(
            select(JobStatusCheck).where(JobStatusCheck.job_id == created["id"])
        ).scalar_one()
        assert job.status == "active"
        assert job.status_check_result == "unsupported"
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(days=7)
        assert check_row.evidence_code == "rendered_resolver_unavailable"


def test_pending_generic_404_preserves_longer_retry_after() -> None:
    retry_after_seconds = 48 * 60 * 60

    decision = classify_http_status(
        404,
        source_kind="generic",
        generic_404_count=0,
        source_url="https://careers.acme.test/jobs/123",
        retry_after_seconds=retry_after_seconds,
    )

    assert decision.result == "inconclusive"
    assert decision.evidence_code == "generic_http_404_pending_confirmation"
    assert decision.retry_after_seconds == retry_after_seconds


def test_generic_404_confirmation_does_not_cross_case_distinct_urls() -> None:
    first_url = "https://careers.example.com/jobs/Platform-Engineer"
    changed_url = "https://careers.example.com/jobs/platform-engineer"
    first_checked_at = datetime(2026, 7, 27, 1, tzinfo=UTC)
    target = reconciliation.JobStatusTarget(
        job_id="job_case_sensitive_url",
        status="active",
        source_type="manual",
        source_url=changed_url,
        company="Acme",
        title="Platform Engineer",
        location="Remote",
        candidates=(),
        failure_count=1,
        not_found_count=1,
        last_checked_at=first_checked_at,
        last_check_source_url=first_url,
    )

    count = reconciliation._eligible_generic_404_count(
        target,
        changed_url,
        observed_at=first_checked_at + timedelta(hours=7),
    )
    decision = classify_http_status(
        404,
        source_kind="generic",
        generic_404_count=count,
        source_url=changed_url,
    )

    assert count == 0
    assert decision.result == "inconclusive"
    assert decision.evidence_code == "generic_http_404_pending_confirmation"


def test_status_candidates_never_unwrap_arbitrary_stored_query_parameters() -> None:
    stored_url = (
        "https://careers.acme.example/job?jobId=123&"
        "url=https%3A%2F%2Fboards.greenhouse.io%2Fother%2Fjobs%2F999"
    )
    job = SimpleNamespace(
        source={
            "source_resolution": {
                "official_url": stored_url,
            },
        },
        application={},
        source_url=None,
    )

    candidates = ordered_status_urls(job)

    assert len(candidates) == 1
    assert candidates[0].url == stored_url
    assert candidates[0].kind == "official_ats"
    assert candidates[0].source_kind == "generic"


def test_status_candidates_dedupe_provider_identity_without_rewriting_stored_url() -> None:
    preferred_url = (
        "https://www.linkedin.com/jobs/view/platform-engineer-4425912315"
    )
    equivalent_url = "https://www.linkedin.com/jobs/view/4425912315"
    job = SimpleNamespace(
        source={
            "source_resolution": {
                "official_url": preferred_url,
            },
        },
        application={},
        source_url=equivalent_url,
    )

    candidates = ordered_status_urls(job)

    assert len(candidates) == 1
    assert candidates[0].url == preferred_url
    assert candidates[0].kind == "official_ats"
    assert candidates[0].source_kind == "linkedin"


def test_budget_exhaustion_without_observation_defers_without_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    monkeypatch.setattr(
        reconciliation,
        "_direct_check",
        lambda *_args, **_kwargs: DeferredRequired(
            "runtime_budget_exhausted",
            source_url,
        ),
    )

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        now=observed_at,
    )

    assert summary["checked"] == 0
    assert summary["deferred"] == 1
    assert summary["inconclusive"] == 0
    assert summary["failed"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert job.status_check_failure_count == 0
        assert job.status_last_checked_at is None
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(hours=2)
        assert db.execute(select(JobStatusCheck)).scalars().all() == []
        assert run.checked_count == 0
        assert run.deferred_count == 1


def test_budget_exhaustion_persists_only_prior_completed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    official_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    fallback_url = "https://boards.greenhouse.io/acme/jobs/8888888"
    created = _ingest_job(settings, source_url=fallback_url)
    _set_official_url(settings, created["id"], official_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def check(_target, candidate, _settings, **_kwargs):
        if candidate.url == official_url:
            return StatusCheckDecision(
                "inconclusive",
                "generic_http_404_pending_confirmation",
                candidate.url,
                candidate.source_kind,
                http_status=404,
                retryable=True,
            )
        assert candidate.url == fallback_url
        return DeferredRequired(
            "runtime_budget_exhausted",
            candidate.url,
        )

    monkeypatch.setattr(reconciliation, "_direct_check", check)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        now=observed_at,
    )

    assert summary["checked"] == 1
    assert summary["inconclusive"] == 1
    assert summary["deferred"] == 1
    assert summary["failed"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(select(JobStatusCheck)).scalars().all()
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert len(checks) == 1
        assert checks[0].source_url == official_url
        assert checks[0].evidence_code == "generic_http_404_pending_confirmation"
        assert job.status_check_failure_count == 1
        assert job.status_check_not_found_count == 1
        assert _as_utc(job.status_next_check_at) >= observed_at + timedelta(hours=6)
        assert run.checked_count == 1
        assert run.inconclusive_count == 1
        assert run.deferred_count == 1


def test_unavailable_scheduled_renderer_is_unsupported_not_source_failure(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = "https://www.linkedin.com/jobs/view/3456789012"
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=1,
        now=observed_at,
    )

    assert summary["checked"] == 1
    assert summary["skipped"] == 1
    assert summary["inconclusive"] == 0
    assert summary["failed"] == 0
    assert summary["deferred"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(select(JobStatusCheck)).scalars().all()
        run = db.get(JobStatusScanRun, summary["run_id"])
        assert job.status == "active"
        assert job.status_check_result == "unsupported"
        assert job.status_check_evidence_code == "rendered_resolver_unavailable"
        assert job.status_check_failure_count == 0
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(days=7)
        assert len(checks) == 1
        assert checks[0].result == "unsupported"
        assert checks[0].evidence_code == "rendered_resolver_unavailable"
        assert run.checked_count == 1
        assert run.skipped_count == 1
        assert run.inconclusive_count == 0
        assert run.failed_count == 0


def test_indeed_local_jobcard_identity_is_never_fetched_or_rendered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    init_database(settings)
    source_url = f"https://ca.indeed.com/pagead/clk?jobcard={'a' * 64}"
    created = _ingest_job(settings, source_url=source_url)
    observed_at = datetime(2026, 7, 27, 12, tzinfo=UTC)

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("local Indeed jobcard identities must not be fetched or rendered")

    monkeypatch.setattr(reconciliation, "_direct_check", unexpected_call)
    monkeypatch.setattr(
        reconciliation,
        "_rendered_resolver_for_remaining_budget",
        unexpected_call,
    )

    summary = run_status_reconciliation(
        settings,
        job_id=created["id"],
        rendered_limit=1,
        now=observed_at,
    )

    assert summary["checked"] == 1
    assert summary["skipped"] == 1
    assert summary["inconclusive"] == 0
    assert summary["failed"] == 0
    assert summary["deferred"] == 0
    with session_scope(settings) as db:
        job = db.get(JobPosting, created["id"])
        checks = db.execute(select(JobStatusCheck)).scalars().all()
        assert job.status == "active"
        assert job.status_check_result == "unsupported"
        assert job.status_check_evidence_code == "stored_url_not_job_detail"
        assert job.status_check_failure_count == 0
        assert _as_utc(job.status_next_check_at) == observed_at + timedelta(days=7)
        assert len(checks) == 1
        assert checks[0].source_url == source_url
        assert checks[0].result == "unsupported"
        assert checks[0].evidence_code == "stored_url_not_job_detail"


def test_deadline_after_unavailable_renderer_purely_defers_later_candidate() -> None:
    first = reconciliation.StatusUrlCandidate(
        "https://www.linkedin.com/jobs/view/3456789012",
        "canonical_source",
        "linkedin",
    )
    second = reconciliation.StatusUrlCandidate(
        "https://boards.greenhouse.io/acme/jobs/7777777",
        "aggregator_fallback",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_deadline_after_unsupported_render",
        status="active",
        source_type="gmail_alert",
        source_url=first.url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(first, second),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )
    request = reconciliation.RenderRequest(
        target,
        first,
        resume_candidate_index=1,
    )
    deferred: list[tuple[object, object, object]] = []

    reconciliation._continue_after_render(
        request,
        StatusCheckDecision(
            "unsupported",
            "rendered_resolver_unavailable",
            first.url,
            first.source_kind,
        ),
        settings=Settings(),
        deadline=0,
        observation_clock=lambda: datetime(2026, 7, 27, 12, tzinfo=UTC),
        host_locks={},
        host_locks_guard=threading.Lock(),
        queue_or_record=lambda *_args, **_kwargs: pytest.fail(
            "continuation must not start after the deadline"
        ),
        record_observation=lambda *_args, **_kwargs: pytest.fail(
            "unsupported is not a completed source observation"
        ),
        defer_with_prior=lambda *args: deferred.append(args),
    )

    assert len(deferred) == 1
    assert deferred[0][0] is target
    assert deferred[0][1].result == "unsupported"
    assert deferred[0][2] is None


class _AllowAllPolicy:
    def __init__(self, **_kwargs) -> None:
        pass

    def check(self, _url: str) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, reason="allowed")


def test_deadline_limited_direct_timeout_is_pure_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_url = "https://boards.greenhouse.io/acme/jobs/7777777"
    candidate = reconciliation.StatusUrlCandidate(
        target_url,
        "official_ats",
        "greenhouse",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_deadline_limited_direct_timeout",
        status="active",
        source_type="gmail_alert",
        source_url=target_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class TimeoutClient:
        def __init__(self, *, timeout, **_kwargs):
            self.timeout = timeout
            self.url_policy = _AllowAllPolicy()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url: str, **_kwargs):
            raise httpx.ReadTimeout(
                "deadline-limited read",
                request=httpx.Request("GET", url),
            )

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "UrlFetchPolicy", _AllowAllPolicy)
    monkeypatch.setattr(reconciliation, "PinnedHttpClient", TimeoutClient)

    resolution = reconciliation._direct_check(
        target,
        candidate,
        Settings(posting_direct_fetch_timeout_seconds=60),
        deadline=time.monotonic() + 0.1,
    )

    assert isinstance(resolution, DeferredRequired)
    assert resolution.evidence_code == "runtime_budget_exhausted"


def test_deadline_limited_workday_timeout_is_pure_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_url = (
        "https://acme.wd5.myworkdayjobs.com/Careers/job/"
        "Platform-Engineer_R123"
    )
    candidate = reconciliation.StatusUrlCandidate(
        target_url,
        "official_ats",
        "workday",
    )
    target = reconciliation.JobStatusTarget(
        job_id="job_deadline_limited_workday_timeout",
        status="active",
        source_type="gmail_alert",
        source_url=target_url,
        company="Acme",
        title="Platform Engineer",
        location="Canada",
        candidates=(candidate,),
        failure_count=0,
        not_found_count=0,
        last_checked_at=None,
    )

    class TimeoutResolver:
        def __init__(self, **_kwargs):
            pass

        def resolve(self, url: str):
            raise httpx.ReadTimeout(
                "deadline-limited Workday read",
                request=httpx.Request("GET", url),
            )

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "AtsApplyPageResolver", TimeoutResolver)

    resolution = reconciliation._workday_check(
        target,
        candidate,
        Settings(posting_direct_fetch_timeout_seconds=60),
        reconciliation.UrlFetchPolicy(),
        deadline=time.monotonic() + 0.1,
    )

    assert isinstance(resolution, DeferredRequired)
    assert resolution.evidence_code == "runtime_budget_exhausted"


def test_host_lock_wait_does_not_start_request_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_request_started = threading.Event()
    release_first_request = threading.Event()
    request_count = 0
    request_count_lock = threading.Lock()

    class BlockingClient:
        def __init__(self, *, timeout, **_kwargs) -> None:
            self.timeout = timeout
            self.url_policy = _AllowAllPolicy()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url: str, **_kwargs) -> httpx.Response:
            nonlocal request_count
            with request_count_lock:
                request_index = request_count
                request_count += 1
            if request_index == 0:
                first_request_started.set()
                assert release_first_request.wait(timeout=2)
            return httpx.Response(200, request=httpx.Request("GET", url))

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "UrlFetchPolicy", _AllowAllPolicy)
    monkeypatch.setattr(reconciliation, "PinnedHttpClient", BlockingClient)
    settings = Settings(posting_direct_fetch_timeout_seconds=1.0)
    host_locks: dict[str, threading.Lock] = {}
    host_locks_guard = threading.Lock()
    deadline = time.monotonic() + 0.08

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            reconciliation._get_with_safe_redirects,
            "https://same.example.test/jobs/1",
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
        assert first_request_started.wait(timeout=2)
        second = executor.submit(
            reconciliation._get_with_safe_redirects,
            "https://same.example.test/jobs/2",
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
        sleep_seconds = max(0.0, deadline - time.monotonic()) + 0.03
        time.sleep(sleep_seconds)
        release_first_request.set()
        with pytest.raises(TimeoutError):
            first.result(timeout=2)
        with pytest.raises(TimeoutError):
            second.result(timeout=2)

    assert request_count == 1


def test_host_lock_wait_recalculates_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_request_started = threading.Event()
    release_first_request = threading.Event()
    request_observations: list[tuple[float, object]] = []
    request_count_lock = threading.Lock()

    class TimeoutRecordingClient:
        def __init__(self, *, timeout, **_kwargs) -> None:
            self.timeout = timeout
            self.url_policy = _AllowAllPolicy()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url: str, **kwargs) -> httpx.Response:
            effective_timeout = kwargs.get("timeout", self.timeout)
            with request_count_lock:
                request_index = len(request_observations)
                request_observations.append((time.monotonic(), effective_timeout))
            if request_index == 0:
                first_request_started.set()
                assert release_first_request.wait(timeout=2)
            return httpx.Response(200, request=httpx.Request("GET", url))

        def close(self):
            pass

    monkeypatch.setattr(reconciliation, "UrlFetchPolicy", _AllowAllPolicy)
    monkeypatch.setattr(reconciliation, "PinnedHttpClient", TimeoutRecordingClient)
    settings = Settings(posting_direct_fetch_timeout_seconds=1.0)
    host_locks: dict[str, threading.Lock] = {}
    host_locks_guard = threading.Lock()
    deadline = time.monotonic() + 0.6

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            reconciliation._get_with_safe_redirects,
            "https://same.example.test/jobs/1",
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
        assert first_request_started.wait(timeout=2)
        second = executor.submit(
            reconciliation._get_with_safe_redirects,
            "https://same.example.test/jobs/2",
            settings,
            deadline=deadline,
            host_locks=host_locks,
            host_locks_guard=host_locks_guard,
        )
        time.sleep(0.15)
        release_first_request.set()
        assert first.result(timeout=2)[0].status_code == 200
        assert second.result(timeout=2)[0].status_code == 200

    assert len(request_observations) == 2
    second_started_at, second_timeout = request_observations[1]
    assert _timeout_ceiling(second_timeout) <= max(
        0.001,
        deadline - second_started_at,
    ) + 0.03


def _timeout_ceiling(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    components = [
        getattr(value, name, None)
        for name in ("connect", "read", "write", "pool")
    ]
    numeric_components = [
        float(component)
        for component in components
        if isinstance(component, (int, float))
    ]
    assert numeric_components, f"unsupported timeout value: {value!r}"
    return max(numeric_components)
