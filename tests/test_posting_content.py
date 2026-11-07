"""Content-state contracts grounded in saved fallback and browser snapshots.

ATCO/Iris/Motorola provide metadata footers, Lumentum an Indeed block,
Audiokinetic a LinkedIn authwall, and Foresters a description inside evidence.
These are ingestion/recovery decisions, not changes to parser expectations.
"""
from dataclasses import replace

import pytest

from jobcockpit.ingestion.posting_discovery import _structured_source_hint_resolution, posting_has_description
from jobcockpit.ingestion.posting_links import raw_text_with_resolution_evidence, source_resolution_for_resolution
from jobcockpit.ingestion.posting_resolution import ExternalPostingHint


def metadata_resolution():
    hint = ExternalPostingHint(source_url="https://ca.indeed.com/viewjob?jk=1234567890abcdef",
        source_kind="indeed", company="Example Engineering", title="Software Engineering Intern",
        location="Calgary, AB")
    resolution = _structured_source_hint_resolution(hint, reason="official_posting_not_found")
    return hint, resolution


def persisted_text(hint, resolution):
    return raw_text_with_resolution_evidence(resolution.posting, hint,
        source_resolution=source_resolution_for_resolution(hint.source_url, resolution))


def test_real_persistence_footer_does_not_turn_metadata_into_description():
    hint, resolution = metadata_resolution()
    raw_text = persisted_text(hint, resolution)
    assert "Official Discovery Status:" in raw_text and "Resolution Evidence:" in raw_text
    assert not posting_has_description(replace(resolution.posting, raw_text=raw_text), hint)


@pytest.mark.parametrize("raw", [
    "Page Title: Blocked - Indeed.com\nSource URL: https://ca.indeed.com/viewjob?jk=1234567890abcdef\n"
    "Find jobs\nCompany Reviews\nRequest Blocked\nYou have been blocked.\nTroubleshooting Cloudflare Errors",
    "Page Title: Sign Up | LinkedIn\nSource URL: https://www.linkedin.com/authwall\n"
    "Skip to main content\nLinkedIn\nJoin LinkedIn\nEmail\nPassword (6+ characters)\n"
    "By clicking Agree & Join, you agree to the LinkedIn User Agreement, Privacy Policy, and Cookie Policy.",
])
def test_blocked_snapshot_is_not_description_even_with_identity_and_evidence(raw):
    hint, resolution = metadata_resolution()
    posting = replace(resolution.posting, raw_text=raw)
    raw_text = persisted_text(hint, replace(resolution, posting=posting))
    assert not posting_has_description(replace(posting, raw_text=raw_text), hint)


def test_provenance_only_fetched_candidate_cannot_qualify_as_description():
    hint, resolution = metadata_resolution()
    posting = replace(resolution.posting, raw_text=persisted_text(hint, resolution),
        source_url="https://www.linkedin.com/jobs/view/1234567890")
    assert not posting_has_description(posting, hint)


def test_legacy_email_title_prefix_does_not_make_repeated_identity_a_description():
    # Observed in the saved PepsiCo Data Science email fallback.
    hint, resolution = metadata_resolution()
    raw = ("Title: Email Row: Example Canada: Data Science Winter 2027 Co-op\n"
           "Company: Example\nExample Canada: Data Science Winter 2027 Co-op\n"
           "Email Row: Example Canada: Data Science Winter 2027 Co-op - Example\n")
    assert not posting_has_description(replace(resolution.posting, raw_text=raw), hint)
