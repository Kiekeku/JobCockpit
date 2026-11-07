from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.parser import parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


def observed_case():
    return next(
        case for case in load_parser_corpus().cases
        if case.id == "frostbite_brand_resolves_to_explicit_ea_employer"
    )


@pytest.mark.parametrize("provider_success", [False, True])
@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_corroborated_frostbite_employer_normalizes_provider_success_and_fallback(
    provider_success, apostrophe,
):
    class Provider:
        last_error = "codex_cli_invalid_parsed_job: rejected response"

        def parse(self, raw_text, source_url=None, baseline=None):
            if not provider_success:
                return None
            parsed = baseline.model_copy(deep=True)
            parsed.employer.company_name = "Frostbite"
            parsed.employer.division = None
            parsed.employer.industry = "Computer Games"
            return parsed

    parsed = parse_job_text_with_settings(
        observed_case().raw_text.replace("EA's", f"EA{apostrophe}s"),
        settings=SimpleNamespace(posting_parser_provider="codex_cli"),
        parser=Provider(),
    )

    assert parsed.employer.company_name == "Electronic Arts"
    assert parsed.employer.division == "Frostbite"
    if provider_success:
        assert parsed.employer.industry == "Computer Games"
    assert parsed.source.parser_outcome == (
        "provider_success" if provider_success else "provider_fallback"
    )


@pytest.mark.parametrize("evidence", [
    "Frostbite is EA's proprietary game engine and suite of tools.",
    "Electronic Arts is an equal opportunity employer.",
    "About Electronic Arts\nBuild tools with the Frostbite game engine.",
    "Source Evidence: Frostbite is EA's proprietary game engine.\n"
    "Electronic Arts is an equal opportunity employer.",
    "Frostbite is EA's proprietary game engine.\n"
    "Resolution Evidence: Electronic Arts is an equal opportunity employer.",
    "Build engine tools.\nSimilar jobs\nSoftware Engineer Intern\n"
    "Frostbite is EA's proprietary game engine.\n"
    "Electronic Arts is an equal opportunity employer.",
])
def test_frostbite_brand_is_preserved_without_both_posting_body_statements(evidence):
    parsed = parse_job_text("Title: Software Engineer Intern\nCompany: Frostbite\n" + evidence)

    assert parsed.employer.company_name == "Frostbite"
    assert parsed.employer.division is None


@pytest.mark.parametrize("company", ["EA SPORTS", "Electronic Arts Inc. / EA", "Another Employer"])
def test_ea_body_does_not_replace_other_explicit_employers(company):
    text = observed_case().raw_text.replace("Company: Frostbite", f"Company: {company}")
    parsed = parse_job_text(text)

    assert parsed.employer.company_name == company
    assert parsed.employer.division is None


def test_provider_conflicting_division_is_preserved():
    class Provider:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employer.company_name = "Frostbite"
            parsed.employer.division = "Another Division"
            return parsed

    parsed = parse_job_text_with_settings(
        observed_case().raw_text,
        settings=SimpleNamespace(posting_parser_provider="codex_cli"),
        parser=Provider(),
    )

    assert parsed.employer.company_name == "Frostbite"
    assert parsed.employer.division == "Another Division"
