import pytest

from jobcockpit.services import workflows as api_main
from jobcockpit.core.config import Settings
from jobcockpit.db.models import (
    JobPosting,
)
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_links import (
    PostingLinkClassification,
    PostingLinkProcessingResult,
)


def test_resolved_company_posting_url_replaces_aggregator_apply_url():
    parsed = parse_job_text(
        "Software Developer Co-Op - Fall 2026\nCompany: TrendAI\nPython Docker",
        source_url="https://trendai.example/careers/software-developer-coop-fall-2026",
    )
    parsed.application.external_apply_url = "https://www.linkedin.com/jobs/view/4425694183"

    api_main.prefer_resolved_non_aggregator_application_url(
        parsed,
        "https://trendai.example/careers/software-developer-coop-fall-2026",
    )

    assert parsed.application.external_apply_url == (
        "https://trendai.example/careers/software-developer-coop-fall-2026"
    )


def test_company_from_source_evidence_strips_linkedin_prefix_and_inline_fields():
    raw_text = (
        "Source Evidence: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | "
        "LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada "
        "| LinkedIn Company: LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - Fall 2026 "
        "in Ottawa, Ontario, Canada Location: Ottawa, Ontario, Canada"
    )

    assert api_main.company_from_source_evidence(raw_text) == "TrendAI"


def test_source_evidence_company_uses_linkedin_hiring_phrase_when_inline_company_is_weak():
    raw_text = (
        "Source Evidence: Intern, Digital Ship NLP (8 Months) Seaspan "
        "Title: Intern, Digital Ship NLP (8 Months) Company: Intern, Location: North Vancouver, "
        "British Columbia, Canada Page Title: Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn "
        "Source Evidence: Seaspan ULC hiring Intern, Digital Ship NLP (8 Months) in North Vancouver, "
        "British Columbia, Canada | LinkedIn Seaspan Title: Intern, Digital Ship NLP (8 Months) "
        "Company: Intern, Location: North Vancouver, British Columbia, Canada"
    )

    assert api_main.company_from_source_evidence(raw_text) == "Seaspan ULC"


def test_reprocess_hint_prefers_linkedin_source_evidence_for_rendered_snapshot():
    raw_text = (
        "Page Title: Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425912315/\n"
        "Source Evidence: Intern, Digital Ship NLP (8 Months) Seaspan "
        "Title: Intern, Digital Ship NLP (8 Months) Company: Intern, Location: North Vancouver, "
        "British Columbia, Canada Page Title: Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn "
        "Source URL: https://www.linkedin.com/jobs/view/4425912315/ "
        "Source Evidence: Seaspan ULC hiring Intern, Digital Ship NLP (8 Months) in North Vancouver, "
        "British Columbia, Canada | LinkedIn Seaspan Title: Intern, Digital Ship NLP (8 Months) "
        "Company: Intern, Location: North Vancouver, British Columbia, Canada\n"
        "Seaspan ULC\n"
        "Intern, Digital Ship NLP (8 Months)"
    )
    job = JobPosting(
        id="job_seaspan",
        source_type="gmail_alert",
        source_url="https://www.linkedin.com/jobs/view/4425912315/",
        employer={"company_name": "Intern,"},
        role={"job_title": "Intern, Digital Ship NLP (8 Months) Seaspan"},
        location={
            "city": "North Vancouver",
            "province_state": "British Columbia",
            "country": "Canada",
        },
    )

    hint = api_main.reprocess_hint_text(job, raw_text)

    assert hint.startswith(
        "Title: Intern, Digital Ship NLP (8 Months)\n"
        "Company: Seaspan ULC\n"
        "Location: North Vancouver, British Columbia, Canada\n"
    )


