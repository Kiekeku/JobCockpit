from __future__ import annotations

from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import (
    GitHubInternshipCollectionAdapter,
)


class CanadianTechInternships2027Adapter(GitHubInternshipCollectionAdapter):
    provider = "canadian_tech_internships_2027"
    display_name = "Canadian Tech Internships 2027"
    supported_owner = "negarprh"
    supported_repo = "Canadian-Tech-Internships-2027"
