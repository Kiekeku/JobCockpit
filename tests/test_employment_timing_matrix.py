from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from typing import Any

import pytest

from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


@pytest.mark.parametrize("month", range(1, 13))
@pytest.mark.parametrize("day,suffix", [
    (1, "st"), (2, "nd"), (3, "rd"), (4, "th"),
    (11, "th"), (12, "th"), (13, "th"),
])
def test_observed_program_date_structure_accepts_other_ordinal_dates(month, day, suffix):
    # Generalize the observed Canada-program sentence, as requested, across dates.
    from calendar import month_name

    end_month = (month + 2) % 12 + 1
    end_year = 2027 + (month + 2) // 12
    parsed = parse_job_text(
        "Intern, Software Developer\nAbout the Canada Intern Program\n"
        f"The 2027 Canada program runs for 16 weeks "
        f"({month_name[month]} {day}{suffix} - {month_name[end_month]} 24th). "
        "All internships are paid."
    )
    assert parsed.employment_timing.start_date == f"2027-{month:02d}-{day:02d}"
    assert parsed.employment_timing.end_date == f"{end_year}-{end_month:02d}-24"
    assert parsed.employment_timing.duration_months == 4


@dataclass(frozen=True)
class EmploymentTimingCase:
    id: str
    raw_text: str
    expected_timing: dict[str, Any]
    ambiguous_fields: tuple[str, ...] = ()


EVIDENCE_ROUTING_CASES = (
    EmploymentTimingCase(
        id="typed_end_without_start",
        raw_text="Software Developer Intern\nEnd Date of Internship: December 31, 2026",
        expected_timing={
            "raw_label": None,
            "timing_type": "fixed_term",
            "start_date": None,
            "end_date": "2026-12-31",
            "duration_months": None,
            "schedule_type": None,
        },
    ),
    EmploymentTimingCase(
        id="schedule_in_details_field",
        raw_text=(
            "Software Developer Intern\n"
            "This is a 6 month internship. Details: The role is part-time."
        ),
        expected_timing={
            "raw_label": "6 month internship",
            "timing_type": "fixed_term",
            "duration_months": 6,
            "schedule_type": "part_time",
        },
    ),
)


def test_pasted_timing_fields_do_not_require_a_source_url() -> None:
    # Golden execution covers the same contract with its anonymized source URL.
    case = next(
        case for case in load_parser_corpus().cases
        if case.id == "combined_contract_type_duration"
    )
    result = parse_job_text(case.raw_text)

    actual = result.employment_timing.model_dump()
    for field, expected in case.expected["employment_timing"].items():
        assert actual[field] == expected, field
    assert result.data_quality.ambiguous_fields == case.expected["data_quality"]["ambiguous_fields"]
    assert result.data_quality.needs_manual_review == (
        case.expected["data_quality"]["needs_manual_review"]
    )


@pytest.mark.parametrize(
    "case",
    EVIDENCE_ROUTING_CASES,
    ids=lambda case: case.id,
)
def test_employment_timing_evidence_matrix(case: EmploymentTimingCase) -> None:
    result = parse_job_text(case.raw_text)

    actual_timing = result.employment_timing.model_dump()
    for field, expected in case.expected_timing.items():
        assert actual_timing[field] == expected, (case.id, field, actual_timing[field])
    assert tuple(result.data_quality.ambiguous_fields) == case.ambiguous_fields
    assert result.data_quality.needs_manual_review is bool(case.ambiguous_fields)


def test_combined_contract_duration_is_field_order_independent() -> None:
    fields = (
        "Start Date of Internship: September 1, 2026",
        "Contract Type And Duration: Full-time, 6 months",
        "Location: Toronto",
    )

    for ordered_fields in permutations(fields):
        result = parse_job_text("Software Developer Intern\n" + "\n".join(ordered_fields))

        context = ordered_fields
        assert result.employment_timing.start_date == "2026-09-01", context
        assert result.employment_timing.end_date == "2027-02-28", context
        assert result.employment_timing.duration_months == 6, context
        assert result.employment_timing.schedule_type == "full_time", context


