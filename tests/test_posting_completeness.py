"""Minimized retrieval regressions from the October 15 saved-source audit."""
import pytest

from jobcockpit.ingestion.posting_completeness import incomplete_posting_reason


@pytest.mark.parametrize(("text", "url", "reason"), [
    ("Jobs\nYou need to enable JavaScript to run this app.", "https://jobs.ashbyhq.com/example/id", "application_shell"),
    ("Recruitment\nPlease switch to a supported browser listed here, or some features may not work correctly.", "https://example.test/job/1", "application_shell"),
    ("Software Intern\nPost\n:\n:\n.\n:", "https://example.test/job/1", "application_shell"),
    ("Career Opportunities Loading… Loading… ©2021 Example. All rights reserved.", "https://example.test/job/1", "application_shell"),
    ("Join LinkedIn\nBy clicking Agree & Join, you agree to the LinkedIn User Agreement, Privacy Policy, and Cookie Policy.", "https://example.test/job/1", "login_or_challenge"),
    ("Sign in to see who you already know at Example\nSign in with Email", "https://example.test/job/1", "login_or_challenge"),
    ("Request Blocked\nYou have been blocked. If you believe this in error, please go to support.", "https://example.test/job/1", "login_or_challenge"),
    ("JavaScript is disabled\nIn order to continue, we need to verify that you're not a robot.", "https://example.test/job/1", "login_or_challenge"),
    ("Client Challenge\nPlease enable JavaScript to proceed.", "https://example.test/job/1", "login_or_challenge"),
    ("Engineer\n©2023 Example. All rights reserved. Privacy Policy Cookies Policy Cookie Setting Linked In Twitter", "https://example.test/job/1", "footer_only"),
    ("All rights reserved. PRIVACY|LEGAL|REQUIREMENTS|Artificial Intelligence\nOur site uses cookies and similar technologies to remember your visit. Set your preferences\nDeny Agree and proceed", "https://example.test/job/1", "footer_only"),
    ("All rights reserved. Cookies Policy. Your privacy matters to us. "
     "We use cookies to give you the best possible experience. By clicking Accept all, "
     "you allow us to enhance your browsing experience and understand how our site is used. "
     "Accept all Reject all Manage Preferences", "https://example.test/job/1", "footer_only"),
    ("Explore Example Jobs in Canada No results for \"${pageStateData.searchKeyword}\"", "https://example.test/job/1", "listing_page"),
    ("Company jobs and news", "https://interninsider.me/companies/example", "listing_page"),
    ("Company jobs", "https://bebee.com/ca/companies/example/jobs", "listing_page"),
    ("Example Internships (89 open) | Intern Insider\nBrowse 89 open internships at Example. Roles, locations, and how to apply, on Intern Insider.", "https://www.linkedin.com/jobs/view/123", "listing_page"),
    ("This job is no longer available. You may also Search for jobs. If you require accessibility assistance or accommodation for a disability, contact us. Privacy Careers Policy", "https://example.test/job/1", "expired_page_without_description"),
    ("Software Engineering Co-op\nAs a software co-op you will join our team developing high-tech cutti...\nJobs Login\nTHIS POSITION HAS BEEN CLOSED! PLEASE CHOOSE ONE OF THE OPTIONS BELOW:\nSearch Current Openings", "https://example.test/job/1", "expired_page_without_description"),
    ("Sign In\nCreate Profile\nThis job is not available.\nThank you for visiting our Careers page and viewing our current opportunities. Requesting an Accommodation. Equal Opportunity Employer.", "https://example.test/job/1", "expired_page_without_description"),
    ("IT Co-op\nSummary: Toronto ON full-time\nEluta URL: https://www.eluta.ca/spl/example\nDirect URL: https://www.eluta.ca/direct/i?i=example\nOverview", "https://example.test/job/1", "truncated_excerpt"),
    ("IT Co-op\nEluta URL: https://www.eluta.ca/spl/example\nThe successful student will join our engineering team focused...", "https://example.test/job/1", "truncated_excerpt"),
])
def test_observed_incomplete_sources(text, url, reason):
    assert incomplete_posting_reason(text, url) == reason


def test_substantive_posting_with_browser_notice_is_not_a_shell():
    text = "You need to enable JavaScript to run this app.\n" + "Build software and test systems with our engineering team. " * 20
    assert incomplete_posting_reason(text, "https://example.test/job/1") is None


