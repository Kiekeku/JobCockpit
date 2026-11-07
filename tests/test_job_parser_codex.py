import json
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.browser_capture_adapters import (
    BrowserCaptureContext,
    extract_browser_capture_fields,
)
from jobcockpit.ingestion.parser import (
    CodexCliPostingParser,
    parse_job_text,
    parse_job_text_with_settings,
)
from jobcockpit.ingestion.parser_models import ParsedDataQuality, ParsedTiming
from jobcockpit.ingestion.parser_corpus import load_parser_corpus
from jobcockpit.ingestion.posting_fields import _field_value
from jobcockpit.ingestion.posting_scope import _posting_field_spans
from jobcockpit.ingestion.provider_timing import (
    _merge_provider_timing_fields,
    _provider_timing_values_match_raw_label,
)


@pytest.mark.parametrize("case_id", [
    "coop_commitment_parenthesized_calendar",
    "temporary_opportunity_starting_on_exact_date",
    "onsite_coop_range_preserves_open_end",
    "split_onsite_coop_range_and_start",
    "french_stage_session_and_labeled_dates",
    "french_mandate_season_heading",
    "french_stage_body_season_and_credited_session",
    "intern_may_june_start_year_alternatives",
    "standalone_sixteen_month_internship_with_start",
    "standalone_internship_preserves_stated_previous_year",
    "duration_option_calendar_conflict_preserves_options",
    "single_season_exact_date_duration_options",
    "intern_title_parenthesized_duration_without_season",
    "mandate_base_duration_and_possible_extension",
    "internship_program_length_field_with_start",
    "completed_academic_year_is_not_employment_duration",
    "split_internship_duration_not_graduation_recency",
    "suspended_hyphen_work_term_keeps_start",
    "suspended_hyphen_duration_choices_after_graduation",
    "part_time_season_availability",
    "unpunctuated_start_end_month_fields",
    "split_start_end_iso_fields",
    "duration_starting_on_exact_date",
    "french_expected_start_end_months",
    "season_with_listed_duration_opportunities",
    "french_season_in_stage_title",
    "counted_cohort_month_ranges",
    "graduation_recency_is_not_term_duration",
    "internship_in_summer_of_year",
    "qualified_role_month_year_duration",
    "qualified_role_preserves_unknown_schedule",
    "intern_header_standalone_start_month",
    "intern_availability_alternative_months",
    "intern_availability_slash_months_duration_options",
    "winter_semester_word_in_intern_title",
    "winter_term_word_in_coop_title",
    "winter_term_word_with_explicit_duration",
    "fall_term_word_overrides_posted_date_fallback",
    "program_ordinal_winter_dates",
    "program_ordinal_fall_dates",
    "program_ordinal_dates_without_spaces",
    "program_ordinal_dates_preserve_work_term_schedule",
    "internship_program_ordinal_dates_with_leading_year",
    "contract_duration_month_range_with_comma_year",
    "student_position_start_date_of_exact_pair",
    "research_program_week_duration_exact_dates",
    "student_role_unlabeled_month_range_after_pay",
    "intern_available_for_period_of_months",
    "student_contract_period_approximately_word_months",
    "year_leading_winter_term_with_alternative_start_months",
    "multi_season_duration_matrix_colon_headings",
    "multi_season_duration_matrix_flattened_colon_headings",
    "french_contract_duration_corroborated_by_mandate",
])
def test_provider_missing_timing_preserves_observed_timing_source(case_id):
    case = next(case for case in load_parser_corpus().cases if case.id == case_id)

    class MissingTimingParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing = ParsedTiming()
            return parsed

    parsed = parse_job_text_with_settings(
        case.raw_text, case.source_url, Settings(posting_parser_provider="codex_cli"),
        parser=MissingTimingParser(), source_type=case.source_type,
        title_hint=case.title_hint,
    )
    actual = parsed.employment_timing.model_dump()
    for field, expected in case.expected["employment_timing"].items():
        assert actual[field] == expected, field


@pytest.mark.parametrize("year", [2027, 2031])
@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_observed_onsite_coop_range_survives_title_hint(year, dash):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "split_onsite_coop_range_and_start")
    text = case.raw_text.replace("2027", str(year)).replace("4-8", f"4{dash}8")
    parsed = parse_job_text(text, title_hint="Embedded Software Developer Co-op")
    assert parsed.employment_timing.raw_label == f"Winter {year}"
    assert parsed.employment_timing.start_date == f"{year}-01-01"
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.employment_timing.duration_label == "4-8 months"


@pytest.mark.parametrize("case_id,old_timing", [
    ("intern_may_june_start_year_alternatives", {
        "raw_label": "Summer 2027", "start_date": "2027-05-01", "end_date": "2027-07-31", "duration_months": 3,
    }),
    ("single_season_exact_date_duration_options", {
        "raw_label": "Summer 2027", "start_date": "2027-05-03", "end_date": "2028-04-21", "duration_months": 12,
    }),
    ("completed_academic_year_is_not_employment_duration", {
        "raw_label": "one year in an engineering program", "duration_months": 12,
    }),
])
def test_provider_cannot_restore_observed_term_errors(case_id, old_timing):
    case = next(case for case in load_parser_corpus().cases if case.id == case_id)

    class OldTimingParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing = ParsedTiming(timing_type="fixed_term", **old_timing)
            parsed.data_quality.ambiguous_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        case.raw_text, settings=Settings(posting_parser_provider="codex_cli"), parser=OldTimingParser(),
    )
    actual = parsed.employment_timing.model_dump()
    for field, expected in case.expected["employment_timing"].items():
        assert actual[field] == expected, field


@pytest.mark.parametrize("title_hint", [
    "Summer 2027 Long Term Software Engineering Intern/Co-op",
    "Graphics Software Engineer Intern/Co-op",
])
def test_observed_duration_options_remain_ambiguous_with_title_hint(title_hint):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "single_season_exact_date_duration_options")
    parsed = parse_job_text(case.raw_text, title_hint=title_hint)
    assert parsed.employment_timing.duration_months is None
    assert parsed.employment_timing.end_date is None
    assert "employment_timing.duration_months" in parsed.data_quality.ambiguous_fields
    assert "employment_timing.end_date" in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review


@pytest.mark.parametrize("year", [2028, 2031])
@pytest.mark.parametrize("month,number,term", [
    ("January", 1, "Winter"), ("February", 2, "Winter"),
    ("March", 3, "Winter"), ("April", 4, "Winter"),
    ("May", 5, "Summer"), ("June", 6, "Summer"),
    ("July", 7, "Summer"), ("August", 8, "Summer"),
    ("September", 9, "Fall"), ("October", 10, "Fall"),
    ("November", 11, "Fall"), ("December", 12, "Fall"),
])
def test_observed_duration_starting_on_generalizes(year, month, number, term):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "duration_starting_on_exact_date")
    text = case.raw_text.replace("January 4, 2027", f"{month} 4, {year}")
    timing = parse_job_text(text).employment_timing
    assert timing.raw_label == f"{term} {year}"
    assert timing.start_date == f"{year}-{number:02d}-04"
    assert timing.duration_months == 8


@pytest.mark.parametrize("year", [2028, 2031])
@pytest.mark.parametrize("season,term,month", [
    ("Hiver", "Winter", 1), ("\u00c9t\u00e9", "Summer", 5),
    ("Printemps", "Summer", 5), ("Automne", "Fall", 9),
])
def test_observed_french_title_generalizes(year, season, term, month):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "french_season_in_stage_title")
    text = case.raw_text.replace("Hiver 2027", f"{season} {year}")
    timing = parse_job_text(text).employment_timing
    assert timing.raw_label == f"{term} {year}"
    assert timing.start_date == f"{year}-{month:02d}-01"
    assert timing.duration_months == 4


@pytest.mark.parametrize("year", [2027, 2028, 2030])
@pytest.mark.parametrize("season,start,end", [
    ("Winter", "01-01", "04-30"),
    ("Summer", "05-01", "08-31"),
    ("Spring", "05-01", "08-31"),
    ("Fall", "09-01", "12-31"),
    ("Autumn", "09-01", "12-31"),
])
def test_observed_semester_title_generalizes_across_seasons_and_years(
    year, season, start, end,
):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "winter_semester_word_in_intern_title")
    # Keep the observed title structure while varying the season and year.
    raw_text = case.raw_text.replace("Winter", season).replace("2027", str(year))
    title = case.title_hint.replace("Winter", season).replace("2027", str(year))
    parsed = parse_job_text(raw_text, source_type=case.source_type, title_hint=title)
    assert parsed.employment_timing.raw_label == f"{season} {year}"
    assert parsed.employment_timing.start_date == f"{year}-{start}"
    assert parsed.employment_timing.end_date == f"{year}-{end}"
    assert parsed.employment_timing.duration_months == 4


@pytest.mark.parametrize("year", [2027, 2031])
@pytest.mark.parametrize("first,second,term", [
    ("January", "February", "Winter"),
    ("May", "June", "Summer"),
    ("September", "October", "Fall"),
    ("August", "September", "Summer/Fall"),
])
@pytest.mark.parametrize("separator", [" or ", "/"])
def test_observed_intern_availability_preserves_month_alternatives(
    year, first, second, term, separator,
):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "intern_availability_alternative_months")
    raw_text = case.raw_text.replace(
        "May or June 2027", f"{first}{separator}{second} {year}",
    ).replace("2028", str(year + 2))
    parsed = parse_job_text(raw_text)
    assert parsed.employment_timing.raw_label == f"{term} {year}"
    assert parsed.employment_timing.duration_months == 16
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert "employment_timing.start_date" in parsed.data_quality.ambiguous_fields
    assert "employment_timing.end_date" in parsed.data_quality.ambiguous_fields


@pytest.mark.parametrize("year", [2027, 2030])
@pytest.mark.parametrize("month,number,term", [
    ("January", "01", "Winter"),
    ("May", "05", "Summer"),
    ("September", "09", "Fall"),
])
def test_observed_role_header_start_month_generalizes(year, month, number, term):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "intern_header_standalone_start_month")
    parsed = parse_job_text(case.raw_text.replace("January 2027", f"{month} {year}"))
    assert parsed.employment_timing.raw_label == f"{term} {year}"
    assert parsed.employment_timing.start_date == f"{year}-{number}-01"
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None


@pytest.mark.parametrize("year", [2027, 2028, 2031])
@pytest.mark.parametrize("month", range(1, 13))
@pytest.mark.parametrize("label", ["Ats Eluta Role", "Linkedin Hint Role"])
def test_qualified_role_start_generalizes_across_months_and_years(year, month, label):
    import calendar

    case = next(case for case in load_parser_corpus().cases
                if case.id == "qualified_role_month_year_duration")
    raw_text = case.raw_text.replace("Ats Eluta Role", label).replace(
        "January 2027", f"{calendar.month_name[month]} {year}",
    )
    parsed = parse_job_text(raw_text, title_hint=case.title_hint)
    timing = parsed.employment_timing
    term = ("Winter", "Summer", "Fall")[(month - 1) // 4]
    end_year, end_index = divmod(year * 12 + month - 1 + 3, 12)
    end_month = end_index + 1
    assert timing.raw_label == f"{term} {year}"
    assert timing.start_date == f"{year}-{month:02}-01"
    assert timing.end_date == (
        f"{end_year}-{end_month:02}-{calendar.monthrange(end_year, end_month)[1]:02}"
    )
    assert timing.duration_months == 4
    assert timing.schedule_type is None


@pytest.mark.parametrize("year", [2027, 2028, 2031])
@pytest.mark.parametrize("season,start,end", [
    ("winter", "01-01", "04-30"),
    ("summer", "05-01", "08-31"),
    ("fall", "09-01", "12-31"),
])
def test_observed_season_of_year_generalizes(year, season, start, end):
    case = next(case for case in load_parser_corpus().cases
                if case.id == "internship_in_summer_of_year")
    raw_text = case.raw_text.replace("summer", season).replace("2027", str(year))
    timing = parse_job_text(raw_text).employment_timing
    assert timing.raw_label == f"{season.title()} {year}"
    assert timing.start_date == f"{year}-{start}"
    assert timing.end_date == f"{year}-{end}"
    assert timing.duration_months == 4
    assert timing.schedule_type == "full_time"


def test_provider_cannot_restore_stale_year_over_observed_winter_correction():
    case = next(case for case in load_parser_corpus().cases
                if case.id == "winter_availability_corrects_stale_year_from_posting_date")

    class StaleYearParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing = ParsedTiming(
                raw_label="Available for four months starting January 2026",
                timing_type="fixed_term", start_date="2026-01-01",
                end_date="2026-04-30", duration_months=4,
            )
            parsed.data_quality.inferred_fields = []
            parsed.data_quality.ambiguous_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        case.raw_text, case.source_url, Settings(posting_parser_provider="codex_cli"),
        parser=StaleYearParser(), source_type=case.source_type,
    )
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.data_quality.inferred_fields == [
        "employment_timing.start_date", "employment_timing.end_date",
    ]
    assert parsed.data_quality.ambiguous_fields == [
        "employment_timing.start_date", "employment_timing.end_date",
    ]
    assert parsed.data_quality.needs_manual_review
    assert "January 2026" in parsed.role.description_raw


def test_data_quality_ambiguities_are_canonical_field_paths() -> None:
    quality = ParsedDataQuality(
        ambiguous_fields=[
            "location.city: conflicting locations were present",
            "Security clearance requirements vary by assignment",
            "Work authorization differs by office",
        ]
    )

    assert quality.ambiguous_fields == [
        "location.city",
        "data_quality.ambiguous_fields",
        "eligibility.work_authorization",
    ]


def test_data_quality_preserves_multiple_paths_from_one_ambiguity() -> None:
    quality = ParsedDataQuality(
        ambiguous_fields=[
            "employment_timing.start_date and employment_timing.end_date conflict"
        ]
    )

    assert quality.ambiguous_fields == [
        "employment_timing.start_date",
        "employment_timing.end_date",
    ]


