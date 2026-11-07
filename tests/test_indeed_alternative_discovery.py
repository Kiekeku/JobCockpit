"""Discovery regressions from the September 6 Indeed audit (metadata, not parsing).

Grounding: five saved ATCO email contexts (three names for the Edmonton role),
three Teledyne contexts including a company-listing replacement. No private IDs.
"""
import pytest

from jobcockpit.ingestion.posting_discovery import PostingDiscoveryResolver
from jobcockpit.ingestion.posting_resolution import (
    ExternalPostingHint, PostingResolution, ResolvedExternalPosting, SearchResult,
)

INDEED = "https://ca.indeed.com/viewjob?jk=1234567890abcdef"
LINKEDIN = "https://www.linkedin.com/jobs/view/1234567890"


def hint():
    return ExternalPostingHint(source_url=INDEED, source_kind="indeed",
                               company="Example Energy", title="Data Analytics Co-op",
                               location="Edmonton")


def body():
    return "Title: Data Analytics Co-op\nCompany: Example Energy\nLocation: Edmonton\nDevelop analytics and reporting for customer operations."


class Render:
    def __init__(self, fail_linkedin=False):
        self.urls = []
        self.fail_linkedin = fail_linkedin

    def resolve(self, hint):
        self.urls.append(hint.source_url)
        if self.fail_linkedin and hint.source_url == LINKEDIN:
            return PostingResolution(status="needs_review", error="blocked_rendered_page")
        return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
            source_url=hint.source_url, raw_text=body(), confidence=0.9))


class Search:
    last_error = ""

    def __init__(self, url=LINKEDIN):
        self.queries = []
        self.url = url

    def search(self, query, limit):
        self.queries.append(query)
        if "site:linkedin.com/jobs/view" in query:
            return [SearchResult(title="Data Analytics Co-op", url=self.url,
                                 snippet="Example Energy Edmonton")]
        return []


def test_targeted_linkedin_search_precedes_indeed():
    search, render = Search(), Render()
    result = PostingDiscoveryResolver(search_client=search, rendered_fallback_resolver=render).resolve(hint())
    assert result.posting.source_url == LINKEDIN
    assert render.urls == [LINKEDIN]
    assert len(search.queries) <= 6
    from jobcockpit.ingestion.posting_links import source_resolution_for_resolution
    metadata = source_resolution_for_resolution(INDEED, result)
    assert metadata is not None
    assert metadata["kind"] == "aggregator_fallback"
    assert metadata["original_url"] == INDEED
    assert metadata["fallback_url"] == LINKEDIN


@pytest.mark.parametrize("page_title", [
    "Data Analytics Co-op | Example Energy | LinkedIn",
    "Example Energy hiring Data Analytics Co-op in Edmonton, Alberta, Canada | LinkedIn",
])
def test_linkedin_browser_capture_verifies_observed_page_title(page_title):
    # Minimized capture structures observed in saved ATCO and SAP snapshots.
    class BrowserRender(Render):
        def resolve(self, hint):
            return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
                source_url=hint.source_url, confidence=0.9,
                raw_text=f"Page Title: {page_title}\nSource URL: {hint.source_url}\n"
                "Source Evidence: unrelated search hint\n"
                "Division: Example Energy\nLocation: Edmonton\n"
                "Develop analytics and reporting for customer operations."))

    result = PostingDiscoveryResolver(search_client=Search(), rendered_fallback_resolver=BrowserRender()).resolve(hint())
    assert result.posting.source_url == LINKEDIN


def test_linkedin_browser_title_cannot_be_verified_by_source_hint():
    from jobcockpit.ingestion.posting_discovery import _official_posting_confidence
    raw = "Page Title: Unrelated Role | Another Employer | LinkedIn\nSource Evidence: " + body().replace("\n", " ")
    assert _official_posting_confidence(hint(), raw) == 0


def test_failed_linkedin_keeps_original_indeed_as_last_resort():
    render = Render(fail_linkedin=True)
    result = PostingDiscoveryResolver(search_client=Search(), rendered_fallback_resolver=render).resolve(hint())
    assert render.urls == [LINKEDIN, INDEED]
    assert result.posting.source_url == INDEED


def test_company_listing_never_becomes_replacement():
    render = Render()
    result = PostingDiscoveryResolver(search_client=Search("https://www.linkedin.com/company/example/jobs"),
                                      rendered_fallback_resolver=render).resolve(hint())
    assert result.posting.source_url == INDEED
    assert render.urls == [INDEED]


def test_provider_failure_does_not_trigger_more_searches():
    search, render = Search(), Render()
    search.last_error = "serper_rate_limited"
    result = PostingDiscoveryResolver(search_client=search, rendered_fallback_resolver=render).resolve(hint())
    assert len(search.queries) == 1
    assert result.official_search_outcome.reason_code == "serper_rate_limited"


