from __future__ import annotations

import re

from pydantic import ValidationError

from jobcockpit.ingestion.employment_timing import (
    _academic_term_defaults,
    _alternative_duration_matches,
    _authoritative_academic_term_label,
    _authoritative_title_timing_fields,
    _canonical_term_key,
    _date_month_marker,
    _duration_month_candidates,
    _employment_month_range_assessments,
    _employment_timing_evidence,
    _hours_per_week,
    _posting_date_candidates,
    _schedule_resolution_from_context,
    _timing_mention_is_employment_context,
    _timing_mention_is_non_employment_context,
)
from jobcockpit.ingestion.timing_clauses import _timing_clause_for_span
from jobcockpit.ingestion.timing_constants import (
    ACADEMIC_TERM_MONTHS,
    ACADEMIC_TERM_PATTERN,
    MONTH_NAMES,
)
from jobcockpit.ingestion.posting_scope import (
    _EMPLOYMENT_END_FIELD_LABELS,
    _EMPLOYMENT_START_FIELD_LABELS,
    _posting_content_lines,
)
from jobcockpit.ingestion.month_range_timing import (
    _extract_month_range_timing_candidates,
    _month_name_pattern,
    _month_range_duration,
)
from jobcockpit.ingestion.parser_models import (
    ParsedDataQuality,
    ParsedJob,
    ParsedTiming,
    ProviderTimingContract,
)
from jobcockpit.ingestion.posting_fields import _clean_field_value, _field_label
from jobcockpit.ingestion.role_parser import _title_value
from jobcockpit.ingestion.timing_dates import _end_date_for_month_span
from jobcockpit.ingestion.timing_models import (
    ProviderDateClaim,
    ProviderTimingClaimResolution,
    _date_range_supports_duration,
    _duration_months_from_date_range,
)


MAX_PROVIDER_TIMING_LABEL_CHARS = 160
MAX_PROVIDER_TIMING_LABEL_WORDS = 16

def _validated_provider_timing(timing: ParsedTiming) -> ParsedTiming | None:
    try:
        validated = ProviderTimingContract.model_validate(timing.model_dump())
    except ValidationError:
        return None
    if (
        validated.start_date
        and validated.end_date
        and validated.start_date > validated.end_date
    ):
        return None
    if (
        validated.hours_per_week_min is not None
        and validated.hours_per_week_max is not None
        and validated.hours_per_week_min > validated.hours_per_week_max
    ):
        return None
    return ParsedTiming.model_validate(validated.model_dump())


def _replace_provider_timing_with_baseline(
    parsed: ParsedJob,
    *,
    baseline: ParsedJob,
) -> None:
    provider_quality = parsed.data_quality.model_copy(deep=True)
    baseline_quality = baseline.data_quality
    parsed.employment_timing = baseline.employment_timing.model_copy(deep=True)
    for attribute in ("missing_fields", "ambiguous_fields", "inferred_fields"):
        provider_fields = getattr(provider_quality, attribute)
        baseline_fields = getattr(baseline_quality, attribute)
        setattr(
            parsed.data_quality,
            attribute,
            _merge_quality_fields(
                _quality_fields_with_prefix(provider_fields, matches=False),
                _quality_fields_with_prefix(baseline_fields, matches=True),
            ),
        )


