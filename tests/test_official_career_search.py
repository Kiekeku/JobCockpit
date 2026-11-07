from __future__ import annotations

import httpx

from jobcockpit.integrations import official_careers as official_careers_module
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint
from jobcockpit.ingestion.posting_resolution import SearchResult
from jobcockpit.integrations.official_careers import (
    GreenhouseCareerSiteSearchClient,
    JibeCareerSiteSearchClient,
    RecruiteeCareerSiteSearchClient,
    ReviewedOfficialCareerSearchClient,
    WorkdayCareerSiteSearchClient,
)


def test_owned_http_client_is_closed_after_each_search(monkeypatch) -> None:
    created_clients: list[httpx.Client] = []
    real_client = httpx.Client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "data": {
                            "title": "Short Term 2027 Data Analyst Intern/Co-Op",
                            "slug": "91311",
                            "hiring_organization": "Advanced Micro Devices, Inc",
                        }
                    }
                ]
            },
        )

    def client_factory(**kwargs) -> httpx.Client:
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        created_clients.append(client)
        return client

    monkeypatch.setattr(official_careers_module.httpx, "Client", client_factory)
    client = JibeCareerSiteSearchClient()

    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4459291699",
            source_kind="linkedin",
            company="AMD",
            title="Short Term 2027 Data Analyst Intern/Co-Op",
        ),
        5,
    )

    assert [result.url for result in results] == [
        "https://careers.amd.com/careers-home/jobs/91311"
    ]
    assert len(created_clients) == 1
    assert created_clients[0].is_closed


def test_reviewed_search_preserves_supported_provider_request_failure() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, text="temporarily unavailable")
        )
    )
    client = ReviewedOfficialCareerSearchClient(
        search_client=type(
            "UnusedSearchClient",
            (),
            {
                "provider_code": "serper",
                "last_error": "",
                "search": lambda self, query, limit: [],
            },
        )(),
        http_client=http_client,
    )

    try:
        results = client.search(
            ExternalPostingHint(
                source_url="https://www.linkedin.com/jobs/view/123",
                source_kind="linkedin",
                company="Huawei Canada",
                title="Co-op Engineer - AI Software Engineering",
            ),
            5,
        )
    finally:
        http_client.close()

    assert results == []
    assert client.provider_code == "recruitee"
    assert client.last_error == "official_site_request_failed"


def test_jibe_career_search_queries_reviewed_amd_site_and_ranks_exact_title_first() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "data": {
                            "slug": "91364",
                            "req_id": "91364",
                            "title": "Short Term 2027 Program Management Intern/Co-Op",
                            "hiring_organization": "Advanced Micro Devices, Inc",
                            "full_location": "MARKHAM, Canada",
                        }
                    },
                    {
                        "data": {
                            "slug": "91311",
                            "req_id": "91311",
                            "title": "Short Term 2027 Data Analyst Intern/Co-Op",
                            "hiring_organization": "Advanced Micro Devices, Inc",
                            "full_location": "MARKHAM, Canada",
                        }
                    },
                ]
            },
        )

    client = JibeCareerSiteSearchClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4459291699",
            source_kind="linkedin",
            company="AMD",
            title="Short Term 2027 Data Analyst Intern/Co-Op",
            location="Markham, Ontario, Canada",
        ),
        5,
    )

    assert [result.url for result in results] == [
        "https://careers.amd.com/careers-home/jobs/91311",
    ]
    assert requests[0].url.params["keywords"] == (
        "Short Term 2027 Data Analyst Intern/Co-Op"
    )
    assert requests[0].url.params["limit"] == "5"
    assert requests[0].headers["referer"] == (
        "https://careers.amd.com/careers-home/jobs"
    )


def test_jibe_career_search_skips_unregistered_company_without_network_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unregistered companies must not select a first-party host")

    client = JibeCareerSiteSearchClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/123",
            source_kind="linkedin",
            company="Example Systems",
            title="Platform Intern",
        ),
        5,
    )

    assert results == []
    assert client.last_error == "unsupported_company"


def test_recruitee_career_search_resolves_reviewed_huawei_offer_by_exact_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "title": "Co-op Engineer - Computer Network for AI",
                        "careers_url": (
                            "https://huaweicanada.recruitee.com/o/"
                            "co-op-engineer-computer-network-for-ai"
                        ),
                    },
                    {
                        "title": "Co-op Engineer - AI Software Engineering",
                        "company_name": "Huawei Technologies Canada Co., Ltd.",
                        "location": "Markham, Ontario, Canada",
                        "careers_url": (
                            "https://huaweicanada.recruitee.com/o/"
                            "co-op-engineer-ai-software-engineering"
                        ),
                    },
                ]
            },
        )

    client = RecruiteeCareerSiteSearchClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4430146034",
            source_kind="linkedin",
            company="Huawei Canada",
            title="Co-op Engineer - AI Software Engineering",
            location="Markham, Ontario, Canada",
        ),
        5,
    )

    assert [result.url for result in results] == [
        "https://huaweicanada.recruitee.com/o/co-op-engineer-ai-software-engineering"
    ]
    assert requests[0].url == "https://huaweicanada.recruitee.com/api/offers/"


