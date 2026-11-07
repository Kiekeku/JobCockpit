import json

import httpx

import jobcockpit.ingestion.ats_adapters.workday as workday_module
from jobcockpit.services import workflows as api_main
from jobcockpit.db.models import (
    AuditLogEntry,
    CandidateProfile,
    JobIngestionItem,
    JobIngestionRun,
    JobPosting,
    JobReprocessRun,
    StoredArtifact,
)
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers
from jobcockpit.ingestion.ats_adapters.workday import (
    WorkdayAdapter,
    canonicalize_workday_job_url,
    parse_workday_url,
)
from jobcockpit.ingestion.llm_providers import register_fit_analyzer_provider
from jobcockpit.ingestion.role_interest import match_title_interest


def test_workday_adapter_fetches_listing_and_detail_payloads():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST" and request.url.path == "/wday/cxs/suncor/Suncor_External/jobs":
            assert json.loads(request.content) == {
                "appliedFacets": {},
                "limit": 2,
                "offset": 0,
                "searchText": "",
            }
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Data Engineer",
                            "externalPath": "/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                            "locationsText": "Calgary, AB, CAN",
                            "postedOn": "Posted Today",
                            "bulletFields": ["R0012345"],
                        }
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/wday/cxs/suncor/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345":
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Data Engineer",
                        "jobReqId": "R0012345",
                        "location": "Calgary, AB, CAN",
                        "timeType": "Full time",
                        "postedOn": "Posted Today",
                        "externalUrl": "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                        "jobDescription": "<p>Build Python services.</p><ul><li>SQL pipelines</li></ul>",
                    }
                },
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            limit=2,
        )
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"
    assert "Data Engineer" in jobs[0].raw_text
    assert "R0012345" in jobs[0].raw_text
    assert "Calgary, AB, CAN" in jobs[0].raw_text
    assert "Build Python services." in jobs[0].raw_text
    assert "SQL pipelines" in jobs[0].raw_text
    assert "<p>" not in jobs[0].raw_text
    assert jobs[0].source_archive is not None
    assert jobs[0].source_archive.source_url == (
        "https://suncor.wd1.myworkdayjobs.com/wday/cxs/suncor/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"
    )
    assert jobs[0].source_archive.status_code == 200
    assert jobs[0].source_archive.content_type.startswith("application/json")
    assert b"Data Engineer" in jobs[0].source_archive.body
    assert [request.method for request in requests_seen] == ["POST", "GET"]


def test_workday_adapter_applies_shared_interest_filter_before_detail_fetch():
    requests_seen: list[httpx.Request] = []
    data_url = (
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/"
        "job/Calgary-AB-CAN/Data-Engineer_R002"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "jobPostings": [
                        {
                            "title": "Accounting Intern",
                            "externalPath": "/job/Calgary-AB-CAN/Accounting-Intern_R001",
                        },
                        {
                            "title": "Data Engineer",
                            "externalPath": "/job/Calgary-AB-CAN/Data-Engineer_R002",
                        },
                    ],
                },
            )
        if request.url.path.endswith("Accounting-Intern_R001"):
            raise AssertionError("interest-filtered Workday detail should not be fetched")
        if request.url.path.endswith("Data-Engineer_R002"):
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Data Engineer",
                        "jobReqId": "R002",
                        "externalUrl": data_url,
                        "jobDescription": "<p>Build data platforms.</p>",
                    }
                },
            )
        return httpx.Response(404)

    adapter = WorkdayAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    fetch_request = AtsFetchRequest(
        source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
        limit=2,
        listing_interest_matcher=lambda title: match_title_interest(
            title,
            {"target_role_include_keywords": ["data"]},
        ),
    )

    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.source_url for job in jobs] == [data_url]
    assert [request.method for request in requests_seen] == ["POST", "GET"]
    assert fetch_request.stats.interest_skips[0].title == "Accounting Intern"


