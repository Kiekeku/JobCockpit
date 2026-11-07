import pytest

from jobcockpit.core.config import Settings
from jobcockpit.ingestion.parser import parse_job_text, parse_job_text_with_settings
from jobcockpit.ingestion.parser_models import (
    CapabilityMention,
    RequirementExpression,
    RoleClaim,
    RoleEvidence,
)
from jobcockpit.ingestion.role_semantic_projection import (
    project_validated_role_evidence,
)
from jobcockpit.ingestion.role_semantics import parse_role_document


def test_role_semantics_preserves_sections_and_modality_with_source_evidence() -> None:
    raw_text = (
        "Title: Pricing Data Intern\n"
        "What you'll do\n"
        "- Build and maintain Python data pipelines.\n"
        "What we're looking for\n"
        "- Strong Python skills.\n"
        "- Familiarity with Power BI is an asset.\n"
        "About Us\n"
        "Our research organization advances machine learning."
    )

    parsed = parse_job_text(raw_text)

    assert parsed.role.responsibilities == ["Build and maintain Python data pipelines."]
    assert parsed.role.must_have_requirements == ["Strong Python skills."]
    assert parsed.role.nice_to_have_requirements == [
        "Familiarity with Power BI is an asset."
    ]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["Power BI"]
    assert parsed.role.responsibility_capabilities == ["Python", "Data Pipelines"]
    assert parsed.role.capabilities == ["Python", "Power BI", "Data Pipelines"]
    assert "Machine Learning" not in parsed.role.capabilities

    assert [item.source_section for item in parsed.role.evidence] == [
        "responsibilities",
        "qualifications",
        "qualifications",
        "company",
    ]
    assert [item.modality for item in parsed.role.evidence] == [
        "descriptive",
        "required",
        "preferred",
        "non_requirement",
    ]
    for item in parsed.role.evidence:
        assert item.evidence_start is not None
        assert item.evidence_end is not None
        assert (
            parsed.role.description_clean[item.evidence_start : item.evidence_end]
            == item.evidence_text
        )


def test_company_section_applicant_language_overrides_context_default() -> None:
    parsed = parse_job_text(
        "Title: Data Analytics Co-op\n"
        "About Us\n"
        "Applicants must be enrolled in an eligible post-secondary co-op program.\n"
        "Power BI or Tableau experience and familiarity with SQL or Python are strong assets."
    )

    assert parsed.role.must_have_requirements == [
        "Applicants must be enrolled in an eligible post-secondary co-op program."
    ]
    assert parsed.role.nice_to_have_requirements == [
        "Power BI or Tableau experience and familiarity with SQL or Python are strong assets."
    ]
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == ["Python", "SQL", "Tableau", "Power BI"]


def test_unsectioned_posting_keeps_legacy_capability_fallback() -> None:
    parsed = parse_job_text("Backend Engineer\nCompany: ExampleCo\nPython SQL FastAPI")

    assert parsed.role.evidence == []
    assert parsed.role.capabilities[:3] == ["Python", "SQL", "FastAPI"]
    assert parsed.role.tech_stack == ["Python", "SQL", "FastAPI"]


@pytest.mark.parametrize(
    ("title", "technology"),
    [
        ("React Developer", "React"),
        ("React Native iOS Developer", "React"),
        ("React UI Developer", "React"),
        ("React UX Developer", "React"),
        ("Go Engineer", "Go"),
        ("R Programmer", "R"),
        ("Rust Embedded Engineer", "Rust"),
        ("Backend Engineer (Go)", "Go"),
        ("Software Engineer - Rust", "Rust"),
        ("Senior Software Engineer, React", "React"),
        ("AWS DevOps Engineer", "AWS"),
        ("Python Data Analyst", "Python"),
        ("SQL Database Administrator", "SQL"),
        ("Kubernetes Administrator", "Kubernetes"),
    ],
)
def test_explicit_technology_job_titles_survive_section_projection(
    title: str,
    technology: str,
) -> None:
    parsed = parse_job_text(
        f"Title: {title}\nQualifications: Three years of web experience required."
    )

    assert technology in parsed.role.capabilities
    assert technology in parsed.role.tech_stack


def test_explicit_technology_title_only_posting_is_detected() -> None:
    parsed = parse_job_text("Title: Senior React Developer")

    assert parsed.role.capabilities == ["React"]
    assert parsed.role.tech_stack == ["React"]


def test_title_grammar_rejects_non_technical_ontology_lexeme() -> None:
    parsed = parse_job_text(
        "Title: Airflow Technician\n"
        "Qualifications: HVAC certification is required."
    )

    assert "Airflow" not in parsed.role.capabilities
    assert "Airflow" not in parsed.role.tech_stack
    assert "data" not in parsed.role.capability_domains


def test_nominal_capability_list_survives_a_later_role_section() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Python SQL FastAPI\n"
        "Responsibilities\n"
        "Build services."
    )

    assert parsed.role.tech_stack == ["Python", "SQL", "FastAPI"]
    assert parsed.role.capabilities[:3] == ["Python", "SQL", "FastAPI"]


def test_numbered_items_are_recognized_only_at_structural_boundaries() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications\n"
        "1. Experience with Python 2.\n"
        "2. SQL is preferred."
    )

    assert parsed.role.must_have_requirements == ["Experience with Python 2."]
    assert parsed.role.nice_to_have_requirements == ["SQL is preferred."]


def test_flattened_bare_heading_grammar_preserves_role_sections() -> None:
    parsed = parse_job_text(
        "Title: Junior Software Engineer\n"
        "Responsibilities and Duties Build reliable services. "
        "Core Skills Experience developing web frontend features in Vue 3. "
        "Preferred Experience developing highly available services. "
        "Supervision This position has no direct reports."
    )

    assert parsed.role.responsibilities == ["Build reliable services."]
    assert parsed.role.must_have_requirements == [
        "Experience developing web frontend features in Vue 3."
    ]
    assert parsed.role.nice_to_have_requirements == [
        "Experience developing highly available services."
    ]


@pytest.mark.parametrize(
    ("raw_text", "field_name", "expected"),
    [
        (
            "Title: Test Role\nQualifications: • Python required Benefits: Health insurance.",
            "must_have_requirements",
            ["Python required"],
        ),
        (
            "Title: Test Role\nResponsibilities: • Build APIs Compensation: $100,000.",
            "responsibilities",
            ["Build APIs"],
        ),
        (
            "Title: Test Role\nQualifications: • Python required How to apply: Send a resume.",
            "must_have_requirements",
            ["Python required"],
        ),
        (
            "Title: Test Role\nQualifications: • Experience with Python Benefits: Health insurance.",
            "must_have_requirements",
            ["Experience with Python"],
        ),
        (
            "Title: Test Role\nQualifications: • Bachelor degree Compensation: $100,000.",
            "must_have_requirements",
            ["Bachelor degree"],
        ),
        (
            "Title: Test Role\nQualifications: • Communication skills How to apply: Send a resume.",
            "must_have_requirements",
            ["Communication skills"],
        ),
    ],
)
def test_flattened_boundary_heading_closes_completed_bullet_item(
    raw_text: str,
    field_name: str,
    expected: list[str],
) -> None:
    parsed = parse_job_text(raw_text)

    assert getattr(parsed.role, field_name) == expected


def test_heading_grammar_is_invariant_to_supported_document_delimiters() -> None:
    postings = (
        (
            "Title: Backend Engineer\nResponsibilities\nBuild reliable services.\n"
            "Qualifications\nExperience with Python.\nPreferred\nExperience with AWS."
        ),
        (
            "Title: Backend Engineer\nResponsibilities: Build reliable services. "
            "Qualifications: Experience with Python. Preferred: Experience with AWS."
        ),
        (
            "Title: Backend Engineer\nResponsibilities Build reliable services. "
            "Qualifications Experience with Python. Preferred Experience with AWS."
        ),
    )

    semantics = [
        (
            parsed.role.responsibilities,
            parsed.role.must_have_requirements,
            parsed.role.nice_to_have_requirements,
        )
        for parsed in (parse_job_text(posting) for posting in postings)
    ]

    assert semantics == [semantics[0]] * len(postings)


def test_terminal_footer_cannot_reopen_role_parsing_for_another_listing() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications\n"
        "Experience with Python is required.\n"
        "Equal Opportunity Employer\n"
        "We consider all qualified applicants.\n"
        "Title: Sales Associate\n"
        "Responsibilities:\n"
        "Sell unrelated products."
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.responsibilities == []


def test_post_role_boundary_cannot_reopen_at_repeated_posting_identity() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications\n"
        "Experience with Python is required.\n"
        "Benefits\n"
        "Health coverage.\n"
        "Title: Sales Associate\n"
        "Responsibilities:\n"
        "Sell unrelated products."
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.responsibilities == []


def test_inline_posting_identity_cannot_reopen_after_role_boundary() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications: Python experience required. "
        "Benefits: Health coverage. "
        "Position Title: Sales Associate "
        "Responsibilities: Sell unrelated products. "
        "Qualifications: CRM experience required."
    )

    assert parsed.role.must_have_requirements == ["Python experience required."]
    assert parsed.role.responsibilities == []


def test_posting_identity_after_candidate_content_closes_current_document() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications\n"
        "Experience with Python is required.\n"
        "Position Title: Sales Associate\n"
        "Responsibilities: Sell unrelated products.\n"
        "Qualifications: CRM experience required."
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.responsibilities == []


def test_related_listing_collection_is_a_terminal_document_boundary() -> None:
    parsed = parse_job_text(
        "Title: DevOps / DevSecOps Intern\n"
        "Required Qualifications and Skills:\n"
        "Currently pursuing a degree in Computer Science or Software Engineering.\n"
        "Similar jobs\n"
        "Senior DevOps Specialist\n"
        "Cloud Site Reliability Engineer"
    )

    assert parsed.role.must_have_requirements == [
        "Currently pursuing a degree in Computer Science or Software Engineering."
    ]


def test_flattened_related_listing_label_stops_recommendation_titles() -> None:
    parsed = parse_job_text(
        "Title: DevOps / DevSecOps Intern\n"
        "Qualifications\n"
        "Currently pursuing a degree in Computer Science.\n"
        "Sign in to create job alert Similar jobs Senior DevOps Specialist "
        "Cloud Site Reliability Engineer"
    )

    assert all(
        "Senior DevOps Specialist" not in requirement
        and "Cloud Site Reliability Engineer" not in requirement
        for requirement in parsed.role.must_have_requirements
    )


def test_capitalized_terminal_phrase_inside_clause_is_not_a_heading() -> None:
    requirement = (
        "Demonstrate a clear commitment to Equality of Opportunity and willingness "
        "to enhance and develop Equal Opportunity approaches, understanding of "
        "anti-discriminatory practice."
    )

    parsed = parse_job_text(f"Title: Analytics Role\nQualifications\n{requirement}")

    assert parsed.role.must_have_requirements == [requirement]


def test_privacy_role_subheading_preserves_responsibility_state() -> None:
    parsed = parse_job_text(
        "Title: Privacy Lead\n"
        "Key Responsibilities\n"
        "Privacy Governance & Strategy\n"
        "Develop and execute the enterprise-wide Privacy Program and Privacy Governance Framework.\n"
        "Define privacy policies, standards, procedures, and controls aligned with regulatory requirements.\n"
        "Required Qualifications\n"
        "Bachelor's Degree in Engineering, Information Technology, Computer Science, Law, Risk Management, or related discipline."
    )

    assert parsed.role.responsibilities == [
        "Develop and execute the enterprise-wide Privacy Program and Privacy Governance Framework.",
        "Define privacy policies, standards, procedures, and controls aligned with regulatory requirements.",
    ]
    assert parsed.role.must_have_requirements == [
        "Bachelor's Degree in Engineering, Information Technology, Computer Science, Law, Risk Management, or related discipline."
    ]


def test_inline_privacy_role_subheading_uses_clause_grammar() -> None:
    parsed = parse_job_text(
        "Title: Privacy Engineer\n"
        "Qualifications\n"
        "Security experience.\n"
        "Privacy: Experience with GDPR is required.\n"
        "Preferred: SQL experience."
    )

    assert parsed.role.must_have_requirements == [
        "Security experience.",
        "Experience with GDPR is required.",
    ]
    assert parsed.role.nice_to_have_requirements == ["SQL experience."]


def test_inline_privacy_subsection_does_not_depend_on_action_verb() -> None:
    parsed = parse_job_text(
        "Title: Privacy Engineer\n"
        "Responsibilities\n"
        "Privacy: Ensure GDPR compliance across our products.\n"
        "Qualifications\n"
        "CIPP certification required."
    )

    assert parsed.role.responsibilities == [
        "Ensure GDPR compliance across our products."
    ]
    assert parsed.role.must_have_requirements == ["CIPP certification required."]


def test_privacy_domain_subheading_is_not_emitted_as_role_evidence() -> None:
    parsed = parse_job_text(
        "Title: HR Data Steward and Audit Specialist\n"
        "Responsibilities\n"
        "Data Privacy & Compliance\n"
        "Ensure compliance with global and regional data privacy laws, including GDPR."
    )

    assert parsed.role.responsibilities == [
        "Ensure compliance with global and regional data privacy laws, including GDPR."
    ]


def test_privacy_footer_still_terminates_role_parsing() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications\n"
        "Experience with Python is required.\n"
        "Privacy\n"
        "Terms of use\n"
        "Accessibility\n"
        "Experience with unrelated cookies is required."
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]


def test_semicolon_keeps_dependent_qualification_continuation_attached() -> None:
    requirement = (
        "Five years of engineering experience; including two years using Python."
    )

    parsed = parse_job_text(f"Title: Backend Engineer\nQualifications: {requirement}")

    assert parsed.role.must_have_requirements == [requirement]


def test_semicolon_splits_elliptical_item_with_its_own_modality() -> None:
    parsed = parse_job_text(
        "Title: Backend Engineer\n"
        "Qualifications: Python experience required; with SQL experience preferred."
    )

    assert parsed.role.must_have_requirements == ["Python experience required"]
    assert parsed.role.nice_to_have_requirements == ["with SQL experience preferred."]


def test_unsectioned_candidate_material_survives_a_later_company_section() -> None:
    parsed = parse_job_text(
        "Title: Software Developer Intern\n"
        "Basic programming experience in Java, Python, or TypeScript.\n"
        "About Us\n"
        "We invest in new technologies and products."
    )

    assert parsed.role.capabilities == ["Python", "TypeScript", "Java"]
    assert parsed.role.tech_stack == ["Python", "TypeScript", "Java"]


def test_company_owned_modality_words_remain_context() -> None:
    parsed = parse_job_text(
        "Title: Software Developer Intern\n"
        "About Us\n"
        "Inclusion is a must, and we are stronger together.\n"
        "Our preferred data platform is Python."
    )

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == []
    assert "Python" not in parsed.role.capabilities


def test_applicant_future_duty_is_not_a_requirement_in_company_state() -> None:
    parsed = parse_job_text(
        "Title: Software Engineering Intern\n"
        "About Us\n"
        "We build enterprise software.\n"
        "Why Intern With Us\n"
        "You will help shape modern data platforms used by large enterprises."
    )

    assert parsed.role.must_have_requirements == []
    assert parsed.role.required_capabilities == []


def test_descriptive_applicant_predicate_owns_soft_cues_in_its_complement() -> None:
    clauses = (
        "You will join a strong engineering team using Python.",
        "You will work with our preferred Python platform.",
    )

    for clause in clauses:
        parsed = parse_job_text(f"Title: Software Engineering Intern\nAbout Us\n{clause}")

        assert parsed.role.must_have_requirements == []
        assert parsed.role.nice_to_have_requirements == []
        assert parsed.role.required_capabilities == []
        assert parsed.role.preferred_capabilities == []