def test_reviewed_atco_names_share_search_identity():
    from jobcockpit.integrations.official_careers import reviewed_company_aliases
    assert "atco" in reviewed_company_aliases("ATCO Energy Systems")
    assert "atco electric" in reviewed_company_aliases("ATCO Energy Systems")


@pytest.mark.parametrize("company", ["Foresters Financial", "Foresters Financial Services, Inc."])
def test_reviewed_foresters_legal_name_verifies_company_handoff(company):
    from jobcockpit.ingestion.posting_discovery import _official_posting_confidence

    # The observed Workday job uses this legal name in its JobPosting JSON-LD,
    # while the Indeed software/QA postings use either the brand or Services, Inc.
    hint = ExternalPostingHint(
        source_url=INDEED, source_kind="indeed", company=company,
        title="Software Engineer Co-op Student (4-months contract)",
    )
    observed = (
        "Title: Software Engineer Co-op Student (4-months contract)\n"
        "Company: The Independent Order of Foresters - Canada\n"
        "Build and test enterprise applications with an engineering team."
    )
    assert _official_posting_confidence(hint, observed) >= .8
    assert _official_posting_confidence(hint, observed.replace(
        "The Independent Order of Foresters - Canada", "Unrelated Employer",
    )) < .8


@pytest.mark.parametrize("observed_company", ["IG Machine & Fibers Ltd.", "IG Machine &amp; Fibers Ltd."])
def test_reviewed_iko_plant_name_verifies_company_handoff(observed_company):
    from jobcockpit.ingestion.posting_discovery import _official_posting_confidence

    # The observed Engineering Co-Op handoff names IKO in Indeed and the
    # description, but its Workday JSON-LD names the plant with an encoded &.
    hint = ExternalPostingHint(
        source_url=INDEED, source_kind="indeed", company="IKO Industries Ltd.",
        title="Engineering Co-Op", location="Ashcroft, BC",
    )
    observed = (
        f"Title: Engineering Co-Op\nCompany: {observed_company}\nLocation: Ashcroft, BC\n"
        "IKO Industries Ltd. manufactures roofing materials. Work on engineering projects."
    )
    assert _official_posting_confidence(hint, observed) >= .8
    # A brand mention in the description must not override an unrelated employer.
    assert _official_posting_confidence(hint, observed.replace(observed_company, "Unrelated Employer")) < .8
    assert _official_posting_confidence(hint, observed.replace("Title: Engineering Co-Op", "Title: Production Operator")) < .8
    assert _official_posting_confidence(hint, observed.replace("Location: Ashcroft, BC", "Location: Toronto, ON")) < .8


def test_employer_listing_leads_to_scoped_search_and_verified_detail():
    from jobcockpit.ingestion.ats_adapters.base import AtsJob
    landing = "https://careers.exampleenergy.com/careers"
    detail = "https://careers.exampleenergy.com/jobs/12345"
    queries = []

    class PortalSearch:
        def search(self, query, limit):
            queries.append(query)
            url = detail if "site:careers.exampleenergy.com" in query else landing
            return [SearchResult(title="Data Analytics Co-op", url=url)]

    class Apply:
        def resolve(self, url, context):
            return AtsJob(source_url=url, title="Data Analytics Co-op", raw_text=body())

    render = Render()
    result = PostingDiscoveryResolver(search_client=PortalSearch(), apply_resolver=Apply(),
                                      rendered_fallback_resolver=render).resolve(hint())
    assert result.posting.source_url == detail
    assert queries[-1] == "site:careers.exampleenergy.com Data Analytics Co-op"
    assert not render.urls


def test_listing_in_generic_results_is_filtered_before_replacement_selection():
    class Listings(Search):
        def search(self, query, limit):
            return [SearchResult(title="Data Analytics Co-op", snippet="Example Energy Edmonton",
                                 url="https://www.linkedin.com/company/example/jobs")]

    render = Render()
    result = PostingDiscoveryResolver(search_client=Listings(), rendered_fallback_resolver=render).resolve(hint())
    assert result.posting.source_url == INDEED
    assert render.urls == [INDEED]


def test_linkedin_result_must_verify_city_from_fetched_page():
    from dataclasses import replace

    class WrongCity(Render):
        def resolve(self, hint):
            result = super().resolve(hint)
            if hint.source_url == LINKEDIN:
                return replace(result, posting=replace(result.posting, raw_text=body().replace("Edmonton", "Calgary")))
            return result

    render = WrongCity()
    result = PostingDiscoveryResolver(search_client=Search(), rendered_fallback_resolver=render).resolve(hint())
    assert render.urls == [LINKEDIN, INDEED]
    assert result.posting.source_url == INDEED
