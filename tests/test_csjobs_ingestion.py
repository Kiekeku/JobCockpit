from __future__ import annotations

import httpx
import pytest

from jobcockpit.db.models import JobIngestionRun
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.ingestion.ats_adapters.base import (
    AtsFetchRequest,
    AtsJob,
    AtsProviderError,
    AtsSourceExclusion,
)
from jobcockpit.ingestion.ats_adapters.csjobs import CsJobsAdapter
from jobcockpit.ingestion.ats_adapters.registry import (
    get_ats_adapter,
    registered_ats_providers,
)
from jobcockpit.ingestion.posting_discovery import PostingResolution, ResolvedExternalPosting
from jobcockpit.schemas.requests import AtsIngestRequest


CSJOBS_URL = "https://csjobs.ca/jobs"


@pytest.mark.parametrize('render_available', [True, False])
def test_csjobs_metadata_only_preview_requires_rendered_body(allow_public_dns, render_available):
    allow_public_dns({'csjobs.ca', 'jobs.example.com'})
    # Observed Bamboo response: social preview in head, empty JS application body.
    html = '''<html><head><title>ATS</title>
      <meta property="og:title" content="Software Engineering Intern">
      <meta property="og:description" content="Our mission is to improve patient care with precise technology. Changing the standard of care is p">
      </head><body><div id="app"></div><script>loadPosting()</script></body></html>'''
    def handler(request):
        if request.url.host == 'csjobs.ca':
            payload = _listing_payload(total=1)
            payload['jobs'] = [payload['jobs'][1]]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, text=html)
    rendered = RecordingRenderedResolver() if render_available else None
    adapter = CsJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rendered_page_resolver=rendered,
    )
    request = AtsFetchRequest(source_url=CSJOBS_URL, limit=1)
    jobs = adapter.fetch_jobs(request)
    if render_available:
        assert len(rendered.hints) == 1
        assert len(jobs) == 1
        assert 'Rendered full posting' in jobs[0].raw_text
        assert 'standard of care is p' not in jobs[0].raw_text
    else:
        assert jobs == []
        assert len(request.stats.link_failures) == 1


class RecordingApplyResolver:
    def __init__(self) -> None:
        self.calls = []

    def resolve(self, apply_url, context):
        self.calls.append((apply_url, context))
        return AtsJob(
            source_url=apply_url,
            raw_text=f"Resolved posting for {context.role}",
        )


class FailingApplyResolver:
    def resolve(self, apply_url, context):
        raise AtsProviderError(502, "Apply page request failed")


class RecordingRenderedResolver:
    def __init__(self) -> None:
        self.hints = []

    def resolve(self, hint):
        self.hints.append(hint)
        return PostingResolution(
            status="resolved",
            posting=ResolvedExternalPosting(
                source_url=hint.source_url,
                raw_text=f"Rendered full posting for {hint.title}",
            ),
        )


def _listing_payload(*, total: int = 4) -> dict:
    return {
        "jobs": [
            {
                "id": 101,
                "job_type": "internship",
                "title": "Software Engineering Intern",
                "company": "Example Labs",
                "location": "Toronto, Ontario, Canada",
                "date_posted": "2026-08-30",
                "link": "https://www.linkedin.com/jobs/view/101",
                "source": "LinkedIn",
            },
            {
                "id": 102,
                "job_type": "internship",
                "title": "Data Co-op",
                "company": "Example Analytics",
                "location": "Waterloo, Ontario, Canada",
                "date_posted": "2026-08-29",
                "link": "https://jobs.example.com/data-coop?utm_source=csjobs",
                "source": "Company",
            },
        ],
        "meta": {
            "is_authenticated": False,
            "is_premium": False,
            "page": 1,
            "per_page": 90,
            "total": total,
            "total_pages": 1,
        },
    }


def test_csjobs_fetches_public_internships_and_resolves_each_apply_link():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_listing_payload())

    apply_resolver = RecordingApplyResolver()
    adapter = CsJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        apply_resolver=apply_resolver,
    )
    fetch_request = AtsFetchRequest(source_url=CSJOBS_URL, limit=75)

    jobs = adapter.fetch_jobs(fetch_request)

    assert len(jobs) == 2
    assert jobs[0].title == "Software Engineering Intern"
    assert jobs[0].portal_source_url == "https://www.linkedin.com/jobs/view/101"
    assert jobs[1].portal_source_url == "https://jobs.example.com/data-coop?utm_source=csjobs"
    assert [call[0] for call in apply_resolver.calls] == [
        "https://www.linkedin.com/jobs/view/101",
        "https://jobs.example.com/data-coop?utm_source=csjobs",
    ]
    assert apply_resolver.calls[0][1].source_label == "CS Jobs"
    assert apply_resolver.calls[0][1].source_url == CSJOBS_URL
    assert apply_resolver.calls[0][1].company == "Example Labs"
    assert apply_resolver.calls[0][1].role == "Software Engineering Intern"
    assert requests[0].url == httpx.URL(
        "https://csjobs.ca/api/v1/jobs?page=1&per_page=90&job_type=internship"
    )
    assert fetch_request.stats.links_seen_count == 2
    assert fetch_request.stats.source_exclusions == [
        AtsSourceExclusion(
            category="premium_locked",
            count=2,
            reason="CS Jobs reports additional internship postings behind Premium without public links",
        )
    ]


