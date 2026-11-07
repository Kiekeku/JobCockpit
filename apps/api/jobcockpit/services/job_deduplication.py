"""Conservative, unique cross-site matches for dated student opportunities."""

import re
from hashlib import sha256
from urllib.parse import unquote, urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from jobcockpit.ingestion.inputs import request_source_url_dedup_safe

from jobcockpit.api.job_access import job_is_merged_duplicate
from jobcockpit.db.models import JobAlertLink, JobPosting, StoredArtifact
from jobcockpit.ingestion.ingestion_identity import (
    authoritative_ingestion_identity, ingestion_locator_identity, is_verification_url,
)
from jobcockpit.ingestion.status_identity import posting_identity
from jobcockpit.ingestion.browser_capture_adapters import capture_source_identity
from jobcockpit.ingestion.parser_models import ParsedApplication
from jobcockpit.ingestion.location_parser import CANADIAN_PROVINCE_CODES, CANADIAN_PROVINCE_ALIASES
from jobcockpit.ingestion.ats_adapters.workday import parse_workday_url
from jobcockpit.ingestion.ats_adapters.base import AtsProviderError


def workday_requisition_identity(source_url: str | None, raw_text: str) -> dict | None:
    """Use the fetched Req ID, never guess a requisition by stripping URL suffixes."""
    if not source_url:
        return None
    try:
        site = parse_workday_url(source_url)
    except (ValueError, AtsProviderError):
        return None
    if not site.external_path or not site.external_path.startswith("/job/"):
        return None
    requisition = labeled_requisition(raw_text)
    if not requisition:
        return None
    segment = unquote(site.external_path.rstrip("/").rsplit("/", 1)[-1])
    if not re.search(rf"(?:^|_){re.escape(requisition)}(?:-\d+)?$", segment, re.I):
        return None
    return {"host": urlparse(site.origin).hostname, "tenant": site.tenant,
            "requisition_id": requisition.casefold()}


def find_workday_requisition_duplicate(request, db: Session, storage=None) -> JobPosting | None:
    if not request_source_url_dedup_safe(request):
        return None
    incoming = workday_requisition_identity(request.source_url, getattr(request, "raw_text", ""))
    if not incoming:
        return None
    # Deterministic selection also handles historical duplicate rows: several
    # copies of one authoritative requisition must not cause another insert.
    for job in db.scalars(select(JobPosting).where(
        JobPosting.source_url.contains(incoming["host"])
    ).order_by(JobPosting.created_at, JobPosting.id)):
        if job_is_merged_duplicate(job) or (job.source or {}).get("source_url_dedup_safe") is False:
            continue
        evidence = (job.source or {}).get("workday_requisition")
        if evidence:
            # Revalidate the locator after source edits; metadata may be historical.
            known = workday_requisition_identity(
                job.source_url, f"Req ID: {evidence.get('requisition_id', '')}",
            )
            if known != evidence:
                continue
        else:
            raw = verified_posting_text(job, db, storage) if storage is not None else None
            known = workday_requisition_identity(
                job.source_url, raw if raw is not None else (job.role or {}).get("description_raw") or "",
            )
        if known == incoming:
            return job
    return None


def normalized(value) -> str:
    return " ".join(str(value or "").casefold().split())


def find_authoritative_source_duplicate(request, db: Session) -> JobPosting | None:
    """An immutable posting ID outranks provider-generated names/translations.

    Scan current locators as well as the registry: rows imported before an
    identity adapter existed have no entry under its new identity scheme.
    """
    if not request_source_url_dedup_safe(request):
        return None
    incoming = authoritative_ingestion_identity(request.source_url)
    if not incoming:
        return None
    host = (urlparse(request.source_url).hostname or "").removeprefix("www.")
    if incoming[0] in {"linkedin", "indeed"}:
        host = "linkedin.com" if incoming[0] == "linkedin" else "indeed."
    candidates = db.scalars(select(JobPosting).where(
        func.lower(JobPosting.source_url).contains(host.casefold()),
    ).order_by(JobPosting.created_at, JobPosting.id))
    for job in candidates:
        if job_is_merged_duplicate(job) or (job.source or {}).get("source_url_dedup_safe") is False:
            continue
        if authoritative_ingestion_identity(job.source_url) == incoming:
            return job
    return None


