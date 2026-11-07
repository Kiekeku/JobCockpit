from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobcockpit.api.profile_routes import CANDIDATE_ID
from jobcockpit.core.config import Settings
from jobcockpit.db.models import CandidateProfile, EvidenceItem, JobPosting, StoredArtifact
from jobcockpit.ingestion.fit_analysis import analyze_fit_with_settings
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_discovery import (
    ExternalPostingHint,
    external_aggregator_kind,
    external_source_kind,
    posting_search_term_from_text,
)
from jobcockpit.ingestion.posting_links import canonicalize_posting_url
from jobcockpit.ingestion.url_hosts import hostname_matches_domain
from jobcockpit.storage.artifacts import ArtifactStorageService


def structured_reprocess_hint(
    job: JobPosting, source_url: str, hint_text: str
) -> ExternalPostingHint:
    parsed_hint = parse_job_text(hint_text, source_url)
    location = parsed_hint.location
    location_text = ", ".join(
        part for part in (location.city, location.province_state, location.country) if part
    )
    return ExternalPostingHint(
        source_url=source_url,
        source_kind="linkedin",
        company=parsed_hint.employer.company_name
        or str((job.employer or {}).get("company_name") or ""),
        title=parsed_hint.role.job_title or str((job.role or {}).get("job_title") or ""),
        location=location_text,
        term=posting_search_term_from_text(
            parsed_hint.employment_timing.raw_label or hint_text
        ),
        link_text=" ".join(
            part
            for part in (
                parsed_hint.role.job_title,
                parsed_hint.employer.company_name,
            )
            if part
        ),
        evidence_text=hint_text[:2000],
        source_text=hint_text,
    )


def reprocess_source_url(job: JobPosting, raw_text: str) -> str:
    source_url = canonicalize_posting_url(
        job.source_url or str((job.source or {}).get("source_url") or "")
    )
    recovered_linkedin_url = reprocess_embedded_linkedin_job_url(job, raw_text)
    if _is_linkedin_auth_or_checkpoint_url(source_url):
        return recovered_linkedin_url or source_url
    if _is_linkedin_non_job_url(source_url) and recovered_linkedin_url:
        return recovered_linkedin_url
    if external_aggregator_kind(source_url) and recovered_linkedin_url:
        return recovered_linkedin_url
    return source_url


def reprocess_source_is_auth_polluted(job: JobPosting, raw_text: str) -> bool:
    original_source_url = job.source_url or str((job.source or {}).get("source_url") or "")
    canonical_source_url = canonicalize_posting_url(original_source_url)
    return (
        _is_linkedin_auth_or_checkpoint_url(original_source_url)
        or _is_linkedin_auth_or_checkpoint_url(canonical_source_url)
        or _is_linkedin_non_job_url(canonical_source_url)
        and bool(reprocess_embedded_linkedin_job_url(job, raw_text))
        or _is_reprocess_auth_or_checkpoint_snapshot(original_source_url, raw_text)
    )


def reprocess_embedded_linkedin_job_url(job: JobPosting, raw_text: str) -> str:
    return linkedin_job_url_from_text(raw_text) or linkedin_job_url_from_text(
        _job_structured_text(job)
    )


def linkedin_job_url_from_text(text: str) -> str:
    match = re.search(r"https://www\.linkedin\.com/jobs/view/\d+", text)
    return match.group(0) if match else ""


def _job_structured_text(job: JobPosting) -> str:
    parts = [
        job.source_url or "",
        str((job.source or {}).get("source_url") or ""),
        str((job.role or {}).get("description_raw") or ""),
        str((job.role or {}).get("description_clean") or ""),
    ]
    return "\n".join(part for part in parts if part)


def _is_linkedin_auth_or_checkpoint_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return hostname_matches_domain(hostname, "linkedin.com") and any(
        token in path for token in ("/authwall", "/checkpoint/", "/login", "/uas/login")
    )


def _is_linkedin_non_job_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return hostname_matches_domain(hostname, "linkedin.com") and "/jobs/view/" not in path


def fit_analysis_candidate_context(db: Session | None) -> tuple[dict | None, list[EvidenceItem]]:
    if db is None:
        return None, []
    profile = db.get(CandidateProfile, CANDIDATE_ID)
    evidence_items = (
        db.execute(
            select(EvidenceItem)
            .where(
                EvidenceItem.candidate_id == CANDIDATE_ID,
                EvidenceItem.allowed_for_resume.is_(True),
            )
            .order_by(EvidenceItem.created_at.asc(), EvidenceItem.evidence_id.asc())
        )
        .scalars()
        .all()
    )
    return (profile.profile_data if profile else None), list(evidence_items)