def test_hard_requirement_predicate_overrides_future_auxiliary() -> None:
    clause = "You will be required to travel."

    parsed = parse_job_text(f"Title: Field Engineering Intern\nAbout Us\n{clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_possession_predicate_requires_a_qualification_complement() -> None:
    benefit = "You have access to professional development sessions and mentorship."
    qualification = "You have three years of Python experience."

    benefit_parsed = parse_job_text(
        f"Title: Software Engineering Intern\nAbout Us\n{benefit}"
    )
    qualification_parsed = parse_job_text(
        f"Title: Software Engineering Intern\nAbout Us\n{qualification}"
    )

    assert benefit_parsed.role.must_have_requirements == []
    assert benefit_parsed.role.required_capabilities == []
    assert qualification_parsed.role.must_have_requirements == [qualification]
    assert qualification_parsed.role.required_capabilities == ["Python"]


def test_inline_section_grammar_is_independent_of_physical_lines() -> None:
    parsed = parse_job_text(
        "Title: Private Equity Summer Intern\n"
        "Who we are: We invest in technology-enabled businesses. "
        "Responsibilities: Assist with financial modelling and valuation analysis. "
        "Qualifications: Demonstrated interest in finance and artificial intelligence. "
        "Location: Toronto."
    )

    assert parsed.role.responsibilities == [
        "Assist with financial modelling and valuation analysis."
    ]
    assert parsed.role.must_have_requirements == [
        "Demonstrated interest in finance and artificial intelligence."
    ]


def test_observed_candidate_and_benefit_headings_transition_parser_state() -> None:
    parsed = parse_job_text(
        "Title: Software Engineer Intern\n"
        "What you'll do\n"
        "Build the product and ship real features.\n"
        "What you'll bring\n"
        "Enough Python or JavaScript/TypeScript to be productive in two weeks.\n"
        "What's in it for you\n"
        "Real ownership from week one."
    )

    assert parsed.role.responsibilities == ["Build the product and ship real features."]
    assert parsed.role.must_have_requirements == [
        "Enough Python or JavaScript/TypeScript to be productive in two weeks."
    ]
    assert parsed.role.nice_to_have_requirements == []


def test_company_description_is_not_unsectioned_candidate_evidence() -> None:
    parsed = parse_job_text(
        "Title: Junior Data Analyst\n"
        "Company Description\n"
        "The company applies computer vision through API-enabled vision intelligence.\n"
        "Role Description\n"
        "Build dashboards and reports with Python.\n"
        "Qualifications\n"
        "Basic SQL experience."
    )

    assert parsed.role.capabilities == ["SQL", "Python", "Dashboarding", "Reporting"]
    assert "Computer Vision" not in parsed.role.capabilities
    assert "API Development" not in parsed.role.capabilities


def test_determined_successful_candidate_is_an_applicant_owner() -> None:
    clause = (
        "The successful candidate must meet the security requirement of the position "
        "and be legally able to work in Canada."
    )

    parsed = parse_job_text(f"Title: Engineering Student\nAbout Us\n{clause}")

    assert parsed.role.must_have_requirements == [
        "The successful candidate must meet the security requirement of the position.",
        "The successful candidate must be legally able to work in Canada.",
    ]
    assert parsed.role.evidence[0].kind == "requirement"
    assert parsed.role.evidence[0].modality == "required"


def test_conditional_required_and_asset_clause_preserves_both_modalities() -> None:
    clause = (
        "Fluency in French is required for candidates applying in Quebec "
        "and an asset for candidates applying in Ottawa."
    )
    parsed = parse_job_text(f"Title: Analyst Intern\nQualifications\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Fluency in French is required for candidates applying in Quebec."
    ]
    assert parsed.role.nice_to_have_requirements == [
        "Fluency in French is an asset for candidates applying in Ottawa."
    ]
    assert parsed.role.evidence[0].evidence_text == clause
    assert [claim.modality for claim in parsed.role.evidence[0].claims] == [
        "mandatory",
        "preferred",
    ]


def test_inline_nice_to_have_heading_is_preferred_not_required() -> None:
    parsed = parse_job_text(
        "Title: Software Engineer\nNice to have, not required: PHP, Laravel, Vue, MySQL."
    )

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == ["PHP, Laravel, Vue, MySQL."]
    assert parsed.role.preferred_capabilities == ["PHP", "Laravel", "Vue", "MySQL"]


def test_provider_role_evidence_is_used_only_when_exactly_grounded() -> None:
    clause = "Experience with Python is considered an asset."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="preferred",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    confidence=0.61,
                    entities=["Python", "Kubernetes"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Data Intern\nQualifications\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.evidence[0].confidence == 0.61
    assert parsed.role.preferred_capabilities == ["Python"]
    assert "Kubernetes" not in parsed.role.capabilities


def test_provider_projection_preserves_title_derived_technology() -> None:
    raw_text = (
        "Title: React Developer\n"
        "Qualifications: Three years of web experience required."
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            return baseline.model_copy(deep=True)

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.capabilities == ["React"]
    assert parsed.role.tech_stack == ["React"]


def test_provider_role_evidence_must_match_deterministic_grammar() -> None:
    raw_text = (
        "Title: Research Intern\n"
        "About Us\n"
        "Our preferred data platform is Python.\n"
        "Qualifications\n"
        "Experience with SQL is required."
    )

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            company_clause = "Our preferred data platform is Python."
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=company_clause,
                    evidence_text=company_clause,
                    entities=["Python"],
                )
            ]
            parsed.role.must_have_requirements = [company_clause]
            parsed.role.required_capabilities = ["Python"]
            parsed.role.capabilities = ["Python"]
            parsed.role.tech_stack = ["Python"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == ["Experience with SQL is required."]
    assert parsed.role.required_capabilities == ["SQL"]
    assert parsed.role.capabilities == ["SQL"]
    assert parsed.role.tech_stack == ["SQL"]


def test_provider_entity_support_uses_tokens_not_substrings() -> None:
    clause = "Maintain production systems."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="responsibility",
                    modality="descriptive",
                    source_section="responsibilities",
                    text=clause,
                    evidence_text=clause,
                    entities=["AI"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Operations Intern\nResponsibilities\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.evidence[0].entities == []
    assert "AI" not in parsed.role.capabilities


def test_provider_entities_must_form_capability_phrases() -> None:
    clause = "Three years of experience with Rust is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Three", "Three years", "Experience", "Rust"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Systems Intern\nQualifications\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.required_capabilities == ["Rust"]
    assert parsed.role.capabilities == ["Rust"]


def test_provider_entities_in_coordinate_phrase_share_requirement_scope() -> None:
    clause = "Rust and Go are required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Rust", "Go"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Systems Intern\nQualifications\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.required_capabilities == ["Rust", "Go"]
    assert parsed.role.capabilities == ["Rust", "Go"]


def test_provider_entity_uses_the_governed_occurrence_when_repeated() -> None:
    clause = "Rust is mentioned, but Rust is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Rust"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Systems Intern\nQualifications\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.required_capabilities == ["Rust"]
    assert parsed.role.evidence[0].entities == ["Rust"]


def test_provider_projection_offsets_reference_the_final_clean_description() -> None:
    clause = "Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.description_clean = "Provider summary."
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Python"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Data Intern\nQualifications\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    evidence = parsed.role.evidence[0]
    assert evidence.evidence_start is not None
    assert evidence.evidence_end is not None
    assert (
        parsed.role.description_clean[evidence.evidence_start : evidence.evidence_end]
        == evidence.evidence_text
    )


def test_provider_subset_enriches_without_discarding_deterministic_evidence() -> None:
    required = "Experience with Python is required."
    preferred = "Experience with SQL is an asset."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=required,
                    evidence_text=required,
                    confidence=0.61,
                    entities=["Python"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Data Intern\nQualifications\n{required}\n{preferred}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == [required]
    assert parsed.role.nice_to_have_requirements == [preferred]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["SQL"]
    assert parsed.role.evidence[0].confidence == 0.61


def test_invalid_provider_evidence_cannot_survive_without_section_evidence() -> None:
    raw_text = "Backend Engineer\nPython SQL FastAPI"

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            fabricated = "Kubernetes is required."
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=fabricated,
                    evidence_text=fabricated,
                    entities=["Kubernetes"],
                )
            ]
            parsed.role.must_have_requirements = [fabricated]
            parsed.role.required_capabilities = ["Kubernetes"]
            parsed.role.capabilities = ["Kubernetes"]
            parsed.role.capability_domains = ["cybersecurity"]
            parsed.role.tech_stack = ["Kubernetes"]
            parsed.role.domain_keywords = ["fabricated"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.evidence == []
    assert parsed.role.must_have_requirements == []
    assert parsed.role.required_capabilities == []
    assert parsed.role.capabilities[:3] == ["Python", "SQL", "FastAPI"]
    assert parsed.role.capability_domains == parse_job_text(raw_text).role.capability_domains
    assert parsed.role.tech_stack == ["Python", "SQL", "FastAPI"]
    assert parsed.role.domain_keywords == []


def test_valid_provider_evidence_cannot_supply_ungrounded_domains() -> None:
    clause = "Experience with Python is required."
    raw_text = f"Title: Data Intern\nQualifications\n{clause}"

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Python"],
                )
            ]
            parsed.role.capability_domains = ["cybersecurity"]
            parsed.role.domain_keywords = ["fabricated"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )
    baseline = parse_job_text(raw_text)

    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.capability_domains == baseline.role.capability_domains
    assert parsed.role.domain_keywords == baseline.role.domain_keywords


def test_provider_clause_semantics_without_evidence_are_rejected() -> None:
    raw_text = "Title: Data Intern\nQualifications\nExperience with Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = []
            parsed.role.must_have_requirements = ["Kubernetes is required."]
            parsed.role.required_capabilities = ["Kubernetes"]
            parsed.role.capabilities = ["Kubernetes"]
            parsed.role.tech_stack = ["Kubernetes"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]


def test_provider_aggregate_capabilities_without_evidence_are_rejected() -> None:
    raw_text = "Title: Data Intern\nQualifications\nExperience with Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = []
            parsed.role.responsibilities = []
            parsed.role.must_have_requirements = []
            parsed.role.nice_to_have_requirements = []
            parsed.role.required_capabilities = []
            parsed.role.preferred_capabilities = []
            parsed.role.responsibility_capabilities = []
            parsed.role.capabilities = ["Kubernetes"]
            parsed.role.capability_domains = ["cybersecurity"]
            parsed.role.tech_stack = ["Kubernetes"]
            parsed.role.domain_keywords = ["fabricated"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.capability_domains == parse_job_text(raw_text).role.capability_domains
    assert parsed.role.tech_stack == ["Python"]
    assert parsed.role.domain_keywords == []


def test_fabricated_provider_role_evidence_falls_back_to_grounded_baseline() -> None:
    raw_text = "Title: Data Intern\nQualifications\nExperience with Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            fabricated = "Experience with Kubernetes is required."
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=fabricated,
                    evidence_text=fabricated,
                    entities=["Kubernetes"],
                )
            ]
            parsed.role.must_have_requirements = [fabricated]
            parsed.role.required_capabilities = ["Kubernetes"]
            parsed.role.capabilities = ["Kubernetes"]
            parsed.role.tech_stack = ["Kubernetes"]
            return parsed

    parsed = parse_job_text_with_settings(
        raw_text,
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == ["Experience with Python is required."]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]


@pytest.mark.parametrize(
    ("raw_text", "required", "preferred"),
    [
        (
            "Title: Test Role\nMinimum Qualifications: Familiarity with Python.",
            ["Familiarity with Python."],
            [],
        ),
        (
            "Title: Test Role\nQualifications: Five plus years of Python experience.",
            ["Five plus years of Python experience."],
            [],
        ),
        (
            "Title: Test Role\nQualifications: Experience in digital assets.",
            ["Experience in digital assets."],
            [],
        ),
        (
            "Title: Test Role\nQualifications: Classify Bitcoin as an asset.",
            ["Classify Bitcoin as an asset."],
            [],
        ),
        (
            "Title: Test Role\nPreferred Qualifications: Familiarity with Python.",
            [],
            ["Familiarity with Python."],
        ),
        (
            "Title: Test Role\nQualifications: Python experience preferred for this role.",
            [],
            ["Python experience preferred for this role."],
        ),
        (
            "Title: Test Role\nQualifications: French an asset.",
            [],
            ["French an asset."],
        ),
        (
            "Title: Test Role\nQualifications: Python a plus.",
            [],
            ["Python a plus."],
        ),
        (
            "Title: Test Role\nQualifications: SQL an advantage.",
            [],
            ["SQL an advantage."],
        ),
    ],
)
def test_modality_uses_scoped_constructions_and_section_priors(
    raw_text: str,
    required: list[str],
    preferred: list[str],
) -> None:
    parsed = parse_job_text(raw_text)

    assert parsed.role.must_have_requirements == required
    assert parsed.role.nice_to_have_requirements == preferred


@pytest.mark.parametrize(
    "clause",
    [
        "Python experience is optional.",
        "No prior Python experience is necessary.",
        "No experience required.",
        "Experience is not necessary.",
        "Python experience is not mandatory.",
        "Candidates do not need Python experience.",
    ],
)
def test_explicit_non_requirement_grammar_overrides_qualification_section(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == []
    assert parsed.role.evidence[0].claims[0].modality == "optional"


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates may apply.",
        "Candidates may use Java.",
        "Candidates are allowed to use Java for the assessment.",
        "Candidates are permitted to use Java for the assessment.",
        "Candidates should be permitted to use Java for the assessment.",
    ],
)
def test_positive_permission_grammar_overrides_qualification_section(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == []
    assert claim.owner == "applicant"
    assert claim.modality == "optional"


@pytest.mark.parametrize(
    "clause",
    [
        "Applicants must be legally permitted to work in Canada.",
        "Applicants must be legally allowed to work in Canada.",
        "Applicants are required to be permitted to work in Canada.",
        "Applicants must ensure they are legally permitted to work in Canada.",
        "Applicants must confirm that they are legally allowed to work in Canada.",
        "Applicants are required to have been permitted to access the system.",
    ],
)
def test_governing_obligation_outscopes_embedded_passive_permission(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []
    assert claim.owner == "applicant"
    assert claim.modality == "mandatory"


@pytest.mark.parametrize(
    "clause",
    [
        "Applicants should be legally permitted to work in Canada.",
        "Applicants should ensure they are legally permitted to work in Canada.",
    ],
)
def test_deontic_should_governs_legal_work_permission(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []
    assert claim.kind == "qualification"
    assert claim.modality == "expected"
    assert claim.owner == "applicant"


def test_matrix_should_governs_embedded_permission_without_legal_work_lexemes() -> None:
    clause = (
        "Applicants should ensure they are permitted to access the production "
        "environment."
    )
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []
    assert claim.modality == "expected"
    assert claim.owner == "applicant"


@pytest.mark.parametrize(
    ("matrix_predicate", "expected_modality"),
    [
        ("must provide", "mandatory"),
        ("are required to provide", "mandatory"),
        ("are expected to provide", "expected"),
    ],
)
def test_matrix_modality_distributes_across_coordinated_predicates(
    matrix_predicate: str,
    expected_modality: str,
) -> None:
    clause = (
        f"Applicants {matrix_predicate} credentials and ensure they are legally "
        "permitted to work in Canada."
    )
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert claims[0].text == f"Applicants {matrix_predicate} credentials"
    assert claims[1].text == "ensure they are legally permitted to work in Canada."
    assert [claim.modality for claim in claims] == [
        expected_modality,
        expected_modality,
    ]
    assert [claim.owner for claim in claims] == ["applicant", "applicant"]


def test_negated_passive_permission_remains_a_mandatory_negative_constraint() -> None:
    clause = "Candidates are not permitted to use Java for the assessment."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.required_capabilities == []
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


@pytest.mark.parametrize(
    "permission_predicate",
    [
        "may use Java.",
        "are permitted to use Java.",
    ],
)
def test_positive_permission_remains_claim_local_after_mandatory_proposition(
    permission_predicate: str,
) -> None:
    clause = f"Candidates must know Python and {permission_predicate}"
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.text for claim in claims] == [
        "Candidates must know Python",
        permission_predicate,
    ]
    assert [claim.modality for claim in claims] == ["mandatory", "optional"]
    assert [claim.owner for claim in claims] == ["applicant", "applicant"]
    assert [[entity.canonical_name for entity in claim.entities] for claim in claims] == [
        ["Python"],
        ["Java"],
    ]
    assert parsed.role.required_capabilities == ["Python"]


def test_mandatory_capability_remains_local_after_positive_permission() -> None:
    clause = "Applicants are permitted to use Java and must know Python."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["optional", "mandatory"]
    assert [[entity.canonical_name for entity in claim.entities] for claim in claims] == [
        ["Java"],
        ["Python"],
    ]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates must be available in May 2027.",
        "May through August availability is required.",
        "May-August availability is required.",
        "May 2027 availability is required.",
        "Candidates may occasionally be required to travel.",
        "Candidates may sometimes have to work weekends.",
    ],
)
def test_calendar_may_and_contingent_obligations_remain_mandatory(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.evidence[0].claims[0].modality == "mandatory"


def test_no_less_than_requirement_is_not_negated_by_no() -> None:
    clause = "No less than three years of experience is required."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.evidence[0].claims[0].modality == "mandatory"


def test_sentence_and_proposition_scope_separate_mixed_modalities() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nQualifications: Python is required. SQL is preferred."
    )

    assert parsed.role.must_have_requirements == ["Python is required."]
    assert parsed.role.nice_to_have_requirements == ["SQL is preferred."]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["SQL"]
    assert [item.claims[0].modality for item in parsed.role.evidence] == [
        "mandatory",
        "preferred",
    ]


def test_dotted_credentials_remain_attached_to_their_modalities() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nQualifications: B.A. required in finance. M.Eng. preferred."
    )

    assert parsed.role.must_have_requirements == ["B.A. required in finance."]
    assert parsed.role.nice_to_have_requirements == ["M.Eng. preferred."]
    assert [item.claims[0].modality for item in parsed.role.evidence] == [
        "mandatory",
        "preferred",
    ]