def find_linked_portal_duplicate(request, db: Session) -> JobPosting | None:
    """Recover legacy Gmail aliases only while their resolved destination holds.

    A historical link/registry row alone is not evidence after a source edit.
    Manual overrides, unsafe sources and conflicting official targets win.
    """
    if not request_source_url_dedup_safe(request):
        return None
    portal_url = getattr(request, "portal_source_url", None) or request.source_url
    portal = posting_identity(portal_url or "")
    if portal.source_kind not in {"linkedin", "indeed"} or not portal.is_detail:
        return None
    expected = authoritative_ingestion_identity(portal_url)
    incoming = authoritative_ingestion_identity(request.source_url)
    if not expected or not incoming:
        # Generic official routes may still be safe exact locators.
        incoming_capture = capture_source_identity(request.source_url or "")
        if not expected or not incoming_capture or not incoming_capture.source_url_dedup_safe:
            return None
        incoming = (incoming_capture.provider, incoming_capture.identity_hash)
    matches = {}
    links = db.scalars(select(JobAlertLink).where(
        JobAlertLink.original_url.contains(portal.posting_id),
        JobAlertLink.linked_job_id.is_not(None),
        JobAlertLink.ingestion_status.in_(("imported", "duplicate")),
    ))
    for link in links:
        if authoritative_ingestion_identity(link.original_url) != expected:
            continue
        job = db.get(JobPosting, link.linked_job_id)
        if job is None or job_is_merged_duplicate(job):
            continue
        source = job.source or {}
        if (source.get("source_url_dedup_safe") is False or is_verification_url(job.source_url)
                or (source.get("source_resolution") or {}).get("kind") == "manual_override"):
            continue
        def locator(url):
            identity = capture_source_identity(url or "")
            return authoritative_ingestion_identity(url) or (
                (identity.provider, identity.identity_hash) if identity and identity.source_url_dedup_safe else None
            )
        current = locator(job.source_url)
        if not current or current != locator(link.resolved_url):
            continue
        if incoming != expected and incoming != current:
            continue
        # A different current portal ID invalidates an old portal association.
        current_posting = posting_identity(job.source_url or "")
        if current_posting.source_kind == portal.source_kind and current != expected:
            continue
        matches[job.id] = job
    return next(iter(matches.values())) if len(matches) == 1 else None


def find_portal_identity_duplicate(request, parsed, db: Session) -> JobPosting | None:
    """Reuse an explicit provider job ID carried through official-site resolution.

    Provider summaries may omit the legal employer name and duration. Those
    omissions cannot erase an exact listing identity, but conflicting populated
    fields or different official destinations must still prevent a match.
    """
    incoming = capture_source_identity(request.source_url or "")
    if (not incoming or not incoming.source_url_dedup_safe
            or not request_source_url_dedup_safe(request)):
        return None
    portal = capture_source_identity(getattr(request, "portal_source_url", None) or "")
    # Only dedicated aggregator job IDs can bridge a portal and employer site.
    def is_portal(identity):
        return (identity is not None and identity.provider in {"linkedin", "indeed"}
                and identity.strength == "authoritative" and identity.source_url_dedup_safe)

    identity = portal if is_portal(portal) else incoming
    if not is_portal(identity):
        return None
    if incoming.provider == identity.provider and incoming.identity_hash != identity.identity_hash:
        return None
    matches = []
    for job in db.scalars(select(JobPosting).where(or_(
        JobPosting.normalized_title == parsed.role.normalized_title,
        func.lower(func.trim(JobPosting.role["job_title"].as_string()))
        == normalized(parsed.role.job_title),
    ))):
        source = job.source or {}
        if (job_is_merged_duplicate(job) or source.get("source_url_dedup_safe") is False
                or (source.get("source_resolution") or {}).get("kind") == "manual_override"):
            continue
        current = capture_source_identity(job.source_url or "")
        if not current or not current.source_url_dedup_safe:
            continue
        if current.provider == identity.provider and current.identity_hash != identity.identity_hash:
            continue
        # Do not use historical registry rows after their provenance is removed.
        known_urls = [job.source_url, source.get("portal_source_url"),
                      *source.get("portal_source_urls", [])]
        known = [capture_source_identity(url or "") for url in known_urls]
        if not any(is_portal(value) and (value.provider, value.identity_hash)
                   == (identity.provider, identity.identity_hash) for value in known):
            continue
        # A resolved alias must not redirect an already official job to another opening.
        if not is_portal(incoming) and not is_portal(current) and (
            incoming.provider, incoming.identity_hash
        ) != (current.provider, current.identity_hash):
            continue
        company, other = normalized(parsed.employer.company_name), normalized(job.company_name)
        short, long = sorted((company, other), key=len)
        # An omitted employer is not a conflicting employer when an explicit
        # provider ID and exact title already establish the alias. Never relax
        # two populated but unrelated employer names.
        if not long or (short and not (short == long or long.startswith(short + " "))):
            continue
        # Legal-name variation is allowed only inside this exact provider-ID match.
        if not (normalized(parsed.role.normalized_title)
                and normalized(parsed.role.normalized_title) == normalized(job.normalized_title)):
            if not (normalized(parsed.role.job_title) and normalized(parsed.role.job_title)
                    == normalized((job.role or {}).get("job_title"))):
                continue
        timing_fields = [
            (getattr(parsed.employment_timing, field), (job.employment_timing or {}).get(field))
            for field in ("start_date", "end_date", "duration_months")
        ]
        # A verified portal alias can use a season label or an explicit date
        # range for the same term. Complete matching dates/duration establish
        # timing; partial timing still needs compatible provider wording.
        if not all(left and right and normalized(left) == normalized(right)
                   for left, right in timing_fields):
            timing_fields.extend(
                (getattr(parsed.employment_timing, field), (job.employment_timing or {}).get(field))
                for field in ("raw_label", "duration_label")
            )
        fields = [
            (getattr(parsed.location, field), (job.location or {}).get(field))
            for field in ("city", "country")
        ] + timing_fields
        if any(left and right and normalized(left) != normalized(right) for left, right in fields):
            continue
        matches.append(job)
    return matches[0] if len(matches) == 1 else None


