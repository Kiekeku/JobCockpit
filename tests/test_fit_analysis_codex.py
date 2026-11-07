import json
from types import SimpleNamespace

from jobcockpit.core.config import Settings
from jobcockpit.db.models import EvidenceItem
from jobcockpit.ingestion.fit_analysis import (
    CodexCliFitAnalyzer,
    MAX_ALLOWED_EVIDENCE_ITEMS,
    MAX_EVIDENCE_CLAIM_CHARS,
    MAX_EVIDENCE_SKILLS,
    MAX_EVIDENCE_SKILL_CHARS,
    MAX_PROFILE_SKILL_CHARS,
    MAX_PROFILE_SKILLS,
    MAX_PROFILE_SUMMARY_CHARS,
    analyze_fit_with_settings,
    fit_analysis_prompt,
)
from jobcockpit.ingestion.parser import ParsedFit, parse_job_text
from jobcockpit.ingestion.parser_corpus import load_parser_corpus


def test_fit_context_retains_source_year_and_resolved_inferred_winter_term():
    case = next(case for case in load_parser_corpus().cases
                if case.id == "winter_availability_corrects_stale_year_from_posting_date")
    parsed = parse_job_text(case.raw_text, case.source_url, source_type=case.source_type)
    prompt = fit_analysis_prompt(
        parsed, raw_text=case.raw_text, source_url=case.source_url,
        profile_data={}, evidence_items=[], max_input_chars=4000,
    )
    context = json.loads(prompt.split("Context JSON:\n", 1)[1])
    assert context["job"]["employment_timing"]["start_date"] == "2027-01-01"
    assert "employment_timing.start_date" in context["job"]["data_quality"]["inferred_fields"]
    assert "January 2026" in context["posting_text"]
    assert "An inferred year is not an employer-confirmed date" in prompt


def test_fit_analysis_prompt_includes_profile_job_and_allowed_evidence_without_contact_details():
    parsed = parse_job_text(
        "Backend Engineering Intern\nCompany: Globex\nLocation: Toronto, ON, Canada\nPython SQL",
        "https://jobs.example.test/1",
    )
    private_contact = "jane" + "@" + "example.invalid"
    private_phone = "555" + "-0100"
    profile_data = {
        "full_name": "Jane Candidate",
        "em" + "ail": private_contact,
        "pho" + "ne": private_phone,
        "street_address": "123 Private Street",
        "city": "Calgary",
        "province_state": "AB",
        "country": "Canada",
        "current_year": "3rd year",
        "graduation_date": "May 2027",
        "co_op_program": True,
        "willing_to_relocate": True,
        "work_authorized_countries": ["Canada"],
        "requires_sponsorship": False,
        "summary": "Backend student focused on APIs.",
        "skills": ["Python", "SQL", "FastAPI"],
        "linkedin_url": "https://www.linkedin.com/in/jane",
    }
    evidence_items = [
        EvidenceItem(
            evidence_id="evidence_allowed",
            candidate_id="candidate_local",
            source_type="resume_upload",
            claim="Built FastAPI services backed by PostgreSQL.",
            skills=["FastAPI", "SQL"],
            allowed_for_resume=True,
            allowed_for_cover_letter=True,
        ),
        EvidenceItem(
            evidence_id="evidence_disallowed",
            candidate_id="candidate_local",
            source_type="resume_upload",
            claim="Private claim that should not be sent.",
            skills=[],
            allowed_for_resume=False,
            allowed_for_cover_letter=True,
        ),
    ]

    prompt = fit_analysis_prompt(
        parsed,
        raw_text="Backend Engineering Intern posting body",
        source_url="https://jobs.example.test/1",
        profile_data=profile_data,
        evidence_items=evidence_items,
        max_input_chars=4000,
    )

    assert "Backend Engineering Intern" in prompt
    assert "Backend student focused on APIs." in prompt
    assert "3rd year" in prompt
    assert "May 2027" in prompt
    assert "Built FastAPI services backed by PostgreSQL." in prompt
    assert "Private claim that should not be sent." not in prompt
    assert private_contact not in prompt
    assert private_phone not in prompt
    assert "123 Private Street" not in prompt
    assert "fit_score" in prompt
    assert "capabilities" in prompt
    assert "capability_domains" in prompt
    assert "technical_match_score as capability/requirements match" in prompt
    assert "willing_to_relocate is true" in prompt
    assert "do not lower timing_location_score" in prompt
    assert "legal authorization in a country" in prompt
    assert "no-sponsorship requirement" in prompt
    context = json.loads(prompt.split("Context JSON:\n", 1)[1])
    profile = context["candidate_profile"]
    assert profile["willing_to_relocate"] is True
    assert profile["work_authorized_countries"] == ["Canada"]
    assert profile["requires_sponsorship"] is False


