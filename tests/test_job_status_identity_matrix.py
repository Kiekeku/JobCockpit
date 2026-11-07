from __future__ import annotations

import pytest
import httpx

from jobcockpit.ingestion.status_reconciliation import (
    classify_http_status,
    infer_status_from_page,
    posting_identity,
    _looks_like_auth_wall,
    status_urls_match,
)


ATS_DETAIL_URLS = (
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Platform-Engineer_R123",
        "workday",
        id="workday-detail",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/Page/Software-Engineer_R123",
        "workday",
        id="workday-page-location-detail",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/Software-Engineer_R123",
        "workday",
        id="workday-no-location-detail",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/01853388",
        "workday",
        id="workday-numeric-posting-detail",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/1234567",
        "greenhouse",
        id="greenhouse-detail",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1234567",
        "greenhouse",
        id="greenhouse-embed-detail",
    ),
    pytest.param(
        "https://jobs.lever.co/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        "lever",
        id="lever-detail",
    ),
    pytest.param(
        "https://jobs.ashbyhq.com/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        "ashby",
        id="ashby-detail",
    ),
    pytest.param(
        "https://example.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123456",
        "oracle",
        id="oracle-candidate-experience-detail",
    ),
    pytest.param(
        "https://jobs.smartrecruiters.com/UniversityHealthNetwork/sr744000127738917x",
        "smartrecruiters",
        id="smartrecruiters-sr-detail",
    ),
    pytest.param(
        "https://jobs.smartrecruiters.com/GDMSI/744000077012345",
        "smartrecruiters",
        id="smartrecruiters-numeric-detail",
    ),
    pytest.param(
        "https://smartrecruiters.com/GDMSI/744000077012345-platform-engineer",
        "smartrecruiters",
        id="smartrecruiters-bare-host-slug-detail",
    ),
    pytest.param(
        "https://jobs.workable.com/view/cf61xDXE26Bzsuf9d2cnun/hybrid-intern-devops",
        "workable",
        id="workable-jobs-detail",
    ),
    pytest.param(
        "https://apply.workable.com/mealsuite/jobs/view/4E451BC0DA.md",
        "workable",
        id="workable-apply-detail",
    ),
    pytest.param(
        "https://jobs-acme.icims.com/jobs/12345/platform-engineer/job",
        "icims",
        id="icims-detail",
    ),
    pytest.param(
        (
            "https://recruiting.ultipro.com/ACM1000/JobBoard/JobDetails/"
            "OpportunityDetail?jobId=a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
        ),
        "ultipro",
        id="ultipro-detail",
    ),
    pytest.param(
        (
            "https://recruiting.ultipro.ca/ACM1000/JobBoard/"
            "2526a5be-e33e-4de9-90ae-3b23d42b9441/OpportunityDetail"
            "?opportunityId=a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
        ),
        "ultipro",
        id="ultipro-canada-detail",
    ),
    pytest.param(
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
            "recruitment.html?cid=e7e9ca8d-f0d1-41a8-98bd-38aa2afbcc97&jobId=599336"
        ),
        "adp_workforcenow",
        id="adp-workforce-now-detail",
    ),
    pytest.param(
        "https://jobs.dayforcehcm.com/en-CA/acme/CANDIDATEPORTAL/jobs/3973",
        "dayforce",
        id="dayforce-detail",
    ),
    pytest.param(
        "https://acme.recruitee.com/o/platform-engineer",
        "recruitee",
        id="recruitee-detail",
    ),
    pytest.param(
        "https://jobs.jobvite.com/acme/job/platform-engineer",
        "jobvite",
        id="jobvite-detail",
    ),
    pytest.param(
        "https://acme.breezy.hr/p/platform-engineer",
        "breezy",
        id="breezy-detail",
    ),
    pytest.param(
        "https://acme.applytojob.com/apply/abc123/platform-engineer",
        "applytojob",
        id="applytojob-detail",
    ),
    pytest.param(
        "https://acme.bamboohr.com/careers/123",
        "bamboohr",
        id="bamboohr-detail",
    ),
)


AMBIGUOUS_404_ATS_SOURCE_KINDS = frozenset(
    {
        "adp_workforcenow",
        "applytojob",
        "breezy",
        "dayforce",
        "jobvite",
        "recruitee",
    }
)


