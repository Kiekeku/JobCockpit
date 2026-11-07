import json

import httpx

import jobcockpit.ingestion.ats_adapters.phenom_oracle as phenom_oracle_module
from jobcockpit.db.models import JobPosting, StoredArtifact
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.phenom_oracle import (
    PhenomOracleAdapter,
    phenom_oracle_job_identity,
)
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers
from jobcockpit.ingestion.role_interest import match_title_interest


def listing_job(index: int) -> dict:
    return {
        "title": f"Engineer {index}",
        "reqId": f"20{index}",
        "jobId": f"20{index}",
        "jobSeqNo": f"AGZAGAGLOBAL20{index}EXTERNALENGLOBAL",
        "location": "Calgary, AB, CA",
        "category": "Engineering",
        "businessUnit": "ATCO Energy",
        "type": "FULL_TIME",
        "postedDate": "2026-06-01T12:00:00.000+0000",
        "jobStatus": "Open",
        "descriptionTeaser": f"Engineer teaser {index}",
        "applyUrl": f"https://eezy.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/20{index}/apply/email",
    }


def detail_job(index: int) -> dict:
    return {
        **listing_job(index),
        "description": f"<p>Build Python services {index}.</p><ul><li>Maintain SQL systems</li></ul>",
        "ml_skills": ["python", "sql"],
    }


def test_phenom_oracle_identity_ignores_mutable_title_slug_and_query():
    urls = [
        "https://careers.atco.com/global/en/job/201/Engineer-1",
        "https://careers.atco.com/global/en/job/201/Renamed-Engineer/?source=board",
        "https://careers.atco.com/global/en/job/201",
    ]

    assert {
        phenom_oracle_job_identity(url)
        for url in urls
    } == {"phenom_oracle:careers.atco.com:global:en:201"}


def test_phenom_oracle_adapter_pages_listing_with_keyword_and_fetches_details(monkeypatch):
    monkeypatch.setattr(phenom_oracle_module, "PHENOM_ORACLE_PAGE_SIZE", 2)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/widgets"
        body = json.loads(request.content)
        assert body["lang"] == "en_global"
        assert body["deviceType"] == "desktop"
        assert body["country"] == "global"
        if body["ddoKey"] == "refineSearch":
            assert body["pageName"] == "search-results"
            assert body["keywords"] == "engineer"
            assert body["jobs"] is True
            assert body["counts"] is True
            if body["from"] == 0:
                assert body["size"] == 2
                return httpx.Response(
                    200,
                    json={
                        "refineSearch": {
                            "status": 200,
                            "hits": 2,
                            "totalHits": 3,
                            "data": {"jobs": [listing_job(1), listing_job(2)]},
                        }
                    },
                )
            if body["from"] == 2:
                assert body["size"] == 1
                return httpx.Response(
                    200,
                    json={
                        "refineSearch": {
                            "status": 200,
                            "hits": 1,
                            "totalHits": 3,
                            "data": {"jobs": [listing_job(3)]},
                        }
                    },
                )
        if body["ddoKey"] == "jobDetail":
            assert body["pageName"] == "job"
            job_index = int(str(body["jobSeqNo"]).removeprefix("AGZAGAGLOBAL20").removesuffix("EXTERNALENGLOBAL"))
            return httpx.Response(
                200,
                json={
                    "jobDetail": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"job": detail_job(job_index)},
                    }
                },
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://careers.atco.com/global/en/search-results",
            limit=3,
            search_text="engineer",
        )
    )

    assert [job.raw_text.splitlines()[0] for job in jobs] == ["Engineer 1", "Engineer 2", "Engineer 3"]
    assert jobs[0].source_url == "https://careers.atco.com/global/en/job/201/Engineer-1"
    assert "Req ID: 201" in jobs[0].raw_text
    assert "Location: Calgary, AB, CA" in jobs[0].raw_text
    assert "Business Unit: ATCO Energy" in jobs[0].raw_text
    assert "Category: Engineering" in jobs[0].raw_text
    assert "Employment Type: FULL_TIME" in jobs[0].raw_text
    assert "Status: Open" in jobs[0].raw_text
    assert "Apply URL: https://eezy.fa.ca2.oraclecloud.com" in jobs[0].raw_text
    assert "Skills: python, sql" in jobs[0].raw_text
    assert "Build Python services 1." in jobs[0].raw_text
    assert "Maintain SQL systems" in jobs[0].raw_text
    assert "<p>" not in jobs[0].raw_text
    post_bodies = [json.loads(request.content) for request in requests_seen if json.loads(request.content)["ddoKey"] == "refineSearch"]
    assert [body["from"] for body in post_bodies] == [0, 2]


