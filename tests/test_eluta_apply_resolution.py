"""Observed Eluta JavaScript apply redirect through the CS Jobs shared resolver."""
import httpx
import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsProviderError
from jobcockpit.ingestion.ats_adapters.csjobs import CsJobsAdapter, CsJobsListing, CsJobsSite
from jobcockpit.ingestion.ats_adapters.eluta import ElutaAdapter
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.posting_discovery import PostingResolution, ResolvedExternalPosting


ELUTA = "https://www.eluta.ca/spl/ai-data-analyst-intern-example?imo=12"
EMPLOYER = "https://parcelco.wd3.myworkdayjobs.com/en-US/Careers/job/Montreal/AI-Data-Analyst-Intern_JR12345"
HTML = """<h1 class="job-title">AI Data Analyst Intern</h1>
<h5 class="employer-name">Parcelco Courier Canada Inc. / Parcelco</h5>
<span itemprop="jobLocation">Montréal, QC</span>
<div itemprop="description">Collect and analyze data as part of our internship.</div>
<a href="#" class="apply-btn" onclick="enavOpenNew('/direct/i?i=example&amp;imo=12')">Apply Now</a>"""
BODY = """Parcelco is a leading last mile carrier with delivery teams across Canada.
Interns contribute to real projects and gain practical experience in a growing team.
Responsibilities include collecting, cleaning and manipulating data from various teams,
conducting detailed analysis to identify trends, patterns and anomalies, and applying
machine learning techniques to develop predictive models. Communicate results clearly
to internal and external stakeholders. Collaborate with teams to understand data needs
and propose solutions. Contribute to data pipelines and analysis processes.
Qualifications include current study in computer science, statistics, applied mathematics
or a related field, strong Python programming skills, knowledge of data manipulation
libraries, artificial intelligence concepts, and strong collaboration and communication skills."""


@pytest.mark.parametrize("rendered", [False, True])
@pytest.mark.parametrize("problem", [None, "private", "title", "company", "location", "empty", "missing", "redirect_loop"])
def test_eluta_redirect_is_followed_and_destination_verified(allow_public_dns, rendered, problem):
    allow_public_dns({"www.eluta.ca", "parcelco.wd3.myworkdayjobs.com"})
    seen = []
    loaded = []
    html = HTML if problem != "missing" else HTML.replace("enavOpenNew", "unknownAction")

    class Loader:
        def load_page(self, hint):
            loaded.append(hint.source_url)
            return PostingResolution(status="resolved", posting=ResolvedExternalPosting(
                source_url=hint.source_url, raw_text="AI Data Analyst Intern", rendered_html=html,
            ))

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.startswith("/spl/"):
            return httpx.Response(403 if rendered else 200, text=html)
        if request.url.path == "/direct/i":
            destination = "http://127.0.0.1/private" if problem == "private" else EMPLOYER
            if problem == "redirect_loop":
                destination = ELUTA
            return httpx.Response(302, headers={"location": destination})
        if request.url.path.startswith("/wday/cxs/"):
            return httpx.Response(200, json={"jobPostingInfo": {
                "title": "Different Internship" if problem == "title" else "AI Data Analyst Intern",
                "jobReqId": "JR12345", "externalUrl": EMPLOYER,
                "location": "Vancouver, BC" if problem == "location" else "Canada, Quebec, Montreal",
                "jobDescription": ("" if problem == "empty" else BODY.replace(
                    "Parcelco", "Unrelated Corporation" if problem == "company" else "Parcelco")),
            }})
        raise AssertionError(f"Unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resolver = AtsApplyPageResolver(http_client=client, eluta_adapter_factory=lambda: ElutaAdapter(
            http_client=client, rendered_page_loader=Loader(),
        ))
        context = AtsPostingContext(company="Parcelco Courier Canada Inc. / Parcelco",
                                    role="AI Data Analyst Intern", location="Montréal, QC")
        if problem:
            with pytest.raises(AtsProviderError):
                resolver.resolve(ELUTA, context)
        else:
            adapter = CsJobsAdapter(http_client=client, apply_resolver=resolver)
            job = adapter._job_from_listing(CsJobsListing(
                "123", context.role, context.company, context.location, "2026-09-20", ELUTA,
            ), CsJobsSite("https://csjobs.ca"))
            assert job.source_url == EMPLOYER
            assert job.portal_source_url == ELUTA
            assert job.content_source_url == EMPLOYER
            assert job.source_archive and "jobPostingInfo" in job.source_archive.body.decode()
            assert "Req ID: JR12345" in job.raw_text
            assert "CS Jobs Apply URL:" in job.raw_text
            assert "CS Jobs" not in job.observed_raw_text
    assert loaded == ([ELUTA] if rendered else [])
    assert not any("127.0.0.1" in url for url in seen)


@pytest.mark.parametrize("error", [
    "invisible_session_preparation_cooldown",
    "invisible_session_preparation_needs_review",
    "invisible_session_preparation_failed",
])
def test_csjobs_does_not_render_eluta_again_after_failed_preparation(allow_public_dns, error):
    # The October 7 CS Jobs import retried seven Eluta links after an already
    # known preparation/cooldown failure from the first guarded load.
    allow_public_dns("www.eluta.ca")
    loaded, fallback = [], []

    class Loader:
        def load_page(self, hint):
            loaded.append(hint.source_url)
            return PostingResolution(status="needs_review", error="playwright_failed: " + error)

    class RenderedFallback:
        def resolve(self, hint):
            fallback.append(hint.source_url)
            return PostingResolution(status="needs_review", error="playwright_failed: " + error)

    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(403))) as client:
        apply = AtsApplyPageResolver(http_client=client, eluta_adapter_factory=lambda: ElutaAdapter(
            http_client=client, rendered_page_loader=Loader(),
        ))
        adapter = CsJobsAdapter(http_client=client, apply_resolver=apply,
                               rendered_page_resolver=RenderedFallback())
        with pytest.raises(AtsProviderError, match="required user verification") as caught:
            adapter._job_from_listing(CsJobsListing(
                "123", "AI Data Analyst Intern", "Parcelco", "Montreal", "2026-10-07", ELUTA,
            ), CsJobsSite("https://csjobs.ca"))
        assert caught.value.reason_code == error

    assert loaded == [ELUTA]
    assert fallback == []