def posting_site(url: str) -> str:
    identity = capture_source_identity(url)
    if identity and identity.provider != "generic":
        return identity.provider
    return (urlparse(url).hostname or "").removeprefix("www.")


def _description_words(value: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (value or "").casefold())


def _matching_descriptions(left: str, right: str) -> bool:
    # Eight-word shingles ignore markup and punctuation, but require substantial
    # shared wording. Short snippets or repeated boilerplate cannot meet the
    # minimum number of distinct shingles. This supplements identity fields;
    # description similarity alone never identifies an opportunity.
    def shingles(value):
        words = _description_words(value)
        return {tuple(words[i:i + 8]) for i in range(len(words) - 7)}

    def matches(first_text, second_text):
        first, second = shingles(first_text), shingles(second_text)
        size = min(len(first), len(second))
        return size >= 100 and len(first & second) / size >= 0.9

    if matches(left, right):
        return True
    # Observed employer/aggregator copies wrap the same duties in different
    # page chrome and company boilerplate. Compare the bounded role section
    # with the same evidence threshold, never the shared company introduction.
    def role_section(value):
        match = re.search(
            r"(?im)^[ \t#]*Role Overview[ \t:]*\r?$\n"
            r"(?P<body>[\s\S]+?)"
            r"^[ \t#]*Education Requirements[ \t:]*\r?$",
            value or "",
        )
        return match.group("body") if match else ""

    return matches(role_section(left), role_section(right))


def _declared_companies(text: str) -> set[str]:
    lines = [line.strip().lstrip("#").strip() for line in text.splitlines()]
    companies = set()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"Company\s*:\s*(.+)", line, re.I)
        if match:
            companies.add(normalized(match[1]))
        elif line.casefold().rstrip(":") == "company" and index + 1 < len(lines):
            companies.add(normalized(lines[index + 1]))
    return companies


def labeled_requisition(text: str) -> str | None:
    """A single explicit source label, not a number guessed from prose or a URL."""
    ids = {match.casefold() for match in re.findall(
        r"(?im)^(?:Req(?:uisition)?[ \t]+(?:ID|Number|#)|Job[ \t]+(?:ID|Requisition ID|Number))"
        r"[ \t]*[:#]?[ \t]*\n?([A-Za-z0-9_-]{3,40})[ \t]*$", text,
    ) if re.search(r"\d", match)}
    # Saved Autodesk renderings also flatten this explicit header and its body.
    ids.update(match.casefold() for match in re.findall(
        r"(?im)^Job Requisition ID[ \t]*#?[ \t]+([A-Za-z0-9_-]{3,40})(?=[ \t]+[A-Z][a-z])",
        text,
    ) if re.search(r"\d", match))
    return next(iter(ids)) if len(ids) == 1 else None