def _apply_authoritative_timing_resolution(
    parsed: ParsedJob,
    *,
    baseline: ParsedJob | None,
    posting_text: str,
) -> bool:
    if not baseline:
        return False

    provider_timing = _validated_provider_timing(parsed.employment_timing)
    if provider_timing is None:
        _replace_provider_timing_with_baseline(parsed, baseline=baseline)
        return True

    provider_claim_resolution = _resolve_provider_timing_date_claims(
        provider_timing,
    )
    provider_timing = provider_claim_resolution.timing
    baseline_timing = baseline.employment_timing.model_copy(deep=True)
    provider_matches_baseline = provider_timing == baseline_timing
    provider_label_supported = _provider_timing_has_scoped_evidence(
        provider_timing,
        posting_text,
    )
    provider_strength = _provider_employment_timing_strength(provider_timing)
    provider_field_strengths = _provider_timing_field_strengths(
        provider_timing,
        posting_text,
        include_label_evidence=provider_label_supported,
    )
    provider_supported = (
        provider_matches_baseline
        or provider_label_supported
        or bool(provider_field_strengths)
    )
    baseline_field_strengths = _baseline_timing_field_strengths(
        baseline_timing,
        posting_text,
    )
    authoritative_academic_label = _authoritative_academic_term_label(
        _employment_timing_evidence(posting_text)
    )
    authoritative_title_fields = _authoritative_title_timing_fields(
        baseline.role.job_title
    )
    conflicting_title_fields = frozenset(
        field
        for field in authoritative_title_fields
        if _provider_timing_field_conflicts_with_title(
            field.removeprefix("employment_timing."),
            provider_timing,
            baseline_timing,
        )
    )
    provider_claim_ambiguities = _unresolved_provider_date_claim_ambiguities(
        provider_claim_resolution,
        baseline_timing=baseline_timing,
        baseline_field_strengths=baseline_field_strengths,
        posting_text=posting_text,
    )
    provider_supported_fields = (
        frozenset(ParsedTiming.model_fields)
        if provider_matches_baseline
        else frozenset(provider_field_strengths)
    )
    unresolved_baseline_fields = {
        field.removeprefix("employment_timing.")
        for field in baseline.data_quality.ambiguous_fields
        if field.startswith("employment_timing.")
    }
    if baseline_timing.duration_months is None and _alternative_duration_matches(
        baseline_timing.raw_label or ""
    ):
        unresolved_baseline_fields.add("duration_months")
    selected_provider_fields = frozenset(
        field
        for field, strength in provider_field_strengths.items()
        if field not in unresolved_baseline_fields
        and f"employment_timing.{field}" not in conflicting_title_fields
        and getattr(provider_timing, field) is not None
        and (
            getattr(baseline_timing, field) is None
            or strength > baseline_field_strengths.get(field, 0)
        )
    )
    use_provider = bool(selected_provider_fields)
    use_provider_label = (
        authoritative_academic_label is None
        and provider_label_supported
        and bool(
            selected_provider_fields & {"start_date", "end_date", "duration_months"}
        )
    )
    parsed.employment_timing = (
        _merge_provider_timing_fields(
            provider_timing,
            baseline_timing,
            supported_fields=selected_provider_fields,
            use_provider_label=use_provider_label,
        )
        if use_provider
        else baseline_timing
    )

    provider_quality = parsed.data_quality.model_copy(deep=True)
    baseline_quality = baseline.data_quality
    parsed.data_quality.missing_fields = _reconciled_timing_missing_fields(
        provider_quality,
        baseline_quality,
        use_provider=use_provider,
        provider_selected_fields=selected_provider_fields,
    )
    parsed.data_quality.ambiguous_fields = [
        field
        for field in _merge_quality_fields(
            _reconciled_timing_ambiguous_fields(
                provider_quality,
                baseline_quality,
                provider_timing=provider_timing,
                baseline_timing=baseline_timing,
                provider_supported=provider_supported,
                use_provider=use_provider,
                provider_supported_fields=provider_supported_fields,
                provider_selected_fields=selected_provider_fields,
                provider_field_strengths=provider_field_strengths,
                baseline_field_strengths=baseline_field_strengths,
            ),
            list(provider_claim_ambiguities),
        )
        if field not in conflicting_title_fields
    ]
    parsed.data_quality.inferred_fields = _reconciled_timing_inferred_fields(
        provider_quality,
        baseline_quality,
        provider_timing=provider_timing,
        selected_timing=parsed.employment_timing,
        provider_supported=provider_supported,
        use_provider=use_provider,
        provider_supported_fields=provider_supported_fields,
        provider_selected_fields=selected_provider_fields,
    )
    return (
        not provider_supported
        and provider_timing != baseline_timing
        and provider_strength > 0
    )


def _employment_timing_strength(timing: ParsedTiming) -> int:
    if timing.timing_type == "fixed_term":
        return 2
    if timing.timing_type == "posted_date_fallback":
        return 1
    if timing.start_date or timing.end_date or timing.duration_months:
        return 2
    return 0


def _provider_employment_timing_strength(timing: ParsedTiming) -> int:
    strength = _employment_timing_strength(timing)
    explicit_dates = _posting_date_candidates(timing.raw_label or "")
    if len(explicit_dates) >= 2:
        return max(strength, 5)
    if explicit_dates:
        return max(strength, 4)
    return strength


def _baseline_timing_field_strengths(
    timing: ParsedTiming,
    posting_text: str,
) -> dict[str, int]:
    value_fields = (
        "start_date",
        "end_date",
        "duration_months",
        "hours_per_week_min",
        "hours_per_week_max",
        "schedule_type",
    )
    base_strength = _employment_timing_strength(timing)
    strengths = {
        field: base_strength
        for field in value_fields
        if getattr(timing, field) is not None
    }
    evidence = _employment_timing_evidence(posting_text)

    if _employment_month_range_assessments(posting_text):
        for field in ("start_date", "end_date", "duration_months"):
            if getattr(timing, field) is not None:
                strengths[field] = max(strengths.get(field, 0), 4)

    if timing.start_date and evidence.start_date == timing.start_date:
        strengths["start_date"] = max(strengths.get("start_date", 0), 5)
    if timing.end_date and evidence.end_date == timing.end_date:
        strengths["end_date"] = max(strengths.get("end_date", 0), 5)
    if (
        timing.duration_months is not None
        and evidence.start_date
        and evidence.end_date
        and timing.duration_months
        == _duration_months_from_date_range(evidence.start_date, evidence.end_date)
    ):
        strengths["duration_months"] = max(strengths.get("duration_months", 0), 5)
    elif (
        timing.duration_months is not None
        and evidence.duration_months == timing.duration_months
    ):
        strengths["duration_months"] = max(strengths.get("duration_months", 0), 4)

    schedule_resolution = evidence.schedule_fields.merged(evidence.schedule_prose)
    if timing.schedule_type and schedule_resolution.value == timing.schedule_type:
        strengths["schedule_type"] = max(strengths.get("schedule_type", 0), 4)
    elif timing.schedule_type and evidence.schedule_defaults.value == timing.schedule_type:
        strengths["schedule_type"] = max(strengths.get("schedule_type", 0), 2)

    hours_per_week = _hours_per_week(posting_text)
    if hours_per_week is not None:
        for field in ("hours_per_week_min", "hours_per_week_max"):
            if getattr(timing, field) == hours_per_week:
                strengths[field] = max(strengths.get(field, 0), 4)
    return strengths