@pytest.mark.parametrize(
    ("company", "title"),
    [
        ("Intern, Digital Ship NLP (8 Months) Seaspan", "Intern, Digital Ship NLP (8 Months)"),
        ("Software Developer Co-Op - Fall 2026 TrendAI", "Software Developer Co-Op - Fall 2026"),
        ("Title: Backend Developer Company: Acme", "Backend Developer"),
        ("Intern", "Intern, Digital Ship NLP (8 Months)"),
        ("Software", "Software Developer Co-Op - Fall 2026"),
    ],
)
def test_weak_reprocess_company_rejects_title_fragments_and_role_words(company: str, title: str):
    assert api_main._is_weak_reprocess_company(company, title) is True


@pytest.mark.parametrize(
    ("company", "title"),
    [
        ("Seaspan ULC", "Intern, Digital Ship NLP (8 Months)"),
        ("TrendAI", "Software Developer Co-Op - Fall 2026"),
        ("Intern Queen", "Marketing Intern"),
    ],
)
def test_weak_reprocess_company_allows_real_companies_with_role_words(company: str, title: str):
    assert api_main._is_weak_reprocess_company(company, title) is False


def test_reprocess_hint_falls_back_to_stored_company_when_source_company_is_title_fragment():
    raw_text = (
        "Page Title: Intern, Digital Ship NLP (8 Months) | Seaspan ULC | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425912315/\n"
        "Source Evidence: Title: Intern, Digital Ship NLP (8 Months) "
        "Company: Intern, Digital Ship NLP (8 Months) Seaspan "
        "Location: North Vancouver, British Columbia, Canada"
    )
    job = JobPosting(
        id="job_seaspan",
        source_type="gmail_alert",
        source_url="https://www.linkedin.com/jobs/view/4425912315/",
        employer={"company_name": "Seaspan ULC"},
        role={"job_title": "Intern, Digital Ship NLP (8 Months)"},
        location={
            "city": "North Vancouver",
            "province_state": "British Columbia",
            "country": "Canada",
        },
    )

    hint = api_main.reprocess_hint_text(job, raw_text)

    assert hint.startswith("Title: Intern, Digital Ship NLP (8 Months)\nCompany: Seaspan ULC\n")


def test_reprocess_hint_prefers_clean_snapshot_identity_over_stale_db_fields():
    raw_text = (
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn\n"
        "Source URL: https://www.linkedin.com/jobs/view/4425694183\n"
        "Source Evidence: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | "
        "LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada "
        "| LinkedIn Company: LinkedIn TrendAI Title: TrendAI hiring Software Developer Co-Op - Fall 2026 "
        "in Ottawa, Ontario, Canada Location: Ottawa, Ontario, Canada TrendAI hiring Software Developer "
        "Co-Op - Fall 2026 in Ottawa\n"
        "Software Developer Co-Op - Fall 2026\n"
        "TrendAI  Ottawa, Ontario, Canada\n"
        "Python Docker Java"
    )
    job = JobPosting(
        id="job_trendai",
        source_type="gmail_alert",
        source_url="https://www.linkedin.com/jobs/view/4425694183",
        employer={"company_name": "LinkedIn TrendAI"},
        role={
            "job_title": "TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn"
        },
        location={
            "city": "Ottawa",
            "province_state": "Ontario",
            "country": "Canada TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa",
        },
    )

    hint = api_main.reprocess_hint_text(job, raw_text)

    assert hint.startswith(
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
    )


