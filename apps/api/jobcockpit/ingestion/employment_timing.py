from __future__ import annotations

import re
from collections.abc import Hashable
from dataclasses import replace
from datetime import date, timedelta
from typing import TypeVar

from jobcockpit.ingestion.evidence_grammar import EvidenceMention
from jobcockpit.ingestion.field_resolution import EvidenceRule, select_evidence
from jobcockpit.ingestion.location_parser import looks_like_comma_separated_location
from jobcockpit.ingestion.month_range_timing import (
    MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING,
    _classify_month_range_timing_candidate,
    _duration_field_precedes_generic_date_range,
    _extract_month_range_timing_candidates,
    _month_range_duration,
    _month_range_matches,
    _nearest_label_before_offset,
    _parsed_timing_from_month_range_candidate,
    _timing_prefix_is_history_context,
)
from jobcockpit.ingestion.parser_models import ParsedTiming
from jobcockpit.ingestion.posting_document import session_cached
from jobcockpit.ingestion.posting_fields import (
    EMPLOYMENT_HOURS_FIELD_LABELS,
    _clean_field_value,
    _field_label,
    _field_values,
    _provenance_field_semantic,
)
from jobcockpit.ingestion.posting_scope import (
    _EMPLOYMENT_ACADEMIC_TERM_FIELD_LABELS,
    _EMPLOYMENT_DESCRIPTIVE_SCHEDULE_FIELD_LABELS,
    _EMPLOYMENT_END_FIELD_LABELS,
    _EMPLOYMENT_SCHEDULE_FIELD_LABELS,
    _EMPLOYMENT_START_FIELD_LABELS,
    _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS,
    _employment_timing_content_scope,
    _posting_content_lines,
    _posting_content_scope,
    _posting_field_spans,
    _strip_timing_note_label,
    _text_without_labeled_fields,
)
from jobcockpit.ingestion.role_parser import _title_from_mixed_line, _title_value
from jobcockpit.ingestion.text_patterns import NUMERIC_RANGE_SEPARATOR_PATTERN
from jobcockpit.ingestion.timing_clauses import (
    TIMING_CLAUSE_CONTEXT_APPLICATION,
    TIMING_CLAUSE_CONTEXT_EMPLOYMENT,
    TIMING_CLAUSE_CONTEXT_INTERVIEW,
    _timing_clause_for_span,
)
from jobcockpit.ingestion.timing_constants import (
    ACADEMIC_TERM_MONTHS,
    ACADEMIC_TERM_PATTERN,
    COMBINED_ACADEMIC_TERMS,
    DURATION_NUMBER_WORDS,
    MONTH_NAMES,
    MONTH_YEAR_ACADEMIC_TERM_LABELS,
    NON_EMPLOYMENT_TIMING_DATE_LABELS,
)
from jobcockpit.ingestion.timing_dates import (
    _end_date_for_month_span,
    _last_day_of_month,
)
from jobcockpit.ingestion.timing_models import (
    AcademicTermEvidence,
    AcademicTimingSignature,
    EmploymentDateEvidence,
    EmploymentDurationEvidence,
    EmploymentTimingEvidence,
    EmploymentTimingResolution,
    MonthRangeTimingAssessment,
    MonthRangeTimingCandidate,
    ParsedTimingCandidate,
    PostingFieldSpan,
    ScheduleEvidenceResolution,
    _date_range_supports_duration,
    _duration_months_from_date_range,
    _single_employment_date,
)

MAX_TIMING_DATE_DURATION_DISTANCE = 160
_ClaimValue = TypeVar("_ClaimValue", bound=Hashable)


def _apply_title_hint_duration(
    timing: ParsedTiming,
    title_hint: str | None,
    *,
    body_duration_is_authoritative: bool = False,
) -> ParsedTiming:
    if not title_hint or not _title_text_is_safe_timing_authority(title_hint):
        return timing

    title_month_year_term = _title_hint_month_year_term(title_hint)
    title_academic_term = _title_hint_academic_term(title_hint)
    title_month_range_timing = _title_hint_month_range_timing(title_hint)
    title_month_alternative_term = _title_hint_month_alternative_term(title_hint)
    if (
        not _employment_academic_term_evidence(f"Ingest Title Hint: {title_hint}")
        and title_month_year_term is None
        and title_academic_term is None
        and title_month_range_timing is None
        and title_month_alternative_term is None
    ):
        return timing

    def finalize(candidate: ParsedTiming) -> ParsedTiming:
        if title_month_alternative_term is not None and candidate.timing_type in {
            "unknown",
            "posted_date_fallback",
        }:
            return candidate.model_copy(
                update={
                    "raw_label": title_month_alternative_term,
                    "timing_type": "fixed_term",
                    "start_date": None,
                    "end_date": None,
                    "duration_months": None,
                    "duration_label": None,
                    "schedule_type": candidate.schedule_type or "full_time",
                }
            )
        if title_month_range_timing is not None:
            title_range = title_month_range_timing
            return title_range.model_copy(
                update={
                    "duration_label": candidate.duration_label,
                    "hours_per_week_min": candidate.hours_per_week_min,
                    "hours_per_week_max": candidate.hours_per_week_max,
                    "schedule_type": candidate.schedule_type or title_range.schedule_type,
                }
            )
        title_term = title_month_year_term or title_academic_term
        if title_term is None:
            return candidate
        raw_label, start_date = title_term
        update: dict[str, object] = {
            "raw_label": raw_label,
            "timing_type": "fixed_term",
            "start_date": (
                candidate.start_date
                if candidate.start_date
                and candidate.start_date[:7] == start_date[:7]
                else start_date
            ),
        }
        if (
            title_academic_term is not None
            and candidate.duration_months is None
            and re.search(
                rf"\(\s*(?:{ACADEMIC_TERM_PATTERN})\s+\d{{2}}\s*\)",
                title_hint,
                flags=re.IGNORECASE,
            )
        ):
            term_key = _canonical_term_key(raw_label.rsplit(" ", 1)[0])
            start_term, duration_months = _academic_term_defaults(term_key)
            start_year = int(start_date[:4])
            start_month = ACADEMIC_TERM_MONTHS[start_term][0]
            update.update(
                {
                    "end_date": _end_date_for_month_span(
                        start_year,
                        start_month,
                        duration_months,
                    ),
                    "duration_months": duration_months,
                    "schedule_type": candidate.schedule_type or "full_time",
                }
            )
        conflicting_start = bool(
            candidate.timing_type != "posted_date_fallback"
            and candidate.start_date
            and candidate.start_date[:7] != start_date[:7]
        )
        if conflicting_start:
            update["end_date"] = None
        if candidate.duration_months and (not candidate.end_date or conflicting_start):
            start_year, start_month = map(int, start_date.split("-")[:2])
            update["end_date"] = _end_date_for_month_span(
                start_year,
                start_month,
                candidate.duration_months,
            )
        return candidate.model_copy(update=update)

    alternatives = [
        match
        for match in _alternative_duration_matches(title_hint)
        if not _duration_label_is_experience_requirement(title_hint, match.end())
    ]
    if len(alternatives) == 1:
        match = alternatives[0]
        duration_label = _clean_field_value(match.group(0))
        title_options = _alternative_duration_options(match)
        if timing.duration_months in title_options:
            return finalize(timing.model_copy(update={"duration_label": duration_label}))
        if body_duration_is_authoritative and timing.duration_months is not None:
            return finalize(timing)
        if body_duration_is_authoritative and timing.duration_label is not None:
            body_alternatives = list(
                _alternative_duration_matches(timing.duration_label)
            )
            if (
                len(body_alternatives) == 1
                and _alternative_duration_options(body_alternatives[0])
                == title_options
            ):
                return finalize(
                    timing.model_copy(update={"duration_label": duration_label})
                )
            return finalize(timing)
        return finalize(
            timing.model_copy(
                update={
                    "end_date": None,
                    "duration_months": None,
                    "duration_label": duration_label,
                }
            )
        )

    candidates = [
        candidate
        for candidate in _duration_month_candidates(title_hint)
        if not _duration_label_is_experience_requirement(title_hint, candidate[2])
        and not _duration_is_auxiliary_period(title_hint, candidate[1], candidate[2])
    ]
    durations = {months for months, *_ in candidates}
    if len(durations) != 1:
        return finalize(timing)
    if body_duration_is_authoritative and (
        timing.duration_months is not None or timing.duration_label is not None
    ):
        return finalize(timing)
    duration_months = next(iter(durations))
    update: dict[str, object] = {"duration_months": duration_months}
    if timing.start_date:
        start_year, start_month = map(int, timing.start_date.split("-")[:2])
        update["end_date"] = _end_date_for_month_span(
            start_year,
            start_month,
            duration_months,
        )
    return finalize(timing.model_copy(update=update))


def _title_hint_month_year_term(title_hint: str) -> tuple[str, str] | None:
    if any(
        field.label in _EMPLOYMENT_START_FIELD_LABELS
        for field in _posting_field_spans(title_hint)
    ):
        return None
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    if re.search(
        rf"\b(?:{month_pattern})\.?\s+(?:or|and|/)\s+"
        rf"(?:{month_pattern})\.?\s+20\d{{2}}\b",
        title_hint,
        flags=re.IGNORECASE,
    ):
        return None
    starts = {
        parsed_date
        for _start, _end, _raw_label, parsed_date in _month_year_start_candidates(
            title_hint
        )
    }
    if len(starts) != 1:
        return None
    start_date = next(iter(starts))
    start_year, start_month = map(int, start_date.split("-")[:2])
    raw_label = _academic_term_label_for_month_year_start(start_month, start_year)
    if raw_label is None:
        return None
    return raw_label, start_date


def _title_hint_academic_term(title_hint: str) -> tuple[str, str] | None:
    academic_terms = list(
        re.finditer(
            rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b",
            title_hint,
            flags=re.IGNORECASE,
        )
    )
    term_keys = {
        _canonical_term_key(match.group("term")) for match in academic_terms
    }
    if len(term_keys) != 1:
        return None

    claims = {
        (_canonical_term_key(match.group("term")), int(match.group("year")))
        for match in re.finditer(
            rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b(?:\s+(?:term|semester)\b)?\s*,?\s*"
            r"(?P<year>20\d{2})\b",
            title_hint,
            flags=re.IGNORECASE,
        )
    }
    for match in re.finditer(
        rf"\b(?P<year>20\d{{2}})\b[^()\n]{{0,80}}?"
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b",
        title_hint,
        flags=re.IGNORECASE,
    ):
        if re.match(
            r"\s*,?\s*20\d{2}\b",
            title_hint[match.end() :],
        ):
            continue
        claims.add(
            (
                _canonical_term_key(match.group("term")),
                int(match.group("year")),
            )
        )
    for match in re.finditer(
        rf"\(\s*(?P<term>{ACADEMIC_TERM_PATTERN})\s+"
        r"(?P<year>\d{2})\s*\)"
        r"[^\n]{0,80}\b(?:intern(?:ship)?|co-?op|student)\b",
        title_hint,
        flags=re.IGNORECASE,
    ):
        claims.add(
            (
                _canonical_term_key(match.group("term")),
                2000 + int(match.group("year")),
            )
        )
    if len(claims) != 1:
        return None
    term_key, year = next(iter(claims))
    if term_key not in term_keys:
        return None
    start_term, _duration_months = _academic_term_defaults(term_key)
    start_month = ACADEMIC_TERM_MONTHS[start_term][0]
    return (
        f"{_titlecase_term_label(term_key)} {year}",
        f"{year:04d}-{start_month:02d}-01",
    )


def _has_explicit_body_duration_evidence(
    raw_text: str,
    timing: ParsedTiming,
) -> bool:
    evidence = _employment_timing_evidence(raw_text)
    if evidence.has_duration_terms or timing.duration_label is not None:
        return True
    if evidence.has_start_dates and evidence.has_end_dates:
        return True
    return any(
        assessment.context_type == "employment_timing"
        for assessment in _employment_month_range_assessments(raw_text)
    )


def _title_hint_month_range_timing(title_hint: str) -> ParsedTiming | None:
    title_segment = f"Title: {title_hint}"
    assessments = [
        _classify_month_range_timing_candidate(candidate)
        for candidate in _extract_month_range_timing_candidates(title_segment)
    ]
    candidates = [
        assessment.candidate
        for assessment in assessments
        if assessment.context_type == "employment_timing"
    ]
    if len(candidates) != 1:
        return None
    return _parsed_timing_from_month_range_candidate(candidates[0])


def _title_hint_month_alternative_term(title_hint: str) -> str | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    matches = list(
        re.finditer(
            rf"\b(?P<first>{month_pattern})\.?\s+(?:or|and|/)\s+"
            rf"(?P<second>{month_pattern})\.?\s+(?P<year>20\d{{2}})\b",
            title_hint,
            flags=re.IGNORECASE,
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    year = int(match.group("year"))
    labels = [
        _academic_term_label_for_month_year_start(
            MONTH_NAMES[match.group(group).casefold()],
            year,
        )
        for group in ("first", "second")
    ]
    if None in labels or labels[0] == labels[1]:
        return None
    return f"{labels[0].split()[0]}/{labels[1].split()[0]} {year}"


def _title_hint_timing_ambiguities(
    title_hint: str | None,
) -> tuple[str, ...]:
    if not title_hint or not _title_text_is_safe_timing_authority(title_hint):
        return ()
    if _title_hint_month_alternative_term(title_hint) is not None:
        return ("employment_timing.start_date",)
    return ()


def _authoritative_title_timing_fields(title: str | None) -> frozenset[str]:
    if (
        not title
        or not _title_text_is_safe_timing_authority(title)
        or _title_hint_month_alternative_term(title) is not None
    ):
        return frozenset()
    if _title_hint_month_range_timing(title) is not None:
        return frozenset(
            {
                "employment_timing.start_date",
                "employment_timing.end_date",
                "employment_timing.duration_months",
            }
        )

    fields: set[str] = set()
    if _title_hint_month_year_term(title) or _title_hint_academic_term(title):
        fields.update(
            {
                "employment_timing.start_date",
                "employment_timing.end_date",
            }
        )
    return frozenset(fields)


def _source_title_has_authoritative_term(title: str | None) -> bool:
    if (
        not title
        or len(title) > 180
        or "." in title
        or "\n" in title
        or not _title_text_is_safe_timing_authority(title)
    ):
        return False
    return bool(
        _title_hint_month_range_timing(title)
        or _title_hint_month_year_term(title)
        or _title_hint_academic_term(title)
    )


def _title_text_is_safe_timing_authority(title: str) -> bool:
    return not bool(
        re.search(
            r"\b(?:anticipated\s+|expected\s+|target\s+)?"
            r"(?:start\s+date|end\s+date|duration|term\s+length)\s*:",
            title,
            flags=re.IGNORECASE,
        )
    )


def _exact_title_duration_months(title: str) -> int | None:
    if list(_alternative_duration_matches(title)):
        return None
    if not re.search(
        r"\b(?:analyst|developer|engineer|intern(?:ship)?|co-?op|student)\b",
        title,
        flags=re.IGNORECASE,
    ):
        return None
    if not re.search(
        r"(?:[-\u2013\u2014,(]\s*)\d{1,2}\s*-?months?\)?\s*$",
        title,
        flags=re.IGNORECASE,
    ):
        return None
    durations = {
        months
        for months, _start, end, _raw_label in _duration_month_candidates(title)
        if not _duration_label_is_experience_requirement(title, end)
    }
    return next(iter(durations)) if len(durations) == 1 else None


def _source_role_title_month_year_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    candidates: list[ParsedTimingCandidate] = []
    for line in _posting_content_lines(raw_text):
        label = _field_label(line)
        if label == "ingest title hint":
            continue
        # Discovery adapters retain the complete title in qualified Role fields.
        is_qualified_role = _provenance_field_semantic(label) == "role"
        if is_qualified_role:
            label = "role"
        if label and label not in {"job title", "page title", "position", "role", "title"}:
            continue
        title_text = line.split(":", 1)[1].strip() if label else line.strip()
        if not title_text or len(title_text) > 180 or "." in title_text:
            continue
        if re.search(
            r"\b\d+\s+(?:minutes?|hours?|days?|weeks?|months?)\s+ago\b",
            title_text,
            flags=re.IGNORECASE,
        ):
            continue
        if not re.search(
            r"\b(?:co-?op|coop|intern(?:ship)?|student)\b",
            title_text,
            flags=re.IGNORECASE,
        ):
            continue
        month_year_starts = _month_year_start_candidates(title_text)
        if len(month_year_starts) != 1:
            continue
        durations = {
            months for months, *_ in _duration_month_candidates(title_text)
        }
        if len(durations) != 1:
            continue
        start_date = month_year_starts[0][3]
        start_year, start_month = map(int, start_date.split("-")[:2])
        term = ("Winter", "Summer", "Fall")[(start_month - 1) // 4]
        raw_label = f"{term} {start_year}"
        duration_months = next(iter(durations))
        # A discovery title supplies dates, not evidence of working hours.
        schedule_type = None if is_qualified_role else "full_time"
        candidates.append(
            ParsedTimingCandidate(
                timing=ParsedTiming(
                    raw_label=raw_label,
                    timing_type="fixed_term",
                    start_date=start_date,
                    end_date=_end_date_for_month_span(
                        start_year,
                        start_month,
                        duration_months,
                    ),
                    duration_months=duration_months,
                    schedule_type=schedule_type,
                ),
                default_schedule_type=schedule_type,
                prefer_academic_term_label=True,
            )
        )
    if len(candidates) != 1:
        return None
    return candidates[0]


def _role_header_month_year_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    lines = _posting_content_lines(raw_text)
    candidates: list[ParsedTimingCandidate] = []
    for index, line in enumerate(lines):
        if index < 2:
            continue
        dates = _month_year_start_candidates(line)
        if len(dates) != 1 or dates[0][2] != line.strip():
            continue
        role_line = lines[index - 2]
        if len(role_line) > 180 or not re.search(
            r"\b(?:co-?op|coop|intern(?:ship)?|student)\b", role_line, re.IGNORECASE,
        ):
            continue
        if re.search(
            r"\b(?:graduat\w*|enroll\w*|deadline|interview|school)\b|[.!?](?:\s|$)",
            role_line, re.IGNORECASE,
        ):
            continue
        location_line = re.sub(
            r"\s*[-\u2013\u2014]\s*(?:hybrid|remote|on[- ]?site)\s*$",
            "", lines[index - 1], flags=re.IGNORECASE,
        )
        if not looks_like_comma_separated_location(location_line):
            continue
        # This observed header is role / location / start month. It supplies no
        # duration; keep that unknown unless separate employment evidence has one.
        candidates.append(_month_year_prose_candidate(
            line, duration_months=None, default_schedule_type=None,
        ))
    starts = {candidate.timing.start_date for candidate in candidates}
    return candidates[0] if len(starts) == 1 else None


def _prose_month_alternative_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?P<first>{month_pattern})\.?(?:\s+(?P<first_year>20\d{{2}}))?,?\s+(?:or|and|/)\s+"
        rf"(?P<second>{month_pattern})\.?\s+(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    candidates: list[tuple[str, str | None]] = []
    for line in _posting_content_lines(raw_text):
        for match in pattern.finditer(line):
            prefix = line[max(0, match.start() - 180) : match.start()]
            has_start_field = bool(
                re.search(
                    r"\b(?:target|anticipated|expected)?\s*start\s+dates?\s*:\s*$",
                    prefix,
                    flags=re.IGNORECASE,
                )
            )
            has_join_cue = bool(
                re.search(
                    r"\b(?:join|joining)\b[^.;\n]{0,80}\b(?:team\s+)?in\s*$",
                    prefix,
                    flags=re.IGNORECASE,
                )
            )
            has_hiring_cohort_cue = bool(
                re.search(r"\bhiring\s+cohorts?\s+commencing\s+in\s*$", prefix, re.I)
            )
            has_start_in_cue = bool(re.search(r"\bstart\s+in\s*$", prefix, re.I))
            if not (has_start_field or has_join_cue or has_hiring_cohort_cue or has_start_in_cue):
                continue
            if not has_start_field and re.search(
                r"\b(?:graduat(?:e|ed|ing|ion)|enroll(?:ed|ment)?|"
                r"return(?:ing)?\s+to\s+school)\b",
                prefix,
                flags=re.IGNORECASE,
            ):
                continue
            year = int(match.group("year"))
            first_year = int(match.group("first_year") or year)
            if first_year != year and not has_start_in_cue:
                continue
            labels = [
                _academic_term_label_for_start_month(
                    MONTH_NAMES[match.group(group).casefold()],
                    first_year if group == "first" else year,
                )
                for group in ("first", "second")
            ]
            term_names = list(dict.fromkeys(label.split()[0] for label in labels))
            raw_label = (
                "/".join(term_names) + f" {year}"
                if first_year == year else " / ".join(labels)
            )
            end_date = None
            if has_start_in_cue:
                # Both offered starts share this explicitly stated end month.
                end = re.match(
                    rf"[^.;\n]{{0,120}}\.\s*Runs\s+through\s+"
                    rf"(?P<month>{month_pattern})\s+(?P<year>20\d{{2}})\b",
                    line[match.end():], re.I,
                )
                if end:
                    end_year = int(end["year"])
                    end_month = MONTH_NAMES[end["month"].casefold()]
                    if (end_year, end_month) >= max(
                        (first_year, MONTH_NAMES[match["first"].casefold()]),
                        (year, MONTH_NAMES[match["second"].casefold()]),
                    ):
                        end_date = (
                            f"{end_year:04d}-{end_month:02d}-"
                            f"{_last_day_of_month(end_year, end_month):02d}"
                        )
            candidates.append((raw_label, end_date))
    unique_candidates = list(dict.fromkeys(candidates))
    if len(unique_candidates) != 1:
        return None
    raw_label, end_date = unique_candidates[0]
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=raw_label,
            timing_type="fixed_term",
            end_date=end_date,
        ),
        ambiguous_fields=("employment_timing.start_date",),
    )


def _prose_month_year_start_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    candidates: list[ParsedTimingCandidate] = []
    for line in _posting_content_lines(raw_text):
        for match in re.finditer(
            rf"\blooking\s+for\s+(?:an?\s+)?(?P<term>{ACADEMIC_TERM_PATTERN})\s+"
            r"(?P<year>20\d{2})\s+(?:[A-Za-z]+[\s-]+){0,4}"
            r"(?:students?|interns?|co-?ops?)\b",
            line,
            flags=re.IGNORECASE,
        ):
            term, _duration = _academic_term_defaults(match["term"].casefold())
            year = int(match["year"])
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=f"{_titlecase_term_label(term)} {year}",
                        timing_type="fixed_term",
                        start_date=f"{year:04d}-{ACADEMIC_TERM_MONTHS[term][0]:02d}-01",
                    ),
                    prefer_academic_term_label=True,
                )
            )
        for match in re.finditer(
            rf"\b(?:anticipated|expected|target)\s+start\s+date\s+is\s+"
            rf"(?P<start>(?:{month_pattern})\.?\s+20\d{{2}})\b",
            line,
            flags=re.IGNORECASE,
        ):
            candidates.append(
                _month_year_prose_candidate(
                    match.group("start"),
                    duration_months=None,
                    default_schedule_type=None,
                )
            )

        for match in re.finditer(
            rf"\b(?:seeking|hiring|looking\s+for)\b[^.;\n]{{0,180}}\b"
            rf"(?:co-?ops?|intern(?:s|ships?)?|students?)\b[^.;\n]{{0,220}}\b"
            rf"(?:(?:starting|beginning)(?:\s+in)?|"
            rf"to\s+join\s+our\b[^.;\n]{{0,120}}\bteam\s+in)\s+"
            rf"(?P<start>(?:{month_pattern})\.?\s+20\d{{2}})\b",
            line,
            flags=re.IGNORECASE,
        ):
            candidate = _month_year_prose_candidate(
                match.group("start"),
                duration_months=None,
                default_schedule_type=None,
            )
            candidates.append(
                ParsedTimingCandidate(
                    timing=candidate.timing,
                    default_schedule_type=candidate.default_schedule_type,
                    prefer_academic_term_label=True,
                    prefer_start_date_term_label=True,
                )
            )

        for match in re.finditer(
            rf"(?P<clause>\b\d{{1,2}}[\s-]+months?\b[^.;\n]{{0,100}}"
            rf"\b(?:co-?op|intern(?:ship)?|position|placement|term)\b"
            rf"[^.;\n]{{0,80}}\b(?:starting|beginning)(?:\s+in)?\s+"
            rf"(?P<start>(?:{month_pattern})\.?\s+20\d{{2}})\b)",
            line,
            flags=re.IGNORECASE,
        ):
            durations = {
                months for months, *_ in _duration_month_candidates(match.group("clause"))
            }
            if len(durations) != 1:
                continue
            candidates.append(
                _month_year_prose_candidate(
                    match.group("start"),
                    duration_months=next(iter(durations)),
                    default_schedule_type="full_time",
                )
            )

        for match in re.finditer(
            rf"\b(?:seeking|hiring)\b[^.;\n]{{0,180}}\b"
            rf"(?:co-?op|intern(?:ship)?|student)\b[^.;\n]{{0,180}}\bfor\s+"
            rf"(?P<term>{ACADEMIC_TERM_PATTERN})\s+(?P<year>20\d{{2}})\b",
            line,
            flags=re.IGNORECASE,
        ):
            term_key = match.group("term").casefold()
            if term_key == "autumn":
                term_key = "fall"
            start_term, duration_months = _academic_term_defaults(term_key)
            start_month = ACADEMIC_TERM_MONTHS[start_term][0]
            year = int(match.group("year"))
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=f"{_titlecase_term_label(term_key)} {year}",
                        timing_type="fixed_term",
                        start_date=f"{year:04d}-{start_month:02d}-01",
                        end_date=_end_date_for_month_span(
                            year,
                            start_month,
                            duration_months,
                        ),
                        duration_months=duration_months,
                    ),
                    default_schedule_type="full_time",
                    prefer_academic_term_label=True,
                )
            )

    starts = {
        candidate.timing.start_date
        for candidate in candidates
        if candidate.timing.start_date is not None
    }
    if len(starts) != 1:
        return None
    matching = [
        candidate
        for candidate in candidates
        if candidate.timing.start_date in starts
    ]
    return max(
        matching,
        key=lambda candidate: (
            candidate.timing.duration_months is not None,
            candidate.prefer_academic_term_label,
        ),
    )