def test_greenhouse_career_search_resolves_reviewed_riverside_job_by_exact_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 5409377008,
                        "title": "Junior Data Analyst Intern/Co-op",
                        "absolute_url": (
                            "https://job-boards.greenhouse.io/"
                            "riversidenaturalfoodsltd/jobs/5409377008"
                        ),
                        "location": {"name": "2233 Sheppard Avenue West"},
                    }
                ]
            },
        )

    client = GreenhouseCareerSiteSearchClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/4462032166",
            source_kind="linkedin",
            company="Riverside Natural Foods Ltd. (Home of MadeGood)",
            title="Junior Data Analyst Intern/Co-op",
            location="Toronto, Ontario, Canada",
        ),
        5,
    )

    assert [result.url for result in results] == [
        "https://job-boards.greenhouse.io/riversidenaturalfoodsltd/jobs/5409377008"
    ]
    assert requests[0].url == (
        "https://boards-api.greenhouse.io/v1/boards/"
        "riversidenaturalfoodsltd/jobs?content=true"
    )


def test_workday_career_search_resolves_reviewed_suncor_job_by_exact_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobPostings": [
                    {
                        "title": (
                            "Automation, Software or Computer Engineering Student - "
                            "January 2027"
                        ),
                        "externalPath": (
                            "/job/Fort-McMurray-Base-Plant-AB-CAN/"
                            "Automation-Software-Computer-Engineering-Student_R0017525"
                        ),
                        "locationsText": "5 Locations",
                    }
                ]
            },
        )

    client = WorkdayCareerSiteSearchClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/123",
            source_kind="linkedin",
            company="Suncor",
            title=(
                "Automation, Software or Computer Engineering Student - January 2027"
            ),
            location="Calgary, Alberta, Canada",
        ),
        5,
    )

    assert [result.url for result in results] == [
        "https://suncor.wd1.myworkdayjobs.com/Suncor_External/job/"
        "Fort-McMurray-Base-Plant-AB-CAN/"
        "Automation-Software-Computer-Engineering-Student_R0017525"
    ]
    assert requests[0].url == (
        "https://suncor.wd1.myworkdayjobs.com/wday/cxs/"
        "suncor/Suncor_External/jobs"
    )
    assert (
        b'"searchText":"Automation, Software or Computer Engineering Student - '
        b'January 2027"' in requests[0].content
    )


def test_reviewed_official_search_uses_company_domain_before_open_search() -> None:
    queries: list[tuple[str, int]] = []
    official_url = (
        "https://bdo.wd3.myworkdayjobs.com/BDO/job/Vancouver/"
        "Co-op-Intern-Forensic-Disputes-Investigation_JR6569"
    )

    class FakeSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, query: str, limit: int):
            queries.append((query, limit))
            return [
                SearchResult(
                    title="BDO role copied elsewhere",
                    url="https://www.jobs.ca/bdo/jobs/example",
                ),
                SearchResult(
                    title="Co-op/Intern – Forensic Disputes & Investigation",
                    url=official_url,
                ),
            ]

    client = ReviewedOfficialCareerSearchClient(search_client=FakeSearchClient())
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/123",
            source_kind="linkedin",
            company="BDO Canada",
            title="Co-op/Intern – Forensic Disputes & Investigation",
        ),
        5,
    )

    assert [result.url for result in results] == [official_url]
    assert queries == [
        (
            'site:bdo.wd3.myworkdayjobs.com '
            '"Co-op/Intern – Forensic Disputes & Investigation"',
            10,
        )
    ]
    assert client.provider_code == "serper_official_domain"


def test_reviewed_official_search_does_not_constrain_unregistered_company() -> None:
    class FakeSearchClient:
        provider_code = "serper"
        last_error = ""

        def search(self, _query: str, _limit: int):
            raise AssertionError("unregistered companies belong to the open-search fallback")

    client = ReviewedOfficialCareerSearchClient(search_client=FakeSearchClient())
    results = client.search(
        ExternalPostingHint(
            source_url="https://www.linkedin.com/jobs/view/123",
            source_kind="linkedin",
            company="Example Systems",
            title="Software Intern",
        ),
        5,
    )

    assert results == []
    assert client.last_error == "unsupported_company"
