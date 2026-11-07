from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobcockpit.ingestion.parser import parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.parser_models import ParsedApplication


@pytest.mark.parametrize(
    ("posting_excerpt", "expected_documents", "cover_letter_required"),
    [
        (
            "Please submit your resume, cover letter, and unofficial transcript together as one attachment.",
            ["resume", "cover_letter", "transcript"],
            True,
        ),
        (
            "Submit your resume and a short cover letter via our Careers Page.",
            ["resume", "cover_letter"],
            True,
        ),
        (
            "To Apply: Please submit your resume and portfolio.",
            ["resume", "portfolio"],
            None,
        ),
    ],
    ids=["combined-packet", "short-cover-letter", "resume-portfolio"],
)
def test_real_minimized_posting_instructions_are_parsed(
    posting_excerpt: str,
    expected_documents: list[str],
    cover_letter_required: bool | None,
) -> None:
    parsed = parse_job_text(f"Example Intern\n{posting_excerpt}")

    assert parsed.application.documents_required == expected_documents
    assert parsed.application.resume_required is True
    assert parsed.application.cover_letter_required is cover_letter_required


def test_provider_cannot_replace_evidence_backed_document_requirements() -> None:
    class UnsupportedDocumentParser:
        def parse(self, _raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.application.documents_required = ["resume"]
            parsed.application.resume_required = True
            parsed.application.cover_letter_required = False
            return parsed

    parsed = parse_job_text_with_settings(
        "Example Intern\nSubmit a resume, cover letter, and transcript.",
        settings=SimpleNamespace(posting_parser_provider="test_provider"),
        parser=UnsupportedDocumentParser(),
    )

    assert parsed.application.documents_required == ["resume", "cover_letter", "transcript"]
    assert parsed.application.cover_letter_required is True
    assert parsed.source.parser_provider == "test_provider"
    assert parsed.source.parser_outcome == "provider_success"
    assert parsed.source.parser_error_code is None


def test_provider_empty_result_records_sanitized_fallback_code() -> None:
    class EmptyParser:
        last_error = "provider_invalid_payload: posting text must not be persisted"

        def parse(self, _raw_text, source_url=None, baseline=None):
            return None

    parsed = parse_job_text_with_settings(
        "Example Intern\nUpload your CV.",
        settings=SimpleNamespace(posting_parser_provider="test_provider"),
        parser=EmptyParser(),
    )

    assert parsed.application.documents_required == ["resume"]
    assert parsed.source.parser_provider == "test_provider"
    assert parsed.source.parser_outcome == "provider_fallback"
    assert parsed.source.parser_error_code == "provider_invalid_payload"


def test_provider_exception_records_fallback_without_exception_text() -> None:
    class FailingParser:
        def parse(self, _raw_text, source_url=None, baseline=None):
            raise RuntimeError("private posting detail")

    parsed = parse_job_text_with_settings(
        "Example Intern\nNo resume is required.",
        settings=SimpleNamespace(posting_parser_provider="test_provider"),
        parser=FailingParser(),
    )

    assert parsed.application.documents_required == []
    assert parsed.application.resume_required is False
    assert parsed.source.parser_outcome == "provider_fallback"
    assert parsed.source.parser_error_code == "provider_exception"


def test_explicit_do_not_submit_documents_is_not_ambiguous() -> None:
    parsed = parse_job_text(
        "Support Intern\nDo not submit a resume or cover letter."
    )

    assert parsed.application.documents_required == []
    assert parsed.application.resume_required is False
    assert parsed.application.cover_letter_required is False
    assert "application.documents_required" not in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is False


def test_contracted_do_not_submit_is_not_ambiguous() -> None:
    parsed = parse_job_text("Support Intern\nDon't submit a resume.")

    assert parsed.application.documents_required == []
    assert parsed.application.resume_required is False
    assert parsed.application.cover_letter_required is None
    assert "application.documents_required" not in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is False


def test_reference_implementation_is_not_an_application_document() -> None:
    parsed = parse_job_text(
        "Software Intern\nProvide a reference implementation for the API."
    )

    assert parsed.application.documents_required is None
    assert parsed.application.resume_required is None
    assert parsed.application.cover_letter_required is None


def test_negated_cover_letter_does_not_cancel_required_resume() -> None:
    parsed = parse_job_text(
        "Support Intern\nSubmit your resume, but do not include a cover letter."
    )

    assert parsed.application.documents_required == ["resume"]
    assert parsed.application.resume_required is True
    assert parsed.application.cover_letter_required is False
    assert "application.documents_required" not in parsed.data_quality.ambiguous_fields


def test_provider_document_ambiguity_cannot_leave_stale_manual_review() -> None:
    class UnsupportedDocumentParser:
        def parse(self, _raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.application.documents_required = ["resume"]
            parsed.application.resume_required = True
            parsed.data_quality.ambiguous_fields = ["application.documents_required"]
            parsed.data_quality.needs_manual_review = True
            return parsed

    parsed = parse_job_text_with_settings(
        "Example Intern\nBuild APIs.",
        settings=SimpleNamespace(posting_parser_provider="test_provider"),
        parser=UnsupportedDocumentParser(),
    )

    assert parsed.application.documents_required is None
    assert parsed.application.resume_required is None
    assert "application.documents_required" not in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is False


@pytest.mark.parametrize(
    ("resume_required", "cover_letter_required"),
    [
        (False, None),
        (None, False),
        (False, False),
    ],
)
def test_explicit_false_document_flags_create_an_empty_required_list(
    resume_required: bool | None,
    cover_letter_required: bool | None,
) -> None:
    application = ParsedApplication(
        documents_required=None,
        resume_required=resume_required,
        cover_letter_required=cover_letter_required,
    )

    assert application.documents_required == []