def _scheduled_start_date_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    patterns = (
        re.compile(
            rf"\b(?:the\s+)?scheduled\s+start\s+date\s+for\s+(?:this\s+)?"
            rf"(?:position|role|internship|co-?op)\s+is\s+"
            rf"(?P<date>{_posting_date_text_pattern()})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\b(?:replacement\s+)?(?:co-?op\s+|intern(?:ship)?\s+|student\s+)*"
            rf"(?:position|role)\s*,?\s*starting\s+"
            rf"(?P<date>{_posting_date_text_pattern()})",
            flags=re.IGNORECASE,
        ),
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for pattern in patterns:
            for match in pattern.finditer(segment):
                start_date = _parse_posting_date(match.group("date"))
                if start_date is None:
                    continue
                year, month, day = map(int, start_date.split("-"))
                candidates.append(
                    ParsedTimingCandidate(
                        timing=ParsedTiming(
                            raw_label=_academic_term_label_for_start_date(
                                year,
                                month,
                                day,
                            ),
                            timing_type="fixed_term",
                            start_date=start_date,
                        ),
                        prefer_start_date_term_label=True,
                    )
                )
    starts = {candidate.timing.start_date for candidate in candidates}
    return candidates[0] if len(starts) == 1 else None


def _preferred_duration_month_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\bpreferably\s+(?P<duration>\d{{1,2}})\s+months?\s*:\s*"
        rf"(?P<start>(?P<start_month>{month_pattern})\.?\s+"
        rf"(?P<start_year>20\d{{2}}))\s*[-\u2013\u2014]\s*"
        rf"(?P<end>(?P<end_month>{month_pattern})\.?\s+"
        rf"(?P<end_year>20\d{{2}}))\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            start_year = int(match.group("start_year"))
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            end_year = int(match.group("end_year"))
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            start_date = f"{start_year:04d}-{start_month:02d}-01"
            end_date = f"{end_year:04d}-{end_month:02d}-{_last_day_of_month(end_year, end_month):02d}"
            duration_months = int(match.group("duration"))
            if not _date_range_supports_duration(
                start_date,
                end_date,
                duration_months,
            ):
                continue
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=_clean_field_value(
                            segment[match.start("start") : match.end("end")]
                        ),
                        timing_type="fixed_term",
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=duration_months,
                        schedule_type="full_time",
                    ),
                    default_schedule_type="full_time",
                    authoritative_date_range=True,
                    authoritative_duration=True,
                )
            )
    signatures = {
        (
            candidate.timing.start_date,
            candidate.timing.end_date,
            candidate.timing.duration_months,
        )
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _approximate_commence_conclude_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?:position|role|internship|co-?op|term|residency)\b"
        rf"[^.;\n]{{0,50}}\bcommenc(?:e|es|ing)\s+in\s+"
        rf"(?:mid[-\s]*to[-\s]*late\s+|mid[-\s]*|late\s+)?"
        rf"(?P<start_month>{month_pattern})\.?\s+(?P<start_year>20\d{{2}})"
        rf"[^.;\n]{{0,80}}\bconclud(?:e|es|ing)\s+by\s+(?:the\s+)?end\s+of\s+"
        rf"(?P<end_month>{month_pattern})\.?\s+(?P<end_year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            start_year = int(match.group("start_year"))
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            end_year = int(match.group("end_year"))
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            start_date = f"{start_year:04d}-{start_month:02d}-01"
            end_date = f"{end_year:04d}-{end_month:02d}-{_last_day_of_month(end_year, end_month):02d}"
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=(
                            f"{match.group('start_month')} {start_year} to "
                            f"{match.group('end_month')} {end_year}"
                        ),
                        timing_type="fixed_term",
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=_duration_months_from_date_range(
                            start_date,
                            end_date,
                        ),
                    ),
                    authoritative_date_range=True,
                )
            )
    signatures = {
        (candidate.timing.start_date, candidate.timing.end_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _labeled_exact_date_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![A-Za-z])(?:"
        rf"(?P<label>position\s+term|term\s+date|term|timeline|internship\s+dates?)"
        rf"\s*:\s*(?P<schedule>full[-\s]?time\s*,\s*)?(?:from\s+)?|"
        rf"date\s*:\s*(?:-\s*)?\(?\s*"
        rf"(?P<academic_term>{ACADEMIC_TERM_PATTERN})\s+term\s*"
        rf")"
        rf"(?P<start_month>{month_pattern})\.?\s+"
        rf"(?P<start_day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s*,?\s*"
        rf"(?P<start_year>20\d{{2}})?\s*"
        rf"(?P<separator>to|till|through|[-\u2013\u2014\ufffd])\s*"
        rf"(?P<end_month>{month_pattern})\.?\s+"
        rf"(?P<end_day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s*,?\s*"
        rf"(?P<end_year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    candidate_segments = [
        (segment, False) for segment in _timing_candidate_segments(raw_text)
    ]
    candidate_segments.append((raw_text, True))
    for segment, academic_date_only in candidate_segments:
        for match in pattern.finditer(segment):
            if academic_date_only and not match.group("academic_term"):
                continue
            label = (match.group("label") or "").casefold()
            if label == "timeline" and not match.group("schedule"):
                continue
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            start_day = int(match.group("start_day"))
            end_year = int(match.group("end_year"))
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            end_day = int(match.group("end_day"))
            start_year_text = match.group("start_year")
            start_year = int(start_year_text or end_year)
            if start_year_text is None and (start_month, start_day) > (
                end_month,
                end_day,
            ):
                start_year -= 1
            start_date = _valid_date(start_year, start_month, start_day)
            end_date = _valid_date(end_year, end_month, end_day)
            if start_date is None or end_date is None or start_date > end_date:
                continue
            elapsed_months = (end_year - start_year) * 12 + end_month - start_month
            duration_months = elapsed_months + (1 if end_day > start_day else 0)
            if not 1 <= duration_months <= 24:
                continue
            academic_term = match.group("academic_term")
            nearby_academic_term = None
            if not academic_term and label == "term":
                nearby_claims = list(
                    re.finditer(
                        rf"(?<!\d)(?P<year>20\d{{2}})\s+"
                        rf"(?P<term>{ACADEMIC_TERM_PATTERN})\s+term\b",
                        segment[max(0, match.start() - 240) : match.start()],
                        flags=re.IGNORECASE,
                    )
                )
                if nearby_claims:
                    nearby_academic_term = nearby_claims[-1]
            raw_label = (
                f"{_titlecase_term_label(academic_term)} {start_year}"
                if academic_term
                else (
                    f"{_titlecase_term_label(nearby_academic_term.group('term'))} "
                    f"{nearby_academic_term.group('year')}"
                    if nearby_academic_term
                    else _clean_field_value(
                        segment[match.start("start_month") : match.end("end_year")]
                    )
                )
            )
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=raw_label,
                        timing_type="fixed_term",
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=duration_months,
                        schedule_type="full_time",
                    ),
                    default_schedule_type="full_time",
                    prefer_academic_term_label=label == "term",
                    authoritative_date_range=True,
                    authoritative_duration=True,
                )
            )
    signatures = {
        (candidate.timing.start_date, candidate.timing.end_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _labeled_invalid_end_date_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    date_pattern = _posting_date_text_pattern()
    pattern = re.compile(
        rf"\b(?P<label>expected\s+start\s+date\s+to\s+end\s+date)\s*:\s*"
        rf"(?P<start>{date_pattern})\s*"
        rf"(?P<separator>to|through|[-\u2013\u2014\ufffd])\s*"
        rf"(?P<end>{date_pattern})",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            start_date = _parse_posting_date(match.group("start"))
            end_date = _parse_posting_date(match.group("end"))
            if start_date is None or end_date is not None:
                continue
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=_clean_field_value(match.group(0)),
                        timing_type="fixed_term",
                        start_date=start_date,
                    ),
                    ambiguous_fields=("employment_timing.end_date",),
                )
            )
    signatures = {
        (candidate.timing.raw_label, candidate.timing.start_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _role_owned_exact_date_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    date_pattern = _posting_date_text_pattern()
    patterns = (
        re.compile(
            rf"\b(?:co-op\s+program\s+period\s*:\s*|"
            rf"available\s+for\s+a\s+full-time\s+commitment\s+from\s+)"
            rf"(?P<start>{date_pattern})\s*(?P<separator>to|[-–—])\s*"
            rf"(?P<end>{date_pattern})", re.I,
        ),
        re.compile(
            rf"\btemporary\s+student\s+role\s+available\s+"
            rf"(?P<start>{date_pattern})\s*,?\s*"
            rf"(?P<separator>through|to|until|[-\u2013\u2014])\s+"
            rf"(?P<end>{date_pattern})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\bemployment\s+tenure\b[^\n]{{0,220}}?"
            rf"(?P<start>{date_pattern})\s*"
            rf"(?:\([^)]{{1,100}}\)\s*)?"
            rf",?\s*"
            rf"(?P<separator>through|to|until|[-\u2013\u2014])\s+"
            rf"(?P<end>{date_pattern})",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"\blooking\s+for\s+an?\b[^.;\n]{{0,220}}?"
            rf"\b(?:intern(?:ship)?|co-?op|student)\b[^.;\n]{{0,220}}?"
            rf"\bfor\s+the\s+period\s+of\s+"
            rf"(?P<start>{date_pattern})\s*,?\s*"
            rf"(?P<separator>through|to|until|[-\u2013\u2014])\s+"
            rf"(?P<end>{date_pattern})",
            flags=re.IGNORECASE,
        ),
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for pattern in patterns:
            for match in pattern.finditer(segment):
                start_date = _parse_posting_date(match.group("start"))
                end_date = _parse_posting_date(match.group("end"))
                if not start_date or not end_date or start_date > end_date:
                    continue
                start_year, start_month, start_day = map(int, start_date.split("-"))
                end_year, end_month, end_day = map(int, end_date.split("-"))
                duration_months = (end_year - start_year) * 12 + end_month - start_month
                if end_day > start_day:
                    duration_months += 1
                period_duration = re.search(
                    r"(?P<months>\d{1,2})\s+months?\s*$",
                    segment[max(0, match.start() - 25):match.start()], re.I,
                ) if match[0].lower().startswith('co-op program period') else None
                if period_duration:
                    duration_months = int(period_duration['months'])
                if not 1 <= duration_months <= 24:
                    continue
                candidates.append(
                    ParsedTimingCandidate(
                        timing=ParsedTiming(
                            raw_label=_clean_field_value(
                                f"{match.group('start')} "
                                f"{match.group('separator')} {match.group('end')}"
                            ),
                            timing_type="fixed_term",
                            start_date=start_date,
                            end_date=end_date,
                            duration_months=duration_months,
                        ),
                        authoritative_date_range=True,
                        authoritative_duration=True,
                    )
                )
    signatures = {
        (candidate.timing.start_date, candidate.timing.end_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _role_runs_shared_year_month_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?:runs?|would\s+work\s+from)\s+"
        rf"(?P<range>(?P<start_month>{month_pattern})\.?\s+"
        rf"(?:to|through|[-\u2013\u2014])\s+"
        rf"(?P<end_month>{month_pattern})\.?\s+(?P<year>20\d{{2}}))\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            ownership_context = segment[max(0, match.start() - 220) : match.start()]
            immediate_ownership_context = segment[
                max(0, match.start() - 100) : match.start()
            ]
            if re.search(
                r"\b(?:posting\s+dates?|application\s+(?:dates?|period|window)|"
                r"recruiting\s+cycle)\b",
                immediate_ownership_context,
                flags=re.IGNORECASE,
            ):
                continue
            if not re.search(
                r"\b(?:fixed[-\s]?term|co-?op|internship|work\s+term)\b",
                ownership_context,
                flags=re.IGNORECASE,
            ):
                continue
            year = int(match.group("year"))
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            end_year = year if end_month >= start_month else year + 1
            start_date = f"{year:04d}-{start_month:02d}-01"
            end_date = (
                f"{end_year:04d}-{end_month:02d}-"
                f"{_last_day_of_month(end_year, end_month):02d}"
            )
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=_clean_field_value(match.group("range")),
                        timing_type="fixed_term",
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=_duration_months_from_date_range(
                            start_date,
                            end_date,
                        ),
                    ),
                    default_schedule_type="full_time",
                    authoritative_date_range=True,
                    authoritative_duration=True,
                )
            )
    signatures = {
        (candidate.timing.start_date, candidate.timing.end_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _program_runs_year_duration_yearless_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?P<year>20\d{{2}})\s+Canada\s+"
        rf"(?:(?:(?P<research>Research\s+intern)|Internship)\s+)?program\s+runs\s+for\s+"
        rf"(?P<weeks>\d{{1,2}})\s+weeks?\s*\(\s*"
        rf"(?P<start_month>{month_pattern})\.?\s+(?P<start_day>\d{{1,2}})"
        rf"(?:st|nd|rd|th)?\s*"
        rf"(?:to|through|[-\u2013\u2014])\s*"
        rf"(?P<end_month>{month_pattern})\.?\s+(?P<end_day>\d{{1,2}})"
        rf"(?:st|nd|rd|th)?(?:,?\s+(?P<end_year>20\d{{2}}))?\s*\)",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            year = int(match.group("year"))
            weeks = int(match.group("weeks"))
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            start_day = int(match.group("start_day"))
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            end_day = int(match.group("end_day"))
            end_year = (
                year if (end_month, end_day) >= (start_month, start_day) else year + 1
            )
            if match.group("end_year") and not match.group("research"):
                # Existing internship ranges with their own year keep their
                # approved source label through the general date-range parser.
                continue
            if match.group("end_year") and int(match.group("end_year")) != end_year:
                continue
            start_date = _valid_date(year, start_month, start_day)
            end_date = _valid_date(end_year, end_month, end_day)
            if start_date is None or end_date is None:
                continue
            duration_months = (end_year - year) * 12 + end_month - start_month
            if end_day > start_day:
                duration_months += 1
            declared_duration_months = max(1, round(weeks / 4))
            if match.group("research"):
                # Research programs can span partial calendar months. Retain
                # the declared week duration while using their explicit dates.
                elapsed_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
                if abs(elapsed_days - weeks * 7) > 7:
                    continue
                duration_months = declared_duration_months
            if (
                duration_months is None
                or duration_months != declared_duration_months
                or not 1 <= duration_months <= 24
            ):
                continue
            raw_label = (
                f"Fall {year}"
                if start_month == 8
                else _academic_term_label_for_start_month(start_month, year)
            )
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=raw_label,
                        timing_type="fixed_term",
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=duration_months,
                    ),
                    authoritative_date_range=True,
                    authoritative_duration=True,
                )
            )
    signatures = {
        (candidate.timing.start_date, candidate.timing.end_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _ability_to_work_month_year_start_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\bability\s+to\s+work\s+(?P<schedule>full[-\s]?time|part[-\s]?time)"
        rf"[^.;\n]{{0,80}}?\bstarting\s+"
        rf"(?P<month>{month_pattern})\.?\s*,?\s*"
        rf"(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            year = int(match.group("year"))
            month = MONTH_NAMES[match.group("month").casefold()]
            start_date = f"{year:04d}-{month:02d}-01"
            schedule_type = _schedule_type_from_token(match.group("schedule"))
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=(
                            f"Fall {year}"
                            if month == 8
                            else _academic_term_label_for_start_month(month, year)
                        ),
                        timing_type="fixed_term",
                        start_date=start_date,
                    ),
                    schedule_resolution=ScheduleEvidenceResolution(
                        positive_values=frozenset({schedule_type})
                    ),
                    authoritative_duration=True,
                )
            )
    starts = {candidate.timing.start_date for candidate in candidates}
    return candidates[0] if len(starts) == 1 else None


def _term_exact_start_open_duration_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    pattern = re.compile(
        rf"\bterm\s+(?P<start>{_posting_date_text_pattern()})\s+"
        r"(?P<label>up\s+to\s+(?P<duration>\d{1,2})\s+months?)\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            start_date = _parse_posting_date(match.group("start"))
            duration_option = int(match.group("duration"))
            if not start_date or not 1 <= duration_option <= 24:
                continue
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        timing_type="fixed_term",
                        start_date=start_date,
                        duration_label=_clean_field_value(match.group("label")),
                    ),
                    default_schedule_type="full_time",
                    alternative_duration=True,
                    prefer_start_date_term_label=True,
                    duration_options=frozenset({duration_option}),
                    ambiguous_fields=("employment_timing.duration_months",),
                )
            )
    starts = {candidate.timing.start_date for candidate in candidates}
    return candidates[0] if len(starts) == 1 else None


def _labeled_duration_month_range_alternatives_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?P<duration>\d{{1,2}})(?P<unit_separator>[-\s]+)months?"
        rf"(?P<term>\s+term)?(?:\s*\([^)]{{1,40}}\))?\s*:\s*"
        rf"(?P<start_month>{month_pattern})\.?\s+(?P<start_year>20\d{{2}})\s*"
        rf"(?:to|[-\u2013\u2014])\s*"
        rf"(?P<end_month>{month_pattern})\.?\s+(?P<end_year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    candidates: list[tuple[int, ParsedTimingCandidate]] = []
    candidate_segments = _timing_candidate_segments(raw_text)
    candidate_segments.append(" ".join(raw_text.split()))
    for segment in dict.fromkeys(candidate_segments):
        options: set[int] = set()
        starts: set[str] = set()
        ranges: list[tuple[str, str, int]] = []
        has_hyphenated_term = False
        has_term_label = False
        inconsistent_duration = False
        for match in pattern.finditer(segment):
            duration = int(match.group("duration"))
            start_year = int(match.group("start_year"))
            start_month = MONTH_NAMES[match.group("start_month").casefold()]
            end_year = int(match.group("end_year"))
            end_month = MONTH_NAMES[match.group("end_month").casefold()]
            start_date = f"{start_year:04d}-{start_month:02d}-01"
            end_date = (
                f"{end_year:04d}-{end_month:02d}-"
                f"{_last_day_of_month(end_year, end_month):02d}"
            )
            if end_date < start_date or not 1 <= duration <= 24:
                continue
            inconsistent_duration = inconsistent_duration or not _date_range_supports_duration(
                start_date, end_date, duration,
            )
            options.add(duration)
            starts.add(start_date)
            ranges.append((start_date, end_date, duration))
            has_hyphenated_term = has_hyphenated_term or (
                "-" in match.group("unit_separator")
                and bool(
                    re.search(
                        r"\bmonths?\s+term\b",
                        match.group(0),
                        flags=re.IGNORECASE,
                    )
                )
            )
            has_term_label = has_term_label or bool(match.group("term"))
        if len(starts) != 1 or not ranges:
            continue
        start_date = next(iter(starts))
        year, month = map(int, start_date.split("-")[:2])
        if len(options) == 1:
            if not has_hyphenated_term or inconsistent_duration:
                continue
            _range_start, end_date, duration = ranges[0]
            candidates.append(
                (
                    1,
                    ParsedTimingCandidate(
                        timing=ParsedTiming(
                            raw_label=_academic_term_label_for_start_month(month, year),
                            timing_type="fixed_term",
                            start_date=start_date,
                            end_date=end_date,
                            duration_months=duration,
                        ),
                        default_schedule_type="full_time",
                        prefer_start_date_term_label=True,
                        authoritative_date_range=True,
                        authoritative_duration=True,
                    ),
                )
            )
            continue

        open_maximum = re.search(
            r"\bup\s+to\s+(?P<duration>\d{1,2})\s+months?\s+"
            r"(?:will\s+be\s+)?considered\b",
            segment,
            flags=re.IGNORECASE,
        )
        open_maximum_value = int(open_maximum.group("duration")) if open_maximum else None
        if open_maximum_value is not None and 1 <= open_maximum_value <= 24:
            options.add(open_maximum_value)
        sorted_options = sorted(options)
        if open_maximum_value is not None and open_maximum_value in options:
            fixed_options = [option for option in sorted_options if option != open_maximum_value]
            duration_label = (
                ", ".join(str(option) for option in fixed_options)
                + f", or up to {open_maximum_value} months"
            )
        elif len(sorted_options) == 2:
            duration_label = f"{sorted_options[0]} or {sorted_options[1]} months"
        else:
            duration_label = (
                ", ".join(str(option) for option in sorted_options[:-1])
                + f", or {sorted_options[-1]} months"
            )
        candidates.append(
            (
                len(options),
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=_academic_term_label_for_start_month(month, year),
                        timing_type="fixed_term",
                        start_date=start_date,
                        duration_label=duration_label,
                    ),
                    default_schedule_type=(
                        "full_time" if has_term_label else None
                    ),
                    alternative_duration=True,
                    prefer_start_date_term_label=True,
                    duration_options=frozenset(options),
                    ambiguous_fields=("employment_timing.duration_months", "employment_timing.end_date")
                    if inconsistent_duration else ("employment_timing.duration_months",),
                ),
            )
        )
    if not candidates:
        return None
    best_score = max(score for score, _candidate in candidates)
    best_candidates = [candidate for score, candidate in candidates if score == best_score]
    signatures = {
        (
            candidate.timing.start_date,
            candidate.timing.end_date,
            candidate.duration_options,
        )
        for candidate in best_candidates
    }
    return best_candidates[0] if len(signatures) == 1 else None


def _listed_duration_opportunities_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    pattern = re.compile(
        r"\b(?P<first>\d{1,2})\s+months?\s*,\s*"
        r"(?P<second>\d{1,2})\s+months?\s+(?:and|or)\s+"
        r"(?P<third>\d{1,2})\s+months?\s+"
        r"opportunities\s+available\b",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            options = {
                int(match.group("first")),
                int(match.group("second")),
                int(match.group("third")),
            }
            if len(options) != 3 or not all(1 <= option <= 24 for option in options):
                continue
            sorted_options = sorted(options)
            duration_label = (
                f"{sorted_options[0]}, {sorted_options[1]}, or "
                f"{sorted_options[2]} month"
            )
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=duration_label,
                        timing_type="fixed_term",
                        duration_label=duration_label,
                    ),
                    alternative_duration=True,
                    default_schedule_type="full_time",
                    duration_options=frozenset(options),
                    ambiguous_fields=("employment_timing.duration_months",),
                )
            )
    signatures = {candidate.duration_options for candidate in candidates}
    return candidates[0] if len(signatures) == 1 else None


def _multi_season_exact_duration_matrix_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    date_pattern = _posting_date_text_pattern()
    pattern = re.compile(
        rf"(?:(?P<term>winter|summer|fall)\s+duration\s*:?\s+)?"
        rf"(?P<duration>\d{{1,2}})\s+months?\s*:\s*"
        rf"(?P<start>{date_pattern})\s+to\s+(?P<end>{date_pattern})",
        flags=re.IGNORECASE,
    )
    candidates: list[ParsedTimingCandidate] = []
    candidate_segments = _timing_candidate_segments(raw_text)
    candidate_segments.append(" ".join(raw_text.split()))
    for segment in dict.fromkeys(candidate_segments):
        terms: list[str] = []
        years: set[int] = set()
        starts: set[str] = set()
        options: set[int] = set()
        current_term: str | None = None
        valid_entries = 0
        for match in pattern.finditer(segment):
            if match.group("term"):
                current_term = match.group("term").casefold()
                if current_term not in terms:
                    terms.append(current_term)
            if current_term is None:
                continue
            start_date = _parse_posting_date(match.group("start"))
            end_date = _parse_posting_date(match.group("end"))
            duration = int(match.group("duration"))
            if (
                not start_date
                or not end_date
                or not _date_range_supports_duration(start_date, end_date, duration)
            ):
                continue
            years.add(int(start_date[:4]))
            starts.add(start_date)
            options.add(duration)
            valid_entries += 1
        if len(terms) < 2 or len(years) != 1 or len(starts) < 2 or valid_entries < 3:
            continue
        year = next(iter(years))
        term_label = ", ".join(term.title() for term in terms[:-1])
        if term_label:
            term_label += f", or {terms[-1].title()} {year}"
        else:
            term_label = f"{terms[-1].title()} {year}"
        duration_label = " or ".join(str(option) for option in sorted(options)) + " Months"
        candidates.append(
            ParsedTimingCandidate(
                timing=ParsedTiming(
                    raw_label=term_label,
                    timing_type="fixed_term",
                    duration_label=duration_label,
                ),
                default_schedule_type="full_time",
                alternative_duration=True,
                duration_options=frozenset(options),
                authoritative_duration=True,
                ambiguous_fields=(
                    "employment_timing.start_date",
                    "employment_timing.duration_months",
                ),
            )
        )
    signatures = {
        (candidate.timing.raw_label, candidate.duration_options)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _seasonal_title_year_only_start_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    pattern = re.compile(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b[^.;\n]{{0,120}}"
        rf"\b(?:intern(?:ship)?|co-?op|student|resident)\b[^.;\n]{{0,120}}"
        rf"\b(?P<year>20\d{{2}})\s+start\s+date\b",
        flags=re.IGNORECASE,
    )
    lines = list(_posting_content_lines(raw_text))
    title_candidates: list[str] = []
    parsed_title = _title_value(lines)
    if parsed_title:
        title_candidates.append(parsed_title)
    first_source_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _field_label(line) != "ingest title hint"
        ),
        -1,
    )
    for index, line in enumerate(lines):
        label = _field_label(line)
        if index != first_source_index and label not in {
            "job title",
            "page title",
            "position",
            "role",
            "title",
        }:
            continue
        title_text = line.split(":", 1)[1].strip() if label else line.strip()
        if title_text and _title_from_mixed_line(title_text):
            title_candidates.append(title_text)
    candidates: list[ParsedTimingCandidate] = []
    for title in dict.fromkeys(title_candidates):
        for match in pattern.finditer(title):
            term_key = _canonical_term_key(match.group("term"))
            if term_key not in ACADEMIC_TERM_MONTHS:
                continue
            year = int(match.group("year"))
            start_term, duration_months = _academic_term_defaults(term_key)
            start_month = ACADEMIC_TERM_MONTHS[start_term][0]
            candidates.append(
                ParsedTimingCandidate(
                    timing=ParsedTiming(
                        raw_label=f"{_titlecase_term_label(term_key)} {year}",
                        timing_type="fixed_term",
                        start_date=f"{year:04d}-{start_month:02d}-01",
                        end_date=_end_date_for_month_span(
                            year,
                            start_month,
                            duration_months,
                        ),
                        duration_months=duration_months,
                        schedule_type="full_time",
                    ),
                    default_schedule_type="full_time",
                    prefer_academic_term_label=True,
                )
            )
    signatures = {
        (candidate.timing.raw_label, candidate.timing.start_date)
        for candidate in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _intern_availability_month_alternative_candidate(
    raw_text: str,
    duration_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    if duration_candidate is None or duration_candidate.timing.start_date:
        return None
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        r"\bfor\s+\d{1,2}[\s-]+months?\s+internships?\s*:\s*"
        r"must\s+be\s+available\s+"
        rf"(?P<first>{month_pattern})\.?\s*(?:\bor\b|/)\s*"
        rf"(?P<second>{month_pattern})\.?\s+(?P<year>20\d{{2}})\b"
        rf"\s*[-\u2013\u2014]\s*(?:{month_pattern})\.?\s*(?:\bor\b|/)\s*"
        rf"(?:{month_pattern})\.?\s+20\d{{2}}\b",
        flags=re.IGNORECASE,
    )
    labels: set[str] = set()
    for line in _posting_content_lines(raw_text):
        for match in pattern.finditer(line):
            year = int(match.group("year"))
            terms = list(dict.fromkeys(
                _academic_term_label_for_start_month(
                    MONTH_NAMES[match.group(group).casefold()], year,
                ).split()[0]
                for group in ("first", "second")
            ))
            labels.add("/".join(terms) + f" {year}")
    if len(labels) != 1:
        return None
    # The start term is known even when neither endpoint has a unique month.
    # Retain the separately parsed duration, including alternative durations.
    return replace(
        duration_candidate,
        timing=duration_candidate.timing.model_copy(update={"raw_label": labels.pop()}),
        ambiguous_fields=_merge_ambiguous_fields(
            duration_candidate.ambiguous_fields,
            ("employment_timing.start_date", "employment_timing.end_date"),
        ),
    )


def _hiring_season_month_window_candidate(
    raw_text: str,
    duration_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    """A hiring window names the term without fixing an internship endpoint."""
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    claims: set[str] = set()
    for line in _posting_content_lines(raw_text):
        for match in re.finditer(
            rf"\b(?:this|the)\s+position\s+is\s+(?:currently\s+)?hiring\s+for\s+"
            rf"(?P<term>{ACADEMIC_TERM_PATTERN})\s*\(\s*"
            rf"(?P<first>{month_pattern})\.?\s*[-–—]\s*"
            rf"(?P<second>{month_pattern})\.?\s*,?\s*(?P<year>20\d{{2}})\s*\)",
            line,
            flags=re.IGNORECASE,
        ):
            label = f"{_titlecase_term_label(match['term'])} {match['year']}"
            if all(
                _academic_term_label_for_start_month(
                    MONTH_NAMES[match[name].casefold()], int(match['year'])
                ) == label
                for name in ("first", "second")
            ):
                claims.add(label)
    if len(claims) != 1:
        return None
    candidate = duration_candidate or ParsedTimingCandidate(
        timing=ParsedTiming(timing_type="fixed_term")
    )
    if candidate.timing.start_date or candidate.timing.end_date:
        return None
    return replace(
        candidate,
        timing=candidate.timing.model_copy(update={"raw_label": claims.pop()}),
        ambiguous_fields=_merge_ambiguous_fields(
            candidate.ambiguous_fields,
            ("employment_timing.start_date", "employment_timing.end_date"),
        ),
    )


def _availability_month_year_start_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    candidates: list[ParsedTimingCandidate] = []
    for line in _posting_content_lines(raw_text):
        for match in re.finditer(
            rf"\b(?:(?:position\s+)?will\s+become\s+available\s+in\s+|"
            rf"available\s+to\s+begin\s+"
            rf"(?:mid[-\s]*)?)(?P<start>(?:{month_pattern})\.?\s+20\d{{2}})\b",
            line,
            flags=re.IGNORECASE,
        ):
            candidate = _month_year_prose_candidate(
                match.group("start"),
                duration_months=None,
                default_schedule_type=None,
            )
            if re.search(
                rf"\b(?:{ACADEMIC_TERM_PATTERN})\s+term\b",
                line[: match.start()],
                flags=re.IGNORECASE,
            ):
                candidate = ParsedTimingCandidate(
                    timing=candidate.timing,
                    default_schedule_type=candidate.default_schedule_type,
                    prefer_start_date_term_label=True,
                    ambiguous_fields=("employment_timing.start_date",),
                )
            candidates.append(candidate)
    starts = {
        candidate.timing.start_date
        for candidate in candidates
        if candidate.timing.start_date is not None
    }
    if len(starts) != 1:
        return None
    return candidates[0]


def _month_year_prose_candidate(
    value: str,
    *,
    duration_months: int | None,
    default_schedule_type: str | None,
) -> ParsedTimingCandidate:
    month_year = re.search(
        rf"\b(?P<month>{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})"
        rf"\.?\s*,?\s*"
        r"(?P<year>20\d{2})\b",
        value,
        flags=re.IGNORECASE,
    )
    if month_year is None:
        raise ValueError("month-year prose candidate requires a month and year")
    start_year = int(month_year.group("year"))
    start_month = MONTH_NAMES[month_year.group("month").casefold()]
    start_date = f"{start_year:04d}-{start_month:02d}-01"
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=_academic_term_label_for_month_year_start(
                start_month,
                start_year,
            ),
            timing_type="fixed_term",
            start_date=start_date,
            end_date=(
                _end_date_for_month_span(
                    start_year,
                    start_month,
                    duration_months,
                )
                if duration_months is not None
                else None
            ),
            duration_months=duration_months,
        ),
        default_schedule_type=default_schedule_type,
        prefer_start_date_term_label=True,
    )


def _has_immediate_start_cue(raw_text: str) -> bool:
    return any(
        re.search(
            r"\b(?:"
            r"immediate\s+start|"
            r"immediate(?:ly)?\s+available\b"
            r"(?!\s+(?:after|following|upon)\b)|"
            r"immediate\s+availability\b"
            r"(?!\s+(?:after|following|upon)\b)|"
            r"availability[^.;\n]{0,20}\bimmediate(?:ly)?\b"
            r"(?!\s+(?:after|following|upon)\b)|"
            r"(?:(?:available\s+to\s+)?"
            r"(?:start(?:ing|s)?|begin(?:ning|s)?|commenc(?:e|es|ing))"
            r"(?:\s+date)?|available)"
            r"[^.;\n]{0,30}\bimmediate(?:ly)?\b"
            r"(?!\s+(?:after|following|upon)\b)"
            r")",
            line,
            flags=re.IGNORECASE,
        )
        for line in _posting_content_lines(raw_text)
    )


