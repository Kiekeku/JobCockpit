from __future__ import annotations

import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsJob
from jobcockpit.ingestion.posting_discovery import (
    PostingDiscoveryResolver,
    build_search_query,
    external_aggregator_kind,
    posting_search_term_from_text,
    rendered_posting_identity_verdict,
)
from jobcockpit.ingestion.posting_resolution import (
    ExternalPostingHint,
    PostingResolution,
    RenderedIdentityVerdict,
    ResolvedExternalPosting,
    SearchResult,
)


def test_serper_posting_discovery_filters_aggregator_before_official_candidate() -> None:
    official_url = "https://careers.example.test/jobs/embedded-intern"
    searched: list[tuple[str, int]] = []
    resolved: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, limit: int):
            searched.append((query, limit))
            return [
                SearchResult(
                    title="Embedded Software Engineer Intern",
                    url="https://www.linkedin.com/jobs/view/1234567890",
                ),
                SearchResult(
                    title="Embedded Software Engineer Intern",
                    url=official_url,
                    snippet="Example Systems careers",
                ),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved.append(apply_url)
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: Embedded Software Engineer Intern\n"
                    "Company: Example Systems\n"
                    "Location: Ottawa, ON"
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
        max_results=5,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Example Systems",
            title="Embedded Software Engineer Intern",
            location="Ottawa, ON",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url
    assert resolved == [official_url]
    assert searched == [
        ('"Example Systems" "Embedded Software Engineer Intern" careers job Ottawa, ON', 15)
    ]
    assert resolution.attempts[0].stage == "official_search"
    assert resolution.attempts[0].provider == "serper"
    assert resolution.attempts[0].candidate_count == 1


def test_official_career_site_search_precedes_serper_for_fresh_posting() -> None:
    official_url = "https://careers.amd.com/careers-home/jobs/91311"
    searched: list[tuple[str, object]] = []

    class FakeOfficialCareerSiteSearchClient:
        provider_code = "jibe"
        last_error = ""

        def search(self, hint: ExternalPostingHint, limit: int):
            searched.append(("official", hint, limit))
            return [
                SearchResult(
                    title="Short Term 2027 Data Analyst Intern/Co-Op",
                    url=official_url,
                    snippet="Advanced Micro Devices, Inc | MARKHAM, Canada",
                )
            ]

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, limit: int):
            searched.append(("serper", query, limit))
            raise AssertionError("Serper must not run after first-party discovery succeeds")

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            assert apply_url == official_url
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: Short Term 2027 Data Analyst Intern/Co-Op\n"
                    "Company: Advanced Micro Devices, Inc\n"
                    "Location: Markham, Ontario, Canada\n"
                    "LinkedIn Hint Company: AMD"
                ),
            )

    hint = ExternalPostingHint(
        source_url="https://www.linkedin.com/jobs/view/4459291699",
        source_kind="linkedin",
        company="AMD",
        title="Short Term 2027 Data Analyst Intern/Co-Op",
        location="Markham, Ontario, Canada",
    )
    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        official_career_site_search_client=FakeOfficialCareerSiteSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(hint)

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url
    assert searched == [("official", hint, 5)]
    assert resolution.attempts[0].provider == "jibe"


def test_serper_remains_fallback_when_company_has_no_first_party_search_match() -> None:
    searched: list[str] = []

    class EmptyOfficialCareerSiteSearchClient:
        provider_code = "jibe"
        last_error = "unsupported_company"

        def search(self, hint: ExternalPostingHint, limit: int):
            searched.append(f"official:{hint.company}:{limit}")
            return []

    class EmptySerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, limit: int):
            searched.append(f"serper:{query}:{limit}")
            return []

    resolution = PostingDiscoveryResolver(
        search_client=EmptySerperSearchClient(),
        official_career_site_search_client=EmptyOfficialCareerSiteSearchClient(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Example Systems",
            title="Platform Intern",
            location="Ottawa, ON",
        )
    )

    assert resolution.status == "not_found"
    assert searched == [
        "official:Example Systems:5",
        'serper:"Example Systems" "Platform Intern" careers job Ottawa, ON:15',
        'serper:"Example Systems" "Platform Intern" careers job:15',
        'serper:"Example Systems" careers:15',
    ]
    assert resolution.attempts[0].provider == "serper"


