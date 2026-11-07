from jobcockpit.ingestion.ats_adapters.base import AtsFetchRequest, AtsJob, AtsProviderAdapter, AtsProviderError

__all__ = [
    "AtsFetchRequest",
    "AtsJob",
    "AtsProviderAdapter",
    "AtsProviderError",
    "get_ats_adapter",
    "registered_ats_providers",
]


def __getattr__(name: str):
    if name == "get_ats_adapter":
        from jobcockpit.ingestion.ats_adapters.registry import get_ats_adapter

        return get_ats_adapter
    if name == "registered_ats_providers":
        from jobcockpit.ingestion.ats_adapters.registry import registered_ats_providers

        return registered_ats_providers
    raise AttributeError(name)