EA_EMPLOYER_NAMES = {"electronic arts", "electronic arts (ea)", "electronic arts inc. / ea"}


def _requisition_employer_key(company: str | None) -> tuple[str, ...]:
    if normalized(company) in EA_EMPLOYER_NAMES:
        return ("electronic", "arts")
    # Used only with a matching labeled requisition and verified role body.
    # Keep geographic and subsidiary names; normalize legal form and word order
    # (e.g. City of Example / Example, Corporation of the City of).
    legal_words = {"inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc", "lp", "of", "the"}
    return tuple(sorted(set(re.findall(r"[a-z0-9]+", normalized(company))) - legal_words))


def source_requisition(text: str, url: str | None, company: str | None) -> str | None:
    """Read source identity, independently of model-generated display titles."""
    if normalized(company) in EA_EMPLOYER_NAMES:
        ids = set(re.findall(r"\bRole\s+ID\s*:?\s*(\d{5,8})\s+Worker\s+Type\b", text))
        locator = authoritative_ingestion_identity(url)
        if locator and locator[0] == "jobs.ea.com":
            ids.add(locator[1])
        return next(iter(ids)) if len(ids) == 1 else None
    return labeled_requisition(text)


def verified_posting_text(job: JobPosting, db: Session, storage) -> str | None:
    if storage is None:
        return None
    artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
    if (artifact is None or artifact.job_id != job.id or artifact.deleted_at is not None
            or artifact.artifact_type != "job_snapshot"):
        return None
    try:
        content = storage.read_bytes(artifact.storage_uri)
        if len(content) != artifact.size_bytes or sha256(content).hexdigest() != artifact.sha256:
            return None
        return content.decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        return None


def _duties_section(text: str) -> str:
    match = re.search(
        r"(?im)^[ \t#]*(?:Job Responsibilities|Duties and Responsibilities|Primary Responsibilities|Responsibilities)[ \t:]*\r?$\n"
        r"(?P<body>[\s\S]+?)"
        r"^[ \t#]*(?:Requirements|Qualifications|Minimum Qualifications|Skills and Qualifications|Required Experience and Skills)"
        r"[ \t:]*\r?$", text,
    )
    return match["body"] if match else ""


def _same_duties(left: str, right: str) -> bool:
    # Source-corroborated requisitions in this audit have 70-150 words of duties.
    # Compare the complete duty section, so company/benefits boilerplate cannot
    # supply the match and a short common sentence is insufficient.
    first, second = (_description_words(_duties_section(text)) for text in (left, right))
    return len(first) >= 40 and first == second


def _ea_role_section(text: str) -> str:
    match = re.search(
        r"(?im)^Description & Requirements\s*$\n(?P<body>[\s\S]+?)"
        r"^(?:Pay Transparency - North America|About Electronic Arts)\s*$", text,
    )
    return match["body"] if match else ""


def _same_ea_workplace(left: str, right: str, city: str, other_city: str) -> bool:
    if not city or not other_city:
        return False
    if city == other_city:
        return True
    # The observed FC copy is advertised as Vancouver but specifies the Burnaby
    # studio in both originals. Require that shared workplace statement and both
    # city names in both sources; this is not a general metro-area equivalence.
    def workplaces(text):
        return set(re.findall(r"\b(?:studio|office) in ([a-z]+(?: [a-z]+)?) (?=\d)", text.casefold()))
    return bool(workplaces(left) & workplaces(right)) and all(
        re.search(rf"\b{re.escape(name)}\b", text.casefold())
        for name in (city, other_city) for text in (left, right)
    )


GOOGLE_EMPLOYER_NAMES = {"google", "google canada"}


