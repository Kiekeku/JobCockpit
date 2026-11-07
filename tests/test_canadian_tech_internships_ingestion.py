import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobcockpit.services.workflows import failed_link_exclusion_urls, legacy_portal_source_url
from jobcockpit.db.models import JobIngestionItem, JobIngestionRun, JobPosting, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsLinkFailure,
    AtsProviderError,
    AtsRecordedLinkFailureError,
    classify_link_failure,
)
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import (
    CanadianTechInternshipsAdapter,
    InternshipListing,
)
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers
from jobcockpit.ingestion.posting_discovery import (
    ChromeCdpRenderedPageResolver,
    CodexCliRenderedPageResolver,
    PostingResolution,
    PrioritizedRenderedPageResolver,
    ResolvedExternalPosting,
)
from jobcockpit.ingestion.posting_links import posting_url_identity
from jobcockpit.ingestion.role_interest import match_title_interest


README_URL = (
    "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2026/main/README.md"
)
GITHUB_URL = "https://github.com/negarprh/Canadian-Tech-Internships-2026?tab=readme-ov-file"
MAGNA_APPLY_URL = (
    "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Milton-Ontario-CA/"
    "Junior-Full-Stack-Developer-Coop_R00244793"
)
MAINTAINX_APPLY_URL = "https://job-boards.greenhouse.io/maintainx/jobs/gh5146022007x"
SMARTRECRUITERS_APPLY_URL = (
    "https://jobs.smartrecruiters.com/UniversityHealthNetwork/sr744000127738917x"
)
ORACLE_APPLY_URL = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/J35268"
ORACLE_REST_URL = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22J35268%22,siteNumber=CX_1"
CANADIAN_TECH_TEST_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "wd3.myworkdaysite.com",
    "job-boards.greenhouse.io",
    "jobs.smartrecruiters.com",
    "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com",
    "jobs.example.com",
}


def test_ats_fetch_request_identifies_excluded_source_urls():
    request = AtsFetchRequest(
        source_url=GITHUB_URL,
        excluded_source_urls=frozenset({MAINTAINX_APPLY_URL}),
    )

    assert request.excludes_source_url(MAINTAINX_APPLY_URL)
    assert request.excludes_source_url(f"{MAINTAINX_APPLY_URL}?utm_source=github-board")
    assert not request.excludes_source_url(f"{MAINTAINX_APPLY_URL}?department=engineering")
    assert not request.excludes_source_url("https://example.com/jobs/other")

    encoded_url = f"{MAINTAINX_APPLY_URL}?redirect=engineering%20jobs&signature=a%2Fb"
    encoded_request = AtsFetchRequest(
        source_url=GITHUB_URL,
        excluded_source_urls=frozenset({encoded_url}),
    )
    assert encoded_request.excludes_source_url(f"{encoded_url}&utm_source=github-board")
    assert encoded_request.excluded_source_urls == frozenset({encoded_url})


def test_ats_fetch_request_supports_shared_canonical_exclusions():
    canonical_urls = {
        "https://interac.wd3.myworkdayjobs.com/Interac/job/Toronto/Software-Development-Intern_REQ-2272",
        "https://www.linkedin.com/jobs/view/4438093173",
        "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54",
        ("https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/11154"),
    }
    request = AtsFetchRequest(
        source_url=GITHUB_URL,
        excluded_source_urls=frozenset(canonical_urls),
        source_url_normalizer=posting_url_identity,
    )

    assert request.excludes_source_url(
        "https://interac.wd3.myworkdayjobs.com/en-US/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272?source=LinkedIn"
    )
    assert request.excludes_source_url(
        "https://ca.linkedin.com/jobs/view/platform-engineer-at-acme-4438093173/?trk=public_jobs"
    )
    assert request.excludes_source_url(
        "https://www.indeed.com/rc/clk/dl?jk=3CAB9868C348DD54&from=ja"
    )
    assert request.excludes_source_url(
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/fr/"
        "sites/CX/requisitions/preview/J11154?source=board"
    )


def test_ats_fetch_request_applies_shared_listing_interest_filter():
    profile_data = {
        "target_role_include_keywords": ["software"],
        "target_role_exclude_keywords": ["accounting"],
    }
    rejected_url = "https://careers.atco.com/global/en/job/201/Accounting-Intern"
    request = AtsFetchRequest(
        source_url=GITHUB_URL,
        listing_interest_matcher=lambda title: match_title_interest(title, profile_data),
        source_url_normalizer=posting_url_identity,
    )

    assert request.allows_listing_candidate(MAINTAINX_APPLY_URL, "Software Developer Intern")
    assert not request.allows_listing_candidate(
        rejected_url,
        "Accounting Intern",
    )
    assert request.stats.interest_skips[0].title == "Accounting Intern"
    assert request.stats.interest_skips[0].source_url == rejected_url

    skipped_request = AtsFetchRequest(source_url=GITHUB_URL)
    assert skipped_request.allows_listing_candidate(
        "https://jobs.example.com/accounting",
        "Accounting Intern",
    )


def readme_fixture(*rows: str) -> str:
    body = "\n".join(rows)
    return f"""
# Canadian Tech Internships - 2026

<!-- BEGIN:INTERNSHIPS_TABLE -->
| Company | Role | Location | Apply | Date Posted |
|--------|------|----------|:-----:|--------------|
{body}
<!-- END:INTERNSHIPS_TABLE -->
"""


def apply_cell(url: str) -> str:
    return f"[![Apply](https://img.shields.io/badge/-Apply-blue?style=for-the-badge)]({url})"