def test_phenom_oracle_applies_shared_interest_filter_before_detail_fetch():
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body["ddoKey"] == "refineSearch":
            accounting = {**listing_job(1), "title": "Accounting Intern"}
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "status": 200,
                        "hits": 2,
                        "totalHits": 2,
                        "data": {"jobs": [accounting, listing_job(2)]},
                    }
                },
            )
        if body["ddoKey"] == "jobDetail":
            assert body["jobSeqNo"] == listing_job(2)["jobSeqNo"]
            return httpx.Response(
                200,
                json={
                    "jobDetail": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"job": detail_job(2)},
                    }
                },
            )
        return httpx.Response(404)

    adapter = PhenomOracleAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    fetch_request = AtsFetchRequest(
        source_url="https://careers.atco.com/global/en/search-results",
        limit=2,
        listing_interest_matcher=lambda title: match_title_interest(
            title,
            {"target_role_include_keywords": ["engineer"]},
        ),
    )

    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.raw_text.splitlines()[0] for job in jobs] == ["Engineer 2"]
    assert [body["ddoKey"] for body in request_bodies] == ["refineSearch", "jobDetail"]
    assert fetch_request.stats.interest_skips[0].title == "Accounting Intern"


def test_phenom_oracle_adapter_bounds_detail_attempts_during_outage():
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body["ddoKey"] == "refineSearch":
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "status": 200,
                        "hits": 2,
                        "totalHits": 3,
                        "data": {"jobs": [listing_job(1), listing_job(2)]},
                    }
                },
            )
        return httpx.Response(503)

    adapter = PhenomOracleAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = AtsFetchRequest(
        source_url="https://careers.atco.com/global/en/search-results",
        limit=2,
    )

    assert adapter.fetch_jobs(request) == []
    assert request.stats.links_seen_count == 2
    assert len(request.stats.link_failures) == 2
    assert [body["ddoKey"] for body in request_bodies] == [
        "refineSearch",
        "jobDetail",
        "jobDetail",
    ]


def test_phenom_oracle_adapter_skips_excluded_source_urls_before_detail_fetch():
    requests_seen: list[httpx.Request] = []
    excluded_url = "https://careers.atco.com/global/en/job/201/Engineer-1"

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        body = json.loads(request.content)
        if body["ddoKey"] == "refineSearch":
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"jobs": [listing_job(1)]},
                    }
                },
            )
        if body["ddoKey"] == "jobDetail":
            raise AssertionError("excluded Phenom/Oracle posting should not fetch detail payload")
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://careers.atco.com/global/en/search-results",
            limit=1,
            excluded_source_urls=frozenset({excluded_url}),
        )
    )

    assert jobs == []
    assert [json.loads(request.content)["ddoKey"] for request in requests_seen] == ["refineSearch"]


def test_phenom_oracle_adapter_fetches_single_detail_url():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        body = json.loads(request.content)
        assert body["ddoKey"] == "jobDetail"
        assert body["jobId"] == "201"
        return httpx.Response(
            200,
            json={
                "jobDetail": {
                    "status": 200,
                    "hits": 1,
                    "totalHits": 1,
                    "data": {"job": detail_job(1)},
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://careers.atco.com/global/en/job/201/Engineer-1",
            limit=10,
        )
    )

    assert len(jobs) == 1
    assert jobs[0].source_url == "https://careers.atco.com/global/en/job/201/Engineer-1"
    assert len(requests_seen) == 1


def test_phenom_oracle_adapter_skips_excluded_direct_detail_url_before_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("excluded Phenom/Oracle direct detail URL should not be fetched")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)
    source_url = "https://careers.atco.com/global/en/job/201/Engineer-1"

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url=source_url,
            limit=1,
            excluded_source_urls=frozenset({source_url}),
        )
    )

    assert jobs == []


