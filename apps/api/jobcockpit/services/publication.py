"""Optional, transactional publication outbox with durable retry and echo protection."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select, update

from jobcockpit.api.background_worker_leases import (
    BackgroundWorkerLeaseGuard, begin_background_worker_lease_transaction,
)
from jobcockpit.db.models import JobPosting, JobPublication, JobStatusCheck
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.url_policy import UrlFetchPolicy
from jobcockpit.ingestion.pinned_http import PinnedHttpClient
from jobcockpit.services.publication_assessment import EditorialAssessment, generate_assessment
from jobcockpit.services.publication_policy import (
    eligible, fingerprint, iso_date, load_policy,
)
from jobcockpit.services.worker_supervisor import LeasedWorker
from jobcockpit.status_evidence import status_evidence_label
from jobcockpit.utils.ids import new_id
from jobcockpit.utils.public_urls import sanitize_public_source_url

PUBLICATION_LEASE = "publication_queue"


def _public_facts(job):
    role = job.role or {}
    timing = job.employment_timing or {}
    return {
        "company": job.company_name,
        "title": role.get("job_title") or job.normalized_title,
        "description": str(role.get("description_clean") or "")[:14000],
        "capabilities": list(role.get("tech_stack") or [])[:30],
        "location": {key: (job.location or {}).get(key) for key in
                     ("city", "province_state", "country", "work_arrangement")},
        "timing": {key: timing.get(key) for key in
                   ("raw_label", "start_date", "end_date", "duration_label", "duration_months")},
        "duration_months": job.duration_months or timing.get("duration_months"),
        "eligibility": {key: (job.eligibility or {}).get(key) for key in
                        ("current_enrollment_required", "co_op_enrollment_required",
                         "work_authorization")},
        "compensation": {key: (job.compensation or {}).get(key) for key in
                         ("pay_min", "pay_max", "pay_currency", "pay_period")},
    }


def _input(job, policy):
    verified = job.status_last_checked_at if job.status_check_result in {
        "open", "closed", "expired"
    } else job.status_last_open_at
    return {
        "facts": _public_facts(job),
        "url": job.source_url,
        "status": job.status,
        "verified": iso_date(verified),
        "next_check": iso_date(job.status_next_check_at),
        "evidence": status_evidence_label(job.status_check_evidence_code)
        if job.status_check_evidence_code else "Imported posting; availability needs confirmation.",
        "check_result": job.status_check_result,
        "deadline": iso_date(job.application_deadline),
        "policy": fingerprint(policy.model_dump()),
    }


def enqueue_job(db, job, settings, *, policy=None, retry=False):
    if not settings.publication_enabled or not job.id:
        return None
    policy = policy or load_policy(settings)
    if policy is None:
        return None
    publication_id = fingerprint([policy.endpoint, job.id])
    row = next((item for item in db.new if isinstance(item, JobPublication)
                and item.publication_id == publication_id), None)
    if row is None:
        # Serialize revision allocation with competing transactions before reading.
        # SQLite takes its write lock even for a missing row; existing rows are
        # refreshed because the session may have cached a pre-lock snapshot.
        db.execute(update(JobPublication).where(
            JobPublication.publication_id == publication_id,
        ).values(publication_id=JobPublication.publication_id),
            execution_options={"synchronize_session": False})
        row = db.get(JobPublication, publication_id, populate_existing=True)
    # Once delivered (or attempted with an uncertain result), lifecycle corrections
    # remain eligible even after the initial selection criteria cease to match.
    already_sent = row is not None and bool(row.published_at or row.payload)
    allowed = already_sent or eligible(job, policy)
    if not allowed:
        if row and row.status not in {"succeeded", "cancelled"}:
            row.status, row.reason_code = "cancelled", "no_longer_eligible"
        return row
    data = _input(job, policy)
    if already_sent and (job.source or {}).get("merged_into_job_id"):
        data["status"] = "withdrawn"
        data["duplicate_removed"] = True
        data["url"] = (row.payload or {}).get("url") or data["url"]
        data["evidence"] = (
            "Withdrawn from this board after duplicate reconciliation; "
            "this does not confirm employer closure."
        )
    digest = fingerprint(data)
    if row and row.input_hash == digest and row.status != "cancelled" and not retry:
        return row
    now = datetime.now(UTC)
    if row is None:
        row = JobPublication(
            publication_id=publication_id, job_id=job.id, destination=policy.endpoint,
            remote_id="jc-" + publication_id, assessment={}, payload={}, history=[], attempts=0,
        )
        db.add(row)
    previous = row.input_data or {}
    # Persist ordering with the input, in the same transaction as the job change.
    data["revision"] = previous.get("revision", 0) + 1
    data["status_changed"] = previous.get("status_changed")
    if previous.get("status") != data["status"]:
        data["status_changed"] = iso_date(now)
    row.input_data = data
    row.input_hash = digest
    row.status, row.reason_code, row.lease_token = "pending", None, None
    row.attempts, row.next_attempt_at = 0, now
    return row


def enqueue_changed_jobs(db, _context, _instances):
    settings = db.info["publication_settings"]
    if not settings.publication_enabled:
        return
    policy = load_policy(settings)
    if policy is None:
        return
    changed = {item.id: item for item in list(db.new) + list(db.dirty)
               if isinstance(item, JobPosting)}
    # The status reconciler uses guarded SQL UPDATEs, not ORM attribute writes.
    # Its check receipt is inserted in that same transaction.
    for item in list(db.new):
        if isinstance(item, JobStatusCheck) and item.job_id not in changed:
            job = db.get(JobPosting, item.job_id, populate_existing=True)
            if job:
                changed[job.id] = job
    for job in changed.values():
        enqueue_job(db, job, settings, policy=policy)


def excluded_publication_ids(db, settings, source_url):
    # Keep echo protection when sending is disabled, provided local policy remains.
    policy = load_policy(settings)
    if not policy:
        return frozenset()
    hosts = set(policy.source_hosts) | {urlsplit(policy.endpoint).hostname}
    if urlsplit(source_url).hostname not in hosts:
        return frozenset()
    return frozenset(db.scalars(select(JobPublication.remote_id).where(
        JobPublication.destination == policy.endpoint,
    )))


def _make_payload(row, policy, assessment):
    data, facts = row.input_data, row.input_data["facts"]
    url = str(data.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_source_url")
    if not facts.get("company") or not facts.get("title"):
        raise ValueError("missing_public_fields")
    country = policy.country_names.get(str(facts["location"].get("country") or ""))
    if not country:
        raise ValueError("unsupported_country")
    closed = data["status"] != "active"
    evidence = data.get("evidence") or "Availability needs confirmation."
    status = "Expired" if closed else "Open" if (
        data.get("verified") and data.get("check_result") == "open"
    ) else "Unknown"
    if data["status"] in {"filled", "withdrawn", "expired"}:
        status = {"filled": "Filled", "withdrawn": "Withdrawn", "expired": "Expired"}[data["status"]]
        evidence = f"Posting marked {data['status']} in the source system."
    elif closed and "filled" in evidence.lower():
        status = "Filled"
    elif closed and any(word in evidence.lower() for word in ("withdrawn", "unposted")):
        status = "Withdrawn"
    if data.get("duplicate_removed"):
        evidence = data["evidence"]
    summary = assessment["summary"]
    duration = facts.get("duration_months")
    duration_label = facts["timing"].get("duration_label") or ""
    if duration and duration > policy.standard_duration_months:
        summary += f" Term commitment: {duration} months; confirm that this longer placement fits."
    elif duration_label:
        summary += f" Term commitment: {duration_label}; confirm that the duration fits."
    elif not duration:
        summary += " Term length is not confirmed; check the required commitment."
    term = policy.display_term
    label = facts["timing"].get("raw_label")
    if label and label.casefold() != term.casefold():
        term += f" · {label}"
    if duration:
        term += f" · {duration} months"
    payload = {
        "id": row.remote_id, "group": "closed" if closed else "latest",
        "company": facts["company"], "title": facts["title"], "country": country,
        "term": term[:160], "status": status, "cadence": "closed" if closed else "daily",
        "failures": 0, "statusEvidence": evidence, "summary": summary,
        "verdict": "Duplicate listing removed" if data.get("duplicate_removed") else
            "No longer accepting applications" if closed else assessment["verdict"],
        "tags": assessment["tags"], "url": sanitize_public_source_url(url),
        "publicationRevision": data.get("revision", 1),
    }
    for key, value in {
        "verified": data.get("verified") or (row.payload or {}).get("verified"),
        "nextCheck": None if closed else data.get("next_check"),
        "deadline": data.get("deadline"), "statusChanged": data.get("status_changed"),
        "location": " · ".join(str(facts["location"].get(key)) for key in
                                ("city", "province_state", "work_arrangement")
                                if facts["location"].get(key) not in (None, "", "unknown")),
    }.items():
        if value:
            payload[key] = value
    # The receiving contract supplies today's date when absent. Never allow that
    # default to turn an unverified import into a freshly verified public claim.
    if not payload.get("verified"):
        raise ValueError("source_verification_required")
    if len(json_bytes(payload)) > 65536:
        raise ValueError("payload_too_large")
    return payload


def json_bytes(payload):
    import json
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _fence(db, worker_id, token):
    return begin_background_worker_lease_transaction(
        db, worker_id, token, lease_name=PUBLICATION_LEASE,
    )


def process_next_publication(settings, *, assessor=None, transport=None, worker_id=None,
                             supervisor_lease_token=None):
    if not settings.publication_enabled or not settings.publication_write_token:
        return None
    policy = load_policy(settings)
    if policy is None:
        return None
    worker_id = worker_id or new_id("publication_worker")
    token = supervisor_lease_token
    if token is None:
        with BackgroundWorkerLeaseGuard(settings, worker_id, lease_name=PUBLICATION_LEASE) as lease:
            if not lease.acquired or not lease.lease_token:
                return None
            return process_next_publication(settings, assessor=assessor, transport=transport,
                                            worker_id=worker_id,
                                            supervisor_lease_token=lease.lease_token)
    with session_scope(settings) as db:
        if not _fence(db, worker_id, token):
            return None
        for stale in db.scalars(select(JobPublication).where(
            JobPublication.destination == policy.endpoint, JobPublication.status == "running",
            or_(JobPublication.lease_token != token, JobPublication.lease_token.is_(None)),
        )):
            stale.status = "failed" if stale.attempts >= settings.publication_max_attempts else "pending"
            stale.next_attempt_at = datetime.now(UTC) if stale.status == "pending" else None
            stale.reason_code = "worker_interrupted"
        db.flush()
        row = db.scalar(select(JobPublication).where(
            JobPublication.destination == policy.endpoint,
            JobPublication.status.in_(["pending", "retry_scheduled"]),
            JobPublication.next_attempt_at <= datetime.now(UTC),
        ).order_by(JobPublication.next_attempt_at, JobPublication.publication_id).limit(1))
        if row is None:
            return None
        job = db.get(JobPosting, row.job_id) if row.job_id else None
        if job is None or (
            not row.payload and not row.published_at and not eligible(job, policy)
        ):
            row.status, row.reason_code = "cancelled", "no_longer_eligible"
            return row.to_api()
        enqueue_job(db, job, settings, policy=policy)
        try:
            _make_payload(row, policy, {"summary": "", "verdict": "", "tags": []})
        except ValueError:
            # Wait for a committed source-data/status change without spending
            # LLM calls or exhausting delivery retries on incomplete evidence.
            row.status, row.reason_code = "blocked", "public_source_data_required"
            row.next_attempt_at, row.lease_token = None, None
            return row.to_api()
        row.status, row.lease_token = "running", token
        row.attempts += 1
        publication_id, guard = row.publication_id, row.input_hash
        assessment_hash = fingerprint([row.input_data["facts"], policy.audience_brief,
                                       policy.assessment_model])
        cached_assessment = copy.deepcopy(row.assessment) if (
            row.assessment_hash == assessment_hash and row.assessment) else None
        data = copy.deepcopy(row.input_data["facts"])
    try:
        assessment = cached_assessment or (assessor or generate_assessment)(data, policy, settings)
        assessment = EditorialAssessment.model_validate(assessment).model_dump()
        # Persist the exact complete payload before delivery. HTTP retries and
        # uncertain acknowledgements reuse both identity and editorial assessment.
        with session_scope(settings) as db:
            if not _fence(db, worker_id, token):
                return None
            row = db.get(JobPublication, publication_id)
            if row.input_hash != guard or row.lease_token != token or row.status != "running":
                return None
            row.assessment, row.assessment_hash = assessment, assessment_hash
            payload = _make_payload(row, policy, assessment)
            row.payload = payload
        if not settings.publication_enabled:
            return None
        # Resolve the destination policy before attaching any credential. Never
        # follow redirects to another host or log response bodies/request headers.
        if transport is not None and not UrlFetchPolicy().check(policy.endpoint).allowed:
            raise ValueError("destination_blocked")
        with session_scope(settings) as db:
            if not _fence(db, worker_id, token):
                return None
            row = db.get(JobPublication, publication_id)
            if row.input_hash != guard or row.lease_token != token or row.status != "running":
                return None
        # Production always connects to the exact policy-approved numeric address.
        # The explicit transport seam is only used by isolated network-free tests.
        client = PinnedHttpClient(timeout=settings.publication_timeout_seconds) if transport is None else \
            httpx.Client(timeout=settings.publication_timeout_seconds, follow_redirects=False,
                         trust_env=False, transport=transport)
        try:
            with client.stream("POST", policy.endpoint, json=payload, headers={
                "Authorization": "Bearer " + settings.publication_write_token.get_secret_value(),
                "Idempotency-Key": fingerprint([publication_id, guard]),
            }) as response:
                response.read()
        finally:
            client.close()
        status_code = response.status_code
        if status_code not in {200, 201}:
            return _finish(settings, worker_id, token, publication_id, guard,
                           f"http_{status_code}", retry=status_code in {408, 425, 429}
                           or status_code >= 500)
        receipt = response.json()
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("data"), dict)
                or receipt["data"].get("id") != payload["id"]
                or receipt.get("publicationRevision") != payload["publicationRevision"]):
            raise ValueError("invalid_delivery_receipt")
    except Exception:
        # Keep unexpected provider/transport failures recoverable too. Never
        # persist exception strings, which can contain prompts or credentials.
        return _finish(settings, worker_id, token, publication_id, guard,
                       "assessment_or_delivery_failed", retry=True)
    return _finish(settings, worker_id, token, publication_id, guard, None, retry=False)


def _finish(settings, worker_id, token, publication_id, guard, reason, *, retry):
    with session_scope(settings) as db:
        if not _fence(db, worker_id, token):
            return None
        row = db.get(JobPublication, publication_id)
        if row is None:
            return None
        now = datetime.now(UTC)
        row.history = [*(row.history or []), {
            "at": now.isoformat(), "input_hash": guard, "reason": reason,
            "outcome": "succeeded" if reason is None else "failed",
        }][-50:]
        if reason is None:
            row.published_at = now
        if row.input_hash != guard or row.lease_token != token:
            # A newer committed change is already queued; never acknowledge it
            # using an older payload's response.
            return row.to_api()
        retry = retry and row.attempts < settings.publication_max_attempts
        row.status = "succeeded" if reason is None else "retry_scheduled" if retry else "failed"
        row.reason_code, row.lease_token = reason, None
        row.next_attempt_at = now + timedelta(seconds=min(3600, 30 * 2 ** row.attempts)) \
            if retry else None
        return row.to_api()


class PublicationWorker(LeasedWorker):
    def __init__(self, settings):
        super().__init__(settings, lease_name=PUBLICATION_LEASE, prefix="publication_worker")

    def _process_once(self, token):
        return process_next_publication(self.settings, worker_id=self.worker_id,
                                        supervisor_lease_token=token)

    def _recover_stale_work(self, token, *, include_owned=False):
        if not include_owned:
            return
        with session_scope(self.settings) as db:
            if not _fence(db, self.worker_id, token):
                return
            for row in db.scalars(select(JobPublication).where(
                JobPublication.status == "running", JobPublication.lease_token == token,
            )):
                row.status = "failed" if row.attempts >= self.settings.publication_max_attempts else "pending"
                row.next_attempt_at = datetime.now(UTC) if row.status == "pending" else None
                row.lease_token, row.reason_code = None, "worker_interrupted"
