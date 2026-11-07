from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from jobcockpit.api import job_reprocess_runs as reprocess_queue
from jobcockpit.api import page_capture_runs as capture_queue
from jobcockpit.services import workflows as api_main
from jobcockpit.api.background_worker_leases import (
    API_BACKGROUND_WORKER_LEASE,
    BackgroundWorkerLeaseGuard,
    acquire_background_worker_lease,
    background_worker_lease_is_valid,
    bootstrap_background_worker,
    current_background_worker_lease_token,
)
from jobcockpit.api.job_merge import JOB_POSTING_FOREIGN_KEY_RELATIONSHIPS
from jobcockpit.api.page_capture_runs import (
    backfill_job_source_identities,
    resolve_job_by_source_identity,
    sync_job_source_identities,
)
from jobcockpit.db.models import (
    Base,
    BackgroundWorkerLease,
    JobPosting,
    JobReprocessRun,
    JobSourceIdentity,
    PageCaptureRun,
    PageCaptureSnapshot,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.posting_discovery import (
    CaptureProcessingAction,
    LinkedInJobBoardAdapter,
    assess_page_capture,
    capture_processing_action,
    capture_source_identity,
)


@pytest.mark.parametrize(
    ("source_url", "provider", "strength"),
    [
        ("https://www.linkedin.com/jobs/view/1234567890", "linkedin", "authoritative"),
        ("https://ca.indeed.com/viewjob?jk=abc123", "indeed", "authoritative"),
        ("https://jobs.example.com/opening?id=42", "generic", "weak"),
    ],
)
def test_capture_identity_strength_matrix(source_url, provider, strength):
    identity = capture_source_identity(source_url)

    assert identity is not None
    assert identity.provider == provider
    assert identity.strength == strength


@pytest.mark.parametrize(
    ("source_url", "provider"),
    [
        (
            "https://jobs.lever.co/example/8d5dd45f-a2f7-4f61-b789-3df32b14bc27"
            "?lever-source=Indeed",
            "lever",
        ),
        ("https://jobs-example.icims.com/jobs/12345/example-role/job?mobile=true", "icims"),
        (
            "https://careersen-example.icims.com/jobs/12345/example-role/job?mobile=true",
            "icims",
        ),
    ],
)
def test_recognized_ats_detail_capture_identity_is_dedup_safe(source_url, provider):
    identity = capture_source_identity(source_url)

    assert identity is not None
    assert identity.provider == provider
    assert identity.strength == "authoritative"
    assert identity.source_url_dedup_safe is True


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.linkedin.com/feed/?currentJobId=1234567890",
        "https://www.indeed.com/account?vjk=abc123",
    ],
)
def test_provider_non_job_pages_never_receive_authoritative_identity(source_url):
    identity = capture_source_identity(source_url)

    assert identity is not None
    assert identity.strength == "weak"


@pytest.mark.parametrize(
    ("source_url", "dedup_safe", "public_source_url", "canonical_source_url"),
    [
        (
            "https://jobs.example.com/careers?posting_id=alpha&signature=secret",
            True,
            "https://jobs.example.com/careers",
            "https://jobs.example.com/careers?posting_id=alpha",
        ),
        (
            "https://jobs.example.com/careers?opaque_posting=alpha&signature=secret",
            False,
            "https://jobs.example.com/careers",
            "https://jobs.example.com/careers",
        ),
        (
            "https://jobs.example.com/careers",
            True,
            "https://jobs.example.com/careers",
            "https://jobs.example.com/careers",
        ),
    ],
)
def test_generic_capture_identity_distinguishes_locator_from_public_projection(
    source_url,
    dedup_safe,
    public_source_url,
    canonical_source_url,
):
    identity = capture_source_identity(source_url)

    assert identity is not None
    assert identity.source_url_dedup_safe is dedup_safe
    assert identity.public_source_url == public_source_url
    assert identity.canonical_source_url == canonical_source_url
    assert "secret" not in identity.public_source_url