AGGREGATOR_DETAIL_URLS = (
    pytest.param(
        "https://www.linkedin.com/jobs/view/platform-engineer-4425912315",
        "linkedin",
        id="linkedin-detail",
    ),
    pytest.param(
        "https://www.linkedin.com/comm/jobs/view/platform-engineer-4425912315",
        "linkedin",
        id="linkedin-comm-detail",
    ),
    pytest.param(
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
        "indeed",
        id="indeed-viewjob-detail",
    ),
    pytest.param(
        "https://ca.indeed.com/job/social-media-coordinator-b7c1b84ddd76944b",
        "indeed",
        id="indeed-pretty-detail",
    ),
    pytest.param(
        "https://www.indeed.com/q-Check-the-Status-of-Your-jobs.html?vjk=c01efd207eb8d10a",
        "indeed",
        id="indeed-selected-result-detail",
    ),
)


TRUSTED_NON_DETAIL_URLS = (
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/search",
        "workday",
        id="workday-search",
    ),
    pytest.param(
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
            "recruitment.html?cid=e7e9ca8d-f0d1-41a8-98bd-38aa2afbcc97"
        ),
        "adp_workforcenow",
        id="adp-workforce-now-without-job-id",
    ),
    pytest.param(
        "https://jobs.dayforcehcm.com/en-CA/acme/CANDIDATEPORTAL/jobs",
        "dayforce",
        id="dayforce-list",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/search",
        "workday",
        id="workday-job-search",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/search/123456",
        "workday",
        id="workday-nested-search",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/page/2",
        "workday",
        id="workday-page-navigation",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/list/2",
        "workday",
        id="workday-list-navigation",
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/browse/2",
        "workday",
        id="workday-browse-navigation",
    ),
    *(
        pytest.param(
            (
                "https://example.wd5.myworkdayjobs.com/Careers/job/"
                f"{segment}/Software-Engineer_R123"
            ),
            "workday",
            id=f"workday-{segment}-navigation-with-posting-like-tail",
        )
        for segment in (
            "browse",
            "jobs",
            "list",
            "preview",
            "result",
            "results",
            "search",
            "search-results",
            "view",
        )
    ),
    *(
        pytest.param(
            (
                "https://example.wd5.myworkdayjobs.com/Careers/job/"
                f"{segment}/Software-Engineer_R123"
            ),
            "workday",
            id=f"workday-{segment}-auth-with-posting-like-tail",
        )
        for segment in (
            "apply",
            "application",
            "candidatehome",
            "home",
            "login",
            "signin",
            "sign-in",
        )
    ),
    pytest.param(
        "https://example.wd5.myworkdayjobs.com/Careers/job/page/Toronto/Software-Engineer_R123",
        "workday",
        id="workday-page-navigation-with-nested-posting-like-tail",
    ),
    pytest.param(
        "https://job-boards.greenhouse.io/acme/jobs/search",
        "greenhouse",
        id="greenhouse-search",
    ),
    pytest.param(
        "https://jobs.lever.co/acme/search",
        "lever",
        id="lever-search",
    ),
    pytest.param(
        "https://jobs.ashbyhq.com/acme/search",
        "ashby",
        id="ashby-search",
    ),
    pytest.param(
        "https://example.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/search",
        "oracle",
        id="oracle-search",
    ),
    pytest.param(
        "https://www.linkedin.com/company/acme/jobs",
        "linkedin",
        id="linkedin-company-jobs",
    ),
    pytest.param(
        "https://www.linkedin.com/authwall",
        "linkedin",
        id="linkedin-authwall",
    ),
    pytest.param(
        "https://ca.indeed.com/jobs?q=platform+engineer",
        "indeed",
        id="indeed-search",
    ),
    pytest.param(
        "https://jobs.smartrecruiters.com/UniversityHealthNetwork",
        "smartrecruiters",
        id="smartrecruiters-board",
    ),
    pytest.param(
        "https://jobs.workable.com/search",
        "workable",
        id="workable-search",
    ),
    pytest.param(
        "https://jobs.workable.com/view/search",
        "workable",
        id="workable-reserved-view-token",
    ),
    pytest.param(
        "https://apply.workable.com/acme/j/signin",
        "workable",
        id="workable-reserved-j-token",
    ),
    pytest.param(
        "https://ca.indeed.com/viewjob?jk=search",
        "indeed",
        id="indeed-invalid-jk-token",
    ),
    *(
        pytest.param(
            f"https://jobs-acme.icims.com/jobs/{route}",
            "icims",
            id=f"icims-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            (
                "https://recruiting.ultipro.com/ACM1000/JobBoard/"
                f"JobDetails?jobId={route}"
            ),
            "ultipro",
            id=f"ultipro-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            f"https://acme.recruitee.com/o/{route}",
            "recruitee",
            id=f"recruitee-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            f"https://jobs.jobvite.com/acme/job/{route}",
            "jobvite",
            id=f"jobvite-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            f"https://acme.breezy.hr/p/{route}",
            "breezy",
            id=f"breezy-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            f"https://acme.applytojob.com/apply/{route}",
            "applytojob",
            id=f"applytojob-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
    *(
        pytest.param(
            f"https://acme.bamboohr.com/careers/{route}",
            "bamboohr",
            id=f"bamboohr-{route}-route",
        )
        for route in ("list", "search", "login")
    ),
)