def _provider_timing_field_conflicts_with_title(
    field: str,
    provider_timing: ParsedTiming,
    title_timing: ParsedTiming,
) -> bool:
    provider_value = getattr(provider_timing, field)
    title_value = getattr(title_timing, field)
    if provider_value is None or title_value is None:
        return False
    if field in {"start_date", "end_date"}:
        return _date_month_marker(provider_value) != _date_month_marker(title_value)
    return provider_value != title_value


def _provider_timing_field_strengths(
    timing: ParsedTiming,
    posting_text: str,
    *,
    include_label_evidence: bool,
) -> dict[str, int]:
    strengths = (
        _provider_label_timing_field_strengths(timing, posting_text)
        if include_label_evidence
        else {}
    )

    def support(field: str, strength: int) -> None:
        strengths[field] = max(strengths.get(field, 0), strength)

    evidence = _employment_timing_evidence(posting_text)
    if timing.start_date and timing.start_date == evidence.start_date:
        support("start_date", 5)
    if timing.end_date and timing.end_date == evidence.end_date:
        support("end_date", 5)
    if (
        timing.duration_months is not None
        and timing.duration_months == evidence.duration_months
    ):
        support("duration_months", 4)

    schedule_resolution = evidence.schedule_fields.merged(evidence.schedule_prose)
    if timing.schedule_type and timing.schedule_type == schedule_resolution.value:
        support("schedule_type", 4)
    elif timing.schedule_type and timing.schedule_type == evidence.schedule_defaults.value:
        support("schedule_type", 2)

    hours_per_week = _hours_per_week(posting_text)
    if hours_per_week is not None:
        if timing.hours_per_week_min == hours_per_week:
            support("hours_per_week_min", 4)
        if timing.hours_per_week_max == hours_per_week:
            support("hours_per_week_max", 4)
    return strengths


def _provider_label_timing_field_strengths(
    timing: ParsedTiming,
    posting_text: str,
) -> dict[str, int]:
    raw_label = timing.raw_label or ""
    corroborating_duration_months = frozenset(
        duration.months
        for duration in _employment_timing_evidence(posting_text).duration_terms
    )
    strengths: dict[str, int] = {}

    def support(field: str, strength: int) -> None:
        strengths[field] = max(strengths.get(field, 0), strength)

    explicit_date_candidates = _posting_date_candidates(raw_label)
    explicit_dates = [candidate[3] for candidate in explicit_date_candidates]
    if len(explicit_date_candidates) >= 2:
        is_exact_range = _provider_label_is_exact_date_range(
            raw_label,
            explicit_date_candidates,
        )
        last_index = len(explicit_date_candidates) - 1
        for index, (_, _, date_text, date_value) in enumerate(explicit_date_candidates):
            if timing.start_date == date_value and (
                (is_exact_range and index == 0)
                or _provider_date_label_has_field_cue(
                    date_text,
                    posting_text,
                    kind="start",
                )
            ):
                support("start_date", 5)
            if timing.end_date == date_value and (
                (is_exact_range and index == last_index)
                or _provider_date_label_has_field_cue(
                    date_text,
                    posting_text,
                    kind="end",
                )
            ):
                support("end_date", 5)
        if (
            timing.duration_months is not None
            and "start_date" in strengths
            and "end_date" in strengths
            and timing.start_date is not None
            and timing.end_date is not None
            and _date_range_supports_duration(
                timing.start_date,
                timing.end_date,
                timing.duration_months,
            )
        ):
            support("duration_months", 5)
    elif explicit_dates:
        if timing.start_date == explicit_dates[0]:
            support(
                "start_date",
                5
                if _provider_date_label_has_field_cue(
                    raw_label,
                    posting_text,
                    kind="start",
                )
                else 4,
            )
        elif timing.end_date == explicit_dates[0]:
            support(
                "end_date",
                5
                if _provider_date_label_has_field_cue(
                    raw_label,
                    posting_text,
                    kind="end",
                )
                else 4,
            )
    month_ranges = _extract_month_range_timing_candidates(raw_label)
    for candidate in month_ranges:
        if timing.start_date and _date_month_marker(timing.start_date) == (
            candidate.start_year,
            candidate.start_month,
        ):
            support("start_date", 4)
        if timing.end_date and _date_month_marker(timing.end_date) == (
            candidate.end_year,
            candidate.end_month,
        ):
            support("end_date", 4)
        expected_duration = _month_range_duration(
            candidate,
            corroborating_duration_months=corroborating_duration_months,
        )
        if timing.duration_months == expected_duration:
            support("duration_months", 4)

    month_year_pattern = re.compile(
        rf"\b(?P<month>{_month_name_pattern()})\.?\s+(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    month_markers = [
        (int(match.group("year")), MONTH_NAMES[match.group("month").casefold()])
        for match in month_year_pattern.finditer(raw_label)
    ]
    if month_markers and timing.start_date:
        if _date_month_marker(timing.start_date) == month_markers[0]:
            support("start_date", 3)
    if len(month_markers) > 1 and timing.end_date:
        if _date_month_marker(timing.end_date) == month_markers[-1]:
            support("end_date", 3)

    academic_match = re.search(
        rf"\b(?P<term>{ACADEMIC_TERM_PATTERN})\s+(?P<year>20\d{{2}})\b",
        raw_label,
        flags=re.IGNORECASE,
    )
    if academic_match:
        term_key = _canonical_term_key(academic_match.group("term"))
        start_term, duration_months = _academic_term_defaults(term_key)
        year = int(academic_match.group("year"))
        start_month = ACADEMIC_TERM_MONTHS[start_term][0]
        expected_start = f"{year:04d}-{start_month:02d}-01"
        expected_end = _end_date_for_month_span(year, start_month, duration_months)
        if timing.start_date == expected_start:
            support("start_date", 2)
        if timing.end_date == expected_end:
            support("end_date", 2)
        if timing.duration_months == duration_months:
            support("duration_months", 2)

    duration_values = {
        duration for duration, _, _, _ in _duration_month_candidates(raw_label)
    }
    if timing.duration_months in duration_values:
        support("duration_months", 4)

    if "start_date" in strengths and "duration_months" in strengths and timing.end_date:
        expected_end = _end_date_for_month_span(
            int(timing.start_date[:4]),
            int(timing.start_date[5:7]),
            timing.duration_months or 0,
        )
        if timing.end_date == expected_end:
            support(
                "end_date",
                min(strengths["start_date"], strengths["duration_months"]),
            )

    schedule_value = _schedule_resolution_from_context(raw_label).value
    if schedule_value and timing.schedule_type == schedule_value:
        support("schedule_type", 4)

    hours_per_week = _hours_per_week(raw_label)
    if hours_per_week is not None:
        if timing.hours_per_week_min == hours_per_week:
            support("hours_per_week_min", 4)
        if timing.hours_per_week_max == hours_per_week:
            support("hours_per_week_max", 4)
    return strengths

