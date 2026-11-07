import json

import httpx

from jobcockpit.services.workflows import legacy_portal_source_url, source_metadata_with_portal_url
from jobcockpit.db.models import JobPosting, StoredArtifact
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.hanzilla import HanzillaAdapter
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.role_interest import match_title_interest


def listing_html() -> str:
    return """
    <html>
      <body>
        <section class="category-section" id="software-engineering">
          <h2 class="category-title">Software Engineering <span>1 position</span></h2>
          <article class="job-card"
                   data-title="python developer intern"
                   data-company="ExampleCo"
                   data-location="Toronto, Ontario hybrid"
                   data-category="swe"
                   data-stage="internship"
                   data-hot="true"
                   data-remote="false"
                   data-hybrid="true"
                   data-search-text="python developer intern exampleco toronto build python APIs swe">
            <a class="job-link"
               href="/jobs/python-developer-intern-exampleco-123/"
               data-job-title="Python Developer Intern"
               data-job-company="ExampleCo">
              <span class="job-title">Python Developer Intern</span>
              <span class="job-company">ExampleCo</span>
              <span class="job-location">Toronto, Ontario - Hybrid</span>
              <span class="job-summary">Build Python APIs</span>
              <span class="job-details">Intern - 4mo - Fall 2026</span>
              <span class="job-date">Posted Jun 03, 2026</span>
            </a>
          </article>
        </section>
        <section class="category-section" id="accounting-fp-a">
          <h2 class="category-title">Accounting / FP&amp;A <span>1 position</span></h2>
          <article class="job-card"
                   data-title="accounting analyst intern"
                   data-company="FinanceCo"
                   data-location="Calgary, Alberta"
                   data-category="accounting"
                   data-stage="internship"
                   data-search-text="accounting analyst intern financeco calgary">
            <a class="job-link" href="/jobs/accounting-analyst-intern-financeco-456/">
              <span class="job-title">Accounting Analyst Intern</span>
            </a>
          </article>
        </section>
      </body>
    </html>
    """


def listing_with_untrusted_detail_link_html() -> str:
    return """
    <html>
      <body>
        <section class="category-section" id="software-engineering">
        <article class="job-card"
                 data-title="python developer intern"
                 data-company="UnsafeCo"
                 data-search-text="python developer intern unsafeco">
          <a class="job-link" href="http://169.254.169.254/latest/meta-data/">
            Python Developer Intern
          </a>
        </article>
        <article class="job-card"
                 data-title="python platform intern"
                 data-company="ExampleCo"
                 data-search-text="python platform intern exampleco">
          <a class="job-link" href="/jobs/python-platform-intern-exampleco-789/">
            Python Platform Intern
          </a>
        </article>
        </section>
      </body>
    </html>
    """


def detail_html(
    *,
    title: str = "Python Developer Intern",
    apply_url: str = "https://apply.example.com/jobs/123",
    locality: str = "Toronto",
    region: str = "Ontario",
    country: str = "CA",
    job_locations: list[dict] | None = None,
) -> str:
    location_payload = (
        job_locations
        if job_locations is not None
        else {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": locality,
                "addressRegion": region,
                "addressCountry": country,
            },
        }
    )
    job_posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "Design, build, and test Python services.",
        "datePosted": "2026-06-03",
        "employmentType": "INTERN",
        "hiringOrganization": {"@type": "Organization", "name": "ExampleCo"},
        "jobLocation": location_payload,
        "url": "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/",
        "directApply": True,
    }
    return f"""
    <html>
      <head>
        <script type="application/ld+json">{json.dumps(job_posting)}</script>
      </head>
      <body>
        <main class="job-detail">
          <header class="job-header">
            <span class="badge-category">Software Engineering</span>
            <span class="badge-intern">Internship</span>
            <h1>{title}</h1>
            <p class="company-name">at ExampleCo</p>
          </header>
          <section class="job-meta-section">
            <div class="meta-item">Location Toronto, Ontario</div>
            <div class="meta-item">Details Intern - 4mo - Fall 2026</div>
            <div class="meta-item">Posted Jun 03, 2026</div>
          </section>
          <div class="summary-block">About the Role Build Python APIs</div>
          <div class="summary-block">About ExampleCo Cloud software company</div>
          <div class="description-content">
            Design, build, and test Python services.
          </div>
          <div class="apply-section">
            <a class="btn-apply" href="{apply_url}">Apply on company site</a>
          </div>
        </main>
      </body>
    </html>
    """