def test_generic_fragment_routes_keep_distinct_private_capture_identities():
    first = capture_source_identity("https://jobs.example.com/#/jobs/alpha")
    second = capture_source_identity("https://jobs.example.com/#/jobs/beta")

    assert first is not None
    assert second is not None
    assert first.identity_hash != second.identity_hash
    assert first.source_url_dedup_safe is False
    assert second.source_url_dedup_safe is False
    assert first.public_source_url == "https://jobs.example.com/"
    assert second.public_source_url == "https://jobs.example.com/"
    assert "#" not in first.canonical_source_url
    assert "#" not in second.canonical_source_url


def test_generic_fragment_routes_do_not_supersede_each_other(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)

    first = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/#/jobs/alpha",
            "page_title": "Engineer | Alpha",
            "visible_text": "Engineer\nCompany: Alpha",
        },
    ).json()
    second = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/#/jobs/beta",
            "page_title": "Accountant | Beta",
            "visible_text": "Accountant\nCompany: Beta",
        },
    ).json()

    assert first["capture_id"] != second["capture_id"]
    assert client.get(first["status_url"]).json()["status"] == "queued"
    assert client.get(second["status_url"]).json()["status"] == "queued"


def test_linkedin_terminal_status_accepts_markers_split_across_lines():
    terminal = LinkedInJobBoardAdapter().terminal_status(
        "This job has been removed.\nThe job ID is not valid."
    )

    assert terminal is not None
    assert terminal.status == "closed"
    assert terminal.evidence_code == "linkedin_job_removed_or_invalid"


@pytest.mark.parametrize(
    "page_text",
    [
        "This job has been removed.",
        "The job ID is not valid.",
        "Job id provided may not be valid or the job posting has been removed.",
    ],
)
def test_linkedin_terminal_status_accepts_each_terminal_marker(page_text):
    terminal = LinkedInJobBoardAdapter().terminal_status(page_text)

    assert terminal is not None
    assert terminal.status == "closed"
    assert terminal.evidence_code == "linkedin_job_removed_or_invalid"


@pytest.mark.parametrize(
    ("source_url", "page_text", "access", "page_kind", "action"),
    [
        (
            "https://www.linkedin.com/jobs/view/1234567890",
            "Backend Intern\\nCompany: Acme",
            "usable",
            "posting",
            CaptureProcessingAction.PARSE,
        ),
        (
            "https://www.linkedin.com/jobs/view/1234567890",
            "This job has been removed or the job ID is not valid.",
            "usable",
            "terminal_posting",
            CaptureProcessingAction.APPLY_TERMINAL,
        ),
        (
            "https://www.linkedin.com/jobs/view/1234567890",
            ("Sign in to continue\\nThis job has been removed or the job ID is not valid."),
            "authentication_wall",
            "posting",
            CaptureProcessingAction.REJECT,
        ),
        (
            "https://www.linkedin.com/feed/?trk=secret",
            "This job has been removed or the job ID is not valid.",
            "usable",
            "non_job",
            CaptureProcessingAction.REJECT,
        ),
        (
            "https://jobs.example.com/opening/42",
            "   ",
            "incomplete",
            "unknown",
            CaptureProcessingAction.REJECT,
        ),
    ],
)
def test_capture_assessment_controls_the_only_mutation_gate(
    source_url,
    page_text,
    access,
    page_kind,
    action,
):
    assessment = assess_page_capture(source_url, page_text)

    assert assessment.access_verdict.value == access
    assert assessment.page_kind.value == page_kind
    assert capture_processing_action(assessment) is action


def test_job_merge_relationship_matrix_covers_every_job_foreign_key():
    actual = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(
            foreign_key.target_fullname == "job_postings.id" for foreign_key in column.foreign_keys
        )
    }

    assert actual == JOB_POSTING_FOREIGN_KEY_RELATIONSHIPS


