"""Reject observed non-posting inputs before extraction or fit analysis."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from jobcockpit.ingestion.ats_adapters.base import AtsProviderError


class IncompletePostingError(AtsProviderError):
    """Retrieval must recover a body rather than import this input."""


def posting_body_text(text: str) -> str:
    # Keep paragraph-shaped evidence even when it begins with "Location:".
    # Remove generated provenance, which must not make a shell look substantial.
    return "\n".join(line for line in text.splitlines() if not (
        re.match(r"^(?:Page Title|Source URL|Source Evidence|Apply URL|Title|"
                 r"(?:GitHub List|CS Jobs|Ats [A-Za-z ]+|Indeed Hint|Linkedin Hint) "
                 r"(?:Company|Role|Location|Date Posted|URL|Apply URL)|"
                 r"Indeed Source URL|Linkedin Source URL|Source Resolution Kind|"
                 r"Official Discovery (?:Status|Error)|Resolution Evidence|Email Row):", line, re.I)
        or (len(line) < 200 and re.match(r"^(?:Company|Location|Posted):", line, re.I))
    ))


def incomplete_posting_reason(text: str, source_url: str = "") -> str | None:
    text = posting_body_text(text)
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if ((host in {"interninsider.me", "www.interninsider.me"} and path.startswith("/companies/"))
            or (host in {"bebee.com", "www.bebee.com"} and re.search(r"/(?:companies|jobs/role)/", path))
            or (host.endswith("builtin.com") and path.endswith("/benefits"))):
        return "listing_page"
    # Notices can coexist with real bodies. Length alone never rejects a job.
    if len(text.split()) >= 140:
        return None
    if re.search(r"By clicking Agree & Join|Sign in to see who you already know|"
                 r"Request Blocked|Client Challenge|verify that you['’]re not a robot", text, re.I):
        return "login_or_challenge"
    if re.search(r"You need to enable JavaScript to run this app|"
                 r"Please switch to a supported browser|Loading\s*[….]|"
                 r"\bPost\s*(?:[:.\s]+)$", text, re.I):
        return "application_shell"
    if "${pageStateData." in text or "Powered by Collage" in text:
        return "listing_page"
    if re.search(r"Browse \d+ open internships|View jobs from .*Active jobs", text, re.I | re.S):
        return "listing_page"
    if "THIS POSITION HAS BEEN CLOSED! PLEASE CHOOSE ONE OF THE OPTIONS BELOW" in text:
        return "expired_page_without_description"
    if (re.search(r"This job (?:posting )?is (?:no longer|not) available", text, re.I)
            and not re.search(r"Responsibilities|Qualifications|Requirements", text, re.I)):
        return "expired_page_without_description"
    if re.search(r"Important Notice for Applicants and Recruiters|PUT YOUR TALENT\s+TO GOOD USE", text, re.I):
        return "listing_page"
    if ("PRIVACY|LEGAL|REQUIREMENTS|Artificial Intelligence" in text
            and "Set your preferences" in text):
        return "footer_only"
    if (re.search(r"All rights reserved", text, re.I)
            and re.search(r"Cookies? Policy|Cookie Setting", text, re.I)
            and (len(text.split()) < 45 or
                 re.search(r"Your privacy matters to us.*Accept all.*Reject all", text, re.I | re.S))):
        return "footer_only"
    if "Eluta URL:" in text:
        body = re.sub(r"(?m)^(?:Company|Location|Summary|Posted|Eluta URL|Direct URL|Apply URL):.*$", "", text)
        body = "\n".join(body.splitlines()[1:]).strip()
        if body.casefold() in {"", "overview"} or body.endswith(("...", "…")):
            return "truncated_excerpt"
    return None


def require_complete_posting(text: str, source_url: str = "") -> None:
    reason = incomplete_posting_reason(text, source_url)
    if reason:
        raise IncompletePostingError(502, f"Incomplete posting ({reason}); full-page retrieval required")
