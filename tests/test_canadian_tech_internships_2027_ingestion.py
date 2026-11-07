from __future__ import annotations

import httpx
import pytest

from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderError
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import (
    CanadianTechInternshipsAdapter,
)
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships_2027 import (
    CanadianTechInternships2027Adapter,
)
from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter, registered_ats_providers

GITHUB_URL = (
    "https://github.com/negarprh/Canadian-Tech-Internships-2027?tab=readme-ov-file"
)
CANONICAL_GITHUB_URL = "https://github.com/negarprh/Canadian-Tech-Internships-2027"
RAW_URL = (
    "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2027/main/README.md"
)
AUTODESK_URL = "https://autodesk.example.test/jobs/cloud-developer"
FCAP_URL = "https://autodesk.example.test/jobs/fcap-developer"

README_WITH_LEADING_CONTENT = f"""
# Canadian Tech Internships - 2027

> A curated list of 2027 tech internships in Canada.

### Looking for something else?

- See the 2026 list.
- Read CONTRIBUTING.md before opening an issue.

| Not | The | Internship | Table | Header |
| --- | --- | ---------- | ----- | ------ |
| This | content | must be | ignored | first |

## 2027 Internship Listings

| Company | Role | Location | Apply | Date Posted |
| ------- | ---- | -------- | ----- | ----------- |
| Autodesk | Cloud Developer Intern | Montreal, QC | [Apply]({AUTODESK_URL}) | Aug 7, 2026 |
| ↳ | Fcap Developer Intern | Montreal, QC | [Apply]({FCAP_URL}) | Aug 7, 2026 |
| Closed Co | Software Developer Intern | Toronto, ON | Closed🔒 | Aug 2, 2026 |
"""


def test_canadian_tech_2027_skips_leading_content_and_reads_listing_table(monkeypatch):
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        assert str(request.url) == RAW_URL
        return httpx.Response(200, text=README_WITH_LEADING_CONTENT)

    resolved_contexts = []

    class FakeResolver:
        def __init__(self, **kwargs):
            pass

        def resolve(self, apply_url, context):
            resolved_contexts.append(context)
            return AtsJob(source_url=apply_url, raw_text=context.role)

    monkeypatch.setattr(
        "jobcockpit.ingestion.ats_adapters.canadian_tech_internships.AtsApplyPageResolver",
        FakeResolver,
    )
    adapter = CanadianTechInternships2027Adapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    jobs = adapter.fetch_jobs(
        AtsFetchRequest(source_url=GITHUB_URL, search_text="developer", limit=30)
    )

    assert requests_seen == [RAW_URL]
    assert [job.source_url for job in jobs] == [AUTODESK_URL, FCAP_URL]
    assert [context.company for context in resolved_contexts] == ["Autodesk", "Autodesk"]
    assert all(context.source_url == CANONICAL_GITHUB_URL for context in resolved_contexts)


def test_canadian_tech_2027_adapter_is_registered():
    assert "canadian_tech_internships_2027" in registered_ats_providers()
    assert isinstance(
        get_ats_adapter("CANADIAN_TECH_INTERNSHIPS_2027"),
        CanadianTechInternships2027Adapter,
    )


def test_canadian_tech_2027_rejects_the_old_repository_location():
    adapter = CanadianTechInternships2027Adapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    with pytest.raises(AtsProviderError) as error:
        adapter.validate_source_url(
            "https://github.com/negarprh/Canadian-Tech-Internships-2026/blob/main/README-2027.md"
        )

    assert error.value.status_code == 400


def test_canadian_tech_2026_rejects_the_2027_readme_path():
    adapter = CanadianTechInternshipsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    with pytest.raises(AtsProviderError) as error:
        adapter.validate_source_url(GITHUB_URL)

    assert error.value.status_code == 400