@pytest.mark.parametrize(
    ("clause", "modality", "polarity"),
    [
        ("You are expected to mentor interns.", "expected", "positive"),
        ("Candidates must not disclose customer data.", "prohibited", "negative"),
    ],
)
def test_responsibility_modality_preserves_expectation_and_prohibition(
    clause: str,
    modality: str,
    polarity: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.kind == "responsibility"
    assert claim.modality == modality
    assert claim.polarity == polarity


def test_comma_scoped_modalities_remain_separate_claims() -> None:
    clause = "Python required, Java preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == ["Python required."]
    assert parsed.role.nice_to_have_requirements == ["Java preferred."]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["Java"]
    assert [claim.modality for claim in parsed.role.evidence[0].claims] == [
        "mandatory",
        "preferred",
    ]


def test_conditional_modalities_become_separate_grounded_claims() -> None:
    clause = "Fluency in French is required in Quebec and preferred in Ottawa."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    evidence = parsed.role.evidence[0]
    assert parsed.role.must_have_requirements == [
        "Fluency in French is required in Quebec."
    ]
    assert parsed.role.nice_to_have_requirements == [
        "Fluency in French is preferred in Ottawa."
    ]
    assert [claim.modality for claim in evidence.claims] == ["mandatory", "preferred"]
    assert [claim.condition_text for claim in evidence.claims] == [
        "in Quebec",
        "in Ottawa",
    ]
    assert [claim.text for claim in evidence.claims] == [
        "Fluency in French is required in Quebec",
        clause,
    ]


def test_alternative_qualification_is_one_coordinated_claim() -> None:
    clause = "Bachelor's degree or equivalent experience."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.coordination == "alternative"
    assert claim.coordination_group is not None


@pytest.mark.parametrize(
    "clause",
    [
        "Our ideal candidate will have Python experience.",
        "The successful candidate will have Python experience.",
        "As the successful candidate, you will need to travel.",
    ],
)
def test_applicant_owner_and_complete_auxiliary_chain_are_resolved(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nAbout Us: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.owner == "applicant"
    assert claim.kind in {"qualification", "eligibility"}


def test_local_mandatory_grammar_overrides_responsibility_section() -> None:
    clause = "Must possess a valid driver's licence."
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "eligibility"
    assert claim.modality == "mandatory"
    assert "role.evidence" in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is True


@pytest.mark.parametrize(
    "clause",
    [
        "Build strong customer relationships.",
        "Deliver desired outcomes for customers.",
        "Provide desired customer outcomes.",
        "Foster strong cross-functional partnerships.",
        "Drive strong execution across teams.",
        "Cultivate strong communication skills across the team.",
        "Apply strong analytical skills to solve customer problems.",
        "Support security clearance processes for new hires.",
        "Manage preferred vendors.",
        "You must cultivate durable customer relationships.",
        "You must build reliable APIs.",
    ],
)
def test_responsibility_predicates_override_incidental_modality_words(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.evidence[0].claims[0].kind == "responsibility"
    assert "role.evidence" not in parsed.data_quality.ambiguous_fields


def test_local_mandatory_grammar_overrides_preferred_section() -> None:
    clause = "Candidates must be authorized to work in Canada."
    parsed = parse_job_text(f"Title: Test Role\nPreferred Qualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []
    assert claim.kind == "eligibility"
    assert claim.modality == "mandatory"
    assert "section_modality_conflict" in parsed.role.evidence[0].quality_flags
    assert parsed.data_quality.needs_manual_review is True


def test_ambiguous_capability_names_require_linguistic_context() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nResponsibilities\n"
        "React quickly to production incidents.\n"
        "Reporting to the VP, analyze weekly metrics."
    )

    assert parsed.role.responsibilities == [
        "React quickly to production incidents.",
        "Reporting to the VP, analyze weekly metrics.",
    ]
    assert "React" not in parsed.role.capabilities
    assert "Reporting" not in parsed.role.capabilities


@pytest.mark.parametrize(
    ("clause", "capability"),
    [
        ("Build React interfaces.", "React"),
        ("Build Go services.", "Go"),
        ("Build Angular applications.", "Angular"),
        ("Build Flask services.", "Flask"),
        ("Deploy Flask services to production.", "Flask"),
        ("Debug Flask services.", "Flask"),
        ("Test Angular components.", "Angular"),
        ("Configure Angular modules.", "Angular"),
        ("Troubleshoot Flask services.", "Flask"),
        ("Build services with Spring Boot.", "Spring"),
    ],
)
def test_ambiguous_technology_names_are_valid_under_capability_governors(
    clause: str,
    capability: str,
) -> None:
    parsed = parse_job_text(f"Title: Engineer\nResponsibilities: {clause}")

    assert parsed.role.capabilities == [capability]
    assert parsed.role.tech_stack == [capability]
    assert parsed.role.capability_domains == ["software"]


def test_go_to_market_idiom_is_not_technology_in_a_capability_section() -> None:
    parsed = parse_job_text(
        "Title: Product Manager\nCapabilities: Go to market strategy."
    )

    assert "Go" not in parsed.role.capabilities
    assert "Go" not in parsed.role.tech_stack


def test_capability_governor_scopes_over_coordinated_technology_list() -> None:
    parsed = parse_job_text(
        "Title: Engineer\n"
        "Qualifications: Experience with Go, React, and R is required."
    )

    assert parsed.role.required_capabilities == ["React", "Go", "R"]
    assert parsed.role.tech_stack == ["React", "Go", "R"]


@pytest.mark.parametrize(
    ("clause", "expected_capabilities"),
    [
        ("Build interfaces using either React or Rust.", ["React", "Rust"]),
        ("Build interfaces using React and/or Rust.", ["React", "Rust"]),
        ("Build data tools using Python & R.", ["Python", "R"]),
        ("Work with both Go and Rust daily.", ["Rust", "Go"]),
    ],
)
def test_capability_governor_scopes_across_coordination_determiners(
    clause: str,
    expected_capabilities: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Engineer\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibility_capabilities == expected_capabilities
    assert parsed.role.tech_stack == expected_capabilities
    assert {entity.canonical_name for entity in claim.entities} == set(expected_capabilities)


def test_punctuation_bearing_technology_does_not_split_coordinate_scope() -> None:
    parsed = parse_job_text(
        "Title: Engineer\n"
        "Qualifications: Experience with .NET and React."
    )

    assert parsed.role.required_capabilities == ["React", ".NET"]
    assert parsed.role.tech_stack == ["React", ".NET"]


@pytest.mark.parametrize(
    ("raw_text", "false_capability"),
    [
        (
            "Title: Marketing Coordinator\n"
            "Responsibilities: Develop go-to-market strategies.",
            "Go",
        ),
        (
            "Title: Product Manager\n"
            "Responsibilities: Build go to market plans.",
            "Go",
        ),
        (
            "Title: Research Associate\n"
            "Qualifications: Experience in R&D is required.",
            "R",
        ),
        (
            "Title: Hardware Technician\n"
            "Responsibilities: Inspect rust on metal components.",
            "Rust",
        ),
        (
            "Title: Project Analyst\n"
            "Responsibilities: Use go/no-go criteria for launch decisions.",
            "Go",
        ),
        (
            "Title: Materials Engineer\n"
            "Responsibilities: Work with rust prevention and corrosion control.",
            "Rust",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Responsibilities: Develop spring mechanisms for assemblies.",
            "Spring",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Qualifications: Experience with spring design is required.",
            "Spring",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Responsibilities: Test spring tension in each assembly.",
            "Spring",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Responsibilities: Measure angular displacement of components.",
            "Angular",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Qualifications: Experience with angular displacement measurement is required.",
            "Angular",
        ),
        (
            "Title: Physics Researcher\n"
            "Qualifications: Knowledge of angular momentum is required.",
            "Angular",
        ),
        (
            "Title: Laboratory Technician\n"
            "Responsibilities: Clean each flask and prepare samples.",
            "Flask",
        ),
        (
            "Title: Laboratory Technician\n"
            "Qualifications: Experience with flask sterilization is required.",
            "Flask",
        ),
        (
            "Title: Laboratory Technician\n"
            "Qualifications: Familiarity with flask cleaning protocols is required.",
            "Flask",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Qualifications: Experience with angular contact bearings is required.",
            "Angular",
        ),
        (
            "Title: Laboratory Technician\n"
            "Qualifications: Experience with flask calibration is required.",
            "Flask",
        ),
        (
            "Title: Mechanical Engineer\n"
            "Qualifications: Experience with spring fatigue testing is required.",
            "Spring",
        ),
        (
            "Title: Mechanical Engineer - Angular Displacement\n"
            "Qualifications: Three years of mechanical design experience required.",
            "Angular",
        ),
    ],
)
def test_technology_lexemes_do_not_match_inside_non_technology_phrases(
    raw_text: str,
    false_capability: str,
) -> None:
    parsed = parse_job_text(raw_text)

    assert false_capability not in parsed.role.capabilities
    assert false_capability not in parsed.role.tech_stack


def test_ambiguous_capability_context_cannot_cross_claim_boundaries() -> None:
    parsed = parse_job_text(
        "Title: Support Specialist\n"
        "Qualifications: Prior sales experience.\n"
        "Responsibilities: React quickly to incidents."
    )

    assert parsed.role.must_have_requirements == ["Prior sales experience."]
    assert parsed.role.responsibilities == ["React quickly to incidents."]
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []
    assert parsed.role.capability_domains == ["customer_support"]


def test_relational_reporting_does_not_create_a_business_domain() -> None:
    parsed = parse_job_text(
        "Title: Coordinator\nResponsibilities: Reporting to the VP, track incidents."
    )

    assert parsed.role.capabilities == []
    assert parsed.role.capability_domains == []


def test_claim_scoped_domain_signal_does_not_require_an_ontology_capability() -> None:
    parsed = parse_job_text(
        "Title: Coordinator\nResponsibilities: Resolve customer support requests."
    )

    assert parsed.role.capabilities == []
    assert parsed.role.capability_domains == ["customer_support"]


def test_inline_heading_requires_a_real_boundary() -> None:
    clause = "Experience with Compensation: payroll systems."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_multiword_inline_heading_requires_a_real_boundary() -> None:
    clause = "Experience with Total Rewards: compensation analysis."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_multiword_inline_heading_is_not_reopened_inside_a_bullet_item() -> None:
    clause = "Experience with Total Rewards: compensation analysis."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: • {clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_multiword_inline_heading_is_not_reopened_after_bullet_modifiers() -> None:
    clause = "Experience with enterprise Total Rewards: compensation analysis."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: • {clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_capability_section_does_not_license_boundary_headings_inside_its_content() -> None:
    parsed = parse_job_text(
        "Title: Test Role\n"
        "Our Tech Stack: Experience with Total Rewards: compensation analysis. "
        "What you'll do: Build APIs."
    )

    assert parsed.role.environment_capabilities == []
    assert parsed.role.evidence[0].text == (
        "Experience with Total Rewards: compensation analysis."
    )
    assert parsed.role.responsibilities == ["Build APIs."]


def test_environment_capabilities_do_not_become_requirements() -> None:
    parsed = parse_job_text("Title: Test Role\nTech Stack: Rust, Go, and AWS.")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.environment_capabilities == ["Rust", "Go", "AWS"]
    assert parsed.role.capabilities == ["Rust", "Go", "AWS"]
    assert parsed.role.evidence[0].claims[0].kind == "environment"


def test_environment_context_does_not_disable_ambiguous_capability_guards() -> None:
    parsed = parse_job_text(
        "Title: Support Engineer\n"
        "Tech Stack: Python.\n"
        "Responsibilities: Go to customer sites and react quickly to incidents."
    )

    assert parsed.role.environment_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]


def test_noun_compound_capability_is_grounded_without_provider_help() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nQualifications: Three years of Rust experience required."
    )

    assert parsed.role.required_capabilities == ["Rust"]


def test_mandatory_negation_preserves_requirement_modality_and_negative_polarity() -> None:
    clause = "Candidates must not require visa sponsorship."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


def test_negated_applicant_constraint_is_not_a_responsibility() -> None:
    clause = "Candidates must not require visa sponsorship."
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


def test_negated_action_predicate_remains_a_responsibility() -> None:
    clause = "Candidates must not disclose customer data."
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []
    assert claim.kind == "responsibility"
    assert claim.polarity == "negative"


def test_punctuation_bearing_capability_names_are_detected() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nQualifications: Experience with C#, C++, and .NET is required."
    )

    assert parsed.role.required_capabilities == ["C#", "C++", ".NET"]


@pytest.mark.parametrize(
    ("clause", "capability"),
    [
        ("Python preferred for this role.", "Python"),
        ("Rust desired when working on systems code.", "Rust"),
        ("C++ advantageous in this team.", "C++"),
        ("Rust desired.", "Rust"),
        ("C++ advantageous.", "C++"),
    ],
)
def test_postfix_preference_cues_allow_condition_tails(
    clause: str,
    capability: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == [clause]
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == [capability]


def test_repeated_capability_mentions_use_claim_local_offsets() -> None:
    raw_text = (
        "Title: Test Role\n"
        "Qualifications: Python is required and Python is preferred."
    )
    parsed = parse_job_text(raw_text)

    first = raw_text.index("Python")
    second = raw_text.index("Python", first + 1)
    claims = parsed.role.evidence[0].claims
    assert [
        (claim.entities[0].source_start, claim.entities[0].source_end)
        for claim in claims
    ] == [
        (first, first + len("Python")),
        (second, second + len("Python")),
    ]


def test_capability_alias_mention_preserves_source_surface_and_offsets() -> None:
    raw_text = (
        "Title: Database Engineer\n"
        "Qualifications: Postgres experience required."
    )
    parsed = parse_job_text(raw_text)

    mention = parsed.role.evidence[0].claims[0].entities[0]
    expected_start = raw_text.index("Postgres")
    assert mention.surface_text == "Postgres"
    assert mention.canonical_name == "PostgreSQL"
    assert mention.source_start == expected_start
    assert mention.source_end == expected_start + len("Postgres")


@pytest.mark.parametrize(
    "clause",
    [
        "Complete other duties as required.",
        "Carry out other duties as required.",
    ],
)
def test_required_duty_adjunct_does_not_create_a_qualification(clause: str) -> None:
    parsed = parse_job_text(f"Title: Associate\nResponsibilities: {clause}")

    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []
    assert parsed.role.evidence[0].claims[0].kind == "responsibility"
    assert parsed.role.evidence[0].claims[0].modality == "descriptive"


def test_grounded_provider_disagreement_is_retained_as_ambiguity() -> None:
    clause = "Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="preferred",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    entities=["Python"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    evidence = parsed.role.evidence[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []
    assert [claim.modality for claim in evidence.alternative_claims] == ["preferred"]
    assert "provider_semantic_disagreement" in evidence.quality_flags
    assert "role.evidence" in parsed.data_quality.ambiguous_fields
    assert parsed.data_quality.needs_manual_review is True


@pytest.mark.parametrize(
    ("clause", "modality", "polarity"),
    [
        ("You are expected to mentor interns.", "expected", "positive"),
        ("Candidates must not disclose customer data.", "prohibited", "negative"),
    ],
)
def test_provider_expected_and_prohibited_claims_match_deterministic_grammar(
    clause: str,
    modality: str,
    polarity: str,
) -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="responsibility",
                    modality="descriptive",
                    source_section="responsibilities",
                    text=clause,
                    evidence_text=clause,
                    claims=[
                        RoleClaim(
                            kind="responsibility",
                            modality=modality,
                            owner="applicant",
                            polarity=polarity,
                            text=clause,
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nResponsibilities: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    evidence = parsed.role.evidence[0]
    assert evidence.alternative_claims == []
    assert "provider_semantic_disagreement" not in evidence.quality_flags
    assert "role.evidence" not in parsed.data_quality.ambiguous_fields


def test_provider_evidence_matches_duplicate_text_within_its_source_section() -> None:
    clause = "Python experience."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    confidence=0.61,
                    entities=["Python"],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        "Title: Test Role\n"
        f"Responsibilities: {clause}\n"
        f"Qualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    responsibility, qualification = parsed.role.evidence
    assert responsibility.source_section == "responsibilities"
    assert responsibility.confidence == 0.92
    assert qualification.source_section == "qualifications"
    assert qualification.confidence == 0.61
    assert qualification.alternative_claims == []
    assert "role.evidence" not in parsed.data_quality.ambiguous_fields


def test_grounded_provider_claim_cannot_rename_a_capability() -> None:
    clause = "Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    claims=[
                        RoleClaim(
                            kind="qualification",
                            modality="mandatory",
                            owner="unknown",
                            text=clause,
                            entities=[
                                CapabilityMention(
                                    surface_text="Python",
                                    canonical_name="Quantum Computing",
                                )
                            ],
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.required_capabilities == ["Python"]
    mention = parsed.role.evidence[0].claims[0].entities[0]
    assert mention.surface_text == "Python"
    assert mention.canonical_name == "Python"
    assert mention.source_start == parsed.role.description_clean.index("Python")
    assert mention.source_end == mention.source_start + len("Python")


def test_provider_alternative_claim_rejects_ungrounded_condition_text() -> None:
    clause = "Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="preferred",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    claims=[
                        RoleClaim(
                            kind="qualification",
                            modality="preferred",
                            owner="unknown",
                            text=clause,
                            condition_text="on Mars",
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.evidence[0].alternative_claims == []
    assert "role.evidence" not in parsed.data_quality.ambiguous_fields


def test_provider_alternative_claim_offsets_are_recomputed_from_source() -> None:
    clause = "Python is required."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="preferred",
                    source_section="qualifications",
                    text=clause,
                    evidence_text=clause,
                    claims=[
                        RoleClaim(
                            kind="qualification",
                            modality="preferred",
                            owner="unknown",
                            text=clause,
                            source_start=999,
                            source_end=1000,
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    alternative = parsed.role.evidence[0].alternative_claims[0]
    expected_start = parsed.role.description_clean.index(clause)
    assert alternative.source_start == expected_start
    assert alternative.source_end == expected_start + len(clause)


def test_role_overview_heading_can_mix_duties_and_requirements() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nAbout the role: You will build APIs. "
        "Candidates must have Python experience."
    )

    assert parsed.role.responsibilities == ["You will build APIs."]
    assert parsed.role.must_have_requirements == [
        "Candidates must have Python experience."
    ]
    assert parsed.role.required_capabilities == ["Python"]


@pytest.mark.parametrize(
    ("clause", "expected_owner"),
    [
        ("Our team requires strong collaboration.", "employer"),
        ("The team must have experience with Python.", "employer"),
        ("The company requires Python for its legacy platform.", "employer"),
        ("The company requires data integrity.", "employer"),
        ("The company requires platform reliability.", "employer"),
        ("The company requires travel data.", "employer"),
        ("The company requires overtime analysis.", "employer"),
        ("We require Python for our legacy platform.", "employer"),
    ],
)
def test_organizational_subjects_remain_context_in_role_overview(
    clause: str,
    expected_owner: str,
) -> None:
    document = parse_role_document(f"Title: Test Role\nAbout the role: {clause}")
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = document.evidence[0].claims[0]
    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []
    assert parsed.role.required_capabilities == []
    assert claim.kind == "company_context"
    assert claim.modality == "descriptive"
    assert claim.owner == expected_owner


def test_organizational_hiring_voice_can_imply_an_applicant_requirement() -> None:
    clause = "We require three years of Python experience."
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.required_capabilities == ["Python"]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"
    assert claim.owner == "applicant"


@pytest.mark.parametrize(
    "clause",
    [
        "We require weekend availability.",
        "We require reliable transportation.",
        "The company requires punctuality.",
        "The company requires regular attendance.",
        "The company requires professional conduct.",
        "The company requires occasional travel.",
        "The company requires frequent international travel.",
        "The company requires weekend work.",
        "The company requires regular night work.",
        "The company requires overtime during peak periods.",
        "The company requires overtime when needed.",
        "The company requires travel and overtime.",
        "The company requires work weekends.",
    ],
)
def test_recruiting_voice_direct_attributes_are_applicant_requirements(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind in {"qualification", "eligibility"}
    assert claim.modality == "mandatory"
    assert claim.owner == "applicant"


@pytest.mark.parametrize(
    "clause",
    [
        "The company requires all new employees to attend orientation.",
        "The company requires every newly hired employee to wear protective equipment.",
        "The organization requires all incoming hires to complete screening.",
    ],
)
def test_organizational_requirement_accepts_modified_applicant_targets(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"
    assert claim.owner == "applicant"


def test_organizational_requirement_with_applicant_object_remains_mandatory() -> None:
    clause = "The company requires candidates to have Python experience."
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.required_capabilities == ["Python"]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"
    assert claim.owner == "applicant"


@pytest.mark.parametrize(
    ("title", "expected_technologies"),
    [
        ("Senior React Native Developer", ["React"]),
        ("Software React Developer", ["React"]),
        ("Java Spring Developer", ["Java", "Spring"]),
    ],
)
def test_compound_technology_titles_use_the_complete_title_grammar(
    title: str,
    expected_technologies: list[str],
) -> None:
    parsed = parse_job_text(f"Title: {title}")

    assert parsed.role.tech_stack == expected_technologies
    assert parsed.role.capabilities == expected_technologies


@pytest.mark.parametrize(
    ("title", "excluded_technology"),
    [
        ("Spring Sales Consultant", "Spring"),
        ("React Support Specialist", "React"),
        ("R and D Engineer", "R"),
    ],
)
def test_title_grammar_rejects_non_technology_words_between_lexeme_and_role_head(
    title: str,
    excluded_technology: str,
) -> None:
    parsed = parse_job_text(f"Title: {title}")

    assert excluded_technology not in parsed.role.tech_stack
    assert excluded_technology not in parsed.role.capabilities


@pytest.mark.parametrize(
    "clause",
    [
        "Python is mandatory.",
        "Python is essential.",
        "Python is necessary.",
    ],
)
def test_copular_mandatory_productions_are_qualifications_in_role_overview(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"


def test_copular_mandatory_production_splits_from_optional_proposition() -> None:
    clause = "Python is optional, while Java is mandatory."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert [claim.modality for claim in parsed.role.evidence[0].claims] == [
        "optional",
        "mandatory",
    ]
    assert parsed.role.required_capabilities == ["Java"]
    assert parsed.role.preferred_capabilities == []


@pytest.mark.parametrize(
    ("clause", "expected_condition"),
    [
        ("For applicants in Quebec, French is required.", "For applicants in Quebec"),
        (
            "For all applicants in Quebec, French is required.",
            "For all applicants in Quebec",
        ),
        (
            "For only the applicants in Quebec, French is required.",
            "For only the applicants in Quebec",
        ),
        ("In Quebec, applicants must speak French.", "In Quebec"),
    ],
)
def test_preposed_conditional_adjunct_is_preserved(
    clause: str,
    expected_condition: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.condition_text == expected_condition


def test_discourse_adjunct_is_not_a_preposed_condition() -> None:
    parsed = parse_job_text(
        "Title: Test Role\nQualifications: In addition, Python is required."
    )

    assert parsed.role.evidence[0].claims[0].condition_text is None


@pytest.mark.parametrize(
    "clause",
    [
        "Applicants in Quebec must speak French.",
        "All applicants in Quebec must speak French.",
        "Any applicants in Quebec must speak French.",
        "Only applicants in Quebec must speak French.",
        "Only the applicants in Quebec must speak French.",
        "The successful applicants in Quebec must speak French.",
        "Applicants who reside in Quebec must speak French.",
    ],
)
def test_embedded_applicant_restriction_is_preserved_as_a_condition(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    condition = parsed.role.evidence[0].claims[0].condition_text
    assert condition is not None
    assert condition.casefold().endswith("in quebec")


@pytest.mark.parametrize(
    "clause",
    [
        "Python experience is not required, though it is preferred.",
        "Python experience is not required though it is preferred.",
    ],
)
def test_anaphoric_contrast_inherits_capability_antecedent(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["optional", "preferred"]
    assert claims[1].text == clause
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == ["Python"]
    assert parsed.role.capabilities == ["Python"]
    assert parsed.role.tech_stack == ["Python"]


def test_plural_anaphoric_contrast_inherits_capability_antecedent() -> None:
    clause = "Python and Java are not required, though they are preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["optional", "preferred"]
    assert claims[1].text == clause
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == ["Python", "Java"]
    assert parsed.role.capabilities == ["Python", "Java"]
    assert parsed.role.tech_stack == ["Python", "Java"]


def test_plural_applicant_pronoun_does_not_inherit_capability_complement() -> None:
    clause = "Candidates are not required to know Python, though they are preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["optional", "preferred"]
    assert claims[1].text == "they are preferred."
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == []
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []


def test_coordinated_capability_objects_are_not_split_by_later_contrast() -> None:
    clause = (
        "Candidates are not required to know Python and Java, "
        "though they are preferred."
    )
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["optional", "preferred"]
    assert claims[0].text == "Candidates are not required to know Python and Java"
    assert claims[1].text == "they are preferred."
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == []
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []


def test_coordinated_subject_keeps_its_shared_predicate_scope() -> None:
    clause = "Python is required and Java and SQL are preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["mandatory", "preferred"]
    assert claims[1].text == "Java and SQL are preferred."
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["SQL", "Java"]


def test_expletive_it_starts_an_independent_contrast_claim() -> None:
    clause = "Python is optional, but it is required that applicants know Java."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.text for claim in claims] == [
        "Python is optional",
        "it is required that applicants know Java.",
    ]
    assert [claim.modality for claim in claims] == ["optional", "mandatory"]


def test_contrast_boundary_blocks_should_from_governing_later_permission() -> None:
    clause = (
        "Applicants should provide references, though they are legally permitted to "
        "work in Canada."
    )
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert [claim.modality for claim in parsed.role.evidence[0].claims] == [
        "expected",
        "optional",
    ]


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates may not disclose customer data.",
        "You cannot disclose customer data.",
        "Employees are forbidden from sharing credentials.",
        "Do not share customer credentials.",
        "Don't share customer credentials.",
        "Don\u2019t share customer credentials.",
        "You aren't allowed to share customer credentials.",
        "You aren\u2019t allowed to share customer credentials.",
    ],
)
def test_negative_action_productions_are_prohibited_responsibilities(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.kind == "responsibility"
    assert claim.modality == "prohibited"
    assert claim.polarity == "negative"


@pytest.mark.parametrize(
    ("section", "clause"),
    [
        ("Qualifications", "Candidates must not use Java."),
        ("Qualifications", "Candidates aren't permitted to use Java."),
        ("Responsibilities", "Do not use Java."),
        ("Responsibilities", "Don't use Java."),
    ],
)
def test_negative_technology_claims_do_not_enter_positive_capability_projections(
    section: str,
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\n{section}: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.polarity == "negative"
    assert parsed.role.required_capabilities == []
    assert parsed.role.responsibility_capabilities == []
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []


def test_negative_environment_technology_is_an_exclusion_not_a_capability() -> None:
    parsed = parse_job_text("Title: Test Role\nTech Stack: We do not use Java.")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.kind == "environment"
    assert claim.polarity == "negative"
    assert parsed.role.environment_capabilities == []
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []


@pytest.mark.parametrize(
    "clause",
    [
        "Python may not be required.",
        "Candidates do not need prior Python experience.",
        "Candidates don't need prior Python experience.",
        "Candidates don\u2019t need prior Python experience.",
        "Candidates don't necessarily need prior Python experience.",
        "Candidates don\u2019t strictly need prior Python experience.",
        "Prior Python experience isn't required.",
        "Prior Python experience isn\u2019t required.",
        "Prior Python experience isn't strictly required.",
        "Prior Python experience isn\u2019t always required.",
        "Python experience isn't necessary.",
        "Python experience isn't needed.",
        "Prior Java experience is not a requirement.",
        "Prior Java experience isn't a requirement.",
    ],
)
def test_negative_non_requirement_productions_remain_optional(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.kind == "qualification"
    assert claim.modality == "optional"
    assert claim.polarity == "negative"
    assert parsed.role.required_capabilities == []
    assert parsed.role.capabilities == []
    assert parsed.role.tech_stack == []


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates may not have prior industry experience.",
        "Candidates may not have experience handling felony convictions.",
        "Applicants may not meet every qualification.",
    ],
)
def test_epistemic_may_not_does_not_invert_a_qualification(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == []
    assert claim.kind == "qualification"
    assert claim.modality == "optional"
    assert claim.polarity == "negative"


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates may not have a criminal record.",
        "Candidates may not have felony convictions.",
        "Applicants may not be under 18 years of age.",
        "Applicants may not be under 18.",
        "Applicants may not be below 18.",
        "Applicants may not be less than 18 years old.",
        "Applicants may not be 17 or younger.",
        "Applicants may not be age 17 or younger.",
        "Applicants may not be 17 years of age or younger.",
        "Applicants may not be minors.",
        "Candidates may not be located outside Canada.",
    ],
)
def test_deontic_may_not_eligibility_constraints_remain_mandatory(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "eligibility"
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


def test_epistemic_may_not_is_descriptive_in_a_responsibility_section() -> None:
    clause = "You may not always have complete information."
    parsed = parse_job_text(f"Title: Test Role\nResponsibilities: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == [clause]
    assert claim.kind == "responsibility"
    assert claim.modality == "descriptive"
    assert claim.polarity == "negative"


def test_role_owned_property_is_descriptive_not_an_applicant_requirement() -> None:
    clause = "This position is essential to our operations."
    document = parse_role_document(f"Title: Test Role\nAbout the role: {clause}")
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = document.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == []
    assert parsed.role.responsibilities == []
    assert claim.kind == "company_context"
    assert claim.modality == "descriptive"
    assert claim.owner == "role"


def test_role_subject_can_metonymically_express_a_requirement() -> None:
    clause = "This role requires Python experience."
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.required_capabilities == ["Python"]
    assert claim.kind == "qualification"
    assert claim.modality == "mandatory"
    assert claim.owner == "role"


def test_role_subject_can_metonymically_express_a_responsibility() -> None:
    clause = "The position will lead cross-functional projects."
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []
    assert claim.kind == "responsibility"
    assert claim.modality == "descriptive"
    assert claim.owner == "role"


@pytest.mark.parametrize(
    "clause",
    [
        "This position is expected to grow.",
        "This role will evolve over time.",
        "The opportunity may expand with the organization.",
        "This role is expected to grow alongside the company.",
        "This role is expected to grow organically.",
        "This role is expected to grow faster.",
        "This role is expected to grow further.",
        "This role is expected to grow fast.",
        "This role is expected to grow large.",
        "This role is expected to grow larger.",
        "This role is expected to grow larger over time.",
        "This role is expected to grow much faster.",
    ],
)
def test_role_change_of_state_predicates_are_descriptive_properties(
    clause: str,
) -> None:
    document = parse_role_document(f"Title: Test Role\nAbout the role: {clause}")
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = document.evidence[0].claims[0]
    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []
    assert claim.kind == "company_context"
    assert claim.modality == "descriptive"
    assert claim.owner == "role"


@pytest.mark.parametrize(
    ("clause", "expected_modality"),
    [
        ("This position is expected to oversee the support team.", "expected"),
        ("This position is expected to grow revenue.", "expected"),
        ("This position is expected to grow more revenue.", "expected"),
        ("This position is expected to grow larger accounts.", "expected"),
        ("This position is expected to grow strong teams.", "expected"),
        ("This role will scale the platform.", "descriptive"),
    ],
)
def test_role_subject_with_agentive_predicate_remains_a_responsibility(
    clause: str,
    expected_modality: str,
) -> None:
    parsed = parse_job_text(f"Title: Test Role\nAbout the role: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.responsibilities == [clause]
    assert claim.kind == "responsibility"
    assert claim.modality == expected_modality
    assert claim.owner == "role"


@pytest.mark.parametrize(
    "clause",
    [
        "No criminal record.",
        "Candidates must have no conflicts of interest.",
        "Candidates must be without a criminal record.",
        "Candidates must be free of conflicts of interest.",
        "Candidates must be free from conflicts of interest.",
        "Candidates must be devoid of conflicts of interest.",
    ],
)
def test_mandatory_nominal_negation_has_negative_polarity(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.kind == "eligibility"
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


def test_no_fewer_than_is_a_positive_minimum_not_nominal_negation() -> None:
    clause = "Candidates must have no fewer than three years of experience."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.modality == "mandatory"
    assert claim.polarity == "positive"


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates must be drug-free.",
        "Candidates must be conflict-free.",
        "Candidates must be smoke‑free.",
    ],
)
def test_hyphenated_free_adjectives_have_privative_polarity(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"


def test_free_infinitival_complement_is_not_privative() -> None:
    clause = "Candidates must be free to travel internationally."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.polarity == "positive"


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates are not without relevant experience.",
        "Candidates are not free from conflicts of interest.",
        "Candidates are not entirely devoid of relevant experience.",
        "Candidates must not be free from conflicts of interest.",
        "Candidates must not be conflict-free.",
        "Candidates must have no lack of relevant experience.",
    ],
)
def test_negated_privative_is_not_misread_as_negative_polarity(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.polarity == "positive"


def test_dotted_abbreviation_can_finish_one_sentence_before_another() -> None:
    parsed = parse_job_text(
        "Title: Test Role\n"
        "Qualifications: Authorized to work in the U.S. Python is preferred."
    )

    assert parsed.role.must_have_requirements == ["Authorized to work in the U.S."]
    assert parsed.role.nice_to_have_requirements == ["Python is preferred."]
    assert parsed.role.required_capabilities == []
    assert parsed.role.preferred_capabilities == ["Python"]


def test_independent_or_propositions_keep_distinct_modalities() -> None:
    clause = "Python is required or Java is preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert [claim.modality for claim in parsed.role.evidence[0].claims] == [
        "mandatory",
        "preferred",
    ]
    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["Java"]


@pytest.mark.parametrize(
    "clause",
    [
        "Python or Java is required.",
        "Python or higher-level languages are required.",
        "A degree or equivalent experience is required.",
    ],
)
def test_nominal_or_alternatives_remain_one_coordinated_claim(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert len(parsed.role.evidence[0].claims) == 1
    assert parsed.role.evidence[0].claims[0].coordination in {"any_of", "alternative"}


@pytest.mark.parametrize(
    "clause",
    [
        "Applicants must be 18 years of age or older.",
        "Applicants must be 18 years or older to apply.",
        "Applicants must be 18 or over.",
        "Applicants must have three years of experience or more.",
        "Applicants must have a GPA of 3.0 or better.",
        "Applicants must have a GPA of 3.0 or better in quantitative coursework.",
        "Applicants must hold a bachelor's degree or higher.",
        "A bachelor degree or higher is required.",
    ],
)
def test_scalar_or_bound_is_not_an_alternative_claim(clause: str) -> None:
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    assert parsed.role.evidence[0].claims[0].coordination == "single"


def test_explicit_copular_subject_does_not_inherit_previous_proposition() -> None:
    clause = "Python is preferred, but Java is required."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.text for claim in claims] == ["Python is preferred", "Java is required."]
    assert parsed.role.required_capabilities == ["Java"]
    assert parsed.role.preferred_capabilities == ["Python"]


def test_expected_claim_outranks_preferred_claim_for_legacy_projection() -> None:
    clause = "You are expected to know Python, and Java is preferred."
    parsed = parse_job_text(f"Title: Test Role\nQualifications: {clause}")

    evidence = parsed.role.evidence[0]
    assert [claim.modality for claim in evidence.claims] == ["expected", "preferred"]
    assert evidence.kind == "requirement"
    assert evidence.modality == "required"


def test_provider_equivalent_claim_order_does_not_create_ambiguity() -> None:
    clause = "Python is required, Java is preferred."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            evidence = baseline.role.evidence[0].model_copy(deep=True)
            evidence.claims = list(reversed(evidence.claims))
            parsed.role.evidence = [evidence]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    evidence = parsed.role.evidence[0]
    assert evidence.alternative_claims == []
    assert "provider_semantic_disagreement" not in evidence.quality_flags
    assert "role.evidence" not in parsed.data_quality.ambiguous_fields


def test_reordered_provider_claims_merge_entities_by_claim_identity() -> None:
    clause = "Experience with QuarkTool is required, NebulaSuite is preferred."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            evidence = baseline.role.evidence[0].model_copy(deep=True)
            for claim in evidence.claims:
                capability = "QuarkTool" if "QuarkTool" in claim.text else "NebulaSuite"
                claim.entities = [
                    CapabilityMention(
                        surface_text=capability,
                        canonical_name=capability,
                    )
                ]
            evidence.claims = list(reversed(evidence.claims))
            parsed.role.evidence = [evidence]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Test Role\nQualifications: {clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    evidence = parsed.role.evidence[0]
    assert parsed.role.required_capabilities == ["QuarkTool"]
    assert parsed.role.preferred_capabilities == ["NebulaSuite"]
    assert [
        [mention.canonical_name for mention in claim.entities]
        for claim in evidence.claims
    ] == [["QuarkTool"], ["NebulaSuite"]]
    assert evidence.alternative_claims == []


def test_high_confidence_unsectioned_clauses_emit_role_evidence() -> None:
    parsed = parse_job_text(
        "Title: Backend Developer\n"
        "Candidates must have Python and SQL experience.\n"
        "You will build APIs and maintain production services.\n"
        "AWS would be an asset."
    )

    assert parsed.role.must_have_requirements == [
        "Candidates must have Python and SQL experience."
    ]
    assert parsed.role.responsibilities == [
        "You will build APIs and maintain production services."
    ]
    assert parsed.role.nice_to_have_requirements == ["AWS would be an asset."]
    assert {item.source_section for item in parsed.role.evidence} == {"unsectioned"}


@pytest.mark.parametrize(
    "promise",
    [
        "You will receive health insurance and paid time off.",
        "You will enjoy competitive compensation and comprehensive benefits.",
        "You will have access to medical and dental coverage.",
        "You will earn a competitive salary and annual bonus.",
        "The successful candidate will receive health insurance.",
        "You will receive health insurance, support, and wellness benefits.",
        "You will receive health insurance and flexible scheduling.",
        "You will receive health insurance and professional development.",
        "You will receive health insurance and a company laptop.",
        "You will receive health insurance, stock options, and a gym membership.",
        "You will receive health insurance and learning opportunities.",
    ],
)
def test_unsectioned_employer_promises_are_not_candidate_duties(
    promise: str,
) -> None:
    parsed = parse_job_text(f"Title: Developer\n{promise}")

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []
    assert parsed.role.evidence == []


def test_unsectioned_applicant_action_remains_a_role_duty() -> None:
    parsed = parse_job_text(
        "Title: Developer\nYou will receive shipments and reconcile inventory."
    )

    assert parsed.role.responsibilities == [
        "You will receive shipments and reconcile inventory."
    ]


def test_unsectioned_employer_promise_does_not_hide_a_coordinated_duty() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "You will receive health insurance and collaborate with engineering."
    )

    assert parsed.role.responsibilities == [
        "You will collaborate with engineering."
    ]
    assert all(
        "health insurance" not in responsibility.casefold()
        for responsibility in parsed.role.responsibilities
    )


@pytest.mark.parametrize(
    ("clause", "expected_responsibilities"),
    [
        (
            "You will receive health insurance, collaborate with engineering, "
            "and support users.",
            [
                "You will collaborate with engineering.",
                "You will support users.",
            ],
        ),
        (
            "You will receive health insurance but will collaborate with engineering.",
            ["You will collaborate with engineering."],
        ),
        (
            "You will receive shipments and receive an annual bonus.",
            ["You will receive shipments."],
        ),
        (
            "The successful candidate will receive health insurance and "
            "collaborate with engineering.",
            ["The successful candidate will collaborate with engineering."],
        ),
    ],
)
def test_unsectioned_mixed_employer_promises_preserve_every_duty(
    clause: str,
    expected_responsibilities: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.responsibilities == expected_responsibilities
    assert all(
        "insurance" not in responsibility.casefold()
        and "bonus" not in responsibility.casefold()
        for responsibility in parsed.role.responsibilities
    )


@pytest.mark.parametrize(
    ("clause", "expected_responsibilities"),
    [
        (
            "Employees are offered health insurance and collaborate with engineering.",
            ["Employees collaborate with engineering."],
        ),
        (
            "The successful candidate is provided with health insurance but will "
            "support users.",
            ["The successful candidate will support users."],
        ),
        (
            "You will receive health insurance and review applications.",
            ["You will review applications."],
        ),
        (
            "You will receive health insurance, present findings, and advise clients.",
            ["You will present findings.", "You will advise clients."],
        ),
    ],
)
def test_unsectioned_promise_coordination_preserves_open_class_duties(
    clause: str,
    expected_responsibilities: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.responsibilities == expected_responsibilities


@pytest.mark.parametrize(
    "clause",
    [
        "The platform must use Python.",
        "This product should support Python.",
        "The application requires Python.",
    ],
)
def test_unsectioned_system_constraints_are_not_candidate_requirements(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert parsed.role.evidence == []


@pytest.mark.parametrize(
    "clause",
    [
        "Must have Python experience.",
        "Python is required.",
        "Python is required for the platform.",
        "The ability to travel is required.",
    ],
)
def test_unsectioned_subjectless_and_qualification_nominals_remain_requirements(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_unsectioned_subjectless_modal_can_take_a_system_object() -> None:
    clause = "Must use the platform."
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []


def test_bare_excel_proficiency_is_a_nontechnical_capability() -> None:
    clause = "Excel proficiency is required."
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.capabilities == ["Excel"]
    assert parsed.role.required_capabilities == ["Excel"]
    assert parsed.role.tech_stack == []


@pytest.mark.parametrize("title", ["Excel Analyst", "Excel Reporting Analyst"])
def test_bare_excel_title_is_a_nontechnical_capability(title: str) -> None:
    parsed = parse_job_text(f"Title: {title}")

    assert "Excel" in parsed.role.capabilities
    assert "Excel" not in parsed.role.tech_stack
    assert "finance" in parsed.role.capability_domains


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates must be proficient in Excel at an advanced level.",
        "Proficiency in Excel as a reporting tool is required.",
    ],
)
def test_excel_noun_before_prepositional_complement_is_a_capability(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert "Excel" in parsed.role.capabilities
    assert "Excel" in parsed.role.required_capabilities
    assert "Excel" not in parsed.role.tech_stack


def test_prohibited_qualification_outranks_preferred_section_default() -> None:
    clause = "Candidates must not require visa sponsorship."
    parsed = parse_job_text(
        f"Title: Developer\nPreferred Qualifications:\n{clause}"
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.polarity == "negative"
    assert "section_modality_conflict" in claim.quality_flags
    assert parsed.role.must_have_requirements == [clause]
    assert parsed.role.nice_to_have_requirements == []


@pytest.mark.parametrize("focus_word", ["only", "just", "merely"])
def test_focus_negation_remains_positive_and_mandatory(focus_word: str) -> None:
    clause = f"Candidates must not {focus_word} know Python but also understand SQL."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "mandatory"
    assert claim.polarity == "positive"
    assert claim.coordination == "all_of"
    assert parsed.role.required_capabilities == ["Python", "SQL"]


def test_capability_section_preserves_environment_and_explicit_requirements() -> None:
    parsed = parse_job_text(
        "Title: Developer\nTechnologies:\n"
        "Python is required.\nAWS would be an asset.\nDocker and Kubernetes."
    )

    assert parsed.role.required_capabilities == ["Python"]
    assert parsed.role.preferred_capabilities == ["AWS"]
    assert parsed.role.environment_capabilities == [
        "Python",
        "Docker",
        "Kubernetes",
        "AWS",
    ]
    assert [claim.kind for claim in parsed.role.evidence[0].claims] == [
        "environment",
        "qualification",
    ]


def test_shared_modal_predicate_coordination_creates_distinct_claims() -> None:
    clause = "Candidates must have Python experience and be authorized to work in Canada."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    claims = parsed.role.evidence[0].claims
    assert [(claim.kind, claim.modality) for claim in claims] == [
        ("qualification", "mandatory"),
        ("eligibility", "mandatory"),
    ]
    assert claims[1].normalized_text == (
        "Candidates must be authorized to work in Canada."
    )


def test_exact_structural_requirement_leaf_links_to_its_claim() -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        "You must maintain systems and support users."
    )

    evidence = parsed.role.evidence[0]
    expression = evidence.requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert expression.children[0].text == evidence.claims[0].text
    assert expression.children[0].source_start == evidence.claims[0].source_start
    assert expression.children[0].source_end == evidence.claims[0].source_end
    assert expression.children[0].claim_id == evidence.claims[0].claim_id
    assert expression.children[1].claim_id is None


def test_mixed_modality_legacy_lists_use_claim_specific_display_text() -> None:
    clause = "Fluency in French is required in Quebec and preferred in Ottawa."
    parsed = parse_job_text(f"Title: Bilingual Agent\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Fluency in French is required in Quebec."
    ]
    assert parsed.role.nice_to_have_requirements == [
        "Fluency in French is preferred in Ottawa."
    ]


@pytest.mark.parametrize(
    ("clause", "unexpected_capability"),
    [
        ("To excel in this role, collaborate with engineering.", "Excel"),
        ("You will report to the Project Manager.", "Project Management"),
    ],
)
def test_ambiguous_role_capabilities_require_positive_context(
    clause: str,
    unexpected_capability: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert unexpected_capability not in parsed.role.capabilities


def test_excel_verb_does_not_infer_a_finance_domain() -> None:
    parsed = parse_job_text(
        "Title: Analyst\nResponsibilities:\n"
        "To excel in this role, collaborate with engineering."
    )

    assert "Excel" not in parsed.role.capabilities
    assert "finance" not in parsed.role.capability_domains


def test_condition_extraction_distinguishes_locations_from_capability_complements() -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        "Proficiency is required in Python and SQL.\n"
        "French is required in Quebec."
    )

    first, second = [item.claims[0] for item in parsed.role.evidence]
    assert first.condition_text is None
    assert first.condition_type is None
    assert second.condition_text == "in Quebec"
    assert second.condition_type == "geographic"


@pytest.mark.parametrize(
    ("clause", "condition_text", "condition_type"),
    [
        (
            "Proficiency is required in python for candidates applying in Quebec.",
            "for candidates applying in Quebec",
            "applicant_subset",
        ),
        (
            "Proficiency is required in python when supporting production.",
            "when supporting production",
            "contingency",
        ),
    ],
)
def test_condition_extraction_skips_a_lowercase_capability_complement(
    clause: str,
    condition_text: str,
    condition_type: str,
) -> None:
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == condition_text
    assert claim.condition_type == condition_type


def test_condition_extraction_is_independent_from_modality() -> None:
    parsed = parse_job_text(
        "Title: Developer\nResponsibilities:\n"
        "When on call, respond to production incidents.\n"
        "During overnight shifts, support weekend deployments."
    )

    first, second = [item.claims[0] for item in parsed.role.evidence]
    assert (first.condition_text, first.condition_type) == (
        "When on call",
        "contingency",
    )
    assert (second.condition_text, second.condition_type) == (
        "During overnight shifts",
        "schedule",
    )


@pytest.mark.parametrize(
    ("clause", "entity"),
    [
        ("Mastery of Simulink.", "Simulink"),
        ("Python experience.", "Python"),
        ("Software development experience.", "Software development"),
        (
            "Software application development experience.",
            "Software application development",
        ),
        (
            "Product software development experience.",
            "Product software development",
        ),
    ],
)
def test_exact_provider_only_evidence_can_rescue_capability_grammar(
    clause: str,
    entity: str,
) -> None:

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="unsectioned",
                    text=clause,
                    evidence_text=clause,
                    entities=[entity],
                    claims=[
                        RoleClaim(
                            kind="qualification",
                            modality="mandatory",
                            owner="applicant",
                            text=clause,
                            entities=[
                                CapabilityMention(
                                    surface_text=entity,
                                    canonical_name=entity,
                                )
                            ],
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Controls Developer\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert parsed.role.required_capabilities == [entity]
    assert "provider_only_grounded_evidence" in parsed.role.evidence[0].quality_flags
    assert parsed.data_quality.needs_manual_review is True


def test_provider_only_grounding_rebuilds_derived_claim_fields() -> None:
    clause = "Software development experience with Python."

    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="unsectioned",
                    text=clause,
                    evidence_text=clause,
                    entities=["Python"],
                    claims=[
                        RoleClaim(
                            claim_id="provider-invented-id",
                            kind="qualification",
                            modality="mandatory",
                            owner="applicant",
                            text=clause,
                            normalized_text=(
                                "Ten years of Kubernetes experience is required."
                            ),
                            decision_trace=["provider:invented"],
                        )
                    ],
                )
            ]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Developer\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    claim = parsed.role.evidence[0].claims[0]
    assert parsed.role.must_have_requirements == [clause]
    assert claim.normalized_text is None
    assert claim.claim_id != "provider-invented-id"
    assert "provider:invented" not in claim.decision_trace
    assert "source:provider_only_grounded" in claim.decision_trace


def test_provider_only_grounding_preserves_an_exact_requirement_expression() -> None:
    clause = "Mastery of Python or Java."
    posting_text = f"Title: Developer\n{clause}"
    clause_start = posting_text.index(clause)
    clause_end = clause_start + len(clause)
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        evidence_start=clause_start,
        evidence_end=clause_end,
        entities=["Python", "Java"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                coordination="any_of",
                text=clause,
            )
        ],
        requirement_expression=RequirementExpression(
            node_id="provider-invented-root",
            operator="any_of",
            text=clause,
            source_start=clause_start,
            source_end=clause_end,
            children=[
                RequirementExpression(
                    node_id="provider-invented-python",
                    claim_id="provider-invented-claim",
                    operator="claim",
                    text="Python",
                    source_start=clause_start + 11,
                    source_end=clause_start + 17,
                ),
                RequirementExpression(
                    node_id="provider-invented-java",
                    operator="claim",
                    text="Java",
                    source_start=clause_start + 21,
                    source_end=clause_start + 25,
                ),
            ],
        ),
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is not None
    expression = projected.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert expression.node_id == (
        f"role-requirement-{clause_start}-{clause_end}-any_of"
    )
    assert [child.text for child in expression.children] == ["Python", "Java"]
    assert [child.node_id for child in expression.children] == [
        f"role-requirement-{clause_start + 11}-{clause_start + 17}-claim",
        f"role-requirement-{clause_start + 21}-{clause_start + 25}-claim",
    ]
    assert [child.claim_id for child in expression.children] == [None, None]


@pytest.mark.parametrize(
    ("clause", "operator", "members"),
    [
        ("Mastery of Python and Java.", "any_of", ["Python", "Java"]),
        ("Mastery of Python or Java.", "all_of", ["Python", "Java"]),
        ("Mastery of Python or Java or Go.", "any_of", ["Python", "Java"]),
        ("Mastery of Python or Java or Go.", "any_of", ["Python", "Go"]),
    ],
)
def test_provider_only_grounding_rejects_semantically_inexact_expression(
    clause: str,
    operator: str,
    members: list[str],
) -> None:
    posting_text = f"Title: Developer\n{clause}"
    clause_start = posting_text.index(clause)
    clause_end = clause_start + len(clause)
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        evidence_start=clause_start,
        evidence_end=clause_end,
        entities=members,
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                coordination=operator,
                text=clause,
            )
        ],
        requirement_expression=RequirementExpression(
            operator=operator,
            text=clause,
            source_start=clause_start,
            source_end=clause_end,
            children=[
                RequirementExpression(
                    operator="claim",
                    text=member,
                    source_start=clause_start + clause.index(member),
                    source_end=clause_start + clause.index(member) + len(member),
                )
                for member in members
            ],
        ),
    )

    assert project_validated_role_evidence(posting_text, [provider_item]) is None


def test_provider_only_grounding_accepts_an_exact_nested_expression() -> None:
    clause = "Mastery of Python or (Java and SQL)."
    posting_text = f"Title: Developer\n{clause}"
    clause_start = posting_text.index(clause)
    clause_end = clause_start + len(clause)

    def span(value: str) -> tuple[int, int]:
        start = clause_start + clause.index(value)
        return start, start + len(value)

    python_start, python_end = span("Python")
    java_start, java_end = span("Java")
    sql_start, sql_end = span("SQL")
    nested_text = "(Java and SQL)"
    nested_start, nested_end = span(nested_text)
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        evidence_start=clause_start,
        evidence_end=clause_end,
        entities=["Python", "Java", "SQL"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                coordination="any_of",
                text=clause,
            )
        ],
        requirement_expression=RequirementExpression(
            operator="any_of",
            text=clause,
            source_start=clause_start,
            source_end=clause_end,
            children=[
                RequirementExpression(
                    operator="claim",
                    text="Python",
                    source_start=python_start,
                    source_end=python_end,
                ),
                RequirementExpression(
                    operator="all_of",
                    text=nested_text,
                    source_start=nested_start,
                    source_end=nested_end,
                    children=[
                        RequirementExpression(
                            operator="claim",
                            text="Java",
                            source_start=java_start,
                            source_end=java_end,
                        ),
                        RequirementExpression(
                            operator="claim",
                            text="SQL",
                            source_start=sql_start,
                            source_end=sql_end,
                        ),
                    ],
                ),
            ],
        ),
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is not None
    expression = projected.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]


def test_provider_only_grounding_rejects_a_hidden_boolean_leaf() -> None:
    clause = "Mastery of Python or Java and SQL."
    posting_text = f"Title: Developer\n{clause}"
    clause_start = posting_text.index(clause)
    clause_end = clause_start + len(clause)

    def leaf(value: str) -> RequirementExpression:
        start = clause_start + clause.index(value)
        return RequirementExpression(
            operator="claim",
            text=value,
            source_start=start,
            source_end=start + len(value),
        )

    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        evidence_start=clause_start,
        evidence_end=clause_end,
        entities=["Python", "Java", "SQL"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                coordination="any_of",
                text=clause,
            )
        ],
        requirement_expression=RequirementExpression(
            operator="any_of",
            text=clause,
            source_start=clause_start,
            source_end=clause_end,
            children=[leaf("Python"), leaf("Java and SQL")],
        ),
    )

    assert project_validated_role_evidence(posting_text, [provider_item]) is None


def test_provider_only_grounding_accepts_an_exact_scalar_boolean_claim() -> None:
    clause = "Mastery of Python and Java."
    posting_text = f"Title: Developer\n{clause}"
    clause_start = posting_text.index(clause)
    clause_end = clause_start + len(clause)
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        evidence_start=clause_start,
        evidence_end=clause_end,
        entities=["Python", "Java"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                coordination="all_of",
                text=clause,
            )
        ],
        requirement_expression=RequirementExpression(
            operator="claim",
            text=clause,
            source_start=clause_start,
            source_end=clause_end,
        ),
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is not None
    expression = projected.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "claim"
    assert expression.claim_id == projected.evidence[0].claims[0].claim_id


def test_provider_only_grounding_rejects_an_ungrounded_requirement_expression() -> None:
    posting_text = "Mastery of Python or Java."
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=posting_text,
        evidence_text=posting_text,
        entities=["Python", "Java"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text=posting_text,
            )
        ],
        requirement_expression=RequirementExpression(
            operator="any_of",
            text=posting_text,
            source_start=0,
            source_end=len(posting_text),
            children=[
                RequirementExpression(
                    operator="claim",
                    text="Python",
                    source_start=11,
                    source_end=17,
                ),
                RequirementExpression(
                    operator="claim",
                    text="Kubernetes",
                    source_start=21,
                    source_end=31,
                ),
            ],
        ),
    )

    assert project_validated_role_evidence(posting_text, [provider_item]) is None


def test_provider_only_grounding_reconstructs_claim_text_from_source() -> None:
    posting_text = "Python experience."
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=posting_text,
        evidence_text=posting_text,
        entities=["Python"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text="python experience.",
                entities=[
                    CapabilityMention(
                        surface_text="python",
                        canonical_name="Python",
                    )
                ],
            )
        ],
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is not None
    claim = projected.evidence[0].claims[0]
    assert claim.text == posting_text
    assert posting_text[claim.source_start : claim.source_end] == claim.text
    assert projected.must_have_requirements == [posting_text]


def test_provider_only_grounding_rejects_reused_source_span() -> None:
    posting_text = "Python experience."
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=posting_text,
        evidence_text=posting_text,
        entities=["Python"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text=posting_text,
            )
        ],
    )

    projected = project_validated_role_evidence(
        posting_text,
        [provider_item, provider_item.model_copy(deep=True)],
    )

    assert projected is None


def test_provider_grounding_rejects_source_span_reused_after_a_match() -> None:
    clause = "Python is required."
    posting_text = f"Qualifications:\n{clause}"
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="qualifications",
        text=clause,
        evidence_text=clause,
        entities=["Python"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text=clause,
            )
        ],
    )

    projected = project_validated_role_evidence(
        posting_text,
        [provider_item, provider_item.model_copy(deep=True)],
    )

    assert projected is None