def test_canadian_tech_internships_follows_apply_link_to_workday_detail(
    allow_public_dns, monkeypatch
):
    requests_seen: list[httpx.Request] = []
    readme = readme_fixture(
        f"| Magna | Junior Full Stack Developer Co-op | Milton, ON | {apply_cell(MAGNA_APPLY_URL)} | May 30, 2026 |",
        "| RBC | Machine Learning Software Engineer Co-op | Toronto, ON | Closed | May 23, 2026 |",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        if (
            request.url.path
            == "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793"
        ):
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Junior Full Stack Developer Coop",
                        "jobReqId": "R00244793",
                        "location": "Milton, Ontario, Canada",
                        "timeType": "Full time",
                        "postedOn": "Posted May 30, 2026",
                        "externalUrl": MAGNA_APPLY_URL,
                        "jobDescription": "<p>Build React services and Python APIs.</p>",
                    }
                },
            )
        return httpx.Response(404)

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url=GITHUB_URL, search_text="full stack", limit=2)
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == MAGNA_APPLY_URL
    assert "Junior Full Stack Developer Coop" in jobs[0].raw_text
    assert "Company: Magna" in jobs[0].raw_text
    assert "Req ID: R00244793" in jobs[0].raw_text
    assert "Build React services and Python APIs." in jobs[0].raw_text
    assert "GitHub List Role: Junior Full Stack Developer Co-op" in jobs[0].raw_text
    assert "GitHub List Date Posted: May 30, 2026" in jobs[0].raw_text
    assert [str(request.url) for request in requests_seen] == [
        README_URL,
        "https://wd3.myworkdaysite.com/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793",
    ]


def test_github_collection_applies_shared_interest_filter_before_apply_fetch(allow_public_dns):
    requests_seen: list[httpx.Request] = []
    accounting_url = "https://jobs.example.com/accounting"
    readme = readme_fixture(
        f"| FinanceCo | Accounting Intern | Calgary, AB | {apply_cell(accounting_url)} | May 30, 2026 |",
        f"| MaintainX | Software Development Intern | Montreal, QC | {apply_cell(MAINTAINX_APPLY_URL)} | May 26, 2026 |",
    )
    job_posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Development Intern",
        "hiringOrganization": {"name": "MaintainX"},
        "description": "Ship TypeScript product features.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        if str(request.url) == accounting_url:
            raise AssertionError("interest-filtered apply page should not be fetched")
        if str(request.url) == MAINTAINX_APPLY_URL:
            return httpx.Response(
                200,
                text=(
                    "<html><head>"
                    f'<script type="application/ld+json">{json.dumps(job_posting)}</script>'
                    "</head><body></body></html>"
                ),
            )
        return httpx.Response(404)

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    fetch_request = AtsFetchRequest(
        source_url=GITHUB_URL,
        limit=1,
        listing_interest_matcher=lambda title: match_title_interest(
            title,
            {"target_role_include_keywords": ["software"]},
        ),
    )

    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.source_url for job in jobs] == [MAINTAINX_APPLY_URL]
    assert [str(request.url) for request in requests_seen] == [README_URL, MAINTAINX_APPLY_URL]
    assert fetch_request.stats.interest_skips[0].title == "Accounting Intern"


def test_canadian_tech_internships_uses_json_ld_for_non_workday_apply_pages(
    allow_public_dns, monkeypatch
):
    requests_seen: list[httpx.Request] = []
    child_company = "\u21b3"
    readme = readme_fixture(
        f"| MaintainX | Software Development Intern | Montreal, QC | {apply_cell(MAINTAINX_APPLY_URL)} | May 26, 2026 |",
        "| RBC | Machine Learning Software Engineer Co-op | Toronto, ON | Closed | May 23, 2026 |",
        f"| Ciena | Waveserver Software Developer Co-op | Ottawa, ON | {apply_cell('https://jobs.example.com/ciena/waveserver')} | May 30, 2026 |",
        f"| {child_company} | ASIC Verification Intern | Ottawa, ON | {apply_cell('https://jobs.example.com/ciena/asic')} | May 30, 2026 |",
    )
    job_posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Development Intern",
        "hiringOrganization": {"name": "MaintainX"},
        "jobLocation": {
            "address": {
                "addressLocality": "Montreal",
                "addressRegion": "QC",
                "addressCountry": "CA",
            }
        },
        "datePosted": "2026-05-26",
        "description": "<p>Ship TypeScript product features.</p>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        if str(request.url) == MAINTAINX_APPLY_URL:
            return httpx.Response(
                200,
                text=(
                    "<html><head>"
                    f'<script type="application/ld+json">{json.dumps(job_posting)}</script>'
                    "</head><body>Fallback page text</body></html>"
                ),
            )
        if str(request.url) == "https://jobs.example.com/ciena/asic":
            return httpx.Response(
                200,
                text="""
                <html>
                  <head><title>ASIC Verification Intern</title></head>
                  <body><main>Verify switching ASICs with Python automation.</main></body>
                </html>
                """,
            )
        return httpx.Response(404)

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)
    )
    child_jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url=README_URL, search_text="asic", limit=1)
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == MAINTAINX_APPLY_URL
    assert "Software Development Intern" in jobs[0].raw_text
    assert "Company: MaintainX" in jobs[0].raw_text
    assert "Ship TypeScript product features." in jobs[0].raw_text
    assert "Closed" not in jobs[0].raw_text
    assert len(child_jobs) == 1
    assert "GitHub List Company: Ciena" in child_jobs[0].raw_text
    assert "ASIC Verification Intern" in child_jobs[0].raw_text
    assert [str(request.url) for request in requests_seen] == [
        README_URL,
        MAINTAINX_APPLY_URL,
        README_URL,
        "https://jobs.example.com/ciena/asic",
    ]


