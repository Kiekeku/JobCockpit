"""Minimized regressions grounded in the September 4 saved-source audit.

The two engineering-intern contexts, pharmacy city mismatch, numeric student
listing, and empty career shells are recorded privately under var/reports.
Expected identity must never become evidence about a fetched destination.
"""

import json

import pytest

from jobcockpit.ingestion.ats_handlers.apply import (
    AtsPostingContext,
    _job_from_generic_apply_page,
)
from jobcockpit.ingestion.posting_discovery import (
    PostingDiscoveryResolver,
    _official_candidate_url_matches_hint,
)
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint, SearchResult
from jobcockpit.integrations.official_careers import jibe_career_site_for_company
from jobcockpit.ingestion.ats_adapters.base import AtsJob
from jobcockpit.ingestion.posting_resolution import PostingResolution, ResolvedExternalPosting
from jobcockpit.ingestion.source_reconciliation import source_url_needs_discovery
from jobcockpit.ingestion.status_identity import posting_identity


def test_observed_eightfold_pid_route_is_an_employer_detail_not_a_career_listing():
    # Minimized from the user-supplied employer /careers?pid=... posting.
    url = "https://exampleengineering.eightfold.ai/careers?location=canada&pid=123456789"
    hint = ExternalPostingHint(source_url="", source_kind="linkedin", company="Example Engineering")
    identity = posting_identity(url)
    assert identity.source_kind == "eightfold"
    assert identity.is_detail
    assert identity.posting_id == "123456789"
    assert _official_candidate_url_matches_hint(url, hint)
    assert not source_url_needs_discovery(url)
    assert not _official_candidate_url_matches_hint(url, hint.__class__(
        source_url="", source_kind="linkedin", company="Different Employer",
    ))


@pytest.mark.parametrize("url", [
    "https://exampleengineering.eightfold.ai/careers",
    "https://exampleengineering.eightfold.ai/careers?pid=",
    "https://exampleengineering.eightfold.ai/careers?pid=123&pid=456",
    "https://exampleengineering.eightfold.ai/careers?pid=not-a-job",
    "https://exampleengineering.eightfold.ai/privacy-policy/?pid=123456789",
    "https://exampleengineering.eightfold.ai.evil.test/careers?pid=123456789",
])
def test_eightfold_detail_contract_rejects_listing_and_invalid_destinations(url):
    hint = ExternalPostingHint(source_url="", source_kind="linkedin", company="Example Engineering")
    assert not _official_candidate_url_matches_hint(url, hint)
    assert not posting_identity(url).is_detail


def resolve_page(html, *, title, location="Calgary, AB", path="/jobs/12345"):
    url = "https://careers.exampleengineering.com" + path
    hint = ExternalPostingHint(
        source_url="https://www.linkedin.com/jobs/view/12345",
        source_kind="linkedin", company="Example Engineering", title=title,
        location=location,
    )

    class Search:
        def search(self, query, limit):
            return [SearchResult(title=title, url=url, snippet=f"{title} {location}")]

    class Apply:
        def resolve(self, candidate_url, context):
            return _job_from_generic_apply_page(candidate_url, html, context)

    return PostingDiscoveryResolver(
        search_client=Search(), apply_resolver=Apply(), allow_aggregator_fallback=False,
    ).resolve(hint)


def page(title, location, description="Design and maintain treatment systems."):
    return '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": title,
        "hiringOrganization": {"name": "Example Engineering"},
        "jobLocation": {"address": {"addressLocality": location}},
        "description": description,
    }) + "</script>"


def test_observed_title_is_returned_separately_from_expected_title():
    job = _job_from_generic_apply_page(
        "https://example.test/job/12345", page("Process Engineer", "Winnipeg"),
        AtsPostingContext(role="Water/Wastewater Process Engineering Intern"),
    )
    assert job.title == "Process Engineer"


@pytest.mark.parametrize("html,title,location,path", [
    (page("Process Engineer - Water/Wastewater Treatment", "Winnipeg"),
     "Water/Wastewater Process Engineering Intern", "Markham, ON", "/jobs/12345"),
    (page("(CAN) Pharmacy Intern", "Grande Prairie"),
     "(CAN) Pharmacy Intern", "Calgary, AB", "/jobs/12345"),
    ("<title>Job Details | Careers</title><main>Find your next adventure</main>",
     "Software Engineering Intern", "Calgary, AB", "/jobs/12345"),
    ("<main>Copyright. Privacy policy.</main>",
     "Applications Engineering Intern", "Calgary, AB", "/jobs/12345"),
    (page("Water & Wastewater Engineering Student", "Mississauga"),
     "Water & Wastewater Engineering Student", "Mississauga, ON", "/go/Student-Jobs/2565100/"),
])
def test_audited_wrong_role_city_listing_and_shell_are_not_resolved(html, title, location, path):
    result = resolve_page(html, title=title, location=location, path=path)
    assert result.status != "resolved"


