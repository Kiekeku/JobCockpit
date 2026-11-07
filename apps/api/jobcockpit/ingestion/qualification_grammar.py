"""Academic predicates and requirement logic shared by role and eligibility.

Lexical recognition identifies a status predicate. Its governing section,
parenthesis and logical branch determine whether it is a requirement.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from jobcockpit.ingestion.parser_models import RequirementExpression
from jobcockpit.ingestion.posting_document import SourceSpan, document_for_text, session_cached

_WORDS = re.compile(r"[A-Za-z]+")


@session_cached
def academic_completion_condition(text: str) -> SourceSpan | None:
    words = tuple(_WORDS.finditer(text))
    tokens = tuple(word.group().casefold() for word in words)
    for index in range(len(tokens) - 3):
        if tokens[index : index + 3] != ("will", "not", "graduate"):
            continue
        boundary = index + 3
        if tokens[boundary : boundary + 1] != ("before",) and tokens[boundary : boundary + 2] != (
            "prior",
            "to",
        ):
            continue
        # Graduation is an applicant status; the boundary must name this work
        # period, rather than a prediction about an unrelated future event.
        if not {"internship", "role", "term"}.intersection(tokens[boundary:]):
            continue
        end = text.find("(", words[boundary].start())
        end = len(text) if end < 0 else end
        while end > 0 and text[end - 1].isspace():
            end -= 1
        start = words[boundary].start()
        return SourceSpan(text[start:end], start, end)
    return None


def academic_predicate_spans(text: str) -> tuple[SourceSpan, ...]:
    """Separate an enrollment matrix, its future condition and preference.

    The source-backed production is a coordinated enrollment/status clause with
    an optional parenthetical preference. Every returned span is an exact slice.
    """
    tokens = tuple(_WORDS.finditer(text))
    if tuple(word.group().casefold() for word in tokens[:2]) != ("currently", "enrolled"):
        return ()
    if academic_completion_condition(text) is None:
        return ()
    future = next((word for word in tokens if word.group().casefold() == "will"), None)
    if future is None:
        return ()
    connector = re.search(r",?\s+and\s*$", text[: future.start()], re.IGNORECASE)
    if connector is None:
        return ()
    document = document_for_text(text)
    preference = next(
        (
            span
            for span in document.parentheses
            if span.start > future.start() and span.text.casefold().startswith("preference for ")
        ),
        None,
    )
    matrix_end = (preference.start - 1) if preference else len(text)
    bounds = [(0, connector.start()), (future.start(), matrix_end)]
    if preference is not None:
        bounds.append((preference.start, preference.end))
    spans = []
    for start, end in bounds:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        spans.append(SourceSpan(text[start:end], start, end))
    return tuple(spans)


def requirement_is_necessary(
    expression: RequirementExpression,
    classify_predicate: Callable[[str], bool | None],
) -> bool | None:
    """Evaluate complete predicates, preserving unknown object fragments.

    A university-name alternative does not waive its shared enrollment head.
    Callers return None for such fragments, False only for a complete predicate
    that permits satisfying the requirement without the fact being projected.
    """
    if not expression.children:
        return classify_predicate(expression.text or "")
    branches = [
        requirement_is_necessary(child, classify_predicate) for child in expression.children
    ]
    if expression.operator in {"any_of", "alternative"}:
        if False in branches:
            return False
        return True if all(branch is True for branch in branches) else None
    if True in branches:
        return True
    return False if all(branch is False for branch in branches) else None
