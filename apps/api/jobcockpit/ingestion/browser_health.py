"""Private, cross-process browser telemetry independent of ingestion transactions."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import BinaryIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from jobcockpit.storage.artifacts import ArtifactStorageService
from jobcockpit.ingestion.status_scan_lock import _acquire_os_file_lock

logger = logging.getLogger(__name__)
_QUERY_KEYS = {"jk", "vjk", "job_id", "gh_jid", "requisitionId", "reqId"}
_PROVIDER_QUERY_KEYS = (
    ("ibm.com", {"jobId"}),
    ("ultipro.ca", {"jobId", "jobid", "opportunityId", "opportunityid", "postingId", "job"}),
    ("ultipro.com", {"jobId", "jobid", "opportunityId", "opportunityid", "postingId", "job"}),
    ("workforcenow.adp.com", {"jobId", "jobid", "cid", "ccId", "lang"}),
    ("njoyn.com", {"Jobid", "BRID", "Page", "clid", "lang", "SBDID"}),
    ("talent.com", {"id"}),
    ("eightfold.ai", {"pid"}),
)
_signals: ContextVar[list[str] | None] = ContextVar("browser_health_signals", default=None)
_session_recovery: ContextVar[list[str] | None] = ContextVar("browser_health_session_recovery", default=None)
_RECOVERY_EVENTS = {
    "preparation_granted", "preparation_denied_site", "preparation_denied_deadline",
    "preparation_denied_active", "preparation_denied_limit", "preparation_denied_cooldown",
    "preparation_denied_cancelled", "recheck_resolved", "recheck_inconclusive", "recovery_interrupted",
}
_ERROR_CODES = (
    "invisible_session_sign_in_required", "invisible_session_preparation_needs_review",
    "invisible_session_preparation_cooldown", "invisible_session_preparation_failed",
    "rendered_http_error", "empty_rendered_page", "auth_wall_unresolved",
    "blocked_rendered_page", "owned_context_cleanup_failed", "browser_render_cancelled",
    "browser_proxy_capacity_exhausted",
    "rendered_url_policy_blocked", "rendered_url_policy_guard_unavailable",
    "rendered_url_policy_guard_install_failed", "rendered_context_hardening_unavailable",
    "rendered_context_hardening_install_failed", "rendered_context_hardening_verification_unavailable",
    "rendered_context_hardening_verification_failed", "rendered_title_snapshot_failed",
    "rendered_title_snapshot_invalid", "rendered_body_snapshot_failed",
    "rendered_body_snapshot_invalid", "rendered_posting_unavailable",
    "browser_target_registration_unavailable", "browser_target_registration_failed",
    "browser_target_cleanup_unconfirmed", "browser_context_page_tracking_unavailable",
    "persistent_source_session_url_invalid", "persistent_storage_state_lock_failed",
    "persistent_storage_state_write_failed", "source_storage_state_read_failed",
    "source_cookie_read_unavailable", "source_cookie_read_failed", "source_cookie_read_invalid",
    "playwright_url_policy_blocked", "playwright_url_mismatch",
    "playwright_content_truncated", "playwright_low_confidence", "playwright_timeout",
    "invisible_playwright_worker_failed", "playwright_failed",
)


@contextmanager
def collect_signals():
    signals: list[str] = []
    token = _signals.set(signals)
    try:
        yield signals
    finally:
        _signals.reset(token)


def record_signal(reason: str) -> None:
    signals = _signals.get()
    if signals is not None and reason not in signals and len(signals) < 5:
        signals.append(reason)


def record_session_recovery(outcome: str) -> None:
    """Record fixed recovery decisions without changing an attempt's outcome."""
    events = _session_recovery.get()
    if events is not None and outcome in _RECOVERY_EVENTS and outcome not in events and len(events) < 4:
        events.append(outcome)


