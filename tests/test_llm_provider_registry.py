from collections.abc import Sequence
import subprocess
import sys

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.documents.cover_letters import CodexCliCoverLetterGenerator
from jobcockpit.ingestion.parser import (
    CodexCliPostingParser,
    ParsedJob,
    parse_job_text,
    parse_job_text_with_settings,
)
from jobcockpit.ingestion.posting_discovery import (
    CodexCliRenderedPageResolver,
    PostingDiscoveryResolver,
    SearchResult,
    TracedRenderedPageResolver,
)
from jobcockpit.integrations.serper import SerperSearchClient
from jobcockpit.ingestion.posting_links import default_posting_resolver


def test_custom_posting_parser_provider_can_be_registered():
    from jobcockpit.ingestion.llm_providers import register_posting_parser_provider

    class CustomParser:
        def parse(
            self,
            raw_text: str,
            source_url: str | None = None,
            baseline: ParsedJob | None = None,
        ):
            return parse_job_text(
                "Custom Provider Engineer\nCompany: Example Labs\nLocation: Toronto, ON, Canada",
                source_url,
            )

    register_posting_parser_provider(
        "unit_test_parser",
        lambda settings: CustomParser(),
        replace=True,
    )

    parsed = parse_job_text_with_settings(
        "Fallback text",
        "https://jobs.example.test/1",
        Settings(posting_parser_provider="unit_test_parser"),
    )

    assert parsed.employer.company_name == "Example Labs"
    assert parsed.role.job_title == "Custom Provider Engineer"
    assert parsed.location.city == "Toronto"


def test_custom_search_provider_can_be_registered_for_posting_resolver():
    from jobcockpit.ingestion.llm_providers import register_search_provider

    class CustomSearchClient:
        def search(self, query: str, limit: int) -> Sequence[SearchResult]:
            return [
                SearchResult(title="Example careers", url="https://careers.example.test/jobs/1")
            ]

    register_search_provider(
        "unit_test_search",
        lambda settings: CustomSearchClient(),
        replace=True,
    )

    resolver = default_posting_resolver(
        Settings(
            posting_resolver_provider="search",
            posting_search_provider="unit_test_search",
            posting_search_max_results=3,
        )
    )

    assert resolver is not None
    assert (
        resolver.search_client.search("engineer", 3)[0].url == "https://careers.example.test/jobs/1"
    )


def test_custom_posting_resolver_provider_can_be_registered_for_default_resolver():
    from jobcockpit.ingestion.llm_providers import register_posting_resolver_provider

    class CustomPostingResolver:
        def resolve(self, hint):
            return {"status": "not_found", "error": "unit-test"}

    register_posting_resolver_provider(
        "unit_test_resolver",
        lambda settings: CustomPostingResolver(),
        replace=True,
    )

    resolver = default_posting_resolver(Settings(posting_resolver_provider="unit_test_resolver"))

    assert isinstance(resolver, CustomPostingResolver)


def test_serper_posting_resolver_attaches_rendered_fallback():
    resolver = default_posting_resolver(
        Settings(
            posting_resolver_provider="search",
            posting_search_provider="serper",
            serper_api_key="test-serper-key",
            posting_rendered_fallback_provider="codex_cli",
            codex_cli_command="codex-test",
            codex_cli_model="gpt-rendered-model",
            posting_rendered_reasoning_effort="medium",
        )
    )

    assert isinstance(resolver, PostingDiscoveryResolver)
    assert isinstance(resolver.search_client, SerperSearchClient)
    assert isinstance(resolver.rendered_fallback_resolver, TracedRenderedPageResolver)
    assert isinstance(
        resolver.rendered_fallback_resolver.inner_resolver,
        CodexCliRenderedPageResolver,
    )
    assert resolver.rendered_fallback_resolver.command == "codex-test"
    assert resolver.rendered_fallback_resolver.inner_resolver.model == "gpt-rendered-model"
    assert resolver.rendered_fallback_resolver.inner_resolver.reasoning_effort == "medium"


def test_custom_fit_analyzer_provider_can_be_registered():
    from jobcockpit.ingestion.llm_providers import (
        create_fit_analyzer,
        register_fit_analyzer_provider,
    )

    class CustomFitAnalyzer:
        def analyze(
            self,
            parsed_job,
            *,
            raw_text: str,
            source_url: str | None,
            profile_data: dict | None,
            evidence_items: list,
        ):
            return parsed_job.fit_analysis.model_copy(update={"fit_score": 77})

    register_fit_analyzer_provider(
        "unit_test_fit",
        lambda settings: CustomFitAnalyzer(),
        replace=True,
    )

    parsed = parse_job_text("Backend Engineer\nCompany: Acme\nPython")
    analyzer = create_fit_analyzer(Settings(fit_analysis_provider="unit_test_fit"))
    fit = analyzer.analyze(
        parsed,
        raw_text="Backend Engineer\nCompany: Acme\nPython",
        source_url=None,
        profile_data={"skills": ["Python"]},
        evidence_items=[],
    )

    assert fit.fit_score == 77