def test_codex_cli_posting_parser_returns_valid_parsed_job():
    calls = []

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "source": {
                        "platform": "indeed",
                        "source_type": "gmail_alert",
                        "source_url": "https://ca.indeed.com/viewjob?jk=abc123",
                    },
                    "employer": {
                        "company_name": "Globex",
                        "division": None,
                        "industry": "Software",
                    },
                    "role": {
                        "job_title": "Backend Engineering Intern",
                        "normalized_title": "backend_engineering_intern",
                        "job_family": "software_engineering",
                        "seniority": "intern",
                        "role_type": "internship",
                        "description_raw": "Backend Engineering Intern role using Python and SQL.",
                        "description_clean": "Backend Engineering Intern role using Python and SQL.",
                        "responsibilities": ["Build APIs"],
                        "must_have_requirements": ["Python"],
                        "nice_to_have_requirements": ["SQL"],
                        "tech_stack": ["Python", "SQL"],
                        "domain_keywords": ["APIs"],
                    },
                    "employment_timing": {
                        "raw_label": "Fall 2026",
                        "timing_type": "fixed_term",
                        "start_date": "2026-09-01",
                        "end_date": "2026-12-18",
                        "duration_months": 4,
                        "hours_per_week_min": 35,
                        "hours_per_week_max": 40,
                        "schedule_type": "full_time",
                    },
                    "location": {
                        "work_arrangement": "remote",
                        "city": "Toronto",
                        "province_state": "Ontario",
                        "country": "Canada",
                        "remote_eligibility": "Canada",
                    },
                    "eligibility": {
                        "current_enrollment_required": True,
                        "co_op_enrollment_required": True,
                        "internship_eligible_only": True,
                        "work_authorization": "Canada",
                    },
                    "compensation": {
                        "pay_min": 32,
                        "pay_max": 38,
                        "pay_currency": "CAD",
                        "pay_period": "hourly",
                    },
                    "application": {
                        "application_deadline": "2026-07-15",
                        "application_method": "external",
                        "external_apply_url": "https://ca.indeed.com/viewjob?jk=abc123",
                        "documents_required": ["resume", "cover_letter"],
                        "resume_required": True,
                        "cover_letter_required": True,
                        "requires_manual_submission": True,
                        "autofill_supported": False,
                        "autofill_support_level": "none",
                        "document_upload_supported": "unknown",
                        "upload_targets": [],
                    },
                    "fit_analysis": {
                        "fit_score": 82,
                        "technical_match_score": 80,
                        "eligibility_score": 90,
                        "interest_score": 75,
                        "compensation_score": None,
                        "timing_location_score": 85,
                        "application_effort_score": 70,
                        "risk_flags": [],
                        "missing_requirements": [],
                        "ranking_reason": ["Matches Python and co-op requirements"],
                        "tailoring_strategy": "Emphasize API projects.",
                    },
                    "data_quality": {
                        "extraction_confidence": 0.93,
                        "missing_fields": [],
                        "ambiguous_fields": [],
                        "inferred_fields": ["employment_timing.start_date"],
                        "needs_manual_review": False,
                    },
                }
            ),
            stderr="",
        )

    workspace = Path("/workspace/JobCockpit")
    parser = CodexCliPostingParser(
        command="codex",
        runner=fake_runner,
        workspace=workspace,
        model="gpt-6-luna",
        reasoning_effort="max",
    )

    parsed = parser.parse(
        "Backend Engineering Intern at Globex\nPython SQL\nFall 2026",
        source_url="https://ca.indeed.com/viewjob?jk=abc123",
        baseline=parse_job_text("Backend Engineering Intern at Globex\nPython SQL"),
    )

    assert parsed is not None
    assert parsed.employer.company_name == "Globex"
    assert parsed.role.job_title == "Backend Engineering Intern"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.application.application_deadline == "2026-07-15"
    assert parsed.data_quality.extraction_confidence == 0.93
    args, kwargs = calls[0]
    assert args[:4] == ["codex", "-a", "never", "exec"]
    assert args[4:11] == [
        "--model",
        "gpt-6-luna",
        "--config",
        'model_reasoning_effort="max"',
        "--ignore-user-config",
        "--cd",
        str(workspace),
    ]
    assert "--sandbox" in args
    assert "read-only" in args
    assert args[-1] == "-"
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "Backend Engineering Intern at Globex" in kwargs["input"]
    assert "Fall 2026" in kwargs["input"]
    assert "academic terms" in kwargs["input"]
    assert "written-out co-op durations" in kwargs["input"]
    assert "capability_domains" in kwargs["input"]
    assert "cybersecurity" in kwargs["input"]
    assert "quality_assurance" in kwargs["input"]
    assert "product_management" in kwargs["input"]
    assert "business_analysis" in kwargs["input"]
    assert "title as a strong prior" in kwargs["input"]
    assert (
        "hardware, electrical, optical, lab/test, ECAD, CAD, circuit, and measurement tools"
        in kwargs["input"]
    )
    assert "Posting Close Date" in kwargs["input"]
    assert "application.application_deadline" in kwargs["input"]
    assert "Convert written-out durations to integer month counts" in kwargs["input"]
    assert "Preserve exact labeled dates" in kwargs["input"]
    assert "12-month term commencing on August 31, 2026" in kwargs["input"]
    assert "set raw_label to Fall <year>" in kwargs["input"]
    assert kwargs["timeout"] == 60


