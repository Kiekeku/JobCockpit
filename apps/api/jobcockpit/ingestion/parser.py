from __future__ import annotations

import re

from jobcockpit.ingestion.application_documents import (
    APPLICATION_DOCUMENT_AMBIGUITY,
    parse_required_documents,
)
from jobcockpit.ingestion.browser_capture_adapters import (
    BrowserCaptureContext,
    extract_browser_capture_fields,
)
from jobcockpit.ingestion.codex_posting_parser import CodexCliPostingParser
from jobcockpit.ingestion.compensation_parser import _parse_compensation
from jobcockpit.ingestion.eligibility_parser import (
    _role_type_from_title,
    resolve_eligibility,
)
from jobcockpit.ingestion.employer_identity import resolve_corroborated_employer_identity
from jobcockpit.ingestion.employment_timing import (
    _application_deadline,
    _apply_title_hint_duration,
    _authoritative_academic_term_label,
    _authoritative_title_timing_fields,
    _duration_months_from_date_range,
    _employment_timing_evidence,
    _has_explicit_body_duration_evidence,
    _intern_start_month_choices_candidate,
    _labeled_exact_duration_options_candidate,
    _parse_employment_timing_resolution,
    _owned_role_calendar_candidate,
    _role_owned_exact_date_range_candidate,
    _source_title_calendar_candidate,
    _source_title_has_authoritative_term,
    _stale_winter_availability_correction,
    _title_hint_academic_term,
    _title_hint_month_range_timing,
    _title_hint_timing_ambiguities,
    _yearless_month_range_timing,
)
from jobcockpit.ingestion.llm_providers import (
    PostingParser,
    create_posting_parser,
)
from jobcockpit.ingestion.location_parser import (
    extract_country_value as _extract_country_value,
)
from jobcockpit.ingestion.location_parser import (
    parse_location as _parse_location,
)
from jobcockpit.ingestion.parser_models import (
    ParsedApplication,
    ParsedDataQuality,
    ParsedEmployer,
    ParsedFit,
    ParsedJob,
    ParsedLocation,
    ParsedRole,
    ParsedSource,
    ProviderTimingContract,
)
from jobcockpit.ingestion.parser_models import (
    ParsedTiming as ParsedTiming,
)
from jobcockpit.ingestion.posting_document import parsing_session
from jobcockpit.ingestion.posting_fields import _provenance_field_semantic
from jobcockpit.ingestion.posting_scope import (
    _employment_timing_content_scope,
    _posting_content_scope,
    _posting_field_spans,
    _text_without_labeled_fields,
)
from jobcockpit.ingestion.posting_text_cleaning import clean_aggregator_posting_text
from jobcockpit.ingestion.provider_timing import (
    _apply_authoritative_timing_resolution,
)
from jobcockpit.ingestion.role_parser import (
    _company_value,
    _domains_from_capabilities,
    _extract_non_technical_capabilities,
    _extract_role_capabilities,
    _extract_role_option_capabilities,
    _extract_tech_stack,
    _fill_role_capability_defaults,
    _infer_capability_domains,
    _is_generic_careers_title,
    _location_value,
    _merge_domains,
    _replace_implausible_title_with_baseline,
    _title_value,
)
from jobcockpit.ingestion.role_semantic_projection import (
    RoleSemanticProjection,
    project_role_semantics,
    project_validated_role_evidence,
)

__all__ = (
    "CodexCliPostingParser",
    "ParsedFit",
    "ParsedJob",
    "ParsedLocation",
    "ParsedTiming",
    "ProviderTimingContract",
    "parse_job_text",
    "parse_job_text_with_settings",
)

_CAPABILITY_IDENTITY_FIELD_LABELS = frozenset(
    {
        "email card",
        "email row",
        "job title",
        "page title",
        "position",
        "resolution evidence",
        "role",
        "source evidence",
        "title",
    }
)


