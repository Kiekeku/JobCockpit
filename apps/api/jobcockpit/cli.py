from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Sequence

import httpx
from sqlalchemy import select

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard,
    bootstrap_background_worker,
)
from jobcockpit.api.cover_letter_generation_runs import (
    COVER_LETTER_BACKGROUND_WORKER_LEASE,
    bootstrap_cover_letter_generation_worker,
    recover_stale_cover_letter_generation_runs,
)
from jobcockpit.services.workflows import persist_job_ingest_result, process_next_background_job, process_next_cover_letter_generation
from jobcockpit.core.config import Settings
from jobcockpit.core.tls import install_windows_certifi_workaround
from jobcockpit.db.models import JobAlertLink, JobAlertSource
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.integrations.gmail_live import (
    GmailProviderConfigurationError,
    authorize_google_gmail,
)
from jobcockpit.integrations.gmail_processing import (
    GmailProcessingError,
    process_gmail_source,
)
from jobcockpit.integrations.gmail_runs import run_gmail_source
from jobcockpit.ingestion.application_document_backfill import (
    backfill_required_documents,
    create_required_documents_sqlite_backup,
)
from jobcockpit.ingestion.ats_defaults import ats_provider_defaults
from jobcockpit.ingestion.job_alert_link_retry import (
    record_unresolved_job_alert_link_retry_failure,
    retry_failure_to_api,
    retry_unresolved_job_alert_link,
)
from jobcockpit.ingestion.output import (
    normalize_ats_ingestion,
    normalize_failed_ingestion,
    normalize_gmail_ingestion,
)
from jobcockpit.ingestion.posting_links import is_successful_posting_link_import
from jobcockpit.ingestion.role_grammar_backfill import (
    DEFAULT_MAX_SNAPSHOT_BYTES,
    active_role_grammar_job_ids,
    backfill_role_grammar,
    create_role_grammar_sqlite_backup,
    merge_role_grammar_backfill_results,
)
from jobcockpit.ingestion.status_reconciliation import (
    StatusReconciliationRunFailed,
    StatusScanAlreadyRunningError,
    run_status_reconciliation,
    sanitize_status_reconciliation_error,
    status_report,
)
from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.utils.ids import new_id


logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_ATS_INGEST_TIMEOUT_SECONDS = 14400


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    install_windows_certifi_workaround()
    if args.command == "gmail" and args.gmail_command == "auth":
        return auth_gmail_command(args, settings or Settings())
    if args.command == "gmail" and args.gmail_command == "process":
        return process_gmail_command(args, settings or Settings())
    if args.command == "links" and args.links_command == "retry-unresolved":
        return retry_unresolved_links_command(args, settings or Settings())
    if args.command == "ats" and args.ats_command == "ingest-hanzilla":
        return ingest_ats_portal_command(args, provider="hanzilla", display_name="Hanzilla")
    if args.command == "ats" and args.ats_command == "ingest-eluta":
        return ingest_ats_portal_command(args, provider="eluta", display_name="Eluta")
    if args.command == "ats" and args.ats_command == "ingest-csjobs":
        return ingest_ats_portal_command(args, provider="csjobs", display_name="CS Jobs")
    if args.command == "ats" and args.ats_command == "ingest-rankalpha":
        return ingest_ats_portal_command(
            args,
            provider="rankalpha_jobs",
            display_name="RankAlpha Jobs",
        )
    if args.command == "ats" and args.ats_command == "ingest-canadian-tech":
        return ingest_ats_portal_command(
            args,
            provider="canadian_tech_internships",
            display_name="Canadian Tech Internships",
        )
    if args.command == "ats" and args.ats_command == "ingest-canadian-tech-2027":
        return ingest_ats_portal_command(
            args,
            provider="canadian_tech_internships_2027",
            display_name="Canadian Tech Internships 2027",
        )
    if args.command == "ats" and args.ats_command == "ingest-canada-tech-2027":
        return ingest_ats_portal_command(
            args,
            provider="canada_tech_internships_summer_2027",
            display_name="Canada Tech Internships Summer 2027",
        )
    if args.command == "jobs" and args.jobs_command == "reconcile-status":
        return reconcile_job_status_command(args, settings or Settings())
    if args.command == "jobs" and args.jobs_command == "status-report":
        return job_status_report_command(args, settings or Settings())
    if args.command == "jobs" and args.jobs_command == "backfill-required-documents":
        return required_documents_backfill_command(args, settings or Settings())
    if args.command == "jobs" and args.jobs_command == "backfill-role-grammar":
        return role_grammar_backfill_command(args, settings or Settings())
    if args.command == "worker" and args.worker_command == "reprocess":
        return reprocess_worker_command(args, settings or Settings())
    if args.command == "worker" and args.worker_command == "cover-letter":
        return cover_letter_worker_command(args, settings or Settings())
    if args.command == "worker" and args.worker_command == "source":
        return source_worker_command(args, settings or Settings())
    if args.command == "worker" and args.worker_command == "publication":
        return publication_worker_command(args, settings or Settings())
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobcockpit")
    subparsers = parser.add_subparsers(dest="command")
    gmail = subparsers.add_parser("gmail", help="Gmail job alert processing")
    gmail_subparsers = gmail.add_subparsers(dest="gmail_command")
    auth_parser = gmail_subparsers.add_parser(
        "auth",
        help="Authorize real Gmail read-only access and store a local token",
    )
    auth_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary.",
    )
    process_parser = gmail_subparsers.add_parser(
        "process",
        help="Automatically process enabled Gmail job alert sources",
    )
    process_parser.add_argument(
        "--provider",
        choices=["synthetic", "google"],
        help="Override JOBCOCKPIT_GMAIL_PROVIDER for this run.",
    )
    process_parser.add_argument(
        "--source-id",
        help="Process one job alert source. If omitted, all enabled Gmail sources are processed.",
    )
    process_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary for scheduled jobs.",
    )
    ats = subparsers.add_parser("ats", help="ATS portal ingestion")
    ats_subparsers = ats.add_subparsers(dest="ats_command")
    ingest_hanzilla = ats_subparsers.add_parser(
        "ingest-hanzilla",
        help="Import Hanzilla internship postings through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_hanzilla,
        provider="hanzilla",
        source_help="Hanzilla listing or detail URL to import.",
        limit_help="Maximum Hanzilla detail links to attempt.",
    )
    ingest_eluta = ats_subparsers.add_parser(
        "ingest-eluta",
        help="Import Eluta co-op postings through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_eluta,
        provider="eluta",
        source_help="Eluta search or detail URL to import.",
        limit_help="Maximum Eluta detail links to attempt.",
    )
    ingest_csjobs = ats_subparsers.add_parser(
        "ingest-csjobs",
        help="Import the public CS Jobs internship preview through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_csjobs,
        provider="csjobs",
        source_help="CS Jobs board URL to import (internships only).",
        limit_help="Maximum public CS Jobs internship links to scan.",
    )
    ingest_rankalpha = ats_subparsers.add_parser(
        "ingest-rankalpha",
        help="Import filtered jobs from the authenticated RankAlpha jobs API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_rankalpha,
        provider="rankalpha_jobs",
        source_help="RankAlpha /api/jobs URL, including any repeated filter parameters.",
        limit_help="Maximum non-duplicate RankAlpha jobs to import.",
    )
    ingest_canadian_tech = ats_subparsers.add_parser(
        "ingest-canadian-tech",
        help="Import Canadian Tech Internships GitHub README postings through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_canadian_tech,
        provider="canadian_tech_internships",
        source_help="Canadian Tech Internships GitHub README URL to import.",
        limit_help="Maximum matching Canadian Tech Internships apply links to scan.",
    )
    ingest_canadian_tech_2027 = ats_subparsers.add_parser(
        "ingest-canadian-tech-2027",
        help="Import Canadian Tech Internships 2027 GitHub README postings through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_canadian_tech_2027,
        provider="canadian_tech_internships_2027",
        source_help="Canadian Tech Internships 2027 GitHub README URL to import.",
        limit_help="Maximum matching Canadian Tech Internships 2027 apply links to scan.",
    )
    ingest_canada_tech_2027 = ats_subparsers.add_parser(
        "ingest-canada-tech-2027",
        help="Import Canada Tech Internships Summer 2027 GitHub README postings through the local API.",
    )
    add_ats_portal_ingest_arguments(
        ingest_canada_tech_2027,
        provider="canada_tech_internships_summer_2027",
        source_help="Canada Tech Internships Summer 2027 GitHub README URL to import.",
        limit_help="Maximum matching Canada Tech Internships Summer 2027 apply links to scan.",
    )
    links = subparsers.add_parser("links", help="Job alert link maintenance")
    links_subparsers = links.add_subparsers(dest="links_command")
    retry_unresolved_parser = links_subparsers.add_parser(
        "retry-unresolved",
        help="Retry unresolved job alert links.",
    )
    retry_unresolved_parser.add_argument(
        "--link-id",
        help="Retry one unresolved job alert link. If omitted, unresolved links are scanned oldest first.",
    )
    retry_unresolved_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of unresolved links to retry when --link-id is omitted.",
    )
    retry_unresolved_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary.",
    )
    worker = subparsers.add_parser("worker", help="Background worker processes")
    jobs = subparsers.add_parser("jobs", help="Job lifecycle maintenance")
    jobs_subparsers = jobs.add_subparsers(dest="jobs_command")
    reconcile_status = jobs_subparsers.add_parser(
        "reconcile-status", help="Check due active jobs for definitive closure evidence."
    )
    reconcile_status.add_argument("--limit", type=int, default=100)
    reconcile_status.add_argument("--rendered-limit", type=int, default=30)
    reconcile_status.add_argument("--max-runtime-seconds", type=int, default=5400)
    reconcile_status.add_argument("--job-id")
    reconcile_status.add_argument("--force", action="store_true")
    reconcile_status.add_argument("--dry-run", action="store_true")
    reconcile_status.add_argument("--json", action="store_true")
    status_report_parser = jobs_subparsers.add_parser(
        "status-report", help="Read durable job status scan history."
    )
    status_report_parser.add_argument("--job-id")
    status_report_parser.add_argument("--run-id")
    status_report_parser.add_argument("--limit", type=int, default=100)
    status_report_parser.add_argument("--json", action="store_true")
    required_documents_backfill = jobs_subparsers.add_parser(
        "backfill-required-documents",
        help="Recompute evidence-backed required documents from stored job snapshots.",
    )
    backfill_mode = required_documents_backfill.add_mutually_exclusive_group()
    backfill_mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the computed updates. The default is a read-only dry run.",
    )
    backfill_mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Explicitly select the default read-only mode.",
    )
    required_documents_backfill.set_defaults(apply=False)
    required_documents_backfill.add_argument("--json", action="store_true")
    role_grammar_backfill = jobs_subparsers.add_parser(
        "backfill-role-grammar",
        help="Recompute grammar-derived role semantics from active-job snapshots.",
    )
    role_grammar_mode = role_grammar_backfill.add_mutually_exclusive_group()
    role_grammar_mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the computed updates in batches after creating a SQLite backup.",
    )
    role_grammar_mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="apply",
        help="Explicitly select the default read-only mode.",
    )
    role_grammar_backfill.set_defaults(apply=False)
    role_grammar_backfill.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum active jobs committed in each apply transaction.",
    )
    role_grammar_backfill.add_argument(
        "--max-snapshot-bytes",
        type=int,
        default=DEFAULT_MAX_SNAPSHOT_BYTES,
        help="Skip and report snapshots larger than this parser safety bound.",
    )
    role_grammar_backfill.add_argument("--json", action="store_true")

    worker_subparsers = worker.add_subparsers(dest="worker_command")
    publication_worker = worker_subparsers.add_parser("publication", help="Process optional publications")
    publication_worker.add_argument("--once", action="store_true", help="Process at most one due publication")
    source_worker = worker_subparsers.add_parser("source", help="Process original-source discovery independently")
    source_worker.add_argument("--once", action="store_true", help="Process at most one due source job")
    reprocess_worker = worker_subparsers.add_parser(
        "reprocess",
        help="Drain queued job reprocess requests.",
    )
    reprocess_worker.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued reprocess request and exit.",
    )
    reprocess_worker.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Seconds to wait between empty queue polls in loop mode.",
    )
    reprocess_worker.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output. Loop mode emits one JSON object per processed run.",
    )
    cover_letter_worker = worker_subparsers.add_parser(
        "cover-letter",
        help="Drain queued cover-letter generation requests.",
    )
    cover_letter_worker.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued cover-letter request and exit.",
    )
    cover_letter_worker.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Seconds to wait between empty queue polls in loop mode.",
    )
    cover_letter_worker.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output. Loop mode emits one JSON object per processed run.",
    )
    return parser