def _structured_start_end_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Read complete employment date fields even when captures omit colons."""
    months = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    date = rf"(?:{_posting_date_text_pattern()}|(?:{months})\s+20\d{{2}})"
    patterns = (
        rf"(?m)^\s*Start Date\s+(?P<start>{date})\s+End Date\s+(?P<end>{date})\b",
        rf"\bstudent\s+term\s+position,\s+with\s+a\s+start\s+date\s+of\s+"
        rf"(?P<start>{date})\s+and\s+end\s+date\s+of\s+(?P<end>{date})\b",
    )
    ranges: set[tuple[str, str]] = set()
    for match in (match for pattern in patterns for match in re.finditer(pattern, raw_text, re.IGNORECASE)):
        dates = []
        for kind in ("start", "end"):
            value = match.group(kind)
            parsed = _parse_posting_date(value)
            if parsed is None and re.fullmatch(rf"(?:{months})\s+20\d{{2}}", value, re.IGNORECASE):
                month, year = value.split()
                number = MONTH_NAMES[month.casefold()]
                day = 1 if kind == "start" else _last_day_of_month(int(year), number)
                parsed = _valid_date(int(year), number, day)
            dates.append(parsed)
        if all(dates) and dates[0] <= dates[1]:
            ranges.add(tuple(dates))

    french_months = {
        "janvier": 1, "février": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    }
    french_pattern = (
        r"\bDates prévues de début et de fin\s*:\s*"
        rf"(?P<sm>{'|'.join(french_months)})\s+(?P<sy>20\d{{2}})\s*[-–—]\s*"
        rf"(?P<em>{'|'.join(french_months)})\s+(?P<ey>20\d{{2}})\b"
    )
    for match in re.finditer(french_pattern, raw_text, re.IGNORECASE):
        sy, ey = int(match['sy']), int(match['ey'])
        sm, em = french_months[match['sm'].casefold()], french_months[match['em'].casefold()]
        start, end = f"{sy:04d}-{sm:02d}-01", f"{ey:04d}-{em:02d}-{_last_day_of_month(ey, em):02d}"
        if start <= end:
            ranges.add((start, end))
    if len(ranges) != 1:
        return None
    start, end = next(iter(ranges))
    year, month = map(int, start.split("-")[:2])
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=_academic_term_label_for_start_month(month, year),
            timing_type="fixed_term", start_date=start, end_date=end,
            duration_months=_duration_months_from_date_range(start, end),
        ),
        authoritative_date_range=True,
    )


def _duration_starting_on_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    number_pattern = "|".join(DURATION_NUMBER_WORDS)
    pattern = (
        rf"\b(?:Duration\s*:\s*(?P<months>\d{{1,2}})\s+months?|"
        rf"(?P<temporary_months>\d{{1,2}}|{number_pattern})[-\s]+months?\s+"
        r"temporary\s+opportunity)\s+starting\s+on\s+"
        rf"(?P<date>{_posting_date_text_pattern()})\b"
    )
    claims = set()
    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
        value = (match['months'] or match['temporary_months']).casefold()
        duration = int(value) if value.isdigit() else DURATION_NUMBER_WORDS[value]
        claims.add((_parse_posting_date(match['date']), duration))
    if len(claims) != 1:
        return None
    start, duration = next(iter(claims))
    if start is None or not 1 <= duration <= 24:
        return None
    year, month = map(int, start.split("-")[:2])
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=_academic_term_label_for_start_month(month, year),
            timing_type="fixed_term", start_date=start,
            end_date=_end_date_for_month_span(year, month, duration),
            duration_months=duration,
        ),
        authoritative_duration=True,
        default_schedule_type="full_time" if re.search(
            r"\b(?:intern(?:ship)?|co-?op)\b",
            _title_value(_posting_content_lines(raw_text)) or "", re.IGNORECASE,
        ) else None,
    )


def _french_title_term_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    # Restrict French season evidence to the primary role, not education prose
    # or related-job cards. Bilingual postings keep the English evidence path.
    title = _title_value(_posting_content_lines(raw_text)) or ""
    if not re.search(r"\b(?:stage|stagiaire|co-?op)\b", title, re.IGNORECASE):
        return None
    seasons = {"hiver": ("Winter", 1), "été": ("Summer", 5),
               "printemps": ("Summer", 5), "automne": ("Fall", 9)}
    matches = list(re.finditer(
        rf"\b(?P<season>{'|'.join(seasons)})\s+(?P<year>20\d{{2}})\b"
        r"(?!\s*[-–—/]\s*20\d{2})", title, re.IGNORECASE,
    ))
    claims = {(m['season'].casefold(), int(m['year'])) for m in matches}
    if len(claims) != 1:
        return None
    season, year = next(iter(claims))
    label, month = seasons[season]
    durations = {
        int(m[1]) for m in re.finditer(r"\b(\d{1,2})\s+mois\b", title, re.IGNORECASE)
    } | {
        int(m[1]) for m in re.finditer(r"\bDurée\s*:\s*(\d{1,2})\s+mois\b", raw_text, re.IGNORECASE)
    }
    if len(durations) > 1 or any(not 1 <= d <= 24 for d in durations):
        return None
    duration = next(iter(durations), 4)
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=f"{label} {year}", timing_type="fixed_term",
            start_date=f"{year:04d}-{month:02d}-01",
            end_date=_end_date_for_month_span(year, month, duration),
            duration_months=duration,
        ),
        authoritative_duration=True,
    )


def _counted_cohort_ranges_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ""
    years = set(re.findall(r"\b20\d{2}\b", title))
    if len(years) != 1 or not re.search(r"\b(?:co-?op|intern|student)\b", title, re.IGNORECASE):
        return None
    year = int(next(iter(years)))
    months = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    entry = rf"({months})\s*[-–—]\s*({months})\s*\(\d+\)"
    pattern = rf"\bDates\s*:\s*(?P<first>{entry})\s+and\s+(?P<second>{entry})"
    claims = set()
    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
        ranges = [re.fullmatch(entry, match[key], re.IGNORECASE) for key in ('first', 'second')]
        spans = [(MONTH_NAMES[m[1].casefold()], MONTH_NAMES[m[2].casefold()]) for m in ranges]
        durations = {end - start + 1 for start, end in spans}
        if len(durations) == 1 and 1 <= next(iter(durations)) <= 12:
            labels = tuple(dict.fromkeys(
                _academic_term_label_for_start_month(start, year).split()[0] for start, _ in spans
            ))
            claims.add((labels, next(iter(durations))))
    if len(claims) != 1:
        return None
    labels, duration = next(iter(claims))
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f"{'/'.join(labels)} {year}",
                            timing_type="fixed_term", duration_months=duration),
        authoritative_duration=True,
        ambiguous_fields=("employment_timing.start_date", "employment_timing.end_date"),
    )


def _french_internship_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Read role-owned internship prose, not graduation or school-return dates."""
    months = {
        "janvier": 1, "février": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    }
    month_pattern = "|".join(months)
    pattern = (
        r"\bstage\s+qui\s+se\s+déroulera\s+du\s+"
        rf"(?P<sd>\d{{1,2}})\s+(?P<sm>{month_pattern})\s+au\s+"
        rf"(?P<ed>\d{{1,2}})\s+(?P<em>{month_pattern})\s+(?P<year>20\d{{2}})\b"
    )
    ranges = set()
    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
        year = int(match['year'])
        start = _valid_date(year, months[match['sm'].casefold()], int(match['sd']))
        end = _valid_date(year, months[match['em'].casefold()], int(match['ed']))
        if start and end and start <= end:
            ranges.add((start, end))
    if len(ranges) == 1:
        start, end = next(iter(ranges))
        return ParsedTimingCandidate(
            timing=ParsedTiming(
                raw_label=_academic_term_label_for_start_date(*map(int, start.split('-'))),
                timing_type="fixed_term", start_date=start, end_date=end,
                duration_months=_duration_months_from_date_range(start, end),
            ),
            authoritative_date_range=True,
        )
    if ranges:
        return None
    # A required credited placement names this internship's season. Do not
    # generalize this to academic eligibility or returning to school in winter.
    seasons = {"hiver": ("Winter", 1), "été": ("Summer", 5),
               "automne": ("Fall", 9)}
    claims = {
        (m['season'].casefold(), int(m['year']))
        for m in re.finditer(
            r"\bstage\s+crédité\s+à\s+l[’']"
            rf"(?P<season>{'|'.join(seasons)})\s+(?P<year>20\d{{2}})\b",
            raw_text, re.IGNORECASE,
        )
    }
    if len(claims) != 1:
        return None
    season, year = next(iter(claims))
    label, month = seasons[season]
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f"{label} {year}", timing_type="fixed_term",
                            start_date=f"{year:04d}-{month:02d}-01"),
    )


def _internship_calendar_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    dates = {}
    for label in ("Kick-off", "Conclusion"):
        pattern = (
            rf"(?P<date>{_posting_date_text_pattern()})\s*[-–—]\s*"
            rf"Internship\s+Program\s+{label}\b"
        )
        values = {_parse_posting_date(m['date'])
                  for m in re.finditer(pattern, raw_text, re.IGNORECASE)}
        if len(values) != 1 or None in values:
            return None
        dates[label] = next(iter(values))
    start, end = dates['Kick-off'], dates['Conclusion']
    if start > end:
        return None
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=_academic_term_label_for_start_date(*map(int, start.split('-'))),
            timing_type="fixed_term", start_date=start, end_date=end,
            duration_months=_duration_months_from_date_range(start, end),
        ),
        authoritative_date_range=True,
    )


def _extension_duration_date_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """An optional extension does not replace the separately labelled base dates."""
    months = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = (
        r"\bDuration\s*:\s*(?P<months>\d{1,2})\s+Months?\s*"
        rf"\(with\s+potential\s+extension\s+for\s+(?:{ACADEMIC_TERM_PATTERN})\s+20\d{{2}}\)"
        rf"\s+Date\s*:\s*(?P<range>(?:{months})\s*[-–—]\s*(?:{months})\s+20\d{{2}})\b"
    )
    candidates = []
    for match in re.finditer(pattern, raw_text, re.IGNORECASE):
        ranges = _extract_month_range_timing_candidates(match['range'])
        if len(ranges) != 1:
            continue
        timing = _parsed_timing_from_month_range_candidate(ranges[0])
        if timing.duration_months != int(match['months']):
            continue
        candidates.append(timing)
    if len({(c.start_date, c.end_date) for c in candidates}) != 1:
        return None
    return ParsedTimingCandidate(timing=candidates[0], authoritative_date_range=True,
                                 authoritative_duration=True, default_schedule_type="full_time")


def _role_commitment_calendar_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    candidates = []
    for match in re.finditer(
        r"\b(?:co-?op|internship)\s+role\s+requires\s+(?P<months>\d{1,2})\s+"
        r"months?\s+term\s+commitment\s*\((?P<calendar>[^)]{1,100})\)",
        raw_text, re.I,
    ):
        ranges = _extract_month_range_timing_candidates(match['calendar'])
        if len(ranges) != 1:
            continue
        timing = _parsed_timing_from_month_range_candidate(ranges[0])
        if timing.duration_months != int(match['months']) or not timing.start_date:
            continue
        timing.raw_label = _academic_term_label_for_start_date(
            *map(int, timing.start_date.split('-')),
        )
        candidates.append(timing)
    if len({(t.start_date, t.end_date, t.duration_months) for t in candidates}) != 1:
        return None
    return ParsedTimingCandidate(timing=candidates[0], authoritative_date_range=True,
                                 authoritative_duration=True, default_schedule_type="full_time")


def _onsite_coop_range_candidate(
    raw_text: str, duration_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    months = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = (
        r"\bThis\s+is\s+a\s+(?P<first>\d{1,2})\s*[-–—]\s*(?P<last>\d{1,2})\s+"
        r"months?\s+onsite\s+co-op\s+position\s+based\s+in\s+[^.;]{1,80}?"
        rf",\s*starting\s+in\s+(?P<start>(?:{months})\s+20\d{{2}})\b"
    )
    claims = {
        (int(m['first']), int(m['last']), m['start'])
        for m in re.finditer(pattern, " ".join(raw_text.split()), re.I)
    }
    if len(claims) != 1:
        return None
    first, last, start = next(iter(claims))
    if not 1 <= first < last <= 24:
        return None
    candidate = _month_year_prose_candidate(
        start, duration_months=None,
        default_schedule_type=duration_candidate.default_schedule_type if duration_candidate else None,
    )
    return replace(
        candidate,
        timing=candidate.timing.model_copy(update={'duration_label': f'{first}-{last} months'}),
        alternative_duration=True, authoritative_duration=True,
        duration_options=frozenset({first, last}),
        ambiguous_fields=('employment_timing.duration_months', 'employment_timing.end_date'),
    )


def _french_stage_fields_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ''
    if not re.search(r'\b(?:stage|stagiaire)\b', title, re.I):
        return None
    seasons = {'hiver': 'Winter', 'été': 'Summer', 'printemps': 'Summer', 'automne': 'Fall'}
    season_pattern = '|'.join(seasons)
    claims = {
        (seasons[m['season'].casefold()], int(m['year']))
        for m in re.finditer(
            r'(?m)^\s*(?:Session\s+du\s+stage\s*:\s*|Le\s+Mandat\s*[-–—]\s*|Stage\s+|'
            r'Mandat\s+de\s+stage\s*[-–—]\s*(?:session|saison)\s+)'
            rf'(?P<season>{season_pattern})\s+(?P<year>20\d{{2}})(?:/hybride)?\s*:?\s*$',
            raw_text, re.I,
        )
    }
    if len(claims) != 1:
        return None
    season, year = next(iter(claims))
    timing = ParsedTiming(raw_label=f'{season} {year}', timing_type='fixed_term')
    months = {'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
              'juillet': 7, 'août': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12}
    dates = {}
    for kind, label in (('start', 'Début'), ('end', 'Fin')):
        values = {
            _valid_date(int(m['year']), months[m['month'].casefold()], int(m['day']))
            for m in re.finditer(
                rf'(?m)^\s*{label}\s+du\s+stage\s*:\s*(?P<day>\d{{1,2}})\s+'
                rf'(?P<month>{"|".join(months)})\s+(?P<year>20\d{{2}})\s*$', raw_text, re.I,
            )
        }
        if values:
            if len(values) != 1 or None in values:
                return None
            dates[kind] = values.pop()
    if dates:
        if set(dates) != {'start', 'end'} or dates['start'] > dates['end']:
            return None
        if _academic_term_label_for_start_date(*map(int, dates['start'].split('-'))) != timing.raw_label:
            return None
        timing.start_date, timing.end_date = dates['start'], dates['end']
        timing.duration_months = _duration_months_from_date_range(timing.start_date, timing.end_date)
    flexible = bool(re.search(r'\bLes\s+dates\s+peuvent\s+être\s+flexibles\b', raw_text, re.I))
    return ParsedTimingCandidate(
        timing=timing, authoritative_date_range=bool(dates),
        ambiguous_fields=('employment_timing.start_date', 'employment_timing.end_date') if flexible else (),
    )


def _intern_start_month_choices_candidate(
    raw_text: str, duration_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ''
    if not re.search(r'\bintern(?:ship)?\b', title, re.I):
        return None
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    claims = {
        (MONTH_NAMES[m['first'].casefold()], MONTH_NAMES[m['second'].casefold()], int(m['year']))
        for m in re.finditer(
            rf'\bAbility\s+to\s+start\s+in\s+the\s+month\s+of\s+(?P<first>{months})\s*/?\s*'
            rf'(?P<year>20\d{{2}})\s+or\s+(?P<second>{months})\s*/?\s*(?P=year)\b', raw_text, re.I,
        )
    }
    if len(claims) != 1:
        return None
    first, second, year = next(iter(claims))
    labels = {_academic_term_label_for_start_month(month, year) for month in (first, second)}
    if len(labels) != 1 or first == second:
        return None
    timing = duration_candidate.timing.model_copy(deep=True) if duration_candidate else ParsedTiming()
    timing.raw_label, timing.timing_type = labels.pop(), 'fixed_term'
    timing.start_date = timing.end_date = None
    return ParsedTimingCandidate(timing=timing, authoritative_duration=True, default_schedule_type="full_time",
                                 ambiguous_fields=('employment_timing.start_date', 'employment_timing.end_date'))


def _exclusive_summer_month_choices_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """An exclusive program restriction is evidence even when printed in a form."""
    claims = {
        int(match["year"])
        for match in re.finditer(
            r"\bPlease\s+confirm\s+that\s+you\s+are\s+only\s+interested\s+in\s+a\s+"
            r"Summer\s+internship\s+from\s+May\s*/\s*June\s+to\s+August\s*/\s*"
            r"September\s+of\s+(?P<year>20\d{2})\.\s*"
            r"We\s+will\s+not\s+consider\s+applicants\s+for\s+other\s+terms\b",
            raw_text, re.I,
        )
    }
    if len(claims) != 1:
        return None
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f"Summer {claims.pop()}", timing_type="fixed_term"),
        authoritative_duration=True,
        ambiguous_fields=("employment_timing.start_date", "employment_timing.end_date"),
    )


def _year_only_title_seasonal_duration_candidate(
    raw_text: str, duration_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    if duration_candidate is None or duration_candidate.timing.start_date:
        return None
    title = _title_value(_posting_content_lines(raw_text)) or ""
    title_year = re.match(r"^\s*(?P<year>20\d{2})\b", title)
    if not title_year or not re.search(r"\bintern(?:ship)?\b", title, re.I):
        return None
    # Bind the title year only to the selected, role-owned duration clause,
    # rather than to seasons in graduation or company-benefit prose.
    season = re.search(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\s+internship\s+program\b",
        duration_candidate.timing.raw_label or "", re.I,
    )
    if season is None:
        return None
    term = _titlecase_term_label(_canonical_term_key(season["term"]))
    return replace(duration_candidate, timing=duration_candidate.timing.model_copy(
        update={"raw_label": f"{term} {title_year['year']}"},
    ))


def _labeled_exact_duration_options_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ''
    if not re.search(r'\b(?:intern(?:ship)?|co-?op)\b', title, re.I):
        return None
    pattern = (
        rf'\b(?P<months>\d{{1,2}})\s+Months?\s*:\s*(?P<start>{_posting_date_text_pattern()})'
        rf'\s+to\s+(?P<end>{_posting_date_text_pattern()})\b'
    )
    claims = set()
    for match in re.finditer(pattern, raw_text, re.I):
        duration = int(match['months'])
        start, end = _parse_posting_date(match['start']), _parse_posting_date(match['end'])
        if not start or not end or not 1 <= duration <= 24 or not _date_range_supports_duration(start, end, duration):
            return None
        claims.add((duration, start, end))
    options = {duration for duration, _, _ in claims}
    starts = {start for _, start, _ in claims}
    if len(options) < 2 or len(starts) != 1:
        return None
    start = starts.pop()
    label = ' or '.join(map(str, sorted(options))) + ' months'
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=_academic_term_label_for_start_date(*map(int, start.split('-'))),
                            timing_type='fixed_term', start_date=start, duration_label=label),
        alternative_duration=True, authoritative_duration=True,
        duration_options=frozenset(options),
        ambiguous_fields=('employment_timing.duration_months', 'employment_timing.end_date'),
    )


def _mandate_possible_extension_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    claims = {
        (int(m['base']), int(m['extension']), m['start'])
        for m in re.finditer(
            r'\b(?P<base>\d{1,2})\s+month\s+mandate\s*\(with\s+a\s+possibility\s+of\s+'
            rf'(?P<extension>\d{{1,2}})\s+months\)\s+starting\s+in\s+(?P<start>(?:{months})\s+20\d{{2}})\b',
            raw_text, re.I,
        )
    }
    if len(claims) != 1:
        return None
    base, extension, start = claims.pop()
    if not 1 <= base < extension <= 24:
        return None
    candidate = _month_year_prose_candidate(start, duration_months=base, default_schedule_type="full_time")
    return replace(candidate, authoritative_duration=True, timing=candidate.timing.model_copy(
        update={'duration_label': f'{base} months ({extension} months possible)'},
    ))


def _role_fixed_duration_candidate(
    raw_text: str, evidence: EmploymentTimingEvidence,
) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ''
    if not re.search(r'\b(?:intern(?:ship)?|co-?op|student)\b', title, re.I):
        return None
    labels = {
        (int(m['months']), m[0].strip())
        for m in re.finditer(r'(?m)^\s*(?P<months>\d{1,2})\s+Month\s+Internship\s*$', raw_text, re.I)
    }
    labels.update(
        (int(m['months']), f"{int(m['months'])} months")
        for m in re.finditer(r'\bProgram\s+Length\s*:\s*(?P<months>\d{1,2})\s+months?\b', raw_text, re.I)
    )
    labels.update(
        (int(m['months']), f"{int(m['months'])} months")
        for m in re.finditer(r'\bintern(?:ship)?\s*\(\s*(?P<months>\d{1,2})\s+months?\s*\)', title, re.I)
    )
    durations = {duration for duration, _ in labels}
    if len(durations) != 1 or not all(1 <= duration <= 24 for duration in durations):
        return None
    candidate = _parse_explicit_internship_timing(raw_text, evidence)
    if candidate is None or candidate.timing.duration_months is not None or evidence.has_end_dates:
        return None  # Independently stated body dates/duration retain precedence.
    if len({(item.term_key, item.year) for item in evidence.academic_terms}) > 1:
        return None
    timing = candidate.timing.model_copy(deep=True)
    timing.duration_months = durations.pop()
    timing.timing_type = 'fixed_term'
    if not timing.start_date:
        timing.raw_label = sorted(labels)[0][1]
    if timing.start_date:
        year, month = map(int, timing.start_date.split('-')[:2])
        timing.end_date = _end_date_for_month_span(year, month, timing.duration_months)
    candidate = replace(candidate, timing=timing, authoritative_duration=True)
    return _intern_availability_month_alternative_candidate(raw_text, candidate) or candidate


