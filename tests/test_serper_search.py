from __future__ import annotations

import httpx

from jobcockpit.integrations.serper import SerperSearchClient


def _response(status_code: int, payload: object, *, headers: dict[str, str] | None = None):
    request = httpx.Request("POST", "https://google.serper.dev/search")
    return httpx.Response(status_code, json=payload, headers=headers, request=request)


def test_serper_search_maps_live_organic_schema_and_ignores_malformed_rows() -> None:
    calls: list[dict] = []

    def post(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return _response(
            200,
            {
                "searchParameters": {"q": "example"},
                "organic": [
                    {
                        "title": "Example careers",
                        "link": "https://careers.example.test/jobs/1",
                        "snippet": "Platform Engineer",
                        "position": 1,
                    },
                    {"title": ["Bad title"], "link": "https://bad-title.example.test"},
                    {"title": "Bad link", "link": 123},
                    {"title": "Bad snippet", "link": "https://bad.example.test", "snippet": {}},
                    {"title": "Missing URL"},
                    "invalid",
                ],
                "credits": 1,
            },
        )

    client = SerperSearchClient(
        api_key="secret-key",
        country="ca",
        language="en",
        post=post,
    )

    results = client.search('"Example" "Platform Engineer" careers job', limit=5)

    assert [(item.title, item.url, item.snippet) for item in results] == [
        (
            "Example careers",
            "https://careers.example.test/jobs/1",
            "Platform Engineer",
        )
    ]
    assert client.last_error == ""
    assert calls == [
        {
            "url": "https://google.serper.dev/search",
            "headers": {"X-API-KEY": "secret-key", "Content-Type": "application/json"},
            "json": {
                "q": '"Example" "Platform Engineer" careers job',
                "num": 5,
                "gl": "ca",
                "hl": "en",
            },
            "timeout": 15.0,
        }
    ]


def test_serper_search_treats_missing_organic_results_as_empty_success() -> None:
    client = SerperSearchClient(
        api_key="secret-key",
        post=lambda *_args, **_kwargs: _response(200, {"credits": 1}),
    )

    assert client.search("no matches", limit=3) == []
    assert client.last_error == ""


def test_serper_search_requires_api_key_without_dispatching_request() -> None:
    def unexpected_post(*_args, **_kwargs):
        raise AssertionError("missing credentials must not dispatch a request")

    client = SerperSearchClient(api_key="", post=unexpected_post)

    assert client.search("engineer", limit=3) == []
    assert client.last_error == "serper_missing_api_key"


def test_serper_search_retries_timeout_then_succeeds() -> None:
    attempts = 0

    def post(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timed out")
        return _response(
            200,
            {
                "organic": [
                    {
                        "title": "Example careers",
                        "link": "https://careers.example.test/jobs/1",
                    }
                ]
            },
        )

    client = SerperSearchClient(
        api_key="secret-key",
        max_attempts=2,
        post=post,
        sleeper=lambda _seconds: None,
    )

    assert len(client.search("engineer", limit=3)) == 1
    assert attempts == 2
    assert client.last_error == ""


def test_serper_search_caps_timeout_attempts_at_two() -> None:
    attempts = 0

    def post(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out")

    client = SerperSearchClient(
        api_key="secret-key",
        max_attempts=99,
        post=post,
        sleeper=lambda _seconds: None,
    )

    assert client.search("engineer", limit=3) == []
    assert client.last_error == "serper_timeout"
    assert attempts == 2


def test_serper_search_retries_rate_limit_and_reports_stable_error() -> None:
    sleeps: list[float] = []
    client = SerperSearchClient(
        api_key="secret-key",
        max_attempts=2,
        post=lambda *_args, **_kwargs: _response(
            429,
            {"message": "quota for secret-key exhausted"},
            headers={"Retry-After": "2"},
        ),
        sleeper=sleeps.append,
    )

    assert client.search("engineer", limit=3) == []
    assert client.last_error == "serper_rate_limited"
    assert sleeps == [2.0]
    assert "secret-key" not in client.last_error


def test_serper_search_reports_unauthorized_without_exposing_response_body() -> None:
    client = SerperSearchClient(
        api_key="secret-key",
        post=lambda *_args, **_kwargs: _response(
            401,
            {"message": "invalid secret-key"},
        ),
    )

    assert client.search("engineer", limit=3) == []
    assert client.last_error == "serper_unauthorized"
    assert "secret-key" not in client.last_error


def test_serper_search_rejects_invalid_response_shape() -> None:
    client = SerperSearchClient(
        api_key="secret-key",
        post=lambda *_args, **_kwargs: _response(200, {"organic": {"link": "bad"}}),
    )

    assert client.search("engineer", limit=3) == []
    assert client.last_error == "serper_invalid_response"


def test_serper_search_bounds_requested_results() -> None:
    seen_num: list[int] = []

    def post(*_args, **kwargs):
        seen_num.append(kwargs["json"]["num"])
        return _response(200, {"organic": []})

    client = SerperSearchClient(api_key="secret-key", post=post)

    client.search("engineer", limit=100)

    assert seen_num == [20]