def test_builtin_codex_and_serper_providers_are_resolved_through_registry():
    from jobcockpit.ingestion.llm_providers import (
        create_cover_letter_generator,
        create_fit_analyzer,
        create_posting_parser,
        create_search_provider,
    )
    from jobcockpit.ingestion.fit_analysis import CodexCliFitAnalyzer

    settings = Settings(
        cover_letter_generator_provider="codex_cli",
        fit_analysis_provider="codex_cli",
        posting_parser_provider="codex_cli",
        posting_resolver_provider="search",
        posting_search_provider="serper",
        serper_api_key="test-serper-key",
        serper_country="ca",
        serper_language="en",
        serper_timeout_seconds=12,
        codex_cli_command="codex-test",
        codex_cli_model="gpt-test-model",
        cover_letter_generator_model="gpt-cover-model",
        cover_letter_verification_model="gpt-verification-model",
        posting_parser_reasoning_effort="medium",
        cover_letter_generator_reasoning_effort="high",
        cover_letter_generator_timeout_seconds=180,
        cover_letter_verification_reasoning_effort="medium",
        fit_analysis_reasoning_effort="medium",
        fit_analysis_timeout_seconds=90,
    )

    parser = create_posting_parser(settings)
    posting_resolver = default_posting_resolver(settings)
    search_client = create_search_provider(settings)
    cover_letter_generator = create_cover_letter_generator(settings)
    fit_analyzer = create_fit_analyzer(settings)

    assert isinstance(parser, CodexCliPostingParser)
    assert parser.command == "codex-test"
    assert parser.model == "gpt-test-model"
    assert parser.reasoning_effort == "medium"
    assert isinstance(posting_resolver, PostingDiscoveryResolver)
    assert isinstance(posting_resolver.search_client, SerperSearchClient)
    assert isinstance(search_client, SerperSearchClient)
    assert search_client.country == "ca"
    assert search_client.language == "en"
    assert search_client.timeout_seconds == 12
    assert isinstance(cover_letter_generator, CodexCliCoverLetterGenerator)
    assert cover_letter_generator.command == "codex-test"
    assert cover_letter_generator.model == "gpt-cover-model"
    assert cover_letter_generator.verification_model == "gpt-verification-model"
    assert cover_letter_generator.reasoning_effort == "high"
    assert cover_letter_generator.timeout_seconds == 180
    assert cover_letter_generator.verification_reasoning_effort == "medium"
    assert isinstance(fit_analyzer, CodexCliFitAnalyzer)
    assert fit_analyzer.command == "codex-test"
    assert fit_analyzer.model == "gpt-test-model"
    assert fit_analyzer.reasoning_effort == "medium"
    assert fit_analyzer.timeout_seconds == 90

    with pytest.raises(ValueError, match="Unsupported search provider: codex_cli"):
        create_search_provider(Settings(posting_search_provider="codex_cli"))


def test_builtin_providers_are_available_from_direct_registry_import():
    script = """
from jobcockpit.core.config import Settings
from jobcockpit.ingestion.llm_providers import (
    create_cover_letter_generator,
    create_fit_analyzer,
    create_posting_parser,
    create_search_provider,
)
from jobcockpit.ingestion.posting_links import default_posting_resolver

settings = Settings(
    cover_letter_generator_provider="codex_cli",
    posting_parser_provider="codex_cli",
    posting_resolver_provider="search",
    posting_search_provider="serper",
    serper_api_key="test-serper-key",
    codex_cli_command="codex-test",
)
print(type(create_cover_letter_generator(settings)).__name__)
print(type(create_fit_analyzer(Settings(fit_analysis_provider="codex_cli", codex_cli_command="codex-test"))).__name__)
print(type(create_posting_parser(settings)).__name__)
print(type(default_posting_resolver(settings)).__name__)
print(type(create_search_provider(settings)).__name__)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "CodexCliCoverLetterGenerator",
        "CodexCliFitAnalyzer",
        "CodexCliPostingParser",
        "PostingDiscoveryResolver",
        "SerperSearchClient",
    ]
