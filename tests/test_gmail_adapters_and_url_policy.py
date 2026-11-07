import socket


from jobcockpit.services.workflows import url_policy_result
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    JobAlertSource,
)
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.url_policy import UrlFetchPolicy
from jobcockpit.integrations.gmail import EmailLinkExtractor, classify_email_link
from jobcockpit.schemas.requests import JobIngestRequest


def test_url_policy_blocks_private_network_and_allows_mock_localhost(monkeypatch):
    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "careers.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    policy = UrlFetchPolicy(allowed_localhosts={"localhost:8001", "127.0.0.1:8001"})

    assert policy.check("https://careers.example.com/jobs/123").allowed is True
    assert policy.check("http://169.254.169.254/latest/meta-data").allowed is False
    assert policy.check("http://192.168.1.20/jobs").allowed is False
    assert policy.check("file:///C:/Users/example/resume.pdf").allowed is False
    assert policy.check("http://localhost:8001/acme/External").allowed is True
    assert policy.check("http://localhost:3000/acme/External").allowed is False


def test_url_policy_uses_configured_mock_port():
    request = JobIngestRequest(
        source_type="url",
        source_url="http://localhost:8101/ats/generic/acme/jobs/123",
        raw_text="Configurable Port Role\nPython SQL",
    )

    assert url_policy_result(request, Settings(ats_mock_port=8101)).allowed is True
    assert url_policy_result(request, Settings(ats_mock_port=8001)).allowed is False


def test_url_policy_blocks_hostnames_that_resolve_to_private_addresses(monkeypatch):
    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "metadata.invalid"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    result = UrlFetchPolicy().check("https://metadata.invalid/latest/meta-data")

    assert result.allowed is False
    assert result.reason == "private_network_blocked"


def test_email_link_extractor_ignores_footer_links_and_keeps_job_links():
    html = """
    <a href="https://email.example/click?u=https%3A%2F%2Fjobs.example.com%2Frole%2F123">
      Software Developer
    </a>
    <a href="https://www.linkedin.com/jobs/view/424242">Platform Engineer at Acme</a>
    <a href="https://www.linkedin.com/comm/jobs/view/515151">Backend Engineer at Globex</a>
    <a href="https://ca.indeed.com/viewjob?jk=3cab9868c348dd54">Data Scientist at Roche</a>
    <a href="https://example.com/unsubscribe">unsubscribe</a>
    <a href="https://mapleradar.com/preferences/manage?email=user%40example.invalid">Manage preferences</a>
    <a href="mailto:support@mapleradar.com">support@mapleradar.com</a>
    <a href="https://instagram.com/example">Instagram</a>
    """

    links = EmailLinkExtractor().extract(html)
    classifications = {
        link.link_text.strip(): classify_email_link(link).classification for link in links
    }

    assert classifications["Software Developer"] == "probable_job_posting"
    assert classifications["Platform Engineer at Acme"] == "requires_external_resolution"
    assert classifications["Backend Engineer at Globex"] == "requires_external_resolution"
    assert classifications["Data Scientist at Roche"] == "requires_external_resolution"
    assert classifications["unsubscribe"] == "unsubscribe_or_preferences"
    assert classifications["Manage preferences"] == "unsubscribe_or_preferences"
    assert classifications["support@mapleradar.com"] == "contact_or_support"
    assert classifications["Instagram"] == "social_or_marketing"


def test_email_link_extractor_keeps_raw_href_until_adapter_normalizes():
    html = """
    <a href="https://xgvsd7lt.r.us-east-1.awstrack.me/L0/https:%2F%2Fca.indeed.com%2Fviewjob%3Fjk=3cab9868c348dd54/1/0100019e940080d4/token">
      2026 Intern - Data Scientist / Machine Learning Engineer
    </a>
    """

    links = EmailLinkExtractor().extract(html)

    assert len(links) == 1
    assert links[0].href == (
        "https://xgvsd7lt.r.us-east-1.awstrack.me/L0/"
        "https:%2F%2Fca.indeed.com%2Fviewjob%3Fjk=3cab9868c348dd54/1/0100019e940080d4/token"
    )