def _google_eluta_sections(text: str) -> tuple | None:
    """Fingerprint the observed role sections, excluding search/page chrome.

    Google and Eluta use different footers. Require both section boundaries and
    the explicit complete workplace list; a summary or generic company text
    cannot establish identity. Qualifications distinguish BS, MS and PhD copies.
    """
    workplaces = re.findall(r"preferred working location from the following:[ \t]*([^\n]+)", text)
    body = re.search(
        r"(?im)^Minimum qualifications:[ \t]*\r?$\n(?P<body>[\s\S]+?)^Responsibilities[ \t]*\r?$\n"
        r"(?P<duties>[\s\S]+?)^(?:Report Job|Information collected and processed as part of your Google Careers profile)\b",
        text,
    )
    if len(workplaces) != 1 or not body:
        return None
    locations = tuple(sorted(normalized(value.rstrip(" .")) for value in workplaces[0].split(";")))
    role, duties = _description_words(body["body"]), _description_words(body["duties"])
    if (not all(locations) or len(role) < 150 or len(duties) < 40
            or not re.search(r"(?im)^Preferred qualifications:[ \t]*\r?$", body["body"])
            or not re.search(r"(?im)^About the job[ \t]*\r?$", body["body"])):
        return None
    return locations, role, duties


def _find_google_eluta_duplicate(request, parsed, db: Session, storage) -> JobPosting | None:
    incoming = authoritative_ingestion_identity(request.source_url)
    if (not incoming or incoming[0] not in {"google.com", "eluta"}
            or (getattr(request, "source_resolution", None) or {}).get("kind") == "manual_override"):
        return None
    evidence = _google_eluta_sections(request.raw_text)
    title = normalized(parsed.role.job_title)
    term = normalized(parsed.employment_timing.raw_label)
    if (not evidence or not title or not re.search(r"\b20\d{2}\b", term)
            or title not in {normalized(line) for line in request.raw_text.splitlines()}):
        return None
    matches = []
    for job in db.scalars(select(JobPosting).where(
        func.lower(func.trim(JobPosting.company_name)).in_(GOOGLE_EMPLOYER_NAMES),
    )):
        source = job.source or {}
        current = authoritative_ingestion_identity(job.source_url)
        if (job_is_merged_duplicate(job) or source.get("source_url_dedup_safe") is False
                or (source.get("source_resolution") or {}).get("kind") == "manual_override"
                or not current or {incoming[0], current[0]} != {"google.com", "eluta"}
                or title != normalized((job.role or {}).get("job_title"))
                or term != normalized((job.employment_timing or {}).get("raw_label"))):
            continue
        if any(getattr(parsed.employment_timing, field) and (job.employment_timing or {}).get(field)
               and getattr(parsed.employment_timing, field) != job.employment_timing[field]
               for field in ("start_date", "end_date", "duration_months", "duration_label")):
            continue
        locations = evidence[0]
        if any(normalized(country) != "canada" for country in
               (parsed.location.country, (job.location or {}).get("country"))):
            continue
        if any(city and not any(normalized(city) == place.split(",")[0] for place in locations)
               for city in (parsed.location.city, (job.location or {}).get("city"))):
            continue
        deadline = parsed.application.application_deadline
        other_deadline = (job.application or {}).get("application_deadline")
        if deadline and other_deadline and deadline != other_deadline:
            continue
        # Previously verified aliases must not let a second ID on either site
        # silently replace the first opening, even when the role text is reused.
        known_urls = [job.source_url, source.get("portal_source_url"),
                      *source.get("portal_source_urls", []),
                      *[item.get("source_url") for item in source.get("cross_source_matches", [])]]
        if any(known and known[0] == incoming[0] and known != incoming
               for known in (authoritative_ingestion_identity(url) for url in known_urls)):
            continue
        raw = verified_posting_text(job, db, storage)
        if (raw is not None and title in {normalized(line) for line in raw.splitlines()}
                and _google_eluta_sections(raw) == evidence):
            matches.append(job)
    return matches[0] if len(matches) == 1 else None


