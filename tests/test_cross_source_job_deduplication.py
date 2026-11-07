from types import SimpleNamespace

import pytest
from sqlalchemy import select

from jobcockpit.db.models import JobPosting, JobReprocessRun, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.services import workflows
from jobcockpit.services.job_deduplication import _matching_descriptions
from jobcockpit.storage.artifacts import ArtifactStorageService


# Minimized from the saved Physical Mixed Layout employer/Eluta copies.
# Keep role-specific duties separate from the differing site boilerplate.
LAYOUT_DUTIES = (
    "As a layout engineering intern, work alongside the design team on complex integrated "
    "circuits from concept through production. Collaborate with senior engineers to create "
    "and optimize physical mixed signal layouts. Assist with floorplanning, placement and "
    "routing of analog and digital blocks. Use electronic design automation tools for layout "
    "design and verification. Participate in design reviews and provide input on layout "
    "decisions. Work with cross functional teams to meet performance, power and area "
    "requirements. Troubleshoot and resolve layout issues during implementation.\n"
    "Minimum Qualifications:\n"
    "Understand semiconductor fundamentals and integrated circuit design principles. "
    "Be familiar with layout design tools and scripting languages. Bring problem solving "
    "skills and attention to detail.\nPreferred Qualifications:\n"
    "Circuit board and programmable logic experience, plus coursework in complementary "
    "metal oxide semiconductor design, will support the practical work of this team.\n"
)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("site_chrome", [False, True])
@pytest.mark.parametrize("conflict", [None, "term", "duration", "duration_options", "same_site", "ambiguous"])
def test_observed_single_duration_role_reuses_cross_site_copy(
    jobcockpit_settings_factory, allow_public_dns, reverse, site_chrome, conflict,
):
    allow_public_dns({"careers.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://careers.example.com/jobs/456", province="ON"),
             posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario")]
    for index, (request, parsed) in enumerate(pairs):
        parsed.employer.company_name = "Example Computing Canada ULC" if index else "Example Computing"
        parsed.role.job_title = "Physical Layout Internship (16 months)"
        parsed.role.normalized_title = "physical_layout_internship_16_months"
        chrome = (" ".join(f"portal{index}word{n}" for n in range(200)) if site_chrome else "")
        parsed.role.description_clean = (
            f"{chrome}\nCompany: Example Computing Canada ULC\nRole Overview\n"
            + LAYOUT_DUTIES + "Education Requirements:\nStudying engineering.\n" + chrome
        )
        parsed.employment_timing.start_date = None
        parsed.employment_timing.end_date = None
        parsed.employment_timing.duration_months = 16
        parsed.employment_timing.duration_label = None
        parsed.employment_timing.raw_label = "Summer 2027"
        request.raw_text = parsed.role.description_clean
    if conflict == "term":
        pairs[1][1].employment_timing.raw_label = "Summer 2028"
    elif conflict == "duration":
        pairs[1][1].employment_timing.duration_months = 12
    elif conflict == "duration_options":
        pairs[1][1].employment_timing.duration_label = "12 or 16 months"
    elif conflict == "same_site":
        pairs[0][0].source_url = "https://www.linkedin.com/jobs/view/999999"
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
            if conflict == "ambiguous" and len(results) == 1:
                original = db.get(JobPosting, results[0].payload["id"])
                db.add(JobPosting(
                    id="other", source_type="url", source_url="https://other.example.com/jobs/789",
                    raw_content_artifact_id=original.raw_content_artifact_id,
                    posting_snapshot_hash="other", company_name=original.company_name,
                    normalized_title=original.normalized_title, role=original.role,
                    location=original.location, employment_timing=original.employment_timing,
                ))
    assert [r.created for r in results] == [True, conflict is not None]
    if conflict is None:
        assert results[0].payload["id"] == results[1].payload["id"]


def test_role_section_matching_does_not_use_shared_company_boilerplate():
    common = " ".join(f"companyword{n}" for n in range(120))
    left = common + "\nRole Overview\n" + LAYOUT_DUTIES + "Education Requirements:\n" + common
    right = common + "\nRole Overview\n" + " ".join(f"otherduty{n}" for n in range(200))
    right += "\nEducation Requirements:\n" + common
    assert not _matching_descriptions(left, right)


@pytest.mark.parametrize("refresh_succeeds", [False, True])
def test_full_reprocess_replays_supplement_and_preserves_provenance(
    jobcockpit_settings_factory, monkeypatch, refresh_succeeds
):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    original_request, original = posting(settings, "https://careers.example.com/jobs/456")
    request, supplement = posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario")
    request.raw_text += "\nResponsibilities:\n" + "Build data models and explain experiments to engineering teams. " * 20
    supplement.role.responsibilities = ["Build data models"]
    supplement.compensation.pay_min = 45
    monkeypatch.setattr(workflows, "enforce_url_policy", lambda *args: None)
    monkeypatch.setattr(workflows, "parse_job_text_with_settings", lambda text, *args, **kwargs:
                        (supplement if text == request.raw_text else original).model_copy(deep=True))
    fitted = []
    monkeypatch.setattr(workflows, "enrich_parsed_job_fit_analysis",
                        lambda parsed, **kwargs: fitted.append(parsed.compensation.pay_min))

    def refresh(job, raw_text, settings):
        if not refresh_succeeds:
            return None
        return SimpleNamespace(ingestion_status="imported", detected_status=None,
                               ingest_request=original_request,
                               source_resolution=None, payload=None,
                               resolved_url=original_request.source_url, error_message="")

    monkeypatch.setattr(workflows, "prepare_reprocess_source", refresh)
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            original_request, db, storage, settings, parsed_job=original,
            run_fit_analysis=False,
        )
        job = db.get(JobPosting, result.payload["id"])
        workflows.enrich_cross_source_duplicate(job, request, supplement, db, storage, queue_fit=False)
        db.flush()
        observation = job.source["cross_source_matches"][0].copy()
        for _ in range(2):
            workflows.execute_job_reprocess(job, db, storage, settings)
            assert job.compensation["pay_min"] == 45
            assert job.location["province_state"] == "Ontario"
            assert "explain experiments" in job.role["description_clean"]
            assert job.source["cross_source_matches"] == [observation]
            assert request.source_url in job.source["portal_source_urls"]
            assert fitted[-1] == 45
        workflows.enrich_cross_source_duplicate(job, request, supplement, db, storage, queue_fit=False)
        assert len(job.source["cross_source_matches"]) == 1
        assert len(db.scalars(select(StoredArtifact).where(
            StoredArtifact.original_file_name == "supplemental-posting.txt")).all()) == 1


