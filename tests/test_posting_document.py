from concurrent.futures import ThreadPoolExecutor

import pytest
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_document import (
    document_for_text,
    parse_session,
    session_cached,
)
from jobcockpit.ingestion.posting_scope import _posting_content_scope


def test_parse_session_reuses_variants_and_releases_private_text():
    with parse_session() as session:
        first = _posting_content_scope("Title: Student\nDuration: 4 months")
        assert _posting_content_scope("Title: Student\nDuration: 4 months") is first
        assert _posting_content_scope("Title: Student\nDuration: 8 months") != first
        assert session.cache
    assert session.cache == {}
    assert _posting_content_scope("Title: Student\nDuration: 4 months") is not first


def test_sessions_are_isolated_between_threads_and_clean_up_after_failure():
    def parse_variant(text):
        with parse_session() as session:
            scope = _posting_content_scope(text)
            with parse_session() as nested:
                assert nested is session
            return scope.text

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(parse_variant, ["Title: One", "Title: Two"])) == [
            "Title: One",
            "Title: Two",
        ]
    with pytest.raises(RuntimeError):
        with parse_session() as session:
            _posting_content_scope("Title: Failing")
            raise RuntimeError("parse failed")
    assert not session.cache


def test_document_retains_exact_spans_and_parenthetical_ownership():
    text = "Qualifications:\n- Currently enrolled (preference for co-op students).\n"
    with parse_session():
        document = document_for_text(text)
        assert document_for_text(text) is document
        for block in document.blocks:
            assert text[block.start : block.end] == block.text
        start = text.index("co-op")
        owned = document.enclosing_parenthetical(start, start + 5)
        assert owned is not None
        assert text[owned.start : owned.end] == "preference for co-op students"
        assert document.physical_lines == tuple(text.splitlines(keepends=True))


def test_nested_parser_call_keeps_session_and_cached_results_are_immutable():
    with parse_session() as session:
        parsed = parse_job_text("Title: Student\nDuration: 4 months")
        assert parsed.role.job_title == "Student"
        assert session.cache
        document = document_for_text("Title: Student")
        with pytest.raises(AttributeError):
            document.text = "changed"


def test_session_cache_is_bounded_and_does_not_cache_exceptions():
    attempts = 0

    @session_cached
    def fail_once(value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("retry")
        return value

    with parse_session(max_entries=2) as session:
        with pytest.raises(ValueError):
            fail_once("private")
        assert fail_once("private") == "private"
        assert fail_once("private") == "private"
        assert attempts == 2
        for i in range(10):
            fail_once(i)
        assert len(session.cache) <= 2