def test_expired_posting_with_full_description_can_still_be_recovered():
    text = "This job posting is no longer available\nResponsibilities\n" + "Develop and maintain software solutions for corporate business operations. " * 20
    assert incomplete_posting_reason(text, "https://example.test/job/1") is None


def test_short_substantive_posting_is_not_rejected_by_length():
    assert incomplete_posting_reason("Software intern: Build Python tools and test APIs.", "https://example.test/job/1") is None


def test_workable_detail_url_does_not_require_job_words_in_preview():
    from jobcockpit.ingestion.posting_links import classify_posting_link
    assert classify_posting_link("https://apply.workable.com/example/j/ABC123/", "Example").classification == "probable_job_posting"


def test_company_listing_routes_to_individual_posting_discovery(monkeypatch):
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion import posting_links
    sentinel = object()
    monkeypatch.setattr(posting_links, "_prepare_external_link", lambda *args, **kwargs: sentinel)
    result = posting_links.prepare_posting_link(posting_links.PostingLinkContext(
        source_type="url", source_url="https://interninsider.me/companies/example", raw_text="Software intern at Example"),
        settings=Settings(), posting_resolver=object())
    assert result is sentinel


def test_generated_provenance_does_not_hide_login_screen():
    text = "Sign in to see who you already know at Example\n" + "Resolution Evidence: " + "Summary evidence " * 100
    assert incomplete_posting_reason(text) == "login_or_challenge"


def test_eluta_paragraph_starting_with_location_is_preserved():
    text = "Intern\nEluta URL: https://www.eluta.ca/spl/example\nLocation: Toronto " + "Build software and test systems with our engineering team. " * 20
    assert incomplete_posting_reason(text) is None


@pytest.mark.parametrize("rendered", [True, False])
def test_observed_shell_triggers_retrieval_without_reimporting_hint(monkeypatch, rendered):
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion import posting_links
    from jobcockpit.ingestion.ats_adapters.base import AtsJob
    from jobcockpit.ingestion.posting_completeness import IncompletePostingError

    url = "https://jobs.example.test/123"
    class Fetcher:
        def resolve(self, *args, **kwargs):
            return AtsJob(source_url=url, raw_text="Jobs\nYou need to enable JavaScript to run this app.")
    body = AtsJob(source_url=url, raw_text="Build software and test APIs as a software intern.")
    calls = []
    def fallback(*args):
        calls.append(args)
        return body if rendered else None
    monkeypatch.setattr(posting_links, "_fetch_rendered_direct_posting", fallback)
    context = posting_links.PostingLinkContext(source_type="url", source_url=url, raw_text="Old preview")
    if rendered:
        assert posting_links._fetch_direct_posting(context, Settings(), Fetcher()) == body
    else:
        with pytest.raises(IncompletePostingError):
            posting_links._fetch_direct_posting(context, Settings(), Fetcher())
    assert len(calls) == 1


@pytest.mark.parametrize("destination, truncated, accepted", [
    ("https://jobs.example.test/jobs/9480", False, True),
    ("https://jobs.example.test/jobs/9481", False, False),
    ("https://jobs.example.test/jobs/9480", True, False),
])
def test_direct_retrieval_uses_exact_url_when_saved_hints_are_wrong(monkeypatch, destination, truncated, accepted):
    from types import SimpleNamespace
    from jobcockpit.core.config import Settings
    from jobcockpit.ingestion import posting_links
    from jobcockpit.ingestion.posting_discovery import PostingResolution, ResolvedExternalPosting

    body = "Develop and maintain software solutions for corporate business operations. " * 12
    candidate = ResolvedExternalPosting(source_url=destination, raw_text=body,
        complete_body_text=body, evidence_origin="browser_dom", confidence=0.0,
        content_truncated=truncated)
    resolver = SimpleNamespace(resolve=lambda hint: PostingResolution(
        status="needs_review", error="playwright_low_confidence", candidates=(candidate,)))
    monkeypatch.setattr(posting_links, "default_rendered_page_resolver", lambda settings: resolver)
    monkeypatch.setattr(posting_links.UrlFetchPolicy, "check", lambda self, url: SimpleNamespace(allowed=True))
    context = posting_links.PostingLinkContext(source_type="url", source_url="https://jobs.example.test/jobs/9480",
        raw_text="Title: Corporate Services IT Student\nCompany: Airport, The\nLocation: Corporate Services IT Student")
    result = posting_links._fetch_rendered_direct_posting(context, Settings())
    assert (result is not None) is accepted