def test_first_party_search_failure_survives_empty_serper_fallback() -> None:
    class FailingOfficialCareerSiteSearchClient:
        provider_code = "workday"
        last_error = "official_site_request_failed"

        def search(self, _hint: ExternalPostingHint, _limit: int):
            return []

    class EmptySerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return []

    resolution = PostingDiscoveryResolver(
        search_client=EmptySerperSearchClient(),
        official_career_site_search_client=FailingOfficialCareerSiteSearchClient(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="RBC",
            title="Software Developer Intern",
            location="Toronto, ON",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "official_site_request_failed"
    assert resolution.official_discovery_status == "failed"
    assert resolution.reason_code == "official_site_request_failed"
    assert resolution.attempts[0].provider == "workday"
    assert resolution.attempts[0].reason_code == "official_site_request_failed"


def test_first_party_search_failure_survives_portal_only_serper_results() -> None:
    class FailingOfficialCareerSiteSearchClient:
        provider_code = "workday"
        last_error = "official_site_request_failed"

        def search(self, _hint: ExternalPostingHint, _limit: int):
            return []

    class PortalOnlySerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Software Developer Intern",
                    url="https://www.linkedin.com/jobs/view/1234567890",
                )
            ]

    resolution = PostingDiscoveryResolver(
        search_client=PortalOnlySerperSearchClient(),
        official_career_site_search_client=FailingOfficialCareerSiteSearchClient(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="RBC",
            title="Software Developer Intern",
            location="Toronto, ON",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "official_site_request_failed"
    assert resolution.reason_code == "official_site_request_failed"
    assert [(attempt.provider, attempt.reason_code) for attempt in resolution.attempts] == [
        ("workday", "official_site_request_failed"),
        ("serper", "official_posting_not_found"),
        ("serper", "official_posting_not_found"),
        ("serper", "official_posting_not_found"),
    ]


def test_serper_filters_internhunt_before_trans_mountain_official_candidate() -> None:
    portal_url = "https://internhunt.eu/"
    official_url = (
        "https://careers.transmountain.com/job/Calgary-IT%2C-Enterprise-and-"
        "Business-Solutions%2C-Co-op-Student-%2812-month-term%29-Albe/1291506047/"
    )
    resolved: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="IT, Enterprise and Business Solutions, Co-op Student",
                    url=portal_url,
                    snippet="Trans Mountain Calgary internship",
                ),
                SearchResult(
                    title="IT, Enterprise and Business Solutions, Co-op Student",
                    url=official_url,
                    snippet="Trans Mountain careers",
                ),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved.append(apply_url)
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: IT, Enterprise and Business Solutions, Co-op Student\n"
                    "Company: Trans Mountain\n"
                    "Location: Calgary, AB"
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Trans Mountain",
            title="IT, Enterprise and Business Solutions, Co-op Student",
            location="Calgary, AB",
        )
    )

    assert external_aggregator_kind(portal_url) == "internhunt"
    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url
    assert resolved == [official_url]
    assert resolution.attempts[0].candidate_count == 1


def test_serper_never_promotes_internhunt_to_official_source() -> None:
    portal_url = "https://internhunt.eu/"
    resolved: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="IT, Enterprise and Business Solutions, Co-op Student",
                    url=portal_url,
                    snippet="Trans Mountain Calgary internship",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            resolved.append(apply_url)
            raise AssertionError("known job portals must not be resolved as official sources")

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Trans Mountain",
            title="IT, Enterprise and Business Solutions, Co-op Student",
            location="Calgary, AB",
        )
    )

    assert resolution.status == "not_found"
    assert resolution.posting is None
    assert resolved == []
    assert resolution.attempts[0].candidate_count == 0


def test_serper_searches_past_portal_heavy_top_results_for_official_detail() -> None:
    official_url = "https://careers.example.test/jobs/98765-software-intern"
    requested_limits: list[int] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, limit: int):
            requested_limits.append(limit)
            portals = [
                SearchResult(
                    title="Software Intern",
                    url=f"https://www.linkedin.com/jobs/view/{index}",
                )
                for index in range(10000, 10005)
            ]
            return [
                *portals,
                SearchResult(
                    title="Software Intern",
                    url=official_url,
                    snippet="Example Systems careers",
                ),
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text="Title: Software Intern\nCompany: Example Systems",
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
        max_results=5,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Example Systems",
            title="Software Intern",
        )
    )

    assert requested_limits == [15]
    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url