def enrich_parsed_job_fit_analysis(
    parsed,
    *,
    raw_text: str,
    source_url: str | None,
    db: Session | None,
    settings: Settings | None,
    analyzer=analyze_fit_with_settings,
) -> None:
    profile_data, evidence_items = fit_analysis_candidate_context(db)
    parsed.fit_analysis = analyzer(
        parsed,
        raw_text=raw_text,
        source_url=source_url,
        settings=settings,
        profile_data=profile_data,
        evidence_items=evidence_items,
    )


def reprocess_hint_text(job: JobPosting, raw_text: str) -> str:
    parsed_snapshot = parse_job_text(raw_text, job.source_url, source_type=job.source_type)
    source_url = reprocess_source_url(job, raw_text) or job.source_url or ""
    role = job.role or {}
    employer = job.employer or {}
    stored_location = job.location or {}
    parsed_location = parsed_snapshot.location.model_dump()
    location_keys = ("city", "province_state", "country")
    parsed_location_count = sum(bool(parsed_location.get(key)) for key in location_keys)
    stored_location_count = sum(bool(stored_location.get(key)) for key in location_keys)
    location = (
        parsed_location
        if parsed_location_count >= stored_location_count and parsed_location_count
        else stored_location
    )
    auth_or_challenge_snapshot = reprocess_source_is_auth_polluted(job, raw_text)
    stored_title = str(role.get("job_title") or "")
    stored_company = str(employer.get("company_name") or "")
    parsed_title = str(parsed_snapshot.role.job_title or "")
    parsed_company = str(parsed_snapshot.employer.company_name or "")
    usable_parsed_title = "" if _is_weak_auth_snapshot_title(parsed_title) else parsed_title
    usable_parsed_company = (
        ""
        if _is_weak_reprocess_company(parsed_company, parsed_title)
        or _is_weak_auth_snapshot_title(parsed_title)
        else parsed_company
    )
    source_evidence_title = title_from_source_evidence(raw_text)
    evidence_company = company_from_source_evidence(raw_text)
    capture_source = bool(external_source_kind(source_url))
    portal_source = bool(external_aggregator_kind(source_url))
    if auth_or_challenge_snapshot or capture_source:
        title = source_evidence_title or usable_parsed_title or stored_title or ""
        company = evidence_company or usable_parsed_company or stored_company or ""
    elif portal_source:
        title = source_evidence_title or stored_title or usable_parsed_title or ""
        company = evidence_company or stored_company or usable_parsed_company or ""
    else:
        title = parsed_title or stored_title
        company = parsed_company or stored_company
    if evidence_company and _is_weak_reprocess_company(company, title):
        company = evidence_company
    lines = [
        f"Title: {title}" if title else "",
        f"Company: {company}" if company else "",
        "Location: "
        + ", ".join(
            str(part)
            for part in (
                location.get("city"),
                location.get("province_state"),
                location.get("country"),
            )
            if part
        )
        if any(location.get(key) for key in ("city", "province_state", "country"))
        else "",
        raw_text,
    ]
    return "\n".join(line for line in lines if line).strip()


def _is_reprocess_auth_or_checkpoint_snapshot(source_url: str, raw_text: str) -> bool:
    lowered_text = raw_text.casefold()
    if _is_linkedin_auth_or_checkpoint_url(source_url):
        return True
    linkedin_specific_snapshot = any(
        marker in lowered_text
        for marker in (
            "page title: sign up | linkedin",
            "page title: linkedin app challenge",
            "check your linkedin app",
            "verify using authenticator app",
        )
    )
    if linkedin_specific_snapshot:
        return True
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").casefold()
    return (
        hostname_matches_domain(hostname, "linkedin.com") and "sign in to continue" in lowered_text
    )


def preserve_existing_company_for_weak_reprocess(job: JobPosting, parsed) -> None:
    existing_company = str(
        job.company_name or (job.employer or {}).get("company_name") or ""
    ).strip()
    new_company = str(parsed.employer.company_name or "").strip()
    title = str(parsed.role.job_title or "").strip()
    if existing_company and _is_weak_reprocess_company(new_company, title):
        parsed.employer.company_name = existing_company


def _is_weak_auth_snapshot_title(title: str) -> bool:
    normalized_title = title.casefold().strip()
    if not normalized_title:
        return True
    return normalized_title in {
        "linkedin app challenge",
        "sign up | linkedin",
        "sign in | linkedin",
        "login | linkedin",
    }


def company_from_source_evidence(raw_text: str) -> str:
    for line in raw_text.splitlines():
        if not line.strip().casefold().startswith("source evidence:"):
            continue
        evidence = line.split(":", 1)[1]
        evidence_title = clean_reprocess_evidence_title(_inline_evidence_field(evidence, "Title"))
        inline_company = clean_reprocess_evidence_company(
            _inline_evidence_field(evidence, "Company")
        )
        if inline_company and not _is_weak_reprocess_company(inline_company, evidence_title):
            return inline_company
        hiring_company = clean_reprocess_evidence_company(
            _hiring_evidence_field(evidence, "company")
        )
        if hiring_company:
            return hiring_company
        segments = [
            segment.strip()
            for segment in re.split(r"\s*(?:\u00b7|Â·|\|)\s*", evidence)
            if segment.strip()
        ]
        if len(segments) >= 2:
            company = clean_reprocess_evidence_company(segments[1])
            if company:
                return company
    return ""