def test_workday_adapter_classifies_retryable_detail_failure_and_continues():
    failed_url = (
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Engineer-1_R001"
    )
    valid_url = (
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Engineer-2_R002"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "jobPostings": [
                        {
                            "title": "Engineer 1",
                            "externalPath": "/job/Calgary-AB-CAN/Engineer-1_R001",
                        },
                        {
                            "title": "Engineer 2",
                            "externalPath": "/job/Calgary-AB-CAN/Engineer-2_R002",
                        },
                    ],
                },
            )
        if request.url.path.endswith("_R001"):
            return httpx.Response(503)
        if request.url.path.endswith("_R002"):
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "title": "Engineer 2",
                        "jobReqId": "R002",
                        "externalUrl": valid_url,
                        "jobDescription": "<p>Build reliable systems.</p>",
                    }
                },
            )
        return httpx.Response(404)

    adapter = WorkdayAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(
        source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
        limit=2,
    )

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [valid_url]
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 1
    assert request.stats.link_failures[0].source_url == failed_url
    assert request.stats.link_failures[0].category == "retryable"
    assert request.stats.link_failures[0].upstream_status_code == 503


def test_workday_adapter_bounds_detail_attempts_during_outage():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 3,
                    "jobPostings": [
                        {
                            "title": f"Engineer {index}",
                            "externalPath": f"/job/Calgary-AB-CAN/Engineer-{index}_R00{index}",
                        }
                        for index in range(1, 3)
                    ],
                },
            )
        return httpx.Response(503)

    adapter = WorkdayAdapter(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = AtsFetchRequest(
        source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
        limit=2,
    )

    assert adapter.fetch_jobs(request) == []
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 2
    assert [request.method for request in requests_seen] == ["POST", "GET", "GET"]


def test_workday_adapter_skips_excluded_source_urls_before_detail_fetch():
    requests_seen: list[httpx.Request] = []
    excluded_url = "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST" and request.url.path == "/wday/cxs/suncor/Suncor_External/jobs":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Data Engineer",
                            "externalPath": "/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                        }
                    ],
                },
            )
        if request.method == "GET":
            raise AssertionError("excluded Workday posting should not fetch detail payload")
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            limit=1,
            excluded_source_urls=frozenset({excluded_url}),
        )
    )

    assert jobs == []
    assert [request.method for request in requests_seen] == ["POST"]


def test_workday_adapter_skips_excluded_direct_detail_url_before_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("excluded Workday direct detail URL should not be fetched")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(http_client=http_client)
    source_url = "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url=source_url,
            limit=1,
            excluded_source_urls=frozenset({source_url}),
        )
    )

    assert jobs == []


def test_workday_adapter_pages_listing_results_with_search_text(monkeypatch):
    monkeypatch.setattr(workday_module, "WORKDAY_PAGE_SIZE", 2)
    requests_seen: list[httpx.Request] = []

    def listing_posting(index: int) -> dict:
        return {
            "title": f"Engineer {index}",
            "externalPath": f"/job/Calgary-AB-CAN/Engineer-{index}_R00{index}",
        }

    def detail_payload(index: int) -> dict:
        return {
            "jobPostingInfo": {
                "title": f"Engineer {index}",
                "jobReqId": f"R00{index}",
                "externalUrl": f"https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Engineer-{index}_R00{index}",
                "jobDescription": "<p>Build production systems.</p>",
            }
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST" and request.url.path == "/wday/cxs/suncor/Suncor_External/jobs":
            body = json.loads(request.content)
            assert body["searchText"] == "engineer"
            if body["offset"] == 0:
                assert body["limit"] == 2
                return httpx.Response(200, json={"total": 3, "jobPostings": [listing_posting(1), listing_posting(2)]})
            if body["offset"] == 2:
                assert body["limit"] == 1
                return httpx.Response(200, json={"total": 3, "jobPostings": [listing_posting(3)]})
        if request.method == "GET" and request.url.path.endswith("_R001"):
            return httpx.Response(200, json=detail_payload(1))
        if request.method == "GET" and request.url.path.endswith("_R002"):
            return httpx.Response(200, json=detail_payload(2))
        if request.method == "GET" and request.url.path.endswith("_R003"):
            return httpx.Response(200, json=detail_payload(3))
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            limit=3,
            search_text="engineer",
        )
    )

    assert [job.raw_text.splitlines()[0] for job in jobs] == ["Engineer 1", "Engineer 2", "Engineer 3"]
    post_bodies = [json.loads(request.content) for request in requests_seen if request.method == "POST"]
    assert [body["offset"] for body in post_bodies] == [0, 2]


