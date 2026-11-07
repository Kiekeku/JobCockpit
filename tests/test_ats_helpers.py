from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from jobcockpit.ingestion.ats_helpers import (
    archived_headers,
    clean_text,
    html_to_text,
    line,
    source_archive_from_response,
    select_text,
)


def test_source_archive_from_response_keeps_safe_metadata_only():
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://jobs.example.test/detail"),
        content=b"payload",
        headers={
            "content-type": "application/json; charset=utf-8",
            "etag": "abc",
            "set-cookie": "private=value",
            "last-modified": "today",
        },
    )

    archive = source_archive_from_response("https://jobs.example.test/source", response)

    assert archive.source_url == "https://jobs.example.test/source"
    assert archive.final_url == "https://jobs.example.test/detail"
    assert archive.content_type == "application/json; charset=utf-8"
    assert archive.body == b"payload"
    assert archive.headers == {"etag": "abc", "last-modified": "today"}


def test_ats_text_helpers_normalize_common_adapter_text():
    soup = BeautifulSoup("<main><h1> Role </h1><p>Build   systems</p></main>", "html.parser")

    assert archived_headers(httpx.Headers({"content-language": "en", "authorization": "secret"})) == {
        "content-language": "en"
    }
    assert clean_text("A\n  B\tC") == "A B C"
    assert html_to_text("<p>A</p><p>B</p>") == "A\nB"
    assert select_text(soup, "main") == "Role Build systems"
    assert line("Role", " Engineer ") == "Role: Engineer"
    assert line("Role", "") == ""