def test_provider_grounding_rejects_a_subspan_of_deterministic_evidence() -> None:
    clause = "Candidates must have Python experience and SQL knowledge."
    posting_text = f"Qualifications:\n{clause}"
    evidence_text = "Python experience"
    evidence_start = posting_text.index(evidence_text)
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="qualifications",
        text=evidence_text,
        evidence_text=evidence_text,
        evidence_start=evidence_start,
        evidence_end=evidence_start + len(evidence_text),
        entities=["Python"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text=evidence_text,
            )
        ],
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is None


def test_provider_evidence_case_drift_does_not_duplicate_deterministic_evidence() -> None:
    posting_text = "Qualifications:\nPython is required."
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="qualifications",
        text="python is required.",
        evidence_text="python is required.",
        entities=["Python"],
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text="python is required.",
            )
        ],
    )

    projected = project_validated_role_evidence(posting_text, [provider_item])

    assert projected is None


@pytest.mark.parametrize(
    ("clause", "entities"),
    [
        ("We are a growing company.", []),
        ("We use Python internally.", ["Python"]),
        ("The platform uses Python.", ["Python"]),
        ("This product uses Python.", ["Python"]),
        ("The platform is developed using Python.", ["Python"]),
        ("The platform incorporates Python development.", ["Python"]),
        ("The platform improves Python experience.", ["Python"]),
    ],
)
def test_provider_only_unsectioned_evidence_requires_candidate_grammar(
    clause: str,
    entities: list[str],
) -> None:
    provider_item = RoleEvidence(
        kind="requirement",
        modality="required",
        source_section="unsectioned",
        text=clause,
        evidence_text=clause,
        entities=entities,
        claims=[
            RoleClaim(
                kind="qualification",
                modality="mandatory",
                owner="applicant",
                text=clause,
                entities=[
                    CapabilityMention(surface_text=entity, canonical_name=entity)
                    for entity in entities
                ],
            )
        ],
    )

    assert project_validated_role_evidence(clause, [provider_item]) is None