def _owned_role_calendar_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Complete role calendars outrank partial month starts and title defaults."""
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    calendar = rf"(?:{months})\s*(?:to|[-–—])\s*(?:{months})\s+20\d{{2}}"
    patterns = (
        rf"\bThis\s+is\s+an?\s+(?P<duration>\d{{1,2}})-month\s+co-op\s+term\.\s*\((?P<calendar>{calendar})\)",
        rf"\bThis\s+co-op\s+role\s+will\s+work\s+on-site\s+from\s+(?P<calendar>{calendar})",
        rf"\bLimited\s+Term\s+Employment\s+position\s+\((?P<duration>\d{{1,2}})-Month\s+Co-op\)\s+from\s+(?P<calendar>{calendar})",
    )
    candidates = []
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text, re.I):
            ranges = _extract_month_range_timing_candidates(match['calendar'])
            if len(ranges) != 1:
                continue
            timing = _parsed_timing_from_month_range_candidate(ranges[0])
            duration = match.groupdict().get('duration')
            if duration and timing.duration_months != int(duration):
                continue
            candidates.append(ParsedTimingCandidate(timing=timing, authoritative_date_range=True,
                                                    authoritative_duration=True))
    if len({(c.timing.start_date, c.timing.end_date) for c in candidates}) == 1:
        return candidates[0]
    return None


def _duplicated_position_start_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    claims = {(int(m['months']), m['start']) for m in re.finditer(
        rf"\bThis\s+is\s+a\s+(?P<months>\d{{1,2}})-month\s+position\s+that\s+will\s+"
        rf"start\s+(?P<start>(?:{months})\s+20\d{{2}})\b", raw_text, re.I,
    )}
    if len(claims) != 1:
        return None
    duration, start = next(iter(claims))
    if not 1 <= duration <= 24:
        return None
    return _month_year_prose_candidate(start, duration_months=duration,
                                       default_schedule_type=None)


def _source_title_calendar_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    candidates = []
    for line in _posting_content_lines(raw_text):
        label = _field_label(line)
        if label and label not in {'title', 'job title', 'page title', 'role'} and _provenance_field_semantic(label) != 'role':
            continue
        title = line.split(':', 1)[1].strip() if label else line.strip()
        if len(title) > 180 or not re.search(r'\b(?:co-op|intern(?:ship)?|student)\b', title, re.I):
            continue
        range_title = re.fullmatch(
            rf"[^.:\n]{{1,140}}\((?P<calendar>(?:{months})\s+(?:to\s+|[-–—]\s*)(?:{months})\s+20\d{{2}})\)",
            title, re.I,
        )
        duration_title = re.search(
            rf"\((?P<start>(?:{months})\s+20\d{{2}})\s+(?P<months>\d{{1,2}})\s+Months?\)",
            title, re.I,
        )
        timing = _title_hint_month_range_timing(title) if range_title else None
        if duration_title and 1 <= int(duration_title['months']) <= 24:
            candidate = _month_year_prose_candidate(duration_title['start'],
                duration_months=int(duration_title['months']), default_schedule_type=None)
            timing = candidate.timing if candidate else None
        if timing:
            candidates.append(ParsedTimingCandidate(timing=timing, authoritative_date_range=True,
                                                    authoritative_duration=True))
    if len({(c.timing.start_date, c.timing.end_date) for c in candidates}) == 1:
        return candidates[0]
    return None


def _qualified_slash_month_duration_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    months = '|'.join(sorted(MONTH_NAMES, key=len, reverse=True))
    matches = list(re.finditer(
        rf"\bFor\s+(?P<months>\d{{1,2}})-month\s+internships?:\s*must\s+be\s+available\s+"
        rf"(?:{months})/(?:{months})\s+20\d{{2}}\s*[-–—]\s*"
        rf"(?:{months})/(?:{months})\s+20\d{{2}}", raw_text, re.I,
    ))
    durations = {int(m['months']) for m in matches}
    # Multiple duration sections describe alternatives, not a selected term.
    if len(durations) != 1:
        return None
    title_durations = {n for line in _posting_content_lines(raw_text)
                       if re.search(r'\b(?:intern|internship|co-op)\b', line, re.I)
                       and len(line) <= 180
                       for n, *_ in _duration_month_candidates(line)}
    duration = next(iter(durations))
    if title_durations != {duration} or not 1 <= duration <= 24:
        return None
    candidate = ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f'{duration} months', timing_type='fixed_term',
                            duration_months=duration), authoritative_duration=True,
        ambiguous_fields=('employment_timing.start_date', 'employment_timing.end_date'),
    )
    return _intern_availability_month_alternative_candidate(raw_text, candidate) or candidate


def _parse_employment_timing_resolution(
    raw_text: str,
) -> EmploymentTimingResolution:
    posting_scope = _posting_content_scope(raw_text)
    timing_scope = _employment_timing_content_scope(posting_scope.text)
    posting_text = timing_scope.timing_text

    evidence = _employment_timing_evidence(posting_text)
    corroborating_duration_months = frozenset(
        duration.months for duration in evidence.duration_terms
    )
    month_range_assessments = _employment_month_range_assessments(posting_text)
    alternative_candidate = _parse_alternative_duration_term_timing(posting_text)
    cross_source_ambiguities = _cross_source_timing_ambiguities(
        evidence,
        month_range_assessments,
        alternative_duration_options=(
            alternative_candidate.duration_options if alternative_candidate else frozenset()
        ),
    )

    explicit_candidate = _parse_explicit_internship_timing(posting_text, evidence)
    asap_through_candidate = _parse_asap_through_end_timing(posting_text)
    fixed_end_candidate = _parse_fixed_end_timing(posting_text)
    scheduled_start_candidate = _scheduled_start_date_candidate(posting_text)
    preferred_duration_month_range_candidate = (
        _preferred_duration_month_range_candidate(posting_text)
    )
    approximate_commence_conclude_candidate = (
        _approximate_commence_conclude_candidate(posting_text)
    )
    multi_season_matrix_candidate = _multi_season_exact_duration_matrix_candidate(
        posting_text
    )
    labeled_duration_alternatives_candidate = (
        _labeled_duration_month_range_alternatives_candidate(posting_text)
    )
    listed_duration_opportunities_candidate = (
        _listed_duration_opportunities_candidate(posting_text)
    )
    role_owned_exact_range_candidate = _role_owned_exact_date_range_candidate(
        posting_text
    )
    labeled_exact_date_range_candidate = _labeled_exact_date_range_candidate(
        posting_text
    )
    invalid_end_exact_date_candidate = _labeled_invalid_end_date_range_candidate(
        posting_text
    )
    role_runs_shared_year_candidate = _role_runs_shared_year_month_range_candidate(
        posting_text
    )
    program_runs_yearless_range_candidate = (
        _program_runs_year_duration_yearless_range_candidate(posting_text)
    )
    ability_month_year_candidate = _ability_to_work_month_year_start_candidate(
        posting_text
    )
    term_open_duration_candidate = _term_exact_start_open_duration_candidate(
        posting_text
    )
    repaired_same_year_range_candidate = _same_year_duration_range_candidate(
        posting_text
    )
    duration_term_candidate = _parse_duration_term_start_timing(posting_text)
    duration_only_candidate = _parse_duration_only_term_timing(posting_text)
    prose_month_year_candidate = _prose_month_year_start_candidate(posting_text)
    availability_month_year_candidate = _availability_month_year_start_candidate(
        posting_scope.text
    )
    if (
        alternative_candidate
        and not alternative_candidate.timing.start_date
        and duration_term_candidate
        and duration_term_candidate.timing.start_date
        and duration_term_candidate.timing.duration_months
        in alternative_candidate.duration_options
    ):
        alternative_candidate = ParsedTimingCandidate(
            timing=alternative_candidate.timing.model_copy(
                update={
                    "start_date": duration_term_candidate.timing.start_date,
                    "schedule_type": duration_term_candidate.timing.schedule_type,
                }
            ),
            schedule_resolution=alternative_candidate.schedule_resolution.merged(
                duration_term_candidate.schedule_resolution
            ),
            default_schedule_type=duration_term_candidate.default_schedule_type,
            alternative_duration=True,
            prefer_academic_term_label=(
                alternative_candidate.prefer_academic_term_label
            ),
            prefer_start_date_term_label=(
                alternative_candidate.prefer_start_date_term_label
            ),
            duration_options=alternative_candidate.duration_options,
            ambiguous_fields=alternative_candidate.ambiguous_fields,
        )
    elif (
        alternative_candidate
        and not alternative_candidate.timing.start_date
        and prose_month_year_candidate
        and prose_month_year_candidate.timing.start_date
    ):
        alternative_candidate = ParsedTimingCandidate(
            timing=alternative_candidate.timing.model_copy(
                update={
                    "start_date": prose_month_year_candidate.timing.start_date,
                    "schedule_type": prose_month_year_candidate.timing.schedule_type,
                }
            ),
            schedule_resolution=alternative_candidate.schedule_resolution.merged(
                prose_month_year_candidate.schedule_resolution
            ),
            default_schedule_type=prose_month_year_candidate.default_schedule_type,
            alternative_duration=True,
            prefer_academic_term_label=(
                alternative_candidate.prefer_academic_term_label
            ),
            prefer_start_date_term_label=(
                alternative_candidate.prefer_start_date_term_label
            ),
            duration_options=alternative_candidate.duration_options,
            ambiguous_fields=alternative_candidate.ambiguous_fields,
        )
    month_range_assessment = _select_month_range_timing_assessment(month_range_assessments)
    month_range_candidate = None
    if month_range_assessment:
        month_range_timing = _parsed_timing_from_month_range_candidate(
            month_range_assessment.candidate,
            corroborating_duration_months=corroborating_duration_months,
        )
        if month_range_assessment.reason == "opportunity duration range followed by calendar":
            prefix = month_range_assessment.candidate.segment[:month_range_assessment.candidate.start]
            range_match = next(_alternative_duration_matches(prefix), None)
            if range_match:
                month_range_timing.duration_label = _clean_field_value(range_match.group())
        semantic_durations = _semantic_durations_near_month_range(
            month_range_assessment
        )
        authoritative_duration = (
            next(iter(semantic_durations)) if len(semantic_durations) == 1 else None
        )
        if authoritative_duration is not None:
            month_range_timing.duration_months = authoritative_duration
        default_schedule_type = (
            None
            if month_range_assessment.reason
            == "term or temporary duration contains range"
            else "full_time"
        )
        month_range_timing.schedule_type = default_schedule_type
        month_range_candidate = ParsedTimingCandidate(
            timing=month_range_timing,
            default_schedule_type=default_schedule_type,
            authoritative_date_range=True,
            authoritative_duration=authoritative_duration is not None,
        )
    if program_runs_yearless_range_candidate and month_range_candidate:
        # Exact program dates refine the month range without losing its schedule default.
        program_runs_yearless_range_candidate = replace(
            program_runs_yearless_range_candidate,
            default_schedule_type=month_range_candidate.default_schedule_type,
        )
    academic_alternative_candidate = _academic_term_alternative_candidate(evidence)
    academic_candidate = _academic_term_timing_candidate(
        evidence.academic_terms[0] if evidence.academic_terms else None,
        alternative_candidate=alternative_candidate,
    )
    if academic_candidate and evidence.academic_terms:
        availability_schedule = re.search(
            r"\bavailable\s+to\s+work\s+(part|full)[-\s]*time\s+in\s+",
            evidence.academic_terms[0].line, re.IGNORECASE,
        )
        if availability_schedule:
            academic_candidate = replace(
                academic_candidate,
                schedule_resolution=ScheduleEvidenceResolution(
                    positive_values=frozenset({availability_schedule[1].lower() + "_time"}),
                ),
            )
    academic_duration_only_candidate = None
    if (
        academic_candidate
        and duration_only_candidate
        and alternative_candidate is None
        and academic_candidate.timing.start_date
        and duration_only_candidate.timing.duration_months is not None
        and (
            re.search(
                r"\b(?:intern(?:ship)?|co-?op)\b",
                duration_only_candidate.timing.raw_label or "",
                flags=re.IGNORECASE,
            )
            or re.search(
                r"\binternship\s+is\s+a\s+\d{1,2}[-\s]week\s*,\s*"
                r"paid\s+program\b",
                posting_text,
                flags=re.IGNORECASE,
            )
        )
    ):
        duration_months = duration_only_candidate.timing.duration_months
        start_year, start_month = map(
            int,
            academic_candidate.timing.start_date.split("-")[:2],
        )
        academic_duration_only_candidate = ParsedTimingCandidate(
            timing=academic_candidate.timing.model_copy(
                update={
                    "end_date": _end_date_for_month_span(
                        start_year,
                        start_month,
                        duration_months,
                    ),
                    "duration_months": duration_months,
                }
            ),
            schedule_resolution=academic_candidate.schedule_resolution.merged(
                duration_only_candidate.schedule_resolution
            ),
            default_schedule_type=academic_candidate.default_schedule_type,
            prefer_academic_term_label=True,
            authoritative_duration=True,
        )
    combined_source_title_candidate = _combined_academic_source_title_candidate(
        evidence.academic_terms[0] if evidence.academic_terms else None,
        alternative_candidate=alternative_candidate,
    )
    source_title_candidate = _source_role_title_month_year_candidate(posting_text)
    role_header_month_year_candidate = _role_header_month_year_candidate(posting_text)
    seasonal_title_year_only_candidate = _seasonal_title_year_only_start_candidate(
        posting_text
    )
    prose_month_alternative_candidate = _prose_month_alternative_candidate(posting_text)
    intern_availability_alternative_candidate = _intern_availability_month_alternative_candidate(
        posting_text, alternative_candidate or duration_only_candidate,
    )
    if listed_duration_opportunities_candidate and academic_candidate:
        # The list describes duration choices, not competing start terms.
        terms = {(item.term_key, item.year) for item in evidence.academic_terms}
        if len(terms) == 1:
            listed_duration_opportunities_candidate = replace(
                listed_duration_opportunities_candidate,
                timing=listed_duration_opportunities_candidate.timing.model_copy(
                    update={
                        "raw_label": academic_candidate.timing.raw_label,
                        "start_date": academic_candidate.timing.start_date,
                    }
                ),
            )

    selected_candidate = select_evidence(
        "employment_timing",
        (
            EvidenceRule("multi_season_matrix", multi_season_matrix_candidate),
            EvidenceRule("owned_role_calendar", _owned_role_calendar_candidate(posting_text)),
            EvidenceRule("duplicated_position_start", _duplicated_position_start_candidate(posting_text)),
            EvidenceRule("qualified_slash_month_duration", _qualified_slash_month_duration_candidate(posting_text)),
            EvidenceRule("role_commitment_calendar", _role_commitment_calendar_candidate(posting_text)),
            EvidenceRule("onsite_coop_range", _onsite_coop_range_candidate(posting_text, duration_term_candidate)),
            EvidenceRule("french_stage_fields", _french_stage_fields_candidate(posting_text)),
            EvidenceRule("exclusive_summer_month_choices", _exclusive_summer_month_choices_candidate(posting_text)),
            EvidenceRule("intern_start_month_choices", _intern_start_month_choices_candidate(posting_text, duration_only_candidate)),
            EvidenceRule("labeled_exact_duration_options", _labeled_exact_duration_options_candidate(posting_text)),
            EvidenceRule("mandate_possible_extension", _mandate_possible_extension_candidate(posting_text)),
            EvidenceRule("counted_cohort_ranges", _counted_cohort_ranges_candidate(posting_text)),
            EvidenceRule("structured_start_end", _structured_start_end_candidate(posting_text)),
            EvidenceRule("french_internship", _french_internship_candidate(posting_text)),
            EvidenceRule("internship_calendar", _internship_calendar_candidate(posting_text)),
            EvidenceRule("extension_duration_date", _extension_duration_date_candidate(posting_text)),
            EvidenceRule("duration_starting_on", _duration_starting_on_candidate(posting_text)),
            EvidenceRule("role_fixed_duration", _role_fixed_duration_candidate(posting_text, evidence)),
            EvidenceRule("labeled_duration_alternatives", labeled_duration_alternatives_candidate),
            EvidenceRule("listed_duration_opportunities", listed_duration_opportunities_candidate),
            EvidenceRule("role_owned_exact_range", role_owned_exact_range_candidate),
            EvidenceRule("term_open_duration", term_open_duration_candidate),
            EvidenceRule("ability_month_year", ability_month_year_candidate),
            EvidenceRule(
                "preferred_duration_month_range", preferred_duration_month_range_candidate
            ),
            EvidenceRule("approximate_commence_conclude", approximate_commence_conclude_candidate),
            EvidenceRule("labeled_exact_date_range", labeled_exact_date_range_candidate),
            EvidenceRule("invalid_end_exact_date", invalid_end_exact_date_candidate),
            EvidenceRule("program_runs_yearless_range", program_runs_yearless_range_candidate),
            EvidenceRule("role_runs_shared_year", role_runs_shared_year_candidate),
            EvidenceRule(
                "month_range_compatible_with_explicit",
                month_range_candidate,
                lambda: (
                    month_range_assessment
                    and _month_range_is_compatible_with_explicit_evidence(
                        month_range_assessment, evidence
                    )
                ),
            ),
            EvidenceRule(
                "repaired_same_year_range_compatible",
                repaired_same_year_range_candidate,
                lambda: (
                    repaired_same_year_range_candidate
                    and _timing_candidate_is_compatible_with_explicit_evidence(
                        repaired_same_year_range_candidate, evidence
                    )
                ),
            ),
            EvidenceRule("combined_source_title", combined_source_title_candidate),
            EvidenceRule(
                "alternative_with_start_evidence",
                alternative_candidate,
                lambda: (
                    alternative_candidate
                    and (alternative_candidate.timing.start_date or evidence.has_start_dates)
                ),
            ),
            EvidenceRule(
                "explicit_start_and_end",
                explicit_candidate,
                lambda: explicit_candidate and evidence.start_date and evidence.end_date,
            ),
            EvidenceRule("asap_through", asap_through_candidate),
            EvidenceRule("fixed_end", fixed_end_candidate),
            EvidenceRule("scheduled_start", scheduled_start_candidate),
            EvidenceRule("availability_month_year", availability_month_year_candidate),
            EvidenceRule(
                "academic_title_agrees_with_duration",
                academic_candidate,
                lambda: (
                    academic_candidate
                    and duration_term_candidate
                    and evidence.academic_terms
                    and (
                        _single_academic_title_duration(evidence.academic_terms[0].line)
                        == duration_term_candidate.timing.duration_months
                    )
                    and (
                        academic_candidate.timing.start_date
                        == duration_term_candidate.timing.start_date
                    )
                ),
            ),
            EvidenceRule(
                "duration_term_compatible_with_explicit",
                duration_term_candidate,
                lambda: (
                    duration_term_candidate
                    and (
                        explicit_candidate is None
                        or not evidence.start_date
                        or _timing_candidate_is_compatible_with_explicit_evidence(
                            duration_term_candidate, evidence
                        )
                    )
                ),
            ),
            EvidenceRule(
                "explicit_start",
                explicit_candidate,
                lambda: explicit_candidate and evidence.start_date,
            ),
            EvidenceRule("duration_term_fallback", duration_term_candidate),
            EvidenceRule("month_range_fallback", month_range_candidate),
            EvidenceRule(
                "alternative_with_academic_alternatives",
                alternative_candidate,
                lambda: academic_alternative_candidate and alternative_candidate,
            ),
            EvidenceRule("academic_alternative", academic_alternative_candidate),
            EvidenceRule("academic_duration_only", academic_duration_only_candidate),
            EvidenceRule("academic_term_fallback", academic_candidate),
            EvidenceRule("french_title_term", _french_title_term_candidate(posting_text)),
            EvidenceRule("source_title", source_title_candidate),
            EvidenceRule("role_header_month_year", role_header_month_year_candidate),
            EvidenceRule("seasonal_title_year_only", seasonal_title_year_only_candidate),
            EvidenceRule(
                "prose_range_with_duration_or_term",
                prose_month_year_candidate,
                lambda: (
                    prose_month_year_candidate
                    and (
                        prose_month_year_candidate.timing.duration_months is not None
                        or prose_month_year_candidate.prefer_academic_term_label
                    )
                ),
            ),
            EvidenceRule("prose_month_alternative", prose_month_alternative_candidate),
            EvidenceRule("intern_availability_alternative", intern_availability_alternative_candidate),
            EvidenceRule("year_leading_role_term", _year_leading_role_term_candidate(posting_text)),
            EvidenceRule(
                "hiring_season_month_window",
                _hiring_season_month_window_candidate(
                    posting_text, alternative_candidate or duration_only_candidate,
                ),
            ),
            EvidenceRule("alternative_fallback", alternative_candidate),
            EvidenceRule("year_only_title_seasonal_duration", _year_only_title_seasonal_duration_candidate(posting_text, duration_only_candidate)),
            EvidenceRule("duration_only", duration_only_candidate),
            EvidenceRule("french_contract_duration", _french_contract_duration_candidate(posting_text)),
            EvidenceRule("explicit_fallback", explicit_candidate),
        ),
    )

    if selected_candidate:
        selected_ambiguities = cross_source_ambiguities
        if (
            selected_candidate is labeled_exact_date_range_candidate
            and selected_candidate.timing.duration_months is not None
            and evidence.duration_months is not None
            and selected_candidate.timing.duration_months
            != evidence.duration_months
        ):
            selected_ambiguities = _merge_ambiguous_fields(
                selected_ambiguities,
                ("employment_timing.duration_months",),
            )
        return _complete_employment_resolution(
            _resolve_employment_candidate(
                selected_candidate,
                evidence,
                additional_ambiguous_fields=selected_ambiguities,
            ),
            evidence,
            posting_text,
        )

    unresolved_ambiguities = _merge_ambiguous_fields(
        evidence.ambiguous_fields,
        cross_source_ambiguities,
    )
    if unresolved_ambiguities:
        return _complete_employment_resolution(
            EmploymentTimingResolution(
                timing=ParsedTiming(),
                ambiguous_fields=unresolved_ambiguities,
            ),
            evidence,
            posting_text,
        )
    if _has_immediate_start_cue(posting_text):
        return _complete_employment_resolution(
            EmploymentTimingResolution(ParsedTiming(raw_label="Immediate")),
            evidence,
            posting_text,
        )
    posted_date = _posted_date_fallback(timing_scope.posted_date_text)
    if posted_date:
        return _complete_employment_resolution(
            EmploymentTimingResolution(
                ParsedTiming(
                    raw_label=f"Posted {posted_date}",
                    timing_type="posted_date_fallback",
                    start_date=posted_date,
                )
            ),
            evidence,
            posting_text,
        )
    return _complete_employment_resolution(
        EmploymentTimingResolution(ParsedTiming()),
        evidence,
        posting_text,
    )


def _complete_employment_resolution(
    resolution: EmploymentTimingResolution,
    evidence: EmploymentTimingEvidence,
    raw_text: str,
) -> EmploymentTimingResolution:
    timing = resolution.timing
    hours_min, hours_max, hours_ambiguous = _hours_per_week_resolution(raw_text)
    timing.hours_per_week_min = hours_min
    timing.hours_per_week_max = hours_max

    schedule_resolution = evidence.schedule_fields.merged(evidence.schedule_prose)
    if not schedule_resolution.positive_values and evidence.schedule_defaults.value is not None:
        schedule_resolution = schedule_resolution.merged(evidence.schedule_defaults)
    if not schedule_resolution.positive_values:
        if hours_min is not None and hours_min >= 35:
            schedule_resolution = schedule_resolution.merged(
                ScheduleEvidenceResolution(positive_values=frozenset({"full_time"}))
            )
        elif hours_max is not None and hours_max < 35:
            schedule_resolution = schedule_resolution.merged(
                ScheduleEvidenceResolution(positive_values=frozenset({"part_time"}))
            )
    if timing.schedule_type is None:
        timing.schedule_type = schedule_resolution.value
    ambiguous_fields = list(resolution.ambiguous_fields)
    repaired_range = _same_year_duration_range_repair(raw_text, timing)
    if repaired_range is not None:
        timing.raw_label = repaired_range
        if "employment_timing.end_date" not in ambiguous_fields:
            ambiguous_fields.append("employment_timing.end_date")
    if hours_ambiguous:
        for field in (
            "employment_timing.hours_per_week_min",
            "employment_timing.hours_per_week_max",
            "employment_timing.schedule_type",
        ):
            if field not in ambiguous_fields:
                ambiguous_fields.append(field)
    if (
        schedule_resolution.is_ambiguous
        and "employment_timing.schedule_type" not in ambiguous_fields
    ):
        ambiguous_fields.append("employment_timing.schedule_type")
    return EmploymentTimingResolution(
        timing=timing,
        ambiguous_fields=tuple(ambiguous_fields),
    )


def _same_year_duration_range_repair(
    raw_text: str,
    timing: ParsedTiming,
) -> str | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    matches = list(
        re.finditer(
            rf"\b8[-\s]month\b[^.;\n]{{0,100}}\bfrom\s+"
            rf"(?P<start_month>{month_pattern})\.?\s+(?P<year>20\d{{2}})\s+"
            rf"to\s+(?P<end_month>{month_pattern})\.?\s+(?P=year)\b",
            raw_text,
            flags=re.IGNORECASE,
        )
    )
    if len(matches) != 1 or timing.duration_months != 8:
        return None
    match = matches[0]
    start_month = MONTH_NAMES[match.group("start_month").casefold()]
    end_month = MONTH_NAMES[match.group("end_month").casefold()]
    if end_month >= start_month:
        return None
    start_year = int(match.group("year"))
    expected_start = f"{start_year:04d}-{start_month:02d}-01"
    expected_end = _end_date_for_month_span(start_year, start_month, 8)
    if int(expected_end[5:7]) != end_month:
        return None
    if timing.start_date != expected_start or timing.end_date != expected_end:
        return None
    start_label = match.group("start_month").rstrip(".").title()
    end_label = match.group("end_month").rstrip(".").title()
    return f"{start_label} {start_year} to {end_label} {start_year + 1}"


def _same_year_duration_range_candidate(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    matches = list(
        re.finditer(
            rf"\b8[-\s]month\b[^.;\n]{{0,100}}\bfrom\s+"
            rf"(?P<start_month>{month_pattern})\.?\s+(?P<year>20\d{{2}})\s+"
            rf"to\s+(?P<end_month>{month_pattern})\.?\s+(?P=year)\b",
            raw_text,
            flags=re.IGNORECASE,
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    start_month = MONTH_NAMES[match.group("start_month").casefold()]
    end_month = MONTH_NAMES[match.group("end_month").casefold()]
    if end_month >= start_month:
        return None
    start_year = int(match.group("year"))
    expected_end = _end_date_for_month_span(start_year, start_month, 8)
    end_month_matches_duration = int(expected_end[5:7]) == end_month
    start_label = match.group("start_month").rstrip(".").title()
    end_label = match.group("end_month").rstrip(".").title()
    if not end_month_matches_duration:
        raw_label = re.split(
            r"\s+from\s+",
            _clean_field_value(match.group(0)),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        return ParsedTimingCandidate(
            timing=ParsedTiming(
                raw_label=raw_label,
                timing_type="fixed_term",
                start_date=f"{start_year:04d}-{start_month:02d}-01",
                end_date=expected_end,
                duration_months=8,
                schedule_type="full_time",
            ),
            default_schedule_type="full_time",
            authoritative_duration=True,
            ambiguous_fields=("employment_timing.end_date",),
        )
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=f"{start_label} {start_year} to {end_label} {start_year + 1}",
            timing_type="fixed_term",
            start_date=f"{start_year:04d}-{start_month:02d}-01",
            end_date=expected_end,
            duration_months=8,
            schedule_type="full_time",
        ),
        default_schedule_type="full_time",
        authoritative_date_range=True,
        authoritative_duration=True,
        ambiguous_fields=("employment_timing.end_date",),
    )


def _timing_candidate_is_compatible_with_explicit_evidence(
    candidate: ParsedTimingCandidate,
    evidence: EmploymentTimingEvidence,
) -> bool:
    timing = candidate.timing
    if evidence.start_date and (
        timing.start_date is None
        or _date_month_marker(evidence.start_date)
        != _date_month_marker(timing.start_date)
    ):
        return False
    if evidence.end_date and (
        timing.end_date is None
        or _date_month_marker(evidence.end_date) != _date_month_marker(timing.end_date)
    ):
        return False
    explicit_durations = {duration.months for duration in evidence.duration_terms}
    return not explicit_durations or timing.duration_months in explicit_durations


def _month_range_is_compatible_with_explicit_evidence(
    assessment: MonthRangeTimingAssessment,
    evidence: EmploymentTimingEvidence,
) -> bool:
    candidate = assessment.candidate
    if evidence.start_date and _date_month_marker(evidence.start_date) != (
        candidate.start_year,
        candidate.start_month,
    ):
        return False
    candidate_start = f"{candidate.start_year:04d}-{candidate.start_month:02d}-01"
    if (
        evidence.end_date
        and not _generic_end_date_precedes_start(evidence, candidate_start)
        and _date_month_marker(evidence.end_date)
        != (candidate.end_year, candidate.end_month)
    ):
        return False
    return True


def _generic_end_date_precedes_start(
    evidence: EmploymentTimingEvidence,
    start_date: str | None,
) -> bool:
    return bool(
        start_date
        and evidence.end_date
        and evidence.end_date < start_date
        and not evidence.start_dates
        and evidence.end_dates
        and all(item.label == "end date" for item in evidence.end_dates)
    )


def _academic_term_alternative_candidate(
    evidence: EmploymentTimingEvidence,
) -> ParsedTimingCandidate | None:
    for claim in _academic_term_claims(evidence.academic_terms).values():
        if len(claim) < 2:
            continue

        signatures = tuple(
            dict.fromkeys(_academic_term_timing_signature(term) for term in claim)
        )
        compatible_signatures = _compatible_academic_timing_signatures(
            signatures,
            start_date=evidence.start_date,
            end_date=evidence.end_date,
            duration_months=evidence.duration_months,
        )
        resolved_signatures = compatible_signatures or signatures
        start_dates = {signature.start_date for signature in resolved_signatures}
        end_dates = {signature.end_date for signature in resolved_signatures}
        durations = {signature.duration_months for signature in resolved_signatures}

        ambiguous_fields: list[str] = []
        if len(start_dates) > 1:
            ambiguous_fields.append("employment_timing.start_date")
        if len(durations) > 1:
            ambiguous_fields.append("employment_timing.duration_months")

        first, last = claim[0], claim[-1]
        raw_label = _clean_field_value(first.line[first.start : last.end])
        start_date = next(iter(start_dates)) if len(start_dates) == 1 else None
        duration_months = next(iter(durations)) if len(durations) == 1 else None
        end_date = next(iter(end_dates)) if len(end_dates) == 1 else None
        if "employment_timing.start_date" in ambiguous_fields:
            start_date = None
            end_date = None
            duration_months = None
        elif "employment_timing.duration_months" in ambiguous_fields:
            end_date = None
            duration_months = None

        return ParsedTimingCandidate(
            timing=ParsedTiming(
                raw_label=raw_label,
                timing_type="fixed_term",
                start_date=start_date,
                end_date=end_date,
                duration_months=duration_months,
            ),
            default_schedule_type="full_time",
            ambiguous_fields=tuple(ambiguous_fields),
        )
    return None


def _academic_term_claims(
    academic_terms: tuple[AcademicTermEvidence, ...],
) -> dict[int, list[AcademicTermEvidence]]:
    claims: dict[int, list[AcademicTermEvidence]] = {}
    for term in academic_terms:
        claims.setdefault(term.claim_group, []).append(term)
    return claims


def _academic_term_timing_signature(
    term: AcademicTermEvidence,
) -> AcademicTimingSignature:
    start_term, duration_months = _academic_term_defaults(term.term_key)
    start_month = ACADEMIC_TERM_MONTHS[start_term][0]
    start_date = f"{term.year:04d}-{start_month:02d}-01"
    return AcademicTimingSignature(
        start_date=start_date,
        end_date=_end_date_for_month_span(term.year, start_month, duration_months),
        duration_months=duration_months,
    )


def _compatible_academic_timing_signatures(
    signatures: tuple[AcademicTimingSignature, ...],
    *,
    start_date: str | None,
    end_date: str | None,
    duration_months: int | None,
) -> tuple[AcademicTimingSignature, ...]:
    return tuple(
        signature
        for signature in signatures
        if (
            start_date is None
            or _date_month_marker(signature.start_date) == _date_month_marker(start_date)
        )
        and (
            end_date is None
            or _date_month_marker(signature.end_date) == _date_month_marker(end_date)
        )
        and (duration_months is None or signature.duration_months == duration_months)
    )

def _academic_term_timing_candidate(
    academic_term: AcademicTermEvidence | None,
    *,
    alternative_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    if not academic_term:
        return None

    start_term, default_duration_months = _academic_term_defaults(academic_term.term_key)
    duration_months = (
        _single_academic_title_duration(academic_term.line)
        or _academic_term_duration_months(academic_term.line)
        or default_duration_months
    )
    start_month = ACADEMIC_TERM_MONTHS[start_term][0]
    academic_start_date = f"{academic_term.year:04d}-{start_month:02d}-01"
    if alternative_candidate:
        return ParsedTimingCandidate(
            timing=alternative_candidate.timing.model_copy(
                update={"start_date": academic_start_date}
            ),
            schedule_resolution=alternative_candidate.schedule_resolution,
            default_schedule_type="full_time",
            alternative_duration=True,
            prefer_academic_term_label=(
                alternative_candidate.prefer_academic_term_label
            ),
            prefer_start_date_term_label=(
                alternative_candidate.prefer_start_date_term_label
            ),
            duration_options=alternative_candidate.duration_options,
            ambiguous_fields=alternative_candidate.ambiguous_fields,
        )

    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=(
                f"{_titlecase_term_label(academic_term.term_label)} "
                f"{academic_term.year}"
            ),
            timing_type="fixed_term",
            start_date=academic_start_date,
            end_date=_end_date_for_month_span(
                academic_term.year,
                start_month,
                duration_months,
            ),
            duration_months=duration_months,
        ),
        default_schedule_type="full_time",
    )


def _combined_academic_source_title_candidate(
    academic_term: AcademicTermEvidence | None,
    *,
    alternative_candidate: ParsedTimingCandidate | None,
) -> ParsedTimingCandidate | None:
    if (
        academic_term is None
        or alternative_candidate is None
        or academic_term.term_key not in COMBINED_ACADEMIC_TERMS
        or bool(_field_label(academic_term.line))
        or len(academic_term.line) > 180
        or "." in academic_term.line
        or not re.search(
            r"\b(?:co-?op|coop|intern(?:ship)?|student)\b",
            academic_term.line,
            flags=re.IGNORECASE,
        )
    ):
        return None
    start_term, duration_months = _academic_term_defaults(academic_term.term_key)
    start_month = ACADEMIC_TERM_MONTHS[start_term][0]
    return ParsedTimingCandidate(
        timing=ParsedTiming(
            raw_label=(
                f"{_titlecase_term_label(academic_term.term_label)} "
                f"{academic_term.year}"
            ),
            timing_type="fixed_term",
            start_date=f"{academic_term.year:04d}-{start_month:02d}-01",
            end_date=_end_date_for_month_span(
                academic_term.year,
                start_month,
                duration_months,
            ),
            duration_months=duration_months,
            duration_label=(
                alternative_candidate.timing.duration_label
                or alternative_candidate.timing.raw_label
            ),
        ),
        schedule_resolution=alternative_candidate.schedule_resolution,
        default_schedule_type="full_time",
        prefer_academic_term_label=True,
        duration_options=alternative_candidate.duration_options,
        ambiguous_fields=_merge_ambiguous_fields(
            alternative_candidate.ambiguous_fields,
            ("employment_timing.duration_months",),
        ),
        authoritative_duration=True,
    )


def _cross_source_timing_ambiguities(
    evidence: EmploymentTimingEvidence,
    month_range_assessments: tuple[MonthRangeTimingAssessment, ...],
    *,
    alternative_duration_options: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    corroborating_duration_months = frozenset(
        duration.months for duration in evidence.duration_terms
    )
    start_claims: list[frozenset[tuple[int, int]]] = []
    if evidence.start_date:
        start_claims.append(frozenset({_date_month_marker(evidence.start_date)}))

    academic_claims: dict[int, set[tuple[int, int]]] = {}
    for term in evidence.academic_terms:
        start_term, _ = _academic_term_defaults(term.term_key)
        academic_claims.setdefault(term.claim_group, set()).add(
            (term.year, ACADEMIC_TERM_MONTHS[start_term][0])
        )
    start_claims.extend(frozenset(claim) for claim in academic_claims.values())

    end_claims: list[frozenset[tuple[int, int]]] = []
    ignore_generic_end = any(
        assessment.context_type == "employment_timing"
        and _generic_end_date_precedes_start(
            evidence,
            f"{assessment.candidate.start_year:04d}-"
            f"{assessment.candidate.start_month:02d}-01",
        )
        for assessment in month_range_assessments
    )
    if evidence.end_date and not ignore_generic_end:
        end_claims.append(frozenset({_date_month_marker(evidence.end_date)}))
    for assessment in month_range_assessments:
        candidate = assessment.candidate
        start_claims.append(frozenset({(candidate.start_year, candidate.start_month)}))
        end_claims.append(frozenset({(candidate.end_year, candidate.end_month)}))

    ambiguous_fields: list[str] = []
    if _independent_claims_conflict(start_claims):
        ambiguous_fields.append("employment_timing.start_date")
    if _independent_claims_conflict(end_claims):
        ambiguous_fields.append("employment_timing.end_date")

    duration_claims = [
        frozenset({duration.months}) for duration in evidence.duration_terms
    ]
    for assessment in month_range_assessments:
        semantic_durations = _semantic_durations_near_month_range(assessment)
        geometric_duration = frozenset(
            {
                _month_range_duration(
                    assessment.candidate,
                    corroborating_duration_months=corroborating_duration_months,
                )
            }
        )
        if semantic_durations and _month_range_has_month_qualified_term_duration(
            assessment
        ):
            duration_claims.append(semantic_durations)
        else:
            duration_claims.append(geometric_duration)
            duration_claims.extend(
                frozenset({duration}) for duration in semantic_durations
            )
    if alternative_duration_options:
        duration_claims.append(alternative_duration_options)

    has_non_academic_duration_claim = bool(duration_claims)
    for claim in _academic_term_claims(evidence.academic_terms).values():
        claim_has_combined_term = any(
            term.term_key in COMBINED_ACADEMIC_TERMS for term in claim
        )
        if has_non_academic_duration_claim and not claim_has_combined_term:
            continue
        duration_claims.append(
            frozenset(
                _academic_term_timing_signature(term).duration_months for term in claim
            )
        )
    if _independent_claims_conflict(duration_claims):
        ambiguous_fields.append("employment_timing.duration_months")
    return tuple(ambiguous_fields)


def _month_range_has_month_qualified_term_duration(
    assessment: MonthRangeTimingAssessment,
) -> bool:
    candidate = assessment.candidate
    clause = _timing_clause_for_span(
        candidate.segment,
        candidate.start,
        candidate.end,
    )
    return bool(
        re.search(
            r"\b\d{1,2}[\s-]+months?\s+"
            r"(?:co-?op\s+|work\s+)?term(?:\s+period)?\b",
            clause.text,
            flags=re.IGNORECASE,
        )
    )


def _date_month_marker(value: str) -> tuple[int, int]:
    return int(value[:4]), int(value[5:7])


def _independent_claims_conflict(
    claims: list[frozenset[_ClaimValue]],
) -> bool:
    unique_claims: list[frozenset[_ClaimValue]] = []
    for claim in claims:
        if claim and claim not in unique_claims:
            unique_claims.append(claim)
    if len(unique_claims) < 2:
        return False

    compatible_values = set(unique_claims[0])
    for claim in unique_claims[1:]:
        compatible_values.intersection_update(claim)
    return not compatible_values


def _merge_ambiguous_fields(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for field in group:
            if field not in merged:
                merged.append(field)
    return tuple(merged)


def _apply_chronology_invariant(
    timing: ParsedTiming,
    evidence: EmploymentTimingEvidence,
    ambiguous_fields: list[str],
) -> None:
    if not (
        timing.start_date
        and timing.end_date
        and timing.start_date > timing.end_date
    ):
        return

    timing.raw_label = None
    timing.duration_months = None
    if evidence.has_start_dates and not evidence.has_end_dates:
        timing.end_date = None
        discarded_fields = ("employment_timing.end_date",)
    elif evidence.has_end_dates and not evidence.has_start_dates:
        timing.start_date = None
        discarded_fields = ("employment_timing.start_date",)
    else:
        timing.start_date = None
        timing.end_date = None
        discarded_fields = (
            "employment_timing.start_date",
            "employment_timing.end_date",
        )

    for field in discarded_fields:
        if field not in ambiguous_fields:
            ambiguous_fields.append(field)

def _resolve_employment_candidate(
    candidate: ParsedTimingCandidate,
    evidence: EmploymentTimingEvidence,
    *,
    additional_ambiguous_fields: tuple[str, ...] = (),
) -> EmploymentTimingResolution:
    timing = candidate.timing.model_copy(deep=True)
    academic_labels = {
        (_titlecase_term_label(item.term_label), item.year)
        for item in evidence.academic_terms
    }
    authoritative_academic_label = _authoritative_academic_term_label(evidence)
    selected_academic_label = (
        authoritative_academic_label
        or (next(iter(academic_labels)) if len(academic_labels) == 1 else None)
    )
    if selected_academic_label and (
        candidate.prefer_academic_term_label
        or authoritative_academic_label is not None
    ):
        term_label, term_year = selected_academic_label
        timing.raw_label = f"{term_label} {term_year}"
        title_durations = {
            duration
            for item in evidence.academic_terms
            if item.is_title_authority
            if (duration := _single_academic_title_duration(item.line)) is not None
        }
        if len(title_durations) == 1:
            duration_months = next(iter(title_durations))
            start_term, _ = _academic_term_defaults(
                evidence.academic_terms[0].term_key
            )
            start_month = ACADEMIC_TERM_MONTHS[start_term][0]
            timing.duration_months = duration_months
            timing.end_date = _end_date_for_month_span(
                term_year,
                start_month,
                duration_months,
            )
    if evidence.has_start_dates:
        timing.start_date = evidence.start_date
        month_year_start_evidence = next(
            (
                item
                for item in evidence.start_dates
                if item.value == timing.start_date
                and item.raw_label
                and _parse_posting_date(item.raw_label) is None
                and len(_month_year_start_candidates(item.raw_label)) == 1
            ),
            None,
        ) if not evidence.academic_terms else None
        if month_year_start_evidence and month_year_start_evidence.raw_label:
            start_year, start_month = map(int, timing.start_date.split("-")[:2])
            timing.raw_label = _academic_term_label_for_month_year_start(
                start_month,
                start_year,
            ) or month_year_start_evidence.raw_label
    if evidence.has_end_dates and not (
        candidate.authoritative_date_range
        and _generic_end_date_precedes_start(evidence, timing.start_date)
    ):
        timing.end_date = evidence.end_date
    if candidate.prefer_start_date_term_label and timing.start_date:
        start_year, start_month, start_day = (
            int(part) for part in timing.start_date.split("-")
        )
        timing.raw_label = _academic_term_label_for_start_date(
            start_year,
            start_month,
            start_day,
        )

    if candidate.authoritative_date_range and timing.start_date and timing.end_date:
        if not candidate.authoritative_duration:
            candidate_duration = candidate.timing.duration_months
            timing.duration_months = (
                candidate_duration
                if candidate_duration is not None
                and _date_range_supports_duration(
                    timing.start_date,
                    timing.end_date,
                    candidate_duration,
                )
                else _duration_months_from_date_range(
                    timing.start_date,
                    timing.end_date,
                )
            )
    elif timing.start_date and timing.end_date and evidence.has_end_dates:
        timing.duration_months = _duration_months_from_date_range(
            timing.start_date,
            timing.end_date,
        )
    elif evidence.has_duration_terms and not candidate.authoritative_duration:
        timing.duration_months = evidence.duration_months
        if not evidence.has_end_dates:
            timing.end_date = None
            if timing.start_date and timing.duration_months:
                start_year = int(timing.start_date[:4])
                start_month = int(timing.start_date[5:7])
                timing.end_date = _end_date_for_month_span(
                    start_year,
                    start_month,
                    timing.duration_months,
                )
    elif timing.start_date and timing.end_date:
        timing.duration_months = _duration_months_from_date_range(
            timing.start_date,
            timing.end_date,
        )
    elif candidate.alternative_duration:
        timing.duration_months = None
    elif timing.start_date and timing.duration_months and not evidence.has_end_dates:
        start_year = int(timing.start_date[:4])
        start_month = int(timing.start_date[5:7])
        timing.end_date = _end_date_for_month_span(
            start_year,
            start_month,
            timing.duration_months,
        )
    elif evidence.has_start_dates and timing.start_date is None and not evidence.has_end_dates:
        timing.end_date = None

    schedule_resolution = _candidate_schedule_resolution(candidate, evidence)
    timing.schedule_type = (
        _academic_schedule_value(schedule_resolution)
        if candidate.default_schedule_type == "full_time"
        else schedule_resolution.value
    )
    ambiguous_fields = list(
        _merge_ambiguous_fields(
            candidate.ambiguous_fields,
            evidence.ambiguous_fields,
            additional_ambiguous_fields,
        )
    )
    _apply_chronology_invariant(timing, evidence, ambiguous_fields)
    if (
        candidate.duration_options
        and (
            timing.duration_months is None
            or timing.duration_months not in candidate.duration_options
        )
        and "employment_timing.duration_months" not in ambiguous_fields
    ):
        ambiguous_fields.append("employment_timing.duration_months")
    return _employment_resolution(
        timing,
        schedule_resolution,
        ambiguous_fields=tuple(ambiguous_fields),
    )


def _has_authoritative_academic_term_label(
    evidence: EmploymentTimingEvidence,
) -> bool:
    return _authoritative_academic_term_label(evidence) is not None


def _authoritative_academic_term_label(
    evidence: EmploymentTimingEvidence,
) -> tuple[str, int] | None:
    labels = {
        (_titlecase_term_label(item.term_label), item.year)
        for item in evidence.academic_terms
        if item.is_title_authority
    }
    return next(iter(labels)) if len(labels) == 1 else None


def _single_academic_title_duration(line: str) -> int | None:
    if list(_alternative_duration_matches(line)):
        return None
    if not re.search(
        rf"\b(?:{ACADEMIC_TERM_PATTERN})\s*,?\s*20\d{{2}}\b",
        line,
        flags=re.IGNORECASE,
    ):
        return None
    if not re.search(
        r"\b(?:analyst|developer|engineer|intern(?:ship)?|co-?op|student)\b",
        line,
        flags=re.IGNORECASE,
    ):
        return None
    if not re.search(
        r"(?:[-\u2013\u2014,(]\s*)\d{1,2}\s*-?months?\)?\s*$",
        line,
        flags=re.IGNORECASE,
    ):
        return None
    durations = {months for months, *_ in _duration_month_candidates(line)}
    return next(iter(durations)) if len(durations) == 1 else None


def _candidate_schedule_resolution(
    candidate: ParsedTimingCandidate,
    evidence: EmploymentTimingEvidence,
) -> ScheduleEvidenceResolution:
    strong_resolution = evidence.schedule_fields.merged(evidence.schedule_prose).merged(
        candidate.schedule_resolution
    )
    if strong_resolution.positive_values:
        return strong_resolution
    return strong_resolution.merged(evidence.schedule_defaults)


def _employment_resolution(
    timing: ParsedTiming,
    schedule_resolution: ScheduleEvidenceResolution,
    *,
    ambiguous_fields: tuple[str, ...] = (),
) -> EmploymentTimingResolution:
    resolved_ambiguities = list(ambiguous_fields)
    if (
        schedule_resolution.is_ambiguous
        and "employment_timing.schedule_type" not in resolved_ambiguities
    ):
        resolved_ambiguities.append("employment_timing.schedule_type")
    return EmploymentTimingResolution(
        timing=timing,
        ambiguous_fields=tuple(resolved_ambiguities),
    )


def _application_deadline(raw_text: str) -> str | None:
    if re.search(r"\bday\s+prior\s+to\s+the\s+application\s+deadline\s+date\s+above\b",
                 raw_text, re.I):
        headers = {_parse_posting_date(m['date']) for m in re.finditer(
            r"(?i)\bApplication Deadline\s*:?\s*(?P<date>20\d{2}-\d{2}-\d{2})\b",
            raw_text,
        )}
        formal_text = re.sub(r"\s+,", ",", " ".join(raw_text.split()))
        formal = {_parse_posting_date(m['date']) for m in re.finditer(
            r"\bformal\s+application\s+deadline\s+is\s+(?P<date>[^.;\n]+)", formal_text, re.I,
        )}
        if len(headers) == 1 and None not in headers:
            closing = (date.fromisoformat(next(iter(headers))) - timedelta(days=1)).isoformat()
            return closing if not formal or formal == {closing} else None
    deadline_labels = {
        "application deadline",
        "apply by",
        "closing date",
        "close date",
        "posting close date",
        "posting closing date",
    }
    multiline_label = re.search(
        r"(?im)^\s*(?:application deadline|apply by|closing date|close date|"
        r"posting close date|posting closing date)\s*:\s*"
        r"(?P<date>\S[^\n]{0,80})",
        raw_text,
    )
    if multiline_label:
        candidate = multiline_label.group("date")
        if _starts_with_date_literal(candidate):
            parsed = _parse_posting_date(candidate)
            if parsed:
                return parsed
    for line in raw_text.splitlines():
        label = _field_label(line)
        if label in deadline_labels:
            value = line.split(":", 1)[1] if ":" in line else line
            parsed = _parse_posting_date(value)
            if parsed:
                return parsed
        match = re.search(
            r"\b(?:applications?\s+)?(?:close|closing|deadline|apply\s+by)(?:\s+date)?[:\s]+(?P<date>[^.;\n]+)",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            parsed = _parse_posting_date(match.group("date"))
            if parsed:
                return parsed
    return None


def _starts_with_date_literal(value: str) -> bool:
    return bool(
        re.match(
            rf"^\s*(?:(?:{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\b|"
            r"\d{1,4}(?:\s*[-/]\s*|\s+))",
            value,
            flags=re.IGNORECASE,
        )
    )


def _parse_duration_term_start_timing(raw_text: str) -> ParsedTimingCandidate | None:
    for segment in _timing_candidate_segments(raw_text):
        timing = _parse_duration_term_start_segment(segment)
        if timing:
            return timing
    return None



def _employment_month_range_assessments(
    raw_text: str,
) -> tuple[MonthRangeTimingAssessment, ...]:
    candidates: list[MonthRangeTimingCandidate] = []
    for segment in _timing_candidate_segments(raw_text):
        candidates.extend(
            _extract_month_range_timing_candidates(segment, order_offset=len(candidates))
        )
    assessments: list[MonthRangeTimingAssessment] = []
    recognized_keys: set[tuple[object, ...]] = set()
    for candidate in candidates:
        assessment = _classify_month_range_timing_candidate(candidate)
        key = _month_range_candidate_identity(candidate)
        if (
            assessment.context_type != MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING
            or key in recognized_keys
        ):
            continue
        assessments.append(assessment)
        recognized_keys.add(key)
    for candidate in _extract_month_range_timing_candidates(
        raw_text,
        order_offset=len(candidates),
    ):
        key = _month_range_candidate_identity(candidate)
        if (
            key in recognized_keys
            or not _duration_field_precedes_generic_date_range(candidate)
        ):
            continue
        assessment = _classify_month_range_timing_candidate(candidate)
        if assessment.context_type != MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING:
            continue
        assessments.append(assessment)
        recognized_keys.add(key)
    return tuple(assessments)


def _month_range_candidate_identity(
    candidate: MonthRangeTimingCandidate,
) -> tuple[object, ...]:
    return (
        candidate.start_year,
        candidate.start_month,
        candidate.end_year,
        candidate.end_month,
        candidate.start_day,
        candidate.end_day,
        candidate.raw_label.casefold(),
    )


def _semantic_durations_near_month_range(
    assessment: MonthRangeTimingAssessment,
) -> frozenset[int]:
    candidate = assessment.candidate
    clause = _timing_clause_for_span(
        candidate.segment,
        candidate.start,
        candidate.end,
    )
    return _semantic_term_duration_values(clause.text)


def _select_month_range_timing_assessment(
    assessments: tuple[MonthRangeTimingAssessment, ...],
) -> MonthRangeTimingAssessment | None:
    if not assessments:
        return None
    return max(
        assessments,
        key=lambda assessment: (assessment.confidence, -assessment.candidate.order),
    )


def _timing_candidate_segments(raw_text: str) -> list[str]:
    return list(_immutable_timing_candidate_segments(raw_text))


@session_cached
def _immutable_timing_candidate_segments(raw_text: str) -> tuple[str, ...]:
    lines = _posting_content_lines(raw_text)
    candidates = list(lines)
    candidates.extend(f"{line} {lines[index + 1]}" for index, line in enumerate(lines[:-1]))
    seen: set[str] = set()
    normalized: list[str] = []
    for candidate in candidates:
        stripped = _strip_timing_note_label(candidate)
        if stripped and stripped not in seen:
            seen.add(stripped)
            normalized.append(stripped)
    return tuple(normalized)


def _employment_date_evidence(
    raw_text: str,
    kind: str,
    fields: tuple[PostingFieldSpan, ...] | None = None,
) -> tuple[EmploymentDateEvidence, ...]:
    allowed_labels = (
        _EMPLOYMENT_START_FIELD_LABELS if kind == "start" else _EMPLOYMENT_END_FIELD_LABELS
    )
    resolved_fields = fields if fields is not None else _posting_field_spans(raw_text)
    evidence: list[EmploymentDateEvidence] = []
    for field in resolved_fields:
        month_year_term = kind == "start" and (
            field.label in _EMPLOYMENT_ACADEMIC_TERM_FIELD_LABELS
            and re.fullmatch(
                rf"\s*(?:{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})"
                r"\.?\s*,?\s+20\d{2}\s*",
                field.value,
                flags=re.IGNORECASE,
            ) is not None
        )
        if field.label not in allowed_labels and not month_year_term:
            continue
        parsed_date = _parse_posting_date(field.value)
        raw_label = _clean_field_value(field.value)
        if parsed_date is None and kind == "start":
            # Browser captures can split the program sentence and its date across
            # lines. Match only the complete continuation of this start field,
            # without borrowing a date from a subsequent field or paragraph.
            continuation = re.match(
                rf"\s*The\s+internship\s+program\s+begins\s+on\s+"
                rf"(?P<date>{_posting_date_text_pattern()})\b",
                raw_text[field.value_start :],
                flags=re.IGNORECASE,
            )
            if continuation:
                parsed_date = _parse_posting_date(continuation.group("date"))
                raw_label = " ".join(continuation.group().split())
        if parsed_date is None and kind == "start":
            month_year_candidates = _month_year_start_candidates(field.value)
            if len(month_year_candidates) == 1:
                parsed_date = month_year_candidates[0][3]
                raw_label = _clean_field_value(month_year_candidates[0][2])
        if parsed_date:
            evidence.append(
                EmploymentDateEvidence(
                    kind=kind,
                    value=parsed_date,
                    label=field.label,
                    offset=field.start,
                    raw_label=raw_label,
                )
            )
    return tuple(evidence)


def _employment_duration_evidence(
    fields: tuple[PostingFieldSpan, ...],
) -> tuple[EmploymentDurationEvidence, ...]:
    evidence: list[EmploymentDurationEvidence] = []
    for field in fields:
        if field.label not in _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS:
            continue
        if any(_alternative_duration_matches(field.value)):
            continue
        for months, duration_start, duration_end, duration_text in _duration_month_candidates(
            field.value
        ):
            if _duration_is_optional_extension(field.value, duration_start):
                continue
            raw_label = _duration_term_label(
                field.value,
                duration_start,
                duration_end,
            ) or _clean_field_value(duration_text)
            evidence.append(
                EmploymentDurationEvidence(
                    months=months,
                    raw_label=raw_label,
                    label=field.label,
                    offset=field.value_start + duration_start,
                )
            )
    return tuple(evidence)


@session_cached
def _employment_timing_evidence(raw_text: str) -> EmploymentTimingEvidence:
    fields = _posting_field_spans(raw_text)
    start_dates = _employment_date_evidence(raw_text, "start", fields)
    end_dates = _employment_date_evidence(raw_text, "end", fields)
    duration_terms = _employment_duration_evidence(fields)
    academic_terms = _employment_academic_term_evidence(raw_text)
    relevant_schedule_labels = (
        _EMPLOYMENT_SCHEDULE_FIELD_LABELS | _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
    )
    return EmploymentTimingEvidence(
        start_dates=start_dates,
        end_dates=end_dates,
        duration_terms=duration_terms,
        academic_terms=academic_terms,
        schedule_fields=_schedule_resolution_from_field_spans(
            fields,
            relevant_schedule_labels,
        ),
        schedule_prose=_employment_prose_schedule_resolution(raw_text, fields),
        schedule_defaults=_employment_default_schedule_resolution(raw_text, fields),
        semantic_ambiguous_fields=_semantic_timing_ambiguities(
            raw_text,
            start_dates=start_dates,
            end_dates=end_dates,
            duration_terms=duration_terms,
            academic_terms=academic_terms,
        ),
    )


def _semantic_timing_ambiguities(
    raw_text: str,
    *,
    start_dates: tuple[EmploymentDateEvidence, ...],
    end_dates: tuple[EmploymentDateEvidence, ...],
    duration_terms: tuple[EmploymentDurationEvidence, ...],
    academic_terms: tuple[AcademicTermEvidence, ...],
) -> tuple[str, ...]:
    ambiguous_fields: list[str] = []
    start_date = _single_employment_date(start_dates)
    end_date = _single_employment_date(end_dates)

    academic_claims = _academic_term_claims(academic_terms)
    academic_start_is_ambiguous = any(
        (
            start_date is not None
            and not any(
                _academic_term_matches_start_date(term, start_date) for term in claim
            )
        )
        or (
            start_date is None
            and len(_academic_term_start_markers(claim)) > 1
        )
        for claim in academic_claims.values()
    )
    if academic_start_is_ambiguous:
        ambiguous_fields.append("employment_timing.start_date")

    explicit_duration_values = {term.months for term in duration_terms}
    explicit_duration = (
        next(iter(explicit_duration_values)) if len(explicit_duration_values) == 1 else None
    )
    academic_duration_is_ambiguous = any(
        len(
            {
                signature.duration_months
                for signature in (
                    _compatible_academic_timing_signatures(
                        tuple(
                            dict.fromkeys(
                                _academic_term_timing_signature(term) for term in claim
                            )
                        ),
                        start_date=start_date,
                        end_date=end_date,
                        duration_months=explicit_duration,
                    )
                )
            }
        )
        > 1
        for claim in academic_claims.values()
    )
    if academic_duration_is_ambiguous:
        ambiguous_fields.append("employment_timing.duration_months")

    semantic_durations = _semantic_term_duration_values(raw_text)
    if len(semantic_durations) > 1:
        ambiguous_fields.append("employment_timing.duration_months")
    elif semantic_durations and start_date and end_date:
        date_range_duration = _duration_months_from_date_range(start_date, end_date)
        if date_range_duration is not None and date_range_duration not in semantic_durations:
            ambiguous_fields.append("employment_timing.duration_months")
    return tuple(ambiguous_fields)


def _academic_term_start_markers(
    claim: list[AcademicTermEvidence],
) -> frozenset[tuple[int, int]]:
    return frozenset(
        (
            term.year,
            ACADEMIC_TERM_MONTHS[_academic_term_defaults(term.term_key)[0]][0],
        )
        for term in claim
    )

def _semantic_term_duration_values(raw_text: str) -> frozenset[int]:
    values: set[int] = set()
    for segment in _timing_candidate_segments(raw_text):
        alternative_spans = [match.span() for match in _alternative_duration_matches(segment)]
        for duration_months, duration_start, duration_end, _ in _duration_month_candidates(
            segment
        ):
            if any(
                duration_start < alternative_end and alternative_start < duration_end
                for alternative_start, alternative_end in alternative_spans
            ):
                continue
            if _duration_term_label(segment, duration_start, duration_end):
                values.add(duration_months)
    return frozenset(values)


def _employment_academic_term_evidence(raw_text: str) -> tuple[AcademicTermEvidence, ...]:
    lines = _posting_content_lines(raw_text)
    title = _title_value(lines)
    pattern = re.compile(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b"
        rf"(?:(?:\s+(?:term|semester)\b)?\s*,?\s*(?:of\s+)?(?P<year>20\d{{2}})\b)?",
        flags=re.IGNORECASE,
    )
    seasonal_role_title_pattern = re.compile(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b"
        rf"\s+(?:intern(?:ship)?|co-?op|student)\s*"
        r"(?:[-\u2013\u2014|:,]\s*)?"
        rf"(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    year_leading_seasonal_role_title_pattern = re.compile(
        rf"\b(?P<year>20\d{{2}})\b[^()\n]{{0,80}}"
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b\s+"
        r"(?:(?:business(?:\s+and\s+data)?|data|financial|software|systems?)\s+)?"
        r"(?:analyst|developer|engineer|intern(?:ship)?|co-?op|student)\b",
        flags=re.IGNORECASE,
    )
    year_leading_title_pattern = re.compile(
        rf"\b(?P<year>20\d{{2}})\b[^()\n]{{0,80}}?"
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b"
        rf"(?!\s*[/\-\u2013]\s*(?:{ACADEMIC_TERM_PATTERN}))",
        flags=re.IGNORECASE,
    )
    evidence: list[AcademicTermEvidence] = []
    next_claim_group = 0
    for line in lines:
        if _field_label(line) == "source evidence":
            continue
        label = _field_label(line)
        line_is_title = (
            label in {
                "ingest title hint",
                "job title",
                "page title",
                "position",
                "role",
                "title",
            }
            or _provenance_field_semantic(label) == "role"
            or (not label and title is not None and _title_from_mixed_line(line) == title)
            or (
                not label
                and len(line) <= 180
                and re.search(
                    r"\b(?:co-?op|coop|intern(?:ships?)?|student)\b",
                    line,
                    flags=re.IGNORECASE,
                )
                and not re.search(r"[.!?](?:\s|$)", line)
                and re.search(
                    rf"\b(?:{ACADEMIC_TERM_PATTERN})\b"
                    rf"(?:\s+(?:term|semester)\b)?\s*,?\s*20\d{{2}}\b",
                    line,
                    flags=re.IGNORECASE,
                )
            )
        )
        matches = list(pattern.finditer(line))
        year_leading_title_matches = [
            match
            for match in year_leading_seasonal_role_title_pattern.finditer(line)
            if line_is_title
            or (
                len(line) <= 160
                and match.start() <= 12
            )
        ]
        if line_is_title and not any(match.group("year") for match in matches):
            year_leading_title_matches.extend(
                year_leading_title_pattern.finditer(line)
            )
        year_leading_title_spans = {
            (match.start(), match.end()) for match in year_leading_title_matches
        }
        matches.extend(year_leading_title_matches)
        seasonal_role_title_matches = list(
            seasonal_role_title_pattern.finditer(line)
        )
        if line_is_title:
            matches.extend(seasonal_role_title_matches)
        else:
            matches.extend(
                match
                for match in seasonal_role_title_matches
                if len(line) <= 160 and match.start() <= 12
            )
        title_authority_spans = (
            {(match.start(), match.end()) for match in matches}
            if line_is_title
            else {
                (match.start(), match.end())
                for match in (
                    *year_leading_title_matches,
                    *seasonal_role_title_matches,
                )
                if len(line) <= 160 and match.start() <= 12
            }
        )
        matches.sort(key=lambda match: (match.start(), -match.end()))
        matches = [
            match
            for index, match in enumerate(matches)
            if not any(
                other.start() <= match.start() and match.end() <= other.end()
                for other in matches[:index]
            )
        ]
        coordinated_groups: list[list[re.Match[str]]] = []
        for match in matches:
            if (
                coordinated_groups
                and _academic_term_connector(
                    line[coordinated_groups[-1][-1].end() : match.start()]
                )
            ):
                coordinated_groups[-1].append(match)
            else:
                coordinated_groups.append([match])

        for matches_in_group in coordinated_groups:
            explicit_year_indexes = [
                index
                for index, match in enumerate(matches_in_group)
                if match.group("year")
            ]
            if not explicit_year_indexes:
                continue

            claim_group = next_claim_group
            next_claim_group += 1
            for index, match in enumerate(matches_in_group):
                # Short eligibility prose can resemble a seasonal role title.
                # School-return dates remain education evidence in that case.
                if re.search(
                    r"\breturn(?:ing)?\s+to\s+school\s+"
                    r"(?:(?:in|next|the)\s+|(?:winter|summer|spring|fall)\s+(?:or|and)\s+)*$",
                    line[:match.start()], flags=re.IGNORECASE,
                ):
                    continue
                if (
                    (match.start(), match.end()) not in year_leading_title_spans
                    and not line_is_title
                    and not _academic_term_is_employment_context(line, match, title=title)
                ):
                    continue
                year_source_index = min(
                    explicit_year_indexes,
                    key=lambda candidate_index: (
                        abs(candidate_index - index),
                        candidate_index < index,
                    ),
                )
                year = int(matches_in_group[year_source_index].group("year"))
                item = AcademicTermEvidence(
                    term_key=_canonical_term_key(match.group("term")),
                    term_label=match.group("term"),
                    year=year,
                    claim_group=claim_group,
                    line=line,
                    start=match.start(),
                    end=match.end(),
                    is_title_authority=(match.start(), match.end())
                    in title_authority_spans,
                )
                if item not in evidence:
                    evidence.append(item)
    return tuple(evidence)


def _academic_term_connector(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:(?:,\s*)?(?:and\s*/\s*or|and|or)|/|,|&)\s*(?:the\s+)?",
            value,
            flags=re.IGNORECASE,
        )
    )


def _academic_term_is_employment_context(
    line: str,
    match: re.Match[str],
    *,
    title: str | None,
) -> bool:
    prefix = line[max(0, match.start() - 40) : match.start()]
    suffix = line[match.end() : match.end() + 30]
    if re.search(
        r"\bavailable\s+to\s+work(?:\s+with\s+us)?\s+(?:part|full)[-\s]*time"
        r"(?:,\s*\d+(?:\.\d+)?\s+hours\s+per\s+week,)?\s+(?:in|for)\s+(?:the\s+)?$",
        line[:match.start()], flags=re.IGNORECASE,
    ):
        return True
    if re.search(r"\b(?:fully\s+)?funded\s*$", prefix, flags=re.IGNORECASE) and re.match(
        r"\s+PhD\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(
        r"\s+(?:semester\s+)?work[\s-]+term\s+"
        r"(?:opportunity|placement|position|role)\b",
        line[match.end() :],
        flags=re.IGNORECASE,
    ):
        return True
    return _timing_mention_is_employment_context(
        line,
        match.start(),
        match.end(),
        title=title,
    )


def _timing_mention_is_employment_context(
    line: str,
    start: int,
    end: int,
    *,
    title: str | None,
) -> bool:
    clause = _timing_clause_for_span(line, start, end)
    label = _field_label(clause.text)
    non_employment_labels = NON_EMPLOYMENT_TIMING_DATE_LABELS | {
        "education type",
        "enrollment",
        "enrollment status",
        "interview date",
        "interview window",
        "student status",
    }
    if label in non_employment_labels:
        return False
    if _timing_mention_is_non_employment_context(line, start, end):
        return False
    if clause.context_type == TIMING_CLAUSE_CONTEXT_EMPLOYMENT:
        return True
    if label in (
        _EMPLOYMENT_ACADEMIC_TERM_FIELD_LABELS
        | _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
        | _EMPLOYMENT_START_FIELD_LABELS
        | _EMPLOYMENT_END_FIELD_LABELS
    ):
        return True
    if _is_standalone_academic_term_clause(clause.text):
        return True
    if label in {
        "ingest title hint",
        "job title",
        "page title",
        "position",
        "role",
        "title",
    }:
        return True
    if not label and title and _title_from_mixed_line(clause.text) == title:
        return True
    if len(clause.text) <= 180 and re.search(
        r"\bstudent\s+(?:researcher|research\s+assistant|assistant|developer|"
        r"analyst|coordinator)\b",
        clause.text,
        flags=re.IGNORECASE,
    ):
        return True

    return bool(
        re.search(
            r"\b(?:co-?op|employment|intern(?:ship)?|job|placement|position|role|"
            r"work[\s-]+term)\b",
            clause.text,
            flags=re.IGNORECASE,
        )
    )



def _is_standalone_academic_term_clause(clause_text: str) -> bool:
    return bool(
        re.fullmatch(
            rf"\s*(?:{ACADEMIC_TERM_PATTERN})\s+20\d{{2}}\s*[.!]?\s*",
            clause_text,
            flags=re.IGNORECASE,
        )
    )
def _timing_mention_is_non_employment_context(
    line: str,
    start: int,
    end: int,
) -> bool:
    clause = _timing_clause_for_span(line, start, end)
    if clause.context_type in {
        TIMING_CLAUSE_CONTEXT_APPLICATION,
        TIMING_CLAUSE_CONTEXT_INTERVIEW,
    }:
        return True
    local_start = start - clause.start
    local_end = end - clause.start
    if _schedule_mention_is_education_context(clause.text, local_start, local_end):
        return True

    prefix = clause.text[max(0, local_start - 90) : local_start]
    suffix = clause.text[local_end : local_end + 70]
    if _timing_prefix_is_history_context(prefix):
        return True
    if re.search(
        r"\b(?:applications?|interviews?)\s+(?:for\s+)?(?:the\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(
        r"\s+(?:(?:co-?op|internship|job|position|role)\s+)?"
        r"(?:applications|interviews)\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:applications\b[^.;:\n]{0,35}\b"
        r"(?:accepted|available|close|closing|due|open)|"
        r"application\s+(?:(?:is|remains?)\s+)?(?:due|open)|"
        r"apply\s+(?:before|by|during|through|until)|"
        r"(?:application|interview|posting|recruiting)\s+"
        r"(?:cycle|dates?|deadline|period|term|window)|"
        r"interviews?\s+(?:(?:are|will\s+be)\s+)?(?:available|held|scheduled))\b"
        r"[^.;:\n]{0,50}$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"\s+(?:application|interview|posting|recruiting)\s+"
            r"(?:cycle|dates?|deadline|period|term|window)\b",
            suffix,
            flags=re.IGNORECASE,
        )
    )

def _academic_term_matches_start_date(
    term: AcademicTermEvidence,
    start_date: str,
) -> bool:
    start_year, start_month, _ = (int(part) for part in start_date.split("-"))
    start_term, _ = _academic_term_defaults(term.term_key)
    expected_start_month = ACADEMIC_TERM_MONTHS[start_term][0]
    return (
        start_year == term.year
        and expected_start_month <= start_month <= expected_start_month + 3
    )


def _employment_prose_schedule_resolution(
    raw_text: str,
    fields: tuple[PostingFieldSpan, ...] | None = None,
) -> ScheduleEvidenceResolution:
    resolved_fields = fields if fields is not None else _posting_field_spans(raw_text)
    resolution = _employment_schedule_resolution_from_text(
        _text_without_labeled_fields(raw_text, resolved_fields)
    )
    for field in resolved_fields:
        if field.label in _EMPLOYMENT_DESCRIPTIVE_SCHEDULE_FIELD_LABELS:
            resolution = resolution.merged(_employment_schedule_resolution_from_text(field.value))
    return resolution


def _employment_schedule_resolution_from_text(raw_text: str) -> ScheduleEvidenceResolution:
    return _schedule_evidence_for_cued_clauses(
        raw_text,
        (
            r"\bcontracttypeandduration\b",
            r"\b(?:available|able|expected|required|willing|prepared)\b"
            r"[^.;\n]{0,70}\bwork\b",
            r"\b(?:the\s+)?(?:role|position|internship|placement|contract|job|schedule)\s+"
            r"(?:is|will\s+be|would\s+be|may\s+be|can\s+be)\b",
            r"\bthis\s+(?:is|will\s+be)\b",
            r"\brequir(?:e|es|ed)\b",
        ),
    )


def _employment_default_schedule_resolution(
    raw_text: str,
    fields: tuple[PostingFieldSpan, ...] | None = None,
) -> ScheduleEvidenceResolution:
    resolved_fields = fields if fields is not None else _posting_field_spans(raw_text)
    resolution = _default_schedule_resolution_from_text(
        _text_without_labeled_fields(raw_text, resolved_fields)
    )
    for field in resolved_fields:
        if field.label in _EMPLOYMENT_DESCRIPTIVE_SCHEDULE_FIELD_LABELS:
            resolution = resolution.merged(_default_schedule_resolution_from_text(field.value))
    return resolution


def _default_schedule_resolution_from_text(raw_text: str) -> ScheduleEvidenceResolution:
    resolution = _schedule_evidence_for_cued_clauses(
        raw_text,
        (r"(?:^|[.;\n])\s*(?:full[-\s]?time|part[-\s]?time)\b",),
    )
    hours_min, hours_max = _hours_per_week_range(raw_text)
    if hours_min is not None and hours_min >= 35:
        resolution = resolution.merged(
            ScheduleEvidenceResolution(positive_values=frozenset({"full_time"}))
        )
    elif hours_max is not None and hours_max < 35:
        resolution = resolution.merged(
            ScheduleEvidenceResolution(positive_values=frozenset({"part_time"}))
        )
    return resolution


def _schedule_resolution_from_field_spans(
    fields: tuple[PostingFieldSpan, ...],
    allowed_labels: set[str],
) -> ScheduleEvidenceResolution:
    resolution = ScheduleEvidenceResolution()
    for field in fields:
        if field.label in allowed_labels:
            resolution = resolution.merged(_schedule_resolution_from_context(field.value))
    return resolution


def _schedule_evidence_for_cued_clauses(
    raw_text: str,
    patterns: tuple[str, ...],
) -> ScheduleEvidenceResolution:
    resolution = ScheduleEvidenceResolution()
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE):
            clause = _timing_clause_for_span(raw_text, match.start(), match.end())
            resolution = resolution.merged(_schedule_resolution_from_context(clause.text))
    return resolution


def _french_contract_duration_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Read a duration field corroborated by this role's contract mandate."""
    mandates = {
        int(match["months"])
        for match in re.finditer(r"\bpour\s+un\s+mandat\s+de\s+(?P<months>\d{1,2})\s+mois\b",
                                 raw_text, re.I)
    }
    fields = {
        int(match["months"])
        for match in re.finditer(r"(?m)^\s*Durée\s*:\s*(?P<months>\d{1,2})\s+mois\s*$",
                                 raw_text, re.I)
    }
    if len(mandates) != 1 or fields != mandates:
        return None
    months = mandates.pop()
    if not 1 <= months <= 24:
        return None
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f"{months} mois", timing_type="fixed_term", duration_months=months),
        authoritative_duration=True,
    )