def test_canadian_tech_internships_handles_common_non_workday_ats_pages(
    allow_public_dns, monkeypatch
):
    requests_seen: list[httpx.Request] = []
    readme = readme_fixture(
        f"| MaintainX | Software Development Intern | Montreal, QC | {apply_cell(MAINTAINX_APPLY_URL)} | May 26, 2026 |",
        f"| University Health Network | Junior Developer Intern | Toronto, ON | {apply_cell(SMARTRECRUITERS_APPLY_URL)} | May 21, 2026 |",
        f"| Nokia | Embedded C/C++ Software Developer Co-op/Intern | Ottawa, ON | {apply_cell(ORACLE_APPLY_URL)} | May 20, 2026 |",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        if str(request.url) == MAINTAINX_APPLY_URL:
            return httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>Job Application for Software Development Intern at MaintainX</title>
                    <meta property="og:title" content="Software Development Intern" />
                    <meta property="og:description" content="Montreal, Quebec" />
                  </head>
                  <body>
                    <main>
                      <h1>Software Development Intern</h1>
                      <section>Build product features with TypeScript and React.</section>
                    </main>
                  </body>
                </html>
                """,
            )
        if str(request.url) == SMARTRECRUITERS_APPLY_URL:
            return httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>University Health Network Junior Developer (Intern), Analytics & Insights | SmartRecruiters</title>
                    <meta name="description" content="Junior developer internship in analytics and insights." />
                  </head>
                  <body>
                    <main>
                      <h1>Junior Developer (Intern), Analytics &amp; Insights</h1>
                      <section>Develop reporting apps with SQL and Python.</section>
                    </main>
                  </body>
                </html>
                """,
            )
        if str(request.url) == ORACLE_APPLY_URL:
            return httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>Nokia</title>
                    <meta property="og:title" content="Embedded C/C++ SW Developer Co-op/Intern" />
                    <meta property="og:description" content="Gain hands-on experience developing embedded software." />
                    <meta property="og:site_name" content="Nokia" />
                  </head>
                  <body><div id="app"></div></body>
                </html>
                """,
            )
        return httpx.Response(404)

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    fetch_request = AtsFetchRequest(source_url=README_URL, limit=3)
    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.source_url for job in jobs] == [
        MAINTAINX_APPLY_URL,
        SMARTRECRUITERS_APPLY_URL,
    ]
    assert len(fetch_request.stats.link_failures) == 1
    assert "Software Development Intern" in jobs[0].raw_text
    assert "Build product features with TypeScript and React." in jobs[0].raw_text
    assert "Junior Developer (Intern), Analytics & Insights" in jobs[1].raw_text
    assert "Develop reporting apps with SQL and Python." in jobs[1].raw_text
    assert [str(request.url) for request in requests_seen] == [
        README_URL,
        MAINTAINX_APPLY_URL,
        SMARTRECRUITERS_APPLY_URL,
        ORACLE_REST_URL,
        ORACLE_APPLY_URL,
    ]


def test_canadian_tech_internships_rejects_private_apply_link_before_fetch(
    allow_public_dns, monkeypatch
):
    requests_seen: list[httpx.Request] = []
    private_apply_url = "http://169.254.169.254/latest/meta-data/"
    readme = readme_fixture(
        f"| UnsafeCo | Software Intern | Toronto, ON | {apply_cell(private_apply_url)} | May 30, 2026 |"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        raise AssertionError(f"Unexpected Canadian Tech Internships adapter fetch: {request.url}")

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)
    assert adapter.fetch_jobs(request) == []
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].reason == (
        "Apply URL is blocked by policy: private_network_blocked"
    )

    assert [str(request.url) for request in requests_seen] == [README_URL]


def test_canadian_tech_internships_records_blocked_link_and_imports_later_valid_row(
    allow_public_dns, monkeypatch
):
    private_apply_url = "http://169.254.169.254/latest/meta-data/"
    valid_apply_url = "https://jobs.example.com/new/software-intern"
    readme = readme_fixture(
        f"| UnsafeCo | Software Intern | Toronto, ON | {apply_cell(private_apply_url)} | May 30, 2026 |",
        f"| NewCo | Software Engineer Intern | Toronto, ON | {apply_cell(valid_apply_url)} | May 31, 2026 |",
    )
    resolved: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            self.url_policy = url_policy

        def resolve(self, apply_url: str, context):
            resolved.append(apply_url)
            from jobcockpit.ingestion.ats_helpers import enforce_url_policy

            enforce_url_policy(self.url_policy, apply_url, label="Apply")
            return AtsJob(
                source_url=apply_url,
                raw_text=(f"{context.role}\nCompany: {context.company}\nBuild software products."),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=2)
    jobs = adapter.fetch_jobs(request)
    assert [job.source_url for job in jobs] == [valid_apply_url]
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].reason == (
        "Apply URL is blocked by policy: private_network_blocked"
    )

    assert sorted(resolved) == sorted([private_apply_url, valid_apply_url])


def test_canadian_tech_internships_delegates_apply_links_to_shared_resolver(monkeypatch):
    readme = readme_fixture(
        f"| MaintainX | Software Development Intern | Montreal, QC | {apply_cell(MAINTAINX_APPLY_URL)} | May 26, 2026 |"
    )
    resolved: list[tuple[str, str, str]] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            resolved.append((apply_url, context.source_label, context.role))
            return AtsJob(
                source_url="https://jobs.example.com/canonical/software-intern",
                raw_text="Software Development Intern\nCompany: MaintainX\nShip TypeScript product features.",
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://jobs.example.com/canonical/software-intern"
    assert jobs[0].portal_source_url == MAINTAINX_APPLY_URL
    assert resolved == [(MAINTAINX_APPLY_URL, "GitHub List", "Software Development Intern")]


def test_canadian_tech_internships_continues_after_failed_apply_link(monkeypatch):
    failing_apply_url = "https://jobs.example.com/failing/software-intern"
    new_apply_url = "https://jobs.example.com/new/software-intern"
    readme = readme_fixture(
        f"| BrokenCo | Software Intern | Toronto, ON | {apply_cell(failing_apply_url)} | May 26, 2026 |",
        f"| NewCo | Software Engineer Intern | Toronto, ON | {apply_cell(new_apply_url)} | May 27, 2026 |",
    )
    resolved: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            resolved.append(apply_url)
            if apply_url == failing_apply_url:
                raise AtsProviderError(502, "Apply page request failed")
            return AtsJob(
                source_url=apply_url,
                raw_text=(f"{context.role}\nCompany: {context.company}\nBuild software products."),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=2)

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [new_apply_url]
    assert request.stats.links_seen_count == 2
    assert resolved == [failing_apply_url, new_apply_url]


def test_canadian_tech_internships_returns_empty_batch_when_scanned_links_all_fail(monkeypatch):
    failing_apply_url = "https://jobs.example.com/failing/software-intern"
    readme = readme_fixture(
        f"| BrokenCo | Software Intern | Toronto, ON | {apply_cell(failing_apply_url)} | May 26, 2026 |",
    )
    resolved: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            resolved.append(apply_url)
            raise AtsProviderError(502, "Apply page request failed")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)

    jobs = adapter.fetch_jobs(request)

    assert jobs == []
    assert request.stats.links_seen_count == 1
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].source_url == failing_apply_url
    assert request.stats.link_failures[0].category == "technical"
    assert resolved == [failing_apply_url]


def test_canadian_tech_internships_uses_cdp_after_workday_403(monkeypatch):
    workday_url = "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Software-Developer-Intern_R123?source=LinkedIn"
    readme = readme_fixture(
        f"| Example | Software Developer Intern | Toronto, ON | "
        f"{apply_cell(workday_url)} | May 26, 2026 |",
    )
    rendered_hints = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            request = httpx.Request("GET", apply_url)
            response = httpx.Response(403, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as cause:
                error = AtsProviderError(502, "Workday detail request failed")
                error.__cause__ = cause
                raise error
            raise AssertionError("Expected the mocked Workday request to fail")

    class FakeRenderedResolver:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=workday_url,
                    raw_text=(
                        "Software Developer Intern\n"
                        "Company: Example\n"
                        "Location: Toronto, Ontario\n"
                        "Build customer-facing software with Python."
                    ),
                    confidence=0.95,
                    evidence="Rendered through Chrome CDP",
                ),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rendered_page_resolver=FakeRenderedResolver(),
    )
    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)

    jobs = adapter.fetch_jobs(request)

    assert len(jobs) == 1
    assert jobs[0].source_url == workday_url
    assert jobs[0].portal_source_url == workday_url
    assert "Build customer-facing software with Python." in jobs[0].raw_text
    assert "GitHub List Company: Example" in jobs[0].raw_text
    assert "GitHub List Role: Software Developer Intern" in jobs[0].raw_text
    assert request.stats.link_failures == []
    assert len(rendered_hints) == 1
    assert rendered_hints[0].source_kind == "workday"
    assert rendered_hints[0].source_url == workday_url
    assert rendered_hints[0].url_identity == "path"
    assert rendered_hints[0].identity_query_groups == ()


@pytest.mark.parametrize(
    ("apply_url", "expected_identity_groups"),
    [
        (
            "https://www.d2l.com/careers/jobs/?job_id=7890294&gh_jid=7890294",
            (("job_id", "gh_jid"),),
        ),
        (
            "https://jobs.example.com/opening?vacancy=7890294",
            (("vacancy",),),
        ),
    ],
)
@pytest.mark.parametrize('metadata_preview', [False, True])
def test_canadian_tech_internships_uses_cdp_after_generic_apply_page_403(
    monkeypatch,
    apply_url,
    expected_identity_groups,
    metadata_preview,
):
    readme = readme_fixture(
        f"| D2L | Software Developer Co-op | Kitchener, ON | "
        f"{apply_cell(apply_url)} | Jul 28, 2026 |",
    )
    rendered_hints = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, resolved_apply_url: str, context):
            if metadata_preview:
                from jobcockpit.ingestion.ats_handlers.apply import MetadataOnlyPostingError
                raise MetadataOnlyPostingError(502, 'Apply page contains only a metadata preview')
            request = httpx.Request("GET", resolved_apply_url)
            response = httpx.Response(403, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as cause:
                error = AtsProviderError(502, "Apply page request failed")
                error.__cause__ = cause
                raise error
            raise AssertionError("Expected the mocked apply-page request to fail")

    class FakeRenderedResolver:
        def resolve(self, hint):
            rendered_hints.append(hint)
            return PostingResolution(
                status="resolved",
                posting=ResolvedExternalPosting(
                    source_url=apply_url,
                    raw_text=(
                        "Software Developer Co-op\n"
                        "Company: D2L\n"
                        "Location: Kitchener, Ontario\n"
                        "Build reliable learning software."
                    ),
                    confidence=0.95,
                    evidence="Rendered through Chrome CDP",
                ),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rendered_page_resolver=FakeRenderedResolver(),
    )
    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)

    jobs = adapter.fetch_jobs(request)

    assert len(jobs) == 1
    assert jobs[0].source_url == apply_url
    assert jobs[0].portal_source_url == apply_url
    assert "Build reliable learning software." in jobs[0].raw_text
    assert request.stats.link_failures == []
    assert len(rendered_hints) == 1
    assert rendered_hints[0].source_kind == "generic"
    assert rendered_hints[0].source_url == apply_url
    assert rendered_hints[0].url_identity == "path_and_query"
    assert rendered_hints[0].identity_query_groups == expected_identity_groups


def test_canadian_tech_internships_marks_cdp_expired_workday_page_permanent(monkeypatch):
    workday_url = (
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/"
        "Expired-Software-Developer-Intern_R122"
    )
    readme = readme_fixture(
        f"| Example | Expired Software Developer Intern | Toronto, ON | "
        f"{apply_cell(workday_url)} | May 20, 2026 |",
    )
    rendered_calls: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            request = httpx.Request("GET", apply_url)
            response = httpx.Response(403, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as cause:
                error = AtsProviderError(502, "Workday detail request failed")
                error.__cause__ = cause
                raise error
            raise AssertionError("Expected the mocked Workday request to fail")

    class FakeRenderedResolver:
        def resolve(self, hint):
            rendered_calls.append(hint.source_url)
            return PostingResolution(
                status="needs_review",
                candidates=(
                    ResolvedExternalPosting(
                        source_url=workday_url,
                        raw_text=(
                            "Careers\nThe page you are looking for doesn't exist.\nSearch for Jobs"
                        ),
                        confidence=0.45,
                        evidence="Rendered through Chrome CDP",
                    ),
                ),
                error="chrome_cdp_low_confidence",
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rendered_page_resolver=FakeRenderedResolver(),
    )
    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)

    jobs = adapter.fetch_jobs(request)

    assert jobs == []
    assert rendered_calls == [workday_url]
    assert len(request.stats.link_failures) == 1
    failure = request.stats.link_failures[0]
    assert failure.source_url == workday_url
    assert failure.category == "permanent"
    assert failure.upstream_status_code == 410
    assert failure.reason == "Workday posting expired or was removed"


def test_canadian_tech_internships_does_not_close_from_mismatched_workday_candidate():
    workday_url = (
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/"
        "Software-Developer-Intern_R122"
    )
    wrong_url = "https://example.wd5.myworkdayjobs.com/Careers/search"

    class FakeRenderedResolver:
        def resolve(self, _hint):
            return PostingResolution(
                status="needs_review",
                candidates=(
                    ResolvedExternalPosting(
                        source_url=wrong_url,
                        raw_text=(
                            "Careers\nThe page you are looking for doesn't exist.\n"
                            "Search for Jobs"
                        ),
                    ),
                ),
                error="chrome_cdp_url_mismatch",
            )

    adapter = CanadianTechInternshipsAdapter(
        rendered_page_resolver=FakeRenderedResolver(),
    )
    listing = InternshipListing(
        company="Example",
        role="Software Developer Intern",
        location="Toronto, ON",
        apply_url=workday_url,
        date_posted="May 20, 2026",
        readme_url=GITHUB_URL,
    )

    with pytest.raises(AtsRecordedLinkFailureError) as caught:
        adapter._job_from_rendered_listing(listing)

    assert caught.value.failure.category == "technical"
    assert caught.value.failure.upstream_status_code is None


def test_canadian_tech_internships_marks_cdp_timeout_retryable(monkeypatch):
    workday_url = (
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto/Software-Developer-Intern_R124"
    )
    readme = readme_fixture(
        f"| Example | Software Developer Intern | Toronto, ON | "
        f"{apply_cell(workday_url)} | May 27, 2026 |",
    )

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            request = httpx.Request("GET", apply_url)
            response = httpx.Response(403, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as cause:
                error = AtsProviderError(502, "Workday detail request failed")
                error.__cause__ = cause
                raise error
            raise AssertionError("Expected the mocked Workday request to fail")

    class FakeRenderedResolver:
        def resolve(self, hint):
            assert hint.source_url == workday_url
            return PostingResolution(
                status="needs_review",
                error="chrome_cdp_timeout",
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rendered_page_resolver=FakeRenderedResolver(),
    )
    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=1)

    jobs = adapter.fetch_jobs(request)

    assert jobs == []
    assert len(request.stats.link_failures) == 1
    failure = request.stats.link_failures[0]
    assert failure.source_url == workday_url
    assert failure.category == "retryable"
    assert failure.reason == "chrome_cdp_timeout"


def test_canadian_tech_internships_continues_after_bad_workday_apply_link(monkeypatch):
    bad_workday_url = "https://bad.wd5.myworkdayjobs.com/Careers"
    new_apply_url = "https://jobs.example.com/new/software-intern"
    readme = readme_fixture(
        f"| BadWorkday | Software Intern | Toronto, ON | {apply_cell(bad_workday_url)} | May 26, 2026 |",
        f"| NewCo | Software Engineer Intern | Toronto, ON | {apply_cell(new_apply_url)} | May 27, 2026 |",
    )
    resolved: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            resolved.append(apply_url)
            if apply_url == bad_workday_url:
                raise AtsProviderError(400, "Workday apply URL must be a job detail URL")
            return AtsJob(
                source_url=apply_url,
                raw_text=(f"{context.role}\nCompany: {context.company}\nBuild software products."),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        return httpx.Response(404)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = AtsFetchRequest(source_url=README_URL, search_text="software", limit=2)

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [new_apply_url]
    assert request.stats.links_seen_count == 2
    assert resolved == [bad_workday_url, new_apply_url]


def test_canadian_tech_internships_skips_previously_imported_apply_urls_before_resolving(
    monkeypatch,
):
    new_apply_url = "https://jobs.example.com/new/software-intern"
    readme = readme_fixture(
        f"| MaintainX | Software Development Intern | Montreal, QC | {apply_cell(MAINTAINX_APPLY_URL)} | May 26, 2026 |",
        f"| NewCo | Software Engineer Intern | Toronto, ON | {apply_cell(new_apply_url)} | May 27, 2026 |",
    )
    resolved: list[str] = []

    class FakeApplyResolver:
        def __init__(self, *, http_client, url_policy) -> None:
            assert http_client is not None
            assert url_policy is not None

        def resolve(self, apply_url: str, context):
            resolved.append(apply_url)
            return AtsJob(
                source_url=apply_url,
                raw_text=f"{context.role}\nCompany: {context.company}\nBuild software products.",
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == README_URL:
            return httpx.Response(200, text=readme)
        raise AssertionError(f"Previously imported apply URL should not be fetched: {request.url}")

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeApplyResolver,
    )
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    request = AtsFetchRequest(
        source_url=README_URL,
        search_text="software",
        limit=1,
        excluded_source_urls=frozenset({MAINTAINX_APPLY_URL}),
    )

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [new_apply_url]
    assert request.stats.links_seen_count == 1
    assert resolved == [new_apply_url]


def test_ats_link_failure_classifies_upstream_and_technical_errors():
    request = httpx.Request("GET", "https://jobs.example.com/posting")

    for status_code, expected_category in [
        (404, "permanent"),
        (410, "permanent"),
        (429, "retryable"),
        (503, "retryable"),
    ]:
        response = httpx.Response(status_code, request=request)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            error = AtsProviderError(502, "ATS detail request failed")
            error.__cause__ = cause
        failure = classify_link_failure(str(request.url), error)
        assert failure.category == expected_category
        assert failure.upstream_status_code == status_code

    timeout = httpx.ConnectTimeout("Connection timed out", request=request)
    assert classify_link_failure(str(request.url), timeout).category == "retryable"
    assert (
        classify_link_failure(
            str(request.url),
            AtsProviderError(502, "Detail response did not include a title"),
        ).category
        == "technical"
    )


def test_failed_link_exclusions_keep_permanent_and_immediately_retry_other_failures(
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory()
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    init_database(settings)
    permanent_url = "https://jobs.example.com/permanently-gone"
    expired_retry_url = "https://jobs.example.com/retry-again"
    recent_technical_url = "https://jobs.example.com/parser-failed"
    with session_scope(settings) as db:
        db.add_all(
            [
                JobIngestionRun(
                    ingestion_run_id="ingestrun_permanent",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed",
                    started_at=now - timedelta(days=30),
                    request_payload={
                        "link_failures": [
                            {
                                "source_url": permanent_url,
                                "category": "permanent",
                                "reason": "Posting expired",
                                "upstream_status_code": 410,
                            }
                        ]
                    },
                ),
                JobIngestionRun(
                    ingestion_run_id="ingestrun_retryable",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed",
                    started_at=now - timedelta(days=2),
                    request_payload={
                        "link_failures": [
                            {
                                "source_url": expired_retry_url,
                                "category": "retryable",
                                "reason": "Server unavailable",
                                "upstream_status_code": 503,
                            }
                        ]
                    },
                ),
                JobIngestionRun(
                    ingestion_run_id="ingestrun_technical",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed",
                    started_at=now - timedelta(hours=2),
                    request_payload={
                        "link_failures": [
                            {
                                "source_url": recent_technical_url,
                                "category": "technical",
                                "reason": "Missing title",
                                "upstream_status_code": None,
                            }
                        ]
                    },
                ),
            ]
        )
        db.flush()

        exclusions = failed_link_exclusion_urls(
            db,
            provider="canadian_tech_internships",
            source_url=GITHUB_URL,
            now=now,
        )

    assert permanent_url in exclusions
    assert recent_technical_url not in exclusions
    assert expired_retry_url not in exclusions


def test_failed_link_exclusions_include_persisted_item_failures(
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory()
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    init_database(settings)
    permanent_url = "https://jobs.example.com/blocked-by-policy"
    recent_retryable_url = "https://jobs.example.com/retry-later"
    expired_retryable_url = "https://jobs.example.com/retry-now"
    with session_scope(settings) as db:
        db.add_all(
            [
                JobIngestionRun(
                    ingestion_run_id="ingestrun_item_permanent",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed_with_warnings",
                    started_at=now - timedelta(days=30),
                ),
                JobIngestionRun(
                    ingestion_run_id="ingestrun_item_recent",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed_with_warnings",
                    started_at=now - timedelta(hours=2),
                ),
                JobIngestionRun(
                    ingestion_run_id="ingestrun_item_expired",
                    provider="canadian_tech_internships",
                    source_url=GITHUB_URL,
                    status="completed_with_warnings",
                    started_at=now - timedelta(days=2),
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                JobIngestionItem(
                    ingestion_item_id="ingestitem_permanent",
                    ingestion_run_id="ingestrun_item_permanent",
                    source_url=permanent_url,
                    canonical_url=permanent_url,
                    status="blocked_by_url_policy",
                    retryable=False,
                ),
                JobIngestionItem(
                    ingestion_item_id="ingestitem_recent",
                    ingestion_run_id="ingestrun_item_recent",
                    source_url=recent_retryable_url,
                    canonical_url=recent_retryable_url,
                    status="failed",
                    retryable=True,
                ),
                JobIngestionItem(
                    ingestion_item_id="ingestitem_expired",
                    ingestion_run_id="ingestrun_item_expired",
                    source_url=expired_retryable_url,
                    canonical_url=expired_retryable_url,
                    status="needs_external_resolution",
                    retryable=True,
                ),
            ]
        )
        db.flush()

        exclusions = failed_link_exclusion_urls(
            db,
            provider="canadian_tech_internships",
            source_url=GITHUB_URL,
            now=now,
        )

    assert permanent_url in exclusions
    assert recent_retryable_url not in exclusions
    assert expired_retryable_url not in exclusions


def test_failed_link_exclusions_keep_default_cooldown_for_other_providers(
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory()
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    init_database(settings)
    retryable_url = "https://jobs.hanzilla.co/jobs/retry-later"
    with session_scope(settings) as db:
        db.add(
            JobIngestionRun(
                ingestion_run_id="ingestrun_hanzilla_retryable",
                provider="hanzilla",
                source_url="https://jobs.hanzilla.co/internships/",
                status="completed_with_warnings",
                started_at=now - timedelta(hours=2),
                request_payload={
                    "link_failures": [
                        {
                            "source_url": retryable_url,
                            "category": "retryable",
                            "reason": "Server unavailable",
                            "upstream_status_code": 503,
                        }
                    ]
                },
            )
        )
        db.flush()

        exclusions = failed_link_exclusion_urls(
            db,
            provider="hanzilla",
            source_url="https://jobs.hanzilla.co/internships/",
            now=now,
        )

    assert retryable_url in exclusions


def test_failed_link_exclusions_match_wrapped_portal_url(jobcockpit_settings_factory):
    settings = jobcockpit_settings_factory()
    init_database(settings)
    canonical_url = "https://jobs.example.com/software-intern"
    wrapped_url = (
        "https://click.example.com/track?target=https%3A%2F%2Fjobs.example.com%2Fsoftware-intern"
    )
    with session_scope(settings) as db:
        db.add(
            JobIngestionRun(
                ingestion_run_id="ingestrun_wrapped_failure",
                provider="canadian_tech_internships",
                source_url=GITHUB_URL,
                status="completed",
                started_at=datetime.now(UTC),
                request_payload={
                    "link_failures": [
                        {
                            "source_url": wrapped_url,
                            "category": "permanent",
                            "reason": "Posting expired",
                            "upstream_status_code": 410,
                        }
                    ]
                },
            )
        )
        db.flush()

        exclusions = failed_link_exclusion_urls(
            db,
            provider="canadian_tech_internships",
            source_url=GITHUB_URL,
        )

    request = AtsFetchRequest(source_url=GITHUB_URL, excluded_source_urls=exclusions)
    assert wrapped_url in exclusions
    assert canonical_url in exclusions
    assert request.excludes_source_url(wrapped_url)


def test_canadian_tech_internships_recovers_legacy_github_apply_url():
    apply_url = "https://jobs.example.com/redirect/software-intern"

    assert (
        legacy_portal_source_url(
            "ats_canadian_tech_internships",
            {"description_raw": f"GitHub List Apply URL: {apply_url}\nDescription"},
        )
        == apply_url
    )


def test_generic_ats_ingest_temporarily_skips_recent_canadian_tech_failures(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    failing_apply_url = "https://jobs.example.com/failing/software-intern"
    excluded_urls_by_call: list[frozenset[str]] = []

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

        def fetch_jobs(self, request: AtsFetchRequest):
            excluded_urls_by_call.append(request.excluded_source_urls)
            request.stats.links_seen_count = 1
            if len(excluded_urls_by_call) == 1:
                request.stats.link_failures.append(
                    AtsLinkFailure(
                        source_url=failing_apply_url,
                        category="permanent",
                        reason="Posting expired",
                        upstream_status_code=410,
                    )
                )
            return []

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: FakeCanadianTechInternshipsAdapter(),
    )

    for _ in range(2):
        response = client.post(
            "/api/jobs/ingest/ats",
            json={
                "provider": "canadian_tech_internships",
                "source_url": GITHUB_URL,
                "limit": 1,
                "skip_interest_filter": True,
            },
        )
        assert response.status_code == 201

    assert failing_apply_url not in excluded_urls_by_call[0]
    assert failing_apply_url in excluded_urls_by_call[1]
    with session_scope(settings) as db:
        runs = db.query(JobIngestionRun).order_by(JobIngestionRun.created_at).all()
        assert runs[0].request_payload["link_failures"] == [
            {
                "source_url": failing_apply_url,
                "category": "permanent",
                "reason": "Posting expired",
                "upstream_status_code": 410,
            }
        ]
        assert runs[0].alert_level == "warning"
        assert "link_failures" not in runs[1].request_payload
        assert runs[1].alert_level == "info"


def test_generic_ats_ingest_reuses_failures_from_partially_failed_run(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    expired_url = "https://jobs.example.com/expired/software-intern"

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"
        fetch_count = 0

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

        def fetch_jobs(self, request: AtsFetchRequest):
            if self.fetch_count == 0:
                self.fetch_count += 1
                request.stats.links_seen_count = 1
                request.stats.link_failures.append(
                    AtsLinkFailure(
                        source_url=expired_url,
                        category="permanent",
                        reason="Posting expired",
                        upstream_status_code=410,
                    )
                )
                raise AtsProviderError(502, "Later listing page failed")
            assert expired_url in request.excluded_source_urls
            return []

    fake_adapter = FakeCanadianTechInternshipsAdapter()
    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: fake_adapter,
    )
    request_payload = {
        "provider": "canadian_tech_internships",
        "source_url": GITHUB_URL,
        "limit": 25,
    }

    first_response = client.post("/api/jobs/ingest/ats", json=request_payload)
    second_response = client.post("/api/jobs/ingest/ats", json=request_payload)

    assert first_response.status_code == 502
    assert second_response.status_code == 201
    with session_scope(settings) as db:
        failed_run = db.query(JobIngestionRun).filter_by(status="failed").one()
        assert failed_run.jobs_seen_count == 1
        assert failed_run.request_payload["link_failures"] == [
            {
                "source_url": expired_url,
                "category": "permanent",
                "reason": "Posting expired",
                "upstream_status_code": 410,
            }
        ]
        failed_item = (
            db.query(JobIngestionItem).filter_by(ingestion_run_id=failed_run.ingestion_run_id).one()
        )
        assert failed_item.source_url == expired_url
        assert failed_item.status == "expired"
        assert failed_item.retryable is False


def test_canadian_tech_internships_is_registered_as_ats_adapter():
    assert "canadian_tech_internships" in registered_ats_providers()
    assert isinstance(get_ats_adapter("CANADIAN_TECH_INTERNSHIPS"), CanadianTechInternshipsAdapter)


def test_canadian_tech_internships_preserves_chrome_cdp_first_fallback(
    jobcockpit_settings_factory,
):
    settings = jobcockpit_settings_factory(
        posting_rendered_fallback_provider="chrome_cdp_first",
        chrome_cdp_auto_start=True,
    )

    adapter = CanadianTechInternshipsAdapter(settings=settings)

    assert isinstance(adapter.rendered_page_resolver, PrioritizedRenderedPageResolver)
    assert isinstance(
        adapter.rendered_page_resolver.primary_resolver,
        ChromeCdpRenderedPageResolver,
    )
    assert adapter.rendered_page_resolver.primary_resolver.auto_start is True
    assert isinstance(
        adapter.rendered_page_resolver.fallback_resolver,
        CodexCliRenderedPageResolver,
    )


def test_generic_ats_ingest_configures_adapter_with_active_settings(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(
        ats_mock_port=8001,
        posting_rendered_fallback_provider="disabled",
        chrome_cdp_auto_start=False,
    )
    client = jobcockpit_client_factory(settings)
    configured_settings = []

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def configure_settings(self, active_settings) -> None:
            configured_settings.append(active_settings)

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

        def fetch_jobs(self, request: AtsFetchRequest):
            return []

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: FakeCanadianTechInternshipsAdapter(),
    )

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "limit": 1,
            "skip_interest_filter": True,
        },
    )

    assert response.status_code == 201
    assert configured_settings == [settings]


def test_generic_ats_ingest_endpoint_persists_canadian_tech_internships_jobs(
    jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == GITHUB_URL
            assert request.limit == 2
            assert request.search_text == "software"
            return [
                AtsJob(
                    source_url=MAINTAINX_APPLY_URL,
                    raw_text=(
                        "Software Development Intern\nCompany: MaintainX\nLocation: Montreal, QC\n"
                        "GitHub List URL: https://github.com/negarprh/Canadian-Tech-Internships-2026\n"
                        "Ship TypeScript product features."
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

    def fake_get_ats_adapter(provider: str):
        assert provider == "canadian_tech_internships"
        return FakeCanadianTechInternshipsAdapter()

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "search_text": "software",
            "limit": 2,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider"] == "canadian_tech_internships"
    assert payload["jobs_created_count"] == 1
    assert payload["jobs"][0]["source"]["source_type"] == "ats_canadian_tech_internships"
    assert payload["jobs"][0]["source"]["source_url"] == MAINTAINX_APPLY_URL

    with session_scope(settings) as db:
        job = db.query(JobPosting).one()
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert job.source_type == "ats_canadian_tech_internships"
        assert artifact is not None
        assert artifact.artifact_type == "job_snapshot"


def test_generic_ats_ingest_endpoint_reports_canadian_tech_links_seen(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def fetch_jobs(self, request: AtsFetchRequest):
            request.stats.links_seen_count = 30
            return [
                AtsJob(
                    source_url=MAINTAINX_APPLY_URL,
                    raw_text=(
                        "Software Development Intern\nCompany: MaintainX\nLocation: Montreal, QC\n"
                        "Ship TypeScript product features."
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeCanadianTechInternshipsAdapter()
    )

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "search_text": "software",
            "limit": 30,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 30
    assert payload["jobs_created_count"] == 1


def test_generic_ats_ingest_endpoint_passes_existing_canadian_tech_urls_to_adapter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    calls: list[frozenset[str]] = []

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def fetch_jobs(self, request: AtsFetchRequest):
            calls.append(request.excluded_source_urls)
            if len(calls) == 1:
                assert request.excluded_source_urls == frozenset()
                return [
                    AtsJob(
                        source_url=MAINTAINX_APPLY_URL,
                        raw_text=(
                            "Software Development Intern\nCompany: MaintainX\nLocation: Montreal, QC\n"
                            "Ship TypeScript product features."
                        ),
                    )
                ]
            assert MAINTAINX_APPLY_URL in request.excluded_source_urls
            return []

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS)
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeCanadianTechInternshipsAdapter()
    )

    first_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "search_text": "software",
            "limit": 2,
        },
    )
    assert first_response.status_code == 201
    assert first_response.json()["jobs_created_count"] == 1

    second_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "search_text": "software",
            "limit": 2,
        },
    )

    assert second_response.status_code == 201
    assert second_response.json()["jobs_created_count"] == 0
    assert calls == [frozenset(), frozenset({MAINTAINX_APPLY_URL})]


def test_generic_ats_ingest_endpoint_passes_existing_urls_from_other_sources_to_adapter(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    calls: list[frozenset[str]] = []

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url=MAINTAINX_APPLY_URL,
                    raw_text=(
                        "Software Development Intern\nCompany: MaintainX\nLocation: Montreal, QC\n"
                        "Ship TypeScript product features."
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    class FakeCanadianTechInternshipsAdapter:
        provider = "canadian_tech_internships"

        def fetch_jobs(self, request: AtsFetchRequest):
            calls.append(request.excluded_source_urls)
            return []

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == GITHUB_URL

    def fake_get_ats_adapter(provider: str):
        if provider == "workday":
            return FakeWorkdayAdapter()
        if provider == "canadian_tech_internships":
            return FakeCanadianTechInternshipsAdapter()
        raise AssertionError(f"Unexpected provider: {provider}")

    allow_public_dns(CANADIAN_TECH_TEST_HOSTS | {"suncor.wd1.myworkdayjobs.com"})
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    workday_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "workday",
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External",
        },
    )
    assert workday_response.status_code == 201
    assert workday_response.json()["jobs_created_count"] == 1

    canadian_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "canadian_tech_internships",
            "source_url": GITHUB_URL,
            "search_text": "software",
            "limit": 2,
        },
    )

    assert canadian_response.status_code == 201
    assert calls == [frozenset({MAINTAINX_APPLY_URL})]


def test_canadian_tech_internships_rejects_unsupported_hosts():
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    try:
        adapter.validate_source_url("https://example.com/negarprh/Canadian-Tech-Internships-2026")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert (
            error.detail
            == "Only the negarprh/Canadian-Tech-Internships-2026 GitHub README is supported"
        )
    else:
        raise AssertionError("Expected unsupported host to raise AtsProviderError")