def _provider_label_is_exact_date_range(
    raw_label: str,
    explicit_dates: list[tuple[int, int, str, str]],
) -> bool:
    if len(explicit_dates) != 2:
        return False
    if any(
        _timing_mention_is_non_employment_context(raw_label, start, end)
        for start, end, _, _ in explicit_dates
    ):
        return False

    connector = raw_label[explicit_dates[0][1] : explicit_dates[1][0]]
    return bool(
        re.fullmatch(
            r"\s*(?:to|through|thru|until|-|\u2013|\u2014)\s*",
            connector,
            flags=re.IGNORECASE,
        )
    )


def _resolve_provider_timing_date_claims(
    timing: ParsedTiming,
) -> ProviderTimingClaimResolution:
    raw_label = timing.raw_label or ""
    explicit_dates = _posting_date_candidates(raw_label)
    if len(explicit_dates) < 2:
        return ProviderTimingClaimResolution(timing=timing)

    date_claims = _provider_date_claims(raw_label, explicit_dates)
    if not date_claims:
        return ProviderTimingClaimResolution(timing=timing)
    if _provider_label_is_exact_date_range(raw_label, explicit_dates):
        return ProviderTimingClaimResolution(
            timing=timing,
            date_claims=date_claims,
        )

    ambiguous_fields = tuple(
        f"employment_timing.{role}_date"
        for role in ("start", "end")
        if len({claim.value for claim in date_claims if claim.role == role}) > 1
    )
    resolved_claims: dict[str, ProviderDateClaim | None] = {}
    for role in ("start", "end"):
        role_claims = [claim for claim in date_claims if claim.role == role]
        distinct_values = {claim.value for claim in role_claims}
        resolved_claims[role] = role_claims[0] if len(distinct_values) == 1 else None

    start_claim = resolved_claims["start"]
    end_claim = resolved_claims["end"]
    normalized = timing.model_copy(deep=True)
    normalized.start_date = start_claim.value if start_claim else None
    normalized.end_date = end_claim.value if end_claim else None
    normalized.duration_months = (
        _duration_months_from_date_range(normalized.start_date, normalized.end_date)
        if normalized.start_date and normalized.end_date
        else None
    )
    normalized.timing_type = "fixed_term"
    if ambiguous_fields:
        normalized.raw_label = _provider_employment_date_span_label(raw_label, date_claims)
    elif bool(start_claim) != bool(end_claim):
        normalized.raw_label = (start_claim or end_claim).text
    elif start_claim and end_claim:
        normalized.raw_label = _provider_employment_date_span_label(
            raw_label,
            (start_claim, end_claim),
        )

    return ProviderTimingClaimResolution(
        timing=normalized,
        date_claims=date_claims,
        ambiguous_fields=ambiguous_fields,
    )


