from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.parser import parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


def observed_case():
    return next(
        case for case in load_parser_corpus().cases
        if case.id == "employer_title_suffix_corroborated_by_posting_body"
    )


@pytest.mark.parametrize("body", [
    "At Databricks, we build data and AI infrastructure.",
    "About Databricks\nDatabricks is the Data and AI company.",
])
@pytest.mark.parametrize("prefix", ["", "Page Title: ", "Title: "])
def test_rejected_provider_retains_body_corroborated_title_employer(body, prefix):
    case = observed_case()

    class RejectedParser:
        last_error = "codex_cli_invalid_parsed_job: invalid response"

        def parse(self, raw_text, source_url=None, baseline=None):
            return None

    parsed = parse_job_text_with_settings(
        prefix + case.raw_text.splitlines()[0] + "\n" + body,
        case.source_url,
        SimpleNamespace(posting_parser_provider="codex_cli"),
        parser=RejectedParser(),
        title_hint=case.title_hint,
    )

    assert parsed.employer.company_name == "Databricks"
    assert parsed.role.job_title == case.title_hint
    assert parsed.source.parser_outcome == "provider_fallback"
    assert parsed.source.parser_error_code == "codex_cli_invalid_parsed_job"


@pytest.mark.parametrize("body", [
    "Build features using Databricks.",
    "About Another Employer\nAt Another Employer, we build data infrastructure.",
    "Source Evidence: About Databricks",
    "Company: Another Employer\nAbout Databricks",
])
def test_title_suffix_needs_posting_body_corroboration_and_preserves_explicit_company(body):
    case = observed_case()
    parsed = parse_job_text(
        case.raw_text.splitlines()[0] + "\n" + body,
        case.source_url,
        title_hint=case.title_hint,
    )

    expected = "Another Employer" if body.startswith("Company:") else None
    assert parsed.employer.company_name == expected


def test_corroborated_title_suffix_must_belong_to_the_selected_role():
    case = observed_case()
    parsed = parse_job_text(
        case.raw_text, case.source_url, title_hint="Data Analyst Intern"
    )

    assert parsed.employer.company_name is None


def test_corroborated_title_suffix_does_not_require_an_ingestion_title_hint():
    case = observed_case()
    parsed = parse_job_text(case.raw_text, case.source_url)

    assert parsed.employer.company_name == "Databricks"