@parsing_session
def parse_job_text_with_settings(
    raw_text: str,
    source_url: str | None = None,
    settings: object | None = None,
    *,
    parser: PostingParser | None = None,
    source_type: str | None = None,
    title_hint: str | None = None,
) -> ParsedJob:
    raw_text = clean_aggregator_posting_text(raw_text, source_url=source_url)
    posting_scope = _posting_content_scope(raw_text)
    heuristic = parse_job_text(
        raw_text,
        source_url,
        source_type=source_type,
        title_hint=title_hint,
    )
    provider = str(getattr(settings, "posting_parser_provider", "heuristic") or "heuristic")
    if provider == "heuristic":
        return heuristic

    parser = parser or create_posting_parser(settings)
    try:
        parsed = parser.parse(posting_scope.text, source_url=source_url, baseline=heuristic)
    except Exception:
        error_code = _provider_error_code(parser)
        return _with_parser_telemetry(
            heuristic,
            provider=provider,
            outcome="provider_fallback",
            error_code=(
                "provider_exception" if error_code == "provider_returned_no_result" else error_code
            ),
        )
    if not parsed:
        return _with_parser_telemetry(
            heuristic,
            provider=provider,
            outcome="provider_fallback",
            error_code=_provider_error_code(parser),
        )
    normalized = _finalize_parsed_job_data_quality(
        parsed,
        baseline=heuristic,
        posting_text=_employment_timing_content_scope(posting_scope.text).timing_text,
        role_posting_text=posting_scope.text,
    )
    # Descriptions retain the source, including when no role clauses were found.
    # Provider summaries must not replace either representation of that evidence.
    normalized.role.description_raw = heuristic.role.description_raw
    normalized.role.description_clean = heuristic.role.description_clean
    return _with_parser_telemetry(
        normalized,
        provider=provider,
        outcome="provider_success",
    )


def _finalize_parsed_job_data_quality(
    parsed: ParsedJob,
    baseline: ParsedJob | None = None,
    *,
    posting_text: str = "",
    role_posting_text: str = "",
) -> ParsedJob:
    normalized = parsed.model_copy(deep=True)
    _replace_implausible_title_with_baseline(normalized, baseline=baseline)
    discarded_unsupported_timing = _apply_authoritative_timing_resolution(
        normalized,
        baseline=baseline,
        posting_text=posting_text,
    )
    provider_role_evidence = _apply_role_semantic_projection(
        normalized,
        baseline=baseline,
        posting_text=role_posting_text,
    )
    _fill_role_capability_defaults(
        normalized,
        baseline=None if provider_role_evidence else baseline,
        posting_text=role_posting_text,
    )
    _apply_authoritative_required_documents(normalized, baseline=baseline)
    original_quality = parsed.data_quality
    original_missing_fields = list(original_quality.missing_fields)

    if "role.tech_stack" in original_missing_fields:
        normalized.data_quality.missing_fields = [
            field for field in normalized.data_quality.missing_fields if field != "role.tech_stack"
        ]

    normalized.data_quality.needs_manual_review = _needs_manual_review_after_quality_cleanup(
        original_quality,
        normalized.data_quality,
        discarded_unsupported_timing=discarded_unsupported_timing,
    )
    _apply_source_term_correction(
        normalized,
        posting_text=role_posting_text,
        title=baseline.role.job_title if baseline else normalized.role.job_title,
    )
    _apply_yearless_work_term_inference(normalized, posting_text=role_posting_text)
    resolve_corroborated_employer_identity(normalized, role_posting_text)
    return normalized


def _apply_source_term_correction(
    parsed: ParsedJob, *, posting_text: str, title: str | None,
) -> None:
    correction = _stale_winter_availability_correction(posting_text, title)
    if correction is None:
        return
    for field in ("raw_label", "timing_type", "start_date", "end_date", "duration_months"):
        setattr(parsed.employment_timing, field, getattr(correction, field))
    for field in ("employment_timing.start_date", "employment_timing.end_date"):
        for values in (parsed.data_quality.inferred_fields, parsed.data_quality.ambiguous_fields):
            if field not in values:
                values.append(field)
    parsed.data_quality.needs_manual_review = True


def _apply_yearless_work_term_inference(parsed: ParsedJob, *, posting_text: str) -> None:
    fields = ("employment_timing.start_date", "employment_timing.end_date")
    if any(field in parsed.data_quality.ambiguous_fields for field in fields):
        return
    inferred = _yearless_month_range_timing(posting_text, parsed.employment_timing)
    if inferred is None:
        return
    parsed.employment_timing = inferred
    for field in fields:
        if field not in parsed.data_quality.inferred_fields:
            parsed.data_quality.inferred_fields.append(field)


