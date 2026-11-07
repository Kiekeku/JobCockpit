import hashlib

from sqlalchemy import select

from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobAlertLink, JobAlertSource
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.integrations.gmail_adapters import IndeedGmailAdapter
from jobcockpit.integrations.gmail_processing import process_gmail_source
from jobcockpit.storage.artifacts import ArtifactStorageService


def test_indeed_gmail_adapter_normalizes_job_keys_and_builds_structured_cards():
    first_url = "https://ca.indeed.com/rc/clk/dl?jk=a17e92235d9d4914&from=ja&tk=private-token"
    duplicate_url = "https://www.indeed.com/viewjob?jk=a17e92235d9d4914&utm_source=jobseeker_emails"
    second_url = "https://ca.indeed.com/rc/clk/dl?jk=5e6d97322f1ff704&from=ja"
    message = {
        "subject": "Three new co-op opportunities",
        "plain_text": (
            "Indeed Job Alert\n\n"
            "New Grad, Information Technology, June 2027 (Calgary)\n"
            "Cenovus Energy - Calgary, AB\n"
            "Build technology solutions for the business.\n"
            "Just posted\n"
            f"{first_url}\n\n"
            "Product Strategy Specialist - Seed & Crop Protection\n"
            "UFA Co-operative Ltd. - Calgary, AB\n"
            "Support product strategy and market analysis.\n"
            "1 day ago\n"
            f"{second_url}\n"
        ),
        "html": (
            "<html><body>"
            f'<a href="{first_url}">New Grad, Information Technology, June 2027 (Calgary)</a>'
            f'<a href="{duplicate_url}">Duplicate card</a>'
            f'<a href="{second_url}">Product Strategy Specialist - Seed &amp; Crop Protection</a>'
            '<a href="https://ca.indeed.com/jobs?q=co-op&amp;from=ja">View all jobs</a>'
            '<a href="https://subscriptions.indeed.com/update/example">Manage alert</a>'
            "</body></html>"
        ),
    }

    adapter = IndeedGmailAdapter()
    links = adapter.extract_links(message)

    assert [link.href for link in links] == [
        "https://ca.indeed.com/viewjob?jk=a17e92235d9d4914",
        "https://ca.indeed.com/viewjob?jk=5e6d97322f1ff704",
    ]
    assert adapter.raw_text_for_link(message, links[0]) == (
        "New Grad, Information Technology, June 2027 (Calgary)\n"
        "Company: Cenovus Energy\n"
        "Location: Calgary, AB\n"
        "Email Card: New Grad, Information Technology, June 2027 (Calgary) | "
        "Cenovus Energy - Calgary, AB"
    )
    assert adapter.raw_text_for_link(message, links[1]) == (
        "Product Strategy Specialist - Seed & Crop Protection\n"
        "Company: UFA Co-operative Ltd.\n"
        "Location: Calgary, AB\n"
        "Email Card: Product Strategy Specialist - Seed & Crop Protection | "
        "UFA Co-operative Ltd. - Calgary, AB"
    )


def test_indeed_gmail_adapter_preserves_sponsored_cards_without_persisting_click_tokens():
    message = {
        "plain_text": "",
        "html": (
            "<html><body>"
            '<a href="https://ca.indeed.com/pagead/clk/dl?ad=personalized-token">Sponsored job</a>'
            '<a href="https://ca.indeed.com/jobs?q=software&amp;vjk=abc123">Search results</a>'
            '<a href="https://subscriptions.indeed.com/update/example">Manage alert</a>'
            "</body></html>"
        ),
    }

    links = IndeedGmailAdapter().extract_links(message)

    identity = hashlib.sha256(b"personalized-token").hexdigest()
    assert [link.href for link in links] == [f"https://ca.indeed.com/pagead/clk?jobcard={identity}"]
    assert "personalized-token" not in links[0].href


def test_indeed_gmail_adapter_processes_through_shared_resolver(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    storage = ArtifactStorageService(settings.artifact_root)
    ad_token = "sponsored-campaign-token"
    alert_url = f"https://ca.indeed.com/pagead/clk/dl?ad={ad_token}&tk=private-token"
    canonical_url = f"https://ca.indeed.com/pagead/clk?jobcard={hashlib.sha256(ad_token.encode()).hexdigest()}"
    official_url = "http://localhost:8001/ats/generic/cenovus/jobs/new-grad-it"
    message_id = "indeed-adapter-message"
    message = {
        "id": message_id,
        "id_hash": hashlib.sha256(message_id.encode()).hexdigest(),
        "thread_hash": hashlib.sha256(b"indeed-adapter-thread").hexdigest(),
        "subject": "New Grad, Information Technology at Cenovus Energy",
        "sender_hash": hashlib.sha256(b"donotreply@jobalert.indeed.com").hexdigest(),
        "sender_domain": "jobalert.indeed.com",
        "received_at": "2026-07-15T18:00:45Z",
        "body_hash": hashlib.sha256(b"sanitized-indeed-alert").hexdigest(),
        "plain_text": (
            "Indeed Job Alert\n"
            "New Grad, Information Technology\n"
            "Cenovus Energy - Calgary, AB\n"
            "Build technology solutions.\n"
            "Just posted\n"
            f"{alert_url}\n"
        ),
        "html": f'<a href="{alert_url}">New Grad, Information Technology</a>',
    }
    resolved_hints = []

    class FakePostingResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": official_url,
                    "raw_text": "New Grad, Information Technology\nCompany: Cenovus Energy\nLocation: Calgary, AB",
                    "confidence": 0.91,
                    "evidence": "Official careers page matched title and company",
                },
            }

    with session_scope(settings) as db:
        source = JobAlertSource(
            job_alert_source_id="alertsrc_indeed_processing",
            candidate_id="candidate_local",
            provider="gmail",
            display_name="Indeed Job Alerts",
            gmail_query="from:donotreply@jobalert.indeed.com {in:inbox in:spam}",
            sender_allowlist=["jobalert.indeed.com"],
            enabled=True,
        )
        db.add(source)
        db.flush()
        result = process_gmail_source(
            db=db,
            storage=storage,
            settings=settings,
            source=source,
            require_preview=False,
            messages=[message],
            posting_resolver=FakePostingResolver(),
        )

    assert result["jobs_created_count"] == 1
    assert result["mailbox_mutations"] == []
    assert len(resolved_hints) == 1
    assert resolved_hints[0].source_url == canonical_url
    assert resolved_hints[0].title == "New Grad, Information Technology"
    assert resolved_hints[0].company == "Cenovus Energy"
    assert resolved_hints[0].location == "Calgary, AB"
    with session_scope(settings) as db:
        link = db.execute(select(JobAlertLink)).scalar_one()
        assert link.original_url == canonical_url
        assert link.resolved_url == official_url
        assert link.ingestion_status == "imported"
