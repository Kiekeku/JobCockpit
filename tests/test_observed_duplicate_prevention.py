"""October saved-source regressions; parsed fields are supplied, not parser expectations.

The real cases include EA Role ID headers and Eluta copies, Autodesk's two
career sites, and the Skyworks/Wawanesa/Mississauga responsibilities sections.
IDs and role prose below are minimized examples, never persisted private jobs.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from jobcockpit.db.models import JobPosting, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.services import workflows
from jobcockpit.storage.artifacts import ArtifactStorageService


DUTIES = (
    "Collaborate with designers and engineers to implement gameplay features. "
    "Develop tools that let artists inspect animation behavior and diagnose problems. "
    "Prototype interactive mechanics and improve existing systems through careful testing. "
    "Use a shared engine to build maintainable modules and review code with the team. "
    "Investigate performance regressions, document findings, and demonstrate working changes "
    "during development reviews. Improve debugging workflows and support designers as they "
    "refine player movement, physical interactions, and the behavior of computer opponents."
)
DETAILS = (
    "Applicants must return to their degree program after this internship and be available "
    "for the full placement. Coursework in software development and practical programming "
    "projects will help with this role. Work with an engineering mentor to learn the team's "
    "development process. Include project examples and explain your own contributions."
)


def posting(settings, url, *, company="Electronic Arts", role_id="900001", title="Gameplay Co-op"):
    text = (
        f"{title}\nCompany: {company}\nGeneral Information\nLocations: Vancouver, Canada\n"
        + (f"Role ID\n{role_id}\nWorker Type\nIntern\n" if role_id else "")
        + f"Description & Requirements\nGameplay team placement, Summer 2027.\n"
        f"Responsibilities:\n{DUTIES}\nRequirements:\n{DETAILS}\nAbout Electronic Arts\n"
    )
    request = SimpleNamespace(
        source_type="ats_csjobs", source_url=url, source_url_dedup_safe=True,
        source_archive=None, skip_language_filter=False, raw_text=text,
    )
    parsed = workflows.parse_job_text_with_settings("Gameplay Co-op\nCompany: Example", None, settings)
    parsed.employer.company_name = company
    parsed.role.job_title = title
    parsed.role.normalized_title = title.casefold().replace(" ", "_")
    parsed.role.description_raw = parsed.role.description_clean = text
    parsed.location.city = "Vancouver"
    parsed.location.country = "Canada"
    parsed.employment_timing.raw_label = "Summer 2027"
    parsed.employment_timing.start_date = "2027-05-01"
    parsed.employment_timing.end_date = "2027-12-31"
    parsed.employment_timing.duration_months = 8
    return request, parsed


@pytest.mark.parametrize("source_type", ["gmail_alert", "ats_csjobs", "ats_eluta", "paste", "browser_capture"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ea_role_id_survives_title_and_employer_variations(
    jobcockpit_settings_factory, allow_public_dns, source_type, reverse,
):
    allow_public_dns({"jobs.ea.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://jobs.ea.com/en_US/careers/JobDetail/Engineer/900001",
                     title="Electronic Arts"),
             posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef",
                     company="Electronic Arts (EA)", title="Gameplay Co-op - Summer 2027")]
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        request.source_type = source_type
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
    assert [r.created for r in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]


@pytest.mark.parametrize("conflict", [None, "different_req", "different_duties", "term", "unsafe", "manual", "corrupt"])
def test_ea_role_evidence_keeps_separate_or_unverifiable_openings(
    jobcockpit_settings_factory, allow_public_dns, conflict,
):
    allow_public_dns({"jobs.ea.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    first = posting(settings, "https://jobs.ea.com/en_US/careers/JobDetail/Engineer/900001",
                    title="Electronic Arts")
    second = posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef",
                     role_id="900002" if conflict == "different_req" else "900001")
    with session_scope(settings) as db:
        original = workflows.persist_job_ingest_result(*first[:1], db, storage, settings,
                                                       parsed_job=first[1], run_fit_analysis=False)
        job = db.get(JobPosting, original.payload["id"])
        if conflict == "manual":
            job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
        if conflict == "corrupt":
            artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
            artifact.sha256 = "0" * 64
    if conflict == "different_duties":
        second[0].raw_text = second[0].raw_text.replace(DUTIES, "Build unrelated database systems.")
    elif conflict == "term":
        second[1].employment_timing.start_date = "2028-05-01"
    elif conflict == "unsafe":
        second[0].source_url_dedup_safe = False
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(second[0], db, storage, settings,
                                                     parsed_job=second[1], run_fit_analysis=False)
        assert result.created == (conflict is not None)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("city_variant", [False, True])
def test_ea_eluta_copy_without_role_id_uses_verified_role_body(
    jobcockpit_settings_factory, allow_public_dns, reverse, city_variant,
):
    allow_public_dns({"www.eluta.ca", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef"),
             posting(settings, "https://www.eluta.ca/sandbox?destination=https%3A%2F%2Fwww.eluta.ca%2Fdirect%2Fi%3Fi%3D0123456789abcdef0123456789abcdef&imo=12",
                     company="Electronic Arts Inc. / EA", role_id=None)]
    # Observed page chrome differs while the duties and dated role remain the same.
    pairs[1][0].raw_text = "Report Job\n" * 60 + pairs[1][0].raw_text
    if city_variant:
        pairs[0][1].location.city = "Burnaby"
        for request, _ in pairs:
            request.raw_text = request.raw_text.replace(
                "Responsibilities:", "This team works onsite at our studio in Burnaby 3 days a week.\nResponsibilities:",
            )
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ))
    assert [r.created for r in results] == [True, False]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("conflict", [None, "duties", "term", "city", "role_id", "corrupt", "manual", "unsafe", "ambiguous"])
def test_ea_primary_responsibilities_copy_without_portal_requisition(
    jobcockpit_settings_factory, allow_public_dns, reverse, conflict,
):
    # Observed official EA / Gmail LinkedIn copies: only the official source
    # supplies a Role ID; both use Primary Responsibilities before Requirements.
    allow_public_dns({"jobs.ea.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://jobs.ea.com/en_US/careers/JobDetail/Engineer/900001"),
             posting(settings, "https://www.linkedin.com/jobs/view/123456789",
                     company="Electronic Arts (EA)", role_id=None)]
    pairs[0][0].source_type = "ats_eluta"
    pairs[1][0].source_type = "gmail_alert"
    for index, (request, parsed) in enumerate(pairs):
        request.raw_text = request.raw_text.replace("Responsibilities:", "Primary Responsibilities")
        if index:
            request.raw_text = request.raw_text.replace("Requirements:", "Requirements")
        parsed.role.description_raw = parsed.role.description_clean = request.raw_text
    if conflict == "duties":
        pairs[1][0].raw_text = pairs[1][0].raw_text.replace(DUTIES, "Build unrelated database systems.")
    elif conflict == "term":
        pairs[1][1].employment_timing.start_date = "2028-05-01"
    elif conflict == "city":
        pairs[1][1].location.city = "Toronto"
    elif conflict == "role_id":
        pairs[1][0].raw_text = "Role ID\n900002\nWorker Type\nIntern\n" + pairs[1][0].raw_text
    elif conflict == "unsafe":
        for request, _ in pairs:
            request.source_url_dedup_safe = False
    if reverse:
        pairs.reverse()
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(
            pairs[0][0], db, storage, settings, parsed_job=pairs[0][1], run_fit_analysis=False,
        )
        job = db.get(JobPosting, first.payload["id"])
        if conflict == "corrupt":
            db.get(StoredArtifact, job.raw_content_artifact_id).sha256 = "0" * 64
        elif conflict == "manual":
            job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
        elif conflict == "ambiguous":
            # Two historical copies of the same source cannot authorize a
            # no-requisition match to either candidate.
            clone = JobPosting(**{attr.key: getattr(job, attr.key)
                                  for attr in JobPosting.__mapper__.column_attrs
                                  if attr.key != "id"})
            clone.id = "job_historical_copy"
            artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
            copy = StoredArtifact(**{column.name: getattr(artifact, column.name)
                                     for column in StoredArtifact.__table__.columns
                                     if column.name not in {"artifact_id", "job_id"}})
            copy.artifact_id = "artifact_historical_copy"
            copy.job_id = clone.id
            clone.raw_content_artifact_id = copy.artifact_id
            clone.posting_snapshot_hash = "a" * 64
            db.add(copy)
            db.flush()
            db.add(clone)
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            pairs[1][0], db, storage, settings, parsed_job=pairs[1][1], run_fit_analysis=False,
        )
        assert result.created == (conflict is not None)
        if conflict is None:
            assert result.payload["id"] == first.payload["id"]


@pytest.mark.parametrize("case", ["same_autodesk_url", "autodesk_two_sites", "generic_requisition", "civic_company_alias"])
def test_observed_employer_id_and_body_survive_parser_differences(
    jobcockpit_settings_factory, allow_public_dns, case,
):
    allow_public_dns({"jobs.autodesk.com", "autodesk.wd1.myworkdayjobs.com",
                      "jobs.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    company = "Example Insurance" if case in {"generic_requisition", "civic_company_alias"} else "Autodesk"
    urls = ["https://jobs.autodesk.com/careers/job/123456789?source=Indeed"] * 2
    if case == "autodesk_two_sites":
        urls[0] = "https://autodesk.wd1.myworkdayjobs.com/Ext/job/Montreal/Intern_27WD123456-2"
    elif case in {"generic_requisition", "civic_company_alias"}:
        urls = ["https://jobs.example.com/job/Intern/123456/", "https://www.linkedin.com/jobs/view/123456789"]
    results = []
    for i, url in enumerate(urls):
        if case == "civic_company_alias":
            company = "City of Exampleville" if i else "Exampleville, Corporation of the City of"
        request, parsed = posting(settings, url, company=company, role_id=None,
                                  title="Software Intern" if i else "Intern Software Developer")
        request.source_type = "gmail_alert" if i else "ats_csjobs"
        if case == "same_autodesk_url":
            parsed.role.normalized_title = (
                "intern_software_developer_stagiaire_en_d_veloppement_logiciel" if i
                else "intern_software_developer_stagiaire_en_developpement_logiciel"
            )
        request.raw_text = f"Req ID: {'27WD123456' if case != 'generic_requisition' else '123456'}\n" + request.raw_text
        if case in {"generic_requisition", "civic_company_alias"}:
            request.raw_text += "\n" + " ".join(f"chrome{i}word{n}" for n in range(200))
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ))
    assert [r.created for r in results] == [True, False]
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 1


def test_different_ea_role_ids_veto_matching_titles_and_shared_boilerplate(
    jobcockpit_settings_factory, allow_public_dns,
):
    allow_public_dns({"jobs.ea.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://jobs.ea.com/en_US/careers/JobDetail/Engineer/900001"),
             posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef", role_id="900002")]
    for request, parsed in pairs:
        with session_scope(settings) as db:
            assert workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ).created


def test_parallel_ea_sources_recheck_verified_identity_under_writer(
    jobcockpit_settings_factory, allow_public_dns, monkeypatch,
):
    allow_public_dns({"jobs.ea.com", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    barrier = Barrier(2, timeout=10)
    write = storage.write_bytes

    def prepare(**kwargs):
        record = write(**kwargs)
        if kwargs["original_file_name"] == "job-posting.txt":
            barrier.wait()
        return record

    monkeypatch.setattr(storage, "write_bytes", prepare)
    pairs = [posting(settings, "https://jobs.ea.com/en_US/careers/JobDetail/Engineer/900001",
                     title="Electronic Arts"),
             posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef")]

    def ingest(pair):
        with session_scope(settings) as db:
            return workflows.persist_job_ingest_result(
                pair[0], db, storage, settings, parsed_job=pair[1], run_fit_analysis=False,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, pairs))
    assert sorted(r.created for r in results) == [False, True]
    assert len({r.payload["id"] for r in results}) == 1
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert db.scalar(select(func.count()).select_from(StoredArtifact)) == 2


@pytest.mark.parametrize("difference", ["duties", "city", "company", "term", "same_site"])
def test_ea_no_id_requires_corroboration_beyond_company_boilerplate(
    jobcockpit_settings_factory, allow_public_dns, difference,
):
    allow_public_dns({"www.eluta.ca", "ca.indeed.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    original = posting(settings, "https://ca.indeed.com/viewjob?jk=1234567890abcdef")
    request, parsed = posting(settings,
        "https://www.eluta.ca/spl/gameplay-0123456789abcdef0123456789abcdef?imo=12",
        company="Electronic Arts Inc. / EA", role_id=None)
    if difference == "duties":
        request.raw_text = request.raw_text.replace(DUTIES, "Build unrelated database systems.")
    elif difference == "city":
        parsed.location.city = "Toronto"
    elif difference == "company":
        parsed.employer.company_name = "Another Company"
    elif difference == "term":
        parsed.employment_timing.start_date = "2028-05-01"
    elif difference == "same_site":
        request.source_url = "https://ca.indeed.com/viewjob?jk=fedcba0987654321"
    for req, fields in [original, (request, parsed)]:
        with session_scope(settings) as db:
            assert workflows.persist_job_ingest_result(
                req, db, storage, settings, parsed_job=fields, run_fit_analysis=False,
            ).created


@pytest.mark.parametrize("url", [
    "https://jobs.ea.com.attacker.test/en_US/careers/JobDetail/Engineer/900001",
    "https://jobs.ea.com/en_US/careers/SearchJobs/900001",
    "https://jobs.autodesk.com/careers/job/no-number",
    "https://www.eluta.ca/sandbox?destination=https%3A%2F%2Fexample.com%2Fdirect%2Fi%3Fi%3D0123456789abcdef0123456789abcdef",
    "https://www.eluta.ca/direct/i?i=0123456789abcdef0123456789abcdef&i=fedcba9876543210fedcba9876543210",
    "https://www.eluta.ca/spl/gameplay-not-an-id",
    "https://www.google.com.attacker.test/about/careers/applications/jobs/results/123456789012345678",
    "https://www.google.com/about/careers/applications/jobs/results/not-an-id",
    "https://www.google.com/search/123456789012345678",
])
def test_observed_url_identities_reject_lookalikes_and_ambiguous_routes(url):
    from jobcockpit.ingestion.ingestion_identity import authoritative_ingestion_identity

    assert authoritative_ingestion_identity(url) is None


@pytest.mark.parametrize("urls", [
    ("https://jobs.autodesk.com/careers/job/123456789-intern-software-developer?domain=autodesk.com",
     "https://jobs.autodesk.com/careers/job/123456789?source=Indeed"),
    ("https://careers.siemens-healthineers.com/global/fr/job/R-12345/Software-Developer-Co-op",
     "https://careers.siemens-healthineers.com/global/en/job/R-12345"),
    ("https://stripe.com/careers/listing/data-analyst-intern/1234567?gh_jid=1234567",
     "https://stripe.com/careers/listing/data-analyst-intern/1234567"),
])
def test_additional_observed_url_variants_reuse_employer_id(
    jobcockpit_settings_factory, allow_public_dns, urls,
):
    from urllib.parse import urlsplit

    allow_public_dns({urlsplit(url).hostname for url in urls})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    for i, url in enumerate(urls):
        request, parsed = posting(settings, url, company="Example", role_id=None,
                                  title="Software Developer" if i else "Intern Developer")
        with session_scope(settings) as db:
            result = workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            )
            assert result.created == (i == 0)


@pytest.mark.parametrize("label", ["Job Requisition ID #\n27WD123456", "Job Requisition ID # 27WD123456 Position Overview"])
def test_observed_workday_requisition_header_survives_board_variants(
    jobcockpit_settings_factory, allow_public_dns, label,
):
    allow_public_dns("autodesk.wd1.myworkdayjobs.com")
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    for i, board in enumerate(["Ext", "uni"]):
        request, parsed = posting(settings,
            f"https://autodesk.wd1.myworkdayjobs.com/{board}/job/Montreal/Intern_27WD123456-{i+1}",
            company="Autodesk", role_id=None, title="Intern" if i else "Software Developer")
        request.raw_text = label + "\n" + request.raw_text
        with session_scope(settings) as db:
            result = workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            )
            assert result.created == (i == 0)


def test_observed_autodesk_duties_end_at_minimum_qualifications():
    from jobcockpit.services.job_deduplication import _same_duties

    body = f"Responsibilities\n{DUTIES}\nMinimum Qualifications\n{DETAILS}"
    assert _same_duties("Portal navigation\n" + body, body + "\nCompany footer")


def google_posting(settings, *, eluta=False, degree="PhD"):
    """Minimized Google/Eluta originals: headings, workplace list, partial timing.

    The observed originals share qualifications, role text and duties; one
    provider names Google Canada and leaves only Waterloo in the display city.
    """
    title = f"Software Developer Intern, {degree}, Summer 2027"
    url = ("https://www.eluta.ca/spl/software-intern-0123456789abcdef0123456789abcdef"
           if eluta else "https://www.google.com/about/careers/applications/jobs/results/123456789012345678-software-intern")
    request, parsed = posting(settings, url, company="Google Canada" if eluta else "Google",
                              role_id=None, title=title)
    text = (
        f"{title}\nApplications accepted until February 26, 2027.\n"
        "Note: By applying to this position you will have an opportunity to share your "
        "preferred working location from the following: Waterloo, ON, Canada; Toronto, ON, Canada.\n"
        f"Minimum qualifications:\nPursuing a {degree} program in software development.\n"
        f"Preferred qualifications:\n{DETAILS}\nAbout the job\n{DUTIES} {DETAILS}\n"
        f"Responsibilities\n{DUTIES}\n"
    )
    request.raw_text = text + ("Report Job\nApply Now\n" if eluta else
                              "Information collected and processed as part of your Google Careers profile.\n")
    parsed.role.description_raw = parsed.role.description_clean = request.raw_text
    parsed.location.city = "Waterloo" if eluta else None
    parsed.location.province_state = "Ontario"
    parsed.employment_timing.end_date = None
    parsed.employment_timing.duration_months = None
    parsed.employment_timing.duration_label = "12-14 week"
    return request, parsed


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("source_type", ["gmail_alert", "ats_csjobs", "ats_eluta", "paste", "browser_capture"])
def test_google_eluta_verified_sections_bridge_employer_and_missing_city(
    jobcockpit_settings_factory, allow_public_dns, reverse, source_type,
):
    allow_public_dns({"www.google.com", "www.eluta.ca"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [google_posting(settings), google_posting(settings, eluta=True)]
    official_url = pairs[0][0].source_url
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        request.source_type = source_type
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ))
    assert [r.created for r in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]
    with session_scope(settings) as db:
        job = db.get(JobPosting, results[0].payload["id"])
        assert job.source_url == official_url
        artifacts = list(db.scalars(select(StoredArtifact).where(StoredArtifact.job_id == job.id)))
        assert len(artifacts) == 2
        assert {storage.read_bytes(a.storage_uri).decode("utf-8") for a in artifacts} == {
            request.raw_text for request, _ in pairs
        }


@pytest.mark.parametrize("difference", [
    "qualifications", "duties", "workplaces", "degree", "term", "city", "company",
    "same_site", "unsafe", "manual", "incoming_manual", "corrupt", "summary_only", "ambiguous", "known_alias",
])
def test_google_eluta_requires_unique_verified_role_and_workplace_evidence(
    jobcockpit_settings_factory, allow_public_dns, difference,
):
    allow_public_dns({"www.google.com", "www.eluta.ca"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    first = google_posting(settings)
    second = google_posting(settings, eluta=True, degree="BS" if difference == "degree" else "PhD")
    if difference == "qualifications":
        second[0].raw_text = second[0].raw_text.replace("Pursuing a PhD", "Pursuing an MS")
    elif difference == "duties":
        second[0].raw_text = second[0].raw_text.replace(f"Responsibilities\n{DUTIES}", "Responsibilities\nBuild databases.")
    elif difference == "workplaces":
        second[0].raw_text = second[0].raw_text.replace("Waterloo, ON, Canada", "Montreal, QC, Canada")
    elif difference == "term":
        second[1].employment_timing.start_date = "2028-05-01"
    elif difference == "city":
        second[1].location.city = "Vancouver"
    elif difference == "company":
        second[1].employer.company_name = "Google Subsidiary"
    elif difference == "same_site":
        second[0].source_url = first[0].source_url.replace("123456789012345678", "223456789012345678")
    elif difference == "unsafe":
        second[0].source_url_dedup_safe = False
    elif difference == "incoming_manual":
        second[0].source_resolution = {"kind": "manual_override"}
    elif difference == "summary_only":
        first[0].raw_text = "JavaScript shell with no posting content"
    with session_scope(settings) as db:
        original = workflows.persist_job_ingest_result(
            first[0], db, storage, settings, parsed_job=first[1], run_fit_analysis=False,
        )
        job = db.get(JobPosting, original.payload["id"])
        if difference == "manual":
            job.source = {**job.source, "source_resolution": {"kind": "manual_override"}}
        elif difference == "corrupt":
            db.get(StoredArtifact, job.raw_content_artifact_id).sha256 = "0" * 64
        elif difference == "known_alias":
            job.source = {**job.source, "portal_source_urls": [
                second[0].source_url.replace("0123456789abcdef0123456789abcdef", "1123456789abcdef0123456789abcdef")
            ]}
    if difference == "ambiguous":
        other = google_posting(settings)
        other[0].source_url = other[0].source_url.replace("123456789012345678", "223456789012345678")
        with session_scope(settings) as db:
            assert workflows.persist_job_ingest_result(
                other[0], db, storage, settings, parsed_job=other[1], run_fit_analysis=False,
            ).created
    with session_scope(settings) as db:
        assert workflows.persist_job_ingest_result(
            second[0], db, storage, settings, parsed_job=second[1], run_fit_analysis=False,
        ).created


def test_google_numeric_posting_id_survives_optional_slug(jobcockpit_settings_factory, allow_public_dns):
    allow_public_dns("www.google.com")
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    for i in range(2):
        request, parsed = google_posting(settings)
        if i:
            request.source_url = request.source_url.rsplit("/", 1)[0] + "/123456789012345678"
            request.raw_text = "Changed rendering\n" + request.raw_text
            parsed.role.job_title = "Intern Developer"
        with session_scope(settings) as db:
            result = workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            )
            assert result.created == (i == 0)


def test_parallel_google_eluta_sources_recheck_evidence_under_writer(
    jobcockpit_settings_factory, allow_public_dns, monkeypatch,
):
    allow_public_dns({"www.google.com", "www.eluta.ca"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    barrier = Barrier(2, timeout=10)
    write = storage.write_bytes

    def prepare(**kwargs):
        record = write(**kwargs)
        if kwargs["original_file_name"] == "job-posting.txt":
            barrier.wait()
        return record

    monkeypatch.setattr(storage, "write_bytes", prepare)

    def ingest(pair):
        with session_scope(settings) as db:
            return workflows.persist_job_ingest_result(
                pair[0], db, storage, settings, parsed_job=pair[1], run_fit_analysis=False,
            )

    pairs = [google_posting(settings), google_posting(settings, eluta=True)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, pairs))
    assert sorted(r.created for r in results) == [False, True]
    assert len({r.payload["id"] for r in results}) == 1
    with session_scope(settings) as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert db.scalar(select(func.count()).select_from(StoredArtifact)) == 2


def test_google_section_match_preserves_existing_explicit_requisition_fallback(
    jobcockpit_settings_factory, allow_public_dns,
):
    allow_public_dns({"www.google.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings,
                     "https://www.google.com/about/careers/applications/jobs/results/123456789012345678",
                     company="Google", role_id=None, title="Intern Developer"),
             posting(settings, "https://www.linkedin.com/jobs/view/123456789",
                     company="Google", role_id=None, title="Software Intern")]
    for i, (request, parsed) in enumerate(pairs):
        request.raw_text = "Req ID: 123456789012345678\n" + request.raw_text
        with session_scope(settings) as db:
            result = workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            )
            assert result.created == (i == 0)
