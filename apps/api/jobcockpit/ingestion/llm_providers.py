from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol


class PostingParser(Protocol):
    def parse(
        self, raw_text: str, source_url: str | None = None, baseline: Any = None
    ) -> Any | None:
        """Parse a job posting into the app's ParsedJob contract."""


class SearchProvider(Protocol):
    def search(self, query: str, limit: int) -> Sequence[Any]:
        """Return public search results for a job identity query."""


class PostingResolver(Protocol):
    def resolve(self, hint: Any) -> Any:
        """Resolve an external job-board hint to a posting resolution."""


class CoverLetterGenerator(Protocol):
    def generate(self, context: Any) -> str | None:
        """Generate cover letter markdown for the supplied package context."""


class FitAnalyzer(Protocol):
    def analyze(
        self,
        parsed_job: Any,
        *,
        raw_text: str,
        source_url: str | None,
        profile_data: dict[str, Any] | None,
        evidence_items: list[Any],
    ) -> Any | None:
        """Analyze candidate fit for a parsed job."""


PostingParserFactory = Callable[[object], PostingParser]
SearchProviderFactory = Callable[[object], SearchProvider]
PostingResolverFactory = Callable[[object], PostingResolver]
CoverLetterGeneratorFactory = Callable[[object], CoverLetterGenerator]
FitAnalyzerFactory = Callable[[object], FitAnalyzer]

_posting_parser_providers: dict[str, PostingParserFactory] = {}
_search_providers: dict[str, SearchProviderFactory] = {}
_posting_resolver_providers: dict[str, PostingResolverFactory] = {}
_cover_letter_generator_providers: dict[str, CoverLetterGeneratorFactory] = {}
_fit_analyzer_providers: dict[str, FitAnalyzerFactory] = {}