def _apply_role_semantic_projection(
    parsed: ParsedJob,
    *,
    baseline: ParsedJob | None,
    posting_text: str,
) -> bool:
    provider_supplied_evidence = bool(parsed.role.evidence)
    provider_supplied_clause_semantics = any(
        (
            parsed.role.responsibilities,
            parsed.role.must_have_requirements,
            parsed.role.nice_to_have_requirements,
            parsed.role.required_capabilities,
            parsed.role.preferred_capabilities,
            parsed.role.responsibility_capabilities,
        )
    )
    provider_supplied_aggregate_semantics = any(
        (
            parsed.role.capabilities,
            parsed.role.capability_domains,
            parsed.role.tech_stack,
            parsed.role.domain_keywords,
        )
    )
    provider_projection = project_validated_role_evidence(
        posting_text,
        parsed.role.evidence,
    )
    if provider_projection is not None:
        provider_tech_order = list(parsed.role.tech_stack)
        _assign_role_semantic_projection(parsed.role, provider_projection)
        projected_by_key = {
            value.casefold(): value for value in parsed.role.tech_stack
        }
        ordered_tech_stack: list[str] = []
        for provider_value in provider_tech_order:
            canonical = projected_by_key.get(provider_value.casefold())
            if canonical is not None and canonical not in ordered_tech_stack:
                ordered_tech_stack.append(canonical)
        for projected_value in parsed.role.tech_stack:
            if projected_value not in ordered_tech_stack:
                ordered_tech_stack.append(projected_value)
        parsed.role.tech_stack = ordered_tech_stack
        for field in provider_projection.ambiguity_fields:
            if field not in parsed.data_quality.ambiguous_fields:
                parsed.data_quality.ambiguous_fields.append(field)
        _restore_deterministic_role_description(parsed.role, baseline=baseline)
        _restore_deterministic_title_capabilities(parsed.role, baseline=baseline)
        _restore_deterministic_role_domains(parsed.role, baseline=baseline)
        return True

    if provider_supplied_evidence or provider_supplied_clause_semantics:
        _restore_deterministic_role_semantics(parsed.role, baseline=baseline)
        return True
    _restore_deterministic_role_clauses(parsed.role, baseline=baseline)
    if provider_supplied_aggregate_semantics:
        _ground_provider_role_aggregates(parsed.role, baseline=baseline)
    return False


