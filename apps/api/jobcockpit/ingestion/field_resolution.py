"""Named evidence precedence shared by field projections and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from jobcockpit.ingestion.posting_document import record_decision

T = TypeVar("T")


@dataclass(frozen=True)
class EvidenceRule(Generic[T]):
    name: str
    candidate: T | None
    applies: Callable[[], object] = lambda: True


def select_evidence(field_name: str, rules: tuple[EvidenceRule[T], ...]) -> T | None:
    """Keep source authority explicit, preserving lazy compatibility checks.

    Return the actual candidate (not a copy): callers use its identity when
    refining compatible body dates and resolving source conflicts.
    """
    selected: T | None = None
    for rule in rules:
        if rule.candidate is None:
            continue
        if selected is not None:
            state = "lower_priority"
        elif rule.applies():
            selected = rule.candidate
            state = "selected"
        else:
            state = "incompatible"
        record_decision(field_name, rule.name, state=state)
    if selected is None:
        record_decision(field_name, "no_authoritative_candidate", state="unknown")
    return selected
