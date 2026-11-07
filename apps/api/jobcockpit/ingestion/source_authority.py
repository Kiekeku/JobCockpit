from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class JobPortalRegistryEntry:
    """A reviewed source that republishes or indexes jobs for many employers."""

    kind: str
    domains: tuple[str, ...]
    evidence_url: str
    evidence_summary: str


JOB_PORTAL_REGISTRY: tuple[JobPortalRegistryEntry, ...] = (
    JobPortalRegistryEntry(
        kind="linkedin",
        domains=("linkedin.com",),
        evidence_url="https://www.linkedin.com/help/linkedin/answer/a511260",
        evidence_summary="LinkedIn documents Jobs as its cross-employer job-search product.",
    ),
    JobPortalRegistryEntry(
        kind="indeed",
        domains=("indeed.com", "indeed.ca"),
        evidence_url="https://www.indeed.com/about",
        evidence_summary="Indeed describes itself as a job site used to search for jobs.",
    ),
    JobPortalRegistryEntry(
        kind="internhunt",
        domains=("internhunt.eu",),
        evidence_url="https://internhunt.eu/",
        evidence_summary=(
            "InternHunt says it aggregates public listings from job boards and company "
            "career pages into a deduplicated feed."
        ),
    ),
    JobPortalRegistryEntry(
        kind="interninsider",
        domains=("interninsider.me",),
        evidence_url="https://interninsider.me/",
        evidence_summary=(
            "Intern Insider calls itself a student job board and sends applicants to "
            "company career pages."
        ),
    ),
    JobPortalRegistryEntry(
        kind="jobright",
        domains=("jobright.ai",),
        evidence_url="https://jobright.ai/",
        evidence_summary=(
            "Jobright calls itself an AI job-hunting platform and cross-ATS job hub."
        ),
    ),
    JobPortalRegistryEntry(
        kind="bebee",
        domains=("bebee.com",),
        evidence_url="https://bebee.com/gb/about",
        evidence_summary=(
            "beBee describes a multi-employer platform where professionals find jobs."
        ),
    ),
    JobPortalRegistryEntry(
        kind="builtin",
        domains=("builtin.com",),
        evidence_url="https://builtin.com/articles/our-sites",
        evidence_summary=(
            "Built In describes its sites as job boards with real-time listings."
        ),
    ),
    JobPortalRegistryEntry(
        kind="capitalg",
        domains=("careers.capitalg.com",),
        evidence_url="https://careers.capitalg.com/jobs",
        evidence_summary=(
            "CapitalG's careers portal lists opportunities across its portfolio companies."
        ),
    ),
    JobPortalRegistryEntry(
        kind="climate_draft",
        domains=("jobs.climatedraft.org",),
        evidence_url="https://jobs.climatedraft.org/jobs",
        evidence_summary=(
            "Climate Draft labels the site a job board spanning hundreds of companies."
        ),
    ),
    JobPortalRegistryEntry(
        kind="csjobs",
        domains=("csjobs.ca",),
        evidence_url="https://csjobs.ca/jobs",
        evidence_summary=(
            "CS Jobs provides cross-employer job browsing, filters, and apply links."
        ),
    ),
    JobPortalRegistryEntry(
        kind="hanzilla",
        domains=("jobs.hanzilla.co",),
        evidence_url="https://jobs.hanzilla.co/internships/",
        evidence_summary=(
            "Hanzilla says its student-job feed is sourced from company career pages "
            "and provides direct apply links."
        ),
    ),
    JobPortalRegistryEntry(
        kind="eluta",
        domains=("eluta.ca",),
        evidence_url="https://www.eluta.ca/postjobs",
        evidence_summary=(
            "Eluta describes itself as a job-search engine that indexes employer jobs."
        ),
    ),
    JobPortalRegistryEntry(
        kind="talent",
        domains=("talent.com",),
        evidence_url=(
            "https://helpcenter.talent.com/hc/en-ca/sections/49966913416980-"
            "About-Talent-com"
        ),
        evidence_summary=(
            "Talent.com describes itself as an intermediary that gathers openings "
            "from across the web."
        ),
    ),
    JobPortalRegistryEntry(
        kind="simplyhired",
        domains=("simplyhired.com", "simplyhired.ca"),
        evidence_url="https://www.simplyhired.com/en-US",
        evidence_summary="SimplyHired presents a cross-employer job-search service.",
    ),
    JobPortalRegistryEntry(
        kind="jobrapido",
        domains=("jobrapido.com",),
        evidence_url=(
            "https://support.jobrapido.com/hc/en-us/articles/"
            "360013273633-What-is-Jobrapido"
        ),
        evidence_summary=(
            "Jobrapido describes itself as a job-search engine that gathers ads from "
            "company and employment sites."
        ),
    ),
    JobPortalRegistryEntry(
        kind="whatjobs",
        domains=("whatjobs.com",),
        evidence_url="https://www.whatjobs.com/nav/about-us",
        evidence_summary="WhatJobs describes a multi-employer job-search platform.",
    ),
    JobPortalRegistryEntry(
        kind="nexxt",
        domains=("nexxt.com",),
        evidence_url=(
            "https://about.nexxt.com/press/"
            "20170619-beyond-the-career-network-reinvents-itself-as-nexxt/"
        ),
        evidence_summary=(
            "Nexxt describes its employment network as spanning many career sites and "
            "thousands of companies."
        ),
    ),
    JobPortalRegistryEntry(
        kind="jobleads",
        domains=("jobleads.com",),
        evidence_url="https://support.jobleads.com/hc/en-us/articles/21152173789458-About-us",
        evidence_summary=(
            "JobLeads describes itself as a job-search platform with millions of jobs "
            "from many employers."
        ),
    ),
    JobPortalRegistryEntry(
        kind="recruit_net",
        domains=("recruit.net",),
        evidence_url="https://www.recruit.net/aboutus.html",
        evidence_summary=(
            "Recruit.net describes a global job-search platform that gathers listings "
            "from corporate sites, job boards, agencies, and other sources."
        ),
    ),
    JobPortalRegistryEntry(
        kind="jobsora",
        domains=("jobsora.com",),
        evidence_url="https://jobsora.com/terms",
        evidence_summary=(
            "Jobsora's terms identify the service as a job aggregator that indexes "
            "vacancies published by third parties."
        ),
    ),
    JobPortalRegistryEntry(
        kind="adzuna",
        domains=("adzuna.com", "adzuna.ca"),
        evidence_url="https://www.adzuna.com/blog/trust-safety/",
        evidence_summary=(
            "Adzuna describes itself as a job-matching platform that aggregates job "
            "listings from third-party sources."
        ),
    ),
    JobPortalRegistryEntry(
        kind="expertini",
        domains=("expertini.com",),
        evidence_url="https://patti.in.expertini.com/what-is-expertini/",
        evidence_summary=(
            "Expertini says listings span direct employer posts and authorized "
            "third-party partner feeds, so the portal is not employer-authoritative."
        ),
    ),
    JobPortalRegistryEntry(
        kind="jobs_in_canada",
        domains=("jobsincanada.com",),
        evidence_url="https://jobsincanada.com/jobbank/",
        evidence_summary=(
            "JobsInCanada operates a cross-employer job bank and employer directory."
        ),
    ),
    JobPortalRegistryEntry(
        kind="jobs_ca",
        domains=("jobs.ca",),
        evidence_url="https://www.jobs.ca/",
        evidence_summary=(
            "Jobs.ca presents a cross-employer Canadian job network and directory."
        ),
    ),
    JobPortalRegistryEntry(
        kind="glassdoor",
        domains=("glassdoor.com", "glassdoor.ca"),
        evidence_url="https://www.glassdoor.com/about/",
        evidence_summary=(
            "Glassdoor describes a job-matching service spanning companies and roles."
        ),
    ),
    JobPortalRegistryEntry(
        kind="ziprecruiter",
        domains=("ziprecruiter.com",),
        evidence_url="https://www.ziprecruiter.com/about",
        evidence_summary=(
            "ZipRecruiter describes itself as a multi-employer online employment "
            "marketplace."
        ),
    ),
    JobPortalRegistryEntry(
        kind="workopolis",
        domains=("workopolis.com",),
        evidence_url="https://www.workopolis.com/",
        evidence_summary=(
            "Workopolis provides cross-employer job search and company browsing."
        ),
    ),
    JobPortalRegistryEntry(
        kind="prosple",
        domains=("prosple.com",),
        evidence_url=(
            "https://recruiters.prosple.com/resources-all/post/"
            "jobs-on-prosple-how-it-works"
        ),
        evidence_summary=(
            "Prosple documents employer-uploaded and internally curated listings on "
            "its student jobs portal."
        ),
    ),
)


def job_portal_kind(url: str) -> str | None:
    """Return the reviewed portal kind without treating hostname lookalikes as matches."""

    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    for entry in JOB_PORTAL_REGISTRY:
        if any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in entry.domains
        ):
            return entry.kind
    return None