def test_workday_is_registered_as_ats_adapter():
    assert "workday" in registered_ats_providers()
    assert isinstance(get_ats_adapter("WORKDAY"), WorkdayAdapter)


def test_workday_apply_candidate_fetches_detail_without_application_suffix():
    # Observed TD search candidate ended in /apply; its CXS endpoint returned
    # 404 while the same requisition's detail endpoint returned the full job.
    base = "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Analytics-Intern_R123"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/apply'):
            return httpx.Response(404)
        return httpx.Response(200, json={"jobPostingInfo": {
            "title": "Analytics Intern", "jobReqId": "R123",
            "jobDescription": "Build analytics tools with our engineering team.",
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        jobs = WorkdayAdapter(client).fetch_jobs(AtsFetchRequest(source_url=base + '/apply/', limit=1))
    assert len(jobs) == 1
    assert seen == ['/wday/cxs/acme/Careers/job/Analytics-Intern_R123']
    assert canonicalize_workday_job_url(base + '/apply?source=search') == base.replace('/en-US', '')
    assert parse_workday_url(base.replace('Analytics-Intern_R123', 'apply')).external_path == '/job/apply'


def test_workday_job_url_canonicalization_preserves_site_and_job_path():
    expected_public_url = (
        "https://interac.wd3.myworkdayjobs.com/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272"
    )
    assert canonicalize_workday_job_url(
        "https://interac.wd3.myworkdayjobs.com/en-US/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272?source=LinkedIn"
    ) == expected_public_url
    assert canonicalize_workday_job_url(
        "https://interac.wd3.myworkdayjobs.com/wday/cxs/interac/Interac/job/Toronto/"
        "Software-Development-Intern_REQ-2272"
    ) == expected_public_url

    expected_recruiting_url = (
        "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/"
        "Penetanguishene-Ontario-CA/IT-Co-op_R00230813"
    )
    assert canonicalize_workday_job_url(
        "https://wd3.myworkdaysite.com/en-US/recruiting/magna/Magna/job/"
        "Penetanguishene-Ontario-CA/IT-Co-op_R00230813?source=LinkedIn"
    ) == expected_recruiting_url


def test_workday_canonicalization_does_not_turn_listings_into_job_urls():
    assert (
        canonicalize_workday_job_url(
            "https://interac.wd3.myworkdayjobs.com/en-US/Interac?source=LinkedIn"
        )
        == ""
    )


def test_workday_two_letter_site_name_is_not_treated_as_locale():
    source_url = (
        "https://acme.wd1.myworkdayjobs.com/IT/job/Toronto/"
        "Software-Engineer-Intern_R001"
    )

    site = parse_workday_url(source_url)

    assert site.site == "IT"
    assert site.external_path == "/job/Toronto/Software-Engineer-Intern_R001"
    assert canonicalize_workday_job_url(source_url) == source_url


def test_generic_ats_ingest_endpoint_persists_fetched_jobs(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"
            assert request.limit == 2
            assert request.search_text == "engineer"
            assert request.applied_facets == {}
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    def fake_get_ats_adapter(provider: str):
        assert provider == "workday"
        return FakeWorkdayAdapter()

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "workday",
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            "search_text": "engineer",
            "limit": 2,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider"] == "workday"
    assert payload["jobs_created_count"] == 1
    assert payload["jobs"][0]["source"]["source_type"] == "workday"
    assert payload["jobs"][0]["source"]["source_url"].endswith("/Data-Engineer_R0012345")

    with session_scope(settings) as db:
        job = db.query(JobPosting).one()
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert job.source_type == "workday"
        assert job.company_name == "Suncor"
        assert artifact is not None
        assert artifact.artifact_type == "job_snapshot"


def test_generic_ats_ingest_endpoint_records_run_counts_and_duplicates(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                ),
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                ),
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 2
    assert payload["jobs_created_count"] == 1
    assert payload["jobs_duplicate_count"] == 1
    assert payload["ingestion_run"]["status"] == "completed"
    assert payload["ingestion_run"]["alert_level"] == "warning"
    assert payload["alerts"] == ["1 duplicate posting skipped."]

    runs_response = client.get("/api/ingestion-runs")
    assert runs_response.status_code == 200
    runs = runs_response.json()
    assert len(runs) == 1
    assert runs[0]["provider"] == "workday"
    assert runs[0]["jobs_seen_count"] == 2
    assert runs[0]["jobs_duplicate_count"] == 1

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.status == "completed"
        assert run.jobs_duplicate_count == 1
        events = {row.event_type: row.payload for row in db.query(AuditLogEntry).all()}
        assert "ingestion.run_started" in events
        completed_counts = events["ingestion.run_completed"]["counts"]
        assert completed_counts["items_seen_count"] == 2
        assert completed_counts["jobs_created_count"] == 1
        assert completed_counts["jobs_reused_count"] == 1


def test_generic_ats_ingest_endpoint_skips_previously_imported_postings_before_processing(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    posting_url = (
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"
    )

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url=posting_url,
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    first_response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )
    assert first_response.status_code == 201
    assert first_response.json()["jobs_created_count"] == 1

    def fail_if_duplicate_reaches_posting_processing(*args, **kwargs):
        raise AssertionError("previously imported ATS posting should be skipped before posting processing")

    monkeypatch.setattr("jobcockpit.ingestion.posting_batch.prepare_posting_link", fail_if_duplicate_reaches_posting_processing)

    second_response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert second_response.status_code == 201
    payload = second_response.json()
    assert payload["jobs_seen_count"] == 1
    assert payload["jobs_created_count"] == 0
    assert payload["jobs_duplicate_count"] == 1
    assert payload["jobs"][0]["source"]["source_url"] == posting_url

def test_generic_ats_ingest_endpoint_records_failed_run_and_alert(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FailingWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            raise AtsProviderError(502, "Workday listing request failed")

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FailingWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Workday listing request failed"

    runs_response = client.get("/api/ingestion-runs")
    assert runs_response.status_code == 200
    runs = runs_response.json()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["alert_level"] == "error"
    assert runs[0]["error_message"] == "Workday listing request failed"

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.status == "failed"
        assert run.error_message == "Workday listing request failed"
        events = {row.event_type: row.payload for row in db.query(AuditLogEntry).all()}
        assert "ingestion.run_started" in events
        failed_counts = events["ingestion.run_failed"]["counts"]
        assert failed_counts["items_seen_count"] == 0
        assert failed_counts["items_failed_count"] == 0
        assert failed_counts["runs_failed_count"] == 1


def test_generic_ats_ingest_partial_failure_commits_success_and_records_items(
    jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class PartiallyFailingWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                ),
                AtsJob(
                    source_url="http://169.254.169.254/latest/meta-data",
                    raw_text="Blocked Engineer\nCompany: Suncor\nPython SQL",
                ),
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: PartiallyFailingWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_created_count"] == 1
    assert payload["ingestion_run"]["status"] == "completed_with_warnings"
    assert payload["ingestion_run"]["alert_level"] == "warning"
    items_response = client.get(
        f"/api/ingestion-runs/{payload['ingestion_run']['ingestion_run_id']}/items"
    )
    assert items_response.status_code == 200
    api_items_by_url = {item["source_url"]: item for item in items_response.json()}
    assert api_items_by_url[
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"
    ]["status"] == "imported"
    assert api_items_by_url["http://169.254.169.254/latest/meta-data"]["status"] == "blocked_by_url_policy"
    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.status == "completed_with_warnings"
        assert run.jobs_seen_count == 2
        assert run.jobs_created_count == 1
        assert db.query(JobPosting).count() == 1
        items_by_url = {item.source_url: item for item in db.query(JobIngestionItem).all()}
        items = [
            items_by_url["https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345"],
            items_by_url["http://169.254.169.254/latest/meta-data"],
        ]
        assert items[0].linked_job_id is not None
        assert items[0].retryable is False
        assert items[1].linked_job_id is None
        assert items[1].error_message == "private_network_blocked"
        assert items[1].retryable is False


def test_generic_ats_ingest_rolls_back_only_failed_item_and_cleans_its_artifacts(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    source_url = "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    class ThreeJobWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url=f"{source_url}/job/Calgary-AB-CAN/Data-Engineer_R001",
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL",
                ),
                AtsJob(
                    source_url=f"{source_url}/job/Calgary-AB-CAN/Failing-Engineer_R002",
                    raw_text="Failing Engineer\nCompany: Suncor\nPython SQL",
                ),
                AtsJob(
                    source_url=f"{source_url}/job/Calgary-AB-CAN/Platform-Engineer_R003",
                    raw_text="Platform Engineer\nCompany: Suncor\nPython SQL",
                ),
            ]

        def validate_source_url(self, value: str) -> None:
            assert value == source_url

    real_persist = api_main.persist_job_ingest_result
    # Retain sessions so Python cannot recycle an object ID between items.
    item_sessions = []

    def fail_after_persist(request, db, storage, active_settings):
        item_sessions.append(db)
        result = real_persist(request, db, storage, active_settings)
        if "Failing Engineer" in request.raw_text:
            raise RuntimeError("synthetic item failure after artifact write")
        return result

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: ThreeJobWorkdayAdapter(),
    )
    monkeypatch.setattr(api_main, "persist_job_ingest_result", fail_after_persist)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": source_url},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_created_count"] == 2
    assert payload["ingestion_run"]["status"] == "completed_with_warnings"
    with session_scope(settings) as db:
        assert db.query(JobPosting).count() == 2
        assert db.query(StoredArtifact).count() == 2
        items_by_url = {item.source_url: item for item in db.query(JobIngestionItem).all()}
        items = [
            items_by_url[f"{source_url}/job/Calgary-AB-CAN/Data-Engineer_R001"],
            items_by_url[f"{source_url}/job/Calgary-AB-CAN/Failing-Engineer_R002"],
            items_by_url[f"{source_url}/job/Calgary-AB-CAN/Platform-Engineer_R003"],
        ]
        assert [item.status for item in items] == ["imported", "failed", "imported"]
        assert items[1].error_message == "synthetic item failure after artifact write"
        assert items[1].retryable is True
    snapshot_files = list((settings.artifact_root / "job-snapshots").iterdir())
    assert len(snapshot_files) == 2
    assert len({id(session) for session in item_sessions}) == 3


def test_generic_ats_ingest_queues_enabled_fit_analysis(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    calls: list[str] = []

    class FitAnalyzerThatMustRunInWorker:
        def analyze(self, *args, **kwargs):
            calls.append("analyze")
            raise AssertionError("fit analysis must not run in the ATS request")

    register_fit_analyzer_provider(
        "unit_test_deferred_ats_fit",
        lambda _settings: FitAnalyzerThatMustRunInWorker(),
        replace=True,
    )
    settings = jobcockpit_settings_factory(
        ats_mock_port=8001,
        fit_analysis_provider="unit_test_deferred_ats_fit",
    )
    client = jobcockpit_client_factory(settings)
    source_url = "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    class OneJobWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url=f"{source_url}/job/Calgary-AB-CAN/Data-Engineer_R001",
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL",
                )
            ]

        def validate_source_url(self, value: str) -> None:
            assert value == source_url

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: OneJobWorkdayAdapter(),
    )

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": source_url},
    )

    assert response.status_code == 201
    assert response.json()["jobs_created_count"] == 1
    assert calls == []
    with session_scope(settings) as db:
        run = db.query(JobReprocessRun).one()
        assert run.run_type == "fit_analysis"
        assert run.status == "queued"


