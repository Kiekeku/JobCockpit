from __future__ import annotations

import json

import httpx
import pytest

from jobcockpit import cli
from jobcockpit.core.config import Settings
from jobcockpit.db.models import AuditLogEntry, JobIngestionRun
from jobcockpit.db.session import session_scope
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsProviderError,
    AtsSourceExclusion,
)
from jobcockpit.ingestion.ats_adapters.rankalpha_jobs import RankAlphaJobsAdapter
from jobcockpit.ingestion.ats_adapters.registry import (
    get_ats_adapter,
    registered_ats_providers,
)
from jobcockpit.schemas.requests import AtsIngestRequest


RANKALPHA_JOBS_URL = "https://jobs.rankalpha.io/api/jobs"


def _job(job_id: str, *, title: str, url: str) -> dict:
    return {
        "id": job_id,
        "title": title,
        "company": "Example Labs",
        "location": "Toronto",
        "country": "Canada",
        "workMode": "Hybrid",
        "status": "Open",
        "postedAt": "2026-08-20",
        "deadline": "2026-09-18",
        "verifiedAt": "2026-08-31T12:00:00Z",
        "workAuthorizationFit": "Likely eligible for Canadian student",
        "tags": ["Python", "SQL"],
        "description": "Build machine-learning data products with Python and SQL.",
        "url": url,
    }


def _payload(jobs: list[dict], *, page: int, total_pages: int) -> dict:
    return {
        "data": jobs,
        "pagination": {
            "page": page,
            "limit": 2,
            "total": 3,
            "totalPages": total_pages,
        },
        "appliedFilters": {
            "status": ["Open"],
            "country": ["Canada", "United States"],
            "tag": ["Python", "SQL"],
        },
    }


def test_rankalpha_jobs_preserves_repeated_filters_paginates_and_archives_each_job():
    first_url = "https://careers.example.com/jobs/one"
    second_url = "https://careers.example.com/jobs/two"
    third_url = "https://careers.example.com/jobs/three"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"page-one"', "X-Private": "do-not-archive"},
                json=_payload(
                    [
                        _job("job-one", title="ML Engineering Intern", url=first_url),
                        _job("job-two", title="Data Engineering Intern", url=second_url),
                    ],
                    page=1,
                    total_pages=2,
                ),
            )
        return httpx.Response(
            200,
            json=_payload(
                [_job("job-three", title="Software Engineering Intern", url=third_url)],
                page=2,
                total_pages=2,
            ),
        )

    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bearer_token="test-token",
    )
    request = AtsFetchRequest(
        source_url=(
            f"{RANKALPHA_JOBS_URL}?status=Open&country=Canada&country=United%20States"
            "&tag=Python&tag=SQL&sort=rank&order=desc&limit=2"
        ),
        limit=2,
        search_text="machine learning",
        applied_facets={"location": ["Toronto", "Waterloo"]},
        excluded_source_urls={first_url},
    )

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [second_url, third_url]
    assert [job.provider_job_id for job in jobs] == ["job-two", "job-three"]
    assert requests[0].url.params.get_list("country") == ["Canada", "United States"]
    assert requests[0].url.params.get_list("tag") == ["Python", "SQL"]
    assert requests[0].url.params.get_list("location") == ["Toronto", "Waterloo"]
    assert requests[0].url.params["q"] == "machine learning"
    assert requests[0].url.params["sort"] == "rank"
    assert requests[0].url.params["order"] == "desc"
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert all(request.url.params["limit"] == "2" for request in requests)
    assert request.stats.links_seen_count == 2
    assert request.stats.source_exclusions == [
        AtsSourceExclusion(
            category="already_imported",
            count=1,
            reason="RankAlpha jobs already present in JobCockpit",
        )
    ]
    assert "ML Engineering Intern" not in jobs[0].raw_text
    assert "Data Engineering Intern" in jobs[0].raw_text
    assert "Work Authorization Fit: Likely eligible for Canadian student" in jobs[0].raw_text
    assert "Tags: Python, SQL" in jobs[0].raw_text
    assert jobs[0].source_archive is not None
    assert json.loads(jobs[0].source_archive.body) == _job(
        "job-two", title="Data Engineering Intern", url=second_url
    )
    assert jobs[0].source_archive.headers == {
        "etag": '"page-one"',
        "x-jobcockpit-capture-source": "rankalpha_api_job_object",
    }
    assert jobs[0].source_archive.source_url == str(requests[0].url)
    assert b"test-token" not in jobs[0].source_archive.body


def test_rankalpha_jobs_configures_token_from_settings():
    requested_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_authorization.append(request.headers["Authorization"])
        return httpx.Response(200, json=_payload([], page=1, total_pages=1))

    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    adapter.configure_settings(Settings(rankalpha_jobs_api_token="settings-token"))

    assert adapter.fetch_jobs(AtsFetchRequest(source_url=RANKALPHA_JOBS_URL)) == []
    assert requested_authorization == ["Bearer settings-token"]


def test_outbound_id_exclusion_precedes_link_budget_even_when_url_changes():
    def handler(request):
        return httpx.Response(200, json=_payload([
            _job("outbound-one", title="Engineering Intern", url="https://new.example/jobs/1"),
            _job("native-two", title="Engineering Intern", url="https://new.example/jobs/2"),
        ], page=1, total_pages=1))

    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)), bearer_token="read-token",
    )
    request = AtsFetchRequest(source_url=RANKALPHA_JOBS_URL, limit=1,
                              excluded_provider_job_ids=frozenset({"outbound-one"}))
    jobs = adapter.fetch_jobs(request)
    assert [job.provider_job_id for job in jobs] == ["native-two"]
    assert request.stats.links_seen_count == 1
    assert request.stats.source_exclusions[0].count == 1