def find_source_evidence_duplicate(request, parsed, db: Session, storage) -> JobPosting | None:
    """Recover observed cross-board copies from checksum-verified source evidence.

    Explicit employer requisitions tolerate title extraction drift. Without an
    ID, EA's observed Eluta copy needs the same title, complete dated placement,
    city, complete duties and substantial matching role content. Google/Eluta
    copies require matching original qualifications, role, duties and workplaces.
    """
    if storage is None or not request_source_url_dedup_safe(request):
        return None
    incoming = ingestion_locator_identity(request.source_url)
    company = normalized(parsed.employer.company_name)
    if company in GOOGLE_EMPLOYER_NAMES:
        duplicate = _find_google_eluta_duplicate(request, parsed, db, storage)
        if duplicate:
            return duplicate
    ea = company in EA_EMPLOYER_NAMES
    requisition = source_requisition(request.raw_text, request.source_url, company)
    if not incoming or not company or (not requisition and not ea):
        return None
    employer_key = _requisition_employer_key(company)
    if not employer_key:
        return None
    matches = []
    for job in db.scalars(select(JobPosting).where(
        func.lower(JobPosting.company_name).contains(employer_key[0], autoescape=True),
    ).order_by(JobPosting.created_at, JobPosting.id)):
        source = job.source or {}
        if (_requisition_employer_key(job.company_name) != employer_key
                or job_is_merged_duplicate(job) or source.get("source_url_dedup_safe") is False
                or (source.get("source_resolution") or {}).get("kind") == "manual_override"):
            continue
        current = ingestion_locator_identity(job.source_url)
        if not current:
            continue
        # Different locators on the same official site may be separate openings.
        if incoming[0] == current[0] and incoming != current:
            continue
        if any(getattr(parsed.employment_timing, field) and (job.employment_timing or {}).get(field)
               and getattr(parsed.employment_timing, field) != job.employment_timing[field]
               for field in ("start_date", "end_date", "duration_months")):
            continue
        raw = verified_posting_text(job, db, storage)
        if raw is None:
            continue
        known_req = source_requisition(raw, job.source_url, job.company_name)
        if requisition and known_req:
            if requisition != known_req:
                continue
            title_matches = (
                normalized(parsed.role.normalized_title) == normalized(job.normalized_title)
                or normalized(parsed.role.job_title) == normalized((job.role or {}).get("job_title"))
            )
            # With a changed title, an unstructured body is insufficient. The
            # observed EA/Workday IDs or identical bounded duties must corroborate
            # that the difference comes from extraction rather than another role.
            same_duties = _same_duties(raw, request.raw_text)
            if not (title_matches or ea or same_duties
                    or workday_requisition_identity(request.source_url, request.raw_text)
                    or workday_requisition_identity(job.source_url, raw)):
                continue
            if (_matching_descriptions(raw, request.raw_text) or same_duties
                    or (ea and _matching_descriptions(_ea_role_section(raw), _ea_role_section(request.raw_text)))):
                matches.append((job, known_req))
            continue
        if not ea or incoming[0] == current[0]:
            continue
        if (not normalized(parsed.role.job_title)
                or normalized(parsed.role.job_title) != normalized((job.role or {}).get("job_title"))
                or not _same_ea_workplace(request.raw_text, raw, normalized(parsed.location.city),
                                          normalized((job.location or {}).get("city")))):
            continue
        if not all(getattr(parsed.employment_timing, field)
                   and getattr(parsed.employment_timing, field) == (job.employment_timing or {}).get(field)
                   for field in ("start_date", "end_date", "duration_months")):
            continue
        if (_same_duties(raw, request.raw_text)
                and _matching_descriptions(_ea_role_section(raw), _ea_role_section(request.raw_text))):
            matches.append((job, known_req))
    if len(matches) == 1:
        return matches[0][0]
    # Historical duplicates of one explicit requisition must not create a third.
    if matches and requisition and all(req == requisition for _, req in matches):
        return matches[0][0]
    return None


def find_body_requisition_duplicate(request, parsed, db: Session, storage) -> JobPosting | None:
    """Bridge cross-board copies using verified original evidence, not summaries.

    The audit's Skyworks copies shared a labeled requisition and long body, but
    one provider omitted the term and legal-name suffix. Missing metadata may
    differ; different populated dates, titles or official IDs still veto reuse.
    """
    if storage is None or not request_source_url_dedup_safe(request):
        return None
    requisition = labeled_requisition(request.raw_text)
    incoming = authoritative_ingestion_identity(request.source_url)
    if not requisition or not incoming:
        return None
    company = " ".join(re.findall(r"[a-z0-9]+", normalized(parsed.employer.company_name)))
    if not company:
        return None
    matches = []
    candidates = db.scalars(select(JobPosting).where(or_(
        JobPosting.normalized_title == parsed.role.normalized_title,
        func.lower(func.trim(JobPosting.role["job_title"].as_string())) == normalized(parsed.role.job_title),
    )))
    for job in candidates:
        source = job.source or {}
        if (job_is_merged_duplicate(job) or source.get("source_url_dedup_safe") is False
                or (source.get("source_resolution") or {}).get("kind") == "manual_override"):
            continue
        current = authoritative_ingestion_identity(job.source_url)
        if not current or current == incoming:
            continue
        # Do not override two different official requisitions on the same site.
        if incoming[0] == current[0] and incoming[0] not in {"linkedin", "indeed"}:
            continue
        other = " ".join(re.findall(r"[a-z0-9]+", normalized(job.company_name)))
        short, long = sorted((company, other), key=len)
        if not short or not (short == long or long.startswith(short + " ")):
            continue
        if any(getattr(parsed.employment_timing, field) and (job.employment_timing or {}).get(field)
               and getattr(parsed.employment_timing, field) != job.employment_timing[field]
               for field in ("start_date", "end_date", "duration_months")):
            continue
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        if (artifact is None or artifact.job_id != job.id or artifact.deleted_at is not None
                or artifact.artifact_type != "job_snapshot"):
            continue
        try:
            content = storage.read_bytes(artifact.storage_uri)
            if len(content) != artifact.size_bytes or sha256(content).hexdigest() != artifact.sha256:
                continue
            raw = content.decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            continue
        if labeled_requisition(raw) == requisition and _matching_descriptions(raw, request.raw_text):
            matches.append(job)
    return matches[0] if len(matches) == 1 else None


