"""Source form defaults shared by CLI shortcuts and the web provider catalog."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AtsProviderDefaults:
    source_url: str = ""
    search_text: str = ""
    limit: int = 25
    skip_interest_filter: bool = False
    skip_language_filter: bool = False


_GENERIC_DEFAULTS = AtsProviderDefaults()
_PROVIDER_DEFAULTS = {
    "hanzilla": AtsProviderDefaults("https://jobs.hanzilla.co/internships/"),
    "eluta": AtsProviderDefaults(
        'https://www.eluta.ca/search?q=co-op+field%3A%22Computing+-+Software+%26+Web+Development%22&l=&qc=',
        skip_interest_filter=True,
    ),
    "csjobs": AtsProviderDefaults(
        "https://csjobs.ca/jobs", limit=75, skip_interest_filter=True, skip_language_filter=True,
    ),
    "rankalpha_jobs": AtsProviderDefaults(
        "https://jobs.rankalpha.io/api/jobs?status=Open&status=Closing%20soon&sort=rank", limit=50,
    ),
    "canadian_tech_internships": AtsProviderDefaults(
        "https://github.com/negarprh/Canadian-Tech-Internships-2026?tab=readme-ov-file",
        search_text="software", limit=30,
    ),
    "canadian_tech_internships_2027": AtsProviderDefaults(
        "https://github.com/negarprh/Canadian-Tech-Internships-2027?tab=readme-ov-file",
        search_text="software", limit=30,
    ),
    "canada_tech_internships_summer_2027": AtsProviderDefaults(
        "https://github.com/michelleokolie/canada-tech-internships-summer-2027",
        search_text="software", limit=30,
    ),
}


def ats_provider_defaults(provider: str) -> AtsProviderDefaults:
    """Employer-specific adapters require a user-supplied portal URL."""
    return _PROVIDER_DEFAULTS.get(provider, _GENERIC_DEFAULTS)
