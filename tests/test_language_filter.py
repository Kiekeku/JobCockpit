from jobcockpit.ingestion.language_filter import evaluate_language_requirements


def profile(french: str = "none", enabled: bool = True) -> dict:
    return {
        "language_filter_enabled": enabled,
        "languages": [
            {"language": "English", "proficiency": "fluent"},
            {"language": "French", "proficiency": french},
        ],
    }


def test_language_filter_skips_strong_french_requirement_when_unmet():
    result = evaluate_language_requirements(
        "Software Developer Intern\nFrench required for this role.",
        profile("basic"),
    )

    assert result.should_skip is True
    assert result.reason == "unmet_required_language"
    assert result.required_languages == ["French"]
    assert result.unmet_languages == ["French"]


def test_language_filter_skips_bilingual_english_french_requirement_when_unmet():
    result = evaluate_language_requirements(
        "Data Analyst Intern\nMust be bilingual in English and French.",
        profile("intermediate"),
    )

    assert result.should_skip is True
    assert result.required_languages == ["French"]


def test_language_filter_allows_soft_french_mentions():
    for text in [
        "French is an asset.",
        "French preferred but not required.",
        "Bilingual would be an advantage.",
        "Nice to have: French language skills.",
    ]:
        result = evaluate_language_requirements(f"Software Developer Intern\n{text}", profile("none"))
        assert result.should_skip is False, text


def test_language_filter_treats_required_plus_other_skills_as_hard_requirement():
    result = evaluate_language_requirements(
        "Software Developer Intern\nFrench required plus English communication skills.",
        profile("none"),
    )

    assert result.should_skip is True
    assert result.reason == "unmet_required_language"
    assert result.required_languages == ["French"]
    assert result.unmet_languages == ["French"]


def test_language_filter_treats_required_asset_context_as_hard_requirement():
    for text in [
        "Software Developer Intern\nFrench required for asset management role.",
        "Software Developer Intern\nFrench required for digital asset platform work.",
        "Software Developer Intern\nFrench required, Python preferred.",
    ]:
        result = evaluate_language_requirements(text, profile("none"))
        assert result.should_skip is True, text
        assert result.reason == "unmet_required_language"
        assert result.required_languages == ["French"]
        assert result.unmet_languages == ["French"]


def test_language_filter_allows_met_french_requirement():
    result = evaluate_language_requirements(
        "Software Developer Intern\nMaitrise du francais requise.",
        profile("professional"),
    )

    assert result.should_skip is False
    assert result.required_languages == ["French"]
    assert result.unmet_languages == []


def test_language_filter_is_disabled_until_enabled():
    result = evaluate_language_requirements(
        "Software Developer Intern\nFrench required.",
        profile("none", enabled=False),
    )

    assert result.should_skip is False
    assert result.reason == "filter_disabled"