def add_ats_portal_ingest_arguments(
    parser: argparse.ArgumentParser,
    *,
    provider: str,
    source_help: str,
    limit_help: str,
) -> None:
    defaults = ats_provider_defaults(provider)
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("JOBCOCKPIT_API_BASE_URL", DEFAULT_API_BASE_URL),
        help="JobCockpit API base URL. Defaults to JOBCOCKPIT_API_BASE_URL or http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--source-url",
        default=defaults.source_url,
        help=source_help,
    )
    parser.add_argument(
        "--search-text",
        default=defaults.search_text,
        help="Filter listing cards by text before importing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=defaults.limit,
        help=limit_help,
    )
    parser.add_argument(
        "--skip-interest-filter",
        action="store_true",
        default=defaults.skip_interest_filter,
        help="Skip the role-interest filter for this import.",
    )
    parser.add_argument(
        "--skip-language-filter",
        action="store_true",
        default=defaults.skip_language_filter,
        help="Skip the language filter for this import.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_default_ats_ingest_timeout_seconds(),
        help=(
            "Seconds to wait for the API ingestion response. Defaults to "
            "JOBCOCKPIT_ATS_INGEST_TIMEOUT_SECONDS or 14400."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the API response as JSON.",
    )


def _default_ats_ingest_timeout_seconds() -> int:
    raw_timeout = os.environ.get("JOBCOCKPIT_ATS_INGEST_TIMEOUT_SECONDS")
    if not raw_timeout:
        return DEFAULT_ATS_INGEST_TIMEOUT_SECONDS
    try:
        return int(raw_timeout)
    except ValueError:
        return DEFAULT_ATS_INGEST_TIMEOUT_SECONDS


def ingest_ats_portal_command(args: argparse.Namespace, *, provider: str, display_name: str) -> int:
    if args.limit < 1:
        print("--limit must be greater than zero", file=sys.stderr)
        return 2
    if args.timeout_seconds < 1:
        print("--timeout-seconds must be greater than zero", file=sys.stderr)
        return 2
    payload = {
        "provider": provider,
        "source_url": args.source_url,
        "search_text": args.search_text,
        "limit": args.limit,
        "skip_interest_filter": args.skip_interest_filter,
        "skip_language_filter": args.skip_language_filter,
    }
    api_base_url = args.api_base_url.rstrip("/")
    try:
        response = httpx.post(
            f"{api_base_url}/api/jobs/ingest/ats",
            json=payload,
            timeout=args.timeout_seconds,
        )
        response.raise_for_status()
        response_payload = response.json()
    except httpx.HTTPStatusError as error:
        detail = _http_error_detail(error.response)
        _emit_ats_ingest_failure(
            args,
            provider=provider,
            source_url=args.source_url,
            display_name=display_name,
            error_message=f"{error.response.status_code} {detail}",
        )
        return 1
    except httpx.HTTPError as error:
        _emit_ats_ingest_failure(
            args,
            provider=provider,
            source_url=args.source_url,
            display_name=display_name,
            error_message=str(error),
        )
        return 1
    except ValueError:
        _emit_ats_ingest_failure(
            args,
            provider=provider,
            source_url=args.source_url,
            display_name=display_name,
            error_message="ATS API returned invalid JSON",
        )
        return 1

    if not isinstance(response_payload, dict):
        _emit_ats_ingest_failure(
            args,
            provider=provider,
            source_url=args.source_url,
            display_name=display_name,
            error_message="ATS API returned a non-object JSON response",
        )
        return 1

    response_payload.setdefault("provider", provider)
    response_payload.setdefault("source_url", args.source_url)
    _emit_ats_ingest_result(args, response_payload, display_name=display_name)
    return 0


def process_gmail_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.provider:
        settings = settings.model_copy(update={"gmail_provider": args.provider})
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    try:
        with session_scope(settings) as db:
            sources = _load_sources(db, args.source_id)
            source_ids = [source.job_alert_source_id for source in sources]
    except ValueError as error:
        if args.json:
            output = normalize_failed_ingestion(
                provider="gmail",
                source_id=args.source_id,
                error=str(error),
            )
            print(json.dumps(output, sort_keys=True))
        else:
            print(str(error), file=sys.stderr)
        return 1

    if not source_ids:
        _emit(args, normalize_gmail_ingestion([], []))
        return 0

    source_results = []
    failed_sources = []
    for source_id in source_ids:
        run_id = new_id("emailrun")
        try:
            with session_scope(settings) as db:
                result = run_gmail_source(
                    db=db, storage=storage, settings=settings, source_id=source_id,
                    job_importer=persist_job_ingest_result, run_id=run_id,
                    processor=process_gmail_source,
                )
                source = db.get(JobAlertSource, source_id)
                source_result = {
                    "source_id": source_id,
                    "display_name": source.display_name,
                    **result,
                }
            source_results.append(source_result)
        except GmailProcessingError as error:
            diagnostic_suffix = (
                f" diagnostics={json.dumps(error.provider_diagnostics, sort_keys=True)}"
                if error.provider_diagnostics
                else ""
            )
            print(f"{source_id}: {error.detail}{diagnostic_suffix}", file=sys.stderr)
            failed_sources.append(
                {
                    "source_id": source_id,
                    "run_id": run_id,
                    "status": "failed",
                    "error": error.detail,
                    "provider_diagnostics": error.provider_diagnostics,
                }
            )
        except ValueError as error:
            print(f"{source_id}: {error}", file=sys.stderr)
            failed_sources.append(
                {
                    "source_id": source_id,
                    "run_id": run_id,
                    "status": "failed",
                    "error": str(error),
                }
            )
        except Exception:
            error_message = "Gmail ingestion failed unexpectedly"
            print(f"{source_id}: {error_message}", file=sys.stderr)
            failed_sources.append(
                {
                    "source_id": source_id,
                    "run_id": run_id,
                    "status": "failed",
                    "error": error_message,
                }
            )

    output = normalize_gmail_ingestion(source_results, failed_sources)
    _emit(args, output)
    return 1 if failed_sources else 0


def auth_gmail_command(args: argparse.Namespace, settings: Settings) -> int:
    try:
        token_path = authorize_google_gmail(settings)
    except GmailProviderConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1
    payload = {
        "status": "authorized",
        "token_path": str(token_path),
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "mailbox_mutations": [],
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Gmail read-only token saved to {token_path}")
    return 0


def _run_background_worker(worker, label: str) -> int:
    worker.start()
    try:
        while worker.thread.is_alive():
            worker.thread.join(1)
        if not worker.stop_event.is_set():
            logger.error("%s worker thread stopped unexpectedly", label)
            return 1
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        worker.stop()


def publication_worker_command(args: argparse.Namespace, settings: Settings) -> int:
    from jobcockpit.services.publication import PublicationWorker, process_next_publication

    if not settings.publication_enabled:
        print(json.dumps({"processed_count": 0, "disabled": True}))
        return 0
    init_database(settings)
    if args.once:
        result = process_next_publication(settings)
        print(json.dumps({"processed_count": int(result is not None), "work": result}))
        return 0
    worker = PublicationWorker(settings)
    return _run_background_worker(worker, "Publication")


def source_worker_command(args: argparse.Namespace, settings: Settings) -> int:
    from jobcockpit.ingestion.source_reconciliation import (
        SOURCE_WORKER_LEASE, SourceBackgroundWorker, process_next_source_reconciliation,
    )
    init_database(settings)
    if args.once:
        worker_id = new_id("source_worker")
        with BackgroundWorkerLeaseGuard(settings, worker_id, lease_name=SOURCE_WORKER_LEASE) as lease:
            result = None
            if lease.acquired and lease.lease_token is not None:
                result = process_next_source_reconciliation(
                    settings, worker_id=worker_id, supervisor_lease_token=lease.lease_token,
                )
            print(json.dumps({"processed_count": int(result is not None), "work": result,
                              "worker_busy": not lease.acquired}))
        return 0
    worker = SourceBackgroundWorker(settings)
    return _run_background_worker(worker, "Source reconciliation")


def reprocess_worker_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.poll_seconds <= 0:
        print("--poll-seconds must be greater than zero", file=sys.stderr)
        return 2
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    worker_id = new_id("worker")
    if args.once:
        with BackgroundWorkerLeaseGuard(settings, worker_id) as lease:
            if not lease.acquired:
                payload = {
                    "processed_count": 0,
                    "work": None,
                    "worker_busy": True,
                }
            else:
                bootstrap_background_worker(
                    settings,
                    worker_id=worker_id,
                    lease_token=lease.lease_token,
                )
                result = process_next_background_job(
                    settings,
                    storage,
                    worker_id=worker_id,
                    supervisor_lease_token=lease.lease_token,
                )
                payload = {
                    "processed_count": 1 if result else 0,
                    "work": result,
                }
        _emit_reprocess_worker_result(args, payload)
        return 0

    try:
        while True:
            with BackgroundWorkerLeaseGuard(settings, worker_id) as lease:
                if not lease.acquired:
                    time.sleep(args.poll_seconds)
                    continue
                bootstrap_background_worker(
                    settings,
                    worker_id=worker_id,
                    lease_token=lease.lease_token,
                )
                while not lease.lost:
                    result = process_next_background_job(
                        settings,
                        storage,
                        worker_id=worker_id,
                        supervisor_lease_token=lease.lease_token,
                    )
                    if result:
                        _emit_reprocess_worker_result(
                            args,
                            {"processed_count": 1, "work": result},
                        )
                        continue
                    time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


def cover_letter_worker_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.poll_seconds <= 0:
        print("--poll-seconds must be greater than zero", file=sys.stderr)
        return 2
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    worker_id = new_id("cover_worker")
    if args.once:
        with BackgroundWorkerLeaseGuard(
            settings,
            worker_id,
            lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
        ) as lease:
            if not lease.acquired or lease.lease_token is None:
                payload = {"processed_count": 0, "work": None, "worker_busy": True}
            else:
                bootstrap_cover_letter_generation_worker(
                    settings,
                    worker_id=worker_id,
                    lease_token=lease.lease_token,
                )
                result = process_next_cover_letter_generation(
                    settings,
                    storage,
                    worker_id=worker_id,
                    supervisor_lease_token=lease.lease_token,
                )
                payload = {"processed_count": 1 if result else 0, "work": result}
        _emit_cover_letter_worker_result(args, payload)
        return 0

    try:
        while True:
            with BackgroundWorkerLeaseGuard(
                settings,
                worker_id,
                lease_name=COVER_LETTER_BACKGROUND_WORKER_LEASE,
            ) as lease:
                if not lease.acquired or lease.lease_token is None:
                    time.sleep(args.poll_seconds)
                    continue
                try:
                    bootstrap_cover_letter_generation_worker(
                        settings,
                        worker_id=worker_id,
                        lease_token=lease.lease_token,
                    )
                except Exception as error:
                    logger.error(
                        "Cover-letter worker bootstrap failed; error_type=%s",
                        error.__class__.__name__,
                    )
                    time.sleep(args.poll_seconds)
                    continue
                while not lease.lost:
                    try:
                        result = process_next_cover_letter_generation(
                            settings,
                            storage,
                            worker_id=worker_id,
                            supervisor_lease_token=lease.lease_token,
                        )
                    except Exception as error:
                        logger.error(
                            "Cover-letter worker iteration failed; error_type=%s",
                            error.__class__.__name__,
                        )
                        try:
                            recover_stale_cover_letter_generation_runs(
                                settings,
                                recover_owned_by=worker_id,
                                required_worker_id=worker_id,
                                required_lease_token=lease.lease_token,
                            )
                        except Exception as recovery_error:
                            logger.error(
                                "Cover-letter worker recovery failed; error_type=%s",
                                recovery_error.__class__.__name__,
                            )
                        time.sleep(args.poll_seconds)
                        continue
                    if result:
                        _emit_cover_letter_worker_result(
                            args,
                            {"processed_count": 1, "work": result},
                        )
                        continue
                    time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


def required_documents_backfill_command(args: argparse.Namespace, settings: Settings) -> int:
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        result = backfill_required_documents(db, storage)

    if args.apply:
        result["mode"] = "apply"
        if not result["error_count"] and not result["changed_count"]:
            result["applied"] = True
        elif not result["error_count"]:
            backup_path = create_required_documents_sqlite_backup(settings.database_url)
            with session_scope(settings) as db:
                result = backfill_required_documents(db, storage, apply=True)
            result["backup_path"] = str(backup_path)

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        action = "Applied" if result["applied"] else "Dry run found"
        print(
            f"{action} {result['changed_count']} required-document change(s) "
            f"across {result['scanned_count']} job(s); {result['error_count']} error(s)."
        )
        for documents, count in result["distribution"].items():
            print(f"- {documents}: {count}")
    return 1 if result["error_count"] else 0


def role_grammar_backfill_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.batch_size < 1:
        print("--batch-size must be greater than zero", file=sys.stderr)
        return 2
    if args.max_snapshot_bytes < 1:
        print("--max-snapshot-bytes must be greater than zero", file=sys.stderr)
        return 2
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)

    with session_scope(settings) as db:
        job_ids = active_role_grammar_job_ids(db)
    preflight_results = []
    for start in range(0, len(job_ids), args.batch_size):
        batch_ids = job_ids[start : start + args.batch_size]
        with session_scope(settings) as db:
            preflight_results.append(
                backfill_role_grammar(
                    db,
                    storage,
                    job_ids=batch_ids,
                    max_snapshot_bytes=args.max_snapshot_bytes,
                )
            )
        print(
            f"Role-grammar dry run: {min(start + len(batch_ids), len(job_ids))}/"
            f"{len(job_ids)} active jobs scanned",
            file=sys.stderr,
        )
    preflight = merge_role_grammar_backfill_results(preflight_results, apply=False)
    preflight["batch_count"] = len(preflight_results)

    if not args.apply:
        result = preflight
    elif preflight["error_count"]:
        result = {**preflight, "mode": "apply", "applied": False, "batch_count": 0}
    elif not preflight["changed_count"]:
        result = {**preflight, "mode": "apply", "applied": True, "batch_count": 0}
    else:
        backup_path = create_role_grammar_sqlite_backup(settings.database_url)
        batch_results = []
        for start in range(0, len(job_ids), args.batch_size):
            batch_ids = job_ids[start : start + args.batch_size]
            with session_scope(settings) as db:
                batch_results.append(
                    backfill_role_grammar(
                        db,
                        storage,
                        apply=True,
                        job_ids=batch_ids,
                        max_snapshot_bytes=args.max_snapshot_bytes,
                    )
                )
            print(
                f"Role-grammar apply: {min(start + len(batch_ids), len(job_ids))}/"
                f"{len(job_ids)} active jobs processed",
                file=sys.stderr,
            )
            if batch_results[-1]["error_count"]:
                break
        result = merge_role_grammar_backfill_results(batch_results, apply=True)
        result["backup_path"] = str(backup_path)
        result["batch_count"] = len(batch_results)

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        action = "Applied" if result["applied"] else "Dry run found"
        print(
            f"{action} {result['changed_count']} role-grammar change(s) "
            f"across {result['scanned_count']} active job(s); "
            f"{result['error_count']} error(s), {result['skipped_count']} skipped."
        )
        for field, count in result["changed_fields"].items():
            print(f"- {field}: {count}")
        if result.get("backup_path"):
            print(f"Backup: {result['backup_path']}")
    return 1 if result["error_count"] else 0


def retry_unresolved_links_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.limit is not None and args.limit < 1:
        print("--limit must be greater than zero", file=sys.stderr)
        return 2
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    link_ids = [args.link_id] if args.link_id else _unresolved_link_ids(settings, args.limit)
    results = []
    for link_id in link_ids:
        try:
            with session_scope(settings) as db:
                results.append(
                    retry_unresolved_job_alert_link(
                        db,
                        link_id=link_id,
                        storage=storage,
                        settings=settings,
                    )
                )
        except Exception as error:
            print(f"{link_id}: {error}", file=sys.stderr)
            try:
                with session_scope(settings) as db:
                    results.append(
                        record_unresolved_job_alert_link_retry_failure(
                            db,
                            link_id=link_id,
                            error=error,
                        )
                    )
            except Exception as record_error:
                results.append(
                    retry_failure_to_api(
                        link_id,
                        RuntimeError(f"{error}; failed_to_record_retry_error: {record_error}"),
                    )
                )

    payload = _summarize_retry_results(results)
    _emit_retry_results(args, payload)
    return 1 if payload["links_failed_count"] else 0


def _unresolved_link_ids(settings: Settings, limit: int | None) -> list[str]:
    with session_scope(settings) as db:
        statement = (
            select(JobAlertLink.job_alert_link_id)
            .where(JobAlertLink.ingestion_status == "needs_external_resolution")
            .order_by(JobAlertLink.updated_at.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(db.execute(statement).scalars())


def _load_sources(db, source_id: str | None) -> list[JobAlertSource]:
    if source_id:
        source = db.get(JobAlertSource, source_id)
        if not source:
            raise ValueError(f"Job alert source not found: {source_id}")
        if source.provider != "gmail":
            raise ValueError(f"Job alert source is not a Gmail source: {source_id}")
        return [source]
    return list(
        db.execute(
            select(JobAlertSource)
            .where(
                JobAlertSource.provider == "gmail",
                JobAlertSource.enabled.is_(True),
            )
            .order_by(JobAlertSource.job_alert_source_id)
        ).scalars()
    )


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail or payload)


def _emit_ats_ingest_failure(
    args: argparse.Namespace,
    *,
    provider: str,
    source_url: str,
    display_name: str,
    error_message: str,
) -> None:
    output = normalize_failed_ingestion(
        provider=provider,
        source_url=source_url,
        error=error_message,
    )
    if args.json:
        print(json.dumps(output, sort_keys=True))
        return
    print(f"{display_name} ingestion failed: {error_message}", file=sys.stderr)


def _emit_ats_ingest_result(args: argparse.Namespace, payload: dict, *, display_name: str) -> None:
    output = normalize_ats_ingestion(payload)
    if args.json:
        print(json.dumps(output, sort_keys=True))
        return
    counts = output["totals"]
    print(
        f"{display_name} import complete: "
        f"{counts['items_seen_count']} item(s) seen, "
        f"{counts['jobs_created_count']} job(s) created, "
        f"{counts['jobs_reused_count']} job(s) reused, "
        f"{counts['items_skipped_count']} item(s) skipped, "
        f"{counts['items_needing_review_count']} item(s) needing review, "
        f"{counts['items_failed_count']} item(s) failed."
    )


def _emit(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return
    counts = payload["totals"]
    print(
        "Gmail import complete: "
        f"{counts['items_seen_count']} item(s) seen, "
        f"{counts['jobs_created_count']} job(s) created, "
        f"{counts['jobs_reused_count']} job(s) reused, "
        f"{counts['items_skipped_count']} item(s) skipped, "
        f"{counts['items_needing_review_count']} item(s) needing review, "
        f"{counts['items_failed_count']} item(s) failed."
    )


def _emit_reprocess_worker_result(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return
    work = payload["work"]
    if not work:
        print("No queued background work.")
        return
    result = work["run"] or {}
    if work["kind"] == "page_capture":
        print(
            "Processed page capture "
            f"{result.get('capture_id', 'unknown')}: {result.get('status', 'unknown')}."
        )
        return
    print(
        "Processed job reprocess "
        f"{result.get('reprocess_run_id', 'unknown')} for "
        f"{result.get('job_id', 'unknown')}: {result.get('status', 'unknown')}."
    )


def _emit_cover_letter_worker_result(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return
    work = payload["work"]
    if not work:
        print("No queued cover-letter work.")
        return
    print(
        "Processed cover-letter request "
        f"{work.get('generation_run_id', 'unknown')} for "
        f"{work.get('job_id', 'unknown')}: {work.get('status', 'unknown')}."
    )


def _summarize_retry_results(results: list[dict]) -> dict:
    return {
        "links_retried_count": len(results),
        "links_imported_count": sum(
            1 for result in results if is_successful_posting_link_import(result["ingestion_status"])
        ),
        "links_failed_count": sum(
            1 for result in results if result["error_message"] == "retry_failed"
        ),
        "jobs_created_count": sum(1 for result in results if result["created"]),
        "links": results,
    }


def _emit_retry_results(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(
        "Retried "
        f"{payload['links_retried_count']} link(s), "
        f"{payload['links_imported_count']} imported, "
        f"{payload['links_failed_count']} failed, "
        f"{payload['jobs_created_count']} new job(s)."
    )


def reconcile_job_status_command(args: argparse.Namespace, settings: Settings) -> int:
    try:
        summary = run_status_reconciliation(
            settings,
            limit=args.limit,
            rendered_limit=args.rendered_limit,
            max_runtime_seconds=args.max_runtime_seconds,
            job_id=args.job_id,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ValueError, StatusScanAlreadyRunningError) as error:
        payload = _failed_status_reconciliation_summary(error)
        print(
            json.dumps(payload, sort_keys=True) if args.json else payload["error"],
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        payload = _failed_status_reconciliation_summary(error)
        print(
            json.dumps(payload, sort_keys=True) if args.json else payload["error"],
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, sort_keys=True) if args.json else _status_summary_text(summary))
    return 0


def _failed_status_reconciliation_summary(error: Exception) -> dict:
    if isinstance(error, StatusReconciliationRunFailed):
        return dict(error.summary)
    return {
        "run_id": None,
        "status": "failed",
        "selected": 0,
        "checked": 0,
        "open": 0,
        "closed": 0,
        "expired": 0,
        "inconclusive": 0,
        "failed": 0,
        "deferred": 0,
        "skipped": 0,
        "error": sanitize_status_reconciliation_error(error),
    }


def job_status_report_command(args: argparse.Namespace, settings: Settings) -> int:
    try:
        if args.limit < 1:
            raise ValueError("--limit must be greater than zero")
        payload = status_report(
            settings,
            job_id=args.job_id,
            run_id=args.run_id,
            limit=args.limit,
        )
    except ValueError as error:
        clean_error = sanitize_status_reconciliation_error(error)
        payload = {"status": "failed", "error": clean_error}
        print(
            json.dumps(payload, sort_keys=True) if args.json else clean_error,
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        clean_error = sanitize_status_reconciliation_error(error)
        payload = {"status": "failed", "error": clean_error}
        print(
            json.dumps(payload, sort_keys=True) if args.json else clean_error,
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, sort_keys=True) if args.json else json.dumps(payload, indent=2))
    return 0


def _status_summary_text(summary: dict) -> str:
    return (
        f"Status reconciliation {summary['status']}: "
        f"selected={summary['selected']} checked={summary['checked']} "
        f"open={summary['open']} closed={summary['closed']} expired={summary['expired']} "
        f"inconclusive={summary['inconclusive']} failed={summary['failed']} "
        f"deferred={summary['deferred']} skipped={summary['skipped']}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