def _provider_date_claims(
    raw_label: str,
    explicit_dates: list[tuple[int, int, str, str]],
) -> tuple[ProviderDateClaim, ...]:
    if _provider_label_is_exact_date_range(raw_label, explicit_dates):
        return tuple(
            ProviderDateClaim(
                start=start,
                end=end,
                text=date_text,
                value=date_value,
                role="start" if index == 0 else "end",
            )
            for index, (start, end, date_text, date_value) in enumerate(explicit_dates)
        )

    claims: list[ProviderDateClaim] = []
    for start, end, date_text, date_value in explicit_dates:
        role = _provider_date_local_role(raw_label, start, end)
        if role:
            claims.append(
                ProviderDateClaim(
                    start=start,
                    end=end,
                    text=date_text,
                    value=date_value,
                    role=role,
                )
            )
    return tuple(claims)


def _provider_employment_date_span_label(
    raw_label: str,
    date_claims: tuple[ProviderDateClaim, ...],
) -> str:
    first_date_start = min(claim.start for claim in date_claims)
    last_date_end = max(claim.end for claim in date_claims)
    clause = _timing_clause_for_span(raw_label, first_date_start, last_date_end)
    candidate_label = _clean_field_value(clause.text)
    candidate_dates = _posting_date_candidates(candidate_label)
    if candidate_label and not any(
        _timing_mention_is_non_employment_context(candidate_label, start, end)
        for start, end, _, _ in candidate_dates
    ):
        return candidate_label
    return raw_label


def _unresolved_provider_date_claim_ambiguities(
    resolution: ProviderTimingClaimResolution,
    *,
    baseline_timing: ParsedTiming,
    baseline_field_strengths: dict[str, int],
    posting_text: str,
) -> tuple[str, ...]:
    if not resolution.ambiguous_fields:
        return ()
    raw_label = resolution.timing.raw_label or ""
    if not _provider_label_has_scoped_employment_context(raw_label, posting_text):
        return ()

    unresolved: list[str] = []
    for field in resolution.ambiguous_fields:
        attribute = field.removeprefix("employment_timing.")
        role = attribute.removesuffix("_date")
        baseline_value = getattr(baseline_timing, attribute)
        role_values = resolution.values_for_role(role)
        if (
            baseline_value
            and baseline_value in role_values
            and baseline_field_strengths.get(attribute, 0) >= 5
        ):
            continue
        unresolved.append(field)
    return tuple(unresolved)

def _provider_date_local_role(
    raw_label: str,
    date_start: int,
    date_end: int,
) -> str | None:
    if _timing_mention_is_non_employment_context(raw_label, date_start, date_end):
        return None

    clause = _timing_clause_for_span(raw_label, date_start, date_end)
    local_date_start = date_start - clause.start
    label = _field_label(clause.text)
    if label in _EMPLOYMENT_START_FIELD_LABELS:
        return "start"
    if label in _EMPLOYMENT_END_FIELD_LABELS:
        return "end"

    prefix = clause.text[:local_date_start]
    cues = [
        (match.start(), kind)
        for kind in ("start", "end")
        for match in _provider_date_cue_matches(kind, prefix)
    ]
    if cues:
        return max(cues, key=lambda cue: cue[0])[1]

    previous_dates = [
        candidate
        for candidate in _posting_date_candidates(raw_label)
        if candidate[1] <= date_start
    ]
    if not previous_dates:
        return None
    previous_start, previous_end, _, _ = max(
        previous_dates,
        key=lambda candidate: candidate[1],
    )
    connector = raw_label[previous_end:date_start]
    if not re.fullmatch(r"\s*,?\s*(?:or|and/or)\s*", connector, re.IGNORECASE):
        return None
    return _provider_date_local_role(raw_label, previous_start, previous_end)


_PROVIDER_DIRECT_DATE_BRIDGE_PATTERN = re.compile(
    r"\s*(?:[,:(\[\-–—]\s*)?"
    r"(?:(?:is|will\s+be)\s+)?"
    r"(?:(?:(?:the|this|that|its|their|our|your)\s+)?"
    r"(?:(?:internship|placement|employment|contract|work)\s+(?:term|period)|"
    r"term|position|placement|employment|internship|contract|role|job|"
    r"work(?:\s+term)?)\s+)?"
    r"(?:(?:on\s+or\s+(?:about|around)|on|in|by|around|approximately|about|"
    r"as\s+of|no\s+later\s+than|effective|of)\s+)?$",
    flags=re.IGNORECASE,
)


def _provider_date_cue_matches(kind: str, prefix: str) -> tuple[re.Match[str], ...]:
    matches: list[re.Match[str]] = []
    for match in re.finditer(
        _provider_date_cue_pattern(kind),
        prefix,
        flags=re.IGNORECASE,
    ):
        bridge = prefix[match.end() :]
        if len(bridge) > 45:
            continue
        if not _PROVIDER_DIRECT_DATE_BRIDGE_PATTERN.fullmatch(bridge):
            continue
        matches.append(match)
    return tuple(matches)


