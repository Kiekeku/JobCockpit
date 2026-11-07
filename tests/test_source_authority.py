from __future__ import annotations

import pytest

from jobcockpit.ingestion.source_authority import JOB_PORTAL_REGISTRY, job_portal_kind
from jobcockpit.ingestion.posting_discovery import (
    ExternalAggregatorFallbackUrlPolicy,
    external_source_kind,
)
from jobcockpit.ingestion.posting_links import classify_posting_link


@pytest.mark.parametrize(
    ("url", "expected_kind"),
    [
        ("https://internhunt.eu/", "internhunt"),
        ("https://interninsider.me/jobs/123", "interninsider"),
        ("https://jobright.ai/jobs/info/123", "jobright"),
        ("https://www.bebee.com/job/123", "bebee"),
        ("https://builtin.com/job/software-engineer/123", "builtin"),
        ("https://careers.capitalg.com/jobs/acme/software-intern", "capitalg"),
        ("https://jobs.climatedraft.org/jobs/123", "climate_draft"),
        ("https://csjobs.ca/jobs/123", "csjobs"),
        ("https://jobs.hanzilla.co/internships/123", "hanzilla"),
        ("https://www.eluta.ca/search?q=software", "eluta"),
        ("https://ca.talent.com/view?id=123", "talent"),
        ("https://www.simplyhired.ca/job/123", "simplyhired"),
        ("https://ca.jobrapido.com/job/123", "jobrapido"),
        ("https://en-ca.whatjobs.com/job/123", "whatjobs"),
        ("https://www.nexxt.com/job/123", "nexxt"),
        ("https://www.jobleads.com/ca/job/example--markham--123", "jobleads"),
        ("https://www.recruit.net/job/example-jobs/123", "recruit_net"),
        ("https://ca.jobsora.com/jobs-front-end-developer-toronto-on", "jobsora"),
        ("https://www.adzuna.ca/embedded-systems", "adzuna"),
        ("https://ca.expertini.com/job/example/", "expertini"),
        ("https://jobsincanada.com/jobbank/company/152082/", "jobs_in_canada"),
        ("https://www.jobs.ca/acme/jobs/software-intern", "jobs_ca"),
        ("https://www.glassdoor.ca/job-listing/software-intern", "glassdoor"),
        ("https://www.ziprecruiter.com/jobs/acme/software-intern", "ziprecruiter"),
        ("https://www.workopolis.com/jobsearch/example", "workopolis"),
        ("https://ca.prosple.com/graduate-employers/acme/jobs", "prosple"),
        ("https://ca.indeed.com/viewjob?jk=123", "indeed"),
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
    ],
)
def test_job_portal_registry_classifies_known_non_authoritative_sources(
    url: str,
    expected_kind: str,
) -> None:
    assert job_portal_kind(url) == expected_kind


def test_job_portal_registry_does_not_match_lookalike_hosts() -> None:
    assert job_portal_kind("https://internhunt.eu.example.com/jobs/123") is None
    assert job_portal_kind("https://notlinkedin.com/jobs/view/123") is None
    assert job_portal_kind("ftp://internhunt.eu/jobs/123") is None


def test_every_job_portal_entry_has_reviewable_first_party_evidence() -> None:
    assert JOB_PORTAL_REGISTRY
    for entry in JOB_PORTAL_REGISTRY:
        assert entry.domains
        assert entry.evidence_url.startswith("https://")
        assert entry.evidence_summary.strip()


@pytest.mark.parametrize(
    "url",
    [
        "https://internhunt.eu/",
        "https://interninsider.me/internships/acme/software-intern",
        "https://jobright.ai/jobs/info/123",
        "https://careers.capitalg.com/jobs/acme/software-intern",
        "https://www.eluta.ca/search?q=software",
    ],
)
def test_registered_portals_route_to_external_resolution_and_allow_same_portal_fallback(
    url: str,
) -> None:
    assert external_source_kind(url) is None
    assert classify_posting_link(url).classification == "requires_external_resolution"
    policy_result = ExternalAggregatorFallbackUrlPolicy(url).check(url)
    assert policy_result.allowed
    assert policy_result.reason == "external_aggregator_fallback_allowed"


@pytest.mark.parametrize(
    ("url", "expected_classification"),
    [
        ("https://www.linkedin.com/company/acme", "social_or_marketing"),
        ("https://www.linkedin.com/help/linkedin", "contact_or_support"),
    ],
)
def test_linkedin_non_job_urls_keep_path_aware_classification(
    url: str,
    expected_classification: str,
) -> None:
    assert job_portal_kind(url) == "linkedin"
    assert external_source_kind(url) is None
    assert classify_posting_link(url).classification == expected_classification


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/1234567890",
        "https://ca.indeed.com/viewjob?jk=abcdef0123456789",
    ],
)
def test_capture_supported_job_urls_still_route_to_external_resolution(url: str) -> None:
    assert external_source_kind(url) is not None
    assert classify_posting_link(url).classification == "requires_external_resolution"
