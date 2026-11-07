from __future__ import annotations

from collections.abc import Callable

from jobcockpit.ingestion.ats_adapters.base import AtsProviderAdapter, AtsProviderError
from jobcockpit.ingestion.ats_adapters.canada_tech_internships_summer_2027 import (
    CanadaTechInternshipsSummer2027Adapter,
)
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships import CanadianTechInternshipsAdapter
from jobcockpit.ingestion.ats_adapters.canadian_tech_internships_2027 import (
    CanadianTechInternships2027Adapter,
)
from jobcockpit.ingestion.ats_adapters.csjobs import CsJobsAdapter
from jobcockpit.ingestion.ats_adapters.eluta import ElutaAdapter
from jobcockpit.ingestion.ats_adapters.hanzilla import HanzillaAdapter
from jobcockpit.ingestion.ats_adapters.oracle_candidate_experience import OracleCandidateExperienceAdapter
from jobcockpit.ingestion.ats_adapters.phenom_oracle import PhenomOracleAdapter
from jobcockpit.ingestion.ats_adapters.rankalpha_jobs import RankAlphaJobsAdapter
from jobcockpit.ingestion.ats_adapters.workday import WorkdayAdapter

AdapterFactory = Callable[[], AtsProviderAdapter]

_ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    CanadaTechInternshipsSummer2027Adapter.provider: CanadaTechInternshipsSummer2027Adapter,
    CanadianTechInternshipsAdapter.provider: CanadianTechInternshipsAdapter,
    CanadianTechInternships2027Adapter.provider: CanadianTechInternships2027Adapter,
    CsJobsAdapter.provider: CsJobsAdapter,
    ElutaAdapter.provider: ElutaAdapter,
    HanzillaAdapter.provider: HanzillaAdapter,
    OracleCandidateExperienceAdapter.provider: OracleCandidateExperienceAdapter,
    PhenomOracleAdapter.provider: PhenomOracleAdapter,
    RankAlphaJobsAdapter.provider: RankAlphaJobsAdapter,
    WorkdayAdapter.provider: WorkdayAdapter,
}


def get_ats_adapter(provider: str) -> AtsProviderAdapter:
    normalized_provider = provider.strip().lower()
    factory = _ADAPTER_FACTORIES.get(normalized_provider)
    if not factory:
        raise AtsProviderError(400, f"Unsupported ATS provider: {normalized_provider or provider}")
    return factory()


def registered_ats_providers() -> list[str]:
    return sorted(_ADAPTER_FACTORIES)