@pytest.mark.parametrize(
    ("clause", "entity"),
    [
        ("Candidates must know COBOL.", "COBOL"),
        ("Strong COBOL skills.", "COBOL"),
        ("Hands-on COBOL development.", "COBOL"),
        ("Programming languages such as COBOL.", "COBOL"),
        ("Mastery of Simulink.", "Simulink"),
    ],
)
def test_provider_novel_entities_use_claim_and_capability_frames(
    clause: str,
    entity: str,
) -> None:
    class FakeParser:
        def parse(self, raw_text, source_url=None, baseline=None):
            parsed = baseline.model_copy(deep=True)
            evidence = baseline.role.evidence[0].model_copy(deep=True)
            evidence.entities = [entity]
            evidence.claims[0].entities = [
                CapabilityMention(surface_text=entity, canonical_name=entity)
            ]
            parsed.role.evidence = [evidence]
            return parsed

    parsed = parse_job_text_with_settings(
        f"Title: Developer\nQualifications:\n{clause}",
        settings=Settings(posting_parser_provider="codex_cli"),
        parser=FakeParser(),
    )

    assert entity in parsed.role.required_capabilities


def test_nested_coordination_has_a_grounded_requirement_expression() -> None:
    clause = (
        "A bachelor's degree in Computer Science, Engineering, or a related field "
        "and two years of Python experience."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["any_of", "claim"]
    assert expression.children[0].children
    assert all(child.text for child in expression.children[0].children)
    nodes = [
        expression,
        *expression.children,
        *expression.children[0].children,
    ]
    assert all(node.node_id for node in nodes)
    assert len({node.node_id for node in nodes}) == len(nodes)
    claims_by_id = {
        claim.claim_id: claim for claim in parsed.role.evidence[0].claims
    }
    for node in nodes:
        if node.claim_id is None:
            continue
        linked_claim = claims_by_id[node.claim_id]
        assert (node.text, node.source_start, node.source_end) == (
            linked_claim.text,
            linked_claim.source_start,
            linked_claim.source_end,
        )


def test_or_both_coordination_preserves_the_explicit_nested_scope() -> None:
    clause = "Candidates must know Python or both Java and SQL."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [
        child.text for child in expression.children[1].children
    ] == ["Java", "SQL."]


def test_or_both_predicates_ignore_a_quantitative_or_more_bound() -> None:
    clause = (
        "Candidates must know Python or both have 3 or more years of Java "
        "experience and hold SQL certification."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [
        child.text for child in expression.children[1].children
    ] == [
        "have 3 or more years of Java experience",
        "hold SQL certification.",
    ]


def test_or_both_predicates_ignore_a_between_range_complement() -> None:
    clause = (
        "Candidates must know Python or both have between 3 and 5 years of "
        "experience and hold a degree."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    conjunction = expression.children[1]
    assert conjunction.operator == "all_of"
    assert [child.text for child in conjunction.children] == [
        "have between 3 and 5 years of experience",
        "hold a degree.",
    ]


def test_both_or_coordination_preserves_the_explicit_nested_scope() -> None:
    clause = "Candidates must know both Java and SQL or Python."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["all_of", "claim"]
    assert [
        child.text for child in expression.children[0].children
    ] == ["Java", "SQL"]
    assert expression.children[1].text == "Python."


def test_both_or_recursively_parses_the_other_conjunctive_alternative() -> None:
    clause = "Candidates must know both Java and SQL or Python and Go."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["all_of", "all_of"]
    assert [child.text for child in expression.children[1].children] == [
        "Python",
        "Go.",
    ]


def test_and_either_coordination_preserves_the_explicit_nested_scope() -> None:
    clause = "Candidates must know Python and either Java or SQL."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "any_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [
        child.text for child in expression.children[1].children
    ] == ["Java", "SQL."]


def test_and_either_predicates_preserve_the_explicit_nested_scope() -> None:
    clause = (
        "Candidates must know Python and either have Java experience or hold "
        "SQL certification."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "any_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [
        child.text for child in expression.children[1].children
    ] == ["have Java experience", "hold SQL certification."]


def test_and_either_scope_ignores_a_quantitative_or_more_bound() -> None:
    clause = (
        "Candidates must know Python and either have 3 or more years of Java "
        "experience or hold SQL certification."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "any_of"]
    assert [
        child.text for child in expression.children[1].children
    ] == [
        "have 3 or more years of Java experience",
        "hold SQL certification.",
    ]


def test_and_either_scope_ignores_a_discrete_quantitative_range() -> None:
    clause = (
        "Candidates must know Python and either have 3 or 4 years of Java "
        "experience or hold SQL certification."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "any_of"]
    assert [
        child.text for child in expression.children[1].children
    ] == [
        "have 3 or 4 years of Java experience",
        "hold SQL certification.",
    ]


@pytest.mark.parametrize(
    ("clause", "branch_path", "expected_members"),
    [
        (
            "Candidates must know Python and either both Java and SQL or Go.",
            (1, 0),
            ["Java", "SQL"],
        ),
        (
            "Candidates must know Python and either Go or both Java and SQL.",
            (1, 1),
            ["Java", "SQL."],
        ),
        (
            "Candidates must know both Python and Java and either SQL or Go.",
            (0,),
            ["Python", "Java"],
        ),
    ],
)
def test_and_either_recursively_parses_both_branches(
    clause: str,
    branch_path: tuple[int, ...],
    expected_members: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    branch = expression
    for child_index in branch_path:
        branch = branch.children[child_index]
    assert branch.operator == "all_of"
    assert [child.text for child in branch.children] == expected_members


def test_and_either_preserves_three_explicit_alternatives() -> None:
    clause = "Candidates must know Python and either Java, SQL, or Go."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    alternatives = expression.children[1]
    assert alternatives.operator == "any_of"
    assert [child.text for child in alternatives.children] == ["Java", "SQL", "Go."]


@pytest.mark.parametrize(
    ("modifier", "field"),
    [
        ("preferably", "Computer Science"),
        ("ideally", "Engineering"),
    ],
)
def test_and_either_keeps_a_supplemental_modifier_inside_its_branch(
    modifier: str,
    field: str,
) -> None:
    clause = (
        f"Candidates must know Python and either hold a degree, {modifier} in "
        f"{field}, or have equivalent experience."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    evidence = parsed.role.evidence[0]
    expression = evidence.requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "any_of"]
    assert [child.text for child in expression.children[1].children] == [
        f"hold a degree, {modifier} in {field}",
        "have equivalent experience.",
    ]
    assert all(claim.condition_type is None for claim in evidence.claims)


def test_repeated_and_either_groups_are_sibling_conjuncts() -> None:
    clause = "Candidates must know Python and either Java or SQL and either Go or Rust."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == [
        "claim",
        "any_of",
        "any_of",
    ]
    assert [child.text for child in expression.children[1].children] == ["Java", "SQL"]
    assert [child.text for child in expression.children[2].children] == ["Go", "Rust."]


def test_parenthesized_supplement_stays_inside_its_outer_alternative() -> None:
    clause = (
        "Candidates must know Python and either hold a degree (preferably in "
        "Computer Science, Engineering, or Mathematics) or have equivalent experience."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    alternatives = expression.children[1]
    assert alternatives.operator == "any_of"
    assert [child.text for child in alternatives.children] == [
        (
            "hold a degree (preferably in Computer Science, Engineering, or "
            "Mathematics)"
        ),
        "have equivalent experience.",
    ]


def test_nested_and_either_stays_inside_the_outer_alternative() -> None:
    clause = (
        "Candidates must either hold a degree or (have five years of experience "
        "and either be certified or obtain certification within six months)."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]
    nested = expression.children[1]
    assert [child.operator for child in nested.children] == ["claim", "any_of"]
    assert nested.children[0].text == "have five years of experience"
    assert [child.text for child in nested.children[1].children] == [
        "be certified",
        "obtain certification within six months",
    ]


def test_plain_coordination_inside_outer_alternatives_keeps_precedence() -> None:
    clause = "Candidates must either (know Python or SQL) or (know Java and Go)."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["any_of", "all_of"]
    assert [child.text for child in expression.children[0].children] == [
        "know Python",
        "SQL",
    ]
    assert [child.text for child in expression.children[1].children] == [
        "know Java",
        "Go",
    ]


def test_plain_mixed_coordination_uses_and_before_or_precedence() -> None:
    clause = "Candidates must know Python or hold a degree and have experience."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [child.text for child in expression.children[1].children] == [
        "hold a degree",
        "have experience.",
    ]


@pytest.mark.parametrize(
    "shared_head_phrase",
    [
        "Python or SQL experience",
        "Python, SQL, or Java experience",
        "research or development experience",
        "written or verbal communication skills",
    ],
)
def test_shared_head_alternative_stays_inside_required_conjunct(
    shared_head_phrase: str,
) -> None:
    clause = f"Candidates must have {shared_head_phrase} and hold a degree."
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]
    assert [child.text for child in expression.children] == [
        f"Candidates must have {shared_head_phrase}",
        "hold a degree.",
    ]


def test_serial_shared_head_alternative_after_required_conjunct_stays_nested() -> None:
    clause = (
        "Candidates must hold a degree and have Python, SQL, or Java experience."
    )
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]
    assert [child.text for child in expression.children] == [
        "Candidates must hold a degree",
        "have Python, SQL, or Java experience.",
    ]


@pytest.mark.parametrize(
    ("clause", "expected_members", "expected_requirements"),
    [
        (
            (
                "Candidates must have Python or SQL experience, hold a "
                "certification, and hold a degree."
            ),
            [
                "Candidates must have Python or SQL experience",
                "hold a certification",
                "hold a degree.",
            ],
            [
                "Candidates must have Python or SQL experience.",
                "Candidates must hold a certification.",
                "Candidates must hold a degree.",
            ],
        ),
        (
            "Candidates must know Python or SQL, have experience, and hold a degree.",
            [
                "Candidates must know Python or SQL",
                "have experience",
                "hold a degree.",
            ],
            [
                "Candidates must know Python or SQL.",
                "Candidates must have experience.",
                "Candidates must hold a degree.",
            ],
        ),
        (
            (
                "Candidates must hold a degree, have Python, SQL, or Java "
                "experience, and be certified."
            ),
            [
                "Candidates must hold a degree",
                "have Python, SQL, or Java experience",
                "be certified.",
            ],
            [
                "Candidates must hold a degree.",
                "Candidates must have Python, SQL, or Java experience.",
                "Candidates must be certified.",
            ],
        ),
    ],
)
def test_local_alternative_closes_before_serial_required_members(
    clause: str,
    expected_members: list[str],
    expected_requirements: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == [
        "claim",
        "claim",
        "claim",
    ]
    assert [child.text for child in expression.children] == expected_members
    assert parsed.role.must_have_requirements == expected_requirements


@pytest.mark.parametrize(
    ("clause", "expected_requirements"),
    [
        (
            "Candidates must use Python, develop APIs, and maintain services.",
            [
                "Candidates must use Python.",
                "Candidates must develop APIs.",
                "Candidates must maintain services.",
            ],
        ),
        (
            "Candidates must complete training, obtain certification, and receive approval.",
            [
                "Candidates must complete training.",
                "Candidates must obtain certification.",
                "Candidates must receive approval.",
            ],
        ),
    ],
)
def test_serial_requirement_predicates_share_the_matrix_modal(
    clause: str,
    expected_requirements: list[str],
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    claims = parsed.role.evidence[0].claims
    assert [claim.modality for claim in claims] == ["mandatory", "mandatory", "mandatory"]
    assert [claim.owner for claim in claims] == ["applicant", "applicant", "applicant"]
    assert parsed.role.must_have_requirements == expected_requirements


def test_gerund_complements_do_not_inherit_the_matrix_modal() -> None:
    clause = (
        "Candidates must have experience developing APIs, maintaining services, "
        "and supporting users."
    )
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert [claim.normalized_text or claim.text for claim in parsed.role.evidence[0].claims] == [
        clause
    ]


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates must know design, research, and support.",
        "Candidates must demonstrate leadership, research, and support.",
        "Candidates must have design, research, and support experience.",
        "Candidates must use design, research, and support tools.",
    ],
)
def test_predicate_homonyms_in_object_lists_do_not_inherit_the_modal(clause: str) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_multiword_predicate_homonyms_remain_parallel_noun_complements() -> None:
    clause = (
        "Candidates must have knowledge of design systems, research methods, "
        "and support tools."
    )

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert len(parsed.role.evidence[0].claims) == 1


@pytest.mark.parametrize(
    "clause",
    [
        "Candidates must use design systems and support tools effectively.",
        (
            "Candidates must be able to use design systems and support tools "
            "effectively."
        ),
        (
            "Candidates must have knowledge of design systems, research methods, "
            "and support tools at scale."
        ),
        "Candidates must demonstrate leadership and research methods in practice.",
        "Candidates must know design systems and support tools for reporting.",
    ],
)
def test_postmodified_noun_complements_remain_under_the_shared_governor(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert len(parsed.role.evidence[0].claims) == 1


def test_true_coordinated_predicates_still_inherit_the_matrix_modal() -> None:
    clause = "Candidates must research user needs and support customers."

    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert parsed.role.responsibilities == [
        "Candidates must research user needs.",
        "Candidates must support customers.",
    ]


def test_embedded_governor_does_not_control_the_matrix_coordination() -> None:
    clause = (
        "Candidates who have certifications must travel frequently and support tools."
    )

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Candidates who have certifications must travel frequently.",
        "Candidates who have certifications must support tools.",
    ]


def test_relative_clause_governor_controls_coordination_inside_its_scope() -> None:
    clause = "Candidates must support users who have design systems and research tools."

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]


def test_embedded_nominal_coordination_keeps_its_shared_finite_predicate() -> None:
    clause = (
        "Candidates must demonstrate that design systems and support tools are "
        "required."
    )

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert len(parsed.role.evidence[0].claims) == 1


def test_comma_closes_relative_scope_before_matrix_coordination() -> None:
    clause = "Candidates must support users who have certifications, and research tools."

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Candidates must support users who have certifications.",
        "Candidates must research tools.",
    ]


