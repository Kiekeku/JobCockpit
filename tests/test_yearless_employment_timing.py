from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.parser import parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


def observed_student_term():
    return next(
        case for case in load_parser_corpus().cases
        if case.id == "student_position_duration_with_yearless_work_term"
    ).raw_text


@pytest.mark.parametrize(
    ("posting_date", "year"),
    [("2026-01-24", 2026), ("2026-10-06", 2027), ("2026-12-31", 2027)],
)
def test_yearless_month_range_uses_saved_month_boundary(posting_date, year):
    text = observed_student_term().replace("Oct 5, 2026", posting_date)
    parsed = parse_job_text(text)
    assert parsed.employment_timing.start_date == f"{year}-01-01"
    assert parsed.employment_timing.end_date == f"{year}-04-30"
    assert parsed.employment_timing.raw_label == "January - April"
    assert parsed.data_quality.inferred_fields == [
        "employment_timing.start_date", "employment_timing.end_date",
    ]


def test_yearless_month_range_without_reference_date_stays_unresolved():
    text = observed_student_term().replace("\nDate:\nOct 5, 2026", "")
    parsed = parse_job_text(text)
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert not parsed.data_quality.inferred_fields


def test_explicit_work_term_year_takes_precedence_over_next_year_assumption():
    text = next(
        case for case in load_parser_corpus().cases
        if case.id == "temporary_schedule_parenthesized_month_range"
    ).raw_text + "\nPosted: 2027-10-06"
    parsed = parse_job_text(text)
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-08-31"
    assert not parsed.data_quality.inferred_fields


def test_provider_finalization_retains_yearless_term_inference():
    class UnchangedProvider:
        def parse(self, raw_text, source_url=None, baseline=None):
            return baseline.model_copy(deep=True)

    parsed = parse_job_text_with_settings(
        observed_student_term(),
        settings=SimpleNamespace(posting_parser_provider="fixture"),
        parser=UnchangedProvider(),
    )
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.data_quality.inferred_fields == [
        "employment_timing.start_date", "employment_timing.end_date",
    ]
