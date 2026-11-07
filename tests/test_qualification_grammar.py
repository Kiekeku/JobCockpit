from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


def test_scoped_enrollment_claims_retain_owner_condition_and_exact_evidence():
    case = next(
        c
        for c in load_parser_corpus().cases
        if c.id == "grammar_enrollment_parenthetical_preference"
    )
    parsed = parse_job_text(case.raw_text, case.source_url)
    evidence = parsed.role.evidence[0]
    assert [claim.owner for claim in evidence.claims] == ["applicant"] * 3
    assert evidence.claims[1].condition_type == "temporal"
    assert evidence.claims[1].condition_text == "prior to this co-op role"
    assert [claim.polarity for claim in evidence.claims] == ["positive", "negative", "positive"]
    for claim in evidence.claims:
        assert parsed.role.description_clean[claim.source_start : claim.source_end] == claim.text
