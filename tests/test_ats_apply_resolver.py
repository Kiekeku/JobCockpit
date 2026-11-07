import json
import socket

import httpx
import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsProviderError, classify_link_failure
from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import (
    oracle_candidate_experience_job_identity,
    parse_oracle_candidate_experience_url,
)
from jobcockpit.ingestion.ats_handlers import AtsApplyPageResolver, AtsPostingContext
from jobcockpit.ingestion.ats_handlers.apply import _job_from_generic_apply_page
from jobcockpit.ingestion.parser import parse_job_text


def test_empty_observed_ukg_shell_cannot_be_filled_from_context(monkeypatch):
    from jobcockpit.ingestion.posting_completeness import IncompletePostingError

    resolver = AtsApplyPageResolver()
    url = "https://jobs.example.com/OpportunityDetail?opportunityId=example"
    html = '<html><head><title data-bind="text: title()"></title></head><body><div id="PageContainer"></div></body></html>'
    monkeypatch.setattr(resolver, "_get_apply_page", lambda source_url: (url, html, None, None))
    try:
        with pytest.raises(IncompletePostingError, match="no posting content"):
            resolver.resolve(url, AtsPostingContext(role="Student, Cyber Security Analyst", company="Example"))
    finally:
        resolver.close()


@pytest.mark.parametrize('body', ['', '<div id="app"></div>'])
@pytest.mark.parametrize('meta', ['property="og:description"', 'name="description"'])
def test_generic_preview_is_not_a_posting_body(body, meta):
    # Observed empty application shells on Bamboo, Workable, and Oracle.
    html = f'<html><head><meta {meta} content="Our team builds software that improves patient care."></head><body>{body}</body></html>'
    job = _job_from_generic_apply_page('https://jobs.example.com/123', html, AtsPostingContext())
    assert job.has_posting_body is False


@pytest.mark.parametrize('body', [
    '<main>Develop software and test medical imaging systems.</main>',
    '<script type="application/ld+json">{"@type":"JobPosting","description":"Develop software and test medical imaging systems."}</script>',
])
def test_generic_full_content_is_not_rejected_with_preview(body):
    html = '<html><head><meta name="description" content="Short preview"></head><body>' + body + '</body></html>'
    job = _job_from_generic_apply_page('https://jobs.example.com/123', html, AtsPostingContext())
    assert job.has_posting_body is True


@pytest.mark.parametrize('company,location,accepted', [
    ('Dotdash Meredith', 'Edmonton, Alberta', True),
    ('People Inc.', 'Edmonton, Alberta', True),
    ('Unrelated Publisher', 'Edmonton, Alberta', False),
    ('Dotdash Meredith', 'Vancouver, British Columbia', False),
])
def test_workday_handoff_verifies_legal_employer_and_visible_locations(company, location, accepted):
    # Minimized from the observed Dotdash Indeed -> Workday handoff. JSON-LD
    # contains only the remote province; the posting's DOM includes Edmonton.
    from jobcockpit.ingestion.posting_discovery import _official_posting_confidence
    from jobcockpit.ingestion.posting_resolution import ExternalPostingHint

    posting = {'@type': 'JobPosting', 'title': 'Cloud Platform Engineering Intern',
               'hiringOrganization': {'name': 'Investopedia Canada, Inc.'},
               'jobLocation': {'address': {'addressLocality': 'Canada - Alberta', 'addressCountry': 'Canada'}},
               'description': 'Build cloud infrastructure and automation with the engineering team.'}
    html = '<script type="application/ld+json">' + json.dumps(posting) + '</script>'
    html += '<div data-automation-id="job-posting-details"><div data-automation-id="locations"><dl><dt>locations</dt><dd>Edmonton, AB</dd><dd>Alberta, CAN-Remote</dd></dl></div></div>'
    # Unrelated recommended-job locations must not be treated as this job's.
    html += '<aside data-automation-id="locations"><dd>Vancouver, BC</dd></aside>'
    job = _job_from_generic_apply_page('https://meredith.wd5.myworkdayjobs.com/EXT/job/Intern_R123', html, AtsPostingContext())
    hint = ExternalPostingHint(source_url='https://example.com/job', source_kind='indeed',
                               title=posting['title'], company=company, location=location)
    assert (_official_posting_confidence(hint, job.observed_raw_text) >= .8) is accepted
    assert 'Edmonton, AB' in job.observed_raw_text.split('Location: ', 1)[1].splitlines()[0]
    assert 'Location: Canada - Alberta, Canada\n' in job.raw_text