UNTRUSTED_PROVIDER_LOOKALIKES = (
    pytest.param(
        "https://support.greenhouse.io/docs/jobs/1234567",
        id="greenhouse-support",
    ),
    pytest.param(
        "https://help.lever.co/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        id="lever-help",
    ),
    pytest.param(
        "https://docs.ashbyhq.com/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        id="ashby-docs",
    ),
    pytest.param(
        "https://storage.oraclecloud.com/random/job/123456",
        id="oracle-storage",
    ),
    pytest.param(
        "https://support.smartrecruiters.com/acme/8d5dd45f-a2f7-4f61-b789-3df32b14bc27",
        id="smartrecruiters-support",
    ),
    pytest.param(
        "https://support.workable.com/j/ABC123",
        id="workable-support",
    ),
    pytest.param(
        "https://support.bamboohr.com/careers/12345",
        id="bamboohr-support",
    ),
    pytest.param(
        "https://support.recruitee.com/o/security-guide",
        id="recruitee-support",
    ),
    pytest.param(
        "https://support.breezy.hr/p/12345",
        id="breezy-support",
    ),
    pytest.param(
        "https://support.applytojob.com/apply/12345",
        id="applytojob-support",
    ),
    pytest.param(
        "https://notgreenhouse.io/jobs/1234567",
        id="greenhouse-lookalike",
    ),
    pytest.param(
        "https://notlinkedin.com/jobs/view/4425912315",
        id="linkedin-lookalike",
    ),
    pytest.param(
        "https://notindeed.com/viewjob?jk=3cab9868c348dd54",
        id="indeed-lookalike",
    ),
)


@pytest.mark.parametrize(("source_url", "source_kind"), ATS_DETAIL_URLS)
def test_recognized_ats_detail_identity_matrix(source_url: str, source_kind: str):
    identity = posting_identity(source_url)

    assert identity.source_kind == source_kind
    assert identity.is_trusted_provider is True
    assert identity.is_detail is True
    assert identity.posting_id


@pytest.mark.parametrize(("source_url", "source_kind"), AGGREGATOR_DETAIL_URLS)
def test_recognized_aggregator_detail_identity_matrix(source_url: str, source_kind: str):
    identity = posting_identity(source_url)

    assert identity.source_kind == source_kind
    assert identity.is_trusted_provider is True
    assert identity.is_detail is True
    assert identity.posting_id


@pytest.mark.parametrize(("source_url", "source_kind"), TRUSTED_NON_DETAIL_URLS)
def test_trusted_provider_board_search_and_auth_routes_are_not_details(
    source_url: str,
    source_kind: str,
):
    identity = posting_identity(source_url)

    assert identity.source_kind == source_kind
    assert identity.is_trusted_provider is True
    assert identity.is_detail is False
    assert identity.posting_id is None


@pytest.mark.parametrize("source_url", UNTRUSTED_PROVIDER_LOOKALIKES)
def test_non_recruiting_and_lookalike_hosts_are_not_trusted(source_url: str):
    identity = posting_identity(source_url)

    assert identity.is_trusted_provider is False
    assert identity.is_detail is False
    assert identity.posting_id is None


@pytest.mark.parametrize(("source_url", "_source_kind"), ATS_DETAIL_URLS)
def test_explicit_ats_closed_banner_requires_a_recognized_detail(
    source_url: str,
    _source_kind: str,
):
    decision = infer_status_from_page(
        source_url,
        "The job posting is no longer available.",
    )

    assert decision is not None
    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "ats_no_longer_available"