def test_hanzilla_adapter_filters_listing_cards_and_fetches_detail_pages():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing_html())
        if request.url.path == "/jobs/python-developer-intern-exampleco-123/":
            return httpx.Response(200, text=detail_html())
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://jobs.hanzilla.co/internships/",
            limit=5,
            search_text="python",
        )
    )

    assert len(jobs) == 1
    assert [request.url.path for request in requests_seen] == [
        "/internships/",
        "/jobs/python-developer-intern-exampleco-123/",
    ]
    assert jobs[0].source_url == "https://apply.example.com/jobs/123"
    assert jobs[0].portal_source_url == (
        "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
    )
    assert "Python Developer Intern" in jobs[0].raw_text
    assert "Company: ExampleCo" in jobs[0].raw_text
    assert "Location: Toronto, Ontario" in jobs[0].raw_text
    assert "Country: CA" in jobs[0].raw_text
    assert "Category: Software Engineering" in jobs[0].raw_text
    assert "Stage: Internship" in jobs[0].raw_text
    assert "Details: Intern - 4mo - Fall 2026" in jobs[0].raw_text
    assert "Posted: Jun 03, 2026" in jobs[0].raw_text
    assert (
        "Hanzilla URL: https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
        in jobs[0].raw_text
    )
    assert "Apply URL: https://apply.example.com/jobs/123" in jobs[0].raw_text
    assert "Design, build, and test Python services." in jobs[0].raw_text


def test_hanzilla_adapter_applies_shared_interest_filter_before_detail_fetch():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing_html())
        if request.url.path == "/jobs/python-developer-intern-exampleco-123/":
            return httpx.Response(200, text=detail_html())
        if request.url.path == "/jobs/accounting-analyst-intern-financeco-456/":
            raise AssertionError("non-tech Hanzilla detail page should not be fetched")
        return httpx.Response(404)

    adapter = HanzillaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    fetch_request = AtsFetchRequest(
        source_url="https://jobs.hanzilla.co/internships/",
        limit=5,
        listing_interest_matcher=lambda title: match_title_interest(
            title,
            {"target_role_include_keywords": ["python"]},
        ),
    )
    jobs = adapter.fetch_jobs(fetch_request)

    assert len(jobs) == 1
    assert [request.url.path for request in requests_seen] == [
        "/internships/",
        "/jobs/python-developer-intern-exampleco-123/",
    ]
    assert fetch_request.stats.interest_skips[0].title == "Accounting Analyst Intern"


