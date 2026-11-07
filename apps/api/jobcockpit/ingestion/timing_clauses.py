from __future__ import annotations

import re

from jobcockpit.ingestion.posting_document import session_cached
from jobcockpit.ingestion.posting_fields import _field_label
from jobcockpit.ingestion.posting_scope import _clause_separator_positions
from jobcockpit.ingestion.timing_constants import NON_EMPLOYMENT_TIMING_DATE_LABELS
from jobcockpit.ingestion.timing_models import TimingClause

TIMING_CLAUSE_CONTEXT_EMPLOYMENT = "employment"
TIMING_CLAUSE_CONTEXT_APPLICATION = "application"
TIMING_CLAUSE_CONTEXT_INTERVIEW = "interview"
TIMING_CLAUSE_CONTEXT_UNKNOWN = "unknown"
_TIMING_CLAUSE_DETERMINER_PATTERN = r"(?:(?:the|this|that|our|their|your|its|an?)\s+)?"
_EMPLOYMENT_TIMING_CLAUSE_SUBJECT_PATTERN = (
    r"(?:"
    r"(?:internship|placement|employment|contract|work)\s+(?:term|period)|"
    r"placement|internship|position|employment|contract|work\s+term|term|role|job|co-?op)"
)
_TIMING_CLAUSE_SUBJECT_PATTERN = (
    rf"{_TIMING_CLAUSE_DETERMINER_PATTERN}"
    rf"(?:applications?|interviews?|postings?|recruiting|"
    rf"{_EMPLOYMENT_TIMING_CLAUSE_SUBJECT_PATTERN})"
)
_TIMING_CLAUSE_PREDICATE_PATTERN = (
    r"(?:is|are|was|were|will|would|may|can|must|"
    r"starts?|begins?|commences?|launches?|runs?|lasts?|spans?|extends?|"
    r"ends?|finishes?|concludes?|completes?|expires?|terminates?|"
    r"opens?|closes?|closing|requires?|accepts?|accepted)"
)
_COORDINATED_TIMING_CLAUSE_BOUNDARY_PATTERN = re.compile(
    rf",\s*(?:(?:and|but|while|whereas)\s+)?"
    rf"(?=(?:{_TIMING_CLAUSE_SUBJECT_PATTERN}\s+"
    rf"{_TIMING_CLAUSE_PREDICATE_PATTERN}\b|"
    r"(?:application\s+deadline|application\s+(?:start|end)\s+date|"
    r"posting\s+(?:start|end|close|closing)\s+date|start\s+date|end\s+date)\s*:))",
    flags=re.IGNORECASE,
)


@session_cached
def _semantic_timing_clause_boundaries(segment: str) -> tuple[tuple[int, int], ...]:
    hard_boundaries = ((position, position + 1) for position in _clause_separator_positions(segment))
    coordinated_boundaries = (
        match.span() for match in _COORDINATED_TIMING_CLAUSE_BOUNDARY_PATTERN.finditer(segment)
    )
    return tuple(sorted(set((*hard_boundaries, *coordinated_boundaries))))


@session_cached
def _timing_clause_for_span(segment: str, span_start: int, span_end: int) -> TimingClause:
    boundaries = _semantic_timing_clause_boundaries(segment)
    clause_start = max(
        (boundary_end for _, boundary_end in boundaries if boundary_end <= span_start),
        default=0,
    )
    clause_end = min(
        (boundary_start for boundary_start, _ in boundaries if boundary_start >= span_end),
        default=len(segment),
    )
    clause_text = segment[clause_start:clause_end]
    return TimingClause(
        text=clause_text,
        start=clause_start,
        end=clause_end,
        context_type=_timing_clause_context_type(clause_text),
    )


def _timing_clause_context_type(clause_text: str) -> str:
    label = _field_label(clause_text)
    if label in {"interview date", "interview window"}:
        return TIMING_CLAUSE_CONTEXT_INTERVIEW
    if label in NON_EMPLOYMENT_TIMING_DATE_LABELS:
        return TIMING_CLAUSE_CONTEXT_APPLICATION

    stripped = clause_text.lstrip()
    determiner = _TIMING_CLAUSE_DETERMINER_PATTERN
    if re.match(
        rf"{determiner}(?:(?:applications?|postings?|recruiting)\s+"
        rf"{_TIMING_CLAUSE_PREDICATE_PATTERN}\b|"
        r"apply\s+(?:before|by|during|through|until)\b)",
        stripped,
        flags=re.IGNORECASE,
    ):
        return TIMING_CLAUSE_CONTEXT_APPLICATION
    if re.match(
        rf"{determiner}interviews?\s+{_TIMING_CLAUSE_PREDICATE_PATTERN}\b",
        stripped,
        flags=re.IGNORECASE,
    ):
        return TIMING_CLAUSE_CONTEXT_INTERVIEW
    if re.match(
        rf"{determiner}{_EMPLOYMENT_TIMING_CLAUSE_SUBJECT_PATTERN}\b",
        stripped,
        flags=re.IGNORECASE,
    ):
        return TIMING_CLAUSE_CONTEXT_EMPLOYMENT
    return TIMING_CLAUSE_CONTEXT_UNKNOWN