def _restore_deterministic_role_semantics(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    _restore_deterministic_role_clauses(role, baseline=baseline)
    baseline_role = baseline.role if baseline is not None else None
    for field_name in (
        "capabilities",
        "tech_stack",
    ):
        setattr(
            role,
            field_name,
            list(getattr(baseline_role, field_name)) if baseline_role is not None else [],
        )
    _restore_deterministic_role_domains(role, baseline=baseline)


def _ground_provider_role_aggregates(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    baseline_role = baseline.role if baseline is not None else None
    for field_name in (
        "capabilities",
        "capability_domains",
        "tech_stack",
        "domain_keywords",
    ):
        baseline_values = list(getattr(baseline_role, field_name)) if baseline_role else []
        canonical_by_key = {value.casefold(): value for value in baseline_values}
        grounded: list[str] = []
        for provider_value in getattr(role, field_name):
            canonical = canonical_by_key.get(provider_value.casefold())
            if canonical is not None and canonical not in grounded:
                grounded.append(canonical)
        for baseline_value in baseline_values:
            if baseline_value not in grounded:
                grounded.append(baseline_value)
        setattr(role, field_name, grounded)


def _restore_deterministic_role_description(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    if baseline is not None and baseline.role.evidence:
        role.description_clean = baseline.role.description_clean


def _restore_deterministic_role_domains(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    baseline_role = baseline.role if baseline is not None else None
    for field_name in ("capability_domains", "domain_keywords"):
        setattr(
            role,
            field_name,
            list(getattr(baseline_role, field_name)) if baseline_role is not None else [],
        )


def _restore_deterministic_title_capabilities(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    """Preserve grounded technologies encoded by the deterministic title grammar."""

    if baseline is None:
        return
    title_technologies = _extract_tech_stack(
        baseline.role.job_title or "",
        assume_title_context=True,
        title_supporting_domains=baseline.role.capability_domains,
    )
    for field_name in ("capabilities", "tech_stack"):
        values = getattr(role, field_name)
        normalized_values = {value.casefold() for value in values}
        for technology in title_technologies:
            if technology.casefold() in normalized_values:
                continue
            values.append(technology)
            normalized_values.add(technology.casefold())


def _restore_deterministic_role_clauses(
    role: ParsedRole,
    *,
    baseline: ParsedJob | None,
) -> None:
    baseline_role = baseline.role if baseline is not None else None
    _restore_deterministic_role_description(role, baseline=baseline)
    role.evidence = (
        [item.model_copy(deep=True) for item in baseline_role.evidence]
        if baseline_role is not None
        else []
    )
    for field_name in (
        "responsibilities",
        "must_have_requirements",
        "nice_to_have_requirements",
        "required_capabilities",
        "preferred_capabilities",
        "responsibility_capabilities",
        "environment_capabilities",
    ):
        setattr(
            role,
            field_name,
            list(getattr(baseline_role, field_name)) if baseline_role is not None else [],
        )


def _assign_role_semantic_projection(
    role: ParsedRole,
    projection: RoleSemanticProjection,
) -> None:
    role.evidence = projection.evidence
    role.responsibilities = projection.responsibilities
    role.must_have_requirements = projection.must_have_requirements
    role.nice_to_have_requirements = projection.nice_to_have_requirements
    role.required_capabilities = projection.required_capabilities
    role.preferred_capabilities = projection.preferred_capabilities
    role.responsibility_capabilities = projection.responsibility_capabilities
    role.environment_capabilities = projection.environment_capabilities
    role.capabilities = projection.capabilities
    role.tech_stack = projection.tech_stack


def _apply_authoritative_required_documents(
    parsed: ParsedJob,
    *,
    baseline: ParsedJob | None,
) -> None:
    if baseline is None:
        return
    parsed.application.documents_required = baseline.application.documents_required
    parsed.application.resume_required = baseline.application.resume_required
    parsed.application.cover_letter_required = baseline.application.cover_letter_required
    parsed.application = ParsedApplication.model_validate(parsed.application.model_dump())

    marker = APPLICATION_DOCUMENT_AMBIGUITY
    parsed.data_quality.ambiguous_fields = [
        field for field in parsed.data_quality.ambiguous_fields if field != marker
    ]
    if marker in baseline.data_quality.ambiguous_fields:
        parsed.data_quality.ambiguous_fields.append(marker)


def _with_parser_telemetry(
    parsed: ParsedJob,
    *,
    provider: str,
    outcome: str,
    error_code: str | None = None,
) -> ParsedJob:
    parsed.source.parser_provider = provider
    parsed.source.parser_outcome = outcome
    parsed.source.parser_error_code = error_code
    return parsed


def _provider_error_code(parser: PostingParser) -> str:
    raw_error = str(getattr(parser, "last_error", "") or "provider_returned_no_result")
    candidate = raw_error.partition(":")[0].strip().casefold()
    if re.fullmatch(r"[a-z0-9_.-]{1,128}", candidate):
        return candidate
    return "provider_returned_no_result"


def _needs_manual_review_after_quality_cleanup(
    original_quality: ParsedDataQuality,
    normalized_quality: ParsedDataQuality,
    *,
    discarded_unsupported_timing: bool,
) -> bool:
    if normalized_quality.missing_fields or normalized_quality.ambiguous_fields:
        return True
    if not original_quality.needs_manual_review:
        return False
    return not _manual_review_reasons_were_removed(
        original_quality,
        normalized_quality,
        discarded_unsupported_timing=discarded_unsupported_timing,
    )


def _manual_review_reasons_were_removed(
    original_quality: ParsedDataQuality,
    normalized_quality: ParsedDataQuality,
    *,
    discarded_unsupported_timing: bool,
) -> bool:
    if original_quality.extraction_confidence < 0.7:
        return False

    had_explicit_reason = False
    quality_lists = (
        (original_quality.missing_fields, normalized_quality.missing_fields),
        (original_quality.ambiguous_fields, normalized_quality.ambiguous_fields),
        (original_quality.inferred_fields, normalized_quality.inferred_fields),
    )
    for original_fields, normalized_fields in quality_lists:
        for field in original_fields:
            had_explicit_reason = True
            if field == "role.tech_stack" and field not in normalized_fields:
                continue
            if (
                field == APPLICATION_DOCUMENT_AMBIGUITY
                and field not in normalized_fields
            ):
                continue
            if (
                discarded_unsupported_timing
                and field.startswith("employment_timing.")
                and field not in normalized_fields
            ):
                continue
            return False
    return had_explicit_reason




@parsing_session
def parse_job_text(
    raw_text: str,
    source_url: str | None = None,
    *,
    source_type: str | None = None,
    title_hint: str | None = None,
) -> ParsedJob:
    title_hint = _normalize_title_hint(title_hint)
    raw_text = clean_aggregator_posting_text(raw_text, source_url=source_url)
    posting_scope = _posting_content_scope(raw_text)
    posting_text = posting_scope.text
    lines = list(posting_scope.lines)
    capture_context = BrowserCaptureContext(
        raw_text=posting_text,
        lines=lines,
        source_url=source_url,
        source_type=source_type,
    )
    capture_fields = extract_browser_capture_fields(capture_context)
    title = title_hint or capture_fields.title or _title_value(lines)
    normalized_title = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") if title else None
    lower = posting_text.lower()
    company = capture_fields.company or _company_value(lines, title, source_url)
    location_label = capture_fields.location_label or _location_value(lines)
    country_label = _extract_country_value(lines)
    parsed_location = _parse_location(
        location_label,
        lower,
        source_type=source_type,
        country_label=country_label,
    )
    role_semantics = project_role_semantics(posting_text)
    fallback_capability_text = _capability_fallback_text(posting_text, title)
    use_capability_fallback = bool(
        role_semantics is None
        or (
            not role_semantics.tech_stack
            and _role_projection_allows_capability_fallback(role_semantics)
        )
    )
    fallback_tech_stack = (
        _extract_tech_stack(fallback_capability_text)
        if use_capability_fallback
        else []
    )
    capability_text = (
        fallback_capability_text
        if use_capability_fallback
        else role_semantics.candidate_text
    )
    body_tech_stack = (
        list(dict.fromkeys([*role_semantics.tech_stack, *fallback_tech_stack]))
        if role_semantics is not None
        else fallback_tech_stack
    )
    title_supporting_domains = _infer_capability_domains(
        title,
        capability_text,
        supported_capabilities=(
            role_semantics.capabilities if role_semantics is not None else body_tech_stack
        ),
    )
    title_tech_stack = _extract_tech_stack(
        title or "",
        assume_title_context=True,
        title_supporting_domains=title_supporting_domains,
    )
    title_non_technical_capabilities = _extract_non_technical_capabilities(
        title or "",
        assume_title_context=True,
        title_supporting_domains=title_supporting_domains,
    )
    role_option_capabilities = _extract_role_option_capabilities(title, posting_text)
    tech_stack = _extract_tech_stack(
        "\n".join([*body_tech_stack, *title_tech_stack]),
        assume_capability_context=True,
    )
    body_capabilities = (
        list(
            dict.fromkeys(
                [
                    *(body_tech_stack if fallback_tech_stack else []),
                    *role_semantics.capabilities,
                    *title_tech_stack,
                    *role_option_capabilities,
                ]
            )
        )
        if role_semantics is not None
        else list(
            dict.fromkeys(
                [
                    *_extract_role_capabilities(
                        "\n".join(
                            item
                            for item in (title or "", fallback_capability_text)
                            if item
                        ),
                        tech_stack,
                        restrict_technologies_to_stack=True,
                        require_candidate_scope=True,
                    ),
                    *role_option_capabilities,
                ]
            )
        )
    )
    capabilities = list(
        dict.fromkeys([*body_capabilities, *title_non_technical_capabilities])
    )
    for capability, pattern in (
        ("Product Management", r"\b(?:associate\s+)?product\s+manager\b"),
        (
            "Quality Assurance",
            r"\b(?:qa|quality\s+assurance|sdet|software\s+engineer\s+in\s+test|"
            r"test\s+engineer|testing)\b",
        ),
    ):
        if (
            re.search(pattern, title or "", re.IGNORECASE)
            and capability not in capabilities
        ):
            capabilities.append(capability)
    if (
        re.search(r"\bfull[- ]?stack\b", title or "", re.IGNORECASE)
        and "Backend Development" in capabilities
        and "Frontend Development" in capabilities
    ):
        capabilities.append("Full-Stack Development")
    capability_domains = _infer_capability_domains(
        title,
        capability_text,
        supported_capabilities=capabilities,
    )
    capability_domains = _merge_domains(
        capability_domains,
        _domains_from_capabilities(capabilities, capability_domains),
    )
    missing_fields = []
    if not title:
        missing_fields.append("role.job_title")
    compensation, compensation_ambiguous = _parse_compensation(posting_text, parsed_location)
    timing_text = (
        f"Ingest Title Hint: {title_hint}\n{posting_text}" if title_hint else posting_text
    )
    timing_resolution = _parse_employment_timing_resolution(timing_text)
    source_timing_evidence = _employment_timing_evidence(posting_text)
    authoritative_source_term = _authoritative_academic_term_label(
        source_timing_evidence
    )
    authoritative_source_title_lines = {
        item.line
        for item in source_timing_evidence.academic_terms
        if item.is_title_authority
    }
    body_timing_text = posting_text
    for item in source_timing_evidence.academic_terms:
        if not item.is_title_authority:
            continue
        title_term_text = item.line[item.start : item.end]
        body_timing_text = re.sub(
            re.escape(title_term_text),
            lambda match: " " * len(match.group(0)),
            body_timing_text,
            count=1,
            flags=re.IGNORECASE,
        )
    body_timing_resolution = (
        _parse_employment_timing_resolution(body_timing_text)
        if title_hint or authoritative_source_title_lines
        else timing_resolution
    )
    source_timing_title = (
        title
        if (
            title_hint is None
            and _source_title_has_authoritative_term(title)
        )
        else None
    )
    timing_title = title_hint or source_timing_title
    authoritative_title_fields = _authoritative_title_timing_fields(timing_title)
    title_has_explicit_date_range = bool(
        timing_title and _title_hint_month_range_timing(timing_title) is not None
    )
    title_term_defaults = (
        _parse_employment_timing_resolution(
            f"Ingest Title Hint: {timing_title}"
        ).timing
        if timing_title and not title_has_explicit_date_range
        else None
    )
    timing_base = timing_resolution.timing
    body_timing = body_timing_resolution.timing
    body_timing_evidence = _employment_timing_evidence(body_timing_text)
    body_has_duration = _has_explicit_body_duration_evidence(
        posting_text,
        body_timing,
    )
    title_has_authoritative_term = bool(
        authoritative_title_fields
        & {
            "employment_timing.start_date",
            "employment_timing.end_date",
        }
        or (
            title_hint
            and timing_resolution.timing.start_date
            and (
                body_timing.start_date is None
                or timing_resolution.timing.start_date[:7]
                != body_timing.start_date[:7]
            )
        )
    )
    body_duration_is_authoritative = bool(
        timing_title
        and body_has_duration
        and "employment_timing.duration_months"
        not in authoritative_title_fields
    )
    if body_duration_is_authoritative:
        if title_has_authoritative_term:
            compatible_body_end = (
                body_timing.end_date
                if body_timing.start_date
                and timing_base.start_date
                and body_timing.start_date[:7] == timing_base.start_date[:7]
                else None
            )
            timing_base = timing_base.model_copy(
                update={
                    "end_date": compatible_body_end,
                    "duration_months": body_timing.duration_months,
                    "duration_label": body_timing.duration_label,
                }
            )
        else:
            timing_base = body_timing
    employment_timing = _apply_title_hint_duration(
        timing_base,
        timing_title,
        body_duration_is_authoritative=body_duration_is_authoritative,
    )
    start_choices = _intern_start_month_choices_candidate(posting_text, None)
    if (
        start_choices is not None
        and employment_timing.raw_label == start_choices.timing.raw_label
        and (timing_title is None or _title_hint_academic_term(timing_title) is not None)
        and not title_has_explicit_date_range
    ):
        # A season-only title cannot choose between explicit start months.
        employment_timing.start_date = employment_timing.end_date = None
        authoritative_title_fields = authoritative_title_fields - {
            "employment_timing.start_date", "employment_timing.end_date",
        }
    duration_choices = _labeled_exact_duration_options_candidate(posting_text)
    has_open_duration_choices = bool(
        duration_choices is not None
        and employment_timing.duration_label == duration_choices.timing.duration_label
        and employment_timing.duration_months is None
        and employment_timing.end_date is None
    )
    if has_open_duration_choices:
        authoritative_title_fields = authoritative_title_fields - {
            "employment_timing.duration_months", "employment_timing.end_date",
        }
    if authoritative_source_term and not _source_title_has_authoritative_term(
        timing_title
    ):
        term_label, term_year = authoritative_source_term
        employment_timing.raw_label = f"{term_label} {term_year}"
    body_dates_conflict_with_title = bool(
        (
            body_timing_evidence.has_start_dates
            and body_timing.start_date != employment_timing.start_date
        )
        or (
            body_timing_evidence.has_end_dates
            and body_timing.end_date != employment_timing.end_date
        )
        or (
            body_timing_evidence.has_end_dates
            and employment_timing.start_date
            and body_timing.end_date
            and employment_timing.start_date > body_timing.end_date
        )
        or (
            body_timing_evidence.has_start_dates
            and body_timing.start_date
            and employment_timing.end_date
            and body_timing.start_date > employment_timing.end_date
        )
    )
    explicit_body_date_conflicts = {
        field
        for field in body_timing_resolution.ambiguous_fields
        if body_dates_conflict_with_title
        and field
        in {
            "employment_timing.start_date",
            "employment_timing.end_date",
        }
    }
    compatible_body_date_overrides: set[str] = set()
    if (
        not title_has_explicit_date_range
        and body_timing_evidence.has_start_dates
        and body_timing.start_date
        and body_timing.start_date != employment_timing.start_date
        and title_term_defaults
        and title_term_defaults.start_date
        and title_term_defaults.end_date
        and title_term_defaults.start_date
        <= body_timing.start_date
        <= title_term_defaults.end_date
    ):
        compatible_body_date_overrides.add("employment_timing.start_date")
    if (
        not title_has_explicit_date_range
        and body_timing_evidence.has_end_dates
        and body_timing.end_date
        and body_timing.end_date != employment_timing.end_date
        and title_term_defaults
        and title_term_defaults.start_date
        and title_term_defaults.end_date
        and title_term_defaults.start_date
        <= body_timing.end_date
        <= title_term_defaults.end_date
    ):
        compatible_body_date_overrides.add("employment_timing.end_date")
    if (
        body_dates_conflict_with_title
        and body_timing_evidence.has_end_dates
        and employment_timing.start_date
        and body_timing.end_date
        and employment_timing.start_date > body_timing.end_date
    ):
        explicit_body_date_conflicts.add("employment_timing.start_date")
    if (
        body_dates_conflict_with_title
        and body_timing_evidence.has_start_dates
        and body_timing.start_date
        and employment_timing.end_date
        and body_timing.start_date > employment_timing.end_date
    ):
        explicit_body_date_conflicts.add("employment_timing.end_date")
    explicit_body_date_conflicts = frozenset(explicit_body_date_conflicts)
    body_date_overrides = explicit_body_date_conflicts | frozenset(
        compatible_body_date_overrides
    )
    if timing_title and body_date_overrides:
        if (
            "employment_timing.start_date" in body_date_overrides
            and body_timing_evidence.has_start_dates
            and body_timing.start_date
        ):
            employment_timing.start_date = body_timing.start_date
        elif (
            "employment_timing.start_date" in body_date_overrides
            and body_timing_evidence.has_end_dates
            and body_timing.end_date
            and employment_timing.start_date
            and employment_timing.start_date > body_timing.end_date
        ):
            employment_timing.start_date = None
        if (
            "employment_timing.end_date" in body_date_overrides
            and body_timing_evidence.has_end_dates
            and body_timing.end_date
        ):
            employment_timing.end_date = body_timing.end_date
        elif (
            "employment_timing.end_date" in body_date_overrides
            and body_timing_evidence.has_start_dates
            and body_timing.start_date
            and employment_timing.end_date
            and body_timing.start_date > employment_timing.end_date
        ):
            employment_timing.end_date = None
        if body_duration_is_authoritative:
            employment_timing.end_date = body_timing.end_date
            employment_timing.duration_months = body_timing.duration_months
        elif employment_timing.start_date and employment_timing.end_date:
            employment_timing.duration_months = _duration_months_from_date_range(
                employment_timing.start_date,
                employment_timing.end_date,
            )
    timing_ambiguity_source = timing_resolution.ambiguous_fields
    if body_duration_is_authoritative:
        timing_ambiguity_source = tuple(
            field
            for field in timing_resolution.ambiguous_fields
            if field != "employment_timing.duration_months"
        ) + tuple(
            field
            for field in body_timing_resolution.ambiguous_fields
            if field == "employment_timing.duration_months"
        )
    if has_open_duration_choices:
        timing_ambiguity_source = tuple(dict.fromkeys((
            *timing_ambiguity_source,
            *duration_choices.ambiguous_fields,
        )))
    timing_ambiguities = tuple(
        field
        for field in timing_ambiguity_source
        if field not in authoritative_title_fields
        or field in explicit_body_date_conflicts
    )
    title_timing_ambiguities = _title_hint_timing_ambiguities(
        timing_title,
    )
    complete_calendar = _owned_role_calendar_candidate(posting_text)
    if complete_calendar is None and not body_timing_evidence.has_end_dates:
        complete_calendar = _source_title_calendar_candidate(posting_text)
    exact_calendar = _role_owned_exact_date_range_candidate(posting_text)
    if exact_calendar and employment_timing.start_date and employment_timing.end_date:
        exact = exact_calendar.timing
        # A day-level commitment refines the same title calendar window.
        if (exact.start_date and exact.end_date
                and exact.start_date[:7] == employment_timing.start_date[:7]
                and exact.end_date[:7] == employment_timing.end_date[:7]):
            complete_calendar = exact_calendar
    if complete_calendar:
        if employment_timing.timing_type in ('unknown', 'posted_date_fallback'):
            employment_timing.raw_label = complete_calendar.timing.raw_label
        for field in ('start_date', 'end_date', 'duration_months', 'timing_type'):
            setattr(employment_timing, field, getattr(complete_calendar.timing, field))
    role_type = _role_type_from_title(title, posting_text, employment_timing)
    required_documents = parse_required_documents(posting_text)
    eligibility_resolution = resolve_eligibility(
        posting_text,
        role_type,
        role_semantics.evidence if role_semantics else None,
    )
    inferred_fields = []
    if employment_timing.timing_type == "posted_date_fallback":
        inferred_fields.append("employment_timing.start_date")
    parsed = ParsedJob(
        source=ParsedSource(
            source_url=source_url,
            parser_provider="heuristic",
            parser_outcome="heuristic",
        ),
        employer=ParsedEmployer(company_name=company),
        role=ParsedRole(
            job_title=title,
            normalized_title=normalized_title,
            role_type=role_type,
            description_raw=raw_text,
            description_clean=posting_text,
            responsibilities=(role_semantics.responsibilities if role_semantics else []),
            must_have_requirements=(
                role_semantics.must_have_requirements if role_semantics else []
            ),
            nice_to_have_requirements=(
                role_semantics.nice_to_have_requirements if role_semantics else []
            ),
            evidence=role_semantics.evidence if role_semantics else [],
            required_capabilities=(
                role_semantics.required_capabilities if role_semantics else []
            ),
            preferred_capabilities=(
                role_semantics.preferred_capabilities if role_semantics else []
            ),
            responsibility_capabilities=(
                role_semantics.responsibility_capabilities if role_semantics else []
            ),
            environment_capabilities=(
                role_semantics.environment_capabilities if role_semantics else []
            ),
            capabilities=capabilities,
            capability_domains=capability_domains,
            tech_stack=tech_stack,
        ),
        employment_timing=employment_timing,
        location=parsed_location,
        eligibility=eligibility_resolution.eligibility,
        compensation=compensation,
        application=ParsedApplication(
            application_deadline=_application_deadline(posting_text),
            external_apply_url=source_url,
            documents_required=(
                list(required_documents.documents_required)
                if required_documents.documents_required is not None
                else None
            ),
            resume_required=required_documents.resume_required,
            cover_letter_required=required_documents.cover_letter_required,
        ),
        fit_analysis=ParsedFit(),
        data_quality=ParsedDataQuality(
            missing_fields=missing_fields,
            ambiguous_fields=[
                *eligibility_resolution.ambiguous_fields,
                *timing_ambiguities,
                *title_timing_ambiguities,
                *(
                    ["compensation.pay_min", "compensation.pay_max"]
                    if compensation_ambiguous
                    else []
                ),
                *(
                    [APPLICATION_DOCUMENT_AMBIGUITY]
                    if required_documents.ambiguous
                    else []
                ),
                *(role_semantics.ambiguity_fields if role_semantics else []),
            ],
            inferred_fields=inferred_fields,
            needs_manual_review=bool(
                missing_fields
                or eligibility_resolution.ambiguous_fields
                or timing_ambiguities
                or title_timing_ambiguities
                or compensation_ambiguous
                or required_documents.ambiguous
                or (role_semantics and role_semantics.ambiguity_fields)
            ),
        ),
    )
    _apply_source_term_correction(parsed, posting_text=posting_text, title=title)
    _apply_yearless_work_term_inference(parsed, posting_text=posting_text)
    resolve_corroborated_employer_identity(parsed, posting_text)
    return parsed


def _normalize_title_hint(title_hint: str | None) -> str | None:
    if not title_hint:
        return None
    normalized = re.sub(r"\s+", " ", title_hint).strip()
    return None if _is_generic_careers_title(normalized) else normalized or None


def _capability_fallback_text(raw_text: str, title: str | None) -> str:
    identity_fields = tuple(
        field
        for field in _posting_field_spans(raw_text)
        if field.label in _CAPABILITY_IDENTITY_FIELD_LABELS
        or _provenance_field_semantic(field.label) == "role"
    )
    scoped_text = _text_without_labeled_fields(raw_text, identity_fields)
    if not title:
        return scoped_text
    standalone_title = re.compile(
        rf"^[ \t]*{re.escape(title)}[ \t]*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return standalone_title.sub(
        lambda match: " " * len(match.group(0)),
        scoped_text,
    )


def _role_projection_allows_capability_fallback(
    projection: RoleSemanticProjection,
) -> bool:
    return all(
        item.source_section == "unsectioned" for item in projection.evidence
    )