def same_opportunity_fields(parsed, job: JobPosting) -> bool:
    # Providers can canonicalize the same heading differently (engineer versus
    # engineering). An exact nonempty display title is also identity evidence;
    # never use fuzzy title similarity to collapse distinct specializations.
    display_title = normalized(parsed.role.job_title)
    if (normalized(parsed.role.normalized_title) != normalized(job.normalized_title)
            and not (display_title and display_title == normalized((job.role or {}).get("job_title")))):
        return False
    company = normalized(parsed.employer.company_name)
    other_company = normalized(job.company_name)
    new_description = parsed.role.description_clean or ""
    old_description = (job.role or {}).get("description_clean") or ""
    descriptions_match = None
    if company != other_company:
        # A brand/legal-name variation is accepted only when both descriptions
        # explicitly declare the longer name. Never strip legal/geographic
        # suffixes globally or equate different subsidiaries by a shared prefix.
        short, long = sorted((company, other_company), key=len)
        if (not short or not long.startswith(short + " ")
                or long not in (_declared_companies(new_description) & _declared_companies(old_description))):
            return False
        descriptions_match = _matching_descriptions(new_description, old_description)
        if not descriptions_match:
            return False
    location = parsed.location.model_dump()
    timing = parsed.employment_timing.model_dump()
    for field in ("city", "country"):
        value = normalized(location.get(field))
        if not value or value != normalized((job.location or {}).get(field)):
            return False
    province = normalized(location.get("province_state"))
    other_province = normalized((job.location or {}).get("province_state"))
    if normalized(location.get("country")) == "canada":
        regions = {**CANADIAN_PROVINCE_CODES, **CANADIAN_PROVINCE_ALIASES}
        province = normalized(regions.get(province.upper(), province))
        other_province = normalized(regions.get(other_province.upper(), other_province))
    if province and other_province and province != other_province:
        return False
    complete_timing = True
    for field in ("start_date", "end_date", "duration_months"):
        value = timing.get(field)
        if value != (job.employment_timing or {}).get(field):
            return False
        complete_timing = complete_timing and bool(value)
    if not complete_timing:
        # Partial dates still require the same dated term and a strong body
        # match. A shared numeric duration needs no redundant duration label;
        # alternative durations still need identical nonempty option labels.
        for field in ("raw_label", "duration_label"):
            value = normalized(timing.get(field))
            other_value = normalized((job.employment_timing or {}).get(field))
            if value != other_value:
                return False
            if not value and not (field == "duration_label" and (timing.get("duration_months") or 0) > 0):
                return False
        if not re.search(r"\b20\d{2}\b", timing.get("raw_label") or ""):
            return False
        if descriptions_match is None:
            descriptions_match = _matching_descriptions(new_description, old_description)
        if not descriptions_match:
            return False
    return True


