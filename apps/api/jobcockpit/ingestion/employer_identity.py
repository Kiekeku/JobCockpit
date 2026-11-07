from __future__ import annotations

import re

from jobcockpit.ingestion.parser_models import ParsedJob
from jobcockpit.ingestion.posting_fields import _field_label


def resolve_corroborated_employer_identity(parsed: ParsedJob, posting_text: str) -> None:
    """Resolve the observed Frostbite brand only with two employer-owned statements."""
    employer = parsed.employer
    if (employer.company_name or "").strip().casefold() != "frostbite":
        return
    if (employer.division or "").strip().casefold() not in {"", "frostbite"}:
        return
    # The caller supplies the scoped posting. Provenance fields cannot corroborate
    # employer identity, even when they quote otherwise matching body text.
    body = "\n".join(
        line.strip() for line in posting_text.splitlines() if not _field_label(line)
    )
    if not re.search(
        r"^Frostbite\s+is\s+EA['’]s\s+proprietary\s+game\s+engine\b",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        return
    if not re.search(
        r"^Electronic\s+Arts\s+is\s+an\s+equal\s+opportunity\s+employer\b",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        return
    employer.company_name = "Electronic Arts"
    employer.division = "Frostbite"