def _provider_date_cue_pattern(kind: str) -> str:
    if kind == "start":
        return (
            r"(?:\b(?:commenc(?:e|es|ed|ing|ement)|start(?:s|ed|ing)?(?:\s+date)?|"
            r"begin(?:s|ning)?|launch(?:es|ed|ing)?|expected\s+start(?:\s+date)?|"
            r"anticipated\s+start(?:\s+date)?|effective)\b|"
            r"\b(?:term|position|placement|employment|internship|contract)\b"
            r"[^.;:\n]{0,20}\bfrom\b)"
        )
    return (
        r"\b(?:end(?:s|ed|ing)?(?:\s+date)?|finish(?:es|ed|ing)?|"
        r"conclud(?:e|es|ed|ing)|complet(?:e|es|ed|ing)|"
        r"expir(?:e|es|ed|ing)|terminat(?:e|es|ed|ing)|through|until)\b"
    )


def _provider_date_label_has_field_cue(
    raw_label: str,
    posting_text: str,
    *,
    kind: str,
) -> bool:
    allowed_labels = (
        _EMPLOYMENT_START_FIELD_LABELS
        if kind == "start"
        else _EMPLOYMENT_END_FIELD_LABELS
    )
    if any(
        _provider_date_local_role(raw_label, start, end) == kind
        for start, end, _, _ in _posting_date_candidates(raw_label)
    ):
        return True

    label_pattern = re.compile(re.escape(raw_label), flags=re.IGNORECASE)
    for line in _posting_content_lines(posting_text):
        for match in label_pattern.finditer(line):
            if _field_label(line) in allowed_labels:
                return True
            prefix = line[max(0, match.start() - 100) : match.start()]
            if _provider_date_cue_matches(kind, prefix):
                return True
    return False

def _merge_provider_timing_fields(
    provider: ParsedTiming,
    baseline: ParsedTiming,
    *,
    supported_fields: frozenset[str],
    use_provider_label: bool,
) -> ParsedTiming:
    merged = baseline.model_copy(deep=True)
    if use_provider_label:
        merged.raw_label = provider.raw_label
        if provider.timing_type != "unknown":
            merged.timing_type = provider.timing_type
    for attribute in supported_fields:
        setattr(merged, attribute, getattr(provider, attribute))

    if "duration_months" in supported_fields and "end_date" not in supported_fields:
        merged.end_date = None

    if merged.start_date and merged.end_date and merged.start_date > merged.end_date:
        if "end_date" not in supported_fields:
            merged.end_date = None
        elif "start_date" not in supported_fields:
            merged.start_date = None

    if merged.start_date and merged.end_date:
        if (
            merged.duration_months is None
            or not _date_range_supports_duration(
                merged.start_date,
                merged.end_date,
                merged.duration_months,
            )
        ):
            merged.duration_months = _duration_months_from_date_range(
                merged.start_date,
                merged.end_date,
            )
    elif merged.start_date and merged.duration_months:
        merged.end_date = _end_date_for_month_span(
            int(merged.start_date[:4]),
            int(merged.start_date[5:7]),
            merged.duration_months,
        )
    return merged

def _provider_timing_has_scoped_evidence(timing: ParsedTiming, posting_text: str) -> bool:
    raw_label = timing.raw_label
    if not raw_label or not posting_text:
        return False
    if not _provider_timing_label_is_bounded(raw_label):
        return False
    if not _provider_timing_label_has_timing_signal(raw_label):
        return False
    corroborating_duration_months = frozenset(
        duration.months
        for duration in _employment_timing_evidence(posting_text).duration_terms
    )
    if not _provider_timing_values_match_raw_label(
        timing,
        corroborating_duration_months=corroborating_duration_months,
    ):
        return False
    if any(
        _timing_mention_is_non_employment_context(raw_label, start, end)
        for start, end, _, _ in _posting_date_candidates(raw_label)
    ):
        return False

    return _provider_label_has_scoped_employment_context(raw_label, posting_text)


def _provider_label_has_scoped_employment_context(
    raw_label: str,
    posting_text: str,
) -> bool:
    if (
        not raw_label
        or not posting_text
        or not _provider_timing_label_is_bounded(raw_label)
        or not _provider_timing_label_has_timing_signal(raw_label)
    ):
        return False

    lines = _posting_content_lines(posting_text)
    title = _title_value(lines)
    pattern = re.compile(re.escape(raw_label), flags=re.IGNORECASE)
    return any(
        _timing_mention_is_employment_context(
            line,
            match.start(),
            match.end(),
            title=title,
        )
        for line in lines
        for match in pattern.finditer(line)
    )


def _provider_timing_label_is_bounded(raw_label: str) -> bool:
    return (
        len(raw_label) <= MAX_PROVIDER_TIMING_LABEL_CHARS
        and len(re.findall(r"\b[\w'-]+\b", raw_label))
        <= MAX_PROVIDER_TIMING_LABEL_WORDS
    )


def _provider_timing_label_has_timing_signal(raw_label: str) -> bool:
    month_or_term = re.search(
        rf"\b(?:{_month_name_pattern()}|{ACADEMIC_TERM_PATTERN})\.?\s+20\d{{2}}\b",
        raw_label,
        flags=re.IGNORECASE,
    )
    dated = _posting_date_candidates(raw_label)
    duration = re.search(
        r"\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)\s*(?:-|\s)?(?:months?|mos?)\b",
        raw_label,
        flags=re.IGNORECASE,
    )
    immediate = re.search(r"\bimmediate(?:ly)?\b", raw_label, flags=re.IGNORECASE)
    return bool(month_or_term or dated or duration or immediate)