def find_cross_source_duplicate(
    request, parsed, db: Session, *, exclude_job_id: str | None = None, storage=None,
) -> JobPosting | None:
    if not request.source_url or not request_source_url_dedup_safe(request):
        return None
    if not parsed.employer.company_name or not parsed.role.normalized_title:
        return None
    incoming_req = source_requisition(request.raw_text, request.source_url, parsed.employer.company_name)

    def conflicting_requisition(job):
        raw = verified_posting_text(job, db, storage)
        if raw is None:
            raw = (job.role or {}).get("description_raw") or ""
        known = source_requisition(raw, job.source_url, job.company_name)
        return incoming_req and known and incoming_req != known

    candidates = [job for job in db.scalars(select(JobPosting).where(
        or_(
            func.lower(func.trim(JobPosting.company_name)) == normalized(parsed.employer.company_name),
            JobPosting.normalized_title == parsed.role.normalized_title,
            func.lower(func.trim(JobPosting.role["job_title"].as_string())) == normalized(parsed.role.job_title),
        ),
        # Check both title representations below, including all matches in the
        # ambiguity check instead of preferring one representation's candidates.
    )) if job.id != exclude_job_id and not job_is_merged_duplicate(job)
        and (job.source or {}).get("source_url_dedup_safe") is not False
        and same_opportunity_fields(parsed, job) and not conflicting_requisition(job)]
    # Availability does not change identity: a stale cross-site listing can
    # arrive after the original closes. Reuse it without reopening the job.
    # Count all matching opportunities before checking the site. Two openings
    # with identical metadata are ambiguous, even if only one is cross-site.
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    if not candidate.source_url:
        return None
    incoming = capture_source_identity(request.source_url)
    if not incoming or not incoming.source_url_dedup_safe:
        return None
    known_urls = [candidate.source_url, *(candidate.source or {}).get("portal_source_urls", []), *[
        item.get("source_url", "") for item in (candidate.source or {}).get("cross_source_matches", [])
    ]]
    for url in known_urls:
        if posting_site(url) != posting_site(request.source_url):
            continue
        known = capture_source_identity(url)
        # Do not collapse different requisitions from the same site, including
        # sites already attached through an earlier cross-source match.
        if not known or known.identity_hash != incoming.identity_hash:
            return None
    if posting_site(candidate.source_url) == posting_site(request.source_url):
        return None
    return candidate


def fill_missing_posting_fields(job: JobPosting, parsed) -> list[str]:
    """Keep populated values; supplement only descriptive and requirement data."""
    changed = []
    new_text = parsed.role.description_clean or ""
    has_role_detail = bool(
        parsed.role.responsibilities or parsed.role.must_have_requirements
        or len(new_text.split()) >= 150
    )
    sections = {
        "employer": None, "role": None, "location": None, "employment_timing": None,
        "compensation": None, "eligibility": None,
        "application": {"application_deadline", "documents_required", "resume_required",
                        "cover_letter_required"},
    }
    for section, allowed in sections.items():
        # Listing headers and browser-shell skills are not role evidence.
        # Sparse sources may still supply location, timing and other metadata.
        if section == "role" and not has_role_detail:
            continue
        existing = dict(getattr(job, section) or {})
        incoming = getattr(parsed, section).model_dump()
        if section == "compensation" and any(
            value is not None and incoming.get(field) is not None
            and value != incoming[field] for field, value in existing.items()
        ):
            continue
        for field, value in incoming.items():
            if allowed is not None and field not in allowed:
                continue
            if field in {"description_raw", "description_clean"}:
                continue
            old = existing.get(field)
            empty = old is None or old == "" or old == "unknown" or old == []
            available = value is not None and value != "" and value != "unknown" and value != []
            if empty and available:
                existing[field] = value
                changed.append(f"{section}.{field}")
            elif section == "role" and isinstance(old, list) and isinstance(value, list):
                additions = [item for item in value if item not in old]
                if additions:
                    existing[field] = [*old, *additions]
                    changed.append(f"{section}.{field}")
        setattr(job, section, existing)
    job.application = ParsedApplication.model_validate(job.application).model_dump()
    # A title-only JavaScript shell is not a full description. Preserve reviewed
    # descriptions and the old snapshot; use a substantially fuller source body.
    role = dict(job.role or {})
    old_text = str(role.get("description_clean") or "")
    if (has_role_detail and not (job.data_quality or {}).get("manual_review_completed_at")
            and len(new_text.split()) >= 80
            and len(new_text.split()) > len(old_text.split()) * 1.25):
        for field in ("description_raw", "description_clean"):
            role[field] = getattr(parsed.role, field)
            changed.append(f"role.{field}")
        job.role = role
    job.application_deadline = (job.application or {}).get("application_deadline") or job.application_deadline
    if not job.role_type or job.role_type == "unknown":
        job.role_type = (job.role or {}).get("role_type") or job.role_type
    return changed