def test_observed_matching_posting_still_resolves():
    result = resolve_page(
        page("Software Engineering Intern", "Calgary"), title="Software Engineering Intern",
    )
    assert result.status == "resolved"


def test_observed_title_whitespace_and_regional_location_do_not_create_false_misses():
    # Saved bilingual analytics title contains NBSP/doubled spacing; the Suncor
    # saved location is province-only while the official page supplies city + AB.
    result = resolve_page(page("Software\u00a0 Engineering Intern", "Fort McMurray, AB, CAN"),
                          title="Software Engineering Intern", location="Alberta, Canada")
    assert result.status == "resolved"


@pytest.mark.parametrize("company", ["AMD", "AMD Canada", "Advanced Micro Devices, Inc."])
def test_reviewed_amd_aliases_share_search_and_destination_authority(company):
    assert jibe_career_site_for_company(company) is not None
    hint = ExternalPostingHint(source_url="", source_kind="linkedin", company=company)
    assert _official_candidate_url_matches_hint("https://careers.amd.com/careers-home/jobs/91367", hint)
    assert not _official_candidate_url_matches_hint("https://careers.amd.com.evil.test/jobs/91367", hint)


def test_failed_reviewed_candidates_get_bounded_relaxed_search_with_url_deduplication():
    calls = []
    fetched = []
    wrong = "https://careers.exampleengineering.com/jobs/12345"
    correct = "https://careers.exampleengineering.com/jobs/67890"

    class Reviewed:
        def search(self, hint, limit):
            return [SearchResult(title="Intern", url=wrong)]

    class Search:
        def search(self, query, limit):
            calls.append(query)
            return [SearchResult(title="Intern", url=wrong)] + (
                [SearchResult(title="Intern", url=correct)] if len(calls) > 1 else []
            )

    class Apply:
        def resolve(self, url, context):
            fetched.append(url)
            return AtsJob(source_url=url, title="Intern" if url == correct else "Senior Engineer",
                          raw_text="Title: Intern\nCompany: Example Engineering\nLocation: Calgary\nWork term: Summer 2027")

    result = PostingDiscoveryResolver(
        search_client=Search(), official_career_site_search_client=Reviewed(),
        apply_resolver=Apply(), allow_aggregator_fallback=False,
    ).resolve(ExternalPostingHint(source_url="", source_kind="linkedin", company="Example Engineering",
                                  title="Intern", location="Calgary", term="Summer 2027"))
    assert result.status == "resolved"
    assert fetched == [wrong, correct]
    assert len(calls) == 2
    assert "2027" not in calls[-1]
    assert "Calgary" not in calls[-1]


def test_official_shell_can_recover_from_independent_rendered_body():
    url = "https://careers-hexagonpositioning.icims.com/jobs/3086/software-engineering-intern/job"

    class Search:
        def search(self, query, limit):
            return [SearchResult(title="Software Engineering Intern", url=url)]

    class Apply:
        def resolve(self, url, context):
            return _job_from_generic_apply_page(url, "<main>Copyright. Privacy policy.</main>", context)

    class Render:
        def resolve(self, hint):
            assert hint.source_url == url
            assert hint.url_identity == "source_identity"
            # DOM-derived fields, with the reviewed subsidiary name observed on the page.
            return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
                source_url=url, raw_text="Page Title: Software Engineering Intern in Calgary | Careers\n"
                "Software Engineering Intern\nNovAtel Inc\n"
                "Location: Calgary, Alberta\nWork term: May 3, 2027 to May 2, 2028.\n"
                "Develop navigation software and test engineering systems with our team.",
                evidence_origin="browser_dom",
                rendered_html="<h1>Software Engineering Intern</h1><div>Company: NovAtel Inc</div>"
                "<div>Location: Calgary, Alberta</div><main>Work term: May 3, 2027 to May 2, 2028.\n"
                "Develop navigation software and test engineering systems with our team.</main>",
            ))

    result = PostingDiscoveryResolver(
        search_client=Search(), apply_resolver=Apply(), rendered_fallback_resolver=Render(),
        allow_aggregator_fallback=False,
    ).resolve(ExternalPostingHint(source_url="", source_kind="linkedin",
                                  company="Hexagon Autonomous Solutions", title="Software Engineering Intern",
                                  location="Calgary", term="Summer 2027"))
    assert result.status == "resolved"
    assert result.posting.source_url == url


@pytest.mark.parametrize("term", ["Winter 2027", "8 months"])
def test_same_role_and_city_cannot_override_observed_employment_period(term):
    from jobcockpit.ingestion.posting_discovery import _official_posting_confidence
    hint = ExternalPostingHint(source_url="", source_kind="source", company="Hexagon",
                               title="Software Engineering Intern", location="Calgary", term=term)
    # Observed Hexagon role runs May 2027-May 2028. Same employer/title/city
    # must not prove a different internship period or duration.
    raw = "Title: Software Engineering Intern\nCompany: Hexagon\nLocation: Calgary\n" \
          "Work term: May 3, 2027 to May 2, 2028."
    assert _official_posting_confidence(hint, raw) == 0
