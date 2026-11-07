from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

REQUIRED_PROFICIENCIES = {"professional", "fluent", "native"}

_SOFT_LANGUAGE_PATTERNS = (
    re.compile(
        r"\b(french|francais)\b(?:(?!\b(required|mandatory|essential|must|requis|required|requise|obligatoire)\b).){0,60}"
        r"\b(an asset|asset|preferred|preference|nice to have|would be an advantage|an advantage|advantage|not required|optional|a plus|is a plus|would be a plus|atout)\b"
    ),
    re.compile(
        r"\b(an asset|asset|preferred|preference|nice to have|would be an advantage|an advantage|advantage|not required|optional|a plus|is a plus|would be a plus|atout)\b"
        r"(?:(?!\b(required|mandatory|essential|must|requis|required|requise|obligatoire)\b).){0,60}\b(french|francais)\b"
    ),
    re.compile(
        r"\bbilingual(ism)?\b(?:(?!\b(required|mandatory|essential|must)\b).){0,60}"
        r"\b(an asset|asset|preferred|preference|nice to have|would be an advantage|an advantage|advantage|not required|optional|a plus|is a plus|would be a plus)\b"
    ),
    re.compile(
        r"\b(an asset|asset|preferred|preference|nice to have|would be an advantage|an advantage|advantage|not required|optional|a plus|is a plus|would be a plus)\b"
        r"(?:(?!\b(required|mandatory|essential|must)\b).){0,60}\bbilingual(ism)?\b"
    ),
)
_FRENCH_REQUIRED_PATTERNS = (
    re.compile(r"\bfrench\b.{0,80}\b(required|mandatory|essential)\b"),
    re.compile(r"\b(required|mandatory|essential)\b.{0,80}\bfrench\b"),
    re.compile(r"\bmust\b.{0,80}\b(french|speak french|write french|read french|communicate in french)\b"),
    re.compile(r"\b(french|francais)\b.{0,80}\b(requis|required|requise|obligatoire)\b"),
    re.compile(r"\bmaitrise\b.{0,40}\bfrancais\b.{0,40}\b(requise|requis|obligatoire)\b"),
    re.compile(r"\bfluent\b.{0,80}\benglish\b.{0,20}(and|/)\s*\bfrench\b"),
    re.compile(r"\bfluent\b.{0,80}\bfrench\b.{0,20}(and|/)\s*\benglish\b"),
    re.compile(r"\bbilingual\b.{0,80}\b(english\s*(and|/)\s*french|french\s*(and|/)\s*english)\b"),
    re.compile(r"\b(english\s*(and|/)\s*french|french\s*(and|/)\s*english)\b.{0,80}\bbilingual\b"),
    re.compile(r"\bbilingual(ism)?\b.{0,80}\b(required|mandatory|essential|must)\b"),
)


@dataclass(frozen=True)
class LanguageRequirementResult:
    should_skip: bool
    reason: str
    required_languages: list[str]
    unmet_languages: list[str]
    evidence: str = ""


def evaluate_language_requirements(
    posting_text: str,
    profile_data: dict[str, Any] | None,
    *,
    skip_language_filter: bool = False,
) -> LanguageRequirementResult:
    if skip_language_filter:
        return LanguageRequirementResult(False, "filter_skipped", [], [])
    if not profile_data or not profile_data.get("language_filter_enabled"):
        return LanguageRequirementResult(False, "filter_disabled", [], [])

    required_languages, evidence = detect_required_languages(posting_text)
    if not required_languages:
        return LanguageRequirementResult(False, "no_required_language", [], [])

    proficiency_by_language = _profile_language_proficiencies(profile_data)
    unmet_languages = [
        language
        for language in required_languages
        if proficiency_by_language.get(_language_key(language), "none") not in REQUIRED_PROFICIENCIES
    ]
    if unmet_languages:
        return LanguageRequirementResult(
            True,
            "unmet_required_language",
            required_languages,
            unmet_languages,
            evidence,
        )
    return LanguageRequirementResult(False, "requirements_met", required_languages, [], evidence)


def detect_required_languages(posting_text: str) -> tuple[list[str], str]:
    normalized_text = _normalize_text(posting_text)
    for original_chunk, normalized_chunk in _language_requirement_chunks(posting_text, normalized_text):
        if _is_soft_language_mention(normalized_chunk):
            continue
        if any(pattern.search(normalized_chunk) for pattern in _FRENCH_REQUIRED_PATTERNS):
            return ["French"], _compact_evidence(original_chunk)
    return [], ""


def language_filter_error_message(result: LanguageRequirementResult) -> str:
    if not result.unmet_languages:
        return result.reason
    return f"{result.reason}: {', '.join(result.unmet_languages)}"


def _profile_language_proficiencies(profile_data: dict[str, Any]) -> dict[str, str]:
    proficiencies: dict[str, str] = {}
    for item in profile_data.get("languages") or []:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip()
        proficiency = str(item.get("proficiency") or "none").strip().casefold()
        if not language:
            continue
        proficiencies[_language_key(language)] = proficiency
    return proficiencies


def _language_key(language: str) -> str:
    normalized = _normalize_text(language)
    if normalized in {"fr", "francais"}:
        return "french"
    if normalized in {"en", "anglais"}:
        return "english"
    return normalized


def _language_requirement_chunks(original_text: str, normalized_text: str) -> list[tuple[str, str]]:
    original_chunks = [chunk.strip() for chunk in re.split(r"[\n.;:,()]+", original_text) if chunk.strip()]
    normalized_chunks = [chunk.strip() for chunk in re.split(r"[\n.;:,()]+", normalized_text) if chunk.strip()]
    if len(original_chunks) == len(normalized_chunks):
        return list(zip(original_chunks, normalized_chunks, strict=True))
    return [(original_text, normalized_text)]


def _is_soft_language_mention(chunk: str) -> bool:
    return any(pattern.search(chunk) for pattern in _SOFT_LANGUAGE_PATTERNS)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(ascii_text.casefold().split())


def _compact_evidence(value: str, limit: int = 200) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