def test_codex_cli_posting_parser_normalizes_unambiguous_shape_drift():
    raw_text = (
        "Software Engineer Intern\n"
        "Company: Samsara\n"
        "Build backend services and real-time data systems."
    )
    baseline = parse_job_text(raw_text, "https://ca.indeed.com/viewjob?jk=abc123")
    payload = baseline.model_dump()
    role = payload["role"]
    role["title"] = role.pop("job_title")
    role.pop("normalized_title")
    role.pop("description_raw")
    role.pop("description_clean")
    role["capabilities"] = [
        {"name": "Backend development", "evidence": "Build backend services"},
        {"name": "Real-time data processing", "evidence": "real-time data systems"},
    ]
    payload["employment_timing"]["timing_type"] = None
    payload["compensation"]["pay_min"] = 61.9
    payload["compensation"]["pay_max"] = 61.9

    def fake_runner(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    parser = CodexCliPostingParser(command="codex", runner=fake_runner)

    parsed = parser.parse(
        raw_text,
        source_url="https://ca.indeed.com/viewjob?jk=abc123",
        baseline=baseline,
    )

    assert parsed is not None
    assert parsed.role.job_title == baseline.role.job_title
    assert parsed.role.normalized_title == baseline.role.normalized_title
    assert parsed.role.description_raw == raw_text
    assert "Build backend services" in parsed.role.description_clean
    assert parsed.role.capabilities == [
        "Backend development",
        "Real-time data processing",
    ]
    assert parsed.employment_timing.timing_type == baseline.employment_timing.timing_type
    assert parsed.compensation.pay_min == 61.9
    assert parsed.compensation.pay_max == 61.9


def test_codex_cli_posting_parser_normalizes_role_evidence_shape_drift():
    raw_text = (
        "Data Engineering Student\n"
        "Company: Example Energy\n"
        "Build data pipelines. Applicants must be enrolled as a student."
    )
    source_url = "https://example.com/jobs/data-engineering-student"
    baseline = parse_job_text(raw_text, source_url)
    payload = baseline.model_dump()
    payload["role"]["evidence"] = [
        {
            "kind": "qualification",
            "modality": "mandatory",
            "source_section": "qualifications",
            "exact_text": "Applicants must be enrolled as a student.",
            "evidence_text": "Applicants must be enrolled as a student.",
            "claims": [
                {
                    "claim_id": "claim-student",
                    "kind": "eligibility",
                    "modality": "mandatory",
                    "owner": "applicant",
                    "grounded_text": "Applicants must be enrolled as a student.",
                    "conditions": [
                        {
                            "text": "second through fourth year",
                            "condition_type": "applicant_subset",
                        }
                    ],
                    "coordination": {
                        "type": "range",
                        "items": ["second year", "third year", "fourth year"],
                    },
                    "decision_trace": "Explicit mandatory student requirement.",
                }
            ],
        }
    ]

    def fake_runner(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    parser = CodexCliPostingParser(command="codex", runner=fake_runner)
    parsed = parser.parse(raw_text, source_url=source_url, baseline=baseline)

    assert parsed is not None
    evidence = parsed.role.evidence[0]
    assert evidence.kind == "requirement"
    assert evidence.modality == "required"
    assert evidence.text == "Applicants must be enrolled as a student."
    assert evidence.claims[0].text == "Applicants must be enrolled as a student."
    assert evidence.claims[0].condition_text == "second through fourth year"
    assert evidence.claims[0].coordination == "single"
    assert evidence.claims[0].decision_trace == [
        "Explicit mandatory student requirement."
    ]


def test_codex_cli_posting_parser_restores_nonnullable_nulls_from_baseline():
    raw_text = (
        "Software Engineer Intern\n"
        "Company: Samsara\n"
        "Applicants must be enrolled in a co-op program."
    )
    source_url = "https://ca.indeed.com/viewjob?jk=abc123"
    baseline = parse_job_text(raw_text, source_url)
    payload = baseline.model_dump()
    payload["eligibility"]["current_enrollment_required"] = None
    payload["eligibility"]["co_op_enrollment_required"] = None
    payload["application"]["requires_manual_submission"] = None
    payload["application"]["autofill_supported"] = None
    payload["application"]["document_upload_supported"] = None
    payload["application"]["upload_targets"] = ["resume"]
    payload["location"]["remote_eligibility"] = False

    def fake_runner(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    parser = CodexCliPostingParser(command="codex", runner=fake_runner)
    parsed = parser.parse(raw_text, source_url=source_url, baseline=baseline)

    assert parsed is not None
    assert (
        parsed.eligibility.current_enrollment_required
        is baseline.eligibility.current_enrollment_required
    )
    assert (
        parsed.eligibility.co_op_enrollment_required
        is baseline.eligibility.co_op_enrollment_required
    )
    assert (
        parsed.application.requires_manual_submission
        is baseline.application.requires_manual_submission
    )
    assert parsed.application.autofill_supported is baseline.application.autofill_supported
    assert (
        parsed.application.document_upload_supported
        == baseline.application.document_upload_supported
    )
    assert parsed.application.upload_targets == []
    assert parsed.location.remote_eligibility == baseline.location.remote_eligibility


def test_parse_job_text_with_settings_uses_codex_when_enabled():
    codex_result = parse_job_text(
        "Backend Engineering Intern\nCompany: Globex\nLocation: Toronto, ON, Canada\nPython SQL",
        "https://ca.indeed.com/viewjob?jk=abc123",
    )
    calls = []

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            calls.append((raw_text, source_url, baseline.employer.company_name))
            return codex_result

    parsed = parse_job_text_with_settings(
        "Backend Engineering Intern\nPython SQL",
        "https://ca.indeed.com/viewjob?jk=abc123",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    expected = codex_result.model_copy(deep=True)
    expected.role.description_raw = "Backend Engineering Intern\nPython SQL"
    expected.role.description_clean = "Backend Engineering Intern\nPython SQL"
    expected.source.parser_provider = "codex_cli"
    expected.source.parser_outcome = "provider_success"
    expected.source.parser_error_code = None
    assert parsed == expected
    assert calls == [
        (
            "Backend Engineering Intern\nPython SQL",
            "https://ca.indeed.com/viewjob?jk=abc123",
            None,
        )
    ]


def test_parse_job_text_with_settings_cleans_stale_llm_missing_technical_stack():
    calls = []

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            calls.append(
                (
                    raw_text,
                    source_url,
                    list(baseline.role.tech_stack),
                    list(baseline.data_quality.missing_fields),
                )
            )
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope", "FPGA"]
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes and FPGA boards.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert calls == [
        (
            "Hardware Intern\nCompany: Acme\nUse oscilloscopes and FPGA boards.",
            "https://example.com/jobs/hardware-intern",
            ["FPGA", "Oscilloscope"],
            [],
        )
    ]
    assert parsed.role.tech_stack == ["Oscilloscope", "FPGA"]
    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_extracts_finance_capabilities_without_technical_stack_review():
    raw_text = """
    Financial Analyst Intern
    Company: Northwind Capital
    Location: Toronto, ON, Canada
    Responsibilities:
    Build financial models, perform variance analysis, prepare budget forecasts,
    and summarize findings for senior leadership.
    Qualifications:
    Advanced Excel skills and experience with financial statements.
    """

    parsed = parse_job_text(raw_text, "https://jobs.example.test/finance-intern")

    assert parsed.role.capability_domains == ["finance"]
    assert "Financial Modeling" in parsed.role.capabilities
    assert "Variance Analysis" in parsed.role.capabilities
    assert "Budget Forecasting" in parsed.role.capabilities
    assert "Excel" in parsed.role.capabilities
    assert parsed.role.tech_stack == []
    assert "role.tech_stack" not in parsed.data_quality.missing_fields
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    ("raw_text", "expected_domain", "expected_capability"),
    [
        (
            "Sales Development Intern\n"
            "Company: Northwind\n"
            "Use CRM tools for lead generation and pipeline management.",
            "sales",
            "Lead Generation",
        ),
        (
            "Marketing Intern\n"
            "Company: Northwind\n"
            "Plan social media campaigns and SEO content marketing.",
            "marketing",
            "Campaign Management",
        ),
        (
            "Operations Intern\n"
            "Company: Northwind\n"
            "Drive process improvement, scheduling, and vendor management.",
            "operations",
            "Process Improvement",
        ),
        (
            "Customer Success Intern\n"
            "Company: Northwind\n"
            "Use Zendesk ticketing to troubleshoot customer escalations.",
            "customer_support",
            "Ticketing",
        ),
    ],
)
def test_parse_job_text_extracts_domain_capabilities_for_non_technical_roles(
    raw_text,
    expected_domain,
    expected_capability,
):
    parsed = parse_job_text(raw_text, "https://jobs.example.test/domain-role")

    assert expected_domain in parsed.role.capability_domains
    assert expected_capability in parsed.role.capabilities
    assert parsed.role.tech_stack == []
    assert "role.tech_stack" not in parsed.data_quality.missing_fields
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_uses_capability_evidence_for_generic_title_domains():
    parsed = parse_job_text(
        "Summer Intern\n"
        "Company: Acme\n"
        "Use Salesforce to manage customer relationships and track outreach.",
        "https://jobs.example.test/generic-salesforce-intern",
    )

    assert "CRM" in parsed.role.capabilities
    assert "sales" in parsed.role.capability_domains
    assert parsed.role.tech_stack == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_title_prior_does_not_suppress_capability_evidence():
    parsed = parse_job_text(
        "Software Developer Intern\n"
        "Company: Acme\n"
        "Build Python services and use Salesforce to manage customer relationships.",
        "https://jobs.example.test/software-salesforce-intern",
    )

    assert "Python" in parsed.role.capabilities
    assert "CRM" in parsed.role.capabilities
    assert "software" in parsed.role.capability_domains
    assert "sales" in parsed.role.capability_domains
    assert parsed.role.tech_stack == ["Python"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_merges_codex_capability_domains_with_title_prior():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = ["CRM"]
            parsed.role.capability_domains = []
            parsed.role.tech_stack = []
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nCompany: Acme\nUse Salesforce to manage customer relationships.",
        "https://example.com/jobs/software-salesforce-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert "CRM" in parsed.role.capabilities
    assert "software" in parsed.role.capability_domains
    assert "sales" in parsed.role.capability_domains
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_derives_domains_when_codex_omits_domains():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = ["CRM"]
            parsed.role.capability_domains = []
            parsed.role.tech_stack = []
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Summer Intern\nCompany: Acme\nUse Salesforce to manage customer relationships.",
        "https://example.com/jobs/summer-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.capabilities == ["CRM"]
    assert "sales" in parsed.role.capability_domains
    assert "customer_support" in parsed.role.capability_domains
    assert parsed.role.tech_stack == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_preserves_heuristic_capabilities_when_llm_omits_fields():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = []
            parsed.role.capability_domains = []
            parsed.role.tech_stack = []
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Financial Analyst Intern\nCompany: Northwind Capital\nBuild financial models and budget forecasts in Excel.",
        "https://example.com/jobs/finance-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.capability_domains == ["finance"]
    assert "Financial Modeling" in parsed.role.capabilities
    assert "Budget Forecasting" in parsed.role.capabilities
    assert "Excel" in parsed.role.capabilities
    assert parsed.role.tech_stack == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_discards_ungrounded_provider_tech_stack():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = []
            parsed.role.tech_stack = ["Python", "Kubernetes"]
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nCompany: Acme\nBuild Python services.",
        "https://example.com/jobs/software-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]
    assert parsed.role.capability_domains == ["software"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_discards_ungrounded_provider_capability():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = ["Backend APIs"]
            parsed.role.capability_domains = []
            parsed.role.tech_stack = []
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nCompany: Acme\nBuild Python services.",
        "https://example.com/jobs/software-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.tech_stack == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.capability_domains == ["software"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_preserves_partial_baseline_tech_stack_when_codex_omits_one_tool():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = ["Python"]
            parsed.role.tech_stack = ["Python"]
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nCompany: Acme\nBuild Python and SQL services.",
        "https://example.com/jobs/software-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.tech_stack == ["Python", "SQL"]
    assert parsed.role.capabilities == ["Python", "SQL"]
    assert parsed.role.capability_domains == ["software", "data"]
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    "raw_text",
    [
        "Software Developer Intern\nCompany: Acme\nThis role reports to the engineering manager and builds APIs.",
        "Customer Support Intern\nCompany: Acme\nThis role reports to the support manager and handles Zendesk tickets.",
        "Project Coordinator Intern\nCompany: Acme\nNo direct reports; coordinates milestones and meetings.",
    ],
)
def test_parse_job_text_does_not_treat_reports_to_as_reporting_capability(raw_text):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/reports-to")

    assert "Reporting" not in parsed.role.capabilities
    assert "finance" not in parsed.role.capability_domains
    assert "data" not in parsed.role.capability_domains


@pytest.mark.parametrize(
    ("raw_text", "unexpected_capability", "unexpected_domain"),
    [
        (
            "Software Developer Intern\nCompany: Acme\nBuild Python job scheduling services and APIs.",
            "Scheduling",
            "operations",
        ),
        (
            "Software Developer Intern\nCompany: Acme\nBuild React forms in compliance with coding standards.",
            "Compliance",
            "legal_policy",
        ),
        (
            "Cybersecurity Intern\nCompany: Acme\nSupport phishing awareness campaigns and access controls.",
            "Campaign Management",
            "marketing",
        ),
    ],
)
def test_parse_job_text_avoids_broad_capability_false_positives(
    raw_text,
    unexpected_capability,
    unexpected_domain,
):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/pattern-false-positive")

    assert unexpected_capability not in parsed.role.capabilities
    assert unexpected_domain not in parsed.role.capability_domains


def test_parse_job_text_still_extracts_reporting_work():
    parsed = parse_job_text(
        "Business Analyst Intern\n"
        "Company: Acme\n"
        "Prepare monthly reports, summarize findings, and present business requirements.",
        "https://example.com/jobs/reporting-work",
    )

    assert "Reporting" in parsed.role.capabilities
    assert "business_analysis" in parsed.role.capability_domains


def test_parse_job_text_keeps_sql_data_role_out_of_software_domain():
    parsed = parse_job_text(
        "Data Analyst Intern\n"
        "Company: Acme\n"
        "Use SQL to build dashboards and reporting for business stakeholders.",
        "https://example.com/jobs/data-analyst-intern",
    )

    assert parsed.role.capability_domains == ["data", "general_business"]
    assert "SQL" in parsed.role.capabilities
    assert "Reporting" in parsed.role.capabilities
    assert parsed.role.tech_stack == ["SQL"]
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    ("raw_text", "expected_capability"),
    [
        (
            "Backend Developer Intern\nCompany: Acme\nBuild REST APIs and services with Python.",
            "Backend Development",
        ),
        (
            "Frontend Engineer Intern\nCompany: Acme\nBuild React UI components and user interfaces.",
            "Frontend Development",
        ),
        (
            "Full-Stack Developer Intern\nCompany: Acme\nBuild frontend UI and backend APIs.",
            "Full-Stack Development",
        ),
    ],
)
def test_parse_job_text_extracts_software_specializations(raw_text, expected_capability):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/software-specialization")

    assert parsed.role.capability_domains[0] == "software"
    assert expected_capability in parsed.role.capabilities
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    ("raw_text", "expected_domain", "expected_capabilities"),
    [
        (
            "Cybersecurity Analyst Intern\n"
            "Company: Acme\n"
            "Monitor SIEM alerts, support incident response, and assess vulnerabilities.",
            "cybersecurity",
            {"SIEM", "Incident Response", "Vulnerability Assessment"},
        ),
        (
            "Hardware Engineer Intern\n"
            "Company: Acme\n"
            "Design PCB prototypes, validate FPGA logic, and use oscilloscopes in the lab.",
            "hardware_engineering",
            {"PCB Design", "FPGA", "Oscilloscope"},
        ),
        (
            "Data Engineer Intern\n"
            "Company: Acme\n"
            "Build ETL data pipelines with Spark, Airflow, and SQL.",
            "data",
            {"Data Pipelines", "ETL", "Spark", "Airflow", "SQL"},
        ),
        (
            "Data Analyst Intern\n"
            "Company: Acme\n"
            "Create dashboards, reporting, SQL analysis, and Tableau data visualizations.",
            "data",
            {"Dashboarding", "Reporting", "SQL", "Tableau", "Data Visualization"},
        ),
        (
            "Data Science Intern\n"
            "Company: Acme\n"
            "Develop machine learning models, statistical analysis, and experiments in Python.",
            "data",
            {"Machine Learning", "Statistical Analysis", "Experimentation", "Python"},
        ),
        (
            "Product Manager Intern\n"
            "Company: Acme\n"
            "Own product roadmaps, write user stories, and prioritize feature requirements.",
            "product_management",
            {"Product Management", "Roadmapping", "User Stories", "Requirements Gathering"},
        ),
        (
            "Business Analyst Intern\n"
            "Company: Acme\n"
            "Gather business requirements, map processes, support UAT, and prepare reports.",
            "business_analysis",
            {"Business Analysis", "Requirements Gathering", "Process Mapping", "UAT"},
        ),
        (
            "QA Testing Intern\n"
            "Company: Acme\n"
            "Write test plans, regression tests, and Selenium test automation.",
            "quality_assurance",
            {
                "Quality Assurance",
                "Test Planning",
                "Regression Testing",
                "Test Automation",
                "Selenium",
            },
        ),
    ],
)
def test_parse_job_text_extracts_broader_role_domains(
    raw_text, expected_domain, expected_capabilities
):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/domain-coverage")

    assert expected_domain in parsed.role.capability_domains
    assert expected_capabilities.issubset(set(parsed.role.capabilities))
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    ("raw_text", "expected_domains"),
    [
        (
            "Data Science Intern\nCompany: Acme\nDevelop machine learning models in Python.",
            ["data", "software"],
        ),
        (
            "Software Engineer in Test Intern\nCompany: Acme\nBuild Selenium test automation.",
            ["quality_assurance", "software"],
        ),
        (
            "Customer Support Intern\nCompany: Acme\nTroubleshoot Python scripts and Zendesk tickets.",
            ["customer_support", "software"],
        ),
    ],
)
def test_parse_job_text_preserves_strong_title_domain_priority(raw_text, expected_domains):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/domain-priority")

    assert parsed.role.capability_domains[: len(expected_domains)] == expected_domains
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    "raw_text",
    [
        "Application Security Intern\nCompany: Acme\nReview secure coding standards and API authorization.",
        "Security Engineer Intern\nCompany: Acme\nReview secure coding standards and application controls.",
        "Cyber Intern\nCompany: Acme\nReview access controls and security policies.",
        "AppSec Intern\nCompany: Acme\nImprove authentication and authorization controls.",
    ],
)
def test_parse_job_text_extracts_common_cybersecurity_titles(raw_text):
    parsed = parse_job_text(raw_text, "https://example.com/jobs/cyber-role")

    assert parsed.role.capability_domains[0] == "cybersecurity"
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_does_not_add_domains_for_ungrounded_provider_capability():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.capabilities = []
            parsed.role.tech_stack = ["Python", "Optical Measurements"]
            parsed.data_quality.missing_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nCompany: Acme\nBuild Python services.",
        "https://example.com/jobs/software-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]
    assert parsed.role.capability_domains == ["software"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_cleans_stale_tech_stack_missing_for_non_technical_role():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Financial Analyst Intern\nCompany: Northwind Capital\nBuild financial models.",
        "https://example.com/jobs/finance-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.tech_stack == []
    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_uses_body_evidence_when_title_is_generic():
    raw_text = """
    Ciena Summer 2027 Intern
    Company: Ciena
    Location: Ottawa, ON, Canada
    You will automate lab tests with Python, validate optical measurements,
    and work with high-speed digital circuits and test equipment.
    """

    parsed = parse_job_text(raw_text, "https://jobs.example.test/ciena-intern")

    assert "software" in parsed.role.capability_domains
    assert "hardware_engineering" in parsed.role.capability_domains
    assert "Python" in parsed.role.capabilities
    assert "Optical Measurements" in parsed.role.capabilities
    assert "Test Equipment" in parsed.role.capabilities
    assert "Python" in parsed.role.tech_stack
    assert "Optical Measurements" in parsed.role.tech_stack
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_preserves_llm_manual_review_for_other_reasons():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope"]
            parsed.data_quality.missing_fields = []
            parsed.data_quality.ambiguous_fields = []
            parsed.data_quality.inferred_fields = ["employment_timing.start_date"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.tech_stack == ["Oscilloscope"]
    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_drops_stale_timing_inference_but_preserves_review():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope"]
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.inferred_fields = ["employment_timing.start_date"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.inferred_fields == []
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_preserves_review_when_recovered_tech_stack_has_low_confidence():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope"]
            parsed.data_quality.extraction_confidence = 0.4
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.extraction_confidence == 0.4
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_requires_review_when_missing_fields_remain_after_cleanup():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope"]
            parsed.data_quality.missing_fields = ["role.tech_stack", "role.job_title"]
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.data_quality.missing_fields == ["role.job_title"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_requires_review_when_ambiguous_fields_remain_after_cleanup():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.tech_stack = ["Oscilloscope"]
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.ambiguous_fields = ["employer.company_name"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Hardware Intern\nCompany: Acme\nUse oscilloscopes.",
        "https://example.com/jobs/hardware-intern",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.ambiguous_fields == ["employer.company_name"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_preserves_deterministic_timing_ambiguities():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.data_quality.ambiguous_fields = []
            parsed.data_quality.needs_manual_review = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "Start Date of Internship: January 4, 2027\n"
        "Duration: 4 months",
        "https://jobs.example.test/provider-timing-conflict",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.data_quality.ambiguous_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_keeps_title_term_over_provider_body_timing():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September to December 2026"
            parsed.employment_timing.start_date = "2026-09-01"
            parsed.employment_timing.end_date = "2026-12-31"
            parsed.employment_timing.duration_months = 4
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Intern\n"
        "Start Date: September 2026\n"
        "End Date: December 2026\n"
        "Duration: 4 months",
        "https://jobs.example.test/provider-stale-body-term",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
        title_hint="Software Intern | January - April 2027",
    )

    assert parsed.employment_timing.raw_label == "January - April 2027"
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_clears_stale_tech_stack_review_before_posted_date_fallback():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = None
            parsed.employment_timing.timing_type = "unknown"
            parsed.employment_timing.start_date = None
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            parsed.role.tech_stack = ["Python"]
            parsed.data_quality.missing_fields = ["role.tech_stack"]
            parsed.data_quality.inferred_fields = []
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Engineer Co-op\n"
        "Company: ExampleCo\n"
        "Python backend role. Resolution Evidence: official posting has date posted 2026-06-19.",
        "https://example.test/jobs/posted-date-stale-review",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-06-19"
    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.inferred_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_applies_posted_date_fallback_to_llm_result_without_timing():
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = None
            parsed.employment_timing.timing_type = "unknown"
            parsed.employment_timing.start_date = None
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            parsed.data_quality.inferred_fields = []
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Engineer Co-op\n"
        "Company: ExampleCo\n"
        "Resolution Evidence: official posting has date posted 2026-06-19.",
        "https://example.test/jobs/posted-date-llm",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Posted 2026-06-19"
    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-06-19"
    assert parsed.data_quality.inferred_fields == ["employment_timing.start_date"]


def test_parse_job_text_with_settings_falls_back_to_heuristic_when_codex_fails():
    class FailingParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            return None

    parsed = parse_job_text_with_settings(
        "Backend Developer\nCompany: Acme\nPython SQL",
        None,
        Settings(posting_parser_provider="codex_cli"),
        parser=FailingParser(),
    )

    assert parsed.employer.company_name == "Acme"
    assert parsed.role.tech_stack == ["Python", "SQL"]


def test_parse_job_text_extracts_hardware_stack_and_reports_ambiguous_duration():
    raw_text = r"""
    Fall 2026 Optical Modem Hardware Intern (8-12 months)
    Company: Ciena
    Location: Ottawa
    How You Will Contribute:
    You will join a team of hardware designers focusing on electro-optical circuit boards.
    You will utilize a lab capable of electrical and optical measurements from DC to 100s Ghz.
    You will review and test High-Speed, Digital, Analog control, and DC\DC Power Circuits.
    You will design test jig circuits using the Cadence Allegro ECAD Tool Suite.
    Assets:
    Experience with hands on test and measurement equipment, automation scripts and test equipment.
    Coordinate with Signal Integrity, Mechanical, Thermal, Component, Optical, and Test teams.
    Knowledge of high-speed design and transmission line theory.
    Knowledge of basic Op-Amp and Power circuits.
    Knowledge of precision analog design.
    """

    parsed = parse_job_text(
        raw_text, "https://ciena.wd5.myworkdayjobs.com/Careers/job/Ottawa/R031214"
    )

    assert "Cadence Allegro" in parsed.role.tech_stack
    assert "ECAD" in parsed.role.tech_stack
    assert "Signal Integrity" in parsed.role.tech_stack
    assert "DC/DC Power Circuits" in parsed.role.tech_stack
    assert "Op-Amp" in parsed.role.tech_stack
    assert "Transmission Line Theory" in parsed.role.tech_stack
    assert parsed.data_quality.missing_fields == []
    assert parsed.data_quality.ambiguous_fields == ["employment_timing.duration_months"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_uses_source_evidence_when_aggregator_page_is_blocked():
    raw_text = (
        "Page Title: Blocked - Indeed.com\n"
        "Source URL: https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926\n"
        "Source Evidence: Software Development Intern Software Development Intern "
        "\u00b7 Birdseye Solutions \u00b7 Mississauga, ON, CA Software Development Intern "
        "Company: Birdseye Solutions Location: Mississauga, Ontario, Canada "
        "Email Row: Software Development Intern \u00b7 Birdseye Solutions \u00b7 Mississauga, ON, CA\n"
        "Request Blocked\n"
        "You have been blocked.\n"
        "Location: Mississauga, Ontario, Canada\n"
        "Indeed Source URL: https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926"
    )

    parsed = parse_job_text(raw_text, "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926")

    assert parsed.employer.company_name == "Birdseye Solutions"
    assert parsed.role.job_title == "Software Development Intern"
    assert parsed.role.normalized_title == "software_development_intern"
    assert parsed.role.role_type == "internship"
    assert parsed.location.city == "Mississauga"


def test_parse_job_text_extracts_indeed_browser_capture_company_below_title():
    raw_text = (
        "Page Title: Data Analyst Co-Op/Intern (12-16 months) - Oakville, ON L6L 0C4 - Indeed.com\n"
        "Source URL: https://ca.indeed.com/viewjob?jk=cf7e2a3f711a5da7\n"
        "Skip to main content\n"
        "Home\n"
        "Company reviews\n"
        "Salary guide\n"
        "Start of main content\n"
        "What\n"
        "Where\n"
        "Find Jobs\n"
        "This job has expired on Indeed\n"
        "Reasons could include: the employer is not accepting applications, is not actively hiring, or is reviewing applications\n"
        "Data Analyst Co-Op/Intern (12-16 months)\n"
        "Javelin Technologies Inc.\n"
        "3.6\n"
        "3.6 out of 5 stars\n"
        "3457 Superior Crt Unit 1, Oakville, ON L6L 0C4\n"
        "$21 an hour - Permanent\n"
        "Full job description\n"
        "PROFILE\n"
        "We are seeking a Data Analyst Intern to support a major enterprise data transformation initiative.\n"
    )

    parsed = parse_job_text(raw_text, "https://ca.indeed.com/viewjob?jk=cf7e2a3f711a5da7")

    assert parsed.employer.company_name == "Javelin Technologies Inc."
    assert parsed.role.job_title == "Data Analyst Co-Op/Intern (12-16 months)"
    assert parsed.location.city == "Oakville"


def test_browser_capture_adapter_extracts_indeed_company_below_title():
    raw_text = (
        "Page Title: Data Analyst Co-Op/Intern (12-16 months) - Oakville, ON L6L 0C4 - Indeed.com\n"
        "Source URL: https://ca.indeed.com/viewjob?jk=cf7e2a3f711a5da7\n"
        "Data Analyst Co-Op/Intern (12-16 months)\n"
        "Javelin Technologies Inc.\n"
        "3.6\n"
        "3.6 out of 5 stars\n"
        "3457 Superior Crt Unit 1, Oakville, ON L6L 0C4\n"
        "$21 an hour - Permanent"
    )
    context = BrowserCaptureContext(
        raw_text=raw_text,
        lines=[line.strip() for line in raw_text.splitlines() if line.strip()],
        source_url="https://ca.indeed.com/viewjob?jk=cf7e2a3f711a5da7",
        source_type="browser_capture",
    )

    fields = extract_browser_capture_fields(context)

    assert fields.title == "Data Analyst Co-Op/Intern (12-16 months)"
    assert fields.company == "Javelin Technologies Inc."
    assert fields.location_label == "Oakville, ON L6L 0C4"


def test_browser_capture_adapter_ignores_blocked_indeed_page():
    raw_text = (
        "Page Title: Blocked - Indeed.com\n"
        "Source URL: https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926\n"
        "Request Blocked\n"
        "You have been blocked."
    )
    context = BrowserCaptureContext(
        raw_text=raw_text,
        lines=[line.strip() for line in raw_text.splitlines() if line.strip()],
        source_url="https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
        source_type="browser_capture",
    )

    fields = extract_browser_capture_fields(context)

    assert fields.title is None
    assert fields.company is None
    assert fields.location_label is None


def test_browser_capture_adapter_ignores_unmatched_source():
    raw_text = (
        "Page Title: Backend Intern | Example Jobs\n"
        "Source URL: https://jobs.example.com/backend-intern\n"
        "Backend Intern\n"
        "Example Co"
    )
    context = BrowserCaptureContext(
        raw_text=raw_text,
        lines=[line.strip() for line in raw_text.splitlines() if line.strip()],
        source_url="https://jobs.example.com/backend-intern",
        source_type="browser_capture",
    )

    fields = extract_browser_capture_fields(context)

    assert fields.title is None
    assert fields.company is None
    assert fields.location_label is None


def test_parse_job_text_uses_linkedin_page_title_before_noisy_source_evidence():
    raw_text = (
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425694183\n"
        "Source Evidence: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | "
        "LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, "
        "Canada | LinkedIn Company: LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - "
        "Fall 2026 in Ottawa, Ontario, Canada Location: Ottawa, Ontario, Canada TrendAI hiring "
        "Software Developer Co-Op - Fall 2026 in Ottawa\n"
        "Skip to main content\n"
        "LinkedIn\n"
        "Software Developer Co-Op - Fall 2026\n"
        "TrendAI  Ottawa, Ontario, Canada\n"
        "Python Docker Java"
    )

    parsed = parse_job_text(raw_text, "https://www.linkedin.com/jobs/view/4425694183")

    assert parsed.employer.company_name == "TrendAI"
    assert parsed.role.job_title == "Software Developer Co-Op - Fall 2026"
    assert parsed.role.normalized_title == "software_developer_co_op_fall_2026"
    assert parsed.location.city == "Ottawa"
    assert parsed.location.province_state == "Ontario"
    assert parsed.location.country == "Canada"


def test_parse_job_text_trims_linkedin_recommendation_tail_from_description():
    raw_text = (
        "Page Title: Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425912315/\n"
        "Title: Intern, Digital Ship NLP (8 Months)\n"
        "Company: Seaspan ULC\n"
        "Location: North Vancouver, British Columbia, Canada\n"
        "Job Description\n"
        "Build NLP data pipelines for shipyard operations.\n"
        "Start Date of Internship: Tuesday, September 8th, 2026.\n"
        "End Date of Internship: Friday, April 30th, 2027 (8-Months).\n"
        "About the company Seaspan ULC 78,010 followers\n"
        "Interested in working with us in the future? Members who share that they are interested\n"
        "More jobs SR .NET Back End Developer Pacific Programming & Tech Inc.\n"
        "LinkedIn Corporation \u00a9 2026\n"
    )

    parsed = parse_job_text(raw_text, "https://www.linkedin.com/jobs/view/4425912315/")

    assert parsed.employer.company_name == "Seaspan ULC"
    assert parsed.role.job_title == "Intern, Digital Ship NLP (8 Months)"
    assert "Build NLP data pipelines" in parsed.role.description_clean
    assert "End Date of Internship" in parsed.role.description_clean
    assert "About the company" not in parsed.role.description_clean
    assert "Interested in working with us" not in parsed.role.description_clean
    assert "More jobs" not in parsed.role.description_clean


def test_parse_job_text_strips_nested_title_label_from_source_evidence():
    raw_text = (
        "Page Title: Software Developer Co-Op - Fall 2026 | TrendAI | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425694183/\n"
        "Source Evidence: Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "TrendAI\n"
        "Software Developer Co-Op - Fall 2026"
    )

    parsed = parse_job_text(raw_text, "https://www.linkedin.com/jobs/view/4425694183/")

    assert parsed.role.job_title == "Software Developer Co-Op - Fall 2026"
    assert parsed.employer.company_name == "TrendAI"


def test_parse_job_text_preserves_linkedin_title_containing_in():
    raw_text = (
        "Page Title: Acme hiring Software Engineer in Test in Toronto, Ontario, Canada | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/123\n"
        "Python Playwright"
    )

    parsed = parse_job_text(raw_text, "https://www.linkedin.com/jobs/view/123")

    assert parsed.employer.company_name == "Acme"
    assert parsed.role.job_title == "Software Engineer in Test"
    assert parsed.location.city == "Toronto"
    assert parsed.location.province_state == "Ontario"
    assert parsed.location.country == "Canada"


def test_parse_job_text_extracts_academic_term_timing_from_title():
    parsed = parse_job_text(
        "Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "Python Docker Java",
        "https://www.linkedin.com/jobs/view/4425694183",
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.employment_timing.schedule_type == "full_time"


def test_parse_job_text_extracts_duration_from_coop_details():
    parsed = parse_job_text(
        "Python Developer Intern\n"
        "Company: ExampleCo\n"
        "Details: Intern - 8mo - Winter 2027\n"
        "Python SQL",
        "https://example.test/jobs/123",
    )

    assert parsed.employment_timing.raw_label == "Winter 2027"
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-08-31"
    assert parsed.employment_timing.duration_months == 8


def test_parse_job_text_extracts_explicit_internship_date_range_as_fall_term():
    parsed = parse_job_text(
        "Intern, Digital Ship NLP (8 Months)\n"
        "Company: Seaspan ULC\n"
        "We require a full-time commitment Monday-Friday, 40 hours per week.\n"
        "Start Date of Internship: Tuesday, September 8th, 2026.\n"
        "End Date of Internship: Friday, April 30th, 2027 (8-Months).",
        "https://www.linkedin.com/jobs/view/4425912315/",
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-08"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.employment_timing.duration_months == 8
    assert parsed.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    ("timing_text", "expected_raw_label"),
    [
        (
            "Note: This is a 12-month term commencing on August 31, 2026",
            "12-month term commencing on August 31, 2026",
        ),
        (
            "Note: This 12 month term starts on August 31, 2026",
            "12 month term starts on August 31, 2026",
        ),
        (
            "Note: This 12-month work term started at August 31, 2026",
            "12-month work term started at August 31, 2026",
        ),
        (
            "Note: The term begins August 31, 2026 and runs for a 12-month period",
            "The term begins August 31, 2026 and runs for a 12-month period",
        ),
        (
            "Note: From August 31, 2026, this is a 12-month co-op term",
            "From August 31, 2026, this is a 12-month co-op term",
        ),
        (
            "Expected Start Date: August 31, 2026\nDuration: 12 month co-op term",
            "12 month co-op term",
        ),
    ],
)
def test_parse_job_text_extracts_start_date_from_duration_term_timing_signals(
    timing_text: str,
    expected_raw_label: str,
):
    parsed = parse_job_text(
        "Student WestJetter- Data Science- 12 month term\n"
        "Company: WestJet\n"
        "Location: Calgary AB\n"
        "Posting Opening Date: June 23, 2026\n"
        "Posting Close Date: July 7, 2026\n"
        f"{timing_text}\n"
        "Python SQL machine learning",
        "https://ca.indeed.com/viewjob?jk=e228efbdd8873209",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.raw_label == expected_raw_label
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-08-31"
    assert parsed.employment_timing.end_date == "2027-07-31"
    assert parsed.employment_timing.duration_months == 12
    assert parsed.employment_timing.schedule_type == "full_time"
    assert parsed.application.application_deadline == "2026-07-07"


def test_parse_job_text_extracts_concise_labeled_contract_length() -> None:
    parsed = parse_job_text(
        "Full Stack Developer Intern - Fall 2026\n"
        "Location: Vancouver, B.C. Full-time, 40 hrs/week · "
        "Contract length: ~4 months Start date: September 3rd, 2026 "
        "Application deadline: July 23, 2026 Salary range: $54,000-$62,000."
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.start_date == "2026-09-03"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    ("date_text", "expected_start", "expected_end", "expected_duration"),
    [
        (
            "Start date: June 3, 2027\nDuration: 3 months",
            "2027-06-03",
            "2027-08-31",
            3,
        ),
        ("Start date: June 3, 2027", "2027-06-03", None, None),
        ("End date: August 18, 2027", "2027-05-01", "2027-08-18", 4),
    ],
)
def test_parse_job_text_keeps_exact_body_dates_within_title_term(
    date_text: str,
    expected_start: str,
    expected_end: str | None,
    expected_duration: int | None,
) -> None:
    parsed = parse_job_text(f"Software Intern - Summer 2027\n{date_text}")

    assert parsed.employment_timing.raw_label == "Summer 2027"
    assert parsed.employment_timing.start_date == expected_start
    assert parsed.employment_timing.end_date == expected_end
    assert parsed.employment_timing.duration_months == expected_duration
    assert "employment_timing.start_date" not in parsed.data_quality.ambiguous_fields
    assert "employment_timing.end_date" not in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_uses_duration_term_from_title_without_start_date():
    parsed = parse_job_text(
        "Co-op/intern - Environmental Engineer (8 Month Term)\n"
        "Company: Gerdau\n"
        "Posted: 2026-07-14\n"
        "LOCATION: Posted on Jul 14, 2026 Selkirk, Manitoba, Canada "
        "AREA: G.Start Internships Join us in shaping the future. "
        + "Our environmental internship program supports students. " * 8
        + "REQUIREMENTS: Have completed at least two years of a degree.",
        "https://careers.example.test/environmental-engineer",
        source_type="ats_hanzilla",
    )

    assert parsed.employment_timing.raw_label == "8 Month Term"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months == 8
    assert parsed.employment_timing.schedule_type is None


def test_parse_job_text_uses_duration_position_with_prose_month_start() -> None:
    result = parse_job_text(
        """
        Software Developer Intern
        Posted: 2026-06-09
        Term Duration This is a full-time, 4-month position, starting September 2026.
        """
    )

    assert result.employment_timing.raw_label == "4-month position"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date == "2026-12-31"
    assert result.employment_timing.duration_months == 4
    assert result.employment_timing.schedule_type == "full_time"


def test_parse_job_text_preserves_part_time_duration_schedule() -> None:
    result = parse_job_text(
        """
        Software Developer Intern
        This is a part-time 6 month internship.
        """
    )

    assert result.employment_timing.raw_label == "6 month internship"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.start_date is None
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months == 6
    assert result.employment_timing.schedule_type == "part_time"


def test_parse_job_text_ignores_negated_part_time_schedule() -> None:
    result = parse_job_text(
        """
        Software Developer Intern
        This is a full-time 6 month internship; part-time arrangements are not available.
        """
    )

    assert result.employment_timing.raw_label == "6 month internship"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.duration_months == 6
    assert result.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    "schedule_text",
    [
        "This 6 month internship is not full-time.",
        "This is not a full-time 6 month internship.",
        "This 6 month internship does not offer full-time arrangements.",
        "This 6 month internship is not available as a full-time position.",
        "This 6 month internship cannot be worked full-time.",
    ],
)
def test_parse_job_text_does_not_restore_negated_full_time_schedule(schedule_text: str) -> None:
    result = parse_job_text(f"Software Developer Intern\n{schedule_text}")

    assert result.employment_timing.raw_label == "6 month internship"
    assert result.employment_timing.duration_months == 6
    assert result.employment_timing.schedule_type is None


def test_parse_job_text_uses_weekly_hours_for_duration_schedule() -> None:
    result = parse_job_text(
        "Marketing Co-op\nAble to work 40 hours per week and for a 12-month term."
    )

    assert result.employment_timing.raw_label == "12-month term"
    assert result.employment_timing.duration_months == 12
    assert result.employment_timing.schedule_type == "full_time"


def test_parse_job_text_combines_alternative_duration_with_explicit_start() -> None:
    result = parse_job_text(
        """
        Co-op/Intern Software Developer
        Posted: 2026-06-22
        Term Duration This is a full-time, 8, or 12-month position, starting September, 2026.
        """
    )

    assert result.employment_timing.raw_label == "Fall 2026"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == "full_time"


def test_parse_job_text_preserves_repeated_unit_alternative_duration_with_start_date() -> None:
    result = parse_job_text(
        "Commercial Finance Intern (4 or 8 Month Term)\n"
        "ContractTypeAndDuration Full Time, Student Worker. "
        "Term Length: Students may choose either a 4-month or 8-month co-op placement. "
        "Start Date: September 7, 2026."
    )

    assert result.employment_timing.raw_label == "4-month or 8-month co-op placement"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.start_date == "2026-09-07"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    "duration_text",
    [
        "4-month or 8-month term",
        "4 or 8 month term",
    ],
)
def test_parse_job_text_preserves_alternative_duration_with_verbose_start_label(
    duration_text: str,
) -> None:
    result = parse_job_text(
        f"Software Developer Intern ({duration_text})\nStart Date of Internship: September 7, 2026."
    )

    assert result.employment_timing.raw_label.casefold() == duration_text.casefold()
    assert result.employment_timing.start_date == "2026-09-07"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None


def test_parse_job_text_merges_nonadjacent_verbose_start_into_alternative_duration() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Start Date of Internship: September 1, 2026\n"
        "Location: Toronto\n"
        "Term Length: 4 or 8 months\n"
        "This is a full-time role."
    )

    assert result.employment_timing.raw_label == "4 or 8 months"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    "raw_text",
    [
        (
            "Software Developer Intern\n"
            "Start Date: September 1, 2026\n"
            "Location: Toronto\n"
            "Term Length: 4 or 8 months\n"
            "Employment Type: Full-time"
        ),
        (
            "Software Developer Intern\n"
            "Term Length: 4 or 8 months\n"
            "Student Status: Part-time\n"
            "Start Date: September 1, 2026\n"
            "Employment Type: Full-time"
        ),
        (
            "Software Developer Intern\n"
            "Student Status: Part-time Start Date: September 1, 2026 "
            "Location: Toronto Term Length: 4 or 8 months "
            "Employment Type: Full-time"
        ),
    ],
)
def test_parse_job_text_preserves_timing_across_field_layouts(raw_text: str) -> None:
    result = parse_job_text(raw_text)

    assert result.employment_timing.raw_label == "4 or 8 months"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == "full_time"


def test_parse_job_text_uses_employment_dates_not_adjacent_distractor_dates() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Posting Start Date: July 1, 2026\n"
        "Start Date: September 1, 2026\n"
        "Application End Date: October 30, 2026\n"
        "Term Length: 4 or 8 months"
    )

    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None


@pytest.mark.parametrize(
    ("field_text", "expected_schedule"),
    [
        ("Student Status: Part-time Position Type: Internship", "full_time"),
        ("Position Type: Internship Student Status: Part-time", "full_time"),
        ("Education Type: Full-time Position Type: Part-time", "part_time"),
        ("Position Type: Part-time Education Type: Full-time", "part_time"),
    ],
)
def test_parse_job_text_isolates_schedule_evidence_at_flattened_field_boundaries(
    field_text: str,
    expected_schedule: str,
) -> None:
    result = parse_job_text(f"Software Developer Intern - Fall 2026\n{field_text}")

    assert result.employment_timing.schedule_type == expected_schedule
    assert "employment_timing.schedule_type" not in result.data_quality.ambiguous_fields


@pytest.mark.parametrize(
    ("title", "duration_text", "expected_start"),
    [
        (
            "Software Developer Intern - Fall 2026",
            "This is a 4 or 8 month internship.",
            "2026-09-01",
        ),
        (
            "Finance Co-op - Winter 2027",
            "Students may choose either a 4-month or 8-month co-op placement.",
            "2027-01-01",
        ),
        (
            "Data Analyst Intern - Summer 2027",
            "This is a full-time, 8 or 12 month position.",
            "2027-05-01",
        ),
    ],
)
def test_parse_job_text_preserves_alternative_duration_with_academic_term_start(
    title: str,
    duration_text: str,
    expected_start: str,
) -> None:
    result = parse_job_text(f"{title}\n{duration_text}")

    assert result.employment_timing.raw_label == title.rsplit(" - ", 1)[-1]
    assert result.employment_timing.start_date == expected_start
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    ("schedule_text", "expected_schedule"),
    [
        ("This 4 or 8 month internship is not full-time.", None),
        ("This 4 or 8 month internship is not full time.", None),
        ("This 4 or 8 month internship is not part-time.", "full_time"),
        (
            "The role is not a full-time position. This is a 4 or 8 month internship.",
            None,
        ),
        (
            "Candidates need not have full-time work experience. "
            "This is a 4 or 8 month internship.",
            "full_time",
        ),
        ("This is a part-time, 4 or 8 month internship.", "part_time"),
    ],
)
def test_parse_job_text_distinguishes_negative_schedule_evidence_for_academic_terms(
    schedule_text: str,
    expected_schedule: str | None,
) -> None:
    result = parse_job_text(f"Software Developer Intern - Fall 2026\n{schedule_text}")

    assert result.employment_timing.raw_label == "Fall 2026"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type == expected_schedule


@pytest.mark.parametrize(
    ("schedule_text", "expected_schedule"),
    [
        ("This internship is not full-time.", None),
        ("This internship is not full time.", None),
        ("This internship is part-time.", "part_time"),
        ("This internship is not part-time.", "full_time"),
        (
            "Candidates must remain enrolled in full-time education.",
            "full_time",
        ),
    ],
)
def test_parse_job_text_resolves_schedule_evidence_for_plain_academic_terms(
    schedule_text: str,
    expected_schedule: str | None,
) -> None:
    result = parse_job_text(f"Software Developer Intern - Fall 2026\n{schedule_text}")

    assert result.employment_timing.raw_label == "Fall 2026"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.schedule_type == expected_schedule


@pytest.mark.parametrize(
    "schedule_text",
    [
        "This 4 or 8 month internship may be full-time or part-time.",
        "Employment Type: Full-time. Schedule Type: Part-time.",
        "Schedule Type: Part-time. Employment Type: Full-time.",
        "Job Type: Full-time or Part-time.",
        "Schedule Type: Part-time / Full-time.",
        "This internship may be full-time or part-time.",
        "Employment Type: Full-time. The role will be part-time.",
    ],
)
def test_parse_job_text_preserves_ambiguous_academic_schedule(
    schedule_text: str,
) -> None:
    result = parse_job_text(f"Software Developer Intern - Fall 2026\n{schedule_text}")

    assert result.employment_timing.schedule_type is None
    assert "employment_timing.schedule_type" in result.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is True


def test_parse_job_text_preserves_ambiguous_schedule_without_academic_term() -> None:
    result = parse_job_text("Marketing Co-op\nThis 4-month term can be full-time or part-time.")

    assert result.employment_timing.schedule_type is None
    assert "employment_timing.schedule_type" in result.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is True


@pytest.mark.parametrize(
    ("description", "expected_label"),
    [
        (
            "This is a 4 or 8 month program with training and project support "
            "throughout the internship.",
            "4 or 8 month program",
        ),
        (
            "Students may choose a 4 or 8 month paid internship with mentorship.",
            "4 or 8 month paid internship",
        ),
        (
            "This is a 4 or 8 month co-op work term with project rotations.",
            "4 or 8 month co-op work term",
        ),
    ],
)
def test_parse_job_text_keeps_alternative_duration_label_concise(
    description: str,
    expected_label: str,
) -> None:
    result = parse_job_text(f"Software Developer Intern\n{description}")

    assert result.employment_timing.raw_label == expected_label
    assert result.employment_timing.duration_months is None


def test_parse_job_text_does_not_restore_negated_schedule_for_alternative_duration() -> None:
    result = parse_job_text(
        "Software Developer Intern\nThis 4-month or 8-month internship is not full-time."
    )

    assert result.employment_timing.raw_label == "4-month or 8-month internship"
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type is None


@pytest.mark.parametrize(
    ("schedule_text", "expected_schedule"),
    [
        ("Candidates must remain enrolled in full-time education.", None),
        ("Candidates need one year of full-time work experience.", None),
        ("Candidates must have previously worked full-time for one year.", None),
        ("Candidates previously held a full-time position.", None),
        (
            "Job Type: Part-time. Candidates must remain enrolled in full-time education.",
            "part_time",
        ),
        (
            "Job Type: Part-time. Candidates need one year of full-time work experience.",
            "part_time",
        ),
        (
            "Candidates must be available to work full-time during the term.",
            "full_time",
        ),
        (
            "Employment Type: Part-time. Candidates must be available throughout the term.",
            "part_time",
        ),
        (
            "The role is full-time.",
            "full_time",
        ),
    ],
)
def test_parse_job_text_uses_positive_work_evidence_for_alternative_duration_schedule(
    schedule_text: str,
    expected_schedule: str | None,
) -> None:
    result = parse_job_text(
        f"Software Developer Intern\nThis is a 4-month or 8-month internship.\n{schedule_text}"
    )

    assert result.employment_timing.raw_label == "4-month or 8-month internship"
    assert result.employment_timing.schedule_type == expected_schedule


@pytest.mark.parametrize(
    ("schedule_text", "expected_schedule"),
    [
        ("Full-time, 37.5 hours per week", "full_time"),
        ("Able to work 37.5 hours per week", "full_time"),
        ("Full-time in the U.S.", "full_time"),
        ("Part-time in B.C.", "part_time"),
        ("Full-time, 40 hrs/wk.", "full_time"),
        ("Full-time, 40 h/wk.", "full_time"),
        ("Full-time, 40 hrs/wks.", "full_time"),
        ("Full-time, 40 wk.", "full_time"),
        ("Part-time, 20 hrs/wk.", "part_time"),
    ],
)
def test_parse_job_text_preserves_schedule_across_non_clause_periods(
    schedule_text: str,
    expected_schedule: str,
) -> None:
    result = parse_job_text(f"Marketing Co-op\n{schedule_text} for a 4-month term.")

    assert result.employment_timing.raw_label == "4-month term"
    assert result.employment_timing.schedule_type == expected_schedule


def test_parse_job_text_ends_schedule_clause_after_weekly_unit_before_new_sentence() -> None:
    result = parse_job_text(
        "Marketing Co-op\nFull-time, 40 hrs/wk. This is a part-time, 4-month term."
    )

    assert result.employment_timing.raw_label == "4-month term"
    assert result.employment_timing.schedule_type == "part_time"


@pytest.mark.parametrize(
    "first_sentence",
    [
        "Full-time, 40 hrs.",
        "Full-time in the U.S.",
        "Full-time in B.C.",
    ],
)
def test_parse_job_text_ends_schedule_clause_after_abbreviation_before_new_sentence(
    first_sentence: str,
) -> None:
    result = parse_job_text(f"Marketing Co-op\n{first_sentence} This is a part-time, 4-month term.")

    assert result.employment_timing.raw_label == "4-month term"
    assert result.employment_timing.schedule_type == "part_time"


def test_parse_job_text_prefers_explicit_end_date_over_alternative_duration() -> None:
    result = parse_job_text(
        "Software Developer Intern (4 or 8 Month Term)\n"
        "Start Date: September 7, 2026. End Date: April 30, 2027."
    )

    assert result.employment_timing.start_date == "2026-09-07"
    assert result.employment_timing.end_date == "2027-04-30"
    assert result.employment_timing.duration_months == 8


def test_parse_job_text_resolves_alternative_term_independently_of_field_order() -> None:
    fields = (
        "Start Date: September 7, 2026",
        "End Date: April 30, 2027",
        "Term Length: 4 or 8 months",
        "Location: Example City",
    )

    for separator in ("\n", " "):
        for ordered_fields in permutations(fields):
            result = parse_job_text("Software Developer Intern\n" + separator.join(ordered_fields))

            context = (separator, ordered_fields)
            assert result.employment_timing.raw_label == "4 or 8 months", context
            assert result.employment_timing.start_date == "2026-09-07", context
            assert result.employment_timing.end_date == "2027-04-30", context
            assert result.employment_timing.duration_months == 8, context


def test_parse_job_text_resolves_exact_term_schedule_independently_of_field_order() -> None:
    fields = (
        "Expected Start Date: August 31, 2026",
        "Duration: 12 month co-op term",
        "Employment Type: Part-time",
        "Location: Example City",
    )

    for separator in ("\n", " "):
        for ordered_fields in permutations(fields):
            result = parse_job_text("Data Science Intern\n" + separator.join(ordered_fields))

            context = (separator, ordered_fields)
            assert result.employment_timing.start_date == "2026-08-31", context
            assert result.employment_timing.end_date == "2027-07-31", context
            assert result.employment_timing.duration_months == 12, context
            assert result.employment_timing.schedule_type == "part_time", context
            assert "employment_timing.schedule_type" not in result.data_quality.ambiguous_fields, (
                context
            )


def test_parse_job_text_does_not_use_experience_duration_as_term_duration() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "Requirements: At least 6 months experience is required."
    )

    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None


def test_parse_job_text_uses_compatible_negative_and_positive_schedule_evidence() -> None:
    result = parse_job_text(
        "Software Developer Intern - Fall 2026\n"
        "This internship is not full-time. The role is part-time."
    )

    assert result.employment_timing.schedule_type == "part_time"
    assert "employment_timing.schedule_type" not in result.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is False


def test_parse_job_text_preserves_abbreviated_month_in_start_date_field() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Start Date: Sept. 1, 2026\n"
        "Build and maintain software services."
    )

    assert result.employment_timing.raw_label == "Fall 2026"
    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None