def _provider_timing_values_match_raw_label(
    timing: ParsedTiming,
    *,
    corroborating_duration_months: frozenset[int] = frozenset(),
) -> bool:
    if timing.start_date and timing.end_date and timing.start_date > timing.end_date:
        return False

    raw_label = timing.raw_label or ""
    explicit_date_candidates = _posting_date_candidates(raw_label)
    explicit_dates = [candidate[3] for candidate in explicit_date_candidates]
    date_claims = (
        _provider_date_claims(raw_label, explicit_date_candidates)
        if len(explicit_date_candidates) >= 2
        else ()
    )
    if date_claims:
        for role, attribute in (("start", "start_date"), ("end", "end_date")):
            role_values = {claim.value for claim in date_claims if claim.role == role}
            timing_value = getattr(timing, attribute)
            if len(role_values) == 1 and timing_value != next(iter(role_values)):
                return False
            if len(role_values) > 1 and timing_value is not None:
                return False
            if not role_values and timing_value is not None:
                return False
        if timing.duration_months is not None:
            if not timing.start_date or not timing.end_date:
                return False
            if not _date_range_supports_duration(
                timing.start_date,
                timing.end_date,
                timing.duration_months,
            ):
                return False
    elif len(explicit_dates) >= 2:
        if timing.start_date != explicit_dates[0] or timing.end_date != explicit_dates[-1]:
            return False
        if timing.duration_months is not None and not _date_range_supports_duration(
            explicit_dates[0],
            explicit_dates[-1],
            timing.duration_months,
        ):
            return False
    elif explicit_dates:
        explicit_date = explicit_dates[0]
        if timing.start_date:
            if timing.start_date != explicit_date:
                return False
        elif timing.end_date:
            if timing.end_date != explicit_date:
                return False
        else:
            return False

    label_duration_months = frozenset(
        duration for duration, _, _, _ in _duration_month_candidates(raw_label)
    )
    supported_duration_months = (
        corroborating_duration_months | label_duration_months
    )
    month_ranges = _extract_month_range_timing_candidates(raw_label)
    if len(month_ranges) == 1:
        candidate = month_ranges[0]
        if timing.start_date and _date_month_marker(timing.start_date) != (
            candidate.start_year,
            candidate.start_month,
        ):
            return False
        if timing.end_date and _date_month_marker(timing.end_date) != (
            candidate.end_year,
            candidate.end_month,
        ):
            return False
        if (
            timing.duration_months is not None
            and timing.duration_months
            != _month_range_duration(
                candidate,
                corroborating_duration_months=supported_duration_months,
            )
        ):
            return False

    month_year_pattern = re.compile(
        rf"\b(?P<month>{_month_name_pattern()})\.?\s+(?P<year>20\d{{2}})\b",
        flags=re.IGNORECASE,
    )
    month_markers = [
        (int(match.group("year")), MONTH_NAMES[match.group("month").casefold()])
        for match in month_year_pattern.finditer(raw_label)
    ]
    if not month_ranges and month_markers and timing.start_date:
        if _date_month_marker(timing.start_date) != month_markers[0]:
            return False
    if not month_ranges and len(month_markers) > 1 and timing.end_date:
        if _date_month_marker(timing.end_date) != month_markers[-1]:
            return False

    duration_values = {
        int(match.group("months"))
        for match in re.finditer(
            r"\b(?P<months>\d{1,2})\s*(?:-|\s)?(?:months?|mos?)\b",
            raw_label,
            flags=re.IGNORECASE,
        )
    }
    if (
        timing.duration_months is not None
        and duration_values
        and timing.duration_months not in duration_values
    ):
        return False
    return True


def _quality_fields_with_prefix(fields: list[str], *, matches: bool) -> list[str]:
    return [
        field
        for field in fields
        if field.startswith("employment_timing.") is matches
    ]