def test_hanzilla_adapter_expands_full_internship_categories_and_keeps_small_categories():
    root_listing = """
    <html><body>
      <section class="category-section" id="software-engineering">
        <article class="job-card" data-stage="internship" data-title="software intern preview">
          <a class="job-link" href="/jobs/software-intern-preview-1/">Software Intern Preview</a>
        </article>
        <p class="category-view-all">
          <a href="/categories/software-engineering/">View all 51 positions</a>
        </p>
      </section>
      <section class="category-section" id="data-ml">
        <article class="job-card" data-stage="internship" data-title="data intern preview">
          <a class="job-link" href="/jobs/data-intern-preview-2/">Data Intern Preview</a>
        </article>
        <p class="category-view-all">
          <a href="/categories/data-ml/">View all 78 positions</a>
        </p>
      </section>
      <section class="category-section" id="devops-sre-platform">
        <article class="job-card" data-stage="internship" data-title="devops co-op">
          <a class="job-link" href="/jobs/devops-co-op-3/">DevOps Co-op</a>
        </article>
      </section>
      <section class="category-section" id="security">
        <article class="job-card" data-stage="internship" data-title="security intern">
          <a class="job-link" href="/jobs/security-intern-7/">Security Intern</a>
        </article>
        <p class="category-view-all">
          <a href="/categories/security/">View all Security positions</a>
        </p>
      </section>
    </body></html>
    """
    software_listing = """
    <html><body>
      <article class="job-card" data-stage="new-grad" data-title="junior developer">
        <a class="job-link" href="/jobs/junior-developer-4/">Junior Developer</a>
      </article>
      <article class="job-card" data-stage="internship" data-title="software intern preview">
        <a class="job-link" href="/jobs/software-intern-preview-1/">Software Intern Preview</a>
      </article>
      <article class="job-card" data-stage="internship" data-title="backend developer co-op">
        <a class="job-link" href="/jobs/backend-developer-co-op-5/">Backend Developer Co-op</a>
      </article>
    </body></html>
    """
    data_listing = """
    <html><body>
      <article class="job-card" data-stage="internship" data-title="data intern preview">
        <a class="job-link" href="/jobs/data-intern-preview-2/">Data Intern Preview</a>
      </article>
      <article class="job-card" data-stage="new-grad" data-title="junior data analyst">
        <a class="job-link" href="/jobs/junior-data-analyst-6/">Junior Data Analyst</a>
      </article>
    </body></html>
    """
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        if request.url.path == "/internships/":
            return httpx.Response(200, text=root_listing)
        if request.url.path == "/categories/software-engineering/":
            return httpx.Response(200, text=software_listing)
        if request.url.path == "/categories/data-ml/":
            return httpx.Response(200, text=data_listing)
        if request.url.path.startswith("/jobs/"):
            title = request.url.path.removeprefix("/jobs/").strip("/").replace("-", " ").title()
            return httpx.Response(200, text=detail_html(title=title))
        return httpx.Response(404)

    adapter = HanzillaAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    fetch_request = AtsFetchRequest(
        source_url="https://jobs.hanzilla.co/internships/#software-engineering",
        limit=10,
    )
    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.portal_source_url for job in jobs] == [
        "https://jobs.hanzilla.co/jobs/software-intern-preview-1/",
        "https://jobs.hanzilla.co/jobs/backend-developer-co-op-5/",
        "https://jobs.hanzilla.co/jobs/data-intern-preview-2/",
        "https://jobs.hanzilla.co/jobs/devops-co-op-3/",
        "https://jobs.hanzilla.co/jobs/security-intern-7/",
    ]
    assert requests_seen[:3] == [
        "/internships/",
        "/categories/software-engineering/",
        "/categories/data-ml/",
    ]
    assert "/jobs/junior-developer-4/" not in requests_seen
    assert "/jobs/junior-data-analyst-6/" not in requests_seen
    assert "/categories/security/" in requests_seen
    assert [failure.to_dict() for failure in fetch_request.stats.link_failures] == [
        {
            "source_url": "https://jobs.hanzilla.co/categories/security/",
            "category": "permanent",
            "reason": "Hanzilla category page was not found",
            "upstream_status_code": 404,
        }
    ]


def test_hanzilla_country_metadata_reaches_real_time_parser():
    detail_url = "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == detail_url
        return httpx.Response(
            200,
            text=detail_html(locality="Los Angeles", region="CA", country="US"),
        )

    adapter = HanzillaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    job = adapter.fetch_jobs(AtsFetchRequest(source_url=detail_url, limit=1))[0]

    parsed = parse_job_text(job.raw_text, job.source_url, source_type="ats_hanzilla")

    assert parsed.location.city == "Los Angeles"
    assert parsed.location.province_state == "CA"
    assert parsed.location.country == "United States"


