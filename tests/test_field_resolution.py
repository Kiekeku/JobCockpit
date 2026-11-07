from jobcockpit.ingestion.field_resolution import EvidenceRule, select_evidence
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_document import parse_session


def test_authority_preserves_candidate_identity_and_explains_rejected_evidence():
    preferred, fallback = object(), object()
    with parse_session() as session:
        result = select_evidence(
            "timing",
            (
                EvidenceRule("incompatible_range", object(), lambda: False),
                EvidenceRule("explicit_range", preferred, lambda: True),
                EvidenceRule("seasonal_default", fallback, lambda: 1 / 0),
            ),
        )
        assert result is preferred
        assert [item["state"] for item in session.decisions] == [
            "incompatible",
            "selected",
            "lower_priority",
        ]


def test_real_timing_resolution_explains_source_authority():
    # Existing observed-posting fixture: a labelled employment date range.
    with parse_session() as session:
        parsed = parse_job_text(
            "Title: Platform Developer Intern\n"
            "Expected start date to end date: November 1, 2026 to June 31, 2027"
        )
        assert parsed.employment_timing.start_date == "2026-11-01"
        assert parsed.employment_timing.end_date is None
        assert any(item["state"] == "selected" for item in session.decisions)
