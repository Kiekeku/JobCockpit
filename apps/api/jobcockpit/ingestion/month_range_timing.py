from __future__ import annotations

import re
from collections.abc import Iterator

from jobcockpit.ingestion.parser_models import ParsedTiming
from jobcockpit.ingestion.posting_fields import _clean_field_value, _field_label
from jobcockpit.ingestion.posting_scope import _posting_field_spans
from jobcockpit.ingestion.timing_constants import MONTH_NAMES, NON_EMPLOYMENT_TIMING_DATE_LABELS
from jobcockpit.ingestion.timing_dates import (
    _COMMON_COOP_TERM_MONTHS,
    _last_day_of_month,
    _month_span_duration,
)
from jobcockpit.ingestion.timing_models import (
    MonthRangeContextParts,
    MonthRangeTimingAssessment,
    MonthRangeTimingCandidate,
)

MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING = "employment_timing"
MONTH_RANGE_CONTEXT_NON_EMPLOYMENT_TIMING = "non_employment_timing"
MONTH_RANGE_CONTEXT_UNKNOWN = "unknown"
EMPLOYMENT_MONTH_RANGE_LABELS = {
    "contract duration",
    "co-op period",
    "co-op term",
    "coop period",
    "coop term",
    "employment period",
    "employment term",
    "duration of term",
    "duration",
    "internship period",
    "internship term",
    "expected start date to end date",
    "term",
    "target date",
    "type of employment",
    "work period",
    "work term",
}
EMPLOYMENT_AVAILABILITY_MONTH_RANGE_LABELS = {
    "availability",
    "availability period",
    "availability window",
    "co-op availability",
    "coop availability",
    "internship availability",
    "work availability",
}
NON_EMPLOYMENT_MONTH_RANGE_LABELS = {
    "application availability",
    "application availability window",
    "application deadline",
    "application period",
    "application window",
    "interview availability",
    "interview availability window",
    "interview window",
}
MONTH_RANGE_TIMING_SUBJECT_PATTERNS = (
    r"terms?",
    r"(?:co-?op|coop|internship|placement)"
    r"(?:\s+student)?(?:\s+(?:program|opportunity|position|role))?",
    r"role",
    r"position",
    r"(?:co-?op|coop|intern)\s+student\s+to\s+join\s+(?:the\s+)?team",
    r"(?:\d{1,2}\s+months?\s+)?contract",
)
MONTH_RANGE_EMPLOYMENT_LABEL_PATTERN = (
    r"\b(?:co-?op|coop|employment|internship|work)\s+(?:period|term)s?\b"
)
MONTH_RANGE_EMPLOYMENT_ENTITY_PATTERN = (
    r"\b(?:co-?op|coop|employment|internship|placement|students?|term|work)\b"
)
MONTH_RANGE_AVAILABILITY_PREFIX_PATTERN = (
    r"\b(?:candidates?\s+)?(?:must\s+)?(?:be\s+)?available(?:\s+(?:from|between))?\b|"
    r"\brequires?\s+availability(?:\s+(?:from|between))?\b|"
    r"\bavailability\s+(?:is\s+)?(?:from|between)\b"
)
MONTH_RANGE_TIMING_CUE_PATTERN = (
    r"runs?|running|is|from|between|starts?|starting|begins?|beginning|"
    r"commences?|commencing|lasts?|spans?|extends?|working|scheduled|"
    r"requires\s+availability"
)
MONTH_RANGE_TIMING_MODIFIER_PATTERN = (
    r"(?:full[-\s]?time|part[-\s]?time|paid|unpaid|hybrid|remote|on[-\s]?site|in[-\s]?person|"
    r"as\s+a\s+(?:paid\s+|unpaid\s+)?(?:placement|internship|co-?op|coop)|approximately)"
)
NON_EMPLOYMENT_MONTH_RANGE_PREFIX_PATTERNS = (
    r"\bapplications\b(?:\s+for\b[^.\n]{0,80})?\s+"
    r"(?:are\s+)?(?:open|opened|accept|accepted|accepting|close|closing|run|runs)\b",
    r"\bapplication\s+(?:availability|window|period|deadline|deadlines|dates?|start|end)\b",
    r"\bposting\s+(?:availability|period|window|dates?|close|closing|start|opening)\b",
    r"\brecruiting\s+(?:period|window|cycle)\b",
    r"\binterview\s+(?:availability|window|period|dates?)\b",
)


