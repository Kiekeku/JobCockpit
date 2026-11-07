from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


ROLE_INTEREST_MODIFIERS = frozenset(
    {
        "co-op",
        "coop",
        "intern",
        "internship",
        "student",
        "summer",
    }
)

# Preserve recall for ambiguous role-family titles without letting broad domain
# words such as "data" or "analytics" qualify unrelated student postings.
ROLE_FAMILY_ALIASES = {
    "analyst": frozenset({"data analyst"}),
}


@dataclass(frozen=True)
class RoleInterestMatch:
    matched: bool
    reason: str
    title: str
    matched_include: str | None = None
    matched_exclude: str | None = None


def clean_interest_keywords(values: list[str] | tuple[str, ...] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = " ".join(str(value).strip().split())
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


def profile_has_role_interest(profile_data: dict[str, Any] | None) -> bool:
    if not profile_data:
        return False
    return bool(
        clean_interest_keywords(profile_data.get("target_role_include_keywords"))
        or clean_interest_keywords(profile_data.get("target_role_exclude_keywords"))
    )


def match_title_interest(title: str | None, profile_data: dict[str, Any] | None) -> RoleInterestMatch:
    cleaned_title = " ".join(str(title or "").strip().split())
    if not cleaned_title:
        return RoleInterestMatch(True, "unknown_title", "")
    if not profile_has_role_interest(profile_data):
        return RoleInterestMatch(True, "no_interest_filter", cleaned_title)

    include_keywords = clean_interest_keywords((profile_data or {}).get("target_role_include_keywords"))
    exclude_keywords = clean_interest_keywords((profile_data or {}).get("target_role_exclude_keywords"))
    for keyword in exclude_keywords:
        if _keyword_matches(cleaned_title, keyword):
            return RoleInterestMatch(False, "excluded_keyword", cleaned_title, matched_exclude=keyword)

    if include_keywords:
        role_keywords = [
            keyword for keyword in include_keywords if keyword.casefold() not in ROLE_INTEREST_MODIFIERS
        ]
        for keyword in role_keywords:
            if _keyword_matches(cleaned_title, keyword):
                return RoleInterestMatch(True, "included_keyword", cleaned_title, matched_include=keyword)
        for alias, configured_roles in ROLE_FAMILY_ALIASES.items():
            if _keyword_matches(cleaned_title, alias) and any(
                keyword.casefold() in configured_roles for keyword in role_keywords
            ):
                return RoleInterestMatch(
                    True,
                    "included_role_family",
                    cleaned_title,
                    matched_include=alias,
                )
        return RoleInterestMatch(False, "missing_include_keyword", cleaned_title)

    return RoleInterestMatch(True, "no_include_keywords", cleaned_title)


def title_from_job_text(raw_text: str) -> str:
    for line in str(raw_text or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if ":" in cleaned:
            label, value = cleaned.split(":", 1)
            if label.strip().casefold() in {"title", "job title", "role", "position"}:
                return value.strip()
            continue
        return cleaned
    return ""


def match_job_text_interest(raw_text: str, profile_data: dict[str, Any] | None) -> RoleInterestMatch:
    return match_title_interest(title_from_job_text(raw_text), profile_data)


def _keyword_matches(title: str, keyword: str) -> bool:
    normalized_title = title.casefold()
    normalized_keyword = keyword.casefold()
    if re.fullmatch(r"[\w#+.-]+", normalized_keyword):
        return bool(re.search(rf"(?<![\w#+.-]){re.escape(normalized_keyword)}(?![\w#+.-])", normalized_title))
    return normalized_keyword in normalized_title
