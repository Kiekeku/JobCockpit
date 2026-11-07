from __future__ import annotations

import re
from dataclasses import dataclass

from jobcockpit.ingestion.posting_document import punctuation_boundaries

DOCUMENT_ORDER = (
    "resume",
    "cover_letter",
    "transcript",
    "portfolio",
    "writing_sample",
    "references",
)
APPLICATION_DOCUMENT_AMBIGUITY = "application.documents_required"

_DOCUMENT_PATTERNS = {
    "resume": re.compile(
        r"\b(?:resume|r[ée]sum[ée]|curriculum\s+vitae|cv)\b",
        re.IGNORECASE,
    ),
    "cover_letter": re.compile(
        r"\b(?:cover\s+letter|letter\s+of\s+interest|motivation\s+letter)\b",
        re.IGNORECASE,
    ),
    "transcript": re.compile(
        r"\b(?:academic\s+|official\s+|unofficial\s+)?transcripts?\b",
        re.IGNORECASE,
    ),
    "portfolio": re.compile(r"\bportfolios?\b", re.IGNORECASE),
    "writing_sample": re.compile(r"\bwriting\s+samples?\b", re.IGNORECASE),
    "references": re.compile(
        r"\b(?:professional\s+references?|references|reference\s+list)\b",
        re.IGNORECASE,
    ),
}
_CLAUSE_BOUNDARY = re.compile(r"[\n\r.!?;]+")
_ANY_DOCUMENT_ALIAS = "(?:" + "|".join(
    pattern.pattern for pattern in _DOCUMENT_PATTERNS.values()
) + ")"
_DOCUMENT_LIST_SEPARATOR = r"(?:\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+)"


@dataclass(frozen=True)
class RequiredDocumentResolution:
    documents_required: tuple[str, ...] | None
    resume_required: bool | None
    cover_letter_required: bool | None
    ambiguous: bool = False