def _year_leading_role_term_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Keep the stated term when availability leaves the start month open."""
    claims = {
        (match["term"].title(), int(match["year"]))
        for match in re.finditer(
            r"\bfor\s+the\s+(?P<year>20\d{2})\s+(?P<term>winter|summer|fall)\s+term\b",
            raw_text, re.I,
        )
    }
    if len(claims) != 1:
        return None
    term, year = next(iter(claims))
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    durations = set()
    for match in re.finditer(
        rf"\bavailable\s+to\s+work\s+for\s+the\s+full\s+{term}\s+term\s*\("
        rf"(?P<duration>\d{{1,2}}\s*[-–—]\s*\d{{1,2}}\s+months?)\s+"
        rf"starting\s+(?:Late\s+)?(?P<first>{month_pattern})\s+or\s+(?P<second>{month_pattern})\s*\)",
        raw_text, re.I,
    ):
        if all(_academic_term_label_for_start_month(MONTH_NAMES[match[name].casefold()], year)
               == f"{term} {year}" for name in ("first", "second")):
            durations.add(_clean_field_value(match["duration"]))
    if len(durations) != 1:
        return None
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=f"{term} {year}", timing_type="fixed_term",
                            duration_label=durations.pop()),
        alternative_duration=True,
        ambiguous_fields=("employment_timing.start_date", "employment_timing.end_date",
                          "employment_timing.duration_months"),
    )


def _standalone_role_duration_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    title = _title_value(_posting_content_lines(raw_text)) or ""
    if not re.search(r"\b(?:co-?op|intern(?:ship)?|student)\b", title, re.I):
        return None
    labels = {
        _clean_field_value(match["duration"])
        for match in re.finditer(
            r"(?m)^\s*Pay\s+Range\s*:\s*[^\n]+\n\s*"
            r"(?P<duration>\d{1,2}\s*[-–—]\s*\d{1,2}\s+months?)\s*\n"
            r"\s*What\s+You(?:'|’)ll\s+Be\s+Doing\b",
            raw_text, re.I,
        )
        if all(1 <= int(value) <= 24 for value in re.findall(r"\d+", match["duration"]))
    }
    if len(labels) != 1:
        return None
    label = labels.pop()
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=label, timing_type="fixed_term", duration_label=label),
        alternative_duration=True,
        ambiguous_fields=("employment_timing.duration_months",),
    )


def _qualified_program_duration_candidate(raw_text: str) -> ParsedTimingCandidate | None:
    """Retain source qualifications instead of choosing an exact duration."""
    labels: set[str] = set()
    for match in re.finditer(
        rf"\bcontract\s+period\s+of\s+(?P<label>approximately\s+"
        rf"(?P<months>\d{{1,2}}|{'|'.join(DURATION_NUMBER_WORDS)})\s+months?)\b",
        raw_text, re.I,
    ):
        value = match["months"].casefold()
        months = int(value) if value.isdigit() else DURATION_NUMBER_WORDS[value]
        if 1 <= months <= 24:
            labels.add(_clean_field_value(match["label"]))
    for match in re.finditer(
        r"\bterm\s*\(?\s*up\s*to\s+(?P<months>\d{1,2})\s+months?\b",
        raw_text, re.I,
    ):
        if 1 <= int(match["months"]) <= 24:
            labels.add(f"up to {int(match['months'])} months")
    for match in re.finditer(
        r"\b(?P<primary>\d{1,2})\s+month\s+co-op\s+term\s+with\s+"
        r"(?P<alternative>\d{1,2})\s+month\s+terms\s+occasionally\s+being\s+available\b",
        raw_text, re.I,
    ):
        primary, alternative = int(match["primary"]), int(match["alternative"])
        if 1 <= alternative <= 24 and 1 <= primary <= 24:
            labels.add(f"{primary} months ({alternative} months occasionally available)")
    for match in re.finditer(
        r"\bLength\s*:\s*Our\s+program\s+is\s+designed\s+to\s+run\s+for\s+"
        r"approximately\s+(?P<months>\d{1,2})\s+months?\b",
        raw_text, re.I,
    ):
        if 1 <= int(match["months"]) <= 24:
            labels.add(f"approximately {int(match['months'])} months")
    if len(labels) != 1:
        return None
    label = next(iter(labels))
    return ParsedTimingCandidate(
        timing=ParsedTiming(raw_label=label, timing_type="fixed_term", duration_label=label),
        alternative_duration=True,
        authoritative_duration=True,
        ambiguous_fields=("employment_timing.duration_months",),
    )


def _parse_alternative_duration_term_timing(
    raw_text: str,
) -> ParsedTimingCandidate | None:
    candidates: list[tuple[tuple[int, int], ParsedTimingCandidate]] = []
    qualified = _qualified_program_duration_candidate(raw_text)
    if qualified:
        candidates.append(((0, len(qualified.timing.raw_label or "")), qualified))
    standalone = _standalone_role_duration_candidate(raw_text)
    if standalone:
        candidates.append(((0, len(standalone.timing.raw_label or "")), standalone))
    for segment in _timing_candidate_segments(raw_text):
        for match in _alternative_duration_matches(segment):
            if _alternative_duration_is_education_program(segment, match):
                continue
            label = _alternative_duration_term_label(segment, match)
            if not label:
                continue
            raw_label, label_end = label
            if _duration_label_is_experience_requirement(segment, label_end):
                continue
            labeled_seasonal_start = (
                _alternative_duration_labeled_seasonal_month_span_start(
                    segment,
                    match,
                )
            )
            start_date = _start_date_near_timing_span(
                segment,
                match.start(),
                label_end,
            ) or labeled_seasonal_start
            end_date = _end_date_near_timing_span(segment, match.start(), label_end)
            duration_months = (
                _duration_months_from_date_range(start_date, end_date) if start_date else None
            )
            schedule_resolution = _duration_schedule_resolution(
                segment,
                match.start(),
                label_end,
            )
            timing = ParsedTiming(
                raw_label=raw_label,
                timing_type="fixed_term",
                start_date=start_date,
                end_date=end_date,
                duration_months=duration_months,
                duration_label=_clean_field_value(match.group(0)),
                schedule_type=schedule_resolution.value,
            )
            # Suspended-hyphen choices refine the existing work-term start
            # contract; preserve its planning label and schedule convention.
            suspended_work_term_start = bool(
                start_date
                and re.search(r"\d-\s*,", match.group(0))
                and re.match(
                    r"\s+work\s+term\s+start(?:s|ing)\s+",
                    segment[match.end():], re.IGNORECASE,
                )
            )
            candidate = ParsedTimingCandidate(
                timing=timing,
                schedule_resolution=schedule_resolution,
                default_schedule_type="full_time" if suspended_work_term_start else None,
                alternative_duration=True,
                prefer_academic_term_label=(
                    _is_year_leading_seasonal_role_duration(segment, match)
                ),
                prefer_start_date_term_label=(
                    _is_parenthetical_role_duration_without_academic_term(segment, match)
                    or _alternative_duration_has_role_owned_month_year_start(
                        segment,
                        match,
                    )
                    or labeled_seasonal_start is not None
                    or suspended_work_term_start
                ),
                duration_options=_alternative_duration_options(match),
            )
            score = (
                2 if end_date else 1 if start_date else 0,
                len(raw_label),
            )
            candidates.append((score, candidate))
        for match in _open_ended_duration_matches(segment):
            raw_label = _clean_field_value(match.group("label"))
            if _duration_label_is_experience_requirement(segment, match.end()):
                continue
            duration_clause = _duration_clause(segment, match.start(), match.end())
            if _duration_clause_is_non_employment_context(duration_clause) or re.search(
                r"\b(?:background|security|criminal|reference)\s+checks?\b",
                duration_clause,
                flags=re.IGNORECASE,
            ):
                continue
            if match.group("kind").casefold().startswith(("at least", "minimum")):
                prefix = segment[max(0, match.start() - 100) : match.start()]
                if not re.search(
                    r"\b(?:duration|term|intern(?:ship)?|intern|co-?op|placement|"
                    r"contract|commit)\b",
                    prefix,
                    flags=re.IGNORECASE,
                ):
                    continue
            start_date = _start_date_near_timing_span(
                segment,
                match.start(),
                match.end(),
            )
            schedule_resolution = _duration_schedule_resolution(
                segment,
                match.start(),
                match.end(),
            )
            candidate = ParsedTimingCandidate(
                timing=ParsedTiming(
                    raw_label=raw_label,
                    timing_type="fixed_term",
                    start_date=start_date,
                    end_date=None,
                    duration_months=None,
                    schedule_type=schedule_resolution.value,
                ),
                schedule_resolution=schedule_resolution,
                alternative_duration=True,
                ambiguous_fields=("employment_timing.duration_months",),
            )
            candidates.append(((1 if start_date else 0, len(raw_label)), candidate))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _alternative_duration_is_education_program(
    segment: str,
    match: re.Match[str],
) -> bool:
    prefix = segment[max(0, match.start() - 100) : match.start()]
    suffix = segment[match.end() : match.end() + 140]
    return bool(
        re.search(r"\b(?:dev\s+)?degree\s+program\b", prefix, flags=re.IGNORECASE)
        and re.match(
            r"\s+program\b[^.;\n]{0,100}\b(?:earn|complete)\b"
            r"[^.;\n]{0,50}\b(?:computer\s+science\s+)?degree\b",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _alternative_duration_matches(segment: str):
    pattern = re.compile(
        r"\b(?P<leading>(?:\d{1,2}(?:[\s-]*(?:mo|mos|months?|weeks?|years?))?"
        r"(?:\s*-?\s*,\s*|\s+(?:or|and)\s+))*)"
        r"(?P<first>\d{1,2})(?:[\s-]*(?P<first_unit>mo|mos|months?|weeks?|years?))?"
        r"\s*(?:-\s*(?=,))?,?\s*(?:(?:[-\u2011\u2013\u2014]\s*)?(?:or|to|and)"
        r"\s*(?:[-\u2011\u2013\u2014]\s*)?|/|,|[-\u2011\u2013\u2014])"
        r"\s*(?P<second>\d{1,2})"
        r"[\s-]*(?P<second_unit>mo|mos|months?|weeks?|years?)\b",
        flags=re.IGNORECASE,
    )
    yield from pattern.finditer(segment)


def _open_ended_duration_matches(segment: str):
    value_pattern = (
        r"(?:\d{1,2}|"
        + "|".join(sorted(DURATION_NUMBER_WORDS, key=len, reverse=True))
        + r")"
    )
    pattern = re.compile(
        r"\b(?P<label>(?P<kind>"
        r"(?:minimum(?:\s+(?:employment|internship|co-?op|work|term))*\s+"
        r"duration(?:\s+of)?|minimum\s+of|at\s+least)\s+"
        rf"{value_pattern}\+?\s*(?:months?|weeks?|years?)|"
        rf"{value_pattern}\s*(?:months?|weeks?|years?)\s*(?:or\s+longer|\+)))\b",
        flags=re.IGNORECASE,
    )
    yield from pattern.finditer(segment)


def _alternative_duration_options(match: re.Match[str]) -> frozenset[int]:
    second_unit = match.group("second_unit").casefold()
    first_unit = (match.group("first_unit") or second_unit).casefold()
    values = {
        _duration_months_for_unit(int(match.group("first")), first_unit),
        _duration_months_for_unit(int(match.group("second")), second_unit),
    }
    for leading_match in re.finditer(
        r"(?P<number>\d{1,2})(?:[\s-]*(?P<unit>mo|mos|months?|weeks?|years?))?",
        match.group("leading") or "",
        flags=re.IGNORECASE,
    ):
        unit = (leading_match.group("unit") or second_unit).casefold()
        values.add(_duration_months_for_unit(int(leading_match.group("number")), unit))
    return frozenset(value for value in values if 1 <= value <= 24)


def _duration_months_for_unit(number: int, unit: str) -> int:
    if unit.startswith("year"):
        return number * 12
    if unit.startswith("week"):
        return max(1, (number * 12 + 26) // 52)
    return number


def _alternative_duration_term_label(segment: str, match: re.Match[str]) -> tuple[str, int] | None:
    suffix = segment[match.end() : match.end() + 110]
    suffix_match = re.match(
        r"(?P<suffix>\s*,?\s*"
        r"(?:(?:full[-\s]?time|part[-\s]?time|paid|unpaid|student|summer|work|temporary)\s+|"
        r"co-?op(?:\s*/\s*|\s+)){0,4}"
        r"(?:position|terms?|internship|placement|contract|program|co-?op|opportunity)\b)",
        suffix,
        flags=re.IGNORECASE,
    )
    if suffix_match:
        label_end = match.end() + suffix_match.end("suffix")
        return _clean_field_value(segment[match.start() : label_end]), label_end

    containing_field = next(
        (
            field
            for field in _posting_field_spans(segment)
            if field.label in _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
            and field.value_start <= match.start()
            and match.end() <= field.value_end
        ),
        None,
    )
    if containing_field:
        return _clean_field_value(match.group(0)), match.end()
    containing_contract_field = next(
        (
            field
            for field in _posting_field_spans(segment)
            if field.label in _EMPLOYMENT_SCHEDULE_FIELD_LABELS
            and field.value_start <= match.start()
            and match.end() <= field.value_end
            and re.search(r"\bcontract\b", field.value, flags=re.IGNORECASE)
        ),
        None,
    )
    if containing_contract_field:
        return _clean_field_value(match.group(0)), match.end()
    containing_title_field = next(
        (
            field
            for field in _posting_field_spans(segment)
            if field.label in {"job title", "page title", "position", "role", "title"}
            and field.value_start <= match.start()
            and match.end() <= field.value_end
            and re.search(
                r"\b(?:co-?op|coop|intern(?:ship)?|placement|student)\b",
                field.value,
                flags=re.IGNORECASE,
            )
        ),
        None,
    )
    if containing_title_field:
        return _clean_field_value(match.group(0)), match.end()

    prefix = segment[max(0, match.start() - 80) : match.start()]
    suffix_after_match = segment[match.end() :]
    if re.search(r"\bco-?op\s+placement\s*[-–—]\s*$", prefix, re.I) and re.match(
        r"\s+duration\b", suffix_after_match, re.I,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?|placement|contract)\b[^()\n]{0,60}\(\s*$",
        prefix,
        flags=re.IGNORECASE,
    ) and re.match(r"\s*\)", suffix_after_match):
        return _clean_field_value(match.group(0)), match.end()
    if _is_year_leading_seasonal_role_duration(segment, match):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?|placement|student)\b"
        r"[^.;\n]{0,100}\b(?:beginning|starting|commencing)\b"
        r"[^.;\n]{0,45}\bfor\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:term|internship|co-?op|coop|placement)\s+(?:of\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\binternship\s+details\s*:\s*duration\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.fullmatch(r"\s*duration\s*", prefix, flags=re.IGNORECASE):
        return _clean_field_value(match.group(0)), match.end()
    if (
        re.search(r"\bduration\s*$", prefix, flags=re.IGNORECASE)
        and re.search(
            r"\b(?:co-?op|coop|intern(?:ship)?|placement|student)\b",
            segment[: match.start()],
            flags=re.IGNORECASE,
        )
        and not re.search(
            r"\b(?:experience|education|training|benefit|leave|probation|"
            r"background\s+check)\b[^.;\n]{0,45}\bduration\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:internship|co-?op|placement|position|role)\b"
        r"[^.;\n]{0,90}\bcommitment\s+of\s+(?:anywhere\s+)?from\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?|placement|position|student)\b"
        r"[^.;\n]{0,80}\b(?:lasts?|lasting|spans?|spanning)\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.search(
        r"\b(?:employment|internship|co-?op|work\s+term)\s+contracts?\s+"
        r"(?:typically\s+)?range\s+from\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if re.match(
        rf"\s*(?:\([^)]{{1,60}}\)\s*)?"
        rf"(?:starting|beginning|commencing)\s+(?:in\s+)?"
        rf"(?:{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\s+20\d{{2}}\b",
        suffix_after_match,
        flags=re.IGNORECASE,
    ) and re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?|placement|student)\b",
        segment[: match.start()],
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(match.group(0)), match.end()
    if (
        not suffix_after_match.strip()
        and not re.search(r"[.;:\n]", segment[: match.start()])
        and re.search(
            r"\b(?:co-?op|coop|intern(?:ship)?|placement|student)\b",
            segment[: match.start()],
            flags=re.IGNORECASE,
        )
    ):
        return _clean_field_value(match.group(0)), match.end()
    if not re.search(
        r"(?:\b(?:term\s+(?:length|duration)|duration)\s*:\s*"
        r"(?:[^.;:\n]{0,50}\b(?:either|choose|available)\s+)?|"
        r"\bavailable\s+for\s*)$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return None
    return _clean_field_value(match.group(0)), match.end()


def _is_year_leading_seasonal_role_duration(
    segment: str,
    match: re.Match[str],
) -> bool:
    prefix = segment[max(0, match.start() - 80) : match.start()]
    suffix_after_match = segment[match.end() :]
    return bool(
        re.search(
            r"\b20\d{2}\b[^()\n]{0,80}\b(?:fall|winter|spring|summer)\b"
            r"[^()\n]{0,50}\b(?:analyst|developer|engineer)\b[^()\n]{0,30}\(\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        and re.match(r"\s*\)", suffix_after_match)
    )


def _is_parenthetical_role_duration_without_academic_term(
    segment: str,
    match: re.Match[str],
) -> bool:
    prefix = segment[max(0, match.start() - 120) : match.start()]
    suffix_after_match = segment[match.end() :]
    return bool(
        re.search(
            r"\b(?:intern(?:ship)?|co-?op|coop|placement|student)\b"
            r"[^()\n]{0,80}\(\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        and re.match(r"\s*\)", suffix_after_match)
        and not re.search(
            rf"\b(?:{ACADEMIC_TERM_PATTERN})\b[^\n]{{0,30}}\b20\d{{2}}\b|"
            rf"\b20\d{{2}}\b[^\n]{{0,30}}\b(?:{ACADEMIC_TERM_PATTERN})\b",
            prefix,
            flags=re.IGNORECASE,
        )
    )


def _alternative_duration_has_role_owned_month_year_start(
    segment: str,
    match: re.Match[str],
) -> bool:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    suffix = segment[match.end() : match.end() + 140]
    return bool(
        re.match(
            rf"\s+(?:internship|co-?op|coop|placement|position|term)\b(?:"
            rf"[^.;\n]{{0,40}}\b(?:starting|beginning|commencing)"
            rf"(?:\s+in)?\s+(?:{month_pattern})\.?\s*,?\s*20\d{{2}}\b|"
            rf"\s+with\s+(?:an?\s+)?(?:{month_pattern})\.?\s*,?\s*"
            rf"20\d{{2}}\s+start\s+date\b)",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _alternative_duration_labeled_seasonal_month_span_start(
    segment: str,
    match: re.Match[str],
) -> str | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    prefix = segment[max(0, match.start() - 160) : match.start()]
    labeled_range = re.search(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\s+term\s*:\s*"
        rf"(?P<start_month>{month_pattern})\.?\s*[-\u2013\u2014]\s*"
        rf"(?:{month_pattern})\.?\s+(?P<year>20\d{{2}})\s*\|\s*$",
        prefix,
        flags=re.IGNORECASE,
    )
    if not labeled_range:
        return None
    year = int(labeled_range.group("year"))
    start_month = MONTH_NAMES[labeled_range.group("start_month").casefold()]
    expected_label = f"{_titlecase_term_label(labeled_range.group('term'))} {year}"
    if _academic_term_label_for_month_year_start(start_month, year) != expected_label:
        return None
    return f"{year:04d}-{start_month:02d}-01"


def _start_date_near_timing_span(segment: str, span_start: int, span_end: int) -> str | None:
    cue_spans = list(_start_timing_cue_matches(segment))
    best: tuple[int, str] | None = None
    date_candidates = [
        *_posting_date_candidates(segment),
        *_month_year_start_candidates(segment),
    ]
    for date_start, date_end, _date_text, parsed_date in date_candidates:
        if _date_is_non_employment_timing_context(segment, date_start):
            continue
        cue_distance = min(
            (
                _span_distance(date_start, date_end, cue_start, cue_end)
                for cue_start, cue_end in cue_spans
            ),
            default=101,
        )
        timing_distance = _span_distance(date_start, date_end, span_start, span_end)
        if cue_distance > 100 or timing_distance > MAX_TIMING_DATE_DURATION_DISTANCE:
            continue
        score = cue_distance + timing_distance
        if _line_label_is_start_timing(segment, date_start):
            score -= 50
        candidate = (score, parsed_date)
        if best is None or candidate < best:
            best = candidate
    return best[1] if best else None


def _end_date_near_timing_span(segment: str, span_start: int, span_end: int) -> str | None:
    pattern = re.compile(
        rf"\b(?:position\s+)?end\s+date"
        rf"(?:\s+of\s+(?:internship|co-?op|work\s+term))?\s*:\s*"
        rf"(?P<date>(?:{_posting_date_text_pattern()}))",
        flags=re.IGNORECASE,
    )
    best: tuple[int, str] | None = None
    for match in pattern.finditer(segment):
        parsed_date = _parse_posting_date(match.group("date"))
        if not parsed_date:
            continue
        date_start, date_end = match.span("date")
        if _date_is_non_employment_timing_context(segment, date_start):
            continue
        timing_distance = _span_distance(date_start, date_end, span_start, span_end)
        if timing_distance > MAX_TIMING_DATE_DURATION_DISTANCE:
            continue
        candidate = (timing_distance, parsed_date)
        if best is None or candidate < best:
            best = candidate
    return best[1] if best else None


def _parse_duration_only_term_timing(raw_text: str) -> ParsedTimingCandidate | None:
    # A recruiting year in this role title does not identify its start season.
    title_durations = {
        int(match["months"])
        for line in _posting_content_lines(raw_text)
        if (match := re.fullmatch(
            r"Intern\s*[-–—]\s*[^\n]{1,120}[-–—]\s*"
            r"(?P<months>\d{1,2})\s+Month\s+20\d{2}\s+Opportunities",
            line.strip(), re.I,
        )) and 1 <= int(match["months"]) <= 24
    }
    if len(title_durations) == 1:
        months = next(iter(title_durations))
        return ParsedTimingCandidate(
            timing=ParsedTiming(raw_label=f"{months} months", timing_type="fixed_term",
                                duration_months=months),
        )
    # Some ATS captures put the actual field value on the following line.
    # Read the whole field, not a graduation-recency sentence elsewhere.
    split_field_durations = {
        int(match[1]) for match in re.finditer(
            r"(?m)^Internship Duration\s*\n\s*(\d{1,2})\s+months?\s*$",
            raw_text, re.IGNORECASE,
        )
    }
    if len(split_field_durations) == 1:
        months = next(iter(split_field_durations))
        if 1 <= months <= 24:
            return ParsedTimingCandidate(
                timing=ParsedTiming(raw_label=f"{months} months", timing_type="fixed_term",
                                    duration_months=months),
            )
    for segment in _timing_candidate_segments(raw_text):
        for (
            duration_months,
            duration_start,
            duration_end,
            _duration_text,
        ) in _duration_month_candidates(segment):
            if re.match(
                r"\s+ago\b",
                segment[duration_end:],
                flags=re.IGNORECASE,
            ):
                continue
            raw_label = _duration_term_label(segment, duration_start, duration_end)
            if not raw_label:
                continue
            schedule_resolution = _duration_schedule_resolution(
                segment,
                duration_start,
                duration_end,
            )
            timing = ParsedTiming(
                raw_label=raw_label,
                timing_type="fixed_term",
                duration_months=duration_months,
                schedule_type=schedule_resolution.value,
            )
            return ParsedTimingCandidate(
                timing=timing,
                schedule_resolution=schedule_resolution,
            )
    return None


def _completed_by_is_non_employment_context(
    segment: str,
    match: re.Match[str],
) -> bool:
    context = segment[max(0, match.start() - 120) : match.start("date")]
    return bool(
        re.search(
            r"\b(?:application(?:\s+form)?|assessment|background\s+check|"
            r"criminal\s+check|documentation|onboarding|paperwork|"
            r"reference\s+check|screening|security\s+(?:check|training)|training)\b",
            context,
            flags=re.IGNORECASE,
        )
    )


def _parse_fixed_end_timing(raw_text: str) -> ParsedTimingCandidate | None:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    field_pattern = (
        rf"\b(?:Job\s+End\s+Date\s*:?\s*(?P<job_end>{_posting_date_text_pattern()})|"
        rf"Position\s+Length\s*:\s*Until\s+"
        rf"(?P<position_end>\d{{1,2}}\s+(?:{month_pattern})\s+20\d{{2}}))\b"
    )
    ends = {
        date for match in re.finditer(field_pattern, raw_text, re.I)
        if (date := _parse_posting_date(match["job_end"] or match["position_end"]))
    }
    if len(ends) == 1:
        end_date = next(iter(ends))
        return ParsedTimingCandidate(
            timing=ParsedTiming(raw_label=f"Until {end_date}", timing_type="fixed_term",
                                end_date=end_date),
        )
    end_months = {
        (MONTH_NAMES[match["month"].casefold()], int(match["year"]))
        for match in re.finditer(
            rf"\binternship\s+is\s+scheduled\s+to\s+end\s+in\s+"
            rf"(?P<month>{month_pattern})\s+(?P<year>20\d{{2}})\b",
            raw_text, re.I,
        )
    }
    if not ends and len(end_months) == 1:
        month, year = next(iter(end_months))
        name = max((name for name, number in MONTH_NAMES.items() if number == month), key=len)
        return ParsedTimingCandidate(
            timing=ParsedTiming(raw_label=f"Ends {name.title()} {year}", timing_type="fixed_term"),
            ambiguous_fields=("employment_timing.end_date",),
        )
    pattern = re.compile(
        rf"\b(?:the\s+)?(?:position|role|internship|co-?op|contract)\s+"
        rf"(?:runs?|continues?|lasts?)\s+(?P<label>until|through)\s+"
        rf"(?P<date>{_posting_date_text_pattern()})",
        flags=re.IGNORECASE,
    )
    end_only_pattern = re.compile(
        rf"(?:"
        rf"\b(?:position|role|internship|co-?op|contract)\b"
        rf"[^.;\n]{{0,70}}\bend\s+date\s+of\s+|"
        rf"(?P<completed_by>\b(?:position|role|internship|co-?op|contract)\b"
        rf"[^.;\n]{{0,70}}\b"
        rf"(?:must\s+be\s+)?completed\s+by\s+))"
        rf"(?P<date>{_posting_date_text_pattern()})",
        flags=re.IGNORECASE,
    )
    for segment in _timing_candidate_segments(raw_text):
        for match in pattern.finditer(segment):
            if _timing_mention_is_non_employment_context(
                segment,
                match.start(),
                match.end(),
            ):
                continue
            end_date = _parse_posting_date(match.group("date"))
            if not end_date:
                continue
            return ParsedTimingCandidate(
                timing=ParsedTiming(
                    raw_label=_clean_field_value(
                        segment[match.start("label") : match.end("date")]
                    ),
                    timing_type="fixed_term",
                    end_date=end_date,
                )
            )
        for match in end_only_pattern.finditer(segment):
            if match.group("completed_by") and _completed_by_is_non_employment_context(
                segment,
                match,
            ):
                continue
            if _timing_mention_is_non_employment_context(
                segment,
                match.start(),
                match.end(),
            ):
                continue
            end_date = _parse_posting_date(match.group("date"))
            if end_date:
                return ParsedTimingCandidate(
                    timing=ParsedTiming(
                        timing_type="fixed_term",
                        end_date=end_date,
                    )
                )
    return None


def _parse_asap_through_end_timing(raw_text: str) -> ParsedTimingCandidate | None:
    through_pattern = re.compile(
        rf"\b(?P<label>ASAP|as\s+soon\s+as\s+possible)\b\s+"
        rf"(?:through|until)\s+(?P<date>{_posting_date_text_pattern()})",
        flags=re.IGNORECASE,
    )
    for segment in _timing_candidate_segments(raw_text):
        for match in through_pattern.finditer(segment):
            clause = _timing_clause_for_span(segment, match.start(), match.end())
            if clause.context_type in {
                TIMING_CLAUSE_CONTEXT_APPLICATION,
                TIMING_CLAUSE_CONTEXT_INTERVIEW,
            } or _timing_mention_is_non_employment_context(
                segment,
                match.start(),
                match.end(),
            ):
                continue
            durations = [
                duration
                for duration in _duration_month_candidates(segment)
                if clause.start <= duration[1]
                and duration[2] <= clause.end
                and _span_distance(
                    duration[1],
                    duration[2],
                    match.start(),
                    match.end(),
                )
                <= MAX_TIMING_DATE_DURATION_DISTANCE
                and not _duration_label_is_experience_requirement(segment, duration[2])
            ]
            if not durations or not _has_term_timing_context(segment):
                continue
            duration_months, duration_start, duration_end, _duration_text = min(
                durations,
                key=lambda duration: _span_distance(
                    duration[1],
                    duration[2],
                    match.start(),
                    match.end(),
                ),
            )
            end_date = _parse_posting_date(match.group("date"))
            if not end_date:
                continue
            schedule_resolution = _duration_schedule_resolution(
                segment,
                duration_start,
                duration_end,
            )
            return ParsedTimingCandidate(
                timing=ParsedTiming(
                    raw_label=_clean_field_value(match.group("label")),
                    timing_type="fixed_term",
                    start_date=None,
                    end_date=end_date,
                    duration_months=duration_months,
                    schedule_type=schedule_resolution.value,
                ),
                schedule_resolution=schedule_resolution,
                default_schedule_type="full_time",
            )
    return None


def _duration_term_label(segment: str, duration_start: int, duration_end: int) -> str | None:
    prefix_start = max(0, duration_start - 80)
    prefix = segment[prefix_start:duration_start]
    suffix = segment[duration_end : duration_end + 45]
    if _duration_is_optional_extension(segment, duration_start):
        return None
    if re.match(r"\s+completed\b", suffix, re.I) and _duration_clause_is_non_employment_context(
        _duration_clause(segment, duration_start, duration_end),
    ):
        return None
    if re.search(r"\bcompleted\s+at\s+least\s*$", prefix, re.I) and re.match(
        r"\s+in\s+an?\s+engineering\s+program\b", suffix, re.I,
    ):
        return None
    if re.search(r"\bavailable\s+for\s+a\s+period\s+of\s*$", prefix, re.I):
        return _clean_field_value(segment[duration_start:duration_end])
    if re.match(r"\s+in\s+duration\b", suffix, flags=re.IGNORECASE) and re.search(
        r"\b(?:student\s+position|internship\s+position|co-?op\s+position)"
        r"\s+(?:that\s+)?is\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
        yearless_term = re.match(
            rf"\s+in\s+duration\s+for\s+the\s+term(?:\(s\)|s)?\s*:?\s*"
            rf"(?P<range>(?:{month_pattern})\.?\s*[-–—]\s*(?:{month_pattern})\.?)"
            r"(?=\s*(?:[.;]|$))",
            segment[duration_end:],
            flags=re.IGNORECASE,
        )
        # LinkedIn's shortened preview can precede the complete work-term clause.
        if yearless_term is None and re.search(
            r"(?:…|\.\.\.)\s*See\s+this\s+and\s+similar\s+jobs\b",
            segment[duration_end:],
            flags=re.IGNORECASE,
        ):
            return None
        return _clean_field_value(
            yearless_term.group("range") if yearless_term else segment[duration_start:duration_end]
        )
    suffix_match = re.match(
        r"\s*[-\u2013\u2014:]?\s*(?:,\s*(?:paid|unpaid)\s+)?"
        r"(?:(?:co-?op|work)\s+)?(?:temporary\s+)?"
        r"(?:position|term|period|internship|placement|contract|assignment|program|"
        r"opportunity|role|co-?op)\b",
        suffix,
        flags=re.IGNORECASE,
    )
    if suffix_match:
        label_end = duration_end + suffix_match.end()
        academic_term_suffix = re.match(
            rf"\s*\(\s*(?:{ACADEMIC_TERM_PATTERN})\s+term\s*\)",
            segment[label_end : label_end + 45],
            flags=re.IGNORECASE,
        )
        if academic_term_suffix:
            label_end += academic_term_suffix.end()
        if _duration_label_is_experience_requirement(segment, label_end):
            return None
        return _clean_field_value(segment[duration_start:label_end])

    if re.match(
        r"\s+(?:post[-\s]?secondary|skills?[-\s]?training)\b"
        r"[^.;\n]{0,80}\bprogram\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return None
    branded_term_suffix = re.match(
        r"\s+(?:[A-Za-z][A-Za-z0-9&.+-]*\s+){1,4}"
        r"(?:internship|co-?op|placement|program)\b",
        suffix,
        flags=re.IGNORECASE,
    )
    if branded_term_suffix:
        label_end = duration_end + branded_term_suffix.end()
        if _duration_label_is_experience_requirement(segment, label_end):
            return None
        return _clean_field_value(segment[duration_start:label_end])

    program_suffix = re.match(r"\s+program\b", suffix, flags=re.IGNORECASE)
    if program_suffix and re.search(
        r"\bas\s+an?\s+intern\b[^.;\n]{0,65}\bbenefit\s+from\s+an?\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return _clean_field_value(
            segment[duration_start : duration_end + program_suffix.end()]
        )

    program_duration_match = re.search(
        r"\b(?:internship|co-?op|placements?|work\s+term|program)"
        r"(?:\s+(?:program|opportunity|position|role))?"
        r"(?:\s*\([^)]{1,60}\))?\s+"
        r"(?:typically\s+)?(?:runs?|lasts?|spans?)\s+(?:(?:for|about)\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    )
    if program_duration_match:
        subject_prefix = prefix[: program_duration_match.start()]
        if re.search(
            r"\b(?:prior|previous|former)"
            r"(?:\s+(?:full[-\s]?time|part[-\s]?time|paid|student|summer|unpaid)){0,3}\s*$",
            subject_prefix,
            flags=re.IGNORECASE,
        ):
            return None
        return _clean_field_value(segment[duration_start:duration_end])

    title_parenthetical_prefix = segment[:duration_start]
    if re.search(
        r"\b(?:intern(?:ship)?|co-?op|student|placement)\b[^()\n]{0,80}\(\s*$",
        title_parenthetical_prefix,
        flags=re.IGNORECASE,
    ) and re.match(r"\s*\)", segment[duration_end:]):
        return _clean_field_value(segment[duration_start:duration_end])

    preferred_length_match = re.search(
        r"\b(?:the\s+)?preferred\s+(?:"
        r"(?:(?:internship|co-?op|placement|work\s+term|contract)"
        r"(?:\s+position)?\s+length)|"
        r"(?:length\s+of\s+(?:an?\s+)?"
        r"(?:internship|co-?op|placement|work\s+term|contract)"
        r"(?:\s+position)?))(?:\s+(?:is|of)|\s*:)\s*$",
        prefix,
        flags=re.IGNORECASE,
    )
    if preferred_length_match:
        if _duration_label_is_experience_requirement(segment, duration_end):
            return None
        label_start = prefix_start + preferred_length_match.start()
        return _clean_field_value(segment[label_start:duration_end])

    duration_of_match = re.search(
        r"\b(?:co-?op|internship|placement|position|role|term|contract)\b"
        r"[^.;\n]{0,55}\bduration\s+of\s*$",
        prefix,
        flags=re.IGNORECASE,
    )
    if duration_of_match:
        return _clean_field_value(
            segment[prefix_start + duration_of_match.start() : duration_end]
        )

    prefix_match = re.search(
        r"\b(?:(?:co-?op|work)\s+)?(?:position|term|internship|placement|contract|"
        r"assignment|program|opportunity|role)"
        r"(?:\s+(?:of|for))?\s*(?:approximately\s+)?[-\u2013\u2014:]?\s*$",
        prefix,
        flags=re.IGNORECASE,
    )
    if not prefix_match:
        initial_term_match = re.search(
            r"\b(?:the\s+)?initial\s+term\s+(?:is\s+)?"
            r"(?:expected\s+to\s+be|will\s+be|is)\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        if not initial_term_match:
            return None
        return _clean_field_value(segment[duration_start:duration_end])
    return _clean_field_value(segment[prefix_start + prefix_match.start() : duration_end])


def _duration_is_optional_extension(segment: str, duration_start: int) -> bool:
    return bool(re.search(
        r"\bwith\s+the\s+possibility\s+of\s+extending\s+to\s+an?\s*$",
        segment[max(0, duration_start - 100):duration_start], re.I,
    ))


def _duration_label_is_experience_requirement(segment: str, label_end: int) -> bool:
    suffix = segment[label_end:]
    history_prefix = segment[max(0, label_end - 100) : label_end]
    if re.search(
        r"\bmonths?\s+prior\s+to\s+the\s+internship(?:\s+start\s+date)?\s*$",
        history_prefix, flags=re.IGNORECASE,
    ) or re.match(
        r"\s+prior\s+to\s+the\s+internship\s+start\s+date\b",
        suffix, flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:prior|previous)\s+"
        r"(?:(?:internship|professional|related|relevant|work)\s+){0,2}"
        r"experience\s+(?:during|from|in|through|with)\s+(?:an?\s+)?"
        r"(?:(?:full|part)[-\s]?time\s+)?"
        r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"[-\s]*(?:mo|mos|months?|weeks?|years?)\s+"
        r"(?:(?:co-?op|work)\s+)?(?:position|term|internship|placement|contract)\s*$",
        history_prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\bexperience\s+(?:acquired|gained|obtained)\s+"
        r"(?:during|from|in|through)\b[^.;\n]{0,75}$",
        history_prefix,
        flags=re.IGNORECASE,
    ) and re.match(
        r"\s+(?:(?:are|is|was|would\s+be)\s+)?"
        r"(?:necessary|needed|preferred|required)\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:(?:prior(?!\s+to\b)|previous|last)\s+|"
        r"(?:completed?|completion\s+of|having\s+completed|worked|held)\s+(?:an?\s+)?)"
        r"(?:(?:full|part)[-\s]?time\s+)?(?:at\s+least\s+)?"
        r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"[-\s]*(?:mo|mos|months?|weeks?|years?)\s+"
        r"(?:(?:co-?op|work)\s+)?(?:position|term|internship|placement|contract)\s*$",
        history_prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\bat\s+least\s+(?:one|an?)\b[^.;\n]{0,80}$",
        history_prefix,
        flags=re.IGNORECASE,
    ) and re.match(
        r"\s+(?:(?:is|was|would\s+be)\s+)?"
        r"(?:necessary|needed|preferred|required)\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(
        r"\s*(?:of\s+)?(?:(?:(?:[/,;&]|\bor\b|\band\b)\s*)?"
        r"(?:co-?op|full[-\s]?time|industry|internship|part[-\s]?time|placement|"
        r"professional|relevant|prior|previous|work)\s+)*experience\b",
        suffix,
        flags=re.IGNORECASE,
    ):
        return True

    clause = re.split(r"[.;\n]", suffix, maxsplit=1)[0][:120]
    if not re.search(r"\bexperience\b", clause, flags=re.IGNORECASE):
        return False

    clause_start = max(segment.rfind(separator, 0, label_end) for separator in (".", ";", "\n")) + 1
    clause_prefix = segment[clause_start:label_end]
    label = _field_label(clause_prefix)
    requirement_labels = {
        "experience",
        "minimum qualifications",
        "preferred qualifications",
        "qualifications",
        "required qualifications",
        "requirements",
        "what you bring",
        "what we are looking for",
    }
    if label in requirement_labels:
        return True

    prefix = clause_prefix[-120:]
    if re.search(
        r"\b(?:at\s+least|minimum|must\s+have|preferred|required|requires?|"
        r"requirements?|qualifications?)\b",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(
            r"\bexperience\b.{0,40}\b(?:required|preferred|necessary|needed)\b",
            clause,
            flags=re.IGNORECASE,
        )
    )


def _schedule_evidence_for_patterns(
    raw_text: str,
    patterns: tuple[str, ...],
) -> ScheduleEvidenceResolution:
    positive_values: set[str] = set()
    negative_values: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE):
            mention_start, mention_end = match.span("schedule")
            if _schedule_mention_is_education_context(raw_text, mention_start, mention_end):
                continue
            if _schedule_mention_is_experience_context(raw_text, mention_start, mention_end):
                continue
            schedule_type = _schedule_type_from_token(match.group("schedule"))
            target = (
                negative_values
                if _schedule_mention_is_negated(raw_text, mention_start, mention_end)
                else positive_values
            )
            target.add(schedule_type)
    return ScheduleEvidenceResolution(
        positive_values=frozenset(positive_values),
        negative_values=frozenset(negative_values),
    )


def _schedule_type_from_token(token: str) -> str:
    return "full_time" if token.casefold().startswith("full") else "part_time"


def _schedule_resolution_from_context(context: str) -> ScheduleEvidenceResolution:
    resolution = _schedule_evidence_for_patterns(
        context,
        (r"\b(?P<schedule>full[-\s]?time|part[-\s]?time)\b",),
    )
    hours_per_week = _hours_per_week(context)
    if hours_per_week is not None and hours_per_week >= 35:
        resolution = resolution.merged(
            ScheduleEvidenceResolution(positive_values=frozenset({"full_time"}))
        )
    return resolution


def _academic_schedule_value(
    resolution: ScheduleEvidenceResolution,
) -> str | None:
    if resolution.is_ambiguous:
        return None
    if resolution.value is not None:
        return resolution.value
    if "full_time" in resolution.negative_values:
        return None
    return "full_time"


def _duration_schedule_resolution(
    segment: str,
    duration_start: int,
    duration_end: int,
) -> ScheduleEvidenceResolution:
    fields = _posting_field_spans(segment)
    resolution = _schedule_resolution_from_field_spans(
        fields,
        _EMPLOYMENT_SCHEDULE_FIELD_LABELS,
    )
    if re.search(
        r"\binternship\s+details\s*:",
        segment,
        flags=re.IGNORECASE,
    ):
        schedule_field = re.search(
            r"\bschedule\s*:?[ \t]*(?P<value>full[-\s]?time|part[-\s]?time)\b",
            segment,
            flags=re.IGNORECASE,
        )
        if schedule_field:
            value = schedule_field.group("value").casefold().replace("-", "_").replace(
                " ", "_"
            )
            resolution = resolution.merged(
                ScheduleEvidenceResolution(positive_values=frozenset({value}))
            )
    containing_field = next(
        (
            field
            for field in fields
            if field.value_start <= duration_start and duration_end <= field.value_end
        ),
        None,
    )
    if containing_field:
        if containing_field.label in _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS:
            return resolution.merged(_schedule_resolution_from_context(containing_field.value))
        return resolution

    prose_text = _text_without_labeled_fields(segment, fields)
    clause = _timing_clause_for_span(prose_text, duration_start, duration_end)
    return resolution.merged(_schedule_resolution_from_context(clause.text))


def _schedule_mention_is_education_context(
    context: str,
    mention_start: int,
    mention_end: int,
) -> bool:
    prefix = context[max(0, mention_start - 70) : mention_start]
    suffix = context[mention_end : mention_end + 50]
    if re.search(
        r"\b(?:enrolled?|enrollment|registered|studying|return(?:ing)?\s+to\s+school)\b"
        r"[^.;:\n]{0,60}$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"\s+(?:academic\s+(?:semester|year)|education|semester|"
            r"stud(?:y|ies|ents?(?:\s+status)?)|enrollment|post-secondary\s+student)\b",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _schedule_mention_is_experience_context(
    context: str,
    mention_start: int,
    mention_end: int,
) -> bool:
    prefix = context[max(0, mention_start - 70) : mention_start]
    suffix = context[mention_end : mention_end + 70]
    if re.search(
        r"\b(?:experience|employment\s+history)\b[^.;:\n]{0,50}$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:previously|formerly)\s+(?:worked|held|served|employed)"
        r"(?:\s+as)?(?:\s+an?)?\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"\s+(?:(?:(?:and|or|/)\s+)?"
            r"(?:co-?op|industry|internship|placement|professional|relevant|prior|"
            r"previous|work)\s+)*experience\b",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _schedule_mention_is_negated(context: str, mention_start: int, mention_end: int) -> bool:
    prefix = context[max(0, mention_start - 50) : mention_start]
    suffix = context[mention_end : mention_end + 60]
    if re.search(
        r"\b(?:not|no)\s+(?:an?\s+)?$|"
        r"\bnot\s+(?:available|offered|allowed|permitted)\s+(?:for|as)\s+(?:an?\s+)?$|"
        r"\b(?:(?:do(?:es)?|did)\s+not|(?:does?|did)n['’]t)\s+"
        r"(?:offer|allow|permit|support)\s+(?:an?\s+)?$|"
        r"\b(?:cannot|can(?:not|'t)|could\s+not|will\s+not|won't)\s+(?:be\s+)?"
        r"(?:worked|scheduled|offered|performed|arranged)\s+(?:as\s+)?(?:an?\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"\s+(?:(?:work|hours?|schedule|arrangements?|options?|roles?|positions?)\s+)?"
            r"(?:(?:is|are|will\s+be|would\s+be)\s+)?(?:not\b|unavailable\b)",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _parse_duration_term_start_segment(segment: str) -> ParsedTimingCandidate | None:
    durations = [
        duration
        for duration in _duration_month_candidates(segment)
        if not _duration_label_is_experience_requirement(segment, duration[2])
        and not re.match(
            r"\s+ago\b",
            segment[duration[2] :],
            flags=re.IGNORECASE,
        )
        and not _duration_is_auxiliary_period(
            segment,
            duration[1],
            duration[2],
        )
    ]
    if not durations or not (
        _has_term_timing_context(segment) or _has_availability_duration_start(segment)
    ):
        return None
    date_candidates = _posting_date_candidates(segment)
    date_candidates.extend(_month_year_start_candidates(segment))
    if not date_candidates:
        return None
    cue_spans = list(_start_timing_cue_matches(segment))
    if not cue_spans:
        return None

    best: tuple[int, int, int, int, str, int] | None = None
    selected_start_date: str | None = None
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    for date_start, date_end, date_text, start_date in date_candidates:
        if (
            len(re.findall(r"\d+", date_text)) == 1
            and re.search(
                rf"\b(?:{month_pattern})\.?\s*(?:-|\u2013|\u2014)\s*$",
                segment[max(0, date_start - 30) : date_start],
                flags=re.IGNORECASE,
            )
        ):
            continue
        if _date_is_non_employment_timing_context(
            segment,
            date_start,
        ) or _timing_prefix_is_history_context(segment[:date_start]):
            continue
        cue_distance = min(
            _span_distance(date_start, date_end, cue_start, cue_end)
            for cue_start, cue_end in cue_spans
        )
        if cue_distance > 100:
            continue
        duration_months, duration_start, duration_end, _duration_text = min(
            durations,
            key=lambda duration: _span_distance(date_start, date_end, duration[1], duration[2]),
        )
        duration_distance = _span_distance(date_start, date_end, duration_start, duration_end)
        if duration_distance > MAX_TIMING_DATE_DURATION_DISTANCE:
            continue
        score = 200 - cue_distance - duration_distance
        if _line_label_is_start_timing(segment, date_start):
            score += 50
        candidate = (
            score,
            duration_months,
            duration_start,
            duration_end,
            _timing_raw_label(segment, duration_start, duration_end),
            date_start,
        )
        if best is None or candidate > best:
            best = candidate
            selected_start_date = start_date

    if best is None or selected_start_date is None:
        return None

    (
        _score,
        duration_months,
        duration_start,
        duration_end,
        raw_label,
        _date_start,
    ) = best
    start_year, start_month, start_day = (
        int(part) for part in selected_start_date.split("-")
    )
    if len(raw_label) > 120 or "<" in raw_label or ">" in raw_label:
        raw_label = _academic_term_label_for_start_date(
            start_year,
            start_month,
            start_day,
        ) or _duration_term_label(
            segment,
            duration_start,
            duration_end,
        ) or _clean_field_value(segment[duration_start:duration_end])
    timing = ParsedTiming(
        raw_label=raw_label,
        timing_type="fixed_term",
        start_date=selected_start_date,
        end_date=_end_date_for_month_span(start_year, start_month, duration_months),
        duration_months=duration_months,
    )
    generic_labeled_duration = bool(
        re.search(
            r"\b(?:term\s+length|length\s+of\s+term)\s*:\s*$",
            segment[max(0, duration_start - 60) : duration_start],
            flags=re.IGNORECASE,
        )
        and not re.search(
            r"\b(?:co-?op|intern(?:ship)?|placement|position|term)\b",
            raw_label,
            flags=re.IGNORECASE,
        )
    )
    default_schedule_type = None if generic_labeled_duration else "full_time"
    return ParsedTimingCandidate(
        timing=timing,
        schedule_resolution=_duration_schedule_resolution(
            segment,
            duration_start,
            duration_end,
        ),
        default_schedule_type=default_schedule_type,
    )


def _has_availability_duration_start(segment: str) -> bool:
    """Availability for a dated work period need not repeat 'term' or 'internship'."""
    return any(
        re.search(r"\bavailable\s+for\s*$", segment[:start], flags=re.IGNORECASE)
        and re.match(r"\s+starting\b", segment[end:], flags=re.IGNORECASE)
        for _months, start, end, _label in _duration_month_candidates(segment)
    )


def _yearless_month_range_timing(
    raw_text: str, timing: ParsedTiming,
) -> ParsedTiming | None:
    """Anchor a selected yearless work term to the saved posting's calendar date."""
    if (
        timing.timing_type != "fixed_term"
        or timing.start_date
        or timing.end_date
        or not timing.raw_label
    ):
        return None
    ranges = list(_month_range_matches(timing.raw_label))
    if len(ranges) != 1:
        return None
    match = ranges[0]
    if (
        match.span() != (0, len(timing.raw_label))
        or any(match.group(field) for field in (
            "start_year", "end_year", "start_day", "end_day",
        ))
    ):
        return None
    posted_date = _posted_date_fallback(
        _employment_timing_content_scope(raw_text).posted_date_text
    )
    if not posted_date:
        # The observed employer page prints its posting date as a split field.
        dates = {
            parsed
            for field in re.finditer(
                r"^Date:[ \t]*(?:\n[ \t]*)?(?P<value>[^\n]+)$",
                raw_text, flags=re.IGNORECASE | re.MULTILINE,
            )
            if (parsed := _parse_posting_date(field.group("value"))) is not None
        }
        if len(dates) != 1:
            return None
        posted_date = dates.pop()
    start_month = MONTH_NAMES[match.group("start_month").casefold()]
    end_month = MONTH_NAMES[match.group("end_month").casefold()]
    duration_months = (end_month - start_month) % 12 + 1
    if timing.duration_months not in {None, duration_months}:
        return None
    reference_year, reference_month = map(int, posted_date.split("-")[:2])
    start_year = reference_year + (reference_month > start_month)
    return timing.model_copy(update={
        "start_date": f"{start_year:04d}-{start_month:02d}-01",
        "end_date": _end_date_for_month_span(start_year, start_month, duration_months),
        "duration_months": duration_months,
    })