def test_phenom_oracle_adapter_does_not_allow_facets_to_override_request_controls():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["ddoKey"] == "refineSearch":
            assert body["pageName"] == "search-results"
            assert body["from"] == 0
            assert body["size"] == 1
            assert body["selected_fields"] == {"category": ["Engineering"]}
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"jobs": [listing_job(1)]},
                    }
                },
            )
        if body["ddoKey"] == "jobDetail":
            return httpx.Response(
                200,
                json={
                    "jobDetail": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"job": detail_job(1)},
                    }
                },
            )
        return httpx.Response(500)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://careers.atco.com/global/en/search-results",
            limit=1,
            applied_facets={
                "ddoKey": "jobDetail",
                "pageName": "job",
                "from": 999,
                "size": 999,
                "selected_fields": {"category": ["Engineering"]},
            },
        )
    )

    assert len(jobs) == 1


def test_phenom_oracle_adapter_does_not_allow_facets_to_override_search_text():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["ddoKey"] == "refineSearch":
            assert body["keywords"] == "engineer"
            return httpx.Response(
                200,
                json={
                    "refineSearch": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"jobs": [listing_job(1)]},
                    }
                },
            )
        if body["ddoKey"] == "jobDetail":
            return httpx.Response(
                200,
                json={
                    "jobDetail": {
                        "status": 200,
                        "hits": 1,
                        "totalHits": 1,
                        "data": {"job": detail_job(1)},
                    }
                },
            )
        return httpx.Response(500)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(
            source_url="https://careers.atco.com/global/en/search-results",
            limit=1,
            search_text="engineer",
            applied_facets={"keywords": "manager"},
        )
    )

    assert len(jobs) == 1


def test_phenom_oracle_is_registered_as_ats_adapter():
    assert "phenom_oracle" in registered_ats_providers()
    assert isinstance(get_ats_adapter("PHENOM_ORACLE"), PhenomOracleAdapter)


def test_generic_ats_ingest_endpoint_persists_phenom_oracle_jobs(jobcockpit_settings_factory, jobcockpit_client_factory, allow_public_dns, monkeypatch):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)

    class FakePhenomOracleAdapter:
        provider = "phenom_oracle"

        def fetch_jobs(self, request: AtsFetchRequest):
            assert request.source_url == "https://careers.atco.com/global/en/search-results"
            assert request.limit == 2
            assert request.search_text == "engineer"
            return [
                AtsJob(
                    source_url="https://careers.atco.com/global/en/job/201/Engineer-1",
                    raw_text=(
                        "Engineer 1\nCompany: ATCO\nLocation: Calgary, AB, CA\n"
                        "Req ID: 201\nPython SQL"
                    ),
                )
            ]

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == "https://careers.atco.com/global/en/search-results"

    def fake_get_ats_adapter(provider: str):
        assert provider == "phenom_oracle"
        return FakePhenomOracleAdapter()

    allow_public_dns("careers.atco.com")
    monkeypatch.setattr("jobcockpit.services.workflows.get_ats_adapter", fake_get_ats_adapter)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "phenom_oracle",
            "source_url": "https://careers.atco.com/global/en/search-results",
            "search_text": "engineer",
            "limit": 2,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider"] == "phenom_oracle"
    assert payload["jobs_created_count"] == 1
    assert payload["jobs"][0]["source"]["source_type"] == "ats_phenom_oracle"
    assert payload["jobs"][0]["source"]["source_url"].endswith("/Engineer-1")

    with session_scope(settings) as db:
        job = db.query(JobPosting).one()
        artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
        assert job.source_type == "ats_phenom_oracle"
        assert artifact is not None
        assert artifact.artifact_type == "job_snapshot"


def test_phenom_oracle_adapter_rejects_unsupported_hosts():
    adapter = PhenomOracleAdapter(http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))))

    try:
        adapter.validate_source_url("https://example.com/global/en/search-results")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert error.detail == "Only supported Phenom/Oracle careers URLs are accepted"
    else:
        raise AssertionError("Expected unsupported Phenom/Oracle host to raise AtsProviderError")


def test_phenom_oracle_adapter_reports_malformed_json_as_provider_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = PhenomOracleAdapter(http_client=http_client)

    try:
        adapter.fetch_jobs(AtsFetchRequest(source_url="https://careers.atco.com/global/en/search-results"))
    except AtsProviderError as error:
        assert error.status_code == 502
        assert error.detail == "Phenom/Oracle response was not valid JSON"
    else:
        raise AssertionError("Expected malformed Phenom/Oracle JSON to raise AtsProviderError")