@pytest.mark.parametrize(
    ("source_url", "banner", "expected_result", "expected_evidence"),
    (
        pytest.param(
            "https://www.linkedin.com/jobs/view/platform-engineer-4425912315",
            "No longer accepting applications",
            "closed",
            "linkedin_no_longer_accepting",
            id="linkedin-banner",
        ),
        pytest.param(
            "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
            "This job has expired on Indeed",
            "expired",
            "indeed_job_expired",
            id="indeed-viewjob-banner",
        ),
        pytest.param(
            "https://ca.indeed.com/job/social-media-coordinator-b7c1b84ddd76944b",
            "This job has expired on Indeed",
            "expired",
            "indeed_job_expired",
            id="indeed-pretty-banner",
        ),
    ),
)
def test_aggregator_terminal_banner_matrix(
    source_url: str,
    banner: str,
    expected_result: str,
    expected_evidence: str,
):
    decision = infer_status_from_page(source_url, banner)

    assert decision is not None
    assert decision.result == expected_result
    assert decision.detected_status == expected_result
    assert decision.evidence_code == expected_evidence


def test_indeed_selected_result_banner_is_not_bound_to_the_requested_job():
    decision = infer_status_from_page(
        (
            "https://www.indeed.com/q-Check-the-Status-of-Your-jobs.html"
            "?vjk=c01efd207eb8d10a"
        ),
        "Registered Nurse\nHospital X\nThis job has expired on Indeed",
    )

    assert decision is None


@pytest.mark.parametrize(
    "source_url",
    tuple(
        pytest.param(
            parameter.values[0],
            id=parameter.id,
        )
        for parameter in (*TRUSTED_NON_DETAIL_URLS, *UNTRUSTED_PROVIDER_LOOKALIKES)
    ),
)
def test_non_detail_routes_ignore_all_terminal_page_markers(source_url: str):
    decision = infer_status_from_page(
        source_url,
        (
            "No longer accepting applications. "
            "This job has expired on Indeed. "
            "The job posting is no longer available. "
            "The page you are looking for does not exist."
        ),
    )

    assert decision is None


@pytest.mark.parametrize(
    ("source_url", "source_kind"),
    tuple(
        parameter
        for parameter in ATS_DETAIL_URLS
        if parameter.values[1] not in AMBIGUOUS_404_ATS_SOURCE_KINDS
    ),
)
def test_first_404_on_a_recognized_ats_detail_is_definitive(
    source_url: str,
    source_kind: str,
):
    decision = classify_http_status(
        404,
        source_kind=source_kind,
        generic_404_count=0,
        source_url=source_url,
    )

    assert decision.result == "closed"
    assert decision.detected_status == "closed"
    assert decision.evidence_code == "recognized_ats_detail_404"


@pytest.mark.parametrize(
    ("source_url", "source_kind"),
    tuple(
        parameter
        for parameter in ATS_DETAIL_URLS
        if parameter.values[1] in AMBIGUOUS_404_ATS_SOURCE_KINDS
    ),
)
def test_ambiguous_slug_ats_detail_requires_two_404_checks(
    source_url: str,
    source_kind: str,
):
    first = classify_http_status(
        404,
        source_kind=source_kind,
        generic_404_count=0,
        source_url=source_url,
    )
    confirmed = classify_http_status(
        404,
        source_kind=source_kind,
        generic_404_count=1,
        source_url=source_url,
    )

    assert first.result == "inconclusive"
    assert first.evidence_code == "generic_http_404_pending_confirmation"
    assert confirmed.result == "closed"
    assert confirmed.evidence_code == "generic_http_404_confirmed"


@pytest.mark.parametrize(
    ("source_url", "source_kind"),
    tuple(
        parameter
        for parameter in ATS_DETAIL_URLS
        if parameter.values[1] in AMBIGUOUS_404_ATS_SOURCE_KINDS
    ),
)
def test_ambiguous_slug_missing_page_banner_is_not_definitive(
    source_url: str,
    source_kind: str,
):
    identity = posting_identity(source_url)
    decision = infer_status_from_page(
        source_url,
        "The page you are looking for does not exist.",
    )

    assert identity.source_kind == source_kind
    assert identity.is_detail is True
    assert decision is None