def test_official_candidate_redirect_to_portal_is_rejected_by_status_url_contract() -> None:
    candidate_url = "https://careers.example.test/jobs/123"

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [SearchResult(title="Software Intern", url=candidate_url)]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            assert apply_url == candidate_url
            return AtsJob(
                source_url="https://internhunt.eu/",
                raw_text="Title: Software Intern\nCompany: Example Systems",
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Example Systems",
            title="Software Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.error == "official_candidate_not_job_detail"
    assert [a for a in resolution.attempts if a.stage == "official_candidate_render"][-1].reason_code == "official_candidate_not_job_detail"


@pytest.mark.parametrize(
    ("candidate_url", "company"),
    [
        (
            "https://www1.communitech.ca/companies/later/jobs/85948810-software-co-op",
            "Later",
        ),
        ("https://cresta.com/careers/", "Cresta"),
        ("https://validate.perfdrive.com/", "CGI Inc."),
        ("https://www.eclipse.org/downloads/", "Eclipse Automation Inc."),
        ("https://myjobs.adp.com/hendrickson", "Hendrickson"),
        ("https://www.microsoft.com/en-us", "Microsoft"),
        ("https://careers.gerdau.com/view-all-jobs", "Gerdau North America"),
    ],
)
def test_serper_does_not_promote_non_authoritative_or_index_urls(
    candidate_url: str,
    company: str,
) -> None:
    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Software Development Co-op",
                    url=candidate_url,
                    snippet="Later Software Development Co-op",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: Software Development Co-op\n"
                    f"Company: {company}\n"
                    "Location: Vancouver, BC"
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company=company,
            title="Software Development Co-op",
            location="Vancouver, BC",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.error == "official_candidate_not_employer_authoritative"
    assert (
        [a for a in resolution.attempts if a.stage == "official_candidate_render"][-1].reason_code
        == "official_candidate_not_employer_authoritative"
    )


@pytest.mark.parametrize(
    "candidate_url",
    [
        "https://careers.kpmg.ca/jobs/31912",
        "https://stemhealth.applytojobs.ca/software/48742",
        "https://jobs.crelate.com/portal/TalentSphere/job/9km9ojhwidzjz1rot7ayz9w7ew",
    ],
)
def test_serper_accepts_employer_or_tenant_owned_detail_urls(candidate_url: str) -> None:
    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Software Intern",
                    url=candidate_url,
                    snippet="Example Systems Software Intern",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=f"Title: Software Intern\nCompany: {company}",
            )

    company = (
        "KPMG"
        if "kpmg" in candidate_url
        else "Stem Health"
        if "stemhealth" in candidate_url
        else "TalentSphere"
    )
    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company=company,
            title="Software Intern",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == candidate_url


def test_official_candidate_conflicting_page_company_is_low_confidence() -> None:
    candidate_url = "https://careers.example.test/jobs/123"

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Software Intern",
                    url=candidate_url,
                    snippet="Example Systems Software Intern",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: Software Intern\n"
                    "Company: Generic Job Portal\n"
                    "Location: Calgary, AB\n"
                    "Featured listing: Example Systems Software Intern"
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="Example Systems",
            title="Software Intern",
            location="Calgary, AB",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.error == "official_candidates_low_confidence"
    assert [a for a in resolution.attempts if a.stage == "official_candidate_render"][-1].confidence == pytest.approx(0.55)


def test_company_acronym_does_not_match_unreviewed_same_initials() -> None:
    candidate_url = "https://careers.amd.com/careers-home/jobs/123"

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Software Intern",
                    url=candidate_url,
                    snippet="AMD Software Intern",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text="Title: Software Intern\nCompany: Acme Medical Devices",
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/1234567890",
            source_kind="linkedin",
            company="AMD",
            title="Software Intern",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.error == "official_candidates_low_confidence"
    assert [a for a in resolution.attempts if a.stage == "official_candidate_render"][-1].confidence == pytest.approx(0.45)