def test_generic_apply_page_normalizes_observed_country_object():
    # Minimized from a saved posting with a structured JSON-LD country.
    posting = {
        "@type": "JobPosting",
        "title": "Engineering Intern",
        "jobLocation": {"address": {
            "addressLocality": "Toronto",
            "addressRegion": "ON,CA",
            "addressCountry": {"@type": "Country", "name": "CA"},
        }},
        "description": "Build engineering tools with our team.",
    }
    job = _job_from_generic_apply_page(
        "https://example.com/jobs/intern",
        '<script type="application/ld+json">' + json.dumps(posting) + '</script>',
        AtsPostingContext(),
    )
    assert "Location: Toronto, ON,CA\n" in job.raw_text
    assert "Location: Toronto, ON,CA\n" in job.observed_raw_text
    location = parse_job_text(job.raw_text).location
    assert (location.city, location.province_state, location.country) == (
        "Toronto", "Ontario", "Canada",
    )


WORKDAY_APPLY_URL = (
    "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Milton-Ontario-CA/"
    "Junior-Full-Stack-Developer-Coop_R00244793"
)
WORKDAY_LISTING_URL = "https://suncor.wd1.myworkdayjobs.com/Suncor_External"
WORKDAY_TRACKING_URL = "https://click.email.amazon.com/track?target=workday"
GREENHOUSE_APPLY_URL = "https://job-boards.greenhouse.io/maintainx/jobs/gh5146022007x"
ASHBY_APPLY_URL = "https://jobs.ashbyhq.com/NPX/048ca8da-bfb9-4454-8147-ac9497629634/"
ORACLE_APPLY_URL = "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/J35268"
ORACLE_CE_APPLY_URL = "https://hdks.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9148"

def test_oracle_candidate_experience_url_parser_rejects_non_oracle_substring_host():
    try:
        parse_oracle_candidate_experience_url(
            "https://evil-oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9148"
        )
    except AtsProviderError as error:
        assert error.status_code == 400
    else:
        raise AssertionError("Expected non-Oracle substring host to be rejected")


def test_oracle_candidate_experience_identity_normalizes_public_route_variants():
    urls = [
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/J11154",
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/fr/sites/CX/requisitions/preview/11154/",
        "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Careers/requisitions/job/11154?source=board",
    ]

    parsed_sites = [parse_oracle_candidate_experience_url(url) for url in urls]

    assert [site.site for site in parsed_sites] == ["CX_1", "CX", "Careers"]
    assert [site.job_id for site in parsed_sites] == ["J11154", "11154", "11154"]
    assert {
        oracle_candidate_experience_job_identity(url)
        for url in urls
    } == {"oracle_candidate_experience:hcpd.fa.ca2.oraclecloud.com:11154"}


def test_oracle_candidate_experience_identity_rejects_listing_urls():
    assert (
        oracle_candidate_experience_job_identity(
            "https://hcpd.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"
        )
        == ""
    )


def test_shared_apply_resolver_uses_workday_handler(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "wd3.myworkdaysite.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793":
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Junior Full Stack Developer Coop",
                        "jobReqId": "R00244793",
                        "location": "Milton, Ontario, Canada",
                        "externalUrl": WORKDAY_APPLY_URL,
                        "jobDescription": "<p>Build React services and Python APIs.</p>",
                    }
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    job = resolver.resolve(
        WORKDAY_APPLY_URL,
        AtsPostingContext(
            source_label="GitHub List",
            source_url="https://github.com/negarprh/Canadian-Tech-Internships-2026",
            company="Magna",
            role="Junior Full Stack Developer Co-op",
        ),
    )

    assert job.source_url == WORKDAY_APPLY_URL
    assert job.source_archive is not None
    assert job.source_archive.source_url.endswith(
        "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793"
    )
    assert job.source_archive.final_url.endswith(
        "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793"
    )
    assert job.source_archive.status_code == 200
    assert job.source_archive.content_type.startswith("application/json")
    assert b"Junior Full Stack Developer Coop" in job.source_archive.body
    assert "Junior Full Stack Developer Coop" in job.raw_text
    assert "Build React services and Python APIs." in job.raw_text
    assert "GitHub List Company: Magna" in job.raw_text
    assert "GitHub List Role: Junior Full Stack Developer Co-op" in job.raw_text
    assert [str(request.url) for request in requests_seen] == [
        "https://wd3.myworkdaysite.com/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793"
    ]