def record_page_failure(url: str, title: str, error: str) -> None:
    """Record only fixed page diagnostics, never a raw URL or browser title."""
    if error not in {"rendered_http_error", "invisible_session_sign_in_required"}:
        return
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
            return
    except ValueError:
        return
    location = host
    if host == "indeed.com" or host.endswith(".indeed.com"):
        path = parsed.path.split(";", 1)[0].rstrip("/")
        if path in {"/auth", "/account/login"}:
            location += path
    if error == "invisible_session_sign_in_required":
        record_signal(f"Sign-in page at {location}")
        return
    match = re.match(r"^(?:http\s+)?(?:error\s+)?([45]\d\d)\b", title.casefold().strip())
    record_signal(f"HTTP {match.group(1)} at {location}" if match else f"HTTP error page at {location}")


def safe_error_code(error: str) -> str:
    for code in _ERROR_CODES:
        if re.search(rf"(?<![a-z0-9_]){re.escape(code)}(?![a-z0-9_])", error.casefold()):
            return code
    return ""


def inspect_embedded_job_content(page) -> None:
    """Report ATS frames omitted by the current top-document text capture."""
    if _signals.get() is None:
        return
    hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com", "smartrecruiters.com")
    for frame in getattr(page, "frames", ()):
        if frame is getattr(page, "main_frame", None):
            continue
        try:
            host = (urlsplit(frame.url).hostname or "").lower()
            if not any(host == domain or host.endswith("." + domain) for domain in hosts):
                continue
            # Read only the already-loaded document; no extra navigation or fetch.
            has_text = frame.evaluate("Boolean(document.body && document.body.innerText.trim())")
            record_signal(
                "Job content is embedded in a frame and was not captured"
                if has_text else "Embedded job frame did not load readable content"
            )
        except Exception:
            record_signal("Embedded job frame could not be inspected")


def safe_posting_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            return ""
        # Exclude credentials, tracking parameters, and fragments from diagnostics.
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host += f":{parts.port}"
        query_keys = set(_QUERY_KEYS)
        for domain, keys in _PROVIDER_QUERY_KEYS:
            if parts.hostname == domain or parts.hostname.endswith("." + domain):
                query_keys.update(keys)
        # Here 'token' identifies the job, not a session credential. Keep this
        # exception limited to the known public Greenhouse embed endpoint.
        if parts.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts.path.rstrip("/") == "/embed/job_app":
            query_keys.update({"for", "token"})
        return urlunsplit((parts.scheme, host, parts.path, urlencode([
            (key, val) for key, val in parse_qsl(parts.query) if key in query_keys
        ]), ""))[:2048]
    except ValueError:
        return ""


def url_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def classify_result(result=None, *, exception: bool = False) -> tuple[str, str]:
    if exception:
        return "failed", "Browser worker error"
    if result.status == "resolved":
        return "succeeded", "Page resolved"
    error = result.error.casefold()
    for markers, label in (
        (("invisible_session_sign_in_required",), "Site requires sign-in"),
        (("rendered_http_error",), "Site returned an HTTP error page"),
        (("empty_rendered_page", "empty_body"), "Page body was empty"),
        (("not installed", "executable doesn't exist", "browser was not found"), "Browser dependency unavailable"),
        (("timeout", "timed out"), "Timed out"),
        (("cancelled",), "Cancelled"),
        (("url_policy", "private_network", "blocked_host"), "URL blocked by safety policy"),
        (("auth_wall", "blocked_rendered_page", "preparation", "challenge"), "Site blocked access or needs verification"),
        (("cleanup_failed", "cleanup failed"), "Browser cleanup failed"),
        (("browser_proxy_capacity_exhausted",), "Browser proxy connection capacity exhausted"),
        (("worker_failed", "playwright_failed"), "Browser worker error"),
    ):
        if any(marker in error for marker in markers):
            return "failed", label
    return "needs_review", "Page could not be confidently matched"


def _write(root: Path, event: dict) -> None:
    day = event["started_at"][:10]
    storage = ArtifactStorageService(root / "browser-health" / day)
    storage.write_bytes(
        artifact_type="browser_health", content=json.dumps(event).encode(),
        original_file_name=f'{event["attempt_id"]}-{event["status"]}.json',
        content_type="application/json", privacy_class="private", source_kind="browser_diagnostics",
    )