def test_worker_commit_requires_the_exact_live_lease_generation(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(background_worker_lease_seconds=30)
    jobcockpit_client_factory(settings)

    with session_scope(settings) as db:
        assert background_worker_lease_is_valid(db, "worker_a", "missing") is False

    assert acquire_background_worker_lease(settings, "worker_a") is True
    first_token = current_background_worker_lease_token(settings, "worker_a")
    assert first_token

    with session_scope(settings) as db:
        lease = db.get(BackgroundWorkerLease, API_BACKGROUND_WORKER_LEASE)
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert acquire_background_worker_lease(settings, "worker_b") is True
    second_token = current_background_worker_lease_token(settings, "worker_b")
    assert second_token and second_token != first_token

    with session_scope(settings) as db:
        assert background_worker_lease_is_valid(db, "worker_a", first_token) is False
        assert background_worker_lease_is_valid(db, "worker_b", second_token) is True


def test_stale_supervisor_generation_cannot_claim_queue_work(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    capture = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/fenced-claim",
            "page_title": "Intern | Acme",
            "visible_text": "Intern\nCompany: Acme",
        },
    ).json()
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Intern\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        db.add(
            JobReprocessRun(
                reprocess_run_id="reprocess_fenced_claim",
                job_id=job["id"],
                run_type="full",
                status="queued",
                result={},
            )
        )

    assert acquire_background_worker_lease(settings, "worker_old") is True
    old_token = current_background_worker_lease_token(settings, "worker_old")
    assert old_token
    with session_scope(settings) as db:
        lease = db.get(BackgroundWorkerLease, API_BACKGROUND_WORKER_LEASE)
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert acquire_background_worker_lease(settings, "worker_new") is True

    assert (
        capture_queue.claim_next_page_capture(
            settings,
            worker_id="worker_old",
            supervisor_lease_token=old_token,
        )
        is None
    )
    assert (
        reprocess_queue.claim_next_job_reprocess(
            settings,
            worker_id="worker_old",
            supervisor_lease_token=old_token,
        )
        is None
    )
    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, capture["capture_id"]).status == "queued"
        assert db.get(JobReprocessRun, "reprocess_fenced_claim").status == "queued"


def test_stale_supervisor_generation_cannot_recover_queue_work(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    capture = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/fenced-recovery",
            "page_title": "Intern | Acme",
            "visible_text": "Intern\nCompany: Acme",
        },
    ).json()
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Intern\nCompany: Acme"},
    ).json()
    with session_scope(settings) as db:
        db.add(
            JobReprocessRun(
                reprocess_run_id="reprocess_fenced_recovery",
                job_id=job["id"],
                run_type="full",
                status="queued",
                result={},
            )
        )

    assert acquire_background_worker_lease(settings, "worker_old") is True
    old_token = current_background_worker_lease_token(settings, "worker_old")
    assert old_token
    assert capture_queue.claim_next_page_capture(
        settings,
        worker_id="worker_old",
        supervisor_lease_token=old_token,
    )
    assert reprocess_queue.claim_next_job_reprocess(
        settings,
        worker_id="worker_old",
        supervisor_lease_token=old_token,
    )
    with session_scope(settings) as db:
        lease = db.get(BackgroundWorkerLease, API_BACKGROUND_WORKER_LEASE)
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert acquire_background_worker_lease(settings, "worker_new") is True

    assert (
        capture_queue.recover_stale_page_capture_runs(
            settings,
            recover_owned_by="worker_old",
            required_worker_id="worker_old",
            required_lease_token=old_token,
        )
        == 0
    )
    assert (
        reprocess_queue.recover_stale_job_reprocess_runs(
            settings,
            recover_owned_by="worker_old",
            required_worker_id="worker_old",
            required_lease_token=old_token,
        )
        == 0
    )
    with session_scope(settings) as db:
        assert db.get(PageCaptureRun, capture["capture_id"]).status == "running"
        assert db.get(JobReprocessRun, "reprocess_fenced_recovery").status == "running"