def test_parse_job_text_marks_conflicting_duration_fields_ambiguous() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "Duration: 4 months\n"
        "Term Length: 8 months"
    )

    assert result.employment_timing.start_date == "2026-09-01"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert "employment_timing.duration_months" in result.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is True


def test_parse_job_text_marks_conflicting_explicit_start_dates_ambiguous() -> None:
    result = parse_job_text(
        "Software Developer Intern\n"
        "Expected Start Date: September 1, 2026\n"
        "Desired Start Date: October 1, 2026\n"
        "Duration: 4 month internship"
    )

    assert result.employment_timing.start_date is None
    assert result.employment_timing.end_date is None
    assert "employment_timing.start_date" in result.data_quality.ambiguous_fields
    assert result.data_quality.needs_manual_review is True


@pytest.mark.parametrize("label", ["Application End Date", "Posting End Date"])
def test_parse_job_text_ignores_non_employment_end_date_for_alternative_duration(
    label: str,
) -> None:
    result = parse_job_text(
        "Software Developer Intern (4 or 8 Month Term)\n"
        f"Start Date: September 7, 2026. {label}: October 30, 2026."
    )

    assert result.employment_timing.start_date == "2026-09-07"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None


@pytest.mark.parametrize(
    "experience_phrase",
    [
        "two years internship experience",
        "two years internship or co-op experience",
        "2 years internship/co-op experience",
        "two years contract management experience",
        "2 years contract administration experience",
        "2 years work placement coordination experience",
    ],
)
def test_parse_job_text_does_not_treat_internship_experience_as_job_duration(
    experience_phrase: str,
) -> None:
    result = parse_job_text(
        "Software Engineer\n"
        f"Requirements: At least {experience_phrase} is required.\n"
        "Posted: 2026-06-22",
        "https://example.test/jobs/software-engineer",
        source_type="browser_capture",
    )

    assert result.employment_timing.raw_label == "Posted 2026-06-22"
    assert result.employment_timing.timing_type == "posted_date_fallback"
    assert result.employment_timing.start_date == "2026-06-22"
    assert result.employment_timing.end_date is None
    assert result.employment_timing.duration_months is None
    assert result.employment_timing.schedule_type is None