def _attempt_lease(root: Path, event: dict) -> BinaryIO:
    """Keep a per-attempt OS lock until completion, including across processes.

    A killed worker releases the lock automatically. PID reuse and unrelated API
    or scheduled-worker restarts cannot make an active attempt look abandoned.
    """
    storage = ArtifactStorageService(root / "browser-health" / event["started_at"][:10])
    lease = storage.write_bytes(
        artifact_type="browser_health_owner", content=b"0",
        original_file_name=f'{event["attempt_id"]}-owner.lock',
        content_type="application/octet-stream", privacy_class="private",
        source_kind="browser_diagnostics",
    )
    handle = storage._path_for_uri(lease.storage_uri).open("r+b")
    try:
        _acquire_os_file_lock(handle)
    except BaseException:
        handle.close()
        raise
    event["owner_lease_uri"] = lease.storage_uri
    return handle


def _owner_has_exited(root: Path, event: dict) -> bool:
    storage = ArtifactStorageService(root / "browser-health" / event["started_at"][:10])
    path = storage._path_for_uri(event["owner_lease_uri"])
    if not path.name.endswith(f'-{event["attempt_id"]}-owner.lock'):
        raise ValueError("invalid browser ownership artifact")
    with path.open("r+b") as handle:
        try:
            _acquire_os_file_lock(handle)
        except (BlockingIOError, PermissionError):
            return False
        return True  # Closing this handle releases the probe lock.


def _reconcile_attempt(root: Path, event: dict, now: datetime) -> dict:
    if event["status"] != "running":
        return event
    deadline = datetime.fromisoformat(event["expected_by"])
    if event.get("owner_lease_uri"):
        if not _owner_has_exited(root, event):
            return event
        reason = "Browser worker stopped before reporting completion; retry may be needed"
        code = "browser_attempt_interrupted"
    elif event.get("ownership_version") or deadline >= now:
        # New attempts whose ownership recording failed remain unconfirmed.
        return event
    else:
        reason = "Completion missing for legacy attempt (worker ownership unknown); review retry outcome"
        code = "browser_completion_missing"
    reconciled = dict(
        event, status="failed", reason=reason, error_code=code,
        # Preserve the activity window; reconciliation is not a new attempt.
        finished_at=min(deadline, now).isoformat(), reconciled_at=now.isoformat(),
    )
    _write(root, reconciled)
    return reconciled


def observe(root: Path | None, hint, workflow: str, budget_seconds: float, operation):
    if root is None:
        return operation()
    now = datetime.now(UTC)
    event = {
        "attempt_id": uuid4().hex, "provider": "invisible_playwright", "workflow": workflow,
        "source_url": safe_posting_url(hint.source_url), "url_key": url_key(hint.source_url),
        "title": str(hint.title or "")[:160], "status": "running", "reason": "",
        "started_at": now.isoformat(), "finished_at": None,
        "expected_by": (now + timedelta(seconds=max(1, budget_seconds) + 10)).isoformat(),
        "duration_seconds": None, "ownership_version": 1,
    }
    started = time.monotonic()
    signals: list[str] = []
    recovery_events: list[str] = []
    lease = None
    try:
        lease = _attempt_lease(root, event)
    except OSError:
        logger.exception("Could not record browser attempt ownership")
    try:
        _write(root, event)
    except OSError:
        logger.exception("Could not record browser attempt start")
    recovery_token = _session_recovery.set(recovery_events)
    try:
        with collect_signals() as signals:
            result = operation()
    except BaseException:
        event["status"], event["reason"] = classify_result(exception=True)
        raise
    else:
        event["status"], event["reason"] = classify_result(result)
        if code := safe_error_code(result.error):
            event["error_code"] = code
        if event["status"] == "succeeded":
            # An auth/HTTP page seen during a recovered retry is not a final failure.
            signals[:] = [signal for signal in signals if not signal.startswith(("HTTP ", "Sign-in page at "))]
        if signals and event["status"] != "failed":
            event["status"], event["reason"] = "needs_review", signals[0]
        return result
    finally:
        _session_recovery.reset(recovery_token)
        event["finished_at"] = datetime.now(UTC).isoformat()
        event["duration_seconds"] = round(time.monotonic() - started, 2)
        event["signals"] = signals
        if recovery_events:
            event["session_recovery"] = recovery_events
        try:
            _write(root, event)
        except OSError:
            logger.exception("Could not record browser attempt completion")
        finally:
            if lease is not None:
                lease.close()