def test_official_candidate_with_authoritative_mismatched_title_is_rejected() -> None:
    candidate_url = (
        "https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1/job/Toronto/"
        "RBC-Borealis-Software-Developer_R-0000184501"
    )

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="RBC Borealis AI Data Analyst",
                    url=candidate_url,
                    snippet="RBC Borealis AI Data Analyst",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                title=(
                    "2027 Winter Student Opportunities RBC Borealis - "
                    "Software Developer, 4-8 Months"
                ),
                raw_text=(
                    "2027 Winter Student Opportunities RBC Borealis - "
                    "Software Developer, 4-8 Months\nCompany: RBC\n"
                    "Related search: RBC Borealis AI Data Analyst"
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        allow_aggregator_fallback=False,
    ).resolve(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4455348717",
            source_kind="linkedin",
            company="RBC",
            title="2027 Winter Student Opportunities RBC Borealis - AI Data Analyst",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.posting is None
    assert resolution.error == "official_candidate_title_mismatch"
    assert [a for a in resolution.attempts if a.stage == "official_candidate_render"][-1].reason_code == "official_candidate_title_mismatch"


def test_serper_resolves_official_posting_before_rendering_aggregator() -> None:
    source_url = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
    official_url = "https://careers.exampleconstruction.test/jobs/data-science-student"
    rendered_calls: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Data Science Student",
                    url=official_url,
                    snippet="Example Construction careers",
                )
            ]

    class FakeApplyResolver:
        def resolve(self, apply_url: str, context=None):
            return AtsJob(
                source_url=apply_url,
                raw_text=(
                    "Title: Data Science Student\n"
                    "Company: Example Construction\n"
                    "Location: Edmonton, AB"
                ),
            )

    class RenderedAggregatorFallback:
        def resolve(self, hint):
            rendered_calls.append(hint.source_url)
            raise AssertionError("aggregator rendering must be fallback-only")

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        apply_resolver=FakeApplyResolver(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            company="Example Construction",
            title="Data Science Student",
            location="Edmonton, AB",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == official_url
    assert rendered_calls == []
    assert [attempt.stage for attempt in resolution.attempts] == [
        "official_search",
        "official_candidate_render",
    ]


def test_serper_requires_company_and_title_and_includes_optional_context() -> None:
    source_url = "https://ca.indeed.com/viewjob?jk=missing-company"
    calls: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, _limit: int):
            calls.append(f"search:{query}")
            return []

    class RenderedAggregatorFallback:
        def resolve(self, _hint):
            calls.append("render")
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=source_url,
                    raw_text=(
                        "Title: Data Science Student\n"
                        "Company: Example Construction\n"
                        "Location: Edmonton, AB\n"
                        "Employment term: Fall 2026"
                    ),
                    confidence=0.9,
                ),
            )

    PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            title="Data Science Student",
            location="Edmonton, AB",
            term="Fall 2026",
        )
    )

    assert calls == [
        "render",
        'search:"Example Construction" "Data Science Student" careers job "Fall 2026" Edmonton, AB',
        'search:"Example Construction" "Data Science Student" careers job',
        'search:"Example Construction" careers',
        'search:site:linkedin.com/jobs/view "Example Construction" "Data Science Student"',
    ]


def test_serper_is_not_called_when_company_cannot_be_recovered() -> None:
    source_url = "https://ca.indeed.com/viewjob?jk=still-missing-company"
    search_calls: list[str] = []

    class FakeSerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, _limit: int):
            search_calls.append(query)
            return []

    class IncompleteRenderedAggregator:
        def resolve(self, _hint):
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_low_confidence",
            )

    resolution = PostingDiscoveryResolver(
        search_client=FakeSerperSearchClient(),
        rendered_fallback_resolver=IncompleteRenderedAggregator(),
    ).resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            title="Data Science Student",
            location="Edmonton, AB",
            term="Fall 2026",
        )
    )

    assert resolution.status == "needs_review"
    assert resolution.error == "chrome_cdp_low_confidence"
    assert search_calls == []


def test_serper_failure_uses_aggregator_fallback_and_preserves_failure_trace() -> None:
    source_url = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"

    class FailedSerperSearchClient:
        provider_code = "serper"
        last_error = "serper_timeout"

        def search(self, _query: str, _limit: int):
            return []

    class RenderedAggregatorFallback:
        def resolve(self, _hint):
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=source_url,
                    raw_text=(
                        "Title: Platform Engineer\nCompany: Example Systems\nLocation: Ottawa, ON"
                    ),
                    confidence=0.9,
                    evidence="Rendered aggregator fallback",
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=FailedSerperSearchClient(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=source_url,
            source_kind="indeed",
            company="Example Systems",
            title="Platform Engineer",
            location="Ottawa, ON",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.source_path == "aggregator_rendered_fallback"
    assert resolution.error == "serper_timeout"
    assert resolution.official_discovery_status == "timeout"
    official_search = next(
        attempt for attempt in resolution.attempts if attempt.stage == "official_search"
    )
    assert official_search.provider == "serper"
    assert official_search.status == "needs_review"
    assert official_search.reason_code == "serper_timeout"


def test_serper_prefers_matching_linkedin_fallback_without_search_loop() -> None:
    indeed_url = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
    linkedin_url = "https://www.linkedin.com/jobs/view/1234567890"
    search_calls: list[str] = []
    rendered_hints: list[ExternalPostingHint] = []

    class AggregatorOnlySerperSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, _limit: int):
            search_calls.append(query)
            return [
                SearchResult(
                    title="Data Science Student - Example Construction | Indeed",
                    url=indeed_url,
                    snippet="Example Construction hiring in Edmonton - 8 Month Co-op",
                ),
                SearchResult(
                    title="Data Science Student - Example Construction | LinkedIn",
                    url=linkedin_url,
                    snippet="Example Construction hiring in Edmonton - 8 Month Co-op",
                ),
            ]

    class RenderedAggregatorFallback:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text=(
                        "Title: Data Science Student\n"
                        "Company: Example Construction\n"
                        "Location: Edmonton, AB\nDuration: 8 months\nBuild analytics tools for our customer operations team."
                    ),
                    confidence=0.9,
                ),
            )

    resolution = PostingDiscoveryResolver(
        search_client=AggregatorOnlySerperSearchClient(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Example Construction",
            title="Data Science Student",
            location="Edmonton, AB",
            term="8-month term",
        )
    )

    assert resolution.status == "resolved"
    assert resolution.posting is not None
    assert resolution.posting.source_url == linkedin_url
    assert len(search_calls) == 3
    assert [hint.source_url for hint in rendered_hints] == [linkedin_url]
    assert rendered_hints[0].url_identity == "source_identity"
    assert (
        rendered_posting_identity_verdict(
            ResolvedExternalPosting(
                source_url="https://www.linkedin.com/jobs/view/data-science-student-1234567890",
                raw_text="",
            ),
            rendered_hints[0],
        )
        is RenderedIdentityVerdict.MATCH
    )
    assert (
        rendered_posting_identity_verdict(
            ResolvedExternalPosting(
                source_url="https://www.linkedin.com/jobs/view/9999999999",
                raw_text="",
            ),
            rendered_hints[0],
        )
        is RenderedIdentityVerdict.MISMATCH
    )