@pytest.mark.parametrize(
    ("source_url", "source_kind"),
    (*AGGREGATOR_DETAIL_URLS, *TRUSTED_NON_DETAIL_URLS),
)
def test_aggregator_and_non_detail_first_404_requires_confirmation(
    source_url: str,
    source_kind: str,
):
    decision = classify_http_status(
        404,
        source_kind=source_kind,
        generic_404_count=0,
        source_url=source_url,
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "generic_http_404_pending_confirmation"


@pytest.mark.parametrize(
    "source_url",
    (
        "https://ca.indeed.com/job/search-engine-engineer-b7c1b84ddd76944b",
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Search-Engineer_R123",
        "https://jobs.workable.com/view/cf61xDXE26Bzsuf9d2cnun/search-engine-intern",
    ),
)
def test_detail_slugs_containing_search_are_not_search_walls(source_url: str):
    assert posting_identity(source_url).is_detail is True
    assert _looks_like_auth_wall(source_url, "") is False


@pytest.mark.parametrize(
    "source_url",
    (
        "https://ca.indeed.com/jobs/search?q=engineer",
        "https://example.wd5.myworkdayjobs.com/Careers/job/search/123456",
        "https://jobs.workable.com/search",
        "https://jobs.workable.com/view/search",
        "https://apply.workable.com/acme/j/signin",
    ),
)
def test_actual_search_routes_are_search_walls(source_url: str):
    assert posting_identity(source_url).is_detail is False
    assert _looks_like_auth_wall(source_url, "") is True


def test_equivalent_indeed_detail_forms_match_by_posting_identity():
    assert status_urls_match(
        "https://ca.indeed.com/job/search-engine-engineer-b7c1b84ddd76944b",
        "https://ca.indeed.com/viewjob?jk=b7c1b84ddd76944b",
    )


def test_linkedin_comm_detail_form_matches_the_canonical_detail_identity():
    assert status_urls_match(
        "https://www.linkedin.com/comm/jobs/view/platform-engineer-4425912315",
        "https://www.linkedin.com/jobs/view/4425912315",
    )


@pytest.mark.parametrize(
    "redirect_path",
    (
        "rc/clk",
        "rc/clk/dl",
        "pagead/clk",
        "pagead/clk/dl",
    ),
)
def test_indeed_jk_redirect_forms_match_the_same_detail_identity(
    redirect_path: str,
):
    redirect_url = (
        f"https://ca.indeed.com/{redirect_path}?from=alert&jk=3cab9868c348dd54"
    )

    identity = posting_identity(redirect_url)

    assert identity.source_kind == "indeed"
    assert identity.is_detail is True
    assert identity.posting_id == "3cab9868c348dd54"
    assert status_urls_match(
        redirect_url,
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
    )


def test_indeed_local_jobcard_identity_is_not_a_fetchable_job_detail():
    source_url = f"https://ca.indeed.com/pagead/clk?jobcard={'a' * 64}"

    identity = posting_identity(source_url)

    assert identity.source_kind == "indeed"
    assert identity.is_trusted_provider is True
    assert identity.is_detail is False
    assert identity.posting_id is None


def test_indeed_vjk_search_form_matches_the_same_detail_identity():
    source_url = (
        "https://ca.indeed.com/jobs?q=platform&vjk=3cab9868c348dd54"
    )

    identity = posting_identity(source_url)

    assert identity.source_kind == "indeed"
    assert identity.is_detail is True
    assert identity.posting_id == "3cab9868c348dd54"
    assert status_urls_match(
        source_url,
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
    )


def test_workday_identity_uses_requisition_token_not_localized_slug():
    assert status_urls_match(
        (
            "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/"
            "Software-Engineer_R123"
        ),
        (
            "https://example.wd5.myworkdayjobs.com/Careers/job/Montreal/"
            "Ingenieur-logiciel_R123"
        ),
    )


@pytest.mark.parametrize(
    ("expected", "returned"),
    (
        pytest.param(
            "https://example.wd5.myworkdayjobs.com/Careers/job/Role_R123",
            "https://example.wd5.myworkdayjobs.com/Careers/job/Role_r123",
            id="workday-token",
        ),
        pytest.param(
            "https://example.wd5.myworkdayjobs.com/Careers/job/Role_R123",
            "https://example.wd5.myworkdayjobs.com/careers/job/Role_R123",
            id="workday-site",
        ),
        pytest.param(
            "https://jobs.workable.com/view/AbC123/platform-engineer",
            "https://jobs.workable.com/view/aBc123/platform-engineer",
            id="workable",
        ),
        pytest.param(
            "https://acme.recruitee.com/o/AbC123",
            "https://acme.recruitee.com/o/aBc123",
            id="recruitee",
        ),
        pytest.param(
            "https://acme.breezy.hr/p/AbC123",
            "https://acme.breezy.hr/p/aBc123",
            id="breezy",
        ),
        pytest.param(
            "https://jobs.applytojob.com/apply/AbC123",
            "https://jobs.applytojob.com/apply/aBc123",
            id="applytojob",
        ),
        pytest.param(
            "https://acme.bamboohr.com/careers/AbC123",
            "https://acme.bamboohr.com/careers/aBc123",
            id="bamboohr",
        ),
    ),
)
def test_opaque_provider_tokens_and_path_scopes_are_case_sensitive(
    expected: str,
    returned: str,
):
    assert not status_urls_match(expected, returned)


def test_smartrecruiters_identity_ignores_display_slug_changes():
    assert status_urls_match(
        "https://jobs.smartrecruiters.com/Acme/744000077012345-platform-engineer",
        "https://jobs.smartrecruiters.com/Acme/744000077012345-ingenieur-logiciel",
    )
    assert not status_urls_match(
        "https://jobs.smartrecruiters.com/Acme/744000077012345-platform-engineer",
        "https://jobs.smartrecruiters.com/Acme/744000077012346-platform-engineer",
    )


def test_generic_url_match_normalizes_only_scheme_and_host_case():
    assert status_urls_match(
        "HTTPS://Careers.Example.com/jobs/Platform-Engineer?locale=en",
        "https://careers.example.com/jobs/Platform-Engineer?locale=en",
    )

@pytest.mark.parametrize(
    ("expected", "returned"),
    (
        ("https://careers.example.com/jobs/123", "https://careers.example.com:443/jobs/123"),
        ("http://careers.example.com/jobs/123", "http://careers.example.com:80/jobs/123"),
    ),
)
def test_generic_url_match_normalizes_explicit_default_ports(
    expected: str,
    returned: str,
):
    assert status_urls_match(expected, returned)


def test_generic_url_match_preserves_case_sensitive_path_and_query():
    assert not status_urls_match(
        "https://careers.example.com/jobs/Platform-Engineer?locale=en",
        "https://careers.example.com/jobs/platform-engineer?locale=EN",
    )


@pytest.mark.parametrize(
    "returned",
    (
        "http://boards.greenhouse.io/acme/jobs/1234567",
        "https://boards.greenhouse.io:8443/acme/jobs/1234567",
        "https://!@boards.greenhouse.io/acme/jobs/1234567",
    ),
)
def test_provider_identity_match_rejects_unsafe_redirect_transport(returned: str):
    assert not status_urls_match(
        "https://job-boards.greenhouse.io/acme/jobs/1234567",
        returned,
    )


def test_provider_identity_match_allows_http_to_https_upgrade():
    assert status_urls_match(
        "http://boards.greenhouse.io/acme/jobs/1234567",
        "https://job-boards.greenhouse.io/acme/jobs/1234567",
    )


@pytest.mark.parametrize(
    ("http_url", "https_url"),
    (
        (
            (
                "http://acme.wd5.myworkdayjobs.com/en-US/Careers/job/"
                "Platform-Engineer_R123"
            ),
            (
                "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/"
                "Platform-Engineer_R123"
            ),
        ),
        (
            (
                "http://acme.oraclecloud.com/hcmUI/CandidateExperience/"
                "en/sites/CX_1/job/12345"
            ),
            (
                "https://acme.oraclecloud.com/hcmUI/CandidateExperience/"
                "en/sites/CX_1/job/12345"
            ),
        ),
    ),
)
def test_tenant_scoped_provider_identity_allows_only_safe_tls_upgrade(
    http_url: str,
    https_url: str,
):
    assert status_urls_match(http_url, https_url)
    assert not status_urls_match(https_url, http_url)

    parsed = httpx.URL(https_url)
    custom_port_url = str(parsed.copy_with(port=8443))
    assert not status_urls_match(http_url, custom_port_url)


def test_exact_provider_url_with_custom_port_still_matches_without_fallback():
    source_url = "https://boards.greenhouse.io:8443/acme/jobs/1234567"

    assert status_urls_match(source_url, source_url)


@pytest.mark.parametrize("source_url", UNTRUSTED_PROVIDER_LOOKALIKES)
def test_untrusted_host_first_404_requires_confirmation(source_url: str):
    identity = posting_identity(source_url)
    decision = classify_http_status(
        404,
        source_kind=identity.source_kind,
        generic_404_count=0,
        source_url=source_url,
    )

    assert decision.result == "inconclusive"
    assert decision.detected_status is None
    assert decision.evidence_code == "generic_http_404_pending_confirmation"