def parse_required_documents(text: str) -> RequiredDocumentResolution:
    normalized = _normalize_text(text)
    states: dict[str, str] = {}
    requirements_stated = False

    for document in DOCUMENT_ORDER:
        pattern = _DOCUMENT_PATTERNS[document]
        evidence: set[str] = set()
        for match in pattern.finditer(normalized):
            clause = _document_clause(normalized, match.start(), match.end())
            classification = _classify_document_clause(
                clause,
                pattern.pattern,
                document=document,
            )
            if classification is not None:
                evidence.add(classification)

        if evidence:
            requirements_stated = True
        if evidence == {"required", "not_required"}:
            states[document] = "ambiguous"
        elif "required" in evidence:
            states[document] = "required"
        elif "not_required" in evidence:
            states[document] = "not_required"
        else:
            states[document] = "unknown"

    if not requirements_stated:
        return RequiredDocumentResolution(None, None, None)

    documents = tuple(
        document for document in DOCUMENT_ORDER if states[document] == "required"
    )
    return RequiredDocumentResolution(
        documents_required=documents,
        resume_required=_boolean_state(states["resume"]),
        cover_letter_required=_boolean_state(states["cover_letter"]),
        ambiguous=any(state == "ambiguous" for state in states.values()),
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\u00a0", " "))


def _document_clause(text: str, start: int, end: int) -> str:
    left_boundary = 0
    boundaries = punctuation_boundaries(text, _CLAUSE_BOUNDARY.pattern)
    for boundary_start, boundary_end in boundaries:
        if boundary_end <= start:
            left_boundary = boundary_end
    right_boundary = next(
        (boundary_start for boundary_start, _ in boundaries if boundary_start >= end),
        len(text),
    )
    clause = text[left_boundary:right_boundary].strip()
    if len(clause) <= 360:
        return clause
    relative_start = start - left_boundary
    return clause[max(0, relative_start - 180) : relative_start + (end - start) + 180]


def _classify_document_clause(
    clause: str,
    alias: str,
    *,
    document: str,
) -> str | None:
    if (
        _has_post_application_context(clause)
        and not _has_application_anchor(clause)
    ) or _has_incidental_role_context(clause, alias):
        return None
    if _has_optional_or_negative_cue(clause, alias):
        return "not_required"
    if _has_required_cue(clause, alias, document=document):
        return "required"
    return None


def _has_required_cue(clause: str, alias: str, *, document: str) -> bool:
    strong_submission_patterns = (
        rf"\b(?:submit|attach|upload|send|enclose)\b.{{0,180}}(?:{alias})",
        rf"(?:{alias}).{{0,100}}\bmust\s+be\s+(?:submitted|included|attached|uploaded|provided)\b",
        rf"\b(?:apply|application)\b.{{0,100}}\bwith\b.{{0,160}}(?:{alias})",
        rf"\brequired\s+(?:application\s+)?(?:documents?|materials?)\s*:"
        rf".{{0,160}}(?:{alias})",
    )
    if any(re.search(pattern, clause, re.IGNORECASE) for pattern in strong_submission_patterns):
        return True

    weak_submission = bool(
        re.search(
            rf"\b(?:include|provide|supply)\b.{{0,180}}(?:{alias})",
            clause,
            re.IGNORECASE,
        )
    )
    if weak_submission and (
        document in {"resume", "cover_letter"}
        or _has_application_anchor(clause)
        or re.search(
            rf"\b(?:your|a|an|current|most\s+recent|copy\s+of)\b"
            rf"(?:\s+[a-z-]+){{0,2}}\s+(?:{alias})",
            clause,
            re.IGNORECASE,
        )
    ):
        return True

    required_list = (
        rf"(?:{alias})"
        rf"(?:{_DOCUMENT_LIST_SEPARATOR}(?:{_ANY_DOCUMENT_ALIAS}))*"
        rf"\s+(?:(?:is|are)\s+)?required\b"
    )
    return bool(re.search(required_list, clause, re.IGNORECASE))


def _has_optional_or_negative_cue(clause: str, alias: str) -> bool:
    submission_verb = r"(?:submit|attach|upload|send|enclose|include|provide|supply)"
    patterns = (
        rf"\bno\b.{{0,30}}(?:{alias}).{{0,50}}\brequired\b",
        rf"(?:{alias}).{{0,70}}\b(?:optional|not\s+required|not\s+necessary)\b",
        rf"\b(?:do\s+not|don't|need\s+not)\b.{{0,80}}(?:{alias})",
        rf"\bwithout\b.{{0,40}}(?:{alias})",
        rf"\bmay\s+(?:also\s+)?{submission_verb}\b.{{0,80}}(?:{alias})",
        rf"\bfeel\s+free\s+to\s+{submission_verb}\b.{{0,80}}(?:{alias})",
        rf"\boptionally\b.{{0,30}}{submission_verb}\b.{{0,80}}(?:{alias})",
        rf"(?:{alias}).{{0,40}}\bmay\s+be\s+"
        rf"(?:submitted|attached|uploaded|sent|enclosed|included|provided)\b",
    )
    return any(re.search(pattern, clause, re.IGNORECASE) for pattern in patterns)


def _has_application_anchor(clause: str) -> bool:
    return bool(re.search(r"\b(?:apply|applications?)\b", clause, re.IGNORECASE))


def _has_post_application_context(clause: str) -> bool:
    patterns = (
        r"\b(?:after|following|upon)\s+(?:receiving\s+)?"
        r"(?:(?:an?|the)\s+)?(?:job\s+)?offer\b",
        r"\b(?:during|as\s+part\s+of)\s+(?:an?\s+|the\s+)?background\s+check\b",
        r"\b(?:after|during|following)\s+(?:an?\s+|the\s+)?interviews?\b",
        r"\b(?:if|once)\s+(?:you\s+are\s+)?selected\b",
        r"\b(?:successful|selected)\s+(?:applicant|candidate)\b",
    )
    return any(re.search(pattern, clause, re.IGNORECASE) for pattern in patterns)


def _has_incidental_role_context(clause: str, alias: str) -> bool:
    return bool(
        re.search(
            rf"(?:{alias})\s+(?:management|analysis|analytics|data|systems?|"
            rf"strategy|experience|processing|software)\b",
            clause,
            re.IGNORECASE,
        )
    )


def _boolean_state(state: str) -> bool | None:
    if state == "required":
        return True
    if state == "not_required":
        return False
    return None