def test_parse_job_text_keeps_genuine_internship_that_provides_experience() -> None:
    result = parse_job_text(
        "Summer Intern\nThis is a 6 month internship providing hands-on experience."
    )

    assert result.employment_timing.raw_label == "6 month internship"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.duration_months == 6


@pytest.mark.parametrize(
    "preceding_clause",
    [
        "No prior experience required.",
        "Requirements: No prior experience is required.",
        "Qualifications are preferred.",
    ],
)
def test_parse_job_text_keeps_duration_after_requirement_clause(preceding_clause: str) -> None:
    result = parse_job_text(
        f"Summer Intern\n{preceding_clause} This is a 6 month internship providing experience."
    )

    assert result.employment_timing.raw_label == "6 month internship"
    assert result.employment_timing.timing_type == "fixed_term"
    assert result.employment_timing.duration_months == 6


def test_parse_job_text_does_not_pair_posted_date_with_distant_education_duration():
    parsed = parse_job_text(
        "Environmental Engineer Intern\n"
        "Company: Gerdau\n"
        "Posted: 2026-07-14\n"
        "LOCATION: Posted on Jul 14, 2026 Selkirk, Manitoba, Canada "
        "AREA: G.Start Internships Join us in shaping the future. "
        + "Our environmental internship program supports students. " * 8
        + "REQUIREMENTS: Have completed at least two years of a degree.",
        "https://careers.example.test/environmental-engineer",
        source_type="ats_hanzilla",
    )

    assert parsed.employment_timing.raw_label == "Posted 2026-07-14"
    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-07-14"
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None