def _recent_attempts(root: Path, now: datetime) -> tuple[list[dict], list[dict], bool]:
    attempts: dict[str, dict] = {}
    incomplete = False
    read_count = 0
    for offset in range(8):
        directory = root / "browser-health" / (now - timedelta(days=offset)).date().isoformat() / "misc"
        try:
            files = directory.glob("*.json")
            for path in files:
                read_count += 1
                if read_count > 20000:
                    incomplete = True
                    break
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(event, dict) or event.get("status") not in {
                        "running", "succeeded", "failed", "needs_review",
                    }:
                        raise ValueError("invalid monitoring event")
                    for field in ("attempt_id", "source_url", "title", "reason", "workflow"):
                        if not isinstance(event[field], str):
                            raise ValueError("invalid monitoring field")
                    for field in ("started_at", "expected_by"):
                        if datetime.fromisoformat(event[field]).tzinfo is None:
                            raise ValueError("monitoring timestamps must include a timezone")
                    if event["finished_at"]:
                        if datetime.fromisoformat(event["finished_at"]).tzinfo is None:
                            raise ValueError("monitoring timestamps must include a timezone")
                    event["source_url"] = safe_posting_url(event["source_url"])
                    previous = attempts.get(event["attempt_id"])
                    # A genuine completion always wins over an inferred one,
                    # independent of directory enumeration or concurrent readers.
                    def rank(item):
                        return (bool(item["finished_at"]) + bool(
                            item["finished_at"] and not item.get("reconciled_at")
                        ), item["finished_at"] or "")
                    if previous is None or rank(event) > rank(previous):
                        attempts[event["attempt_id"]] = event
                except (OSError, ValueError, KeyError, TypeError):
                    incomplete = True
        except OSError:
            incomplete = True
        if read_count > 20000:
            break
    rows = [row for row in attempts.values() if datetime.fromisoformat(
        row["finished_at"] or row["started_at"]
    ) >= now - timedelta(days=7)]
    for index, row in enumerate(rows):
        try:
            rows[index] = _reconcile_attempt(root, row, now)
        except (OSError, ValueError, KeyError, TypeError):
            incomplete = True
    recent = [row for row in rows if datetime.fromisoformat(row["finished_at"] or row["started_at"]) >= now - timedelta(hours=24)]
    for row in recent:
        if row["status"] == "running" and datetime.fromisoformat(row["expected_by"]) < now:
            row["status"] = "overdue"
            row["reason"] = "No completion reported; the worker may have stopped"
    return rows, recent, incomplete


def _issue_fingerprint(row: dict) -> str:
    return f'{row["attempt_id"]}:{row["status"]}:{row["finished_at"] or ""}'


def _recovered_failure_ids(rows: list[dict], issues: list[dict]) -> set[str]:
    latest_success_start: dict[str, datetime] = {}
    for row in rows:
        key = row.get("url_key")
        if (row["status"] != "succeeded" or not row["finished_at"]
                or not isinstance(key, str) or not key):
            continue
        started = datetime.fromisoformat(row["started_at"])
        if key not in latest_success_start or started > latest_success_start[key]:
            latest_success_start[key] = started

    recovered = set()
    for row in issues:
        key = row.get("url_key")
        if (row["status"] != "failed" or not row["finished_at"]
                or not isinstance(key, str) or not key):
            continue
        success_start = latest_success_start.get(key)
        # Match the private original-URL hash, never a display URL that may
        # have lost a job identifier. An overlapping attempt is not a retry
        # of this failed outcome; require a new attempt after its completion.
        # An interrupted attempt has no observed finish time. A newer successful
        # attempt is sufficient; its retry may finish before the old deadline.
        recovery_boundary = row["started_at"] if row.get("reconciled_at") else row["finished_at"]
        if success_start and success_start > datetime.fromisoformat(recovery_boundary):
            recovered.add(row["attempt_id"])
    return recovered