def test_serper_does_not_replace_source_with_unrelated_linkedin_result() -> None:
    indeed_url = "https://ca.indeed.com/viewjob?jk=original-job"
    rendered_urls: list[str] = []

    class UnrelatedLinkedInSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Data Science Student - Unrelated Employer | LinkedIn",
                    url="https://www.linkedin.com/jobs/view/9999999999",
                    snippet="Unrelated Employer hiring in Toronto",
                )
            ]

    class RenderedAggregatorFallback:
        def resolve(self, hint):
            rendered_urls.append(hint.source_url)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=hint.source_url,
                    raw_text=(
                        "Title: Data Science Student\n"
                        "Company: Example Construction\n"
                        "Location: Edmonton, AB"
                    ),
                    confidence=0.9,
                ),
            )

    PostingDiscoveryResolver(
        search_client=UnrelatedLinkedInSearchClient(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Example Construction",
            title="Data Science Student",
            location="Edmonton, AB",
        )
    )

    assert rendered_urls == [indeed_url]


def test_serper_does_not_replace_source_with_conflicting_location_or_term() -> None:
    indeed_url = "https://ca.indeed.com/viewjob?jk=original-job"
    rendered_urls: list[str] = []

    class ConflictingLinkedInSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            return [
                SearchResult(
                    title="Data Science Student - Example Construction | LinkedIn",
                    url="https://www.linkedin.com/jobs/view/9999999999",
                    snippet="Example Construction role in Toronto for Winter 2027",
                )
            ]

    class RenderedAggregatorFallback:
        def resolve(self, hint):
            rendered_urls.append(hint.source_url)
            return PostingResolution(status="needs_review", error="source_blocked")

    PostingDiscoveryResolver(
        search_client=ConflictingLinkedInSearchClient(),
        rendered_fallback_resolver=RenderedAggregatorFallback(),
    ).resolve(
        ExternalPostingHint(
            source_url=indeed_url,
            source_kind="indeed",
            company="Example Construction",
            title="Data Science Student",
            location="Edmonton, AB",
            term="Fall 2026",
        )
    )

    assert rendered_urls == [indeed_url]


def test_posting_search_term_requires_employment_context() -> None:
    assert posting_search_term_from_text("Requires at least 8 months of Python experience") == ""
    assert posting_search_term_from_text("Requires 8 months internship experience") == ""
    assert posting_search_term_from_text("Graduating in Fall 2026") == ""
    assert posting_search_term_from_text("Employment term: 8 months") == "8 months"
    assert posting_search_term_from_text("Title: Data Science Student - Fall 2026") == "Fall 2026"
    assert (
        build_search_query(
            ExternalPostingHint(
                source_url="https://ca.indeed.com/viewjob?jk=term-query",
                source_kind="indeed",
                company="Example Construction",
                title="Data Science Student",
                location="Edmonton, AB",
                term="8-month term",
            )
        )
        == '"Example Construction" "Data Science Student" careers job "8 month" Edmonton, AB'
    )