def test_parse_job_text_with_settings_replaces_implausible_model_timing_with_baseline():
    raw_text = (
        "Co-op/intern - Environmental Engineer (8 Month Term)\n"
        "Company: Gerdau\n"
        "Posted: 2026-07-14\n"
        "LOCATION: Posted on Jul 14, 2026 AREA: G.Start Internships "
        + "Program details. " * 30
        + "REQUIREMENTS: Have completed at least two years of a degree."
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = raw_text
            parsed.employment_timing.start_date = "2026-07-14"
            parsed.employment_timing.end_date = "2028-06-30"
            parsed.employment_timing.duration_months = 24
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        "https://careers.example.test/environmental-engineer",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
        source_type="ats_hanzilla",
    )

    assert parsed.employment_timing.raw_label == "8 Month Term"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months == 8


@pytest.mark.parametrize(
    ("timing_text", "expected_raw_label", "expected_start", "expected_end", "expected_duration"),
    [
        (
            "The internship term runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The Application Development internship runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The application security co-op runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The application engineering work term runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The Applications Engineering internship runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The Applications Engineer co-op runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term starts September 2026 and ends December 2026.",
            "September 2026 and ends December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term starts in Sept. 2026 and ends in Dec. 2026.",
            "Sept. 2026 and ends in Dec. 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The work term begins September 2026 through December 2026.",
            "September 2026 through December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The work term is September, 2026 to December, 2026.",
            "September, 2026 to December, 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term runs from September to December, 2026.",
            "September to December, 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term runs from September to December 2026. Application deadline is July 1, 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Application deadline is July 1, 2026. The internship term runs from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term runs from September to December 2026; application deadline is July 1, 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term runs September to December 2026, application deadline July 1.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship term runs between September and December 2026.",
            "between September and December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship runs full-time from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The internship runs as a paid placement from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The co-op lasts approximately from September to December 2026.",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Work Term: September - December, 2026",
            "September - December, 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Employment Period: September to December 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Co-op Period: September to December 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Coop Period: September to December 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Work Term: September to December 2026 Application Deadline: July 1, 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Term: September to December 2026 Application Deadline: July 1, 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "Application Deadline: July 1, 2026 Term: September to December 2026",
            "September to December 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "This co-op term runs Sep - Dec 2026.",
            "Sep - Dec 2026",
            "2026-09-01",
            "2026-12-31",
            4,
        ),
        (
            "The work term is from November 2026 through February 2027.",
            "November 2026 through February 2027",
            "2026-11-01",
            "2027-02-28",
            4,
        ),
    ],
)
def test_parse_job_text_extracts_month_range_term_timing(
    timing_text: str,
    expected_raw_label: str,
    expected_start: str,
    expected_end: str,
    expected_duration: int,
):
    parsed = parse_job_text(
        "Software Development Intern\n"
        "Company: Interac Corp.\n"
        "Posted: 2026-06-22\n"
        f"{timing_text}\n"
        "Python SQL machine learning",
        "https://example.test/jobs/interac",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.raw_label == expected_raw_label
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == expected_start
    assert parsed.employment_timing.end_date == expected_end
    assert parsed.employment_timing.duration_months == expected_duration
    assert parsed.employment_timing.schedule_type == "full_time"


def test_parse_job_text_resolves_strongest_month_range_timing_across_lines():
    parsed = parse_job_text(
        "Software Development Intern\n"
        "Company: ExampleCo\n"
        "Posted: 2026-06-22\n"
        "Availability: September to December 2026 for this internship.\n"
        "Work Term: January to April 2027\n"
        "Python SQL machine learning",
        "https://example.test/jobs/competing-month-ranges",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.raw_label == "January to April 2027"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2027-01-01"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.employment_timing.duration_months == 4


@pytest.mark.parametrize(
    "availability_text",
    [
        "Availability: September to December 2026",
        "Availability: September to December 2026 for this internship.",
        "Availability Window: September to December 2026",
        "Co-op Availability: September to December 2026",
        "Availability: September to December 2026 for application development internship.",
        "Availability: September to December 2026 for interview preparation internship.",
        "Availability: September to December 2026 for interview systems work.",
        "The internship requires availability from September to December 2026.",
        "The internship requires availability September to December 2026.",
        "The internship requires availability September to December 2026 for application development.",
        "The internship requires availability September to December 2026 for interview systems work.",
        "Candidates must be available September to December 2026 for the co-op term.",
        "Must be available from September to December 2026 for the internship.",
        "Candidates must be available between September and December 2026 for the work term.",
    ],
)
def test_parse_job_text_extracts_availability_month_range_as_term_timing(availability_text: str):
    parsed = parse_job_text(
        "Software Development Intern\n"
        "Company: Interac Corp.\n"
        "Posted: 2026-06-22\n"
        f"{availability_text}\n"
        "Python SQL machine learning",
        "https://example.test/jobs/availability",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.raw_label in {
        "September to December 2026",
        "between September and December 2026",
    }
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.employment_timing.schedule_type == "full_time"


@pytest.mark.parametrize(
    "availability_text",
    [
        "Application availability window is September to December 2026 for this co-op role.",
        "Application Availability: September to December 2026 for this internship.",
        "Interview Availability: September to December 2026 for this internship.",
        "Interview window runs September to December 2026 for this co-op role.",
        "Availability: September to December 2026 for interviews.",
        "Availability: September to December 2026 for applications.",
        "Availability Window: September to December 2026 for interviews.",
        "Availability Window: September to December 2026 for application interviews.",
        "Availability: September to December 2026 for interview.",
        "Availability: September to December 2026 for an interview.",
        "Availability: September to December 2026 for the interview.",
        "Availability: September to December 2026 for the interview with the hiring manager.",
        "Availability: September to December 2026 for an interview with the hiring manager.",
        "The internship requires availability September to December 2026 for the interview with the hiring manager.",
        "Candidates must be available September to December 2026 for interviews.",
        "The internship requires availability September to December 2026 for interviews.",
        "The co-op requires availability September to December 2026 for applications.",
        "Must be available September to December 2026 for interviews for this internship.",
    ],
)
def test_parse_job_text_does_not_use_non_term_availability_month_ranges(availability_text: str):
    parsed = parse_job_text(
        "Software Development Intern\n"
        "Company: ExampleCo\n"
        f"{availability_text}\n"
        "Posted: 2026-06-22\n"
        "Python SQL machine learning",
        "https://example.test/jobs/non-term-availability",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-06-22"
    assert parsed.employment_timing.duration_months is None


@pytest.mark.parametrize(
    "timing_window_text",
    [
        "Posting Close Date: July 7, 2026\nDuration: 12 month co-op term",
        "Application Start Date: August 31, 2026\nDuration: 12 month co-op term",
        "Duration: 12 month co-op term\nApplication Start Date: August 31, 2026",
        "Posting period runs from September to December 2026 for a student internship.",
        "Application window runs from September to December 2026 for a co-op role.",
        "Applications for this internship are open September to December 2026.",
        "Posting dates for this co-op run September to December 2026.",
        "The application period for this student role is September to December 2026.",
        "The application deadline for this internship is September to December 2026.",
        "The recruiting cycle for this internship runs from September to December 2026.",
        "Applications for the co-op role close from September to December 2026.",
        "Application Deadline: September to December 2026 for this internship.",
        "Application window runs between September and December 2026 for this co-op role.",
        "Applications are accepted September through December 2026 for this internship.",
    ],
)
def test_parse_job_text_preserves_duration_without_using_application_window_as_term_start(
    timing_window_text: str,
):
    parsed = parse_job_text(
        "Student WestJetter- Data Science- 12 month term\n"
        "Company: WestJet\n"
        f"{timing_window_text}\n"
        "Python SQL machine learning",
        "https://ca.indeed.com/viewjob?jk=e228efbdd8873209",
        source_type="browser_capture",
    )

    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months == 12


def test_parse_job_text_does_not_use_non_timing_month_ranges_as_employment_timing():
    for text in (
        "This internship uses Python and March 2026 to September 2026 data for training.",
        "The term includes reports between March and September 2026 data releases.",
        "This internship runs experiments from March 2026 to September 2026 using telemetry data.",
        "This internship runs training jobs from March 2026 to September 2026 data snapshots.",
        "The co-op runs reports from March 2026 to September 2026 for the analytics team.",
    ):
        parsed = parse_job_text(
            "Software Development Intern\n"
            "Company: ExampleCo\n"
            f"{text}\n"
            "Posted: 2026-06-22\n"
            "Python SQL machine learning",
            "https://example.test/jobs/non-timing-month-range",
            source_type="browser_capture",
        )

        assert parsed.employment_timing.timing_type == "posted_date_fallback"
        assert parsed.employment_timing.start_date == "2026-06-22"
        assert parsed.employment_timing.duration_months is None


def test_parse_job_text_extracts_ats_desired_start_and_position_end_dates():
    parsed = parse_job_text(
        "Applied Research Technician IV (Data Analyst - Coop Student)\n"
        "Company: Red Deer Polytechnic\n"
        "Job description: This Fall coop position supports data analysis.\n"
        "Employment Type: Casual. Desired Start Date: 09/01/2026. "
        "Position End Date: 12/23/2026. FTE: 1.0. "
        "Hours of Work: 8:30 a.m. to 4:30 p.m., Monday to Friday, 35 hours per week.",
        "https://employment.rdpolytech.ca/postings/7478",
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-23"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.employment_timing.hours_per_week_min == 35
    assert parsed.employment_timing.hours_per_week_max == 35
    assert parsed.employment_timing.schedule_type == "full_time"


def test_parse_job_text_falls_back_to_posted_date_when_no_start_term_is_available():
    parsed = parse_job_text(
        "Junior Software Engineer Co-Op\n"
        "Company: Collins Aerospace\n"
        "Python C++ software-defined radio.\n"
        "Resolution Evidence: Official Workday record shows date posted 2026-06-19 and postingAvailable=true.",
        "https://globalhr.wd5.myworkdayjobs.com/REC_RTX_Ext_Gateway/job/01853388",
    )

    assert parsed.employment_timing.raw_label == "Posted 2026-06-19"
    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-06-19"
    assert parsed.employment_timing.end_date is None
    assert parsed.data_quality.inferred_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_falls_back_to_labeled_posted_date_when_no_start_term_is_available():
    parsed = parse_job_text(
        "Software Developer Intern\n"
        "Company: ExampleCo\n"
        "Posted: May 30, 2026\n"
        "Python SQL backend role.",
        "https://example.test/jobs/posted-date",
    )

    assert parsed.employment_timing.raw_label == "Posted 2026-05-30"
    assert parsed.employment_timing.timing_type == "posted_date_fallback"
    assert parsed.employment_timing.start_date == "2026-05-30"


def test_parse_job_text_ignores_application_start_date_for_employment_timing():
    parsed = parse_job_text(
        "Software Developer Intern\n"
        "Company: ExampleCo\n"
        "Application Start Date: June 1, 2026\n"
        "Application End Date: June 30, 2026\n"
        "Applications are reviewed on a rolling basis.",
        "https://example.test/jobs/application-window",
    )

    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.timing_type == "unknown"


def test_parse_job_text_ignores_posting_start_date_for_employment_timing():
    parsed = parse_job_text(
        "Software Developer Intern\n"
        "Company: ExampleCo\n"
        "Posting Start Date: 06/01/2026\n"
        "Close Date: 06/30/2026\n"
        "Applications are reviewed on a rolling basis.",
        "https://example.test/jobs/posting-window",
    )

    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.timing_type == "unknown"


def test_parse_job_text_extracts_combined_academic_term_timing():
    for label in ("Fall/Winter 2026", "Fall / Winter 2026", "Fall-Winter 2026"):
        parsed = parse_job_text(
            f"Software Engineer 8 month Co-op - {label}\n"
            "Company: Ciena\n"
            "Location: Ottawa, Ontario, Canada\n"
            "Python SQL",
            "https://example.test/jobs/456",
        )

        assert parsed.employment_timing.raw_label == "Fall/Winter 2026"
        assert parsed.employment_timing.start_date == "2026-09-01"
        assert parsed.employment_timing.end_date == "2027-04-30"
        assert parsed.employment_timing.duration_months == 8


def test_parse_job_text_extracts_academic_term_timing_from_description():
    parsed = parse_job_text(
        "Software Developer Co-Op\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "This Fall 2026 co-op role works on Python Docker Java services.",
        "https://www.linkedin.com/jobs/view/4425694183",
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-31"


def test_parse_job_text_ignores_unrelated_sidebar_duration():
    parsed = parse_job_text(
        "Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "Build Python services.\n"
        "Similar jobs\n"
        "Software Engineer 8 month Co-op - Fall/Winter 2026\n"
        "Ciena",
        "https://www.linkedin.com/jobs/view/4425694183",
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.employment_timing.end_date == "2026-12-31"


def test_codex_parse_prompt_instructs_term_timing_extraction():
    calls = []

    class CapturingParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            calls.append((raw_text, baseline))
            return baseline

    parse_job_text_with_settings(
        "Software Developer Co-Op\nCompany: TrendAI\nThis Fall 2026 role uses Python.",
        "https://www.linkedin.com/jobs/view/4425694183",
        Settings(posting_parser_provider="codex_cli"),
        parser=CapturingParser(),
    )

    raw_text, baseline = calls[0]
    assert "Fall 2026" in raw_text
    assert baseline.employment_timing.start_date == "2026-09-01"


def test_codex_cli_posting_parser_prompt_rejects_posting_window_dates():
    calls = []

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="not used")

    parser = CodexCliPostingParser(command="codex", runner=fake_runner)
    parser.parse(
        "Software Developer Intern\nCompany: ExampleCo\nPosting Start Date: 06/01/2026\nClose Date: 06/30/2026",
        source_url="https://example.test/jobs/posting-window",
        baseline=parse_job_text("Software Developer Intern\nCompany: ExampleCo"),
    )

    prompt = calls[0][1]["input"]
    assert "Posting Start Date" in prompt
    assert "Open Date" in prompt
    assert "Close Date" in prompt
    assert "Closing Date" in prompt
    assert "Requisition Date" in prompt
    assert "for employment_timing" in prompt


def test_parse_job_text_marks_blocked_aggregator_page_without_evidence_missing_title():
    parsed = parse_job_text(
        "Page Title: Blocked - Indeed.com\n"
        "Request Blocked\n"
        "You have been blocked.\n"
        "Troubleshooting Cloudflare Errors",
        "https://ca.indeed.com/viewjob?jk=f9ddd28b706e9926",
    )

    assert parsed.role.job_title is None
    assert parsed.role.normalized_title is None
    assert parsed.employer.company_name is None
    assert "role.job_title" in parsed.data_quality.missing_fields
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_bounds_inline_field_values():
    parsed = parse_job_text(
        "Title: Software Engineer Company: Acme Labs Location: Toronto, ON, Canada\nPython SQL",
        "https://example.test/jobs/123",
    )

    assert parsed.role.job_title == "Software Engineer"
    assert parsed.employer.company_name == "Acme Labs"
    assert parsed.location.city == "Toronto"
    assert parsed.location.province_state == "Ontario"
    assert parsed.location.country == "Canada"


def test_provenance_shaped_prose_does_not_split_structured_field_values():
    raw_text = (
        "Title: Software Engineer\n"
        "Company: Example Systems uses ATS integration across our hiring company: Workday."
    )

    assert _field_value(raw_text.splitlines(), "Company") == (
        "Example Systems uses ATS integration across our hiring company: Workday."
    )
    assert [(field.label, field.value) for field in _posting_field_spans(raw_text)] == [
        ("title", "Software Engineer"),
        (
            "company",
            "Example Systems uses ATS integration across our hiring company: Workday",
        ),
    ]


def test_parse_job_text_ignores_embedded_role_section_as_title():
    parsed = parse_job_text(
        "Full Stack Developer\n"
        "Company: Isaac Operations Ltd.\n"
        "Location: Toronto ON\n"
        "Full Stack Developer Isaac Operations Ltd. Toronto ON full-time "
        "Who we are: We are an operational consultancy. "
        "What you bring to the role: "
        + "Minimum 2 years of experience. " * 30
        + "Why work for us: Flexible working environment.",
        "https://recruiting.example.test/jobs/full-stack-developer",
        source_type="ats_eluta",
    )

    assert parsed.role.job_title == "Full Stack Developer"
    assert parsed.role.normalized_title == "full_stack_developer"


def test_parse_job_text_with_settings_replaces_implausible_model_title_with_baseline():
    raw_text = (
        "Full Stack Developer\n"
        "Company: Isaac Operations Ltd.\n"
        "What you bring to the role: " + "Minimum 2 years of experience. " * 30
    )
    malformed = parse_job_text("Temporary title\nCompany: Isaac Operations Ltd.")
    malformed.role.job_title = "Minimum 2 years of experience. " * 30
    malformed.role.normalized_title = "minimum_2_years_of_experience_" * 30

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            return malformed

    parsed = parse_job_text_with_settings(
        raw_text,
        "https://recruiting.example.test/jobs/full-stack-developer",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
        source_type="ats_eluta",
    )

    assert parsed.role.job_title == "Full Stack Developer"
    assert parsed.role.normalized_title == "full_stack_developer"


@pytest.mark.parametrize("case_id", [
    "generic_title_hint_recovers_posting_1",
    "generic_title_hint_recovers_posting_5",
])
def test_generic_provider_title_uses_source_grounded_baseline(case_id):
    case = next(case for case in load_parser_corpus().cases if case.id == case_id)

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            result = baseline.model_copy(deep=True)
            result.role.job_title = case.title_hint
            result.role.normalized_title = "generic_page_heading"
            return result

    parsed = parse_job_text_with_settings(
        case.raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
        title_hint=case.title_hint,
    )
    expected = case.expected["role"]["job_title"]
    baseline = parse_job_text(case.raw_text, title_hint=expected)
    assert parsed.role.job_title == expected
    assert parsed.role.normalized_title == baseline.role.normalized_title


def test_parse_job_text_prefers_valid_page_title_over_body_text():
    parsed = parse_job_text(
        "Page Title: Software Engineer - Acme Careers\n"
        "Company: Acme Labs\n"
        "Location: Toronto, ON, Canada\n"
        "Python SQL",
        "https://example.test/jobs/software-engineer",
    )

    assert parsed.role.job_title == "Software Engineer - Acme Careers"
    assert parsed.employer.company_name == "Acme Labs"


def test_parse_job_text_with_settings_scopes_provider_input_before_sidebar() -> None:
    calls: list[str] = []

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            calls.append(raw_text)
            return baseline

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Similar jobs:\n"
        "Other Developer Intern - Fall 2026",
        "https://jobs.example.test/provider-sidebar-scope",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert calls == ["Software Developer Intern"]
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None


def test_parse_job_text_with_settings_rejects_malformed_provider_dates_atomically() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employer.company_name = "Provider Employer"
            parsed.employment_timing.raw_label = "September 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "September 2026"
            parsed.data_quality.missing_fields = [
                "employment_timing.end_date",
                "role.job_family",
            ]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nThe role begins when selected.",
        "https://jobs.example.test/provider-malformed-date",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.employer.company_name == "Provider Employer"
    assert parsed.data_quality.missing_fields == ["role.job_family"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_rejects_provider_timing_without_scoped_evidence() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "Fall 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-01"
            parsed.employment_timing.end_date = "2026-12-31"
            parsed.employment_timing.duration_months = 4
            parsed.employment_timing.schedule_type = "full_time"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Similar jobs:\n"
        "Other Developer Intern - Fall 2026",
        "https://jobs.example.test/provider-unsupported-timing",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.duration_months is None


def test_parse_job_text_with_settings_ignores_sidebar_posted_date_fallback() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = None
            parsed.employment_timing.timing_type = "unknown"
            parsed.employment_timing.start_date = None
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            parsed.data_quality.inferred_fields = []
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "People also viewed:\n"
        "Other Developer Intern\n"
        "Date Posted: July 1, 2026",
        "https://jobs.example.test/provider-sidebar-posted-date",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None
    assert parsed.data_quality.inferred_fields == []

def test_codex_cli_parser_prompt_excludes_related_jobs_from_text_and_baseline() -> None:
    raw_text = (
        "Software Developer Intern\n"
        "Similar jobs:\n"
        "Other Developer Intern - Fall 2026"
    )
    baseline = parse_job_text(raw_text)
    calls: list[str] = []

    def fake_runner(args, **kwargs):
        calls.append(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=baseline.model_dump_json(),
            stderr="",
        )

    parser = CodexCliPostingParser(command="codex", runner=fake_runner)
    parsed = parser.parse(raw_text, baseline=baseline)

    assert parsed is not None
    assert "Other Developer Intern" not in calls[0]
    assert '"description_raw"' not in calls[0]


def test_parse_job_text_with_settings_accepts_supported_provider_only_timing() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "Late August 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            parsed.data_quality.inferred_fields = ["employment_timing.start_date"]
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "The placement kicks off when classes finish in Late August 2026.",
        "https://jobs.example.test/provider-supported-timing",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Late August 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-08-24"
    assert parsed.data_quality.inferred_fields == ["employment_timing.start_date"]


def test_parse_job_text_with_settings_discards_unsupported_provider_timing_quality_atomically() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "Winter 2027"
            parsed.employment_timing.start_date = "2027-01-01"
            parsed.employment_timing.end_date = "2027-04-30"
            parsed.data_quality.ambiguous_fields = ["employment_timing.end_date"]
            parsed.data_quality.inferred_fields = ["employment_timing.start_date"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern - Fall 2026",
        "https://jobs.example.test/provider-unsupported-quality",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.inferred_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_preserves_supported_provider_timing_ambiguity() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.data_quality.ambiguous_fields = ["employment_timing.start_date"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern - Fall 2026",
        "https://jobs.example.test/provider-supported-ambiguity",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.data_quality.ambiguous_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_rejects_provider_date_that_contradicts_supported_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "Late August 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2027-01-10"
            parsed.data_quality.inferred_fields = ["employment_timing.start_date"]
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nThe placement begins in Late August 2026.",
        "https://jobs.example.test/provider-contradictory-date",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None
    assert parsed.data_quality.inferred_fields == []


def test_parse_job_text_with_settings_rejects_description_sized_provider_timing_label() -> None:
    description_label = (
        "The placement begins after classes finish in Late August 2026 and includes "
        "mentoring, project rotations, team events, technical workshops, and final presentations"
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = description_label
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            return parsed

    parsed = parse_job_text_with_settings(
        f"Software Developer Intern\n{description_label}.",
        "https://jobs.example.test/provider-description-label",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label is None
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None


def test_parse_job_text_with_settings_rejects_provider_label_without_timing_signal() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "The placement includes mentoring"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nThe placement includes mentoring.",
        "https://jobs.example.test/provider-non-timing-label",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label is None
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None


def test_parse_job_text_with_settings_accepts_written_exact_provider_date() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nThe placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-written-exact-date",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-30"


def test_parse_job_text_with_settings_prefers_exact_provider_date_over_academic_default() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "2026-09-30"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = "2026-12-31"
            parsed.employment_timing.duration_months = 4
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern - Fall 2026\n"
        "The employment orientation date is 2026-09-30.",
        "https://jobs.example.test/provider-exact-date-over-default",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_parse_job_text_with_settings_rejects_provider_iso_range_with_wrong_end() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "2026-08-24 to 2026-12-18"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            parsed.employment_timing.end_date = "2027-07-31"
            parsed.employment_timing.duration_months = 12
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "The placement runs 2026-08-24 to 2026-12-18.",
        "https://jobs.example.test/provider-iso-range-wrong-end",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None

def test_parse_job_text_with_settings_accepts_matching_provider_iso_range() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "2026-08-24 to 2026-12-18"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            parsed.employment_timing.end_date = "2026-12-18"
            parsed.employment_timing.duration_months = 5
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "The placement runs 2026-08-24 to 2026-12-18.",
        "https://jobs.example.test/provider-matching-iso-range",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "2026-08-24 to 2026-12-18"
    assert parsed.employment_timing.start_date == "2026-08-24"
    assert parsed.employment_timing.end_date == "2026-12-18"
    assert parsed.employment_timing.duration_months == 5


def test_parse_job_text_with_settings_keeps_explicit_start_over_provider_prose_date() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "2026-09-30"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "The employment orientation date is 2026-09-30.",
        "https://jobs.example.test/explicit-start-over-provider-prose",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "Fall 2026"
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.needs_manual_review is False

def test_parse_job_text_with_settings_merges_exact_provider_start_with_baseline_duration() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = None
            parsed.employment_timing.duration_months = None
            parsed.employment_timing.schedule_type = None
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Duration: 8 months\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-start-baseline-duration",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date == "2027-04-30"
    assert parsed.employment_timing.duration_months == 8


def test_parse_job_text_with_settings_discards_provider_fields_not_supported_by_exact_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = "2027-12-31"
            parsed.employment_timing.duration_months = 16
            parsed.employment_timing.schedule_type = "part_time"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nThe placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-unsupported-extra-fields",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.employment_timing.schedule_type is None

def test_parse_job_text_with_settings_combines_provider_start_with_baseline_end() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = "2026-12-31"
            parsed.employment_timing.duration_months = 4
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "End Date: December 31, 2026\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-start-baseline-end",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4


def test_parse_job_text_with_settings_uses_scoped_schedule_outside_provider_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.schedule_type = "part_time"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Employment Type: Part-time\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-separate-schedule",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.schedule_type == "part_time"


def test_parse_job_text_with_settings_uses_scoped_hours_outside_provider_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.hours_per_week_min = 20
            parsed.employment_timing.hours_per_week_max = 20
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Schedule: 20 hours per week\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-separate-hours",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.hours_per_week_min == 20
    assert parsed.employment_timing.hours_per_week_max == 20


def test_parse_job_text_with_settings_refines_month_range_with_exact_provider_start() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            parsed.employment_timing.end_date = "2026-12-31"
            parsed.employment_timing.duration_months = 4
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Internship Term: September to December 2026\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-exact-start-month-range",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4


def test_parse_job_text_with_settings_keeps_exact_range_over_conflicting_provider_duration() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "8 months"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.end_date = "2027-04-30"
            parsed.employment_timing.duration_months = 8
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "End Date: December 31, 2026\n"
        "Duration: 8 months",
        "https://jobs.example.test/exact-range-over-provider-duration",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-31"
    assert parsed.employment_timing.duration_months == 4
    assert "employment_timing.duration_months" in parsed.data_quality.ambiguous_fields


@pytest.mark.parametrize(
    (
        "raw_text",
        "provider_label",
        "provider_updates",
        "ambiguous_field",
        "needs_manual_review",
    ),
    [
        (
            "Software Developer Intern\nTerm Length: 4 or 8 months",
            "8 months",
            {"duration_months": 8},
            "employment_timing.duration_months",
            True,
        ),
        (
            "Software Developer Intern\n"
            "This is a full-time 6 month internship. The role is part-time.",
            "full-time 6 month internship",
            {"schedule_type": "full_time"},
            "employment_timing.schedule_type",
            True,
        ),
        (
            "Software Developer Intern\n"
            "Start Date: September 1, 2026\nStart Date: January 4, 2027",
            "January 4, 2027",
            {"start_date": "2027-01-04"},
            "employment_timing.start_date",
            True,
        ),
    ],
)
def test_parse_job_text_with_settings_does_not_collapse_ambiguous_baseline_timing(
    raw_text: str,
    provider_label: str,
    provider_updates: dict[str, object],
    ambiguous_field: str,
    needs_manual_review: bool,
) -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = provider_label
            parsed.employment_timing.timing_type = "fixed_term"
            for field, value in provider_updates.items():
                setattr(parsed.employment_timing, field, value)
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        "https://jobs.example.test/provider-ambiguous-claim",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert (ambiguous_field in parsed.data_quality.ambiguous_fields) is needs_manual_review
    assert parsed.data_quality.needs_manual_review is needs_manual_review
    field = ambiguous_field.removeprefix("employment_timing.")
    assert getattr(parsed.employment_timing, field) is None

def test_parse_job_text_with_settings_accepts_provider_schedule_without_timing_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.schedule_type = "part_time"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nEmployment Type: Part-time",
        "https://jobs.example.test/provider-schedule-only",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label is None
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.schedule_type == "part_time"


def test_parse_job_text_with_settings_accepts_provider_hours_without_timing_label() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.hours_per_week_min = 20
            parsed.employment_timing.hours_per_week_max = 20
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\nSchedule: 20 hours per week",
        "https://jobs.example.test/provider-hours-only",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label is None
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.hours_per_week_min == 20
    assert parsed.employment_timing.hours_per_week_max == 20


def test_parse_job_text_with_settings_does_not_copy_unsupported_label_with_supported_field() -> None:
    oversized_label = (
        "The role is part-time and includes mentoring, project rotations, team events, "
        "technical workshops, customer interviews, documentation, presentations, "
        "training sessions, and professional development throughout the placement"
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = oversized_label
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.schedule_type = "part_time"
            return parsed

    parsed = parse_job_text_with_settings(
        f"Software Developer Intern\nEmployment Type: Part-time\n{oversized_label}",
        "https://jobs.example.test/provider-field-with-unsupported-label",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label is None
    assert parsed.employment_timing.timing_type == "unknown"
    assert parsed.employment_timing.schedule_type == "part_time"

def test_parse_job_text_keeps_academic_term_alternatives_unresolved() -> None:
    parsed = parse_job_text("Software Developer Intern - Fall 2026 or Winter 2027")

    assert parsed.employment_timing.raw_label == "Fall 2026 or Winter 2027"
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.employment_timing.schedule_type == "full_time"
    assert parsed.data_quality.ambiguous_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_marks_same_month_exact_dates_ambiguous() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = "September 30, 2026"
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-30"
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Start Date: September 1, 2026\n"
        "The placement launches on September 30, 2026.",
        "https://jobs.example.test/provider-exact-date-conflict",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.data_quality.ambiguous_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is True


def test_parse_job_text_with_settings_hours_do_not_downgrade_term_metadata() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.timing_type = "unknown"
            parsed.employment_timing.hours_per_week_min = 20
            parsed.employment_timing.hours_per_week_max = 20
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Internship Term: September to December 2026\n"
        "Schedule: 20 hours per week",
        "https://jobs.example.test/provider-metadata-authority",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September to December 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.hours_per_week_min == 20
    assert parsed.employment_timing.hours_per_week_max == 20

def test_parse_job_text_with_settings_classifies_mixed_provider_dates_independently() -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = (
                "Applications close August 24, 2026; "
                "the placement starts September 30, 2026"
            )
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-08-24"
            parsed.employment_timing.end_date = "2026-09-30"
            parsed.employment_timing.duration_months = 2
            return parsed

    parsed = parse_job_text_with_settings(
        "Software Developer Intern\n"
        "Applications close August 24, 2026; "
        "the placement starts September 30, 2026.",
        "https://jobs.example.test/provider-mixed-date-roles",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == "September 30, 2026"
    assert parsed.employment_timing.timing_type == "fixed_term"
    assert parsed.employment_timing.start_date == "2026-09-30"
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    "academic_terms",
    [
        "Fall 2026, Winter 2027, or Spring 2027",
        "Fall 2026, Winter 2027 or Spring 2027",
        "Fall 2026, Winter 2027, Spring 2027",
        "Fall 2026 / Winter 2027 / Spring 2027",
        "Fall or Winter 2026",
        "Fall, Winter, or Spring 2027",
        "Fall 2026 and Winter 2027",
        "Fall 2026 & Winter 2027",
    ],
)
def test_parse_job_text_keeps_academic_term_lists_unresolved(
    academic_terms: str,
) -> None:
    parsed = parse_job_text(f"Software Developer Intern - {academic_terms}")

    assert parsed.employment_timing.raw_label == academic_terms
    assert parsed.employment_timing.start_date is None
    assert parsed.employment_timing.end_date is None
    assert parsed.employment_timing.duration_months is None
    assert parsed.data_quality.ambiguous_fields == ["employment_timing.start_date"]
    assert parsed.data_quality.needs_manual_review is True

@pytest.mark.parametrize(
    "end_predicate",
    ["finishes", "concludes", "completes", "expires", "terminates"],
)
def test_parse_job_text_with_settings_resolves_provider_end_predicates(
    end_predicate: str,
) -> None:
    label = (
        "The placement starts September 1, 2026 "
        f"and {end_predicate} December 18, 2026"
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.employment_timing.raw_label = label
            parsed.employment_timing.timing_type = "fixed_term"
            parsed.employment_timing.start_date = "2026-09-01"
            parsed.employment_timing.end_date = "2026-12-18"
            parsed.employment_timing.duration_months = 4
            return parsed

    parsed = parse_job_text_with_settings(
        f"Software Developer Intern\n{label}.",
        "https://jobs.example.test/provider-end-predicate",
        Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.employment_timing.raw_label == label
    assert parsed.employment_timing.start_date == "2026-09-01"
    assert parsed.employment_timing.end_date == "2026-12-18"
    assert parsed.employment_timing.duration_months == 4
    assert parsed.data_quality.ambiguous_fields == []
    assert parsed.data_quality.needs_manual_review is False


def test_provider_month_range_duration_requires_source_corroboration() -> None:
    timing = ParsedTiming(
        raw_label="January 2026 to September 2026",
        timing_type="fixed_term",
        start_date="2026-01-01",
        end_date="2026-09-01",
        duration_months=8,
        schedule_type="full_time",
    )

    assert _provider_timing_values_match_raw_label(timing) is False


def test_provider_merge_preserves_corroborated_boundary_duration() -> None:
    timing = ParsedTiming(
        raw_label="8-month co-op term from September 2026 to May 2027",
        timing_type="fixed_term",
        start_date="2026-09-01",
        end_date="2027-05-01",
        duration_months=8,
        schedule_type="full_time",
    )

    merged = _merge_provider_timing_fields(
        timing,
        ParsedTiming(),
        supported_fields=frozenset(
            {
                "raw_label",
                "timing_type",
                "start_date",
                "end_date",
                "duration_months",
                "schedule_type",
            }
        ),
        use_provider_label=True,
    )

    assert merged.duration_months == 8


def test_provider_exact_anniversary_matches_explicit_duration() -> None:
    timing = ParsedTiming(
        raw_label=(
            "12-month co-op term from September 1, 2026 "
            "to September 1, 2027"
        ),
        timing_type="fixed_term",
        start_date="2026-09-01",
        end_date="2027-09-01",
        duration_months=12,
        schedule_type="full_time",
    )

    assert _provider_timing_values_match_raw_label(timing) is True


@pytest.mark.parametrize("year", [2028, 2031])
@pytest.mark.parametrize("season,month", [("Winter", 1), ("Summer", 5), ("Fall", 9)])
def test_observed_part_time_availability_generalizes(year, season, month):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "part_time_season_availability")
    text = case.raw_text.replace("Fall 2026", f"{season} {year}")
    timing = parse_job_text(text).employment_timing
    assert timing.raw_label == f"{season} {year}"
    assert timing.start_date == f"{year}-{month:02d}-01"
    assert timing.schedule_type == "part_time"


@pytest.mark.parametrize("year", [2028, 2031])
@pytest.mark.parametrize("month,number", [
    ("janvier", 1), ("f\u00e9vrier", 2), ("mars", 3), ("avril", 4),
    ("mai", 5), ("juin", 6), ("juillet", 7), ("ao\u00fbt", 8),
    ("septembre", 9), ("octobre", 10), ("novembre", 11), ("d\u00e9cembre", 12),
])
def test_observed_french_labeled_range_generalizes(year, month, number):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "french_expected_start_end_months")
    text = case.raw_text.replace("septembre 2026", f"{month} {year}").replace(
        "avril 2027", f"{month} {year + 1}",
    )
    timing = parse_job_text(text).employment_timing
    assert timing.start_date == f"{year}-{number:02d}-01"
    assert timing.end_date.startswith(f"{year + 1}-{number:02d}-")
    assert timing.duration_months == 13


@pytest.mark.parametrize("year", [2028, 2031])
@pytest.mark.parametrize("month", range(1, 13))
def test_observed_split_iso_fields_generalize(year, month):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "split_start_end_iso_fields")
    text = case.raw_text.replace("2026-09-10", f"{year}-{month:02d}-10").replace(
        "2027-03-31", f"{year + 1}-{month:02d}-28",
    )
    timing = parse_job_text(text).employment_timing
    assert timing.start_date == f"{year}-{month:02d}-10"
    assert timing.end_date == f"{year + 1}-{month:02d}-28"


@pytest.mark.parametrize("year", [2028, 2031])
def test_observed_counted_cohorts_generalize(year):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "counted_cohort_month_ranges")
    timing = parse_job_text(case.raw_text.replace("2027", str(year))).employment_timing
    assert timing.raw_label == f"Winter/Summer {year}"
    assert timing.start_date is None and timing.end_date is None
    assert timing.duration_months == 8


@pytest.mark.parametrize("months", [4, 6, 8, 12, 16])
def test_observed_split_duration_preserves_actual_field(months):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "split_internship_duration_not_graduation_recency")
    timing = parse_job_text(case.raw_text.replace("4 months", f"{months} months")).employment_timing
    assert timing.duration_months == months
    assert timing.start_date is None and timing.end_date is None


@pytest.mark.parametrize("year", [2027, 2031])
@pytest.mark.parametrize("separator", ["-", "–", "—"])
def test_observed_hiring_season_preserves_duration_and_open_start(year, separator):
    case = next(c for c in load_parser_corpus().cases
                if c.id == "hiring_season_parenthesized_start_month_window")
    text = case.raw_text.replace("2027", str(year)).replace("May-June", f"May{separator}June")
    parsed = parse_job_text(text)
    timing = parsed.employment_timing
    assert timing.raw_label == f"Summer {year}"
    assert timing.duration_months == 3
    assert timing.start_date is None and timing.end_date is None
    assert timing.schedule_type == "full_time"
    assert timing.hours_per_week_min == timing.hours_per_week_max == 40
    assert "employment_timing.start_date" in parsed.data_quality.ambiguous_fields
    assert "employment_timing.end_date" in parsed.data_quality.ambiguous_fields
    assert not parsed.data_quality.inferred_fields