def register_posting_parser_provider(
    name: str,
    factory: PostingParserFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _provider_name(name)
    _register(_posting_parser_providers, normalized, factory, replace=replace)


def register_search_provider(
    name: str,
    factory: SearchProviderFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _provider_name(name)
    _register(_search_providers, normalized, factory, replace=replace)


def register_posting_resolver_provider(
    name: str,
    factory: PostingResolverFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _provider_name(name)
    _register(_posting_resolver_providers, normalized, factory, replace=replace)


def register_cover_letter_generator_provider(
    name: str,
    factory: CoverLetterGeneratorFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _provider_name(name)
    _register(_cover_letter_generator_providers, normalized, factory, replace=replace)


def register_fit_analyzer_provider(
    name: str,
    factory: FitAnalyzerFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _provider_name(name)
    _register(_fit_analyzer_providers, normalized, factory, replace=replace)


def create_posting_parser(settings: object) -> PostingParser:
    provider = _provider_name(getattr(settings, "posting_parser_provider", "heuristic"))
    return _factory_for(_posting_parser_providers, provider, kind="posting parser")(settings)


def create_search_provider(settings: object) -> SearchProvider | None:
    provider = _provider_name(getattr(settings, "posting_search_provider", "disabled"))
    if provider == "disabled":
        return None
    return _factory_for(_search_providers, provider, kind="search")(settings)


def create_posting_resolver(settings: object) -> PostingResolver | None:
    provider = _provider_name(getattr(settings, "posting_resolver_provider", "search"))
    if provider in {"disabled", "search"}:
        return None
    return _factory_for(_posting_resolver_providers, provider, kind="posting resolver")(settings)


def create_cover_letter_generator(settings: object) -> CoverLetterGenerator:
    provider = _provider_name(getattr(settings, "cover_letter_generator_provider", "heuristic"))
    return _factory_for(_cover_letter_generator_providers, provider, kind="cover letter generator")(
        settings
    )


def create_fit_analyzer(settings: object) -> FitAnalyzer:
    provider = _provider_name(getattr(settings, "fit_analysis_provider", "disabled"))
    return _factory_for(_fit_analyzer_providers, provider, kind="fit analyzer")(settings)


def _register(
    providers: dict[str, Callable],
    name: str,
    factory: Callable,
    *,
    replace: bool,
) -> None:
    if name in providers and not replace:
        raise ValueError(f"Provider already registered: {name}")
    providers[name] = factory


def _factory_for(providers: dict[str, Callable], provider: str, *, kind: str) -> Callable:
    try:
        return providers[provider]
    except KeyError as error:
        raise ValueError(f"Unsupported {kind} provider: {provider}") from error


def _provider_name(name: object) -> str:
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("Provider name must not be empty")
    return normalized


def _codex_posting_parser_provider(settings: object) -> PostingParser:
    from jobcockpit.ingestion.codex_posting_parser import CodexCliPostingParser

    return CodexCliPostingParser(
        command=str(getattr(settings, "codex_cli_command", "codex")),
        model=getattr(settings, "codex_cli_model", None),
        reasoning_effort=getattr(settings, "posting_parser_reasoning_effort", "medium"),
        timeout_seconds=int(getattr(settings, "posting_parser_timeout_seconds", 60)),
        max_input_chars=int(getattr(settings, "posting_parser_max_input_chars", 12000)),
        workspace=getattr(settings, "codex_cli_workspace", None),
        ignore_user_config=bool(getattr(settings, "codex_cli_ignore_user_config", True)),
    )


def _serper_search_provider(settings: object) -> SearchProvider:
    from jobcockpit.integrations.serper import serper_search_client_from_settings

    return serper_search_client_from_settings(settings)


def _codex_cover_letter_generator_provider(settings: object) -> CoverLetterGenerator:
    from jobcockpit.documents.cover_letters import CodexCliCoverLetterGenerator

    return CodexCliCoverLetterGenerator(
        command=str(getattr(settings, "codex_cli_command", "codex")),
        model=(
            getattr(settings, "cover_letter_generator_model", None)
            or getattr(settings, "codex_cli_model", None)
        ),
        verification_model=(
            getattr(settings, "cover_letter_verification_model", None)
            or getattr(settings, "cover_letter_generator_model", None)
            or getattr(settings, "codex_cli_model", None)
        ),
        reasoning_effort=getattr(settings, "cover_letter_generator_reasoning_effort", "high"),
        verification_reasoning_effort=getattr(
            settings, "cover_letter_verification_reasoning_effort", "low"
        ),
        timeout_seconds=int(getattr(settings, "cover_letter_generator_timeout_seconds", 360)),
        max_input_chars=int(getattr(settings, "cover_letter_generator_max_input_chars", 20000)),
        workspace=getattr(settings, "codex_cli_workspace", None),
        ignore_user_config=bool(getattr(settings, "codex_cli_ignore_user_config", True)),
        timezone_name=str(getattr(settings, "cover_letter_timezone", "America/Edmonton")),
    )


def _codex_fit_analyzer_provider(settings: object) -> FitAnalyzer:
    from jobcockpit.ingestion.fit_analysis import CodexCliFitAnalyzer

    return CodexCliFitAnalyzer(
        command=str(getattr(settings, "codex_cli_command", "codex")),
        model=getattr(settings, "codex_cli_model", None),
        reasoning_effort=getattr(settings, "fit_analysis_reasoning_effort", "medium"),
        timeout_seconds=int(getattr(settings, "fit_analysis_timeout_seconds", 60)),
        max_input_chars=int(getattr(settings, "fit_analysis_max_input_chars", 16000)),
        workspace=getattr(settings, "codex_cli_workspace", None),
        ignore_user_config=bool(getattr(settings, "codex_cli_ignore_user_config", True)),
    )


register_posting_parser_provider("codex_cli", _codex_posting_parser_provider, replace=True)
register_search_provider("serper", _serper_search_provider, replace=True)
register_cover_letter_generator_provider(
    "codex_cli", _codex_cover_letter_generator_provider, replace=True
)
register_fit_analyzer_provider("codex_cli", _codex_fit_analyzer_provider, replace=True)
