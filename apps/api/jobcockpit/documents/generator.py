from __future__ import annotations

from typing import Any

from jobcockpit.db.models import EvidenceItem, JobPosting

MAX_RESUME_EVIDENCE_ITEMS = 5
MAX_COVER_LETTER_EVIDENCE_ITEMS = 3


def build_resume_markdown(
    job: JobPosting,
    profile_data: dict[str, Any] | None = None,
    evidence_items: list[EvidenceItem] | None = None,
) -> str:
    title = job.role.get("job_title") or "Target Role"
    company = job.employer.get("company_name") or "the employer"
    target_requirements = ", ".join(_role_capabilities(job))
    if not profile_data:
        bullets = (
            [f"- Tailor evidence-backed examples around target requirements: {target_requirements}."]
            if target_requirements
            else ["- Add evidence-backed accomplishments before approval."]
        )
        return "\n".join(
            [
                "# Synthetic Candidate",
                "",
                f"## Target: {title} at {company}",
                "",
                *bullets,
                "",
            ]
        )

    name = profile_data.get("full_name") or "Candidate"
    contact = _contact_line(profile_data)
    skills = profile_data.get("skills") or []
    evidence = select_evidence_for_job(job, evidence_items or [], MAX_RESUME_EVIDENCE_ITEMS)
    bullets = [f"- {item.claim}" for item in evidence]
    if not bullets:
        bullets = (
            [f"- Add evidence-backed examples around target requirements: {target_requirements}."]
            if target_requirements
            else ["- Add evidence-backed accomplishments before approval."]
        )

    lines = ["# " + name]
    if contact:
        lines.extend(["", contact])
    if profile_data.get("summary"):
        lines.extend(["", "## Summary", profile_data["summary"]])
    lines.extend(["", f"## Target: {title} at {company}"])
    if skills:
        lines.extend(["", "## Skills", ", ".join(skills)])
    lines.extend(["", "## Evidence", *bullets, ""])
    return "\n".join(lines)


def build_cover_letter_markdown(
    job: JobPosting,
    profile_data: dict[str, Any] | None = None,
    evidence_items: list[EvidenceItem] | None = None,
) -> str:
    title = job.role.get("job_title") or "the role"
    company = job.employer.get("company_name") or "your organization"
    name = profile_data.get("full_name") if profile_data else "Synthetic Candidate"
    evidence = select_evidence_for_job(job, evidence_items or [], MAX_COVER_LETTER_EVIDENCE_ITEMS)
    lines = [
        f"# {name}",
        "",
        "Dear hiring team,",
        "",
        f"I am interested in the {title} role at {company}.",
    ]
    if evidence:
        lines.extend(["", "Relevant evidence:", *[f"- {item.claim}" for item in evidence]])
    lines.extend(["", f"Sincerely,\n{name}"])
    return "\n".join(lines)


def _contact_line(profile_data: dict[str, Any]) -> str:
    parts = [
        profile_data.get("email"),
        profile_data.get("phone"),
        _profile_location(profile_data),
        profile_data.get("linkedin_url"),
        profile_data.get("github_url"),
        *(profile_data.get("links") or []),
    ]
    return " | ".join(_unique_contact_parts(parts))


def _unique_contact_parts(parts: list[Any]) -> list[str]:
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique_parts.append(text)
    return unique_parts


def _profile_location(profile_data: dict[str, Any]) -> str:
    return ", ".join(
        str(profile_data.get(key) or "").strip()
        for key in ("city", "province_state", "country")
        if str(profile_data.get(key) or "").strip()
    )


def select_evidence_for_job(job: JobPosting, evidence_items: list[EvidenceItem], limit: int) -> list[EvidenceItem]:
    return _rank_evidence_for_job(job, evidence_items)[:limit]


def _rank_evidence_for_job(job: JobPosting, evidence_items: list[EvidenceItem]) -> list[EvidenceItem]:
    required, preferred, responsibility, fallback = _role_capability_groups(job)
    weighted_terms = {
        **{term.casefold(): 1 for term in fallback},
        **{term.casefold(): 1 for term in responsibility},
        **{term.casefold(): 2 for term in preferred},
        **{term.casefold(): 3 for term in required},
    }
    if not weighted_terms:
        return evidence_items

    def score(item: EvidenceItem) -> int:
        return max(
            (weighted_terms.get(str(skill).strip().casefold(), 0) for skill in item.skills),
            default=0,
        )

    return [
        item
        for _, item in sorted(
            enumerate(evidence_items),
            key=lambda indexed: (-score(indexed[1]), indexed[0]),
        )
    ]


def _role_capabilities(job: JobPosting) -> list[str]:
    groups = _role_capability_groups(job)
    ordered: list[str] = []
    seen: set[str] = set()
    for capability in (item for group in groups for item in group):
        key = capability.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(capability)
    return ordered


def _role_capability_groups(job: JobPosting) -> tuple[list[str], list[str], list[str], list[str]]:
    def values(field: str) -> list[str]:
        raw_values = job.role.get(field) or []
        return [str(value).strip() for value in raw_values if str(value).strip()]

    capabilities = values("capabilities")
    fallback = capabilities or values("tech_stack")
    return (
        values("required_capabilities"),
        values("preferred_capabilities"),
        values("responsibility_capabilities"),
        fallback,
    )