def test_postmodified_true_predicate_still_inherits_the_matrix_modal() -> None:
    clause = "Candidates must design systems and support tools effectively."

    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert parsed.role.responsibilities == [
        "Candidates must design systems.",
        "Candidates must support tools effectively.",
    ]


def test_noun_inside_predicate_adjunct_is_not_the_coordinate_head() -> None:
    clause = "Candidates must use systems and support customers with tools."

    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert parsed.role.responsibilities == [
        "Candidates must use systems.",
        "Candidates must support customers with tools.",
    ]


def test_nominal_coordinate_can_be_followed_by_a_new_predicate() -> None:
    clause = (
        "Candidates must use design systems, research methods, and maintain platforms."
    )

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Candidates must use design systems, research methods.",
        "Candidates must maintain platforms.",
    ]


def test_inherited_predicate_can_govern_its_own_noun_coordination() -> None:
    clause = (
        "Candidates must maintain platforms, use design systems and support tools, "
        "and develop reports."
    )

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == [
        "Candidates must maintain platforms.",
        "Candidates must use design systems and support tools.",
        "Candidates must develop reports.",
    ]


def test_postpositive_shared_noun_head_is_not_an_expression_boundary() -> None:
    clause = "Knowledge of design and support tools is required."

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert parsed.role.must_have_requirements == [clause]
    assert expression is not None
    assert expression.operator == "claim"
    assert expression.text == clause