def test_shared_apply_resolver_does_not_fall_back_after_workday_detail_failure(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "wd3.myworkdaysite.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path.startswith("/wday/cxs/"):
            return httpx.Response(410)
        if str(request.url) == WORKDAY_APPLY_URL:
            return httpx.Response(200, text="<html><body>Workday shell</body></html>")
        raise AssertionError(f"Unexpected apply resolver fetch: {request.url}")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    try:
        resolver.resolve(
            WORKDAY_APPLY_URL,
            AtsPostingContext(company="Magna", role="Junior Full Stack Developer Co-op"),
        )
    except AtsProviderError as error:
        assert classify_link_failure(WORKDAY_APPLY_URL, error).category == "permanent"
    else:
        raise AssertionError("Expected failed Workday detail lookup to propagate")

    assert [request.url.path for request in requests_seen] == [
        "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793"
    ]


def test_shared_apply_resolver_rejects_workday_listing_urls(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "suncor.wd1.myworkdayjobs.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json={"jobPostings": [{"externalPath": "/job/Some-Role_R1"}]})

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    try:
        resolver.resolve(WORKDAY_LISTING_URL)
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Workday apply URL must be a job detail URL"
    else:
        raise AssertionError("Expected Workday listing apply URL to raise AtsProviderError")

    assert requests_seen == []


def test_shared_apply_resolver_uses_workday_handler_after_tracking_redirect(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host in {"click.email.amazon.com", "wd3.myworkdaysite.com"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == WORKDAY_TRACKING_URL:
            return httpx.Response(302, headers={"Location": WORKDAY_APPLY_URL})
        if request.url.path == "/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793":
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Junior Full Stack Developer Coop",
                        "jobReqId": "R00244793",
                        "location": "Milton, Ontario, Canada",
                        "externalUrl": WORKDAY_APPLY_URL,
                        "jobDescription": "<p>Build React services and Python APIs.</p>",
                    }
                },
            )
        raise AssertionError(f"Unexpected apply resolver fetch: {request.url}")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    job = resolver.resolve(WORKDAY_TRACKING_URL, AtsPostingContext(source_label="Email Link"))

    assert job.source_url == WORKDAY_APPLY_URL
    assert "Junior Full Stack Developer Coop" in job.raw_text
    assert "Build React services and Python APIs." in job.raw_text
    assert "Email Link Apply URL: https://click.email.amazon.com/track?target=workday" in job.raw_text
    assert [str(request.url) for request in requests_seen] == [
        WORKDAY_TRACKING_URL,
        "https://wd3.myworkdaysite.com/wday/cxs/magna/Magna/job/Milton-Ontario-CA/Junior-Full-Stack-Developer-Coop_R00244793",
    ]

def test_shared_apply_resolver_uses_oracle_candidate_experience_handler(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "hdks.fa.ca2.oraclecloud.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails":
            assert request.url.params["expand"] == "all"
            assert request.url.params["onlyData"] == "true"
            assert request.url.params["finder"] == 'ById;Id="9148",siteNumber=CX_1'
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "Id": "9148",
                            "Title": "Data Specialist - Claim Operations Management - Fall 2026 Co-op/Intern",
                            "ExternalDescriptionStr": (
                                "<p><strong>YOUR NEXT CO-OP PLACEMENT</strong></p>"
                                "<p><strong>Work term:</strong> Fall 2026</p>"
                                "<p><strong>Work term length:</strong> 4-month</p>"
                                "<p>Use SQL to migrate claims data and build reports.</p>"
                            ),
                            "PrimaryLocation": "Waterloo, Ontario",
                            "JobSchedule": "Full time",
                            "ExternalPostedStartDate": "2026-06-17T19:43:51+00:00",
                        }
                    ]
                },
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    job = resolver.resolve(
        ORACLE_CE_APPLY_URL,
        AtsPostingContext(
            source_label="Gmail Alert",
            company="Definity",
            role="Data Specialist Co-op Intern, Claim Operations Management",
            location="Waterloo, Ontario",
        ),
    )

    assert job.source_url == ORACLE_CE_APPLY_URL
    assert job.source_archive is not None
    assert httpx.URL(job.source_archive.source_url).path == "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    assert job.source_archive.status_code == 200
    assert job.source_archive.content_type.startswith("application/json")
    assert b"ExternalDescriptionStr" in job.source_archive.body
    assert "Data Specialist - Claim Operations Management - Fall 2026 Co-op/Intern" in job.raw_text
    assert "Work term:" in job.raw_text
    assert "Fall 2026" in job.raw_text
    assert "Work term length:" in job.raw_text
    assert "4-month" in job.raw_text
    assert "Use SQL to migrate claims data and build reports." in job.raw_text
    assert job.title == "Data Specialist - Claim Operations Management - Fall 2026 Co-op/Intern"
    assert job.provider_job_id == "9148"
    assert "Gmail Alert Company: Definity" in job.raw_text
    assert [request.url.path for request in requests_seen] == [
        "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    ]

def test_shared_apply_resolver_uses_json_ld_for_ats_pages(monkeypatch):
    job_posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Software Developer Intern",
        "hiringOrganization": {"name": "Nuclear Promise X"},
        "jobLocation": {"address": {"addressLocality": "Kincardine", "addressRegion": "ON", "addressCountry": "CA"}},
        "datePosted": "2026-05-19",
        "description": "<p>Build automation for engineering workflows.</p>",
    }

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "jobs.ashbyhq.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ASHBY_APPLY_URL:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=(
                    "<html><head>"
                    f'<script type="application/ld+json">{json.dumps(job_posting)}</script>'
                    "</head><body>Fallback body</body></html>"
                ),
            )
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    job = resolver.resolve(ASHBY_APPLY_URL, AtsPostingContext(source_label="Email Link", role="Software Developer"))

    assert job.source_url == ASHBY_APPLY_URL
    assert job.source_archive is not None
    assert job.source_archive.source_url == ASHBY_APPLY_URL
    assert job.source_archive.final_url == ASHBY_APPLY_URL
    assert job.source_archive.status_code == 200
    assert job.source_archive.content_type.startswith("text/html")
    assert b"Fallback body" in job.source_archive.body
    assert "Software Developer Intern" in job.raw_text
    assert "Company: Nuclear Promise X" in job.raw_text
    assert "Location: Kincardine, ON, CA" in job.raw_text
    assert "Build automation for engineering workflows." in job.raw_text
    assert "Email Link Role: Software Developer" in job.raw_text


