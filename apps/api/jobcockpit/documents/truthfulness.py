from __future__ import annotations

import re
from typing import Any

from jobcockpit.db.models import GeneratedDocument

UNSUPPORTED_TERMS = {"kubernetes", "invented metric", "10x", "fortune 500"}


def run_truthfulness_check(
    documents: list[GeneratedDocument],
    profile_data: dict[str, Any] | None = None,
) -> dict:
    blocking_errors: list[str] = []
    warnings: list[str] = []
    combined = "\n".join(document.content_preview.lower() for document in documents)
    if any(term in combined for term in UNSUPPORTED_TERMS):
        blocking_errors.append("unsupported_claim")
    if _has_unapproved_contact_leak(combined, profile_data or {}):
        blocking_errors.append("private_data_leak")
    return {
        "status": "blocked" if blocking_errors else "passed",
        "blocking_errors": blocking_errors,
        "warnings": warnings,
    }


def _has_unapproved_contact_leak(text: str, profile_data: dict[str, Any]) -> bool:
    allowed_emails = {str(profile_data.get("email", "")).lower()}
    emails = set(re.findall(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text))
    if any(email not in allowed_emails for email in emails):
        return True

    allowed_phone = _digits(str(profile_data.get("phone", "")))
    phone_candidates = re.findall(r"\+?\d[\d\s().-]{7,}\d", text)
    for candidate in phone_candidates:
        digits = _digits(candidate)
        if len(digits) >= 10 and digits != allowed_phone:
            return True
    return False


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())