def test_hanzilla_multi_country_locations_preserve_primary_country():
    detail_url = "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
    locations = [
        {
            "@type": "Place",
            "address": {
                "addressLocality": "Los Angeles",
                "addressRegion": "CA",
                "addressCountry": "US",
            },
        },
        {
            "@type": "Place",
            "address": {
                "addressLocality": "Toronto",
                "addressRegion": "ON",
                "addressCountry": "CA",
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == detail_url
        return httpx.Response(200, text=detail_html(job_locations=locations))

    adapter = HanzillaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    job = adapter.fetch_jobs(AtsFetchRequest(source_url=detail_url, limit=1))[0]
    parsed = parse_job_text(job.raw_text, job.source_url, source_type="ats_hanzilla")

    assert "Location: Los Angeles, CA, US; Toronto, ON, CA" in job.raw_text
    assert parsed.location.city == "Los Angeles"
    assert parsed.location.province_state == "CA"
    assert parsed.location.country == "United States"


def test_hanzilla_adapter_classifies_expired_detail_and_continues():
    expired_url = "https://jobs.hanzilla.co/jobs/expired-intern-123/"
    valid_url = "https://jobs.hanzilla.co/jobs/python-platform-intern-exampleco-789/"
    listing = """
    <html><body>
      <section class="category-section" id="software-engineering">
      <article class="job-card" data-search-text="expired intern">
        <a class="job-link" href="/jobs/expired-intern-123/">Expired Intern</a>
      </article>
      <article class="job-card" data-search-text="python platform intern">
        <a class="job-link" href="/jobs/python-platform-intern-exampleco-789/">Platform Intern</a>
      </article>
      </section>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing)
        if str(request.url) == expired_url:
            return httpx.Response(410)
        if str(request.url) == valid_url:
            return httpx.Response(200, text=detail_html(title="Platform Intern"))
        return httpx.Response(404)

    adapter = HanzillaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(source_url="https://jobs.hanzilla.co/internships/", limit=2)

    jobs = adapter.fetch_jobs(request)

    assert [job.portal_source_url for job in jobs] == [valid_url]
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].source_url == expired_url
    assert request.stats.link_failures[0].category == "permanent"
    assert request.stats.link_failures[0].upstream_status_code == 410


def test_hanzilla_adapter_bounds_detail_attempts_during_outage():
    listing = """
    <html><body>
      <section class="category-section" id="software-engineering">
      <article class="job-card"><a class="job-link" href="/jobs/intern-1/">Intern 1</a></article>
      <article class="job-card"><a class="job-link" href="/jobs/intern-2/">Intern 2</a></article>
      <article class="job-card"><a class="job-link" href="/jobs/intern-3/">Intern 3</a></article>
      </section>
    </body></html>
    """
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing)
        detail_requests.append(str(request.url))
        return httpx.Response(503)

    adapter = HanzillaAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(source_url="https://jobs.hanzilla.co/internships/", limit=2)

    assert adapter.fetch_jobs(request) == []
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 2
    assert detail_requests == [
        "https://jobs.hanzilla.co/jobs/intern-1/",
        "https://jobs.hanzilla.co/jobs/intern-2/",
    ]


def test_hanzilla_adapter_skips_excluded_detail_urls_before_detail_fetch():
    requests_seen: list[httpx.Request] = []
    excluded_url = "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing_html())
        if request.url.path == "/jobs/python-developer-intern-exampleco-123/":
            raise AssertionError("excluded Hanzilla detail page should not be fetched")
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://jobs.hanzilla.co/internships/",
            limit=1,
            search_text="python",
            excluded_source_urls=frozenset({excluded_url}),
        )
    )

    assert jobs == []
    assert [str(request.url) for request in requests_seen] == [
        "https://jobs.hanzilla.co/internships/"
    ]


def test_hanzilla_adapter_excluded_detail_urls_do_not_consume_limit():
    requests_seen: list[httpx.Request] = []
    excluded_url = "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
    next_url = "https://jobs.hanzilla.co/jobs/python-platform-intern-exampleco-789/"
    listing = """
    <html>
      <body>
        <section class="category-section" id="software-engineering">
        <article class="job-card" data-search-text="python developer intern exampleco">
          <a class="job-link" href="/jobs/python-developer-intern-exampleco-123/">Python Developer Intern</a>
        </article>
        <article class="job-card" data-search-text="python platform intern exampleco">
          <a class="job-link" href="/jobs/python-platform-intern-exampleco-789/">Python Platform Intern</a>
        </article>
        </section>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/internships/":
            return httpx.Response(200, text=listing)
        if str(request.url) == excluded_url:
            raise AssertionError("excluded Hanzilla detail page should not be fetched")
        if str(request.url) == next_url:
            return httpx.Response(
                200,
                text=detail_html(
                    title="Python Platform Intern", apply_url="https://apply.example.com/jobs/789"
                ),
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://jobs.hanzilla.co/internships/",
            limit=1,
            search_text="python",
            excluded_source_urls=frozenset({excluded_url}),
        )
    )

    assert [job.source_url for job in jobs] == ["https://apply.example.com/jobs/789"]
    assert [str(request.url) for request in requests_seen] == [
        "https://jobs.hanzilla.co/internships/",
        next_url,
    ]


def test_hanzilla_adapter_skips_non_hanzilla_listing_links():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.host == "jobs.hanzilla.co" and request.url.path == "/internships/":
            return httpx.Response(200, text=listing_with_untrusted_detail_link_html())
        if (
            request.url.host == "jobs.hanzilla.co"
            and request.url.path == "/jobs/python-platform-intern-exampleco-789/"
        ):
            return httpx.Response(200, text=detail_html(title="Python Platform Intern"))
        raise AssertionError(f"Unexpected Hanzilla adapter fetch: {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://jobs.hanzilla.co/internships/",
            limit=5,
            search_text="python",
        )
    )

    assert len(jobs) == 1
    assert jobs[0].raw_text.startswith("Python Platform Intern")
    assert [str(request.url) for request in requests_seen] == [
        "https://jobs.hanzilla.co/internships/",
        "https://jobs.hanzilla.co/jobs/python-platform-intern-exampleco-789/",
    ]


def test_hanzilla_adapter_imports_single_detail_url():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.url.path == "/jobs/python-developer-intern-exampleco-123/"
        return httpx.Response(200, text=detail_html())

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/",
            limit=10,
        )
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://apply.example.com/jobs/123"
    assert len(requests_seen) == 1