def test_shared_apply_resolver_uses_metadata_and_visible_html(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host in {"job-boards.greenhouse.io", "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if str(request.url) == GREENHOUSE_APPLY_URL:
            return httpx.Response(
                200,
                text="""
                <html>
                  <head>
                    <title>Job Application for Software Development Intern at MaintainX</title>
                    <meta property="og:title" content="Software Development Intern" />
                    <meta property="og:description" content="Montreal, Quebec" />
                  </head>
                  <body><main><h1>Software Development Intern</h1><p>Build TypeScript features.</p></main></body>
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
                    <meta property="og:description" content="Develop embedded networking software." />
                    <meta property="og:site_name" content="Nokia" />
                  </head>
                  <body><div id="app"></div></body>
                </html>
                """,
            )
        return httpx.Response(404)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    greenhouse_job = resolver.resolve(GREENHOUSE_APPLY_URL, AtsPostingContext(company="MaintainX", location="Montreal, QC"))
    with pytest.raises(AtsProviderError, match="metadata preview"):
        resolver.resolve(ORACLE_APPLY_URL, AtsPostingContext(company="Nokia", location="Ottawa, ON"))

    assert "Software Development Intern" in greenhouse_job.raw_text
    assert "Build TypeScript features." in greenhouse_job.raw_text
    assert [str(request.url) for request in requests_seen] == [
        GREENHOUSE_APPLY_URL,
        'https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22J35268%22,siteNumber=CX_1',
        ORACLE_APPLY_URL,
    ]


def test_generic_apply_page_visible_company_overrides_stale_portal_context(monkeypatch):
    apply_url = "https://careers.transmountain.example/job/1291506047"

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "careers.transmountain.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == apply_url
        return httpx.Response(
            200,
            text=(
                "<html><body><h1>IT Enterprise Co-op Student</h1>"
                "<div>Company:</div><div>Trans Mountain</div>"
                "<main>Support enterprise applications.</main></body></html>"
            ),
        )

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    job = resolver.resolve(
        apply_url,
        AtsPostingContext(source_label="Gmail Alert", company="InternHunt"),
    )

    assert "Company: Trans Mountain" in job.raw_text
    assert "Gmail Alert Company: InternHunt" in job.raw_text


def test_generic_apply_page_prefers_open_graph_company_over_unscoped_body_label(
    monkeypatch,
):
    apply_url = "https://careers.example.test/jobs/123"

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "careers.example.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == apply_url
        return httpx.Response(
            200,
            text=(
                '<html><head><meta property="og:site_name" content="Example Systems" />'
                "</head><body><h1>Software Intern</h1>"
                "<footer><div>Company:</div><div>Unrelated Footer Company</div></footer>"
                "</body></html>"
            ),
        )

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    job = resolver.resolve(apply_url)

    raw_lines = job.raw_text.splitlines()
    assert "Company: Example Systems" in raw_lines
    assert "Company: Unrelated Footer Company" not in raw_lines


def test_shared_apply_resolver_blocks_private_apply_redirect(monkeypatch):
    requests_seen: list[httpx.Request] = []

    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "job-boards.greenhouse.io"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("20.12.34.56", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = AtsApplyPageResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    try:
        resolver.resolve(GREENHOUSE_APPLY_URL)
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Apply URL is blocked by policy: private_network_blocked"
    else:
        raise AssertionError("Expected private apply redirect to raise AtsProviderError")

    assert [str(request.url) for request in requests_seen] == [GREENHOUSE_APPLY_URL]