def _extract_month_range_timing_candidates(
    segment: str,
    *,
    order_offset: int = 0,
) -> list[MonthRangeTimingCandidate]:
    candidates: list[MonthRangeTimingCandidate] = []
    for match in _month_range_matches(segment):
        candidate = _month_range_candidate_from_match(
            segment, match, "month_range", order_offset + len(candidates)
        )
        if candidate:
            candidates.append(candidate)
    for match in _month_start_end_pair_matches(segment):
        candidate = _month_range_candidate_from_match(
            segment,
            match,
            "start_end_pair",
            order_offset + len(candidates),
            raw_start=match.start("start_month"),
        )
        if candidate:
            candidates.append(candidate)
    return _dedupe_month_range_timing_candidates(candidates)


def _month_range_matches(segment: str) -> Iterator[re.Match[str]]:
    pattern = re.compile(
        rf"\b(?P<between>between\s+)?(?P<start_month>{_month_name_pattern()})\.?"
        r"(?:\s+(?P<start_day>\d{1,2})(?:st|nd|rd|th)?)?,?"
        r"(?:\s+(?P<start_year>20\d(?:\s+\d|\d))(?P<start_year_comma>,)?)?"
        r"\s*(?P<separator>to|through|thru|until|and|-|\u2013|\u2014|\ufffd)\s*"
        rf"(?P<end_month>{_month_name_pattern()})\.?"
        r"(?:\s+(?P<end_day>\d{1,2})(?:st|nd|rd|th)?)?,?"
        r"(?:\s+(?P<end_year>20\d(?:\s+\d|\d)))?\b",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(segment):
        # A labeled Date field may use a comma before its range connector.
        # Keep prose ranges on their existing interpretation paths.
        if match.group("start_year_comma") and not re.search(
            r"\bdate\s*:\s*$", segment[:match.start()], flags=re.IGNORECASE,
        ):
            continue
        separator = match.group("separator").casefold()
        if separator == "and" and not match.group("between"):
            continue
        yield match


def _month_start_end_pair_matches(segment: str) -> Iterator[re.Match[str]]:
    month = _month_name_pattern()
    pattern = re.compile(
        rf"\b(?:starts?|begins?|commences?|starting|beginning)\s+(?:on\s+|in\s+)?"
        rf"(?P<start_month>{month})\.?,?\s+(?P<start_year>20\d{{2}})"
        r"(?P<middle>[^.\n;]{0,120}?)\b"
        rf"(?:(?:and\s+)?(?:ends?|ending)|through|thru|until|to)\s+(?:on\s+|in\s+)?"
        rf"(?P<end_month>{month})\.?,?\s+(?P<end_year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    yield from pattern.finditer(segment)


def _month_range_candidate_from_match(
    segment: str,
    match: re.Match[str],
    kind: str,
    order: int,
    *,
    raw_start: int | None = None,
) -> MonthRangeTimingCandidate | None:
    start_month = MONTH_NAMES[match.group("start_month").casefold()]
    end_month = MONTH_NAMES[match.group("end_month").casefold()]
    start_year_text = match.group("start_year")
    end_year_text = match.group("end_year")
    if not start_year_text and not end_year_text:
        return None
    if start_year_text:
        start_year = int(re.sub(r"\s+", "", start_year_text))
        end_year = (
            int(re.sub(r"\s+", "", end_year_text))
            if end_year_text
            else start_year + (1 if end_month < start_month else 0)
        )
    else:
        end_year = int(re.sub(r"\s+", "", end_year_text))
        start_year = end_year - (1 if end_month < start_month else 0)

    start_day_text = match.groupdict().get("start_day")
    end_day_text = match.groupdict().get("end_day")
    start_day = int(start_day_text) if start_day_text else None
    end_day = int(end_day_text) if end_day_text else None
    separator = match.groupdict().get("separator")
    has_boundary_separator = separator is not None and separator.casefold() in {"to", "until"}
    supports_end_boundary = has_boundary_separator and (
        (start_day is None and end_day is None)
        or (start_day is not None and end_day is not None and start_day == end_day)
    )
    if start_day is not None and not 1 <= start_day <= _last_day_of_month(start_year, start_month):
        return None
    if end_day is not None and not 1 <= end_day <= _last_day_of_month(end_year, end_month):
        return None

    duration_months = _month_span_duration(
        start_year,
        start_month,
        end_year,
        end_month,
        end_is_boundary=supports_end_boundary and start_day is not None,
    )
    if not 1 <= duration_months <= 24:
        return None

    label_start = raw_start if raw_start is not None else match.start()
    raw_label = _clean_field_value(segment[label_start : match.end()])
    if separator == "\ufffd":
        raw_label = raw_label.replace("\ufffd", "\u2013")
    return MonthRangeTimingCandidate(
        segment=segment,
        start=label_start,
        end=match.end(),
        raw_label=raw_label,
        start_year=start_year,
        start_month=start_month,
        end_year=end_year,
        end_month=end_month,
        kind=kind,
        order=order,
        start_day=start_day,
        end_day=end_day,
        supports_end_boundary=supports_end_boundary,
    )


def _dedupe_month_range_timing_candidates(
    candidates: list[MonthRangeTimingCandidate],
) -> list[MonthRangeTimingCandidate]:
    seen: set[tuple[int, int, int, int, int | None, int | None, str]] = set()
    deduped: list[MonthRangeTimingCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (item.start, item.end, item.order)):
        key = (
            candidate.start_year,
            candidate.start_month,
            candidate.end_year,
            candidate.end_month,
            candidate.start_day,
            candidate.end_day,
            candidate.raw_label.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _month_range_duration(
    candidate: MonthRangeTimingCandidate,
    *,
    corroborating_duration_months: frozenset[int] = frozenset(),
) -> int:
    boundary_duration = _month_span_duration(
        candidate.start_year,
        candidate.start_month,
        candidate.end_year,
        candidate.end_month,
        end_is_boundary=True,
    )
    exact_boundary = candidate.start_day is not None
    corroborated_month_boundary = (
        candidate.start_day is None
        and boundary_duration in _COMMON_COOP_TERM_MONTHS
        and boundary_duration in corroborating_duration_months
    )
    return _month_span_duration(
        candidate.start_year,
        candidate.start_month,
        candidate.end_year,
        candidate.end_month,
        end_is_boundary=(
            candidate.supports_end_boundary and (exact_boundary or corroborated_month_boundary)
        ),
    )


def _month_range_end_day(
    candidate: MonthRangeTimingCandidate,
    *,
    corroborating_duration_months: frozenset[int] = frozenset(),
) -> int:
    if candidate.end_day is not None:
        return candidate.end_day
    if _month_range_duration(
        candidate,
        corroborating_duration_months=corroborating_duration_months,
    ) != _month_span_duration(
        candidate.start_year,
        candidate.start_month,
        candidate.end_year,
        candidate.end_month,
    ):
        return 1
    return _last_day_of_month(candidate.end_year, candidate.end_month)


def _parsed_timing_from_month_range_candidate(
    candidate: MonthRangeTimingCandidate,
    *,
    corroborating_duration_months: frozenset[int] = frozenset(),
) -> ParsedTiming:
    return ParsedTiming(
        raw_label=candidate.raw_label,
        timing_type="fixed_term",
        start_date=(
            f"{candidate.start_year:04d}-{candidate.start_month:02d}-{candidate.start_day or 1:02d}"
        ),
        end_date=(
            f"{candidate.end_year:04d}-{candidate.end_month:02d}-"
            f"{_month_range_end_day(candidate, corroborating_duration_months=corroborating_duration_months):02d}"
        ),
        duration_months=_month_range_duration(
            candidate,
            corroborating_duration_months=corroborating_duration_months,
        ),
        schedule_type="full_time",
    )


def _month_name_pattern() -> str:
    return "|".join(sorted(MONTH_NAMES, key=len, reverse=True))


def _classify_month_range_timing_candidate(
    candidate: MonthRangeTimingCandidate,
) -> MonthRangeTimingAssessment:
    parts = _month_range_context_parts(candidate.segment, candidate.start, candidate.end)
    specific_reject_reason = _specific_non_employment_month_range_reason(parts)
    if specific_reject_reason:
        return MonthRangeTimingAssessment(
            candidate=candidate,
            context_type=MONTH_RANGE_CONTEXT_NON_EMPLOYMENT_TIMING,
            confidence=100,
            reason=specific_reject_reason,
        )
    if re.fullmatch(
        r"\s*The\s+Opportunity\s+\d{1,2}\s*[-–—]\s*\d{1,2}\s+months?\s*:\s*",
        candidate.segment[:candidate.start], re.I,
    ):
        return MonthRangeTimingAssessment(
            candidate=candidate,
            context_type=MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING,
            confidence=95,
            reason="opportunity duration range followed by calendar",
        )
    if _duration_field_precedes_generic_date_range(candidate):
        return MonthRangeTimingAssessment(
            candidate=candidate,
            context_type=MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING,
            confidence=95,
            reason="duration field followed by generic date range",
        )
    containing_field = next(
        (
            field
            for field in _posting_field_spans(candidate.segment)
            if field.label in EMPLOYMENT_MONTH_RANGE_LABELS
            and field.value_start <= candidate.start
            and candidate.end <= field.value_end
        ),
        None,
    )
    if containing_field:
        return MonthRangeTimingAssessment(
            candidate=candidate,
            context_type=MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING,
            confidence=100,
            reason=f"employment field {containing_field.label}",
        )

    employment_confidence, employment_reason = _employment_month_range_confidence(parts)
    if employment_confidence:
        return MonthRangeTimingAssessment(
            candidate=candidate,
            context_type=MONTH_RANGE_CONTEXT_EMPLOYMENT_TIMING,
            confidence=employment_confidence,
            reason=employment_reason,
        )

    return MonthRangeTimingAssessment(
        candidate=candidate,
        context_type=MONTH_RANGE_CONTEXT_UNKNOWN,
        confidence=0,
        reason="no employment timing evidence",
    )


def _duration_field_precedes_generic_date_range(
    candidate: MonthRangeTimingCandidate,
) -> bool:
    prefix = candidate.segment[max(0, candidate.start - 120) : candidate.start]
    return bool(
        re.search(
            r"\bduration\s*:\s*\d{1,2}\s+months?\s*\+?\s+date\s*:\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
    )


def _month_range_context_parts(
    segment: str, range_start: int, range_end: int
) -> MonthRangeContextParts:
    context, range_start_in_context, range_end_in_context = _month_range_clause_context(
        segment, range_start, range_end
    )
    prefix = context[: max(range_start_in_context, 0)].casefold()
    suffix = context[max(range_end_in_context, 0) :].casefold()
    return MonthRangeContextParts(
        context=context,
        prefix=prefix,
        suffix=suffix,
        label=_field_label(context),
        range_start_in_context=range_start_in_context,
    )


def _specific_non_employment_month_range_reason(parts: MonthRangeContextParts) -> str | None:
    if _timing_prefix_is_history_context(parts.prefix):
        return "historical employment timing"
    if (
        parts.label in NON_EMPLOYMENT_TIMING_DATE_LABELS
        or parts.label in NON_EMPLOYMENT_MONTH_RANGE_LABELS
    ):
        return f"non-employment label {parts.label}"
    nearest_label = _nearest_label_before_offset(parts.context, parts.range_start_in_context)
    if nearest_label in NON_EMPLOYMENT_TIMING_DATE_LABELS:
        return f"nearest non-employment label {nearest_label}"
    if _month_range_is_availability_context(
        parts
    ) and _month_range_suffix_points_to_application_or_interview(parts.suffix):
        return "availability is for applications or interviews"
    for pattern in NON_EMPLOYMENT_MONTH_RANGE_PREFIX_PATTERNS:
        if re.search(pattern, parts.prefix, flags=re.IGNORECASE):
            return "application, posting, recruiting, or interview window"
    return None


def _month_range_suffix_points_to_application_or_interview(suffix: str) -> bool:
    return bool(
        re.search(
            r"\bfor\s+(?:(?:(?:an?|the)\s+)?interview|interviews|applications|application\s+interviews?)\b"
            r"(?:\W*$|\s+(?:for|with)\b)",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _employment_month_range_confidence(parts: MonthRangeContextParts) -> tuple[int, str]:
    if parts.label in EMPLOYMENT_MONTH_RANGE_LABELS:
        return 100, f"employment label {parts.label}"
    if parts.label in EMPLOYMENT_AVAILABILITY_MONTH_RANGE_LABELS:
        return 70, f"employment availability label {parts.label}"
    if re.search(
        r"\bschedule\s*:\s*\d{1,3}(?:\s*[-\u2013\u2014]\s*\d{1,3})?\s+"
        r"hours?\s+per\s+week\s*,?\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "weekly work schedule followed by date range"
    if re.search(
        r"\bcontract\s+dates?\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "contract date range"
    if re.search(
        r"\btype\s+of\s+employment\s*:\s*"
        r"(?:temporary|full[-\s]?time|part[-\s]?time|[,()\s])+\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "type of employment contains range"
    if re.search(
        r"\b(?:full|part)[-\s]+time\s*,?\s*temporary\s*\(\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "temporary work schedule followed by date range"
    if re.search(
        r"\bduration\s*\(\s*if\s+term\s*/\s*temporary\s*\)\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "term or temporary duration contains range"
    if re.search(
        r"\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|sixteen)\s+months?\s+date\s*:\s*$|"
        r"\bmonths?\s+date\s*:\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "duration value followed by date range"
    if re.search(
        r"\b(?:co-?op\s+)?work\s+term\s*:\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "work term label before range"
    if re.search(
        r"\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen)"
        r"[\s-]+months?\s+(?:co-?op\s+|work\s+)?term"
        r"(?:\s+period)?\s*\(?\s*$",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 95, "duration-qualified employment term before range"
    if re.match(
        r"\s+(?:co-?op|coop|employment|internship|work)\s+(?:period|term)\b",
        parts.suffix,
        flags=re.IGNORECASE,
    ):
        return 95, "employment term label after range"
    if parts.label == "title" and re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?|placement)\b",
        parts.prefix,
        flags=re.IGNORECASE,
    ):
        return 85, "employment role title contains range"

    near_prefix = parts.prefix[-140:]
    if re.search(
        r"\bwe\s+are\s+currently\s+hiring\b[^.;\n]{0,120}"
        r"\b(?:fall|winter|spring|summer)\s+term\s+(?:position|role)\s*[,(:]?\s*$",
        near_prefix,
        flags=re.IGNORECASE,
    ):
        return 90, "current hiring term position followed by date range"
    if re.search(
        r"\b(?:co-?op|coop|intern(?:ship)?)\b[^.;\n]{0,110}"
        r"\b(?:join(?:ing)?\s+(?:our|the)\s+)?(?:development\s+)?team\s+from\s*$",
        near_prefix,
        flags=re.IGNORECASE,
    ):
        return 88, "co-op or internship role joins team for range"
    if re.search(MONTH_RANGE_EMPLOYMENT_LABEL_PATTERN, near_prefix, flags=re.IGNORECASE):
        return 90, "employment term label near range"
    if any(
        _month_range_timing_cue_leads_to_range(near_prefix, pattern)
        for pattern in MONTH_RANGE_TIMING_SUBJECT_PATTERNS
    ):
        return 85, "employment subject and timing cue lead to range"
    if _month_range_availability_context_points_to_term(parts):
        return 70, "availability tied to employment term"
    return 0, ""


def _month_range_is_availability_context(parts: MonthRangeContextParts) -> bool:
    return parts.label in EMPLOYMENT_AVAILABILITY_MONTH_RANGE_LABELS or bool(
        re.search(MONTH_RANGE_AVAILABILITY_PREFIX_PATTERN, parts.prefix, flags=re.IGNORECASE)
        and re.search(MONTH_RANGE_EMPLOYMENT_ENTITY_PATTERN, parts.context, flags=re.IGNORECASE)
    )


def _month_range_availability_context_points_to_term(parts: MonthRangeContextParts) -> bool:
    return bool(
        not _month_range_suffix_points_to_application_or_interview(parts.suffix)
        and re.search(MONTH_RANGE_AVAILABILITY_PREFIX_PATTERN, parts.prefix, flags=re.IGNORECASE)
        and re.search(MONTH_RANGE_EMPLOYMENT_ENTITY_PATTERN, parts.context, flags=re.IGNORECASE)
    )


def _month_range_timing_cue_leads_to_range(prefix: str, subject_pattern: str) -> bool:
    cue_match = re.search(
        rf"\b{subject_pattern}\s+(?P<cue>{MONTH_RANGE_TIMING_CUE_PATTERN})\b", prefix
    )
    if not cue_match:
        return False
    between_cue_and_range = prefix[cue_match.end() :].strip()
    if not between_cue_and_range or between_cue_and_range in {"from", "between", "in"}:
        return True
    if re.fullmatch(
        r"for\s+\d{1,2}\s+weeks?\s*(?:,|\()?\s*(?:from|between)?",
        between_cue_and_range,
    ):
        return True
    return bool(
        re.fullmatch(
            rf"{MONTH_RANGE_TIMING_MODIFIER_PATTERN}\s+(?:from|between)",
            between_cue_and_range,
            flags=re.IGNORECASE,
        )
    )


def _month_range_clause_context(
    segment: str, range_start: int, range_end: int
) -> tuple[str, int, int]:
    line_start = segment.rfind("\n", 0, range_start) + 1
    line_end = segment.find("\n", range_end)
    if line_end == -1:
        line_end = len(segment)
    line = segment[line_start:line_end]
    local_start = range_start - line_start
    local_end = range_end - line_start

    clause_start = max(line.rfind(".", 0, local_start), line.rfind(";", 0, local_start)) + 1
    label_pattern = re.compile(r"\b[A-Za-z][A-Za-z\s_-]{0,40}:\s*")
    label_matches = list(label_pattern.finditer(line, clause_start, local_start))
    if label_matches:
        clause_start = label_matches[-1].start()
    clause_end_candidates = [
        position
        for position in (line.find(".", local_end), line.find(";", local_end))
        if position != -1
    ]
    next_label = label_pattern.search(line, local_end)
    if next_label:
        clause_end_candidates.append(next_label.start())
    initial_clause_end = min(clause_end_candidates) if clause_end_candidates else len(line)
    for comma in (match.start() for match in re.finditer(",", line[local_end:initial_clause_end])):
        comma_index = local_end + comma
        suffix = line[comma_index + 1 : initial_clause_end].lstrip()
        if re.match(
            r"(?:applications?|application|apply|posting|requisition|recruiting)\b",
            suffix,
            flags=re.IGNORECASE,
        ):
            clause_end_candidates.append(comma_index)
            break
    clause_end = min(clause_end_candidates) if clause_end_candidates else len(line)
    raw_context = line[clause_start:clause_end]
    leading_trim = len(raw_context) - len(raw_context.lstrip())
    context = raw_context.strip()
    range_start_in_context = local_start - clause_start - leading_trim
    range_end_in_context = local_end - clause_start - leading_trim
    return context, range_start_in_context, range_end_in_context


def _timing_prefix_is_history_context(prefix: str) -> bool:
    entity = r"(?:co-?op|intern(?:ship)?|placement|work\s+term)"
    relation = r"(?:during|in|from|between)"
    patterns = (
        r"\b(?:worked|held)\s+(?:(?:an?|the|their)\s+)?"
        r"\d{1,2}\s+months?\s+contract\b[^.;:\n]{0,45}\b(?:from|between)\s*$",
        r"\b(?:completed|finished)\s+(?:(?:an?|the|their)\s+)?"
        r"\d{1,2}\s+months?\s+contract\b[^.;:\n]{0,45}\b(?:from|between)\s*$",
        r"\b(?:(?:my|the|their)\s+)?last\s+\d{1,2}\s+months?\s+contract\b"
        r"[^.;:\n]{0,45}\b(?:from|between)\s*$",
        r"\b(?:prior|previous|former)\s+\d{1,2}\s+months?\s+contract\b"
        r"[^.;:\n]{0,45}\b(?:from|between)\s*$",
        rf"\b(?:prior|previous|former)\s+"
        rf"(?:(?:full[-\s]?time|part[-\s]?time|paid|student|summer|unpaid)\s+){{0,3}}"
        rf"{entity}(?:\s+program)?\s+(?:runs?|lasts?|spans?)\b[^.;:\n]{{0,45}}$",
        rf"\bcompleted\s+(?:(?:an?|the|their)\s+)?{entity}"
        rf"(?:\s+placement)?\s+{relation}\s*$",
        rf"\b(?:prior|previous)\s+{entity}(?:\s+placement)?\s+"
        rf"(?:(?:was|were)\s+)?{relation}\s*$",
        rf"\b(?:held|served|worked)\b[^.;:\n]{{0,45}}\b{entity}\b"
        rf"[^.;:\n]{{0,25}}\b{relation}\s*$",
        rf"\b(?:was|were)\s+(?:(?:an?|the)\s+)?{entity}"
        rf"(?:\s+placement)?\s+{relation}\s*$",
        rf"\b{entity}(?:\s+placement)?\s+"
        rf"(?:occurred|took\s+place|was|were)\s+{relation}\s*$",
    )
    return any(re.search(pattern, prefix, flags=re.IGNORECASE) for pattern in patterns)


def _nearest_label_before_offset(line: str, offset: int) -> str | None:
    prefix = line[:offset]
    labels = sorted(NON_EMPLOYMENT_TIMING_DATE_LABELS, key=len, reverse=True)
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"\b(?P<label>{label_pattern})\s*:\s*[^:]*$", prefix, flags=re.IGNORECASE)
    return match.group("label").casefold() if match else None