def test_hanzilla_adapter_skips_excluded_direct_detail_url_before_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("excluded Hanzilla direct detail URL should not be fetched")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)
    source_url = "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url=source_url,
            limit=1,
            excluded_source_urls=frozenset({source_url}),
        )
    )

    assert jobs == []


def test_hanzilla_is_registered_as_ats_adapter():
    assert "hanzilla" in registered_ats_providers()
    assert isinstance(get_ats_adapter("HANZILLA"), HanzillaAdapter)


def test_legacy_portal_source_url_reads_persisted_hanzilla_role_text():
    role = {
        "description_raw": (
            "Python intern Company: ExampleCo Hanzilla URL: "
            "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
        )
    }
    assert legacy_portal_source_url("ats_hanzilla", role) == (
        "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
    )


def test_source_metadata_preserves_multiple_portal_urls():
    hanzilla_url = "https://jobs.hanzilla.co/jobs/software-intern-exampleco-123/"
    eluta_url = "https://www.eluta.ca/spl/software-intern-exampleco-123"

    source = source_metadata_with_portal_url(
        {
            "portal_source_url": hanzilla_url,
            "portal_source_urls": [hanzilla_url],
        },
        eluta_url,
    )

    assert source["portal_source_url"] == eluta_url
    assert source["portal_source_urls"] == sorted([hanzilla_url, eluta_url])


def test_generic_ats_ingest_endpoint_persists_hanzilla_jobs(
    jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeHanzillaAdapter:
        provider = "hanzilla"
        fetch_count = 0

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://jobs.hanzilla.co/internships/"
            assert request.limit == 2
            assert request.search_text == "python"
            if self.fetch_count:
                assert (
                    "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
                    in request.excluded_source_urls
                )
                return []
            self.fetch_count += 1
            return [
                AtsJob(
                    source_url="https://apply.example.com/jobs/123",
                    portal_source_url="https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/",
                    raw_text=(
                        "Python Developer Intern\nCompany: ExampleCo\nLocation: Toronto, Ontario\n"
                        "Hanzilla URL: https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/\n"
                        "Apply URL: https://apply.example.com/jobs/123\nPython SQL"
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://jobs.hanzilla.co/internships/"

    fake_adapter = FakeHanzillaAdapter()

    def fake_get_ats_adapter(provider: str):
        assert provider == "hanzilla"
        return fake_adapter

    allow_public_dns({"jobs.hanzilla.co", "www.eluta.ca", "apply.example.com"})
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "hanzilla",
            "source_url": "https://jobs.hanzilla.co/internships/",
            "search_text": "python",
            "limit": 2,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider"] == "hanzilla"
    assert payload["jobs_created_count"] == 1
    assert payload["jobs"][0]["source"]["source_type"] == "ats_hanzilla"
    assert payload["jobs"][0]["source"]["source_url"] == "https://apply.example.com/jobs/123"

    assert payload["jobs"][0]["source"]["portal_source_url"] == (
        "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/"
    )

    second_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "hanzilla",
            "source_url": "https://jobs.hanzilla.co/internships/",
            "search_text": "python",
            "limit": 2,
        },
    )
    assert second_response.status_code == 201
    assert second_response.json()["jobs_created_count"] == 0

    class FakeElutaAdapter:
        provider = "eluta"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url="https://apply.example.com/jobs/123",
                    portal_source_url="https://www.eluta.ca/spl/python-developer-intern-123",
                    raw_text="Python Developer Intern\nCompany: ExampleCo",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://www.eluta.ca/search?q=python"

    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeElutaAdapter())
    cross_portal_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "eluta",
            "source_url": "https://www.eluta.ca/search?q=python",
            "search_text": "python",
            "limit": 2,
        },
    )
    assert cross_portal_response.status_code == 201
    assert cross_portal_response.json()["jobs_created_count"] == 0
    with session_scope(settings) as db:
        job = db.query(JobPosting).one()
        assert job.source["portal_source_urls"] == sorted(
            [
                "https://jobs.hanzilla.co/jobs/python-developer-intern-exampleco-123/",
                "https://www.eluta.ca/spl/python-developer-intern-123",
            ]
        )
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert job.source_type == "ats_hanzilla"
        assert artifact is not None
        assert artifact.artifact_type == "job_snapshot"