def title_from_source_evidence(raw_text: str) -> str:
    for line in raw_text.splitlines():
        if not line.strip().casefold().startswith("source evidence:"):
            continue
        evidence = line.split(":", 1)[1]
        title = clean_reprocess_evidence_title(_inline_evidence_field(evidence, "Title"))
        hiring_title = clean_reprocess_evidence_title(_hiring_evidence_field(evidence, "title"))
        if title and " hiring " in title.casefold() and hiring_title:
            return hiring_title
        if title:
            return title
        if hiring_title:
            return hiring_title
    return ""


def _inline_evidence_field(value: str, label: str) -> str:
    match = re.search(
        rf"\b{re.escape(label)}:\s*(?P<value>.*?)(?=\s+(?:Title|Company|Location|Page Title|Source URL):|$)",
        value,
        flags=re.IGNORECASE,
    )
    return match.group("value") if match else ""


def _hiring_evidence_field(value: str, field: str) -> str:
    matches = list(
        re.finditer(
            r"(?:^|\bSource Evidence:\s*|\|\s*)"
            r"(?P<company>[^|:\n]{1,120}?)\s+hiring\s+"
            r"(?P<title>[^|\n]{1,240}?)\s+in\s+.+?(?=\s+\|\s+LinkedIn\b|$)",
            value.strip(),
            flags=re.IGNORECASE,
        )
    )
    match = next(
        (
            candidate
            for candidate in reversed(matches)
            if not re.search(
                r"\b(?:Title|Company|Location|Page Title|Source URL):", candidate.group("company")
            )
        ),
        None,
    )
    if match is None:
        match = next(
            (
                candidate
                for candidate in reversed(matches)
                if not re.search(
                    r"\b(?:Title|Company|Location|Page Title|Source URL):", candidate.group("title")
                )
            ),
            None,
        )
    if not match:
        match = re.search(
            r"(?P<company>[^|:\n]{1,120}?)\s+hiring\s+(?P<title>[^|\n]{1,240}?)\s+in\s+.+?(?:\s+\|\s+LinkedIn\b|$)",
            value.strip(),
            flags=re.IGNORECASE,
        )
    if not match:
        return ""
    return match.group(field).strip()


def clean_reprocess_evidence_title(value: str) -> str:
    title = re.split(
        r"\s+(?:Company|Location|Page Title|Source URL):",
        value.strip(),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    title = " ".join(title.strip(" -|").split())
    return re.sub(r"\s+\|\s+LinkedIn$", "", title, flags=re.IGNORECASE)


def clean_reprocess_evidence_company(value: str) -> str:
    company = re.split(
        r"\s+(?:Title|Company|Location):", value.strip(), maxsplit=1, flags=re.IGNORECASE
    )[0]
    company = " ".join(company.strip(" -|").split())
    for prefix in ("LinkedIn", "Indeed"):
        if company.casefold().startswith(f"{prefix.casefold()} ") and len(company.split()) > 1:
            company = company[len(prefix) :].strip()
    return company


REPROCESS_ROLE_WORD_COMPANIES = {
    "backend",
    "co-op",
    "coop",
    "developer",
    "engineer",
    "engineering",
    "intern",
    "internship",
    "software",
    "student",
}


def _is_weak_reprocess_company(company: str, title: str) -> bool:
    if not company:
        return True
    normalized_company = company.casefold().strip()
    if normalized_company in {"page", "unknown"}:
        return True
    if re.search(
        r"\b(?:Title|Company|Location|Page Title|Source URL):", company, flags=re.IGNORECASE
    ):
        return True
    if normalized_company.strip(" ,.-") in REPROCESS_ROLE_WORD_COMPANIES:
        return True

    normalized_title = " ".join(re.sub(r"[^\w#+.-]+", " ", title.casefold()).split())
    normalized_company_words = " ".join(re.sub(r"[^\w#+.-]+", " ", normalized_company).split())
    if normalized_title and normalized_company_words.startswith(normalized_title):
        return True

    first_title_token = normalized_title.split(maxsplit=1)[0] if normalized_title else ""
    return bool(first_title_token and normalized_company_words == first_title_token)


def raw_job_snapshot_text(
    job: JobPosting,
    db: Session,
    storage: ArtifactStorageService,
) -> str:
    artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
    if (
        artifact is None
        or artifact.job_id != job.id
        or artifact.artifact_type != "job_snapshot"
        or artifact.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail="Job raw snapshot not found")
    try:
        return storage.read_bytes(artifact.storage_uri).decode("utf-8")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=409, detail="Job raw snapshot is not UTF-8 text") from error