def test_extract_message_links_deduplicates_tracking_and_plain_text_versions():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    canonical_url = "https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"
    tracking_url = (
        "https://xgvsd7lt.r.us-east-1.awstrack.me/L0/"
        "https:%2F%2Fca.indeed.com%2Fviewjob%3Fjk=3cab9868c348dd54/1/0100019e940080d4/token"
    )
    message = {
        "html": f'<p><a href="{tracking_url}">2026 Intern - Data Scientist / Machine Learning Engineer</a></p>',
        "plain_text": f"2026 Intern - Data Scientist / Machine Learning Engineer\n{canonical_url}",
    }

    links = MapleRadarGmailAdapter().extract_links(message)

    assert len(links) == 1
    assert links[0].href == canonical_url
    assert links[0].link_text == "2026 Intern - Data Scientist / Machine Learning Engineer"


def test_mapleradar_gmail_adapter_unwraps_tracking_and_builds_digest_row_raw_text():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    tracking_url = (
        "https://xgvsd7lt.r.us-east-1.awstrack.me/L0/"
        "https:%2F%2Fca.indeed.com%2Fviewjob%3Fjk=3cab9868c348dd54/1/0100019e940080d4/token"
    )
    message = {
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "2026 Intern \u2013 Data Scientist / Machine Learning Engineer \u00b7 Roche \u00b7 Mississauga, ON, CA"
        ),
        "html": (
            f'<html><body><p><a href="{tracking_url}">'
            "2026 Intern \u2013 Data Scientist / Machine Learning Engineer</a>"
            " \u00b7 Roche \u00b7 Mississauga, ON, CA</p></body></html>"
        ),
    }

    adapter = MapleRadarGmailAdapter()
    links = adapter.extract_links(message)

    assert [link.href for link in links] == ["https://ca.indeed.com/viewjob?jk=3cab9868c348dd54"]
    assert adapter.raw_text_for_link(message, links[0]) == (
        "2026 Intern \u2013 Data Scientist / Machine Learning Engineer\n"
        "Company: Roche\n"
        "Location: Mississauga, Ontario, Canada\n"
        "Email Row: 2026 Intern \u2013 Data Scientist / Machine Learning Engineer "
        "\u00b7 Roche \u00b7 Mississauga, ON, CA"
    )


def test_mapleradar_gmail_adapter_preserves_location_when_company_segment_is_blank():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    job_url = "https://ca.indeed.com/viewjob?jk=a331d0e4fde08862"
    message = {
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "Software Engineer \u2014 AI LegalTech Startup (Also hiring: Intern) \ufffd  \ufffd Montr\u00e9al, QC, CA"
        ),
        "html": (
            f'<html><body><p><a href="{job_url}">'
            "Software Engineer \ufffd AI LegalTech Startup (Also hiring: Intern)</a>"
            " \u2014  \u2014 Montr\u00e9al, QC, CA</p></body></html>"
        ),
    }

    adapter = MapleRadarGmailAdapter()
    links = adapter.extract_links(message)

    raw_text = adapter.raw_text_for_link(message, links[0])

    assert "Location: Montr\u00e9al, Quebec, Canada" in raw_text
    assert "Company: Unknown" in raw_text
    assert parse_job_text(raw_text, job_url).location.city == "Montr\u00e9al"


def test_mapleradar_gmail_adapter_uses_later_unambiguous_digest_row_when_first_match_is_weak():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    job_url = "https://www.linkedin.com/jobs/view/4425618748"
    message = {
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "Junior Software Engineer\n"
            "Junior Software Engineer \u00b7 Finning Canada \u00b7 Edmonton, AB, CA"
        ),
        "html": (
            f'<html><body><p><a href="{job_url}">Junior Software Engineer</a></p></body></html>'
        ),
    }

    adapter = MapleRadarGmailAdapter()
    links = adapter.extract_links(message)

    raw_text = adapter.raw_text_for_link(message, links[0])

    assert "Company: Finning Canada" in raw_text
    assert "Location: Edmonton, Alberta, Canada" in raw_text
    assert parse_job_text(raw_text, job_url).employer.company_name == "Finning Canada"