def test_codex_fit_analyzer_returns_valid_fit_analysis_and_passes_candidate_context():
    calls = []
    parsed = parse_job_text(
        "Backend Engineering Intern\nCompany: Globex\nLocation: Toronto, ON, Canada\nPython SQL",
        "https://jobs.example.test/1",
    )

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "fit_score": 84,
                    "technical_match_score": 88,
                    "eligibility_score": 90,
                    "interest_score": 75,
                    "compensation_score": None,
                    "timing_location_score": 80,
                    "application_effort_score": 70,
                    "risk_flags": ["Cover letter required"],
                    "missing_requirements": ["GraphQL"],
                    "ranking_reason": ["Python and SQL match profile evidence"],
                    "tailoring_strategy": "Emphasize API and database project evidence.",
                }
            ),
            stderr="",
        )

    analyzer = CodexCliFitAnalyzer(command="codex", runner=fake_runner, timeout_seconds=45)
    fit = analyzer.analyze(
        parsed,
        raw_text="Backend Engineering Intern posting body",
        source_url="https://jobs.example.test/1",
        profile_data={"summary": "API-focused student", "skills": ["Python", "SQL"]},
        evidence_items=[
            EvidenceItem(
                evidence_id="evidence_1",
                candidate_id="candidate_local",
                source_type="resume_upload",
                claim="Built SQL-backed Python APIs.",
                skills=["Python", "SQL"],
                allowed_for_resume=True,
                allowed_for_cover_letter=True,
            )
        ],
    )

    assert fit == ParsedFit(
        fit_score=84,
        technical_match_score=88,
        eligibility_score=90,
        interest_score=75,
        compensation_score=None,
        timing_location_score=80,
        application_effort_score=70,
        risk_flags=["Cover letter required"],
        missing_requirements=["GraphQL"],
        ranking_reason=["Python and SQL match profile evidence"],
        tailoring_strategy="Emphasize API and database project evidence.",
    )
    args, kwargs = calls[0]
    assert args[:4] == ["codex", "-a", "never", "exec"]
    assert kwargs["timeout"] == 45
    assert "API-focused student" in kwargs["input"]
    assert "Built SQL-backed Python APIs." in kwargs["input"]