def test_generic_ats_ingest_endpoint_closes_closeable_adapter(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    adapter_instances = []

    class FakeWorkdayAdapter:
        provider = "workday"

        def __init__(self) -> None:
            self.closed = False

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

        def close(self) -> None:
            self.closed = True

    def fake_get_ats_adapter(provider: str):
        adapter = FakeWorkdayAdapter()
        adapter_instances.append(adapter)
        return adapter

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 201
    assert adapter_instances
    assert adapter_instances[0].closed is True


def test_legacy_workday_ingest_endpoint_uses_registered_adapter(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"
            assert request.limit == 1
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Calgary-AB-CAN/Data-Engineer_R0012345",
                    raw_text="Data Engineer\nCompany: Suncor\nLocation: Calgary, AB, CAN\nReq ID: R0012345\nPython SQL",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    def fake_get_ats_adapter(provider: str):
        assert provider == "workday"
        return FakeWorkdayAdapter()

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/workday",
        json={"source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External", "limit": 1},
    )

    assert response.status_code == 201
    assert response.json()["provider"] == "workday"


def test_generic_ats_ingest_endpoint_rejects_unknown_provider(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "oracle", "source_url": "https://example.com/jobs", "limit": 2},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported ATS provider: oracle"


def test_generic_ats_ingest_endpoint_rejects_non_workday_urls(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://example.com/jobs", "limit": 2},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only Workday public URLs are supported"


def test_workday_rejects_unsupported_myworkdaysite_paths():
    adapter = WorkdayAdapter()

    for source_url in (
        "https://wd3.myworkdaysite.com/recruiting/magna/Magna",
        "https://wd3.myworkdaysite.com/recruiting/magna/Magna/foo",
        "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job",
        "https://wd3.myworkdaysite.com/not-a-recruiting-job-path",
    ):
        try:
            adapter.validate_source_url(source_url)
        except AtsProviderError as error:
            assert error.status_code == 400
            assert error.detail == "Workday myworkdaysite URL must include a recruiting job path"
        else:
            raise AssertionError("Expected unsupported myworkdaysite URL to raise AtsProviderError")
    adapter.close()


def test_workday_adapter_reports_malformed_json_as_provider_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(http_client=http_client)

    try:
        adapter.fetch_jobs(AtsFetchRequest(source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External"))
    except AtsProviderError as error:
        assert error.status_code == 502
        assert error.detail == "Workday response was not valid JSON"
    else:
        raise AssertionError("Expected malformed Workday JSON to raise AtsProviderError")


def test_public_job_ingest_rejects_adapter_source_type_without_url(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/jobs/ingest",
        json={"source_type": "ats_oracle", "raw_text": "Oracle Role\nPython SQL", "source_url": None},
    )

    assert response.status_code == 422


def test_public_job_ingest_rejects_adapter_source_types(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    for source_type in ("workday", "ats_oracle"):
        response = client.post(
            "/api/jobs/ingest",
            json={
                "source_type": source_type,
                "raw_text": "Adapter Role\nPython SQL",
                "source_url": "http://localhost:8001/ats/generic/acme/jobs/123",
            },
        )

        assert response.status_code == 422

    with session_scope(settings) as db:
        assert db.query(JobPosting).count() == 0


def test_generic_ats_ingest_filters_jobs_by_profile_role_interest(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            data_url = (
                "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001"
            )
            accounting_url = (
                "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Accounting-Intern_R002"
            )
            assert request.allows_listing_candidate(data_url, "Data Engineer")
            assert not request.allows_listing_candidate(accounting_url, "Accounting Intern")
            assert request.try_begin_link_attempt()
            return [
                AtsJob(
                    source_url=data_url,
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL",
                    title="Data Engineer",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                },
            )
        )

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 1
    assert payload["jobs_created_count"] == 1
    assert payload["jobs_skipped_count"] == 1
    assert payload["skipped_jobs"][0]["title"] == "Accounting Intern"
    assert payload["alerts"] == ["1 posting skipped by role-interest filter."]
    assert payload["jobs"][0]["role"]["job_title"] == "Data Engineer"

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.jobs_skipped_count == 1
        assert db.query(JobPosting).count() == 1


def test_generic_ats_ingest_language_filter_still_applies_to_previously_imported_posting(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    posting_url = "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001"

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url=posting_url,
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL\nFrench required for this role.",
                    title="Data Engineer",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    first_response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "workday",
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            "skip_language_filter": True,
        },
    )
    assert first_response.status_code == 201
    assert first_response.json()["jobs_created_count"] == 1

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )

    second_response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert second_response.status_code == 201
    payload = second_response.json()
    assert payload["jobs_created_count"] == 0
    assert payload["jobs_duplicate_count"] == 0
    assert payload["jobs_language_skipped_count"] == 1
    assert payload["language_skipped_jobs"] == [
        {
            "source_url": posting_url,
            "reason": "unmet_required_language: French",
        }
    ]
    assert payload["jobs"] == []

def test_generic_ats_ingest_filters_jobs_by_unmet_required_language(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001",
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL\nFrench required for this role.",
                    title="Data Engineer",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/ats",
        json={"provider": "workday", "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 1
    assert payload["jobs_created_count"] == 0
    assert payload["jobs_skipped_count"] == 0
    assert payload["jobs_language_skipped_count"] == 1
    assert payload["language_skipped_jobs"] == [
        {
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001",
            "reason": "unmet_required_language: French",
        }
    ]
    assert payload["alerts"] == ["1 posting skipped by language filter."]
    assert payload["jobs"] == []

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.jobs_skipped_count == 0
        assert run.jobs_language_skipped_count == 1
        assert db.query(JobPosting).count() == 0



def test_workday_ingest_endpoint_can_skip_language_filter(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001",
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL\nFrench required for this role.",
                    title="Data Engineer",
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "language_filter_enabled": True,
                    "languages": [
                        {"language": "English", "proficiency": "fluent"},
                        {"language": "French", "proficiency": "none"},
                    ],
                },
            )
        )

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/workday",
        json={
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            "skip_language_filter": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 1
    assert payload["jobs_created_count"] == 1
    assert payload["jobs_language_skipped_count"] == 0
    assert payload["language_skipped_jobs"] == []
    assert payload["jobs"][0]["role"]["job_title"] == "Data Engineer"

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.jobs_language_skipped_count == 0
        assert run.request_payload["skip_language_filter"] is True
        assert db.query(JobPosting).count() == 1

def test_workday_ingest_endpoint_can_skip_role_interest_filter(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakeWorkdayAdapter:
        provider = "workday"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"
            assert request.listing_interest_matcher is None
            assert request.allows_listing_candidate("https://example.com/accounting", "Accounting Intern")
            return [
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Data-Engineer_R001",
                    raw_text="Data Engineer\nCompany: Suncor\nPython SQL",
                    title="Data Engineer",
                ),
                AtsJob(
                    source_url="https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/Accounting-Intern_R002",
                    raw_text="Accounting Intern\nCompany: Suncor\nExcel reconciliation",
                    title="Accounting Intern",
                ),
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://suncor.wd1.myworkdayjobs.com/Suncor_External"

    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Candidate",
                    "email": "jane@example.test",
                    "target_role_include_keywords": ["engineer"],
                    "target_role_exclude_keywords": ["accounting"],
                },
            )
        )

    allow_public_dns("suncor.wd1.myworkdayjobs.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", lambda provider: FakeWorkdayAdapter())

    response = client.post(
        "/api/jobs/ingest/workday",
        json={
            "source_url": "https://suncor.wd1.myworkdayjobs.com/Suncor_External",
            "skip_interest_filter": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["jobs_seen_count"] == 2
    assert payload["jobs_created_count"] == 2
    assert payload["jobs_skipped_count"] == 0
    assert payload["skipped_jobs"] == []
    assert {job["role"]["job_title"] for job in payload["jobs"]} == {"Data Engineer", "Accounting Intern"}

    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.jobs_skipped_count == 0
        assert run.request_payload["skip_interest_filter"] is True
        assert db.query(JobPosting).count() == 2