def _stale_winter_availability_correction(
    raw_text: str, title: str | None,
) -> ParsedTiming | None:
    """Infer a likely copied year only for the observed Winter availability contract.

    Use the saved posting date, never today's date. Explicit title years, other
    terms, ambiguous dates and still-running periods remain authoritative.
    Callers must retain both inference and source-conflict quality flags.
    """
    if (
        not title
        or not _title_text_is_safe_timing_authority(title)
        or re.search(r"\d", title)
        or not re.search(r"\bwinter\b", title, re.IGNORECASE)
        or not re.search(r"\b(?:co-?op|intern(?:ship)?)\b", title, re.IGNORECASE)
        or re.search(r"\b(?:spring|summer|fall|autumn)\b", title, re.IGNORECASE)
    ):
        return None
    scope = _employment_timing_content_scope(_posting_content_scope(raw_text).text)
    # Official structured pages emit an ISO timestamp for DatePosted. Only this
    # guarded inference uses its calendar date; generic fallback stays unchanged.
    posted_date_text = re.sub(
        r"\b(20\d{2}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?(?=\s|$)",
        r"\1", scope.posted_date_text,
    )
    posted_date = _posted_date_fallback(posted_date_text)
    if not posted_date:
        return None
    candidates = [
        candidate.timing
        for segment in _timing_candidate_segments(scope.timing_text)
        if _has_availability_duration_start(segment)
        and (candidate := _parse_duration_term_start_segment(segment)) is not None
    ]
    if len({(item.start_date, item.end_date, item.duration_months) for item in candidates}) != 1:
        return None
    timing = candidates[0]
    if (
        not timing.start_date
        or not timing.end_date
        or timing.duration_months != 4
        or timing.start_date[5:] != "01-01"
        or timing.end_date[5:] != "04-30"
        or posted_date[:4] != timing.start_date[:4]
        or posted_date <= timing.end_date
    ):
        return None
    evidence = _employment_timing_evidence(scope.timing_text)
    if (
        evidence.ambiguous_fields
        or evidence.academic_terms
        or evidence.has_end_dates
        or any(item.value != timing.start_date for item in evidence.start_dates)
    ):
        return None
    year = int(posted_date[:4]) + 1
    return timing.model_copy(update={
        "raw_label": f"Winter {year}",
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-04-30",
    })