def _merge_quality_fields(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for field in group:
            if field not in merged:
                merged.append(field)
    return merged


def _selected_timing_quality_fields(
    provider_fields: list[str],
    baseline_fields: list[str],
    *,
    provider_selected_fields: frozenset[str],
) -> list[str]:
    return _merge_quality_fields(
        [
            field
            for field in baseline_fields
            if field.removeprefix("employment_timing.") not in provider_selected_fields
        ],
        [
            field
            for field in provider_fields
            if field.removeprefix("employment_timing.") in provider_selected_fields
        ],
    )


def _reconciled_timing_missing_fields(
    provider_quality: ParsedDataQuality,
    baseline_quality: ParsedDataQuality,
    *,
    use_provider: bool,
    provider_selected_fields: frozenset[str],
) -> list[str]:
    non_timing = _quality_fields_with_prefix(provider_quality.missing_fields, matches=False)
    provider_timing_fields = _quality_fields_with_prefix(
        provider_quality.missing_fields,
        matches=True,
    )
    baseline_timing_fields = _quality_fields_with_prefix(
        baseline_quality.missing_fields,
        matches=True,
    )
    selected_timing_fields = (
        _selected_timing_quality_fields(
            provider_timing_fields,
            baseline_timing_fields,
            provider_selected_fields=provider_selected_fields,
        )
        if use_provider
        else baseline_timing_fields
    )
    return _merge_quality_fields(non_timing, selected_timing_fields)


def _reconciled_timing_ambiguous_fields(
    provider_quality: ParsedDataQuality,
    baseline_quality: ParsedDataQuality,
    *,
    provider_timing: ParsedTiming,
    baseline_timing: ParsedTiming,
    provider_supported: bool,
    use_provider: bool,
    provider_supported_fields: frozenset[str],
    provider_selected_fields: frozenset[str],
    provider_field_strengths: dict[str, int],
    baseline_field_strengths: dict[str, int],
) -> list[str]:
    non_timing = _quality_fields_with_prefix(provider_quality.ambiguous_fields, matches=False)
    provider_timing_fields = _quality_fields_with_prefix(
        provider_quality.ambiguous_fields,
        matches=True,
    )
    baseline_timing_fields = _quality_fields_with_prefix(
        baseline_quality.ambiguous_fields,
        matches=True,
    )
    supported_provider_timing_fields = [
        field
        for field in provider_timing_fields
        if field.removeprefix("employment_timing.") in provider_supported_fields
    ]
    supported_conflicts = [
        field
        for field in _employment_timing_conflict_fields(
            baseline_timing,
            provider_timing,
            baseline_field_strengths=baseline_field_strengths,
            provider_field_strengths=provider_field_strengths,
        )
        if field.removeprefix("employment_timing.") in provider_supported_fields
    ]
    if use_provider:
        selected_timing_fields = _merge_quality_fields(
            _selected_timing_quality_fields(
                supported_provider_timing_fields,
                baseline_timing_fields,
                provider_selected_fields=provider_selected_fields,
            ),
            supported_conflicts,
        )
    elif provider_supported:
        selected_timing_fields = _merge_quality_fields(
            baseline_timing_fields,
            supported_provider_timing_fields,
            supported_conflicts,
        )
    else:
        selected_timing_fields = baseline_timing_fields
    return _merge_quality_fields(non_timing, selected_timing_fields)


def _reconciled_timing_inferred_fields(
    provider_quality: ParsedDataQuality,
    baseline_quality: ParsedDataQuality,
    *,
    provider_timing: ParsedTiming,
    selected_timing: ParsedTiming,
    provider_supported: bool,
    use_provider: bool,
    provider_supported_fields: frozenset[str],
    provider_selected_fields: frozenset[str],
) -> list[str]:
    non_timing = _quality_fields_with_prefix(provider_quality.inferred_fields, matches=False)
    provider_timing_fields = _quality_fields_with_prefix(
        provider_quality.inferred_fields,
        matches=True,
    )
    baseline_timing_fields = _quality_fields_with_prefix(
        baseline_quality.inferred_fields,
        matches=True,
    )
    supported_provider_timing_fields = [
        field
        for field in provider_timing_fields
        if field.removeprefix("employment_timing.") in provider_supported_fields
    ]
    if use_provider:
        selected_timing_fields = _selected_timing_quality_fields(
            supported_provider_timing_fields,
            baseline_timing_fields,
            provider_selected_fields=provider_selected_fields,
        )
    else:
        selected_timing_fields = baseline_timing_fields
        if provider_supported:
            selected_timing_fields = _merge_quality_fields(
                selected_timing_fields,
                [
                    field
                    for field in supported_provider_timing_fields
                    if _timing_quality_field_matches_selected_value(
                        field,
                        provider_timing,
                        selected_timing,
                    )
                ],
            )
    return _merge_quality_fields(non_timing, selected_timing_fields)


def _timing_quality_field_matches_selected_value(
    field: str,
    provider_timing: ParsedTiming,
    selected_timing: ParsedTiming,
) -> bool:
    attribute = field.removeprefix("employment_timing.")
    if attribute not in ParsedTiming.model_fields:
        return False
    provider_value = getattr(provider_timing, attribute)
    return provider_value is not None and provider_value == getattr(selected_timing, attribute)


def _employment_timing_conflict_fields(
    baseline_timing: ParsedTiming,
    provider_timing: ParsedTiming,
    *,
    baseline_field_strengths: dict[str, int],
    provider_field_strengths: dict[str, int],
) -> list[str]:
    conflicts: list[str] = []
    for attribute in ("start_date", "end_date"):
        baseline_value = getattr(baseline_timing, attribute)
        provider_value = getattr(provider_timing, attribute)
        if not baseline_value or not provider_value or baseline_value == provider_value:
            continue
        both_dates_are_exact = (
            baseline_field_strengths.get(attribute, 0) >= 5
            and provider_field_strengths.get(attribute, 0) >= 5
        )
        if not both_dates_are_exact and baseline_value[:7] == provider_value[:7]:
            continue
        conflicts.append(f"employment_timing.{attribute}")
    for attribute in ("duration_months", "schedule_type"):
        baseline_value = getattr(baseline_timing, attribute)
        provider_value = getattr(provider_timing, attribute)
        if baseline_value and provider_value and baseline_value != provider_value:
            conflicts.append(f"employment_timing.{attribute}")
    return conflicts