def test_csjobs_passes_search_to_public_internship_feed():
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, json={**_listing_payload(total=0), "jobs": []})

    adapter = CsJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        apply_resolver=RecordingApplyResolver(),
    )

    assert adapter.fetch_jobs(
        AtsFetchRequest(source_url=CSJOBS_URL, limit=75, search_text="machine learning")
    ) == []
    assert requested_urls == [
        "https://csjobs.ca/api/v1/jobs?page=1&per_page=90&job_type=internship&search=machine+learning"
    ]


def test_csjobs_reconciles_public_links_already_present_in_jobcockpit():
    apply_resolver = RecordingApplyResolver()
    adapter = CsJobsAdapter(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_listing_payload(total=2))
            )
        ),
        apply_resolver=apply_resolver,
    )

    fetch_request = AtsFetchRequest(
        source_url=CSJOBS_URL,
        limit=75,
        excluded_source_urls={"https://www.linkedin.com/jobs/view/101"},
    )
    jobs = adapter.fetch_jobs(fetch_request)

    assert [job.source_url for job in jobs] == [
        "https://jobs.example.com/data-coop?utm_source=csjobs"
    ]
    assert fetch_request.stats.source_exclusions == [
        AtsSourceExclusion(
            category="already_imported",
            count=1,
            reason="CS Jobs public internship links already present in JobCockpit",
        )
    ]


def test_csjobs_uses_existing_rendered_resolver_when_direct_apply_fetch_is_gated():
    rendered_resolver = RecordingRenderedResolver()
    adapter = CsJobsAdapter(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_listing_payload(total=2))
            )
        ),
        apply_resolver=FailingApplyResolver(),
        rendered_page_resolver=rendered_resolver,
    )

    jobs = adapter.fetch_jobs(AtsFetchRequest(source_url=CSJOBS_URL, limit=2))

    assert len(jobs) == 2
    assert jobs[0].raw_text.startswith("Rendered full posting for Software Engineering Intern")
    assert "CS Jobs URL: https://csjobs.ca/jobs" in jobs[0].raw_text
    assert rendered_resolver.hints[0].source_url == "https://www.linkedin.com/jobs/view/101"
    assert rendered_resolver.hints[0].title == "Software Engineering Intern"


def test_csjobs_rejects_non_board_urls():
    adapter = CsJobsAdapter(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )

    with pytest.raises(AtsProviderError, match="Only csjobs.ca/jobs is supported"):
        adapter.validate_source_url("https://csjobs.ca/tracker")
    with pytest.raises(AtsProviderError, match="Only csjobs.ca/jobs is supported"):
        adapter.validate_source_url("https://example.com/jobs")


def test_csjobs_is_registered_as_ats_adapter():
    assert "csjobs" in registered_ats_providers()
    assert isinstance(get_ats_adapter("CSJOBS"), CsJobsAdapter)


def test_csjobs_can_request_full_public_preview_without_raising_other_provider_limits():
    assert AtsIngestRequest(provider="csjobs", source_url=CSJOBS_URL, limit=75).limit == 75
    with pytest.raises(ValueError, match="limit must be at most 50"):
        AtsIngestRequest(
            provider="hanzilla",
            source_url="https://jobs.hanzilla.co/internships/",
            limit=75,
        )


def test_generic_ats_ingest_persists_csjobs_source_exclusions(
    jobcockpit_settings_factory,
    jobcockpit_client_factory,
    allow_public_dns,
    monkeypatch,
):
    settings = jobcockpit_settings_factory(ats_mock_port=8001)
    client = jobcockpit_client_factory(settings)
    init_database(settings)

    class FakeCsJobsAdapter:
        provider = "csjobs"

        def validate_source_url(self, source_url: str) -> None:
            assert source_url == CSJOBS_URL

        def fetch_jobs(self, request: AtsFetchRequest):
            request.stats.source_exclusions.append(
                AtsSourceExclusion(
                    category="premium_locked",
                    count=99,
                    reason=(
                        "CS Jobs reports additional internship postings behind Premium "
                        "without public links"
                    ),
                )
            )
            return []

    allow_public_dns({"csjobs.ca"})
    monkeypatch.setattr(
        "jobcockpit.services.workflows.get_ats_adapter",
        lambda provider: FakeCsJobsAdapter(),
    )

    response = client.post(
        "/api/jobs/ingest/ats",
        json={
            "provider": "csjobs",
            "source_url": CSJOBS_URL,
            "limit": 75,
            "skip_interest_filter": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["source_exclusions"] == [
        {
            "category": "premium_locked",
            "count": 99,
            "reason": (
                "CS Jobs reports additional internship postings behind Premium without public links"
            ),
        }
    ]
    with session_scope(settings) as db:
        run = db.query(JobIngestionRun).one()
        assert run.request_payload["source_exclusions"] == response.json()["source_exclusions"]
        assert run.finished_at is not None
