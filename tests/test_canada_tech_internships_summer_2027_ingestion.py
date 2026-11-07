from __future__ import annotations

import httpx

from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.canada_tech_internships_summer_2027 import (
    CanadaTechInternshipsSummer2027Adapter,
)
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers

GITHUB_URL = "https://github.com/michelleokolie/canada-tech-internships-summer-2027"
RAW_URL = "https://raw.githubusercontent.com/michelleokolie/canada-tech-internships-summer-2027/main/README.md"
GOOGLE_URL = "https://careers.google.com/jobs/one"
SHOPIFY_URL = "https://www.shopify.com/careers/two"

README = """
| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Google | Software Developer Intern | Waterloo, ON</br>Toronto, ON | <a href="https://careers.google.com/jobs/one?utm_source=michellesinternshiplist"><img src="https://i.imgur.com/apply.png" alt="Apply"></a> | Jul 20 |
| Shopify | Software Engineer Intern | Remote, Canada | <a href="https://www.shopify.com/careers/two"><img src="https://i.imgur.com/apply.png" alt="Apply"></a> | Jul 21 |
| Closed Co | Software Intern | Toronto, ON | Closed | Jul 19 |

## Contributing
"""


def test_canada_tech_2027_parses_html_apply_links_and_skips_existing_urls(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == RAW_URL
        return httpx.Response(200, text=README)

    resolved_contexts = []

    class FakeResolver:
        def __init__(self, **kwargs):
            pass

        def resolve(self, apply_url, context):
            resolved_contexts.append(context)
            return AtsJob(source_url=apply_url, raw_text=f"{context.role}\nCompany: {context.company}")

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeResolver,
    )
    adapter = CanadaTechInternshipsSummer2027Adapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = AtsFetchRequest(
        source_url=GITHUB_URL,
        search_text="software",
        limit=30,
        excluded_source_urls=frozenset({GOOGLE_URL}),
    )

    jobs = adapter.fetch_jobs(request)

    assert [job.source_url for job in jobs] == [SHOPIFY_URL]
    assert request.stats.links_seen_count == 1
    assert resolved_contexts[0].company == "Shopify"
    assert resolved_contexts[0].location == "Remote, Canada"
    assert resolved_contexts[0].source_url == GITHUB_URL


def test_canada_tech_2027_adapter_is_registered():
    assert "canada_tech_internships_summer_2027" in registered_ats_providers()
    assert isinstance(
        get_ats_adapter("CANADA_TECH_INTERNSHIPS_SUMMER_2027"),
        CanadaTechInternshipsSummer2027Adapter,
    )


def test_canada_tech_2027_rejects_other_github_repositories():
    adapter = CanadaTechInternshipsSummer2027Adapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    try:
        adapter.validate_source_url("https://github.com/negarprh/Canadian-Tech-Internships-2026")
    except AtsProviderError as error:
        assert error.status_code == 400
        assert (
            error.detail
            == "Only the michelleokolie/canada-tech-internships-summer-2027 GitHub README is supported"
        )
    else:
        raise AssertionError("Expected unsupported repository to raise AtsProviderError")