def test_postmodified_shared_noun_head_is_not_an_expression_boundary() -> None:
    clause = "Knowledge of design and support tools for reporting is required."

    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert parsed.role.must_have_requirements == [clause]
    assert expression is not None
    assert expression.operator == "claim"
    assert expression.text == clause


def test_predicate_led_or_branch_keeps_boolean_precedence_with_serial_members() -> None:
    clause = (
        "Candidates must know Python or hold a degree, have experience, "
        "and be certified."
    )
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]
    assert expression.children[0].text == "Candidates must know Python"
    assert [child.text for child in expression.children[1].children] == [
        "hold a degree",
        "have experience",
        "be certified.",
    ]


def test_parallel_head_alternative_keeps_boolean_precedence() -> None:
    clause = (
        "Candidates must have Python experience or SQL experience and hold a degree."
    )
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "all_of"]


def test_explicit_alternatives_recursively_parse_inner_conjunctions() -> None:
    clause = "Candidates must either know Python and SQL or know Java and Go."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["all_of", "all_of"]
    assert [child.text for child in expression.children[0].children] == [
        "know Python",
        "SQL",
    ]
    assert [child.text for child in expression.children[1].children] == [
        "know Java",
        "Go.",
    ]


@pytest.mark.parametrize(
    "range_text",
    [
        "between 3 and 5 years of experience",
        "between Monday and Friday",
    ],
)
def test_between_range_and_is_not_a_logical_conjunction(range_text: str) -> None:
    clause = f"Candidates must either have {range_text} or hold a degree."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]
    assert [child.text for child in expression.children] == [
        f"have {range_text}",
        "hold a degree.",
    ]


@pytest.mark.parametrize(
    "serial_list",
    [
        "know Python, SQL, and Java",
        "know Python, SQL and Java",
    ],
)
def test_serial_conjunction_has_one_child_per_member(serial_list: str) -> None:
    clause = f"Candidates must either ({serial_list}) or know Go."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    conjunction = expression.children[0]
    assert conjunction.operator == "all_of"
    assert [child.text for child in conjunction.children] == [
        "know Python",
        "SQL",
        "Java",
    ]
    assert expression.children[1].text == "know Go."