def test_hanzilla_ingest_shortcuts_linkedin_variants_and_persists_portal_url(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    seeded_job = client.post(
        "/api/jobs/ingest",
        json={
            "source_type": "paste",
            "raw_text": "Platform Engineer Intern\nCompany: ExampleCo\nPython APIs",
            "source_url": None,
        },
    ).json()
    stored_linkedin_url = (
        "https://ca.linkedin.com/jobs/view/"
        "platform-engineer-intern-at-exampleco-4438093173/?trk=public_jobs"
    )
    with session_scope(settings) as db:
        job = db.get(JobPosting, seeded_job["id"])
        job.source_type = "ats_hanzilla"
        job.source_url = stored_linkedin_url

    portal_url = "https://jobs.hanzilla.co/jobs/platform-engineer-intern-exampleco-987/"
    short_linkedin_url = "https://www.linkedin.com/jobs/view/4438093173"

    class FakeHanzillaAdapter:
        provider = "hanzilla"

        def __init__(self):
            self.fetch_count = 0

        def fetch_jobs(self, request: AtsFetchRequest):
            self.fetch_count += 1
            assert request.excludes_source_url(short_linkedin_url)
            if self.fetch_count > 1:
                assert portal_url in request.excluded_source_urls
                return []
            return [
                AtsJob(
                    source_url=short_linkedin_url,
                    portal_source_url=portal_url,
                    raw_text=(
                        "Platform Engineer Intern\nCompany: ExampleCo\n"
                        f"Hanzilla URL: {portal_url}\nApply URL: {short_linkedin_url}"
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://jobs.hanzilla.co/internships/"

    fake_adapter = FakeHanzillaAdapter()
    allow_public_dns({"jobs.hanzilla.co"})
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: fake_adapter)

    def fail_posting_link_processing(*args, **kwargs):
        raise AssertionError("Canonical LinkedIn duplicates must not invoke external resolution")

    monkeypatch.setattr(
        "jobcockpit.ingestion.posting_batch.prepare_posting_link",
        fail_posting_link_processing,
    )
    request_payload = {
        "provider": "hanzilla",
        "source_url": "https://jobs.hanzilla.co/internships/",
        "limit": 50,
    }

    response = client.post("/api/jobs/ingest/ats", json=request_payload)
    assert response.status_code == 201
    assert response.json()["jobs_duplicate_count"] == 1
    with session_scope(settings) as db:
        job = db.get(JobPosting, seeded_job["id"])
        assert portal_url in job.source["portal_source_urls"]

    second_response = client.post("/api/jobs/ingest/ats", json=request_payload)
    assert second_response.status_code == 201
    assert fake_adapter.fetch_count == 2


def test_hanzilla_adapter_rejects_unsupported_hosts():
    adapter = HanzillaAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    try:
        adapter.validate_source_url("https://example.com/internships/")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Only jobs.hanzilla.co URLs are supported"
    else:
        raise AssertionError("Expected unsupported Hanzilla host to raise AtsProviderError")


def test_hanzilla_adapter_reports_missing_job_cards_as_provider_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>No jobs here</body></html>")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HanzillaAdapter(http_client=http_client)

    try:
        adapter.fetch_jobs(AtsFetchRequest(source_url="https://jobs.hanzilla.co/internships/"))
    except AtsProviderError as error:
        assert error.status_code == 502
        assert error.detail == "Hanzilla listing page did not include job cards"
    else:
        raise AssertionError("Expected missing Hanzilla cards to raise AtsProviderError")
