import pytest

from jobcockpit.ingestion.role_interest import match_title_interest


def test_title_interest_matches_include_keywords():
    result = match_title_interest(
        "Software Developer Intern",
        {
            "target_role_include_keywords": ["software", "developer"],
            "target_role_exclude_keywords": ["accounting"],
        },
    )

    assert result.matched is True
    assert result.matched_include == "software"
    assert result.matched_exclude is None


@pytest.mark.parametrize("modifier", ["intern", "internship", "student", "summer", "co-op", "coop"])
def test_title_interest_does_not_treat_generic_modifiers_as_target_roles(modifier: str):
    result = match_title_interest(
        f"Pharmacy {modifier}",
        {
            "target_role_include_keywords": ["software", modifier],
            "target_role_exclude_keywords": [],
        },
    )

    assert result.matched is False
    assert result.reason == "missing_include_keyword"
    assert result.matched_include is None


@pytest.mark.parametrize("modifier", ["intern", "internship", "student", "summer", "co-op", "coop"])
def test_title_interest_allows_modifiers_with_a_target_role(modifier: str):
    result = match_title_interest(
        f"Software Developer {modifier}",
        {
            "target_role_include_keywords": ["software", modifier],
            "target_role_exclude_keywords": [],
        },
    )

    assert result.matched is True
    assert result.reason == "included_keyword"
    assert result.matched_include == "software"


@pytest.mark.parametrize("ambiguous_term", ["data", "analytics"])
def test_title_interest_does_not_treat_ambiguous_domain_terms_as_roles(
    ambiguous_term: str,
):
    result = match_title_interest(
        f"{ambiguous_term.title()} Provisioning Student",
        {
            "target_role_include_keywords": ["data analyst", "student"],
            "target_role_exclude_keywords": [],
        },
    )

    assert result.matched is False
    assert result.reason == "missing_include_keyword"


def test_title_interest_preserves_analyst_role_family_recall():
    result = match_title_interest(
        "Innovation Incentives Analyst Intern",
        {
            "target_role_include_keywords": ["data analyst", "intern"],
            "target_role_exclude_keywords": [],
        },
    )

    assert result.matched is True
    assert result.reason == "included_role_family"
    assert result.matched_include == "analyst"


def test_title_interest_exclusion_wins_over_analyst_role_family_recall():
    result = match_title_interest(
        "Business Analyst Intern",
        {
            "target_role_include_keywords": ["data analyst", "intern"],
            "target_role_exclude_keywords": ["business analyst"],
        },
    )

    assert result.matched is False
    assert result.reason == "excluded_keyword"
    assert result.matched_exclude == "business analyst"


def test_title_interest_skips_excluded_role_terms():
    result = match_title_interest(
        "Accounting Intern",
        {
            "target_role_include_keywords": ["intern"],
            "target_role_exclude_keywords": ["accounting"],
        },
    )

    assert result.matched is False
    assert result.reason == "excluded_keyword"
    assert result.matched_exclude == "accounting"


def test_title_interest_uses_token_boundaries_for_single_word_keywords():
    result = match_title_interest(
        "Internal Tools Engineer",
        {
            "target_role_include_keywords": ["engineer"],
            "target_role_exclude_keywords": ["intern"],
        },
    )

    assert result.matched is True
    assert result.matched_exclude is None


def test_title_interest_allows_unknown_titles():
    result = match_title_interest(
        "",
        {
            "target_role_include_keywords": ["software"],
            "target_role_exclude_keywords": ["accounting"],
        },
    )

    assert result.matched is True
    assert result.reason == "unknown_title"
