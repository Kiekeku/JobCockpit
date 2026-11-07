from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from fastapi import Depends, FastAPI, File, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import load_only
from jobcockpit.core.tls import install_windows_certifi_workaround
from jobcockpit.api.application_routes import create_application_router
from jobcockpit.api.browser_health import create_browser_health_router
from jobcockpit.api.extension_routes import create_extension_router
from jobcockpit.api.extension_assistance import create_extension_assistance_router
from jobcockpit.api.extension_memory import create_extension_memory_router
from jobcockpit.api.job_alert_routes import create_job_alert_router
from jobcockpit.api.profile_routes import create_profile_router
from jobcockpit.schemas.requests import (
    CoverLetterDraftRequest,
    JobOpportunityStatusBulkUpdateRequest,
    JobOpportunityStatusUpdateRequest,
    JobSourceUpdateRequest,
    WorkdayIngestRequest,
)

from jobcockpit.services import workflows
from jobcockpit.ingestion.ats_defaults import ats_provider_defaults


def create_app(
    settings: workflows.Settings | None = None,
    *,
    start_background_workers: bool = False,
) -> FastAPI:
    install_windows_certifi_workaround()
    settings = settings or workflows.Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = None
        cover_letter_worker = None
        source_worker = None
        publication_worker = None
        if start_background_workers and settings.background_workers_enabled:
            from jobcockpit.ingestion.source_reconciliation import SourceBackgroundWorker

            source_worker = SourceBackgroundWorker(settings)
            worker = workflows.ApiBackgroundWorker(settings, app.state.storage)
            cover_letter_worker = workflows.CoverLetterBackgroundWorker(settings, app.state.storage)
            app.state.background_worker = worker
            app.state.cover_letter_background_worker = cover_letter_worker
            app.state.source_background_worker = source_worker
            worker.start()
            cover_letter_worker.start()
            source_worker.start()
            if settings.publication_enabled:
                from jobcockpit.services.publication import PublicationWorker

                publication_worker = PublicationWorker(settings)
                app.state.publication_worker = publication_worker
                publication_worker.start()
        try:
            yield
        finally:
            if publication_worker:
                publication_worker.stop()
            if source_worker:
                source_worker.stop()
            if cover_letter_worker:
                cover_letter_worker.stop()
            if worker:
                worker.stop()

    app = FastAPI(title="JobCockpit API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = workflows.ArtifactStorageService(settings.artifact_root)

    def get_db() -> workflows.Session:
        with workflows.session_scope(settings) as db:
            yield db

    def get_storage() -> workflows.ArtifactStorageService:
        return app.state.storage

    app.include_router(create_browser_health_router(get_db, settings))

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "database": "sqlite", "artifact_storage": "local"}

    app.include_router(create_profile_router(get_db=get_db, get_storage=get_storage))
    if settings.publication_enabled:
        from jobcockpit.api.publication_routes import create_publication_router

        app.include_router(create_publication_router(get_db, settings))
    app.include_router(create_extension_assistance_router(get_db=get_db, get_storage=get_storage, settings=settings))
    app.include_router(create_extension_memory_router(get_db=get_db, settings=settings))

    @app.post("/api/jobs/ingest", status_code=201)
    def ingest_job(
        request: workflows.JobIngestRequest,
        db: workflows.Session = Depends(get_db),
        storage: workflows.ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        if request.source_type == "url" and request.source_url:
            profile = db.get(workflows.CandidateProfile, workflows.CANDIDATE_ID)
            profile_data = profile.profile_data if profile else None
            ingest_result = workflows.process_posting_link(
                workflows.PostingLinkContext(
                    source_type=request.source_type,
                    source_url=request.source_url,
                    raw_text=request.raw_text,
                    link_text=request.source_url,
                    trusted_posting=True,
                ),
                db=db,
                storage=storage,
                settings=settings,
                job_importer=workflows.persist_job_ingest_result,
                language_filter_profile_data=profile_data,
                skip_language_filter=request.skip_language_filter,
                posting_resolver=workflows.default_posting_resolver(settings),
            )
            if ingest_result.ingestion_status == "blocked_by_url_policy":
                raise workflows.HTTPException(
                    status_code=400,
                    detail=f"URL is blocked by policy: {ingest_result.error_message}",
                )
            if ingest_result.ingestion_status == "needs_external_resolution":
                raise workflows.HTTPException(
                    status_code=400,
                    detail=f"Posting URL requires external resolution: {ingest_result.error_message}",
                )
            if ingest_result.ingestion_status == "skipped_by_language_filter":
                raise workflows.HTTPException(
                    status_code=400,
                    detail={
                        "ingestion_status": ingest_result.ingestion_status,
                        "error_message": ingest_result.error_message,
                        "language_filter": (ingest_result.payload or {}).get("language_filter"),
                    },
                )
            if ingest_result.ingestion_status == "terminal_status_detected":
                raise workflows.HTTPException(
                    status_code=410,
                    detail={
                        "ingestion_status": ingest_result.ingestion_status,
                        "detected_status": ingest_result.detected_status,
                        "evidence_code": ingest_result.status_evidence_code,
                        "resolved_url": ingest_result.resolved_url,
                    },
                )
            if (
                not workflows.is_successful_posting_link_import(ingest_result.ingestion_status)
                or not ingest_result.payload
            ):
                raise workflows.HTTPException(
                    status_code=400,
                    detail=f"Posting URL is not importable: {ingest_result.classification.reason}",
                )
            return ingest_result.payload
        return workflows.persist_job_ingest(request, db, storage, settings)

    @app.post("/api/jobs/ingest/ats", status_code=201)
    def ingest_ats_jobs(
        request: workflows.AtsIngestRequest,
        db: workflows.Session = Depends(get_db),
        storage: workflows.ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        return workflows.ingest_ats_request(request, db, storage, settings)

    @app.post("/api/jobs/ingest/workday", status_code=201)
    def ingest_workday_jobs(
        request: WorkdayIngestRequest,
        db: workflows.Session = Depends(get_db),
        storage: workflows.ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        return workflows.ingest_ats_request(
            workflows.AtsIngestRequest(
                provider="workday",
                source_url=request.source_url,
                limit=request.limit,
                search_text=request.search_text,
                applied_facets=request.applied_facets,
                skip_interest_filter=request.skip_interest_filter,
                skip_language_filter=request.skip_language_filter,
            ),
            db,
            storage,
            settings,
        )

    @app.get("/api/ingestion-runs")
    def list_ingestion_runs(db: workflows.Session = Depends(get_db)) -> list[dict]:
        runs = db.execute(
            workflows.select(workflows.JobIngestionRun)
            .order_by(workflows.JobIngestionRun.created_at.desc())
            .limit(20)
        ).scalars()
        return [run.to_api() for run in runs]

    @app.get("/api/ingestion-runs/{run_id}/items")
    def list_ingestion_run_items(
        run_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> list[dict]:
        if not db.get(workflows.JobIngestionRun, run_id):
            raise workflows.HTTPException(status_code=404, detail="Ingestion run not found")
        items = db.execute(
            workflows.select(workflows.JobIngestionItem)
            .where(workflows.JobIngestionItem.ingestion_run_id == run_id)
            .order_by(
                workflows.JobIngestionItem.created_at, workflows.JobIngestionItem.ingestion_item_id
            )
        ).scalars()
        return [item.to_api() for item in items]

    @app.get("/api/ats/providers")
    def list_ats_providers() -> list[str]:
        return workflows.registered_ats_providers()

    @app.get("/api/ats/provider-defaults")
    def list_ats_provider_defaults() -> dict[str, dict]:
        return {
            provider: asdict(ats_provider_defaults(provider))
            for provider in workflows.registered_ats_providers()
        }

    from jobcockpit.api.job_summaries import create_job_summary_router

    app.include_router(create_job_summary_router(get_db))

    @app.get("/api/jobs")
    def list_jobs(
        status: workflows.JobOpportunityStatus | None = None,
        db: workflows.Session = Depends(get_db),
    ) -> list[dict]:
        jobs = workflows.active_job_postings(db)
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        applications = workflows.active_applications(db)
        applications_by_job = {application.job_id: application for application in applications}
        reprocess_runs_by_job = workflows.latest_job_reprocess_runs_by_job(
            db, [job.id for job in jobs]
        )
        capture_runs_by_job = workflows.latest_page_capture_processing_by_job(
            db, [job.id for job in jobs]
        )
        return [
            workflows.job_to_api(
                job,
                applications_by_job.get(job.id),
                reprocess_runs_by_job.get(job.id),
                capture_runs_by_job.get(job.id),
            )
            for job in jobs
        ]

    @app.get("/api/job-status/health")
    def get_job_status_health(db: workflows.Session = Depends(get_db)) -> dict:
        now = workflows.datetime.now(workflows.UTC)
        jobs = list(
            db.scalars(
                workflows.select(workflows.JobPosting)
                .where(
                    workflows.JobPosting.status == "active",
                    workflows.JobPosting.source["merged_into_job_id"].as_string().is_(None),
                )
                .options(
                    load_only(
                        workflows.JobPosting.status,
                        workflows.JobPosting.status_check_result,
                        workflows.JobPosting.status_check_evidence_code,
                        workflows.JobPosting.status_last_checked_at,
                        workflows.JobPosting.status_last_open_at,
                        workflows.JobPosting.status_source_repair_required,
                        workflows.JobPosting.status_next_check_at,
                    )
                )
            )
        )
        states = [
            workflows.availability_state(
                job.status,
                job.status_check_result,
                job.status_check_evidence_code,
                job.status_last_checked_at,
                job.status_last_open_at,
                job.status_source_repair_required,
                now=now,
            )
            for job in jobs
        ]
        latest_run = db.execute(
            workflows.select(workflows.JobStatusScanRun)
            .order_by(
                workflows.JobStatusScanRun.started_at.desc(),
                workflows.JobStatusScanRun.status_scan_run_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        return {
            "observed_at": workflows.api_datetime(now),
            "active_total": len(jobs),
            "due": sum(
                1
                for job in jobs
                if not job.status_source_repair_required
                and (
                    job.status_next_check_at is None
                    or (
                        job.status_next_check_at.replace(tzinfo=workflows.UTC)
                        if job.status_next_check_at.tzinfo is None
                        else job.status_next_check_at
                    )
                    <= now
                )
            ),
            "source_repair_required": states.count("source_repair_required"),
            "provider_confirmed_open": states.count("provider_confirmed_open"),
            "page_present": states.count("page_present"),
            "recently_observed_open": states.count("recently_observed_open"),
            "unverified_active": states.count("unverified_active"),
            "latest_run": latest_run.to_api() if latest_run else None,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, db: workflows.Session = Depends(get_db)) -> dict:
        job = workflows.require_active_job(db, job_id)
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return workflows.job_to_api(
            job,
            application,
            workflows.latest_job_reprocess_run(db, job_id),
            workflows.latest_page_capture_processing_for_job(db, job_id),
        )

    @app.get("/api/jobs/{job_id}/status-checks")
    def list_job_status_checks(
        job_id: str,
        limit: int = 20,
        db: workflows.Session = Depends(get_db),
    ) -> list[dict]:
        workflows.require_active_job(db, job_id)
        if limit < 1 or limit > 100:
            raise workflows.HTTPException(status_code=422, detail="limit must be between 1 and 100")
        checks = db.execute(
            workflows.select(workflows.JobStatusCheck)
            .where(workflows.JobStatusCheck.job_id == job_id)
            .order_by(
                workflows.JobStatusCheck.checked_at.desc(),
                workflows.JobStatusCheck.created_at.desc(),
                workflows.JobStatusCheck.status_check_id.desc(),
            )
            .limit(limit)
        ).scalars()
        return [check.to_api() for check in checks]

    @app.get("/api/jobs/{job_id}/source-reconciliation")
    def get_job_source_reconciliation(job_id: str, db: workflows.Session = Depends(get_db)) -> dict:
        from jobcockpit.db.models import JobSourceReconciliation
        from jobcockpit.ingestion.source_reconciliation import source_reconciliation_eligible

        job = workflows.require_active_job(db, job_id)
        run = db.get(JobSourceReconciliation, job_id)
        state = (
            run.to_api() if run else {"job_id": job_id, "status": "not_attempted", "history": []}
        )
        return {**state, "eligible": source_reconciliation_eligible(db, job)}

    @app.post("/api/jobs/{job_id}/source-reconciliation/retry")
    def retry_job_source_reconciliation(
        job_id: str, db: workflows.Session = Depends(get_db)
    ) -> dict:
        from jobcockpit.ingestion.source_reconciliation import enqueue_source_reconciliation

        job = workflows.require_active_job(db, job_id)
        run = enqueue_source_reconciliation(db, job, retry=True)
        if run is None:
            raise workflows.HTTPException(
                status_code=409, detail="This job is not eligible for source discovery"
            )
        db.flush()
        return run.to_api()

    @app.patch("/api/jobs/{job_id}/source")
    def update_job_source_url(
        job_id: str,
        request: JobSourceUpdateRequest,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        reprocess_run = workflows.latest_job_reprocess_run(db, job_id)
        if (
            reprocess_run
            and reprocess_run.status in workflows.ACTIVE_JOB_REPROCESS_STATUSES
            and not workflows.job_reprocess_run_is_stale(reprocess_run)
        ):
            raise workflows.HTTPException(
                status_code=409, detail="Wait for job re-processing to finish"
            )
        capture_processing = workflows.latest_page_capture_processing_for_job(db, job_id)
        if (
            capture_processing
            and capture_processing.get("status") in workflows.ACTIVE_PAGE_CAPTURE_STATUSES
        ):
            raise workflows.HTTPException(
                status_code=409, detail="Wait for captured page processing to finish"
            )

        source_url = workflows.sanitize_public_source_url(request.source_url)
        policy_result = workflows.UrlFetchPolicy(
            allowed_localhosts=settings.allowed_localhost_urls()
        ).check(source_url)
        if not policy_result.allowed:
            raise workflows.HTTPException(
                status_code=400,
                detail=f"URL is blocked by policy: {policy_result.reason}",
            )

        previous_source_url = job.source_url
        existing_source = job.source if isinstance(job.source, dict) else {}
        existing_resolution = (
            existing_source.get("source_resolution")
            if isinstance(existing_source.get("source_resolution"), dict)
            else {}
        )
        fallback_url = str(
            existing_resolution.get("fallback_url") or previous_source_url or ""
        ).strip()
        source_resolution = {
            "kind": "manual_override",
            "provider": "user",
            "status": "resolved",
            "path": "user_supplied_authoritative_source",
            "official_discovery_status": "resolved",
            "reason_code": "user_supplied_authoritative_source",
            "original_url": previous_source_url,
            "resolved_url": source_url,
            "canonical_url": source_url,
            "official_url": source_url,
        }
        if fallback_url:
            source_resolution["fallback_url"] = fallback_url

        workflows.apply_job_source_url(job, source_url, source_resolution, db)
        db.flush()
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return workflows.job_to_api(
            job,
            application,
            reprocess_run,
            capture_processing,
        )

    @app.patch("/api/jobs/{job_id}/status")
    def update_job_opportunity_status(
        job_id: str,
        request: JobOpportunityStatusUpdateRequest,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        set_user_job_opportunity_status(job, request.status, db)
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return workflows.job_to_api(
            job,
            application,
            workflows.latest_job_reprocess_run(db, job_id),
            workflows.latest_page_capture_processing_for_job(db, job_id),
        )

    @app.patch("/api/jobs/status/bulk")
    def bulk_update_job_opportunity_status(
        request: JobOpportunityStatusBulkUpdateRequest,
        db: workflows.Session = Depends(get_db),
    ) -> list[dict]:
        jobs = [workflows.require_active_job(db, job_id) for job_id in request.job_ids]
        for job in jobs:
            set_user_job_opportunity_status(job, request.status, db)
        db.flush()

        applications_by_job = {
            application.job_id: application
            for application in db.execute(
                workflows.select(workflows.Application).where(
                    workflows.Application.job_id.in_(request.job_ids)
                )
            ).scalars()
        }
        reprocess_runs_by_job = workflows.latest_job_reprocess_runs_by_job(db, request.job_ids)
        capture_runs_by_job = workflows.latest_page_capture_processing_by_job(db, request.job_ids)
        return [
            workflows.job_to_api(
                job,
                applications_by_job.get(job.id),
                reprocess_runs_by_job.get(job.id),
                capture_runs_by_job.get(job.id),
            )
            for job in jobs
        ]

    def set_user_job_opportunity_status(
        job: workflows.JobPosting,
        status: workflows.JobOpportunityStatus,
        db: workflows.Session,
    ) -> None:
        previous_status = job.status
        job.status = status
        workflows.reconcile_job_cover_letter_generation(job, db)
        now = workflows.datetime.now(workflows.UTC)
        job.updated_at = now
        if status == "active":
            job.status_next_check_at = now
            job.status_check_failure_count = 0
            job.status_check_not_found_count = 0
            job.status_source_repair_required = False
        else:
            job.status_next_check_at = None
        db.add(
            workflows.AuditLogEntry.create(
                "job.opportunity_status_updated",
                "job_posting",
                job.id,
                payload={"previous_status": previous_status, "status": status},
            )
        )

    @app.post("/api/jobs/{job_id}/reviewed")
    def mark_job_manual_review_completed(
        job_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        existing_data_quality = job.data_quality if isinstance(job.data_quality, dict) else {}
        previously_needed_review = existing_data_quality.get("needs_manual_review") is True
        reviewed_snapshot_hash = (
            existing_data_quality.get("manual_review_pending_snapshot_hash")
            or job.posting_snapshot_hash
        )
        now = workflows.datetime.now(workflows.UTC)
        job.data_quality = {
            **{
                key: value
                for key, value in existing_data_quality.items()
                if key != "manual_review_pending_snapshot_hash"
            },
            "needs_manual_review": False,
            "manual_review_completed_at": now.isoformat(),
            "manual_review_completed_snapshot_hash": reviewed_snapshot_hash,
        }
        job.updated_at = now
        db.add(
            workflows.AuditLogEntry.create(
                "job.manual_review_completed",
                "job_posting",
                job.id,
                payload={"previously_needed_review": previously_needed_review},
            )
        )
        db.flush()
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return workflows.job_to_api(
            job,
            application,
            workflows.latest_job_reprocess_run(db, job_id),
            workflows.latest_page_capture_processing_for_job(db, job_id),
        )

    @app.post("/api/jobs/{job_id}/reprocess", status_code=202)
    def reprocess_job(
        job_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        run, queued = workflows.enqueue_job_reprocess(job, db)
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return {
            "queued": queued,
            "reprocess": workflows.job_reprocess_run_to_api(run),
            "job": workflows.job_to_api(
                job,
                application,
                run,
                workflows.latest_page_capture_processing_for_job(db, job_id),
            ),
        }

    @app.post("/api/jobs/{job_id}/fit-analysis", status_code=202)
    def reprocess_job_fit_analysis(
        job_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        run, queued = workflows.enqueue_job_reprocess(
            job, db, run_type=workflows.JOB_REPROCESS_TYPE_FIT_ANALYSIS
        )
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        return {
            "queued": queued,
            "reprocess": workflows.job_reprocess_run_to_api(run),
            "job": workflows.job_to_api(
                job,
                application,
                run,
                workflows.latest_page_capture_processing_for_job(db, job_id),
            ),
        }

    @app.get("/api/jobs/{job_id}/source-archive/{artifact_id}/download")
    def download_job_source_archive(
        job_id: str,
        artifact_id: str,
        db: workflows.Session = Depends(get_db),
        storage: workflows.ArtifactStorageService = Depends(get_storage),
    ) -> Response:
        job = workflows.require_active_job(
            db, job_id, not_found_detail="Job source archive not found"
        )
        artifact = db.get(workflows.StoredArtifact, artifact_id)
        if (
            not job
            or not artifact
            or artifact.job_id != job_id
            or artifact.artifact_type != "job_source_archive"
            or artifact.deleted_at is not None
            or job.source.get("source_archive_artifact_id") != artifact_id
        ):
            raise workflows.HTTPException(status_code=404, detail="Job source archive not found")
        return Response(
            content=storage.read_bytes(artifact.storage_uri),
            media_type=artifact.content_type,
            headers=workflows.attachment_headers(artifact.original_file_name),
        )

    @app.get("/api/jobs/{job_id}/cover-letter-workspace")
    def get_cover_letter_workspace(
        job_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        return workflows.cover_letter_workspace_to_api(db, settings, job)

    @app.get("/api/jobs/{job_id}/cover-letter-generation")
    def get_cover_letter_generation(
        job_id: str,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        run = workflows.latest_cover_letter_generation_run(db, job.id)
        return {"run": run.to_api() if run else None}

    @app.get("/api/cover-letter-generation-runs/{run_id}/result")
    def get_cover_letter_result(run_id: str, view: str = "workspace", db=Depends(get_db)):
        run = db.get(workflows.CoverLetterGenerationRun, run_id)
        if run is None:
            raise workflows.HTTPException(status_code=404, detail="Generation request not found")
        workflows.require_active_job(db, run.job_id)
        if run.status in {"failed", "cancelled"}:
            code = (
                409
                if run.error_code
                in {
                    "posting_snapshot_changed",
                    "posting_changed_before_generation_commit",
                    "job_not_active",
                    "package_changed",
                }
                else 502
            )
            raise workflows.HTTPException(
                status_code=code, detail=run.error_message or "Generation failed"
            )
        result = workflows.completed_cover_letter_generation_draft_response(run, db, settings)
        if result is None:
            raise workflows.HTTPException(status_code=409, detail="Generation has not completed")
        if view in {"package", "document"}:
            return result[view]
        return result

    @app.post("/api/jobs/{job_id}/cover-letter-draft", status_code=202)
    def generate_job_cover_letter_draft(
        job_id: str,
        request: CoverLetterDraftRequest | None = None,
        db: workflows.Session = Depends(get_db),
    ) -> dict:
        from jobcockpit.api.cover_letter_generation_runs import queue_manual_cover_letter

        return queue_manual_cover_letter(db, job_id, request, candidate_id=workflows.CANDIDATE_ID)

    @app.post("/api/jobs/{job_id}/cover-letter-word-revision", status_code=201)
    async def create_job_cover_letter_word_revision(
        job_id: str,
        file: UploadFile = File(...),
        db: workflows.Session = Depends(get_db),
        storage: workflows.ArtifactStorageService = Depends(get_storage),
    ) -> dict:
        job = workflows.require_active_job(db, job_id)
        posting_snapshot_hash = job.posting_snapshot_hash
        automatic_run = workflows.active_cover_letter_generation_run(
            db,
            job.id,
            posting_snapshot_hash=posting_snapshot_hash,
        )
        automatic_run_id = automatic_run.generation_run_id if automatic_run else None
        profile = db.get(workflows.CandidateProfile, workflows.CANDIDATE_ID)
        if not profile:
            raise workflows.HTTPException(
                status_code=409,
                detail="Create a candidate profile before uploading a cover letter revision.",
            )
        if not str(file.filename or "").lower().endswith(".docx"):
            raise workflows.HTTPException(status_code=400, detail="Invalid Word cover letter")
        content = await file.read(workflows.MAX_COVER_LETTER_DOCX_UPLOAD_BYTES + 1)
        if len(content) > workflows.MAX_COVER_LETTER_DOCX_UPLOAD_BYTES:
            raise workflows.HTTPException(
                status_code=413, detail="Word cover letter exceeds the 5 MB limit"
            )
        try:
            prepared = workflows.prepare_cover_letter_word_revision(
                content,
                profile_data=profile.profile_data,
            )
        except (OSError, ValueError):
            raise workflows.HTTPException(
                status_code=400, detail="Invalid Word cover letter"
            ) from None

        workflows.begin_serialized_package_write(db)
        job = workflows.require_active_job(db, job_id)
        if job.posting_snapshot_hash != posting_snapshot_hash:
            raise workflows.HTTPException(
                status_code=409,
                detail="The posting changed while the cover letter revision was being prepared.",
            )
        if automatic_run_id:
            completed_run = db.get(workflows.CoverLetterGenerationRun, automatic_run_id)
            if completed_run is not None and completed_run.status == "succeeded":
                raise workflows.HTTPException(
                    status_code=409,
                    detail=(
                        "Automatic cover-letter generation completed while the Word revision "
                        "was being prepared. Reload the job before uploading a new revision."
                    ),
                )
        application = db.execute(
            workflows.select(workflows.Application).where(workflows.Application.job_id == job_id)
        ).scalar_one_or_none()
        if not application:
            application = workflows.Application(
                application_id=workflows.new_id("app"),
                candidate_id=workflows.CANDIDATE_ID,
                job_id=job_id,
                status="draft",
                decision="undecided",
            )
            db.add(application)
            db.add(
                workflows.AuditLogEntry.create(
                    "application.created",
                    "application",
                    application.application_id,
                )
            )
            db.flush()
        package = workflows.ApplicationPackage(
            application_package_id=workflows.new_id("pkg"),
            application_id=application.application_id,
            package_version=workflows.next_application_package_version(
                db, application.application_id
            ),
            status="draft",
            approved_for_extension=False,
        )
        db.add(package)
        db.flush()
        document_payload = workflows.create_cover_letter_package_document(
            db=db,
            storage=storage,
            settings=settings,
            package_id=package.application_package_id,
            prepared=prepared,
        )
        document = db.get(workflows.GeneratedDocument, document_payload["document_id"])
        package_document = db.get(
            workflows.ApplicationPackageDocument,
            document_payload["application_package_document_id"],
        )
        artifact = db.get(workflows.StoredArtifact, document_payload["artifact_id"])
        editable_artifact = (
            db.get(workflows.StoredArtifact, document.editable_artifact_id)
            if document and document.editable_artifact_id
            else None
        )
        if not document or not package_document or not artifact or not editable_artifact:
            raise workflows.HTTPException(
                status_code=500, detail="Cover letter revision could not be loaded"
            )
        db.add(
            workflows.AuditLogEntry.create(
                "document.word_revision_uploaded",
                "generated_document",
                document.document_id,
            )
        )
        workflows.satisfy_cover_letter_generation(
            job,
            db,
            result={
                "application_id": application.application_id,
                "application_package_id": package.application_package_id,
                "document_id": document.document_id,
                "artifact_id": artifact.artifact_id,
            },
            completion_source="manual_word_revision",
        )
        db.flush()
        return {
            "generator": workflows.cover_letter_generator_to_api(settings),
            "package": package.to_api(),
            "document": workflows.cover_letter_document_to_api(
                package_document,
                document,
                artifact,
                editable_artifact,
            ),
        }

    app.include_router(
        create_application_router(
            get_db=get_db,
            get_storage=get_storage,
            settings=settings,
        )
    )

    def capture_extension_page(
        request: workflows.ExtensionPageCaptureRequest,
        db: workflows.Session,
        storage: workflows.ArtifactStorageService,
    ) -> dict:
        run, queued = workflows.enqueue_page_capture(
            request,
            workflows.page_capture_raw_text(request),
            db,
            storage,
        )
        return {**run.to_api(), "queued": queued}

    app.include_router(
        create_extension_router(
            get_db=get_db,
            get_storage=get_storage,
            settings=settings,
            capture_page=capture_extension_page,
        )
    )
    app.include_router(
        create_job_alert_router(
            get_db=get_db,
            get_storage=get_storage,
            settings=settings,
            job_importer=workflows.persist_job_ingest_result,
        )
    )
    return app


runtime_settings = workflows.Settings()
app = create_app(runtime_settings, start_background_workers=True)