def _duration_is_auxiliary_period(
    segment: str,
    duration_start: int,
    duration_end: int,
) -> bool:
    context = segment[max(0, duration_start - 100) : duration_end + 100]
    return bool(
        re.search(
            r"\b(?:relocation|accommodation|onboarding|orientation|training)\b",
            context,
            flags=re.IGNORECASE,
        )
    )


def _has_term_timing_context(segment: str) -> bool:
    return bool(
        re.search(
            r"\b(?:co-?op|internship|placement|student|contract|work\s+term|term)\b",
            segment,
            flags=re.IGNORECASE,
        )
    )


def _start_timing_cue_matches(segment: str):
    pattern = re.compile(
        r"\b(?:commenc(?:e|es|ed|ing|ement)|start(?:s|ed|ing)?(?:\s+date)?|"
        r"begin(?:s|ning)?|expected\s+start(?:\s+date)?|anticipated\s+start(?:\s+date)?|"
        r"effective|from)\b",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(segment):
        yield match.start(), match.end()


def _duration_month_candidates(segment: str) -> list[tuple[int, int, int, str]]:
    number_pattern = "|".join(sorted(DURATION_NUMBER_WORDS, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?P<number>\d{{1,2}}|{number_pattern})"
        rf"(?:\s*\(\s*(?P<confirmation>\d{{1,2}})\s*\))?"
        rf"[\s\u2011\u2013\u2014-]*"
        rf"(?P<unit>mo|mos|months?|weeks?|years?|m(?=\s+contract\b))\b",
        flags=re.IGNORECASE,
    )
    candidates: list[tuple[int, int, int, str]] = []
    for match in pattern.finditer(segment):
        number_text = match.group("number").casefold()
        number = int(number_text) if number_text.isdigit() else DURATION_NUMBER_WORDS[number_text]
        confirmation = match.group("confirmation")
        if confirmation is not None and int(confirmation) != number:
            continue
        unit = match.group("unit").casefold()
        duration_months = _duration_months_for_unit(number, unit)
        if 1 <= duration_months <= 24:
            candidates.append((duration_months, match.start(), match.end(), match.group(0)))
    return candidates


def _posting_date_candidates(segment: str) -> list[tuple[int, int, str, str]]:
    pattern = re.compile(_posting_date_text_pattern(), flags=re.IGNORECASE)
    candidates: list[tuple[int, int, str, str]] = []
    for match in pattern.finditer(segment):
        parsed = _parse_posting_date(match.group(0))
        if parsed:
            candidates.append((match.start(), match.end(), match.group(0), parsed))
    return candidates


def _month_year_start_candidates(segment: str) -> list[tuple[int, int, str, str]]:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?P<month>{month_pattern})\.?\s*,?\s*(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    return [
        (
            match.start(),
            match.end(),
            match.group(0),
            f"{int(match.group('year')):04d}-{MONTH_NAMES[match.group('month').casefold()]:02d}-01",
        )
        for match in pattern.finditer(segment)
    ]


def _posting_date_text_pattern() -> str:
    month_pattern = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    return (
        rf"(?:[A-Za-z]+,\s*)?(?:{month_pattern})\.?\s+"
        r"\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+20\d{2}|"
        r"\d{1,2}/\d{1,2}/20\d{2}|"
        r"20\d{2}-\d{2}-\d{2}"
    )


def _date_is_non_employment_timing_context(segment: str, date_start: int) -> bool:
    line = _line_containing_offset(segment, date_start)
    label = _field_label(line)
    if label in NON_EMPLOYMENT_TIMING_DATE_LABELS:
        return True
    return _nearest_label_before_offset(line, date_start) in NON_EMPLOYMENT_TIMING_DATE_LABELS


def _line_label_is_start_timing(segment: str, date_start: int) -> bool:
    return _field_label(_line_containing_offset(segment, date_start)) in {
        "anticipated start date",
        "expected start date",
        "start date",
    }


def _line_containing_offset(segment: str, offset: int) -> str:
    line_start = segment.rfind("\n", 0, offset) + 1
    line_end = segment.find("\n", offset)
    if line_end == -1:
        line_end = len(segment)
    return segment[line_start:line_end]


def _span_distance(left_start: int, left_end: int, right_start: int, right_end: int) -> int:
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0


def _timing_raw_label(segment: str, duration_start: int, duration_end: int) -> str:
    nearby_academic_label = _academic_term_label_near_duration(
        segment,
        duration_start,
        duration_end,
    )
    if nearby_academic_label:
        return nearby_academic_label

    containing_field = next(
        (
            field
            for field in _posting_field_spans(segment)
            if field.label in _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
            and field.value_start <= duration_start
            and duration_end <= field.value_end
        ),
        None,
    )
    if containing_field:
        local_start = duration_start - containing_field.value_start
        local_end = duration_end - containing_field.value_start
        return _duration_term_label(
            containing_field.value,
            local_start,
            local_end,
        ) or _clean_field_value(containing_field.value)

    duration_label = _duration_term_label(segment, duration_start, duration_end)
    if duration_label and any(
        field.label in _EMPLOYMENT_START_FIELD_LABELS and field.start < duration_start
        for field in _posting_field_spans(segment)
    ):
        return duration_label
    if duration_label and re.search(
        rf"\b(?:starting|beginning|commencing)\s+(?:in\s+)?"
        rf"(?:{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\s+20\d{{2}}\b",
        segment[duration_end:],
        flags=re.IGNORECASE,
    ):
        return duration_label

    prefix_start = max(0, duration_start - 80)
    prefix = segment[prefix_start:duration_start]
    contract_length = re.search(
        r"\bcontract\s+length\s*:\s*"
        r"(?P<qualifier>(?:approximately|about|around|up\s+to|~)\s*)?$",
        prefix,
        flags=re.IGNORECASE,
    )
    if contract_length:
        qualifier = contract_length.group("qualifier")
        label_start = prefix_start + (
            contract_length.start("qualifier") if qualifier else len(prefix)
        )
        return _clean_field_value(segment[label_start:duration_end])
    if segment[:duration_start].strip().casefold() in {"this", "this is a", "this is an"}:
        return _clean_field_value(segment[duration_start:])
    return _clean_field_value(segment.replace("\n", " "))


def _academic_term_label_near_duration(
    segment: str,
    duration_start: int,
    duration_end: int,
) -> str | None:
    context_start = max(0, duration_start - 80)
    context_end = min(len(segment), duration_end + 100)
    context = segment[context_start:context_end]
    duration_clause = _timing_clause_for_span(segment, duration_start, duration_end)
    matches = set()
    for match in re.finditer(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\b\s+(?P<year>20\d{{2}})\b",
        context,
        flags=re.IGNORECASE,
    ):
        match_start = context_start + match.start()
        match_end = context_start + match.end()
        if not (
            duration_clause.start <= match_start
            and match_end <= duration_clause.end
        ) or not _timing_mention_is_employment_context(
            segment,
            match_start,
            match_end,
            title=None,
        ):
            continue
        matches.add(
            (_canonical_term_key(match.group("term")), int(match.group("year")))
        )
    if len(matches) != 1:
        return None
    term_key, year = next(iter(matches))
    return f"{_titlecase_term_label(term_key)} {year}"


def _parse_explicit_internship_timing(
    raw_text: str,
    evidence: EmploymentTimingEvidence,
) -> ParsedTimingCandidate | None:
    if not evidence.has_timing_fields:
        return None

    start_date = evidence.start_date
    end_date = evidence.end_date
    duration_months = (
        _duration_months_from_date_range(start_date, end_date)
        if start_date and end_date
        else evidence.duration_months
    )
    derived_end_date = None
    if start_date and duration_months and not evidence.has_end_dates:
        start_year = int(start_date[:4])
        start_month = int(start_date[5:7])
        derived_end_date = _end_date_for_month_span(
            start_year,
            start_month,
            duration_months,
        )

    raw_label = evidence.duration_raw_label
    if start_date:
        start_year = int(start_date[:4])
        start_month = int(start_date[5:7])
        raw_label = _academic_term_label_for_start_month(start_month, start_year)

    hours_per_week = _hours_per_week(raw_text)
    timing = ParsedTiming(
        raw_label=raw_label,
        timing_type="fixed_term",
        start_date=start_date,
        end_date=end_date or derived_end_date,
        duration_months=duration_months,
        hours_per_week_min=hours_per_week,
        hours_per_week_max=hours_per_week,
    )
    return ParsedTimingCandidate(
        timing=timing,
        default_schedule_type="full_time" if evidence.academic_terms else None,
    )


def _posted_date_fallback(raw_text: str) -> str | None:
    for line in raw_text.splitlines():
        if _field_label(line) in {
            "posted",
            "posted date",
            "date posted",
            "posted on",
            "date posted on",
        }:
            value = line.split(":", 1)[1] if ":" in line else line
            parsed = _parse_posting_date(value)
            if parsed:
                return parsed
        match = re.search(
            r"\b(?:date\s+posted|posted\s+(?:on\s+)?)[:\s]+(?P<date>[^.;\n]+)",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            parsed = _parse_posting_date(match.group("date"))
            if parsed:
                return parsed
    return None


def _parse_posting_date(value: str) -> str | None:
    cleaned = value.strip()
    iso = re.search(r"\b(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})\b", cleaned)
    if iso:
        return _valid_date(int(iso.group("year")), int(iso.group("month")), int(iso.group("day")))
    written = re.search(
        rf"(?:[A-Za-z]+,\s*)?"
        rf"(?P<month>{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+"
        r"(?P<year>20\d{2})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if written:
        month = MONTH_NAMES[written.group("month").casefold()]
        day = int(written.group("day"))
        year = int(written.group("year"))
        return _valid_date(year, month, day)

    day_first_written = re.search(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+"
        rf"(?P<month>{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\s+"
        r"(?P<year>20\d{2})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if day_first_written:
        month = MONTH_NAMES[day_first_written.group("month").casefold()]
        day = int(day_first_written.group("day"))
        year = int(day_first_written.group("year"))
        return _valid_date(year, month, day)

    abbreviated = re.search(
        rf"\b(?P<day>\d{{1,2}})\s*[-/]\s*"
        rf"(?P<month>{'|'.join(sorted(MONTH_NAMES, key=len, reverse=True))})\.?\s*[-/]\s*"
        r"(?P<year>\d{2}|20\d{2})\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if abbreviated:
        month = MONTH_NAMES[abbreviated.group("month").casefold()]
        day = int(abbreviated.group("day"))
        raw_year = int(abbreviated.group("year"))
        year = raw_year + 2000 if raw_year < 100 else raw_year
        return _valid_date(year, month, day)

    numeric = re.search(r"\b(?P<first>\d{1,2})/(?P<second>\d{1,2})/(?P<year>20\d{2})\b", cleaned)
    if not numeric:
        return None
    first = int(numeric.group("first"))
    second = int(numeric.group("second"))
    year = int(numeric.group("year"))
    return _valid_date(year, first, second)


def _valid_date(year: int, month: int, day: int) -> str | None:
    if month < 1 or month > 12:
        return None
    if day < 1 or day > _last_day_of_month(year, month):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _academic_term_label_for_start_month(month: int, year: int) -> str | None:
    if 1 <= month <= 4:
        return f"Winter {year}"
    if 5 <= month <= 8:
        return f"Summer {year}"
    if 9 <= month <= 12:
        return f"Fall {year}"
    return None


def _academic_term_label_for_month_year_start(month: int, year: int) -> str | None:
    term = MONTH_YEAR_ACADEMIC_TERM_LABELS.get(month)
    return f"{term} {year}" if term else None


def _academic_term_label_for_start_date(year: int, month: int, day: int) -> str | None:
    if month == 8 and day >= 20:
        return f"Fall {year}"
    return _academic_term_label_for_start_month(month, year)



def _canonical_term_key(term_label: str) -> str:
    return re.sub(r"\s*[/\-\u2013]\s*", "/", term_label.strip().casefold())


def _academic_term_defaults(term_key: str) -> tuple[str, int]:
    if term_key in COMBINED_ACADEMIC_TERMS:
        return COMBINED_ACADEMIC_TERMS[term_key]
    return term_key, ACADEMIC_TERM_MONTHS[term_key][1]


def _titlecase_term_label(term_label: str) -> str:
    return "/".join(part.title() for part in _canonical_term_key(term_label).split("/"))


def _academic_term_duration_months(timing_context: str) -> int | None:
    fields = _posting_field_spans(timing_context)
    allowed_field_labels = (
        _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
        | _EMPLOYMENT_DESCRIPTIVE_SCHEDULE_FIELD_LABELS
    )
    for duration_months, duration_start, duration_end, _ in _duration_month_candidates(
        timing_context
    ):
        containing_field = next(
            (
                field
                for field in fields
                if field.value_start <= duration_start and duration_end <= field.value_end
            ),
            None,
        )
        candidate_context = containing_field.value if containing_field else timing_context
        local_start = (
            duration_start - containing_field.value_start
            if containing_field
            else duration_start
        )
        local_end = (
            duration_end - containing_field.value_start if containing_field else duration_end
        )
        if _duration_label_is_experience_requirement(candidate_context, local_end):
            continue
        if _duration_term_label(candidate_context, local_start, local_end):
            return duration_months
        if containing_field and containing_field.label in _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS:
            return duration_months
        if containing_field and containing_field.label not in allowed_field_labels:
            continue

        clause = _duration_clause(candidate_context, local_start, local_end)
        if not re.search(
            rf"\b(?:{ACADEMIC_TERM_PATTERN})\s+20\d{{2}}\b",
            clause,
            flags=re.IGNORECASE,
        ):
            continue
        if _duration_clause_is_non_employment_context(clause):
            continue
        return duration_months
    return None


def _duration_clause(segment: str, duration_start: int, duration_end: int) -> str:
    return _timing_clause_for_span(segment, duration_start, duration_end).text


def _duration_clause_is_non_employment_context(clause: str) -> bool:
    if re.search(
        r"\b(?:applications?|apply|interviews?|postings?|recruiting)\b",
        clause,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:degree|education|enroll(?:ed|ment)?|school|college|university|"
            r"coursework|stud(?:y|ies))\b",
            clause,
            flags=re.IGNORECASE,
        )
        and re.search(
            r"\b(?:at\s+least|completed?|minimum|must|preferred|remain(?:ing|s)?|"
            r"requires?|required)\b|\bmonths?\s+of\b",
            clause,
            flags=re.IGNORECASE,
        )
    )


def _hours_per_week(raw_text: str) -> float | None:
    minimum, maximum = _hours_per_week_range(raw_text)
    return minimum if minimum is not None and minimum == maximum else None


def _hours_per_week_range(raw_text: str) -> tuple[float | None, float | None]:
    minimum, maximum, _ambiguous = _hours_per_week_resolution(raw_text)
    return minimum, maximum


def _hours_per_week_resolution(
    raw_text: str,
) -> tuple[float | None, float | None, bool]:
    value_pattern = (
        r"(?:\d{1,2}(?:\.\d+)?|"
        + "|".join(sorted(DURATION_NUMBER_WORDS, key=len, reverse=True))
        + r")"
    )
    unit_pattern = r"(?:hours?|hrs?)\s*(?:/\s*(?:week|wk)|(?:per|a)\s+week)\b"
    field_claims = _weekly_hours_field_claims(raw_text, value_pattern, unit_pattern)
    range_patterns = (
        rf"\brang(?:e|es|ing)\s+from\s+(?P<minimum>{value_pattern})\s+to\s+"
        rf"(?P<maximum>{value_pattern})\s*{unit_pattern}",
        rf"\b(?P<minimum>{value_pattern})\s*{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
        rf"(?P<maximum>{value_pattern})\s*{unit_pattern}",
    )
    range_matches = tuple(
        match
        for pattern in range_patterns
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        if _weekly_hours_claim_is_employment_owned(raw_text, match)
    )
    range_claims = {
        _validated_hours_range(
            _weekly_hours_value(match.group("minimum")),
            _weekly_hours_value(match.group("maximum")),
        )
        for match in range_matches
    }
    range_claims.discard((None, None))

    maximum_matches = tuple(
        match
        for match in re.finditer(
            rf"\b(?:up\s+to(?:\s+a\s+maximum\s+of)?|(?:a\s+)?maximum\s+of)\s+"
            rf"(?P<hours>{value_pattern})\s*{unit_pattern}",
            raw_text,
            flags=re.IGNORECASE,
        )
        if _weekly_hours_claim_is_employment_owned(raw_text, match)
    )
    maximum_claims = {
        (None, maximum)
        for match in maximum_matches
        if 1 <= (maximum := _weekly_hours_value(match.group("hours"))) <= 80
    }
    minimum_matches = tuple(
        match
        for match in re.finditer(
            rf"\b(?:at\s+least|(?:a\s+)?minimum\s+of)\s+(?P<hours>{value_pattern})\s*"
            rf"{unit_pattern}",
            raw_text,
            flags=re.IGNORECASE,
        )
        if _weekly_hours_claim_is_employment_owned(raw_text, match)
    )
    minimum_claims = {
        (minimum, None)
        for match in minimum_matches
        if 1 <= (minimum := _weekly_hours_value(match.group("hours"))) <= 80
    }
    claimed_spans = tuple(
        (match.start(), match.end())
        for match in (*range_matches, *maximum_matches, *minimum_matches)
    )
    exact_claims: set[tuple[float | None, float | None]] = set()
    for match in re.finditer(
        rf"\b(?P<hours>{value_pattern})\s*{unit_pattern}", raw_text, flags=re.IGNORECASE
    ):
        if not _weekly_hours_claim_is_employment_owned(raw_text, match):
            continue
        if any(
            claim_start <= match.start() and match.end() <= claim_end
            for claim_start, claim_end in claimed_spans
        ):
            continue
        prefix = raw_text[max(0, match.start() - 40) : match.start()]
        if re.search(r"(?:\b(?:average(?:s|d)?(?:\s+of)?|approximately|about)|~)\s*$", prefix, re.I):
            continue
        hours = _weekly_hours_value(match.group("hours"))
        validated = _validated_hours_range(hours, hours)
        if validated != (None, None):
            exact_claims.add(validated)
    all_claims = field_claims | range_claims | maximum_claims | minimum_claims | exact_claims
    if all_claims:
        return _resolve_weekly_hours_claims(all_claims)
    return None, None, False


def _weekly_hours_field_claims(
    raw_text: str,
    value_pattern: str,
    unit_pattern: str,
) -> set[tuple[float | None, float | None]]:
    """Parse an exact workload field, whose label supplies the weekly unit."""

    implied_weekly_labels = tuple(
        label for label in EMPLOYMENT_HOURS_FIELD_LABELS if label != "Work Schedule"
    )
    lines = [re.sub(r"\s+:", ":", line) for line in raw_text.splitlines()]
    values = tuple(
        (value, True)
        for value in _field_values(
            lines,
            *implied_weekly_labels,
            allow_following_line=True,
        )
    ) + tuple(
        (value, False)
        for value in _field_values(
            lines,
            "Work Schedule",
            allow_following_line=True,
        )
    )
    # Captures also print a workload heading followed by its numeric value.
    label_keys = {label.casefold() for label in implied_weekly_labels}
    values += tuple(
        (lines[index + 1], True)
        for index, line in enumerate(lines[:-1])
        if line.strip().casefold() in label_keys
    )
    claims: set[tuple[float | None, float | None]] = set()
    for value, label_implies_weekly_hours in values:
        # A field label supplies an implied weekly unit only when its complete
        # value is numeric, optionally followed by the hours dimension. Text
        # such as "8 hours per day" or a 9-to-5 clock window therefore cannot
        # be reinterpreted as weekly workload.
        field_unit_pattern = (
            rf"(?:{unit_pattern}|hours?|hrs?)"
            if label_implies_weekly_hours
            else unit_pattern
        )
        range_match = re.fullmatch(
            rf"\s*(?P<minimum>{value_pattern})\s*"
            rf"{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
            rf"(?P<maximum>{value_pattern})"
            rf"(?P<unit>\s*{field_unit_pattern})?\s*",
            value,
            flags=re.IGNORECASE,
        )
        if range_match and (label_implies_weekly_hours or range_match.group("unit")):
            claim = _validated_hours_range(
                _weekly_hours_value(range_match.group("minimum")),
                _weekly_hours_value(range_match.group("maximum")),
            )
            if claim != (None, None):
                claims.add(claim)
            continue

        exact_match = re.fullmatch(
            rf"\s*(?P<hours>{value_pattern})(?P<unit>\s*/?\s*{field_unit_pattern})?\s*",
            value,
            flags=re.IGNORECASE,
        )
        if exact_match and (label_implies_weekly_hours or exact_match.group("unit")):
            hours = _weekly_hours_value(exact_match.group("hours"))
            claim = _validated_hours_range(hours, hours)
            if claim != (None, None):
                claims.add(claim)
    return claims


def _weekly_hours_claim_is_employment_owned(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    mention = EvidenceMention.from_posting(raw_text, match.start(), match.end())
    clause = mention.clause
    if re.search(r"\bpreferably\b[^.;\n]{0,80}$", mention.prefix, re.I):
        return False
    if re.search(
        r"\banother\s+(?:(?:full|part)[-\s]*time\s+)?job\b[^.;\n]{0,45}$",
        mention.prefix, re.I,
    ):
        return False
    raw_prefix = raw_text[max(0, match.start() - 180) : match.start()]
    hours_field = tuple(
        re.finditer(
            r"(?:^|[.;\r\n])\s*"
            r"(?:hours?|hours?\s+of\s+work|work\s+hours?|work\s+schedule)\s*:",
            raw_prefix,
            flags=re.IGNORECASE,
        )
    )
    if hours_field:
        field_value = raw_prefix[hours_field[-1].end() :]
        field_value = re.sub(r"\b[ap]\.m\.", "", field_value, flags=re.IGNORECASE)
        if not re.search(r"[.!?;]\s+", field_value):
            return True
    benefit_predicates = tuple(
        re.finditer(
            r"\b(?:benefits?|receiv(?:e|es|ed)|get(?:s|ting)?|provid(?:e|es|ed)|"
            r"offer(?:s|ed)?|earn(?:s|ed|ing)?|accru(?:e|es|ed|ing)|"
            r"eligible|entitled|allows?|"
            r"may\s+(?:spend|use|take)|can\s+(?:spend|use|take))\b",
            mention.prefix,
            flags=re.IGNORECASE,
        )
    )
    if _weekly_hours_claim_is_benefit_eligibility_threshold(mention):
        return False
    # The predicate immediately governing the quantity owns it. Anchoring the
    # production at the end of the prefix prevents an earlier "role is" from
    # claiming a subordinate office or facility schedule.
    if _weekly_hours_prefix_has_workload_governor(mention.prefix):
        return True
    if benefit_predicates:
        return False
    if re.search(
        r"\b(?:coursework|stud(?:y|ies)|volunteer(?:ing)?|community\s+service|"
        r"paid\s+time\s+off|p\.?t\.?o\.?|vacation|leave)\b",
        clause,
        flags=re.IGNORECASE,
    ):
        return False
    if re.search(
        r"\b(?:hours?\s+of\s+work|work(?:ing|s|ed)?|schedule[d]?|"
        r"full[-\s]?time|part[-\s]?time|commit(?:ment|ted)?|availability)\b",
        clause,
        flags=re.IGNORECASE,
    ):
        return True
    residual_clause = f"{mention.prefix}{mention.suffix}"
    if not residual_clause.strip(" \t\r\n-*\u2022|:()."):
        return True
    return False


def _weekly_hours_claim_is_benefit_eligibility_threshold(
    mention: EvidenceMention,
) -> bool:
    """Return whether hours delimit benefit eligibility rather than workload.

    The quantity's nearest verb is often ``work`` in both constructions.  The
    enclosing production distinguishes them: a benefits entitlement can select
    employees by hours worked, while a workload clause directly assigns hours
    to the role or employee.
    """

    prefix = mention.prefix[-240:]
    suffix = mention.suffix[:160]
    benefit_head = r"(?:benefits?(?:\s+package)?|benefit\s+coverage)"
    worker = r"(?:employees?|workers?|staff|team\s+members?)"
    entitlement = (
        r"(?:is|are|be|become(?:s)?|remain(?:s)?)?\s*"
        r"(?:available|offered|provided|eligible|covered|entitled)"
    )

    benefit_entitlement = (
        rf"(?:\b{benefit_head}\b[^.;!?\r\n]{{0,100}}\b{entitlement}\b"
        rf"[^.;!?\r\n]{{0,60}}|"
        r"\b(?:offer(?:s|ed)?|provid(?:e|es|ed)|extend(?:s|ed)?|"
        r"make(?:s)?\s+available)\b[^.;!?\r\n]{0,70}"
        rf"\b{benefit_head}\b[^.;!?\r\n]{{0,40}})"
    )
    benefit_selects_workers = re.search(
        rf"{benefit_entitlement}\b(?:to|for)\s+{worker}\s+"
        r"(?:(?:who|that)\s+(?:regularly\s+)?work|working)\s*"
        r"(?:at\s+least\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    )
    if benefit_selects_workers:
        return True

    workers_qualify_for_benefits = re.search(
        rf"\b{worker}\s+(?:(?:who|that)\s+)?"
        r"(?:(?:must|required\s+to)\s+)?(?:regularly\s+)?"
        r"work(?:ing)?\s*(?:at\s+least\s+)?$",
        prefix,
        flags=re.IGNORECASE,
    ) and re.match(
        r"\s*(?:(?:or\s+(?:more|greater)|minimum)\s*)?"
        rf"(?:{entitlement}\s+(?:for\s+)?{benefit_head}\b|"
        rf"to\s+qualify\s+for\s+{benefit_head}\b)",
        suffix,
        flags=re.IGNORECASE,
    )
    if workers_qualify_for_benefits:
        return True

    return bool(
        re.search(
            rf"\b(?:qualify|eligib(?:le|ility))\s+for\s+{benefit_head}\b"
            rf"[^.;!?\r\n]{{0,100}}\b{worker}\s+"
            r"(?:(?:must|required\s+to)\s+)?work\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
    )


def _weekly_hours_prefix_has_workload_governor(prefix: str) -> bool:
    prefix = prefix[-200:]
    if re.search(
        r"\b(?:work(?:ing|s|ed)?|scheduled?\s+to\s+work)\s*$|"
        r"\bwork\s+(?:full|part)[-\s]+time\s+hours\s*,?\s*$|"
        r"\b(?:commitment|availability)(?:\s+(?:is|of|for))?\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True

    role_subject = (
        r"(?:(?:the|this)\s+)?(?:(?:expected|standard|normal)\s+)?"
        r"(?:role|position|job|workload)"
    )
    auxiliary = (
        r"(?:(?:will|would|must|may|should)\s+|"
        r"(?:is|are)\s+expected\s+to\s+)?"
    )
    modifier = r"(?:(?:typically|generally|normally|usually)\s+)?"
    predicate = (
        r"(?:requires?|is|equals?|totals?|entails?|involv(?:e|es|ed|ing)|"
        r"calls?\s+for|consists?\s+of|demands?)"
    )
    quantity_bridge = (
        r"(?:(?:a|an|the)\s+)?(?:(?:weekly|expected|standard|normal)\s+)?"
        r"(?:commitment\s+of\s+)?"
    )
    return bool(
        re.search(
            rf"\b{role_subject}\s+{auxiliary}{modifier}{predicate}\s+"
            rf"{quantity_bridge}$",
            prefix,
            flags=re.IGNORECASE,
        )
    )


def _resolve_weekly_hours_claims(
    claims: set[tuple[float | None, float | None]],
) -> tuple[float | None, float | None, bool]:
    if len(claims) != 1:
        exact_values = {
            minimum
            for minimum, maximum in claims
            if minimum is not None and minimum == maximum
        }
        if len(exact_values) == 1:
            exact = next(iter(exact_values))
            if all(
                (minimum is None or maximum is None or minimum == maximum)
                and (minimum is None or minimum <= exact)
                and (maximum is None or exact <= maximum)
                for minimum, maximum in claims
            ):
                return exact, exact, False
        return None, None, True
    minimum, maximum = next(iter(claims))
    return minimum, maximum, False


def _weekly_hours_value(value: str) -> float:
    normalized = value.casefold()
    if normalized in DURATION_NUMBER_WORDS:
        return float(DURATION_NUMBER_WORDS[normalized])
    return float(normalized)


def _validated_hours_range(
    minimum: float,
    maximum: float,
) -> tuple[float | None, float | None]:
    if not (1 <= minimum <= maximum <= 80):
        return None, None
    return minimum, maximum