def test_reprocess_hint_uses_source_evidence_identity_for_linkedin_checkpoint_snapshot():
    raw_text = (
        "Page Title: LinkedIn App Challenge\n"
        "Source URL: https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc\n"
        "Source Evidence: Software Developer Co-Op - Fall 2026 TrendAI "
        "Title: Software Developer Co-Op - Fall 2026 Company: TrendAI Location: Ottawa, Ontario, Canada\n"
        "Check your LinkedIn app\n"
        "Verify using authenticator app\n"
        "Location: Ottawa, Ontario, Canada"
    )
    job = JobPosting(
        id="job_checkpoint",
        source_type="url",
        source_url="https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc",
        employer={"company_name": "Software"},
        role={"job_title": "Software Developer Co-Op - Fall 2026 TrendAI"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )

    hint = api_main.reprocess_hint_text(job, raw_text)

    assert hint.startswith(
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
    )


def test_reprocess_hint_auth_snapshot_prefers_parsed_page_title_over_stale_company():
    raw_text = (
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn\n"
        "Source URL: https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc\n"
        "Check your LinkedIn app\n"
        "Verify using authenticator app\n"
        "Software Developer Co-Op - Fall 2026\n"
        "TrendAI  Ottawa, Ontario, Canada"
    )
    job = JobPosting(
        id="job_checkpoint",
        source_type="url",
        source_url="https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc",
        employer={"company_name": "Software"},
        role={"job_title": "Software Developer Co-Op - Fall 2026 TrendAI"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )

    hint = api_main.reprocess_hint_text(job, raw_text)

    assert hint.startswith(
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
    )


def test_reprocess_source_url_recovers_linkedin_job_url_from_checkpoint_snapshot():
    job = JobPosting(
        id="job_checkpoint",
        source_type="url",
        source_url="https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc",
        posting_snapshot_hash="hash",
        raw_content_artifact_id="artifact_snapshot",
        employer={"company_name": "TrendAI"},
        role={
            "job_title": "Software Developer Co-Op - Fall 2026",
            "description_raw": "Linkedin Source URL: https://www.linkedin.com/jobs/view/4425694183",
        },
    )
    raw_text = (
        "Page Title: LinkedIn App Challenge\n"
        "Source URL: https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc\n"
        "Source Evidence: TrendAI hiring Software Developer Co-Op - Fall 2026\n"
        "Linkedin Source URL: https://www.linkedin.com/jobs/view/4425694183"
    )

    assert (
        api_main.reprocess_source_url(job, raw_text)
        == "https://www.linkedin.com/jobs/view/4425694183"
    )


def test_reprocess_source_url_recovers_linkedin_job_url_from_feed_snapshot():
    job = JobPosting(
        id="job_feed",
        source_type="url",
        source_url="https://www.linkedin.com/feed/",
        employer={"company_name": "Software"},
        role={
            "job_title": "Software Developer Co-Op - Fall 2026 TrendAI",
            "description_raw": "Source URL: https://www.linkedin.com/jobs/view/4425694183",
        },
    )
    raw_text = (
        "Page Title: Feed | LinkedIn\n"
        "Source URL: https://www.linkedin.com/feed/\n"
        "Source Evidence: Software Developer Co-Op - Fall 2026 TrendAI "
        "Title: Software Developer Co-Op - Fall 2026 Company: TrendAI Location: Ottawa, Ontario, Canada "
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn "
        "Source URL: https://www.linkedin.com/jobs/view/4425694183"
    )

    assert (
        api_main.reprocess_source_url(job, raw_text)
        == "https://www.linkedin.com/jobs/view/4425694183"
    )
    assert api_main.reprocess_source_is_auth_polluted(job, raw_text) is True


def test_reprocess_source_url_recovers_linkedin_job_url_from_known_portal_snapshot():
    job = JobPosting(
        id="job_portal",
        source_type="url",
        source_url="https://internhunt.eu/",
        employer={"company_name": "InternHunt"},
        role={
            "job_title": "IT, Enterprise and Business Solutions, Co-op Student",
        },
    )
    raw_text = (
        "Company: InternHunt\n"
        "Linkedin Hint Company: Trans Mountain\n"
        "Linkedin Hint Title: IT, Enterprise and Business Solutions, Co-op Student\n"
        "Linkedin Source URL: https://www.linkedin.com/jobs/view/4459465547"
    )

    assert (
        api_main.reprocess_source_url(job, raw_text)
        == "https://www.linkedin.com/jobs/view/4459465547"
    )


def test_reprocess_hint_keeps_stored_identity_for_portal_without_embedded_job_url():
    job = JobPosting(
        id="job_talent_portal",
        source_type="url",
        source_url="https://ca.talent.com/view?id=123",
        employer={"company_name": "FortisBC"},
        role={"job_title": "Software Co-op"},
    )
    raw_text = "Title: Software Co-op\nCompany: Talent.com"

    hint_text = api_main.reprocess_hint_text(job, raw_text)

    assert hint_text.splitlines()[:2] == [
        "Title: Software Co-op",
        "Company: FortisBC",
    ]


def test_reprocess_authwall_redirect_uses_structured_hint_discovery(monkeypatch):
    resolved_hints = []

    class FakeResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "https://example.com/careers/software-developer-coop-fall-2026",
                    "raw_text": (
                        "Title: Software Developer Co-Op - Fall 2026\n"
                        "Company: TrendAI\n"
                        "Location: Ottawa, Ontario, Canada"
                    ),
                    "confidence": 0.95,
                    "evidence": "Matched TrendAI careers page",
                },
            }

    def fail_if_normal_path(*args, **kwargs):
        raise AssertionError("auth-polluted reprocess should use structured discovery")
    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())
    monkeypatch.setattr(api_main, "prepare_posting_link", fail_if_normal_path)
    authwall_url = (
        "https://www.linkedin.com/authwall?sessionRedirect="
        "https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F4425694183"
    )
    job = JobPosting(
        id="job_authwall",
        source_type="url",
        source_url=authwall_url,
        employer={"company_name": "Software"},
        role={"job_title": "Software Developer Co-Op - Fall 2026 TrendAI"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )
    raw_text = (
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn\n"
        f"Source URL: {authwall_url}\n"
        "Software Developer Co-Op - Fall 2026\n"
        "TrendAI  Ottawa, Ontario, Canada"
    )

    result = api_main.prepare_reprocess_source(
        job,
        raw_text,
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.ingestion_status == "imported"
    assert resolved_hints[0].source_url == "https://www.linkedin.com/jobs/view/4425694183"
    assert resolved_hints[0].company == "TrendAI"
    assert resolved_hints[0].title == "Software Developer Co-Op - Fall 2026"
    assert (
        result.ingest_request.source_url
        == "https://example.com/careers/software-developer-coop-fall-2026"
    )
    assert result.ingest_request.source_resolution == {
        "kind": "official_discovery",
        "path": "discovered_ats",
        "official_discovery_status": "resolved",
        "reason_code": "official_posting_resolved",
        "official_url": "https://example.com/careers/software-developer-coop-fall-2026",
    }


def test_reprocess_linkedin_feed_with_embedded_job_url_uses_structured_discovery(monkeypatch):
    resolved_hints = []

    class FakeResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "https://example.com/careers/software-developer-coop-fall-2026",
                    "raw_text": (
                        "Title: Software Developer Co-Op - Fall 2026\n"
                        "Company: TrendAI\n"
                        "Location: Ottawa, Ontario, Canada"
                    ),
                    "confidence": 0.95,
                    "evidence": "Matched TrendAI careers page",
                },
            }

    def fail_if_normal_path(*args, **kwargs):
        raise AssertionError("recoverable LinkedIn feed snapshot should use structured discovery")
    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())
    monkeypatch.setattr(api_main, "prepare_posting_link", fail_if_normal_path)
    job = JobPosting(
        id="job_feed",
        source_type="url",
        source_url="https://www.linkedin.com/feed/",
        employer={"company_name": "Software"},
        role={"job_title": "Software Developer Co-Op - Fall 2026 TrendAI"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )
    raw_text = (
        "Page Title: Feed | LinkedIn\n"
        "Source URL: https://www.linkedin.com/feed/\n"
        "Source Evidence: Software Developer Co-Op - Fall 2026 TrendAI "
        "Title: Software Developer Co-Op - Fall 2026 Company: TrendAI Location: Ottawa, Ontario, Canada "
        "Page Title: TrendAI hiring Software Developer Co-Op - Fall 2026 in Ottawa, Ontario, Canada | LinkedIn "
        "Source URL: https://www.linkedin.com/jobs/view/4425694183"
    )

    result = api_main.prepare_reprocess_source(
        job,
        raw_text,
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.ingestion_status == "imported"
    assert resolved_hints[0].source_url == "https://www.linkedin.com/jobs/view/4425694183"
    assert resolved_hints[0].company == "TrendAI"
    assert resolved_hints[0].title == "Software Developer Co-Op - Fall 2026"
    assert (
        result.ingest_request.source_url
        == "https://example.com/careers/software-developer-coop-fall-2026"
    )


def test_reprocess_generic_sign_in_text_does_not_use_linkedin_structured_discovery(monkeypatch):
    normal_path_calls = []

    def fake_process_posting_link(context, **kwargs):
        normal_path_calls.append(context)
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="probable_job_posting",
                confidence=0.9,
                reason="unit_test",
            ),
            ingestion_status="needs_external_resolution",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message="unit_test_normal_path",
        )

    def fail_if_structured_path(*args, **kwargs):
        raise AssertionError("non-LinkedIn source should not use LinkedIn structured discovery")
    monkeypatch.setattr(api_main, "prepare_posting_link", fake_process_posting_link)
    monkeypatch.setattr(
        api_main, "prepare_auth_polluted_reprocess_source", fail_if_structured_path
    )
    source_url = "https://jobs.example.com/software-developer-coop"
    job = JobPosting(
        id="job_company_direct",
        source_type="url",
        source_url=source_url,
        employer={"company_name": "TrendAI"},
        role={"job_title": "Software Developer Co-Op - Fall 2026"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )
    raw_text = (
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "Sign in to continue"
    )

    result = api_main.prepare_reprocess_source(
        job,
        raw_text,
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.error_message == "unit_test_normal_path"
    assert normal_path_calls[0].source_url == source_url


def test_reprocess_linkedin_suffix_lookalike_does_not_use_structured_discovery(monkeypatch):
    normal_path_calls = []

    def fake_process_posting_link(context, **kwargs):
        normal_path_calls.append(context)
        return PostingLinkProcessingResult(
            classification=PostingLinkClassification(
                classification="probable_job_posting",
                confidence=0.9,
                reason="unit_test",
            ),
            ingestion_status="needs_external_resolution",
            original_url=context.source_url,
            resolved_url=context.source_url,
            canonical_url=context.source_url,
            error_message="unit_test_normal_path",
        )

    def fail_if_structured_path(*args, **kwargs):
        raise AssertionError("lookalike host should not use LinkedIn structured discovery")
    monkeypatch.setattr(api_main, "prepare_posting_link", fake_process_posting_link)
    monkeypatch.setattr(
        api_main, "prepare_auth_polluted_reprocess_source", fail_if_structured_path
    )
    source_url = (
        "https://notlinkedin.com/authwall?sessionRedirect=https%3A%2F%2Fjobs.example.com%2Frole"
    )
    job = JobPosting(
        id="job_lookalike",
        source_type="url",
        source_url=source_url,
        employer={"company_name": "TrendAI"},
        role={"job_title": "Software Developer Co-Op - Fall 2026"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )
    raw_text = (
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "Sign in to continue"
    )

    result = api_main.prepare_reprocess_source(
        job,
        raw_text,
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.error_message == "unit_test_normal_path"
    assert normal_path_calls[0].source_url == "https://jobs.example.com/role"


def test_reprocess_auth_polluted_source_uses_structured_hint_discovery(monkeypatch):
    resolved_hints = []

    class FakeResolver:
        def resolve(self, hint):
            resolved_hints.append(hint)
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "https://example.com/careers/software-developer-coop-fall-2026",
                    "raw_text": (
                        "Title: Software Developer Co-Op - Fall 2026\n"
                        "Company: TrendAI\n"
                        "Location: Ottawa, Ontario, Canada"
                    ),
                    "confidence": 0.95,
                    "evidence": "Matched TrendAI careers page",
                },
            }

    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())
    source_url = "https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc"
    job = JobPosting(
        id="job_checkpoint",
        source_type="url",
        source_url=source_url,
        employer={"company_name": "Software"},
        role={"job_title": "Software Developer Co-Op - Fall 2026 TrendAI"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )
    hint_text = (
        "Title: Software Developer Co-Op - Fall 2026\n"
        "Company: TrendAI\n"
        "Location: Ottawa, Ontario, Canada\n"
        "Page Title: LinkedIn App Challenge"
    )

    result = api_main.prepare_auth_polluted_reprocess_source(
        job,
        source_url,
        hint_text,
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.ingestion_status == "imported"
    assert result.error_message == ""
    assert resolved_hints[0].source_url == source_url
    assert resolved_hints[0].company == "TrendAI"
    assert resolved_hints[0].title == "Software Developer Co-Op - Fall 2026"
    assert (
        result.ingest_request.source_url
        == "https://example.com/careers/software-developer-coop-fall-2026"
    )


def test_reprocess_auth_polluted_source_reports_aggregator_fallback_metadata(monkeypatch):
    linkedin_url = "https://www.linkedin.com/jobs/view/4425912315"

    class FakeResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": linkedin_url,
                    "raw_text": (
                        "Title: Software Developer Co-Op - Fall 2026\n"
                        "Company: TrendAI\n"
                        "Location: Ottawa, Ontario, Canada"
                    ),
                    "confidence": 0.95,
                    "evidence": "Rendered LinkedIn fallback matched title and company",
                },
                "error": "codex_cli_failed: private provider output",
            }

    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())
    job = JobPosting(
        id="job_linkedin_fallback",
        source_type="url",
        source_url=linkedin_url,
        employer={"company_name": "TrendAI"},
        role={"job_title": "Software Developer Co-Op - Fall 2026"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )

    result = api_main.prepare_auth_polluted_reprocess_source(
        job,
        linkedin_url,
        "Title: Software Developer Co-Op - Fall 2026\nCompany: TrendAI\nLocation: Ottawa, Ontario, Canada",
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.ingestion_status == "fallback_imported"
    assert result.error_message == "codex_cli_failed"
    assert result.official_discovery_outcome is not None
    assert result.official_discovery_outcome.status == "failed"
    assert result.official_discovery_outcome.reason_code == "codex_cli_failed"
    assert result.source_resolution == {
        "kind": "aggregator_fallback",
        "path": "aggregator_structured_fallback",
        "official_discovery_status": "failed",
        "reason_code": "codex_cli_failed",
        "official_discovery_error": "codex_cli_failed",
        "fallback_url": linkedin_url,
    }
    assert "Official Discovery Status: failed" in result.ingest_request.raw_text
    assert "Official Discovery Error: codex_cli_failed" in result.ingest_request.raw_text


def test_reprocess_auth_polluted_source_blocks_unsafe_discovery_url(monkeypatch):

    class FakeResolver:
        def resolve(self, hint):
            return {
                "status": "resolved",
                "posting": {
                    "source_url": "http://127.0.0.1:9/private",
                    "raw_text": "Title: Software Developer Co-Op - Fall 2026\nCompany: TrendAI",
                    "confidence": 0.95,
                    "evidence": "Unsafe local URL should be blocked",
                },
            }

    monkeypatch.setattr(api_main, "default_posting_resolver", lambda settings: FakeResolver())
    source_url = "https://www.linkedin.com/checkpoint/challengesV2/AQE123?ut=abc"
    job = JobPosting(
        id="job_checkpoint",
        source_type="url",
        source_url=source_url,
        employer={"company_name": "TrendAI"},
        role={"job_title": "Software Developer Co-Op - Fall 2026"},
        location={"city": "Ottawa", "province_state": "Ontario", "country": "Canada"},
    )

    result = api_main.prepare_auth_polluted_reprocess_source(
        job,
        source_url,
        "Title: Software Developer Co-Op - Fall 2026\nCompany: TrendAI\nLocation: Ottawa, Ontario, Canada",
        settings=Settings(posting_parser_provider="heuristic", fit_analysis_provider="disabled"),
    )

    assert result.ingestion_status == "blocked_by_policy"
    assert result.error_message == "localhost_not_allowlisted"
    assert result.ingest_request is None