def test_fit_analysis_prompt_caps_allowed_evidence_payload():
    parsed = parse_job_text("Backend Developer\nCompany: Acme\nPython")
    evidence_items = [
        EvidenceItem(
            evidence_id="evidence_disallowed_first",
            candidate_id="candidate_local",
            source_type="resume_upload",
            claim="Disallowed claim should not count toward cap.",
            skills=["PrivateSkill"],
            allowed_for_resume=False,
            allowed_for_cover_letter=True,
        )
    ]
    for index in range(MAX_ALLOWED_EVIDENCE_ITEMS + 3):
        evidence_items.append(
            EvidenceItem(
                evidence_id=f"evidence_allowed_{index}",
                candidate_id="candidate_local",
                source_type="resume_upload",
                claim=f"Allowed evidence {index} " + ("x" * (MAX_EVIDENCE_CLAIM_CHARS + 25)),
                skills=[
                    f"Skill{skill_index}-" + ("y" * (MAX_EVIDENCE_SKILL_CHARS + 25))
                    for skill_index in range(MAX_EVIDENCE_SKILLS + 2)
                ],
                allowed_for_resume=True,
                allowed_for_cover_letter=True,
            )
        )

    prompt = fit_analysis_prompt(
        parsed,
        raw_text="Backend Developer posting body",
        source_url=None,
        profile_data={},
        evidence_items=evidence_items,
        max_input_chars=4000,
    )
    context = json.loads(prompt.split("Context JSON:\n", 1)[1])
    allowed_evidence = context["allowed_evidence"]

    assert len(allowed_evidence) == MAX_ALLOWED_EVIDENCE_ITEMS
    assert "Disallowed claim should not count toward cap." not in prompt
    assert "Allowed evidence 0" in allowed_evidence[0]["claim"]
    assert "Allowed evidence 19" in allowed_evidence[-1]["claim"]
    assert "Allowed evidence 20" not in prompt
    assert len(allowed_evidence[0]["claim"]) == MAX_EVIDENCE_CLAIM_CHARS
    assert len(allowed_evidence[0]["skills"]) == MAX_EVIDENCE_SKILLS
    assert all(len(skill) <= MAX_EVIDENCE_SKILL_CHARS for skill in allowed_evidence[0]["skills"])


def test_fit_analysis_prompt_caps_candidate_profile_payload():
    parsed = parse_job_text("Backend Developer\nCompany: Acme\nPython")
    prompt = fit_analysis_prompt(
        parsed,
        raw_text="Backend Developer posting body",
        source_url=None,
        profile_data={
            "summary": "s" * (MAX_PROFILE_SUMMARY_CHARS + 25),
            "skills": [
                f"ProfileSkill{skill_index}-" + ("z" * (MAX_PROFILE_SKILL_CHARS + 25))
                for skill_index in range(MAX_PROFILE_SKILLS + 2)
            ],
            "city": "Calgary",
        },
        evidence_items=[],
        max_input_chars=4000,
    )
    context = json.loads(prompt.split("Context JSON:\n", 1)[1])
    profile = context["candidate_profile"]

    assert len(profile["summary"]) == MAX_PROFILE_SUMMARY_CHARS
    assert len(profile["skills"]) == MAX_PROFILE_SKILLS
    assert all(len(skill) <= MAX_PROFILE_SKILL_CHARS for skill in profile["skills"])
    assert "ProfileSkill30" not in prompt
    assert profile["city"] == "Calgary"
    assert "work_authorized_countries" not in profile
    assert "requires_sponsorship" not in profile


def test_analyze_fit_with_settings_falls_back_to_existing_fit_when_disabled():
    parsed = parse_job_text("Backend Developer\nCompany: Acme\nPython")
    parsed.fit_analysis = ParsedFit(fit_score=55, ranking_reason=["Existing parser score"])

    fit = analyze_fit_with_settings(
        parsed,
        raw_text="Backend Developer\nCompany: Acme\nPython",
        source_url=None,
        settings=Settings(fit_analysis_provider="disabled"),
        profile_data={"skills": ["Python"]},
        evidence_items=[],
    )

    assert fit.fit_score == 55
    assert fit.ranking_reason == ["Existing parser score"]


def test_analyze_fit_with_settings_falls_back_to_existing_fit_when_analyzer_raises():
    class RaisingAnalyzer:
        def analyze(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    parsed = parse_job_text("Backend Developer\nCompany: Acme\nPython")
    parsed.fit_analysis = ParsedFit(fit_score=61, ranking_reason=["Existing parser score"])

    fit = analyze_fit_with_settings(
        parsed,
        raw_text="Backend Developer\nCompany: Acme\nPython",
        source_url=None,
        settings=Settings(fit_analysis_provider="codex_cli"),
        profile_data={"skills": ["Python"]},
        evidence_items=[],
        analyzer=RaisingAnalyzer(),
    )

    assert fit.fit_score == 61
    assert fit.ranking_reason == ["Existing parser score"]