NON_CURRENT_TIMING_CONTEXTS = (
    "Enrollment: Must be enrolled in Fall 2026.",
    "Applications for Fall 2026 are open.",
    "Note: Their internship took place in Fall 2026.",
    "Note: Their internship was in Fall 2026.",
    "The candidate completed a placement from January through April 2026.",
)


@pytest.mark.parametrize("distractor", NON_CURRENT_TIMING_CONTEXTS)
def test_non_current_timing_context_cannot_create_job_timing(distractor: str) -> None:
    result = parse_job_text(f"Software Developer Intern\n{distractor}")

    assert result.employment_timing.model_dump() == {
        "raw_label": None,
        "timing_type": "unknown",
        "start_date": None,
        "end_date": None,
        "duration_months": None,
        "duration_label": None,
        "hours_per_week_min": None,
        "hours_per_week_max": None,
        "schedule_type": None,
    }
    assert result.data_quality.ambiguous_fields == []
    assert result.data_quality.needs_manual_review is False


@pytest.mark.parametrize("distractor", NON_CURRENT_TIMING_CONTEXTS)
def test_non_current_timing_context_cannot_change_current_job_timing(distractor: str) -> None:
    baseline = parse_job_text(
        "Software Developer Intern\n"
        "Start Date: January 4, 2027\n"
        "Duration: 4 month internship"
    )
    result = parse_job_text(
        "Software Developer Intern\n"
        "Start Date: January 4, 2027\n"
        "Duration: 4 month internship\n"
        f"{distractor}"
    )

    assert result.employment_timing == baseline.employment_timing
    assert result.data_quality.ambiguous_fields == baseline.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is baseline.data_quality.needs_manual_review


@pytest.mark.parametrize(
    "term_line",
    [
        "Fall 2026",
        "Season: Fall 2026",
        "Academic Term: Fall 2026",
    ],
)
def test_legacy_academic_term_metadata_forms_are_employment_timing(
    term_line: str,
) -> None:
    result = parse_job_text(f"Software Developer Intern\n{term_line}")

    assert result.employment_timing.model_dump() == {
        "raw_label": "Fall 2026",
        "timing_type": "fixed_term",
        "start_date": "2026-09-01",
        "end_date": "2026-12-31",
        "duration_months": 4,
        "duration_label": None,
        "hours_per_week_min": None,
        "hours_per_week_max": None,
        "schedule_type": "full_time",
    }
    assert result.data_quality.ambiguous_fields == []
    assert result.data_quality.needs_manual_review is False


def test_commencement_and_completion_aliases_define_employment_dates() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Commencement Date: September 1, 2026\n"
        "Completion Date: December 18, 2026"
    )

    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date == "2026-12-18"
    assert result.employment_timing.duration_months == 4
    assert result.data_quality.ambiguous_fields == []
    assert result.data_quality.needs_manual_review is False

@pytest.mark.parametrize(
    "label_line",
    [
        "Expected Degree Completion Date: December 18, 2026",
        "Application Completion Date: August 24, 2026",
        "Onboarding Start Date: September 15, 2026",
        "University Program Commencement Date: September 1, 2026",
    ],
)
def test_qualified_non_employment_date_labels_do_not_define_term(
    label_line: str,
) -> None:
    result = parse_job_text(f"Software Developer Intern\n{label_line}")

    assert result.employment_timing.model_dump() == {
        "raw_label": None,
        "timing_type": "unknown",
        "start_date": None,
        "end_date": None,
        "duration_months": None,
        "duration_label": None,
        "hours_per_week_min": None,
        "hours_per_week_max": None,
        "schedule_type": None,
    }
    assert result.data_quality.ambiguous_fields == []
    assert result.data_quality.needs_manual_review is False