def test_rankalpha_jobs_requires_configured_token():
    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )
    )

    with pytest.raises(
        AtsProviderError,
        match="JOBCOCKPIT_RANKALPHA_JOBS_API_TOKEN is not configured",
    ) as error:
        adapter.fetch_jobs(AtsFetchRequest(source_url=RANKALPHA_JOBS_URL))

    assert error.value.status_code == 503


@pytest.mark.parametrize("status_code", [401, 403])
def test_rankalpha_jobs_reports_rejected_token_without_exposing_it(status_code: int):
    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status_code,
                    json={"error": "unauthorized", "message": "bad secret-token"},
                )
            )
        ),
        bearer_token="secret-token",
    )

    with pytest.raises(AtsProviderError, match="rejected the configured bearer token") as error:
        adapter.fetch_jobs(AtsFetchRequest(source_url=RANKALPHA_JOBS_URL))

    assert error.value.status_code == 502
    assert "secret-token" not in error.value.detail


@pytest.mark.parametrize(
    "source_url",
    [
        "http://jobs.rankalpha.io/api/jobs",
        "https://example.com/api/jobs",
        "https://jobs.rankalpha.io/jobs",
        "https://jobs.rankalpha.io/api/jobs#open",
        "https://jobs.rankalpha.io/api/jobs?access_token=secret",
    ],
)
def test_rankalpha_jobs_rejects_unsupported_or_secret_bearing_urls(source_url: str):
    adapter = RankAlphaJobsAdapter(bearer_token="test-token")

    with pytest.raises(AtsProviderError, match="Only https://jobs.rankalpha.io/api/jobs"):
        adapter.validate_source_url(source_url)


@pytest.mark.parametrize(
    ("source_url", "applied_facets"),
    [
        (f"{RANKALPHA_JOBS_URL}?accessToken=secret", {}),
        (f"{RANKALPHA_JOBS_URL}?x-api-key=secret", {}),
        (f"{RANKALPHA_JOBS_URL}?rankalphaAccessToken=secret", {}),
        (f"{RANKALPHA_JOBS_URL}?client-secret-value=secret", {}),
        (f"{RANKALPHA_JOBS_URL}?proxy-authorization=secret", {}),
        (RANKALPHA_JOBS_URL, {"bearer_token": "secret"}),
        (RANKALPHA_JOBS_URL, {"x-api-key": "secret"}),
    ],
)
def test_rankalpha_credentials_are_rejected_before_ingestion_audit_persistence(
    source_url: str,
    applied_facets: dict,
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
):
    settings = jobcockpit_settings_factory(rankalpha_jobs_api_token="configured-token")
    client = jobcockpit_client_factory(settings)

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "rankalpha_jobs",
            "source_url": source_url,
            "applied_facets": applied_facets,
        },
    )

    assert response.status_code == 422
    with session_scope(settings) as db:
        assert db.query(JobIngestionRun).count() == 0
        assert db.query(AuditLogEntry).count() == 0


def test_rankalpha_work_authorization_fit_remains_an_allowed_filter():
    request = AtsIngestRequest(
        provider="rankalpha_jobs",
        source_url=(
            f"{RANKALPHA_JOBS_URL}"
            "?workAuthorizationFit=Likely%20eligible%20for%20Canadian%20student"
        ),
    )

    assert "workAuthorizationFit=" in request.source_url


def test_rankalpha_jobs_rejects_response_contract_drift():
    adapter = RankAlphaJobsAdapter(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [], "pagination": {}})
            )
        ),
        bearer_token="test-token",
    )

    with pytest.raises(AtsProviderError, match="response was invalid"):
        adapter.fetch_jobs(AtsFetchRequest(source_url=RANKALPHA_JOBS_URL))


def test_rankalpha_jobs_is_registered_as_ats_adapter():
    assert "rankalpha_jobs" in registered_ats_providers()
    assert isinstance(get_ats_adapter("RANKALPHA_JOBS"), RankAlphaJobsAdapter)


def test_rankalpha_jobs_cli_posts_filtered_source_url(monkeypatch, capsys):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 20,
                "jobs_created_count": 18,
                "jobs_duplicate_count": 2,
            }

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)
    source_url = (
        f"{RANKALPHA_JOBS_URL}?country=United%20States"
        "&workAuthorizationFit=Likely%20eligible%20for%20Canadian%20student"
    )

    exit_code = cli.main(
        [
            "ats",
            "ingest-rankalpha",
            "--source-url",
            source_url,
            "--search-text",
            "machine learning",
            "--limit",
            "20",
            "--json",
        ]
    )

    assert exit_code == 0
    assert calls[0]["json"] == {
        "provider": "rankalpha_jobs",
        "source_url": source_url,
        "search_text": "machine learning",
        "limit": 20,
        "skip_interest_filter": False,
        "skip_language_filter": False,
    }
    assert json.loads(capsys.readouterr().out)["provider"] == "rankalpha_jobs"


def test_rankalpha_jobs_cli_default_includes_open_and_closing_soon(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jobs_seen_count": 0,
                "jobs_created_count": 0,
                "jobs_duplicate_count": 0,
            }

    def fake_post(url, *, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(cli.httpx, "post", fake_post)

    assert cli.main(["ats", "ingest-rankalpha"]) == 0
    assert calls[0]["source_url"] == (
        f"{RANKALPHA_JOBS_URL}?status=Open&status=Closing%20soon&sort=rank"
    )