def test_mapleradar_gmail_adapter_preserves_company_when_location_is_missing():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    job_url = "https://www.linkedin.com/jobs/view/4430485849"
    message = {
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "Security Engineer Intern, Application Security - Fall 2026 \u00b7 1Password \u00b7"
        ),
        "html": (
            f'<html><body><p><a href="{job_url}">'
            "Security Engineer Intern, Application Security - Fall 2026</a>"
            " \u00b7 1Password \u00b7</p></body></html>"
        ),
    }

    adapter = MapleRadarGmailAdapter()
    links = adapter.extract_links(message)

    raw_text = adapter.raw_text_for_link(message, links[0])

    assert raw_text == (
        "Security Engineer Intern, Application Security - Fall 2026\n"
        "Company: 1Password\n"
        "Email Row: Security Engineer Intern, Application Security - Fall 2026 \u00b7 1Password"
    )


def test_mapleradar_gmail_adapter_does_not_guess_between_duplicate_plaintext_rows():
    from jobcockpit.integrations.gmail_adapters import MapleRadarGmailAdapter

    job_url = "https://www.linkedin.com/jobs/view/4425618748"
    message = {
        "plain_text": (
            "Your daily digest of new Canadian internship opportunities.\n"
            "Junior Software Engineer \u00b7 Finning Canada \u00b7 Edmonton, AB, CA\n"
            "Junior Software Engineer \u00b7 Topicflow \u00b7 Vancouver, BC, CA"
        ),
        "html": (
            f'<html><body><p><a href="{job_url}">Junior Software Engineer</a></p></body></html>'
        ),
    }

    adapter = MapleRadarGmailAdapter()
    links = adapter.extract_links(message)

    assert adapter.raw_text_for_link(message, links[0]) == "Junior Software Engineer"


def test_job_parser_normalizes_canadian_location_codes():
    parsed = parse_job_text(
        "2026 Intern \u2013 Data Scientist / Machine Learning Engineer\n"
        "Company: Roche\n"
        "Location: Mississauga, ON, CA"
    )
    ambiguous = parse_job_text("Backend Engineer\nCompany: Acme\nLocation: San Francisco, CA")

    assert parsed.location.city == "Mississauga"
    assert parsed.location.province_state == "Ontario"
    assert parsed.location.country == "Canada"
    assert ambiguous.location.city == "San Francisco"
    assert ambiguous.location.province_state == "CA"
    assert ambiguous.location.country is None


def test_gmail_adapter_selection_uses_mapleradar_only_for_mapleradar_sources():
    from jobcockpit.integrations.gmail_adapters import (
        GenericGmailAdapter,
        IndeedGmailAdapter,
        LinkedInGmailAdapter,
        MapleRadarGmailAdapter,
        gmail_adapter_for_source,
    )

    mapleradar_source = JobAlertSource(
        job_alert_source_id="alertsrc_mapleradar_adapter",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="MapleRadar jobs",
        gmail_query="from:jobs@mapleradar.com newer_than:30d",
        sender_allowlist=["mapleradar.com"],
        enabled=True,
    )
    generic_source = JobAlertSource(
        job_alert_source_id="alertsrc_generic_adapter",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Generic alerts",
        gmail_query="from:alerts.invalid newer_than:30d",
        sender_allowlist=["alerts.invalid"],
        enabled=True,
    )
    linkedin_source = JobAlertSource(
        job_alert_source_id="alertsrc_linkedin_adapter",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="LinkedIn Job Alerts and Recommendations",
        gmail_query="from:jobalerts-noreply@linkedin.com {in:inbox in:spam}",
        sender_allowlist=["linkedin.com"],
        enabled=True,
    )
    indeed_source = JobAlertSource(
        job_alert_source_id="alertsrc_indeed_adapter",
        candidate_id="candidate_local",
        provider="gmail",
        display_name="Indeed Job Alerts",
        gmail_query="from:donotreply@jobalert.indeed.com {in:inbox in:spam}",
        sender_allowlist=["jobalert.indeed.com"],
        enabled=True,
    )

    assert isinstance(gmail_adapter_for_source(mapleradar_source), MapleRadarGmailAdapter)
    assert isinstance(gmail_adapter_for_source(linkedin_source), LinkedInGmailAdapter)
    assert isinstance(gmail_adapter_for_source(indeed_source), IndeedGmailAdapter)
    assert isinstance(gmail_adapter_for_source(generic_source), GenericGmailAdapter)


