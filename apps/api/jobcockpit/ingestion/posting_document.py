"""Shared immutable source structure and request-local parsing work.

Only immutable values belong in this cache. ParsedJob/RoleEvidence and timing
resolutions contain mutable Pydantic models and must never be memoized here.
Each exact text variant has its own source coordinate system; title hints and
masked bodies cannot accidentally reuse the original body's offsets.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable, Iterator, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class ParseSession:
    max_entries: int = 16384
    cache: dict[tuple, object] = field(default_factory=dict)
    decisions: list[dict[str, object]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0


_SESSION: ContextVar[ParseSession | None] = ContextVar("posting_parse_session", default=None)


@contextmanager
def parse_session(*, max_entries: int = 16384) -> Iterator[ParseSession]:
    existing = _SESSION.get()
    if existing is not None:
        yield existing
        return
    session = ParseSession(max_entries=max_entries)
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)
        session.cache.clear()


def parsing_session(function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with parse_session():
            return function(*args, **kwargs)

    return wrapped


def session_cached(function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        session = _SESSION.get()
        if session is None:
            return function(*args, **kwargs)
        key = (function, args, tuple(sorted(kwargs.items())))
        if key in session.cache:
            session.cache_hits += 1
            return session.cache[key]  # type: ignore[return-value]
        session.cache_misses += 1
        result = function(*args, **kwargs)
        if len(session.cache) < session.max_entries:
            session.cache[key] = result
        return result

    return wrapped


def record_decision(field_name: str, rule: str, **details: object) -> None:
    session = _SESSION.get()
    if session is not None:
        session.decisions.append({"field": field_name, "rule": rule, **details})


@dataclass(frozen=True)
class SourceSpan:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class PostingDocument:
    text: str
    physical_lines: tuple[str, ...]
    blocks: tuple[SourceSpan, ...]
    parentheses: tuple[SourceSpan, ...]

    def enclosing_parenthetical(self, start: int, end: int) -> SourceSpan | None:
        owners = [span for span in self.parentheses if span.start <= start <= end <= span.end]
        return min(owners, key=lambda span: span.end - span.start, default=None)


@session_cached
def document_for_text(text: str) -> PostingDocument:
    blocks = []
    offset = 0
    lines = tuple(text.splitlines(keepends=True))
    for line in lines:
        content = line.strip()
        if content:
            start = offset + len(line) - len(line.lstrip())
            blocks.append(SourceSpan(content, start, start + len(content)))
        offset += len(line)
    stack: list[int] = []
    parentheses = []
    for index, char in enumerate(text):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            start = stack.pop() + 1
            parentheses.append(SourceSpan(text[start:index], start, index))
    return PostingDocument(text, lines, tuple(blocks), tuple(parentheses))


@session_cached
def punctuation_boundaries(text: str, pattern: str) -> tuple[tuple[int, int], ...]:
    """Index a field's existing boundary grammar once, without changing it."""
    return tuple(match.span() for match in re.finditer(pattern, text))