def test_shared_worker_bootstrap_reconciles_identity_registry(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "source_url": "https://www.linkedin.com/jobs/view/1234567890",
            "raw_text": "Backend Intern\nCompany: Acme",
        },
    ).json()
    with session_scope(settings) as db:
        for identity in db.execute(
            select(JobSourceIdentity).where(JobSourceIdentity.job_id == job["id"])
        ).scalars():
            db.delete(identity)

    with BackgroundWorkerLeaseGuard(settings, "bootstrap_worker") as lease:
        assert lease.acquired and lease.lease_token
        backfilled, recovered_captures, recovered_reprocesses = bootstrap_background_worker(
            settings,
            worker_id="bootstrap_worker",
            lease_token=lease.lease_token,
        )

    assert backfilled == 1
    assert recovered_captures == 0
    assert recovered_reprocesses == 0
    with session_scope(settings) as db:
        identity = db.execute(
            select(JobSourceIdentity).where(JobSourceIdentity.job_id == job["id"])
        ).scalar_one()
        assert identity.identity_strength == "authoritative"


def test_weak_identity_uses_parsed_compatibility_without_merging_other_jobs(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    first = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Platform Intern\nCompany: Alpha"},
    ).json()
    second = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Intern\nCompany: Beta"},
    ).json()
    shared_url = "https://jobs.example.com/opening?id=42"
    with session_scope(settings) as db:
        for job_id in (first["id"], second["id"]):
            job = db.get(JobPosting, job_id)
            job.source_url = shared_url
            sync_job_source_identities(db, job)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": shared_url,
            "page_title": "Backend Intern | Beta",
            "visible_text": "Backend Intern\nCompany: Beta",
        },
    ).json()
    with session_scope(settings) as db:
        run = db.get(PageCaptureRun, queued["capture_id"])
        snapshot = db.get(PageCaptureSnapshot, run.snapshot_id)
        match = resolve_job_by_source_identity(db, run, snapshot)
        parsed = api_main.parse_job_text_with_settings(
            "Backend Intern\nCompany: Beta",
            shared_url,
            settings,
            source_type="browser_capture",
        )
        assert match is not None
        assert [
            job.id
            for job in match.jobs
            if api_main.parsed_job_identity_matches_existing(parsed, job)
        ] == [second["id"]]
    processed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert processed["status"] == "succeeded"
    assert processed["job_id"] == second["id"]
    with session_scope(settings) as db:
        first_job = db.get(JobPosting, first["id"])
        second_job = db.get(JobPosting, second["id"])
        assert not (first_job.source or {}).get("merged_into_job_id")
        assert not (second_job.source or {}).get("merged_into_job_id")
        rows = list(
            db.execute(
                select(JobSourceIdentity).where(JobSourceIdentity.provider == "generic")
            ).scalars()
        )
        assert {row.identity_strength for row in rows} == {"weak"}


def test_capture_failure_logging_never_includes_provider_exception_text(
    monkeypatch,
    caplog,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(posting_parser_provider="codex_cli")
    client = jobcockpit_client_factory(settings)
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/private-failure",
            "page_title": "Private failure",
            "visible_text": "Intern\nCompany: Acme",
        },
    ).json()
    secret = "private-capture-text-and-token"

    def fail_parser(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", fail_parser)
    caplog.set_level(logging.ERROR)

    api_main.process_page_capture_run(
        queued["capture_id"], client.app.state.settings, client.app.state.storage
    )

    assert secret not in caplog.text


def test_weak_identity_parser_failure_remains_unassociated(
    monkeypatch,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Old role\nCompany: Acme"},
    ).json()
    source_url = "https://jobs.example.com/opening?id=reused"
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = source_url
        stored.source = {**stored.source, "source_url": source_url}
        sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Unknown posting",
            "visible_text": "Unreadable captured posting",
        },
    ).json()

    def fail_parser(*_args, **_kwargs):
        raise TypeError("parser contract failure")

    monkeypatch.setattr(api_main, "parse_job_text_with_settings", fail_parser)
    failed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert failed["status"] == "failed"
    assert failed["job_id"] is None
    assert client.get(f"/api/jobs/{job['id']}").json()["capture_processing"] is None