def test_coordinated_supplement_stays_inside_its_outer_alternative() -> None:
    clause = (
        "Candidates must know Python and either hold a degree, preferably in "
        "Computer Science, Engineering, or Mathematics, or have equivalent "
        "experience."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    alternatives = expression.children[1]
    assert alternatives.operator == "any_of"
    assert [child.text for child in alternatives.children] == [
        (
            "hold a degree, preferably in Computer Science, Engineering, or "
            "Mathematics"
        ),
        "have equivalent experience.",
    ]


def test_coordinated_supplement_does_not_depend_on_outer_predicate_lexicon() -> None:
    clause = (
        "Candidates must know Python and either hold a degree, preferably in "
        "Computer Science, Engineering, or Mathematics, or bring equivalent "
        "experience."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    alternatives = expression.children[1]
    assert alternatives.operator == "any_of"
    assert [child.text for child in alternatives.children] == [
        (
            "hold a degree, preferably in Computer Science, Engineering, or "
            "Mathematics"
        ),
        "bring equivalent experience.",
    ]


@pytest.mark.parametrize(
    "outer_alternative",
    [
        "submit a portfolio",
        "furnish equivalent credentials",
        "relocate to Toronto",
    ],
)
def test_supplement_closes_before_an_open_predicate_alternative(
    outer_alternative: str,
) -> None:
    clause = (
        "Candidates must either hold a degree, preferably in Computer Science, "
        f"or {outer_alternative}."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.text for child in expression.children] == [
        "hold a degree, preferably in Computer Science",
        f"{outer_alternative}.",
    ]


def test_supplement_does_not_consume_later_outer_alternatives() -> None:
    clause = (
        "Candidates must either hold a degree, preferably in Computer Science, "
        "or have equivalent experience, or obtain certification."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.text for child in expression.children] == [
        "hold a degree, preferably in Computer Science",
        "have equivalent experience",
        "obtain certification.",
    ]


def test_supplemental_modifier_stays_with_its_conjunct() -> None:
    clause = (
        "Candidates must either (hold a degree, preferably in Computer Science, "
        "and have five years of experience) or know Python."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    conjunction = expression.children[0]
    assert conjunction.operator == "all_of"
    assert [child.text for child in conjunction.children] == [
        "hold a degree, preferably in Computer Science",
        "have five years of experience",
    ]


def test_unmatched_delimiter_does_not_hide_a_later_alternative() -> None:
    clause = (
        "Candidates must know Python and either use the Acme (Legacy platform "
        "or use SQL."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    alternatives = expression.children[1]
    assert alternatives.operator == "any_of"
    assert [child.text for child in alternatives.children] == [
        "use the Acme (Legacy platform",
        "use SQL.",
    ]


def test_nested_or_does_not_change_the_top_level_claim_conjunction() -> None:
    clause = (
        "Candidates must have Python experience and be authorized to work in "
        "Canada or the United States."
    )
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]


def test_quantitative_or_more_is_not_a_logical_alternative() -> None:
    clause = "Candidates must have 3 or more years of Python and SQL experience."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    evidence = parsed.role.evidence[0]
    assert evidence.claims[0].coordination == "all_of"
    assert evidence.requirement_expression is not None
    assert evidence.requirement_expression.operator == "claim"


@pytest.mark.parametrize("quantity", ["3 or 4", "three or four"])
def test_discrete_quantitative_range_is_not_a_logical_alternative(
    quantity: str,
) -> None:
    clause = f"Candidates must have {quantity} years of Python experience."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    evidence = parsed.role.evidence[0]
    assert evidence.claims[0].coordination == "single"
    assert evidence.requirement_expression is not None
    assert evidence.requirement_expression.operator == "claim"


def test_top_level_or_between_claims_remains_a_disjunction() -> None:
    clause = "Candidates must know Python or be authorized to work in Canada."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]


def test_claims_include_rule_level_decision_trace() -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\nPython is required in Quebec."
    )

    trace = parsed.role.evidence[0].claims[0].decision_trace
    assert "source_section:qualifications" in trace
    assert "kind:qualification" in trace
    assert "modality:mandatory" in trace
    assert "condition:geographic" in trace


def test_capability_environment_trace_uses_the_computed_polarity() -> None:
    parsed = parse_job_text("Title: Developer\nTechnologies:\nNo Java.")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.polarity == "negative"
    assert f"polarity:{claim.polarity}" in claim.decision_trace
    assert f"coordination:{claim.coordination}" in claim.decision_trace


def test_iso_country_code_is_a_geographic_condition() -> None:
    parsed = parse_job_text(
        "Title: Bilingual Agent\nQualifications:\nFrench is required in CAN."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == "in CAN"
    assert claim.condition_type == "geographic"


@pytest.mark.parametrize(
    ("clause", "condition_text"),
    [
        ("French is required in Waterloo.", "in Waterloo"),
        ("In Waterloo, French is required.", "In Waterloo"),
        ("Python experience is required in Waterloo.", "in Waterloo"),
        ("French is required in Rio de Janeiro.", "in Rio de Janeiro"),
        ("In Rio de Janeiro, French is required.", "In Rio de Janeiro"),
    ],
)
def test_proper_city_name_is_a_geographic_condition(
    clause: str,
    condition_text: str,
) -> None:
    parsed = parse_job_text(f"Title: Bilingual Agent\nQualifications:\n{clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == condition_text
    assert claim.condition_type == "geographic"


@pytest.mark.parametrize(
    ("clause", "condition_text"),
    [
        ("French is required in Quebec or Ontario.", "in Quebec or Ontario"),
        (
            "Candidates must be located in Toronto or Vancouver.",
            "in Toronto or Vancouver",
        ),
        (
            "Candidates must work in Toronto, Vancouver, or Montreal.",
            "in Toronto, Vancouver, or Montreal",
        ),
        (
            "Candidates must be located in Waterloo or Kitchener.",
            "in Waterloo or Kitchener",
        ),
    ],
)
def test_coordinated_locations_are_one_geographic_condition(
    clause: str,
    condition_text: str,
) -> None:
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == condition_text
    assert claim.condition_type == "geographic"


def test_region_suffix_stays_inside_a_coordinated_location_requirement() -> None:
    clause = "Candidates must reside in Denver or Boulder, Colorado, and hold a degree."
    parsed = parse_job_text(f"Title: Developer\nQualifications:\n{clause}")

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "all_of"
    assert [child.text for child in expression.children] == [
        "Candidates must reside in Denver or Boulder, Colorado",
        "hold a degree.",
    ]


@pytest.mark.parametrize(
    "clause",
    [
        "In Engineering, Python is required.",
        "In Sales, French is required.",
        "French is required in Sales and Marketing.",
        "In Core Platform, Python is required.",
        "In Commercial Banking, Python is required.",
        "In Risk Analytics, Python is required.",
        "In Team Alpha, Python is required.",
    ],
)
def test_department_names_are_not_a_geographic_condition(clause: str) -> None:
    parsed = parse_job_text(f"Title: Bilingual Agent\nQualifications:\n{clause}")

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text is None
    assert claim.condition_type is None


@pytest.mark.parametrize("city", ["Waterloo", "WATERLOO"])
def test_city_region_pair_is_a_geographic_condition(city: str) -> None:
    parsed = parse_job_text(
        "Title: Bilingual Agent\nQualifications:\n"
        f"French is required in {city}, Ontario."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == f"in {city}, Ontario"
    assert claim.condition_type == "geographic"


@pytest.mark.parametrize(
    ("location", "condition_text"),
    [
        ("Toronto", "in Toronto"),
        ("WATERLOO, Ontario", "in WATERLOO, Ontario"),
    ],
)
def test_validated_location_outweighs_the_ambiguous_work_in_frame(
    location: str,
    condition_text: str,
) -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        f"Candidates must work in {location}."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == condition_text
    assert claim.condition_type == "geographic"


@pytest.mark.parametrize("capability", ["COBOL", "Simulink"])
def test_novel_capability_name_is_not_a_geographic_condition(
    capability: str,
) -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        f"Candidates must work in {capability}."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text is None
    assert claim.condition_type is None


@pytest.mark.parametrize(
    "compound",
    [
        "research and development experience",
        "research, design, and development experience",
        "design and implementation skills",
        "sales and marketing expertise",
    ],
)
def test_nominal_compound_inside_alternative_remains_one_requirement(
    compound: str,
) -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        f"Candidates must either have {compound} or hold a degree."
    )

    expression = parsed.role.evidence[0].requirement_expression
    assert expression is not None
    assert expression.operator == "any_of"
    assert [child.operator for child in expression.children] == ["claim", "claim"]
    assert expression.children[0].text == f"have {compound}"


@pytest.mark.parametrize("period", ["January", "Summer 2027", "Q1 2027", "2027"])
def test_in_temporal_period_is_not_a_geographic_condition(period: str) -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        f"In {period}, Python is required."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text == f"In {period}"
    assert claim.condition_type == "temporal"


@pytest.mark.parametrize(
    "organizational_name",
    ["Software Engineering", "Global Sales", "Production"],
)
def test_modified_organizational_name_is_not_a_geographic_condition(
    organizational_name: str,
) -> None:
    parsed = parse_job_text(
        "Title: Developer\nQualifications:\n"
        f"In {organizational_name}, Python is required."
    )

    claim = parsed.role.evidence[0].claims[0]
    assert claim.condition_text is None
    assert claim.condition_type is None


@pytest.mark.parametrize(
    "clause",
    [
        "Export reports to Excel.",
        "Save the forecast as Excel.",
        "Write the results to Excel.",
    ],
)
def test_excel_output_complement_is_a_capability(clause: str) -> None:
    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert "Excel" in parsed.role.capabilities
    assert "Excel" not in parsed.role.tech_stack


@pytest.mark.parametrize(
    "clause",
    [
        "Our platform exports reports to Excel.",
        "Our platform exports reports to Microsoft Excel.",
        "Our platform uses Excel.",
        "Their system exports reports to Excel.",
        "Our people analytics platform exports reports to Excel.",
        "The system saves data to Excel.",
        "Our reporting service writes files as Excel.",
    ],
)
def test_company_excel_output_is_not_a_candidate_capability(clause: str) -> None:
    parsed = parse_job_text(f"Title: Marketing Intern\n{clause}")

    assert "Excel" not in parsed.role.capabilities
    assert "finance" not in parsed.role.capability_domains


@pytest.mark.parametrize(
    "clause",
    [
        "Write a plan to excel in this role.",
        "Write a report to excel in this role.",
        "Write clear instructions to excel at the assignment.",
        "Write a guide to excel professionally.",
        "Develop the skills to excel in this role.",
        "Build the expertise to excel professionally.",
    ],
)
def test_infinitive_excel_after_an_output_verb_is_not_a_capability(clause: str) -> None:
    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert "Excel" not in parsed.role.capabilities


@pytest.mark.parametrize(
    "clause",
    [
        "You will report to a project manager.",
        "You will report to a senior project manager.",
        "You will report directly to the project manager.",
        "You will report into the project manager.",
        "You will report functionally and directly to the project manager.",
        "You reported indirectly to a project manager.",
        "You will work under our assigned project manager.",
        "You will partner with the project manager.",
        "You will work alongside the project manager.",
        "You will have a dotted-line reporting relationship to the project manager.",
    ],
)
def test_project_manager_governed_as_a_person_is_not_a_capability(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nResponsibilities:\n{clause}")

    assert "Project Management" not in parsed.role.capabilities


def test_reporting_line_does_not_hide_a_distinct_project_management_requirement() -> None:
    parsed = parse_job_text(
        "Title: Analyst\n"
        "Responsibilities:\n"
        "You will report directly to the project manager.\n"
        "Qualifications:\n"
        "Project management experience is required."
    )

    assert "Project Management" in parsed.role.capabilities
    assert "Project Management" in parsed.role.required_capabilities


@pytest.mark.parametrize(
    ("section", "clause", "field"),
    [
        (
            "Responsibilities",
            "Clean and organize customer and opportunity data.",
            "responsibilities",
        ),
        (
            "Qualifications",
            "Strong self-motivation and organization skills.",
            "must_have_requirements",
        ),
    ],
)
def test_nominal_coordinate_with_owner_homonym_remains_one_claim(
    section: str,
    clause: str,
    field: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\n{section}:\n{clause}")

    assert getattr(parsed.role, field) == [clause]
    assert len(parsed.role.evidence[0].claims) == 1


def test_inverted_should_condition_does_not_become_a_requirement() -> None:
    clause = (
        "Should an individual with the required qualifications not be available, "
        "applicants with closely related qualifications may be considered."
    )
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == []
    assert len(parsed.role.evidence[0].claims) == 1
    claim = parsed.role.evidence[0].claims[0]
    assert claim.modality == "optional"
    assert claim.condition_text == (
        "Should an individual with the required qualifications not be available"
    )
    assert claim.condition_type == "contingency"


@pytest.mark.parametrize(
    "clause",
    [
        "Must plan to return to school after the work term.",
        "Employees must reside within Canada.",
    ],
)
def test_unsectioned_deontic_eligibility_action_is_a_requirement(
    clause: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\n{clause}")

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [clause]


def test_explicit_candidate_action_can_remain_an_unsectioned_duty() -> None:
    clause = "You must build reliable APIs."
    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.responsibilities == [clause]
    assert parsed.role.must_have_requirements == []


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        (
            "Experience with SQL is not required but is considered an asset.",
            "Experience with SQL is considered an asset.",
        ),
        (
            "Experience with SQL is not required but would be an asset.",
            "Experience with SQL would be an asset.",
        ),
    ],
)
def test_finite_preference_ellipsis_inherits_only_the_subject(
    clause: str,
    expected: str,
) -> None:
    parsed = parse_job_text(f"Title: Analyst\nQualifications:\n{clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == [expected]


def test_mentorship_promise_preserves_coordinated_duty() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "You will receive one-on-one mentorship and collaborate with engineering."
    )

    assert parsed.role.responsibilities == [
        "You will collaborate with engineering."
    ]
    assert parsed.role.must_have_requirements == []


def test_training_promise_with_internal_nominal_coordination_preserves_duty() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "You will receive one-on-one and group training and coaching from experts "
        "and take on complex challenges."
    )

    assert parsed.role.responsibilities == [
        "You will take on complex challenges."
    ]
    assert parsed.role.must_have_requirements == []


def test_right_node_shared_prepositional_coordinate_keeps_one_preference() -> None:
    clause = (
        "Bonus: A strong understanding of and experience in the healthcare "
        "industry an asset."
    )

    parsed = parse_job_text(f"Title: Data Science Intern\nQualifications\n{clause}")

    assert parsed.role.must_have_requirements == []
    assert parsed.role.nice_to_have_requirements == [clause]
    assert len(parsed.role.evidence[0].claims) == 1
    assert parsed.role.evidence[0].claims[0].modality == "preferred"


def test_promise_object_and_relative_predicates_stay_under_the_promise() -> None:
    parsed = parse_job_text(
        "Title: Innovation Analyst\n"
        "You will receive one-on-one mentorship from Enterprise Innovation and "
        "Example Lab's business analysts, who will guide you through the process "
        "and set you up for success."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []


def test_embedded_negation_does_not_become_a_distributed_matrix_modal() -> None:
    clause = (
        "Strong problem-solving skills - you can break down a problem, research "
        "what you don't know, and figure it out"
    )

    parsed = parse_job_text(f"Title: Software Engineer Intern\nQualifications\n{clause}")

    assert parsed.role.must_have_requirements == [clause]
    assert len(parsed.role.evidence[0].claims) == 1
    assert parsed.role.evidence[0].claims[0].polarity == "positive"


@pytest.mark.parametrize(
    "promise",
    [
        "You can expect to receive coaching and mentoring to develop your skills.",
        "You will receive training and coaching from experienced engineers.",
        "You will have the opportunity to learn from experienced engineers.",
    ],
)
def test_real_candidate_experience_promises_are_not_duties(promise: str) -> None:
    parsed = parse_job_text(f"Title: Developer\n{promise}")

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []


def test_wrapped_unsectioned_role_promise_is_not_a_duty() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "This role will provide practical experience of creating the\n"
        "infrastructure that supports trading."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []


def test_wrapped_unsectioned_responsibility_is_reflowed() -> None:
    posting_text = (
        "Title: Developer\n"
        "You will work closely with experienced developers who will provide\n"
        "guidance, mentorship, and regular feedback."
    )

    parsed = parse_job_text(posting_text)

    assert parsed.role.responsibilities == [
        "You will work closely with experienced developers who will provide "
        "guidance, mentorship, and regular feedback."
    ]
    evidence = parsed.role.evidence[0]
    assert posting_text[evidence.evidence_start : evidence.evidence_end] == (
        evidence.evidence_text
    )


def test_unsectioned_mandatory_action_is_a_requirement() -> None:
    clause = "Summer student candidates must meet the following criteria:"

    parsed = parse_job_text(f"Title: Analyst\n{clause}")

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [clause]


def test_wrapped_conditional_eligibility_remains_one_requirement() -> None:
    parsed = parse_job_text(
        "Title: Analyst\nQualifications:\n"
        "You need to be legally eligible to work at the location specified above "
        "and, where applicable, must\n"
        "have a valid work permit."
    )

    assert parsed.role.must_have_requirements == [
        "You need to be legally eligible to work at the location specified above "
        "and, where applicable, must have a valid work permit."
    ]


def test_wrapped_participial_responsibility_ignores_embedded_requirement_verb() -> None:
    parsed = parse_job_text(
        "Title: Engineer\nResponsibilities:\n"
        "Participates in support actions related to the maintenance of\n"
        "production systems, while also engineering solutions that require "
        "minimal support."
    )

    assert parsed.role.responsibilities == [
        "Participates in support actions related to the maintenance of production "
        "systems, while also engineering solutions that require minimal support."
    ]
    assert parsed.role.must_have_requirements == []


def test_wrapped_mandatory_predicate_series_stays_in_one_requirement() -> None:
    parsed = parse_job_text(
        "Title: Developer\nMust-have\n"
        "Must be a self-starter, with strong analytical skills, as well as the "
        "ability to work in a fast-paced environment and manage multiple\n"
        "priorities."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [
        "Must be a self-starter, with strong analytical skills, as well as the "
        "ability to work in a fast-paced environment and manage multiple priorities."
    ]


def test_wrapped_embedded_modal_responsibility_is_reflowed() -> None:
    parsed = parse_job_text(
        "Title: Analyst\n"
        "businesses and supporting delivery for our business partners. "
        "You may also need to\n"
        "support internal operations (e.g., Asset Management, Knowledge\n"
        "Management). The organization provides services."
    )

    assert parsed.role.responsibilities == [
        "You may also need to support internal operations "
        "(e.g., Asset Management, Knowledge Management)."
    ]


def test_truncated_modal_before_portal_faq_is_not_a_requirement() -> None:
    parsed = parse_job_text(
        "Title: Security Analyst\nQualifications\n"
        "Success in this position requires attention to detail; you must\n"
        "Frequently Asked Questions\n"
        "Job Description\n"
        "Careers https://jobs.example.test/posting"
    )

    assert parsed.role.must_have_requirements == [
        "Success in this position requires attention to detail"
    ]


def test_wrapped_preposed_adjunct_with_modal_is_reflowed() -> None:
    posting_text = (
        "Title: Developer\n"
        "As you progress through the work terms, you might\n"
        "work with our development team."
    )
    parsed = parse_job_text(posting_text)
    document = parse_role_document(posting_text)

    assert parsed.role.responsibilities == []
    assert any(
        fragment.text
        == "As you progress through the work terms, you might\n"
        "work with our development team."
        for _section, fragment in document.source_fragments
    )


def test_wrapped_candidate_experience_promise_reflows_to_sentence_boundary() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "Candidates can expect an onsite and\n"
        "hands-on technical role where you will\n"
        "gain real-world and practical\n"
        "engineering experience in a research department."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []


def test_in_this_role_you_will_heading_opens_responsibilities() -> None:
    parsed = parse_job_text(
        "Title: Developer\nAbout Us\nWe build controls.\n"
        "In this role you will:\n"
        "• Develop and maintain backend services."
    )

    assert parsed.role.responsibilities == [
        "Develop and maintain backend services."
    ]


def test_lowercase_company_paragraph_continuation_is_not_a_duty() -> None:
    parsed = parse_job_text(
        "Title: Analyst\n"
        "The company delivers comprehensive operational and customer\n"
        "support solutions to the regional aircraft industry. "
        "Our services include maintenance.\n"
        "With a network of service centers,\n"
        "support offices and depots, we're positioned to help customers."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == []


def test_pdf_wrapped_bullet_is_one_responsibility_with_exact_evidence() -> None:
    posting_text = (
        "Title: Developer\n"
        "In this role you will:\n"
        "• Work closely under the mentorship of our Website\n"
        "Operations Manager and team to continue development of our\n"
        "company website\n"
        "• Develop and maintain backend services."
    )

    parsed = parse_job_text(posting_text)

    assert parsed.role.responsibilities == [
        "Work closely under the mentorship of our Website Operations Manager "
        "and team to continue development of our company website",
        "Develop and maintain backend services.",
    ]
    evidence = parsed.role.evidence[0]
    assert evidence.evidence_text == (
        "Work closely under the mentorship of our Website\n"
        "Operations Manager and team to continue development of our\n"
        "company website"
    )
    assert posting_text[evidence.evidence_start : evidence.evidence_end] == (
        evidence.evidence_text
    )


def test_job_openings_ends_role_content_before_portal_footer() -> None:
    parsed = parse_job_text(
        "Title: Developer\n"
        "In this role you will:\n"
        "• Develop and maintain backend services.\n"
        "Job Openings\n"
        "Net-Zero Emissions by\n"
        "2050\n"
        "Careers https://jobs.example.test/posting"
    )

    assert parsed.role.responsibilities == [
        "Develop and maintain backend services."
    ]
    assert parsed.role.must_have_requirements == []


def test_job_openings_navigation_prefix_does_not_hide_the_posting() -> None:
    parsed = parse_job_text(
        "Title: Embedded Developer\n"
        "Job Openings (/)> Embedded Developer\n"
        "Responsibilities:\n"
        "• Develop and test embedded systems."
    )

    assert parsed.role.responsibilities == [
        "Develop and test embedded systems."
    ]


def test_in_this_role_you_will_sentence_is_not_a_heading() -> None:
    clause = (
        "In this role you will learn about how we build these environments, "
        "connect to customers and optimize for performance."
    )

    parsed = parse_job_text(f"Title: Developer\n{clause}")

    assert parsed.role.responsibilities == [clause]


def test_role_environment_statement_completes_a_partial_technology_projection() -> None:
    parsed = parse_job_text(
        "Title: AI Software Engineer Intern\n"
        "You will translate requirements into solutions. These tools are built "
        "with a cloud-first mindset and\n"
        "leverage public cloud platforms such as AWS, Azure, and Google Cloud, "
        "along with modern automation and integration patterns.\n"
        "Responsibilities\n"
        "You will build developer tooling.\n"
        "Qualifications\n"
        "Experience with Azure."
    )

    assert parsed.role.tech_stack == ["AWS", "Azure", "GCP"]
    assert parsed.role.environment_capabilities == ["AWS", "Azure", "GCP"]


def test_company_platform_context_does_not_complete_the_role_technology_stack() -> None:
    parsed = parse_job_text(
        "Title: Test Engineer\n"
        "Example Corp is pioneering AWS-compatible storage for multicloud customers.\n"
        "Responsibilities\n"
        "You will test Java services."
    )

    assert parsed.role.tech_stack == ["Java"]
    assert "AWS" not in parsed.role.capabilities


def test_wrapped_attributive_complement_is_reflowed_before_classification() -> None:
    clause = (
        "A single applicant should not create multiple email addresses to apply "
        "to additional opportunities."
    )
    parsed = parse_job_text(
        "Title: Engineering Intern\n"
        "A single applicant should not create multiple email addresses to apply "
        "to additional\n"
        "opportunities."
    )

    assert parsed.role.responsibilities == []
    assert parsed.role.must_have_requirements == [clause]


def test_expected_relocation_is_a_requirement_but_expected_work_is_a_duty() -> None:
    relocation = parse_job_text(
        "Title: Quality Engineer Intern\n"
        "Successful candidates are expected to arrange relocation at their own expense."
    )
    duty = parse_job_text(
        "Title: Engineering Lead\nYou are expected to mentor junior engineers."
    )

    assert relocation.role.responsibilities == []
    assert relocation.role.must_have_requirements == [
        "Successful candidates are expected to arrange relocation at their own expense."
    ]
    assert duty.role.responsibilities == [
        "You are expected to mentor junior engineers."
    ]
    assert duty.role.must_have_requirements == []


def test_role_language_that_mentions_administrative_terms_is_preserved() -> None:
    parsed = parse_job_text(
        "Title: Platform Engineer\n"
        "Responsibilities\n"
        "Manage cookie-consent settings for customer sites.\n"
        "Monitor application status transitions in the workflow engine.\n"
        "Conduct accessibility testing for the web application.\n"
        "Analyze compensation ranges for the reporting service."
    )

    assert parsed.role.responsibilities == [
        "Manage cookie-consent settings for customer sites.",
        "Monitor application status transitions in the workflow engine.",
        "Conduct accessibility testing for the web application.",
        "Analyze compensation ranges for the reporting service.",
    ]
