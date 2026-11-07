from __future__ import annotations

from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import GitHubInternshipCollectionAdapter


class CanadaTechInternshipsSummer2027Adapter(GitHubInternshipCollectionAdapter):
    provider = "canada_tech_internships_summer_2027"
    display_name = "Canada Tech Internships Summer 2027"
    supported_owner = "michelleokolie"
    supported_repo = "canada-tech-internships-summer-2027"
    apply_column_names = ("Application/Link",)