def _capture_and_process(client, source_url: str, visible_text: str) -> dict:
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Software Engineer | Example",
            "visible_text": visible_text,
        },
    )
    assert queued.status_code == 202
    result = api_main.process_page_capture_run(
        queued.json()["capture_id"],
        client.app.state.settings,
        client.app.state.storage,
    )
    assert result["status"] == "succeeded"
    return result


def test_public_posting_id_matrix_separates_postings_and_reuses_recapture(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    body = "Software Engineer\nCompany: Example"

    alpha = _capture_and_process(
        client,
        "https://jobs.example.com/careers?posting_id=alpha&signature=one",
        body,
    )
    beta = _capture_and_process(
        client,
        "https://jobs.example.com/careers?posting_id=beta&signature=two",
        body,
    )
    alpha_again = _capture_and_process(
        client,
        "https://jobs.example.com/careers?posting_id=alpha&signature=rotated",
        body,
    )

    assert alpha["job_id"] != beta["job_id"]
    assert alpha_again["job_id"] == alpha["job_id"]
    with session_scope(settings) as db:
        jobs = list(db.execute(select(JobPosting)).scalars())
        assert len(jobs) == 2
        assert {job.source_url for job in jobs} == {
            "https://jobs.example.com/careers?posting_id=alpha",
            "https://jobs.example.com/careers?posting_id=beta",
        }


def test_private_query_identity_never_dedupes_on_redacted_display_url(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    body = "Software Engineer\nCompany: Example"

    alpha = _capture_and_process(
        client,
        "https://jobs.example.com/careers?opaque_posting=alpha",
        body,
    )
    beta = _capture_and_process(
        client,
        "https://jobs.example.com/careers?opaque_posting=beta",
        body,
    )
    alpha_again = _capture_and_process(
        client,
        "https://jobs.example.com/careers?opaque_posting=alpha",
        body,
    )

    assert alpha["job_id"] != beta["job_id"]
    assert alpha_again["job_id"] == alpha["job_id"]
    with session_scope(settings) as db:
        jobs = list(db.execute(select(JobPosting)).scalars())
        assert len(jobs) == 2
        assert {job.source_url for job in jobs} == {"https://jobs.example.com/careers"}
        identities = list(
            db.execute(
                select(JobSourceIdentity).where(JobSourceIdentity.provider == "generic")
            ).scalars()
        )
        assert len({identity.identity_hash for identity in identities}) == 2, [
            (
                identity.identity_hash,
                identity.source_kind,
                identity.public_source_url,
                identity.job_id,
            )
            for identity in identities
        ]
        for identity in identities:
            db.delete(identity)
        db.flush()
        assert backfill_job_source_identities(db) == 2
        db.flush()
        restored = list(
            db.execute(
                select(JobSourceIdentity).where(JobSourceIdentity.provider == "generic")
            ).scalars()
        )
        assert len({identity.identity_hash for identity in restored}) == 2


def test_weak_identity_rejects_incompatible_recapture_instead_of_reusing_url(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://jobs.example.com/careers?posting_id=alpha"
    original = _capture_and_process(
        client,
        source_url,
        "Software Engineer\nCompany: Alpha",
    )
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Accountant | Beta",
            "visible_text": "Accountant\nCompany: Beta",
        },
    ).json()

    failed = api_main.process_page_capture_run(
        queued["capture_id"],
        settings,
        client.app.state.storage,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_identity_conflict"
    with session_scope(settings) as db:
        jobs = list(db.execute(select(JobPosting)).scalars())
        assert [job.id for job in jobs] == [original["job_id"]]
        assert jobs[0].company_name == "Alpha"


def test_auth_polluted_terminal_capture_cannot_mutate_a_job(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://www.linkedin.com/jobs/view/9876543210"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "source_url": source_url,
            "raw_text": "Backend Intern\nCompany: Acme",
        },
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "LinkedIn",
            "visible_text": ("Sign in to continue\nThis job is no longer accepting applications"),
            "target_job_id": job["id"],
        },
    ).json()

    failed = api_main.process_page_capture_run(
        queued["capture_id"],
        settings,
        client.app.state.storage,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_authentication_wall"
    assert failed["page_access_verdict"] == "authentication_wall"
    assert failed["page_kind"] == "posting"
    assert failed["identity_verdict"] == "rejected"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "active"


def test_targeted_capture_requires_verified_page_identity_before_update(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Backend Intern\nCompany: Acme",
        },
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/unrelated-role",
            "page_title": "Accountant | Beta",
            "visible_text": "Accountant\nCompany: Beta",
            "target_job_id": job["id"],
        },
    ).json()

    failed = api_main.process_page_capture_run(
        queued["capture_id"],
        settings,
        client.app.state.storage,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_identity_conflict"
    assert failed["identity_verdict"] == "rejected"
    assert failed["association_verdict"] == "conflict"
    unchanged = client.get(f"/api/jobs/{job['id']}").json()
    assert unchanged["role"]["normalized_title"] == "backend_intern"
    assert unchanged["employer"]["company_name"] == "Acme"


def test_targeted_capture_cannot_use_weak_locator_as_identity_proof(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://jobs.example.com/careers?id=reused"
    job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Software Engineer\nCompany: Alpha",
        },
    ).json()
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = source_url
        stored.source = {**stored.source, "source_url": source_url}
        sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Accountant | Beta",
            "visible_text": "Accountant\nCompany: Beta",
            "target_job_id": job["id"],
        },
    ).json()

    failed = api_main.process_page_capture_run(
        queued["capture_id"],
        settings,
        client.app.state.storage,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_identity_conflict"
    assert failed["identity_verdict"] == "rejected"
    assert failed["association_verdict"] == "conflict"
    unchanged = client.get(f"/api/jobs/{job['id']}").json()
    assert unchanged["role"]["normalized_title"] == "software_engineer"
    assert unchanged["employer"]["company_name"] == "Alpha"


def test_targeted_weak_capture_rejects_same_title_from_different_company(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Software Engineer\nCompany: Alpha"},
    ).json()
    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": "https://jobs.example.com/unrelated-software-role",
            "page_title": "Software Engineer | Beta",
            "visible_text": "Software Engineer\nCompany: Beta",
            "target_job_id": job["id"],
        },
    ).json()

    failed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_identity_conflict"
    unchanged = client.get(f"/api/jobs/{job['id']}").json()
    assert unchanged["role"]["normalized_title"] == "software_engineer"
    assert unchanged["employer"]["company_name"] == "Alpha"


def test_untargeted_weak_capture_rejects_different_role_at_same_company(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    source_url = "https://jobs.example.com/careers?posting_id=reused"
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Software Engineer\nCompany: Alpha"},
    ).json()
    with session_scope(settings) as db:
        stored = db.get(JobPosting, job["id"])
        stored.source_url = source_url
        stored.source = {**stored.source, "source_url": source_url}
        sync_job_source_identities(db, stored)

    queued = client.post(
        "/api/extension/page-captures",
        json={
            "source_url": source_url,
            "page_title": "Accountant | Alpha",
            "visible_text": "Accountant\nCompany: Alpha",
        },
    ).json()
    failed = api_main.process_page_capture_run(
        queued["capture_id"], settings, client.app.state.storage
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "capture_identity_conflict"
    unchanged = client.get(f"/api/jobs/{job['id']}").json()
    assert unchanged["role"]["normalized_title"] == "software_engineer"
    assert unchanged["employer"]["company_name"] == "Alpha"