def _dismissed_fingerprints(root: Path, now: datetime) -> tuple[set[str], bool]:
    dismissed: set[str] = set()
    incomplete = False
    read_count = 0
    for offset in range(2):
        directory = root / "browser-health-dismissals" / (now - timedelta(days=offset)).date().isoformat() / "misc"
        try:
            for path in directory.glob("*.json"):
                read_count += 1
                if read_count > 20000:
                    return dismissed, True
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    fingerprints = payload["fingerprints"]
                    if not isinstance(fingerprints, list) or len(fingerprints) > 20000 or not all(
                        isinstance(item, str) for item in fingerprints
                    ):
                        raise ValueError("invalid browser dismissal")
                    dismissed.update(fingerprints)
                except (OSError, ValueError, KeyError, TypeError):
                    incomplete = True
        except OSError:
            incomplete = True
    return dismissed, incomplete


def dismiss_issues(root: Path, attempt_ids: list[str] | None, *, now: datetime | None = None) -> int:
    """Acknowledge current issues without removing their telemetry records."""
    now = now or datetime.now(UTC)
    rows, recent, _ = _recent_attempts(root, now)
    dismissed, _ = _dismissed_fingerprints(root, now)
    issues = {row["attempt_id"]: row for row in recent if row["status"] in {"failed", "needs_review", "overdue"}}
    selected = (
        set(issues) - _recovered_failure_ids(rows, list(issues.values()))
        if attempt_ids is None else set(attempt_ids)
    )
    if not selected.issubset(issues):
        raise KeyError("browser issue not found")
    new_fingerprints = sorted({_issue_fingerprint(issues[attempt_id]) for attempt_id in selected} - dismissed)
    if not new_fingerprints:
        return 0
    storage = ArtifactStorageService(root / "browser-health-dismissals" / now.date().isoformat())
    storage.write_bytes(
        artifact_type="browser_health_dismissal",
        content=json.dumps({"fingerprints": new_fingerprints, "dismissed_at": now.isoformat()}).encode(),
        original_file_name="dismissed-issues.json", content_type="application/json",
        privacy_class="private", source_kind="browser_diagnostics",
    )
    return len(new_fingerprints)


def summarize(root: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    rows, recent, incomplete = _recent_attempts(root, now)
    dismissed, dismissal_incomplete = _dismissed_fingerprints(root, now)
    counts = Counter(row["status"] for row in recent)
    all_issues = sorted(
        (row for row in recent if row["status"] in {"failed", "needs_review", "overdue"}),
        key=lambda row: row["finished_at"] or row["started_at"], reverse=True,
    )
    undismissed = [row for row in all_issues if _issue_fingerprint(row) not in dismissed]
    recovered = _recovered_failure_ids(rows, undismissed)
    issues = [row for row in undismissed if row["attempt_id"] not in recovered]
    active_issue_counts = Counter(row["status"] for row in issues)
    last_success = max((row["finished_at"] for row in rows if row["status"] == "succeeded"), default=None)
    return {
        "observed_at": now.isoformat(), "window_hours": 24, "history_days": 7,
        "counts": {name: counts[name] for name in ("running", "succeeded", "failed", "needs_review", "overdue")},
        "active_issue_counts": {name: active_issue_counts[name] for name in ("failed", "needs_review", "overdue")},
        "dismissed_issue_count": len(all_issues) - len(undismissed),
        "recovered_failure_count": len(recovered),
        "total": len(recent), "last_success_at": last_success, "incomplete": incomplete or dismissal_incomplete,
        "failure_reasons": [{"reason": reason, "count": count} for reason, count in Counter(
            row["reason"] for row in issues if row["status"] == "failed"
        ).most_common()],
        "recent_issues": issues[:20],
    }