def posting(settings, url, *, province=None, city="Toronto", start="2027-05-01"):
    # Minimized ingestion contract from the reported CS Jobs / Gmail pair.
    # Parsed fields are supplied explicitly: this test does not change the parser.
    request = SimpleNamespace(
        source_type="ats_csjobs", source_url=url, source_url_dedup_safe=True,
        skip_language_filter=False, source_archive=None,
        raw_text=f"Data Science Intern, Algorithms (Summer 2027)\nCompany: Example\n{url}",
    )
    parsed = workflows.parse_job_text_with_settings(request.raw_text, url, settings)
    parsed.employer.company_name = "Example"
    parsed.role.normalized_title = "data_science_intern_algorithms_summer_2027"
    parsed.location.city = city
    parsed.location.country = "Canada"
    parsed.location.province_state = province
    parsed.employment_timing.start_date = start
    parsed.employment_timing.end_date = "2027-08-31"
    parsed.employment_timing.duration_months = 4
    return request, parsed


@pytest.mark.parametrize("conflict", [None, "other_id", "weak_url", "company", "title", "term", "unsafe", "official_url", "ambiguous", "stale_alias"])
def test_observed_resolved_portal_identity_survives_legal_name_and_summary_changes(
    jobcockpit_settings_factory, allow_public_dns, conflict,
):
    # Observed CS Jobs LinkedIn fallback followed by Gmail resolving that exact
    # LinkedIn ID to the employer page. Provider summaries omit legal names and dates.
    allow_public_dns({"careers.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    portal = "https://www.linkedin.com/jobs/view/123456"
    if conflict == "weak_url":
        portal = "https://www.linkedin.com/jobs/search"
    original_request, original = posting(settings, portal)
    request, incoming = posting(settings, "https://careers.example.com/jobs/456")
    request.source_type = "gmail_alert"
    request.portal_source_url = portal
    request.source_resolution = {"kind": "official_discovery", "official_url": request.source_url}
    incoming.employer.company_name = "Example International LP"
    for parsed in (original, incoming):
        parsed.employment_timing.end_date = None
        parsed.employment_timing.duration_months = None
        parsed.employment_timing.duration_label = None
        parsed.employment_timing.raw_label = "Summer 2027"
        parsed.role.description_clean = "A provider summary without the legal company name."
    if conflict == "other_id":
        request.portal_source_url = portal.replace("123456", "999999")
    elif conflict == "company":
        incoming.employer.company_name = "Unrelated Employer"
    elif conflict == "title":
        incoming.role.normalized_title = "different_role"
        incoming.role.job_title = "Different Role"
    elif conflict == "term":
        incoming.employment_timing.start_date = "2028-05-01"
    elif conflict == "unsafe":
        request.source_url_dedup_safe = False
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(
            original_request, db, storage, settings, run_fit_analysis=False, parsed_job=original,
        )
        job = db.get(JobPosting, first.payload["id"])
        if conflict == "official_url":
            job.source = {**job.source, "portal_source_urls": [portal]}
            job.source_url = "https://careers.example.com/jobs/other"
        elif conflict == "stale_alias":
            job.source = {**job.source, "portal_source_urls": [portal]}
            job.source_url = portal.replace("123456", "999999")
        elif conflict == "ambiguous":
            db.add(JobPosting(
                id="other", source_type=job.source_type, source_url=portal,
                raw_content_artifact_id=job.raw_content_artifact_id,
                posting_snapshot_hash="other", company_name=job.company_name,
                normalized_title=job.normalized_title, role=job.role,
                location=job.location, employment_timing=job.employment_timing,
            ))
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            request, db, storage, settings, run_fit_analysis=False, parsed_job=incoming,
        )
        assert result.created is (conflict is not None)
        if conflict is None:
            assert result.payload["id"] == first.payload["id"]
            assert result.payload["source"]["source_url"] == request.source_url
            assert portal in result.payload["source"]["portal_source_urls"]
            assert len(result.payload["source"]["cross_source_matches"]) == 1
    if conflict is None:
        # A later import of the original aggregator still reuses the promoted job.
        original_request.raw_text += "\nRefetched listing"
        with session_scope(settings) as db:
            again = workflows.persist_job_ingest_result(
                original_request, db, storage, settings, run_fit_analysis=False, parsed_job=original,
            )
            assert not again.created
            assert again.payload["id"] == first.payload["id"]


@pytest.mark.parametrize("reverse", [False, True])
def test_resolved_email_and_portal_ingestion_reuse_identity_in_both_orders(
    jobcockpit_settings_factory, allow_public_dns, reverse,
):
    from jobcockpit.ingestion.posting_links import PostingLinkContext, prepare_posting_link

    allow_public_dns({"careers.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    portal = "https://www.linkedin.com/jobs/view/123456"
    official = "https://careers.example.com/jobs/456"
    first_request, first_parsed = posting(settings, portal)
    _, official_parsed = posting(settings, official)
    official_parsed.employer.company_name = "Example International LP"
    prepared = prepare_posting_link(
        PostingLinkContext(
            source_type="gmail_alert", source_url=portal, raw_text=first_request.raw_text,
            link_text="Data Science Intern, Algorithms (Summer 2027)",
        ),
        settings=settings,
        posting_resolution={"status": "resolved", "posting": {
            "source_url": official, "raw_text": "Data Science Intern\nCompany: Example International LP",
            "confidence": 0.95, "evidence": "Matched official posting",
        }},
    )
    assert prepared.ingest_request is not None
    pairs = [(first_request, first_parsed), (prepared.ingest_request, official_parsed)]
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
            ))
    assert [result.created for result in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]
    assert results[1].payload["source"]["source_url"] == official
    assert portal in results[1].payload["source"]["portal_source_urls"]


@pytest.mark.parametrize("legacy", [False, True])
def test_workday_requisition_reuses_across_boards_and_parser_title_changes(
    jobcockpit_settings_factory, allow_public_dns, legacy,
):
    # Observed BMO Campus / Privileged / External copies carry the same Req ID
    # while the URL suffix and provider-generated title vary.
    allow_public_dns({"example.wd3.myworkdayjobs.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    results = []
    for index, board in enumerate(("Campus", "Privileged", "External")):
        suffix = "" if index == 0 else f"-{index}"
        url = f"https://example.wd3.myworkdayjobs.com/{board}/job/Toronto/Intern_R12345{suffix}"
        request, parsed = posting(settings, url)
        request.raw_text = f"Intern\nReq ID: R12345\nLocation: Toronto\nBoard: {board}"
        parsed.role.normalized_title = f"provider_title_{index}"
        parsed.role.description_raw = "Provider paraphrase omits the requisition header."
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
            if index == 0 and legacy:
                job = db.get(JobPosting, results[0].payload["id"])
                job.source = {k: v for k, v in job.source.items() if k != "workday_requisition"}
                job.role = {**job.role, "description_raw": request.raw_text}
    assert [r.created for r in results] == [True, False, False]
    assert len({r.payload["id"] for r in results}) == 1
    with session_scope(settings) as db:
        job = db.scalars(select(JobPosting)).one()
        assert len(job.source["cross_source_matches"]) == 2


def test_workday_requisition_requires_explicit_matching_evidence_and_tenant():
    from jobcockpit.services.job_deduplication import workday_requisition_identity

    url = "https://example.wd3.myworkdayjobs.com/External/job/Toronto/Intern_R12345-3"
    identity = workday_requisition_identity(url, "Intern\nReq ID: R12345")
    assert identity is not None
    assert workday_requisition_identity(url, "Intern without a requisition") is None
    assert workday_requisition_identity(url, "Req ID: R99999") is None
    assert workday_requisition_identity(url, "Req ID: R12345\nReq ID: R99999") is None
    assert workday_requisition_identity(url.replace("example.", "another."), "Req ID: R12345") != identity
    assert workday_requisition_identity(url, "Req ID: R12345-3") != identity


@pytest.mark.parametrize("reverse", [False, True])
def test_matching_display_titles_reuse_job_despite_provider_normalization(
    jobcockpit_settings_factory, allow_public_dns, reverse,
):
    # Observed employer/Gmail copies have identical headings, but provider
    # success produces engineer while timeout fallback produces engineering.
    allow_public_dns({"careers.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://careers.example.com/jobs/456"),
             posting(settings, "https://www.linkedin.com/jobs/view/123456")]
    for (request, parsed), title in zip(pairs, ("software_engineer_intern", "software_engineering_intern")):
        request.raw_text = f"Software Engineering Intern\nCompany: Example\n{request.source_url}"
        parsed.role.job_title = "Software Engineering Intern"
        parsed.role.normalized_title = title
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
    assert [result.created for result in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]
    with session_scope(settings) as db:
        job = db.scalars(select(JobPosting)).one()
        assert len(job.source["cross_source_matches"]) == 1


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("difference", [
    None, "company", "province", "term", "duration", "body", "date",
    "undeclared_employer", "missing_term", "same_site",
])
def test_source_corroborated_employer_and_alternative_term_match(
    jobcockpit_settings_factory, allow_public_dns, reverse, difference,
):
    # Minimized from the reported employer/LinkedIn firmware internship pair.
    # Supply parsed fields to test ingestion identity, not extraction behavior.
    body = (
        "Company\nExample Computing Canada ULC\nRole Overview\n"
        "The low power audio and sensors subsystem enables devices to hear, see, sense and "
        "interact with the world through an intelligent hardware ecosystem. As an intern "
        "you will work alongside design, verification, software engineering and architecture "
        "teams, writing reference firmware for next generation hardware and validating audio "
        "interfaces and accelerators. Gain hands on experience writing custom firmware on "
        "emulation environments and development hardware, stepping through and debugging code "
        "on bare metal platforms. Contribute to verification during the pre and post silicon "
        "phases through FPGA emulation. Develop high bandwidth performance use cases and measure "
        "system performance. Participate in debug activities throughout the development cycle. "
        "Use C, C++ and Python to enhance existing models. Develop interfaces to interact with "
        "real hardware. Work with diagnostic equipment including protocol analyzers, high speed "
        "scopes, logic analyzers and debuggers.\n"
        "For 16-month internships: available May/June 2027 - August/September 2028.\n"
        "For 12-month internships: available May/June 2027 - May/June 2028."
    )
    allow_public_dns({"careers.example.com", "www.linkedin.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://careers.example.com/jobs/456", province="ON"),
             posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario")]
    for index, (request, parsed) in enumerate(pairs):
        parsed.employer.company_name = "Example Computing Canada ULC" if index == 0 else "Example Computing"
        parsed.role.job_title = "Firmware Internship (12 or 16 months)"
        parsed.role.normalized_title = "firmware_internship_12_or_16_months"
        parsed.role.description_clean = body
        request.raw_text = f"Title: {parsed.role.job_title}\nURL: {request.source_url}\n{body}"
        parsed.employment_timing.start_date = None
        parsed.employment_timing.end_date = None
        parsed.employment_timing.duration_months = None
        parsed.employment_timing.raw_label = "Summer 2027"
        parsed.employment_timing.duration_label = "12 or 16 months"
    incoming = pairs[1][1]
    if difference == "company":
        incoming.employer.company_name = "Unrelated Computing"
    elif difference == "province":
        incoming.location.province_state = "Alberta"
    elif difference == "term":
        incoming.employment_timing.raw_label = "Summer 2028"
    elif difference == "duration":
        incoming.employment_timing.duration_label = "4 or 8 months"
    elif difference == "body":
        incoming.role.description_clean = "Company\nExample Computing Canada ULC\nA different engineering role."
    elif difference == "date":
        incoming.employment_timing.start_date = "2027-05-01"
    elif difference == "undeclared_employer":
        incoming.role.description_clean = body.replace("Company\nExample Computing Canada ULC\n", "")
    elif difference == "missing_term":
        incoming.employment_timing.raw_label = None
    elif difference == "same_site":
        pairs[0][0].source_url = "https://www.linkedin.com/jobs/view/999999"
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
    assert [result.created for result in results] == [True, difference is not None]


@pytest.mark.parametrize("damage", ["missing", "foreign_owner", "checksum"])
def test_unavailable_supplement_rolls_back_full_reprocess(
    jobcockpit_settings_factory, monkeypatch, damage
):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://careers.example.com/jobs/456")
    supplement_request, supplement = posting(settings, "https://www.linkedin.com/jobs/view/123456")
    supplement.compensation.pay_min = 45
    monkeypatch.setattr(workflows, "enforce_url_policy", lambda *args: None)
    monkeypatch.setattr(workflows, "parse_job_text_with_settings",
                        lambda *args, **kwargs: parsed.model_copy(deep=True))
    monkeypatch.setattr(workflows, "prepare_reprocess_source", lambda *args: None)
    monkeypatch.setattr(workflows, "enrich_parsed_job_fit_analysis", lambda *args, **kwargs: None)
    with session_scope(settings) as db:
        result = workflows.persist_job_ingest_result(
            request, db, storage, settings, parsed_job=parsed, run_fit_analysis=False,
        )
        job_id = result.payload["id"]
        job = db.get(JobPosting, job_id)
        workflows.enrich_cross_source_duplicate(job, supplement_request, supplement, db, storage,
                                               queue_fit=False)
        db.flush()
        artifact = db.get(StoredArtifact, job.source["cross_source_matches"][0]["artifact_id"])
        if damage == "foreign_owner":
            artifact.job_id = "another_job"
        elif damage == "checksum":
            artifact.sha256 = "0" * 64
        else:
            storage.delete(artifact.storage_uri)
    with pytest.raises(workflows.JobReprocessProcessingError, match="Supplemental posting snapshot"):
        with session_scope(settings) as db:
            workflows.execute_job_reprocess(db.get(JobPosting, job_id), db, storage, settings)
    with session_scope(settings) as db:
        assert db.get(JobPosting, job_id).compensation["pay_min"] == 45


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("existing_status", ["active", "closed"])
def test_unique_cross_site_match_reuses_job_and_preserves_provenance(
    jobcockpit_settings_factory, allow_public_dns, reverse, existing_status
):
    allow_public_dns({"www.linkedin.com", "careers.example.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    pairs = [posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario"),
             posting(settings, "https://careers.example.com/jobs/456")]
    # The observed employer page was a JavaScript shell, not evidence that the
    # data-science internship required JavaScript.
    pairs[1][0].raw_text += "\nYou need to enable JavaScript to run this app."
    pairs[1][1].role.tech_stack = ["JavaScript"]
    if reverse:
        pairs.reverse()
    results = []
    for request, parsed in pairs:
        with session_scope(settings) as db:
            results.append(workflows.persist_job_ingest_result(
                request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
            ))
            if len(results) == 1:
                # The reported Robinhood posting was closed before its LinkedIn
                # copy arrived; availability must not change posting identity.
                db.get(JobPosting, results[0].payload["id"]).status = existing_status
    assert [result.created for result in results] == [True, False]
    assert results[0].payload["id"] == results[1].payload["id"]
    with session_scope(settings) as db:
        job = db.scalars(select(JobPosting)).one()
        assert job.status == existing_status
        assert pairs[1][0].source_url in job.source["portal_source_urls"]
        observation = job.source["cross_source_matches"][0]
        assert observation["source_url"] == pairs[1][0].source_url
        assert observation["posting_snapshot_hash"] == workflows.job_posting_snapshot_hash(pairs[1][0])
        assert job.source_url == "https://careers.example.com/jobs/456"
        if not reverse:
            assert "JavaScript" not in job.role["tech_stack"]
        other_request, other_parsed = posting(settings, "https://www.linkedin.com/jobs/view/999999")
        assert workflows.find_duplicate_job_posting(other_request, db, "other", other_parsed) == (None, None)


@pytest.mark.parametrize("deferred", [False, True])
def test_cross_source_enrichment_fills_gaps_keeps_values_and_saves_evidence(
    jobcockpit_settings_factory, allow_public_dns, deferred
):
    allow_public_dns({"www.linkedin.com", "careers.example.com"})
    settings = jobcockpit_settings_factory()
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    request, parsed = posting(settings, "https://careers.example.com/jobs/456")
    parsed.compensation.pay_currency = "CAD"
    with session_scope(settings) as db:
        first = workflows.persist_job_ingest_result(
            request, db, storage, settings, run_fit_analysis=False, parsed_job=parsed,
        )
    request, parsed = posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario")
    request.raw_text += "\nResponsibilities:\n" + "Build data models and explain experiments to engineering teams. " * 20
    parsed.role.responsibilities = ["Build data models"]
    parsed.compensation.pay_min = 45
    parsed.compensation.pay_currency = "CAD"
    parsed.application.cover_letter_required = True
    with session_scope(settings) as db:
        settings.fit_analysis_provider = "codex_cli" if deferred else "heuristic"
        result = workflows.persist_job_ingest_result(
            request, db, storage, settings, parsed_job=parsed, run_fit_analysis=not deferred,
        )
        assert not result.created
        job = db.get(JobPosting, first.payload["id"])
        assert job.location["province_state"] == "Ontario"
        assert job.compensation["pay_min"] == 45
        assert job.compensation["pay_currency"] == "CAD"
        assert job.role["responsibilities"] == ["Build data models"]
        assert "explain experiments" in job.role["description_clean"]
        assert job.application["cover_letter_required"] is True
        assert "cover_letter" in job.application["documents_required"]
        assert len(db.scalars(select(StoredArtifact).where(StoredArtifact.job_id == job.id)).all()) == 2
        run = db.scalars(select(JobReprocessRun).where(JobReprocessRun.job_id == job.id)).one()
        assert run.run_type == "fit_analysis"
        # Same observation is idempotent, and a poorer source cannot erase fields.
        workflows.enrich_cross_source_duplicate(job, request, parsed, db, storage)
        assert len(job.source["cross_source_matches"]) == 1
        parsed.compensation.pay_currency = "USD"
        parsed.compensation.pay_max = 100
        parsed.location.province_state = "Other"
        job.data_quality = {"manual_review_completed_at": "2026-09-11"}
        from jobcockpit.services.job_deduplication import fill_missing_posting_fields
        before = job.role["description_clean"]
        parsed.role.description_clean = "longer " * 1000
        fill_missing_posting_fields(job, parsed)
        assert job.role["description_clean"] == before
        assert job.location["province_state"] == "Ontario"
        assert job.compensation["pay_currency"] == "CAD"
        assert job.compensation["pay_max"] is None


@pytest.mark.parametrize("difference", [
    "city", "province", "term", "missing_term", "duration", "title", "company",
    "same_site", "unsafe_url", "ambiguous", "mixed_title_ambiguity", "merged",
])
def test_cross_site_matching_keeps_distinct_or_uncertain_jobs_separate(
    jobcockpit_settings_factory, difference
):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    request, parsed = posting(settings, "https://www.linkedin.com/jobs/view/123456", province="Ontario")
    with session_scope(settings) as db:
        artifact = ArtifactStorageService(settings.artifact_root).write_bytes(
            artifact_type="job_snapshot", content=b"original", original_file_name="posting.txt",
            content_type="text/plain", job_id="existing", candidate_id="default",
            privacy_class="private",
        )
        db.add(StoredArtifact.from_record(artifact))
        db.flush()
        job = JobPosting(
            id="existing", source_type="ats_eluta", source_url="https://careers.example.com/jobs/456",
            posting_snapshot_hash="existing", company_name=parsed.employer.company_name,
            normalized_title=parsed.role.normalized_title, location=parsed.location.model_dump(),
            employment_timing=parsed.employment_timing.model_dump(), source={},
            raw_content_artifact_id=artifact.artifact_id,
        )
        db.add(job)
        db.flush()
        if difference == "city":
            parsed.location.city = "Calgary"
        elif difference == "province":
            parsed.location.province_state = "Alberta"
        elif difference == "term":
            parsed.employment_timing.start_date = "2028-05-01"
        elif difference == "missing_term":
            parsed.employment_timing.start_date = None
        elif difference == "duration":
            parsed.employment_timing.duration_months = 16
        elif difference == "title":
            parsed.role.normalized_title += "_machine_learning"
        elif difference == "company":
            parsed.employer.company_name = "Other"
        elif difference == "same_site":
            job.source_url = "https://www.linkedin.com/jobs/view/999999"
        elif difference == "unsafe_url":
            request.source_url_dedup_safe = False
        elif difference in {"ambiguous", "mixed_title_ambiguity"}:
            db.add(JobPosting(
                id="other", source_type="url", source_url="https://other.example.com/jobs/789",
                raw_content_artifact_id=artifact.artifact_id,
                posting_snapshot_hash="other", company_name=job.company_name,
                normalized_title=("alternate_provider_title" if difference == "mixed_title_ambiguity"
                                  else job.normalized_title), location=job.location,
                role={"job_title": parsed.role.job_title},
                employment_timing=job.employment_timing,
            ))
        elif difference == "merged":
            job.source = {"merged_into_job_id": "canonical"}
        db.flush()
        assert workflows.find_duplicate_job_posting(request, db, "new", parsed) == (None, None)