def test_linkedin_gmail_adapter_keeps_only_unique_job_cards_and_normalizes_urls():
    from jobcockpit.integrations.gmail_adapters import LinkedInGmailAdapter

    first_tracking_url = (
        "https://www.linkedin.com/comm/jobs/view/4440837421/"
        "?trackingId=tracking-one&trk=company-logo"
    )
    first_card_url = (
        "https://www.linkedin.com/comm/jobs/view/4440837421/?trackingId=tracking-two&trk=job-card"
    )
    second_card_url = "https://ca.linkedin.com/jobs/view/4440823529?refId=example"
    message = {
        "subject": "Technical Records Administrator at Example Aviation",
        "plain_text": "",
        "html": (
            "<html><body>"
            f'<a href="{first_tracking_url}">Example Aviation</a>'
            f'<a href="{first_card_url}">Technical<br>Records Administrator (Co-op/Student)'
            "<br>Example Aviation \u00b7 Calgary, AB<br>Actively recruiting</a>"
            f'<a href="{second_card_url}">Community Relations Student'
            "<br>Example Energy \u00b7 Calgary, AB</a>"
            '<a href="https://www.linkedin.com/comm/jobs/search?keywords=Intern">See all jobs</a>'
            '<a href="https://www.linkedin.com/comm/jobs/alerts">Manage alerts</a>'
            '<a href="https://www.linkedin.com/help/linkedin/answer/67">Help</a>'
            "</body></html>"
        ),
    }

    adapter = LinkedInGmailAdapter()
    links = adapter.extract_links(message)

    assert [link.href for link in links] == [
        "https://www.linkedin.com/jobs/view/4440837421",
        "https://www.linkedin.com/jobs/view/4440823529",
    ]
    assert links[0].link_text == "Technical Records Administrator (Co-op/Student)"
    assert adapter.raw_text_for_link(message, links[0]) == (
        "Technical Records Administrator (Co-op/Student)\n"
        "Company: Example Aviation\n"
        "Location: Calgary, AB\n"
        "Email Card: Technical | Records Administrator (Co-op/Student) | "
        "Example Aviation \u00b7 Calgary, AB | Actively recruiting"
    )


def test_linkedin_gmail_adapter_parses_similar_jobs_recommendation_cards():
    from jobcockpit.integrations.gmail_adapters import LinkedInGmailAdapter

    message = {
        "subject": "Student Software Engineer and similar jobs",
        "plain_text": "",
        "html": (
            "<html><body>"
            '<a href="https://www.linkedin.com/comm/jobs/view/4435393259/?trk=job-card">'
            "Example Pipeline<br>Data Engineer<br>Example Pipeline \u00b7 Calgary, AB</a>"
            '<a href="https://www.linkedin.com/comm/jobs/view/4435383460/?trk=job-card">'
            "Example College<br>AI Solutions Developer<br>Example College \u00b7 Calgary, AB"
            "<br>$20-$40 / hour</a>"
            "</body></html>"
        ),
    }

    adapter = LinkedInGmailAdapter()
    links = adapter.extract_links(message)

    assert [link.link_text for link in links] == ["Data Engineer", "AI Solutions Developer"]
    assert adapter.raw_text_for_link(message, links[0]) == (
        "Data Engineer\n"
        "Company: Example Pipeline\n"
        "Location: Calgary, AB\n"
        "Email Card: Example Pipeline | Data Engineer | Example Pipeline \u00b7 Calgary, AB"
    )
