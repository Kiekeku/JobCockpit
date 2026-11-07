from __future__ import annotations

import re
from dataclasses import dataclass

from jobcockpit.ingestion.evidence_grammar import EvidenceMention
from jobcockpit.ingestion.parser_models import ParsedEligibility, ParsedTiming, RoleEvidence
from jobcockpit.ingestion.posting_document import record_decision
from jobcockpit.ingestion.posting_fields import _field_value
from jobcockpit.ingestion.qualification_grammar import requirement_is_necessary
from jobcockpit.ingestion.role_parser import _title_value
from jobcockpit.ingestion.text_patterns import NUMERIC_RANGE_SEPARATOR_PATTERN


def _role_type_from_title(
    title: str | None,
    posting_text: str,
    employment_timing: ParsedTiming,
) -> str:
    if not title:
        return "unknown"
    if re.match(r"^\s*intern architect\b", title, flags=re.IGNORECASE):
        return "full_time"
    if re.search(
        r"\b(?:intern(?:ship)?|co[-\s]?op|stagiaire)\b",
        title,
        flags=re.IGNORECASE,
    ):
        return "internship"
    if re.match(r"^\s*stage\s*[-–—:]\s*\S", title, re.IGNORECASE) and re.search(
        r"\b(?:le\s+poste|mandat|stage\s+crédité|formation|conditions\s+du\s+stage)\b",
        posting_text,
        re.IGNORECASE,
    ):
        return "internship"
    if re.search(
        r"\b(?:(?:work|student|internship)\s+placement|placement\s+(?:student|intern))\b",
        title,
        flags=re.IGNORECASE,
    ):
        return "internship"
    if _posting_has_current_role_internship_evidence(posting_text):
        return "internship"
    if _title_has_student_employment_evidence(title, posting_text, employment_timing):
        return "internship"
    return "full_time"


def _posting_has_current_role_internship_evidence(posting_text: str) -> bool:
    employment_type = _field_value(
        posting_text.splitlines(),
        "Employment Type",
        "Job Type",
        "Position Type",
        "Worker Type",
    )
    if employment_type and re.search(
        r"\b(?:intern(?:ship)?|co[-\s]?op|student\s+placement)\b|^\s*student\s*$",
        employment_type,
        flags=re.IGNORECASE,
    ):
        return True

    duration_value = (
        r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
    )
    duration = (
        rf"(?:(?:{duration_value}\s*{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*)?"
        rf"{duration_value}[-\s]+(?:weeks?|months?|years?)|"
        r"(?:semester|term)(?:[-\s]+long)?|"
        r"(?:academic|school|full)[-\s]+year(?:[-\s]+long)?|"
        r"(?:one[-\s]+)?year(?:[-\s]+)?long)"
    )
    # A role marker must be a complete lexical head.  An attached lexical
    # hyphen makes it a modifier (for example, "internship-program"), while a
    # title still remains free to contain genuine names such as
    # "Internship-Programme" because title classification is handled above.
    role_marker = (
        r"(?:internship|co[-\s]?op|student\s+placement)"
        r"(?![-\u2010\u2011][A-Za-z0-9])"
    )
    nominal_completion = (
        r"(?:role|position|job|opportunity|placement|contract|term)\b"
    )
    postnominal_adjunct = (
        r"(?:with|at|within|on|from|during|for|in|starting|beginning|lasting|"
        r"running|based|embedded|focused|supporting|working|offering|providing|"
        r"that|which|where)\b"
    )
    role_complement = (
        rf"{role_marker}\b(?=\s*(?:$|[.,;:!?()\-|\u2013\u2014]|"
        rf"{nominal_completion}|{postnominal_adjunct}))"
    )
    modifier = (
        r"(?:paid|unpaid|full[-\s]?time|part[-\s]?time|seasonal|rolling|"
        r"hands[-\s]?on|(?:fully\s+)?(?:remote|hybrid|on[-\s]?site)|"
        r"[A-Za-z]+[-\s]+focused|"
        r"(?:summer|fall|winter|spring|autumn)(?:\s+20\d{2})?|20\d{2})"
    )
    role_attribute = rf"(?:{duration}|{modifier})(?:\s*,\s*|\s+)"
    current_role_subject = (
        r"(?:this|(?:this|the)\s+(?:role|position|job|opportunity|employment|contract))"
    )
    current_role_link = (
        r"(?:(?:is|will\s+be|would\s+be)(?:\s+(?:for|"
        r"(?:offered|structured|classified|designated|intended|designed)\s+"
        r"(?:as|to\s+be)))?|serves?\s+as|functions?\s+as)"
    )
    return bool(
        re.search(
            rf"\b{current_role_subject}\s+{current_role_link}\s+(?:an?\s+)?"
            rf"(?:{role_attribute}){{0,5}}{role_complement}",
            posting_text,
            flags=re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:this|the)\s+{role_marker}\s+"
            r"(?:is|will\s+be|would\s+be|lasts?|runs?|starts?|begins?)\b",
            posting_text,
            flags=re.IGNORECASE,
        )
    )


def _title_has_student_employment_evidence(
    title: str,
    posting_text: str,
    employment_timing: ParsedTiming,
) -> bool:
    if not re.search(r"\bstudent\b", title, flags=re.IGNORECASE):
        return False
    if re.search(r"\bstudent\s+opportunit(?:y|ies)\b", title, flags=re.IGNORECASE):
        return True
    if re.search(
        r"(?:^|[/|(-])\s*(?:(?:fall|winter|spring|summer)\s+)?student\s*,|"
        r"\b(?:fall|winter|spring|summer)\s+student\b",
        title,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\bstudent\b(?:\s*\([^)]{1,80}\))?\s*"
        r"(?:[-|]\s*(?:(?:fall|winter|spring|summer|autumn|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s+)?20\d{2})?\s*$",
        title,
        flags=re.IGNORECASE,
    ):
        return True
    if _posting_requires_current_enrollment(posting_text):
        return True
    if (
        employment_timing.timing_type == "fixed_term"
        and re.search(
            r"\bstudent\b[^\n]{0,120}\b\d{1,2}(?:\.\d+)?[-\s]?months?\s+term\b",
            title,
            flags=re.IGNORECASE,
        )
    ):
        return True
    return bool(
        re.search(
            r"\b(?:this|the)\s+(?:role|position|opportunity)\s+(?:is|will\s+be)\b"
            r"[^.;\n]{0,80}\b(?:co[-\s]?op|internship|student\s+(?:role|position))\b|"
            r"\b\d{1,2}(?:\.\d+)?[-\s]?months?\s+(?:co[-\s]?op|internship)\s+position\b",
            posting_text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:worker\s+type|employment\s+type|job\s+type|category)\s*:?[ \t]*"
            r"student\b|\b(?:student\s+(?:position|role|opportunity|job)|"
            r"applications?\s+(?:are\s+)?(?:invited|accepted)\s+from\b[^.;\n]{0,80}"
            r"\bstudents?)\b",
            posting_text,
            flags=re.IGNORECASE,
        )
    )


_ACADEMIC_ENROLLMENT_CONTEXT_PATTERN = (
    r"\b(?:students?|school|studies|education|university|college|degree|diploma|"
    r"academic|undergraduates?|graduates?|post[-\s]?secondary|faculty|major|"
    r"semester|co[-\s]?op|"
    r"accredited\s+(?:(?:post[-\s]?secondary|academic)\s+)?institution)\b"
)
_ACADEMIC_PERIOD_PATTERN = (
    r"(?:internship(?:\s*/\s*co[-\s]?op)?(?:\s+program)?|"
    r"co[-\s]?op(?:erative\s+education)?(?:\s+program)?|"
    r"(?:work|academic|school)\s+(?:term|year)|placement|semester)"
)
_ACADEMIC_TEMPORAL_ADJUNCT_PATTERN = (
    rf"\b(?:(?:throughout|during)\s+"
    rf"(?:(?:the|this|your|their|an?)\s+)?"
    rf"(?:(?:full\s+)?(?:period|duration)\s+of\s+"
    rf"(?:(?:the|this|your|their|an?)\s+)?)?{_ACADEMIC_PERIOD_PATTERN}|"
    rf"for\s+(?:(?:the|this|your|their)\s+)?(?:full\s+)?duration\s+of\s+"
    rf"(?:(?:the|this|your|their|an?)\s+)?{_ACADEMIC_PERIOD_PATTERN})\b"
)

_ACADEMIC_CREDENTIAL_LEVEL_PATTERN = (
    r"(?:associate(?:['\u2019\ufffd]s)?|bachelor(?:['\u2019\ufffd]s)?|"
    r"master(?:['\u2019\ufffd]s)?|doctoral|doctorate|"
    r"b\.?s\.?c?\.?|m\.?s\.?c?\.?|ph\.?d\.?|undergraduate|"
    r"graduate|college|university|post[-\s]?secondary)"
)
_ACADEMIC_CREDENTIAL_ABBREVIATION_PATTERN = (
    r"(?:b\.?s\.?c?\.?|m\.?s\.?c?\.?|ph\.?d\.?)"
)
_ACADEMIC_NAMED_DEGREE_PATTERN = (
    r"(?:associate(?:['\u2019\ufffd]s)?|bachelor(?:['\u2019\ufffd]s)?|"
    r"master(?:['\u2019\ufffd]s)?)"
)
_ACADEMIC_STANDALONE_CREDENTIAL_PATTERN = (
    r"(?:associate['\u2019\ufffd]s|bachelor['\u2019\ufffd]s|"
    r"master['\u2019\ufffd]s|"
    rf"{_ACADEMIC_CREDENTIAL_ABBREVIATION_PATTERN})"
)
_ACADEMIC_CREDENTIAL_PATTERN = (
    r"(?:(?:a|an|your|their)\s+)?(?:"
    rf"{_ACADEMIC_CREDENTIAL_LEVEL_PATTERN}"
    rf"(?:\s*(?:,\s*(?:or\s+)?|or\s+|/\s*){_ACADEMIC_CREDENTIAL_LEVEL_PATTERN})*\s+"
    r"(?:degree|diploma|program)|"
    rf"{_ACADEMIC_CREDENTIAL_ABBREVIATION_PATTERN}"
    rf"(?:\s*(?:,\s*(?:or\s+)?|or\s+|/\s*){_ACADEMIC_CREDENTIAL_ABBREVIATION_PATTERN})*|"
    rf"{_ACADEMIC_STANDALONE_CREDENTIAL_PATTERN}"
    rf"(?:\s*(?:,\s*(?:or\s+)?|or\s+|/\s*){_ACADEMIC_STANDALONE_CREDENTIAL_PATTERN})*|"
    rf"{_ACADEMIC_NAMED_DEGREE_PATTERN}\s+of\s+"
    r"(?:arts|science|engineering|business|commerce|applied\s+science)|"
    r"degree|diploma|academic\s+program|post[-\s]?secondary\s+education)"
)
_APPLICANT_REQUIREMENT_HEADING_PATTERN = (
    r"(?:(?:basic|minimum|required|essential|mandatory)\s+)?"
    r"(?:qualifications?|requirements?|eligibility(?:\s+requirements?)?)|"
    r"who\s+you\s+are|who\s+we(?:['\u2019\ufffd]re|\s+are)?\s+looking\s+for|"
    r"what\s+you(?:['\u2019\ufffd]ll|\s+will)?\s+bring|"
    r"what\s+we(?:['\u2019\ufffd]re|\s+are)?\s+looking\s+for|"
    r"candidate\s+profile|your\s+profile|"
    r"education(?:\s+(?:(?:and|&)\s+experience|recommendations?))?"
    r"|(?:the\s+)?student\s+we(?:['\u2019\ufffd]re|\s+are)?\s+looking\s+for"
)


def _posting_requires_current_enrollment(posting_text: str) -> bool:
    student_status = (
        r"(?:an?\s+)?(?:(?:current(?:ly)?|full[-\s]?time|part[-\s]?time|"
        r"university|college|undergraduate|graduate|post[-\s]?secondary)\s+){0,3}"
        r"student"
    )
    patterns = (
        re.compile(
            rf"\b(?:(?P<explicit>(?P<academic_enrollment>"
            rf"(?:must|required\s+to)\s+"
            rf"(?:be|remain|continue\s+to\s+be)\s+"
            rf"(?:(?:currently|actively|still)\s+)?enrolled)|"
            rf"(?:must\s+be|required\s+to\s+be)\s+{student_status}|"
            r"enrollment\s+(?:is\s+)?required|proof\s+of\s+enrollment)|"
            rf"(?P<contextual>(?P<contextual_enrolled>"
            r"currently\s+enrolled|current\s+enrollment)|"
            r"return(?:ing)?\s+to\s+(?:school|studies)|"
            rf"(?:currently\s+)?pursuing\s+{_ACADEMIC_CREDENTIAL_PATTERN}|"
            r"registered\s+(?:as|in)\s+"
            r"(?:a\s+)?(?:full[-\s]?time\s+)?student))\b",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|[\r\n;:>])[ \t]*(?:[-*|\u2022][ \t]*)?"
            r"(?P<explicit>(?P<academic_enrollment>be\s+enrolled))\b",
            flags=re.IGNORECASE | re.MULTILINE,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(posting_text):
            evidence_group = "explicit" if match.group("explicit") else "contextual"
            mention = EvidenceMention.from_posting(
                posting_text,
                match.start(evidence_group),
                match.end(evidence_group),
            )
            evidence_text = match.group(evidence_group)
            if (
                match.group("academic_enrollment")
                or match.groupdict().get("contextual_enrolled")
                or re.search(r"\benroll(?:ed|ment)\b", evidence_text, flags=re.IGNORECASE)
            ) and not _enrollment_mention_has_academic_context(
                posting_text,
                mention,
            ):
                continue
            if mention.is_negated_or_nonmandatory():
                continue
            if _enrollment_mention_has_nonstudent_alternative(mention):
                continue
            suffix = posting_text[match.end(evidence_group):]
            if re.match(
                r"[^\n]{0,300}?\.\s*Recent\s+graduates\s+will\s+also\s+be\s+considered\.",
                suffix, re.I,
            ):
                continue
            if match.group("explicit") or _enrollment_mention_is_applicant_owned(mention):
                return True
    return False


def _enrollment_mention_has_academic_context(
    posting_text: str,
    mention: EvidenceMention,
) -> bool:
    attached_suffix = re.split(
        r"[,;.!?\r\n]|\b(?:and|but|before|after|while|whereas)\b",
        mention.suffix,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    # An explicit complement owns the meaning of "enrolled". Do not let an
    # unrelated academic word later in the clause (or in the title) reclassify
    # enrollment in a company, benefit, or certification program.
    if re.match(r"\s*(?:in|at|with)\b", attached_suffix, flags=re.IGNORECASE):
        if re.search(
            _ACADEMIC_ENROLLMENT_CONTEXT_PATTERN,
            attached_suffix,
            flags=re.IGNORECASE,
        ):
            return True
        if re.search(
            r"\b(?:company|employer|employee|benefits?|training|onboarding|"
            r"certification|membership|professional\s+development)\b",
            attached_suffix,
            flags=re.IGNORECASE,
        ):
            return False
        coordinated_suffix = mention.suffix[len(attached_suffix) :]
        if _coordinated_suffix_has_academic_status(coordinated_suffix):
            return True

    if re.search(
        _ACADEMIC_ENROLLMENT_CONTEXT_PATTERN,
        attached_suffix,
        flags=re.IGNORECASE,
    ) or re.search(
        _ACADEMIC_TEMPORAL_ADJUNCT_PATTERN,
        attached_suffix,
        flags=re.IGNORECASE,
    ):
        return True

    if re.search(
        _ACADEMIC_ENROLLMENT_CONTEXT_PATTERN,
        mention.prefix[-120:],
        flags=re.IGNORECASE,
    ):
        return True

    title = _title_value(posting_text.splitlines()) or ""
    return not re.match(r"^\s*intern\s+architect\b", title, flags=re.IGNORECASE) and bool(
        re.search(
            r"\b(?:intern(?:ship)?|co[-\s]?op|student|work\s+placement)\b",
            title,
            flags=re.IGNORECASE,
        )
    )


def _coordinated_suffix_has_academic_status(suffix: str) -> bool:
    schedule = r"(?:(?:full|part)[-\s]?time\s+)?"
    institution = r"(?:university|college|school)"
    student = rf"(?:{institution}|post[-\s]?secondary\s+)?student"
    course_count = (
        r"(?:(?:at\s+least|(?:a\s+)?minimum\s+of)\s+)?"
        r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
        r"(?:\s+or\s+more)?"
    )
    institution_name = rf"(?:[A-Za-z][A-Za-z&'.-]*\s+){{0,4}}{institution}"
    academic_status_predicate = (
        rf"(?:attend(?:ing|s)?\s+{schedule}(?:at\s+)?(?:a\s+)?{institution}|"
        rf"enroll(?:ed|ing)?\s+{schedule}(?:at|in)\s+(?:a\s+)?{institution}|"
        rf"stud(?:y|ying|ies)\s+{schedule}(?:at|in)\s+(?:a\s+)?{institution}|"
        r"pursu(?:e|es|ing)\s+(?:(?:a|an|their|your)\s+)?"
        r"(?:degree|diploma|post[-\s]?secondary\s+education)|"
        r"register(?:ed|ing)?\s+(?:as\s+)?(?:a\s+)?student|"
        r"return(?:ing)?\s+to\s+(?:school|studies)|"
        rf"maintain(?:s|ed|ing)?\s+(?:(?:their|your)\s+)?{schedule}student\s+status|"
        rf"maintain(?:s|ed|ing)?\s*,?\s*{course_count}\s+"
        r"(?:\([^)]{1,24}\)\s+)?"
        rf"(?:(?:full|part)[-\s]?time\s+)?(?:courses?|course\s+load)\s+"
        rf"(?:at|in)\s+(?:an?\s+)?{institution_name}|"
        rf"remain(?:s|ed|ing)?\s+enrolled\s+(?:as\s+)?(?:a\s+)?{schedule}student|"
        rf"continu(?:e|es|ed|ing)\s+(?:as\s+(?:a\s+)?{schedule}student|"
        rf"to\s+be\s+(?:(?:a\s+)?{schedule}student|"
        rf"enrolled(?:\s+as\s+(?:a\s+)?{schedule}student)?"
        rf"(?=\s*(?:$|[,;.!?]|{_ACADEMIC_TEMPORAL_ADJUNCT_PATTERN}))))|"
        rf"(?:is|are|remain(?:s|ing)?)\s+(?:a\s+)?{student})"
    )
    return bool(
        re.match(
            rf"\s*,?\s*(?:and|as\s+well\s+as)\s+"
            rf"(?:(?:is|are|be|must|will|should)\s+)?"
            rf"(?:(?:currently|actively|still)\s+)?{academic_status_predicate}\b",
            suffix,
            flags=re.IGNORECASE,
        )
    )


def _enrollment_mention_has_nonstudent_alternative(
    mention: EvidenceMention,
) -> bool:
    # An interrupted predicate shares its academic object with the alternative:
    # "enrolled in, or recently completed, a university program".
    if re.match(
        r"\s*(?:in\s*)?,?\s*or\s+(?:recently\s+completed\b|"
        r"recent\s+completion\s+of\b|recently\s+graduated\b)",
        mention.suffix, re.I,
    ):
        return True
    if re.match(
        r"\s+in\s+the\s+third\s+or\s+fourth\s+year\b[^.;\n]{0,220}"
        r",\s*or\s+recently\s+graduated\s+from\s+a\s+related\s+program\b",
        mention.suffix, re.I,
    ):
        return True
    complete_nonstudent_status = (
        r"(?:(?:have\s+)?(?:recently\s+)?completed\s*,?\s+"
        r"(?:(?:their|your|a|an)\s+)?"
        rf"(?:{_ACADEMIC_CREDENTIAL_LEVEL_PATTERN}\s+)?"
        r"(?:studies|program|degree|diploma)|"
        r"(?:recent(?:ly)?|upcoming)"
        r"(?:\s*(?:/|or)\s*(?:recent(?:ly)?|upcoming))?\s+graduates?|graduated|"
        rf"{_ACADEMIC_CREDENTIAL_LEVEL_PATTERN}\s+"
        r"(?:degree|diploma)\s+holders?|"
        r"equivalent\s+experience)"
    )
    if mention.has_coordinated_alternative(complete_nonstudent_status):
        return True

    # Before an enrollment predicate, a credential noun phrase is itself a
    # complete alternative ("a bachelor's degree or currently pursuing...").
    # After the predicate it can merely be the second type in a coordinated
    # degree object ("enrolled in a bachelor or master degree program"), so it
    # must not cancel the requirement there.
    prior_credential = (
        rf"(?:(?:a|an)\s+)?{_ACADEMIC_CREDENTIAL_LEVEL_PATTERN}\s+"
        r"(?:degree|diploma)"
    )
    return mention.has_coordinated_alternative(
        prior_credential,
        direction="before",
    )


def _enrollment_mention_is_applicant_owned(mention: EvidenceMention) -> bool:
    prefix = mention.prefix[-120:]
    suffix = mention.suffix[:60]
    normalized_prefix = re.sub(
        r"^\s*(?:(?:[-*|]|\u2022)\s*)?",
        "",
        mention.prefix,
    ).strip(" \t\r\n:")
    if not normalized_prefix or re.fullmatch(
        _APPLICANT_REQUIREMENT_HEADING_PATTERN,
        normalized_prefix,
        flags=re.IGNORECASE,
    ) or re.search(
        rf"\b(?:{_APPLICANT_REQUIREMENT_HEADING_PATTERN})\s*:?[ \t]*$",
        mention.prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:seek(?:ing|s)?|hir(?:e|ing)|open\s+to)\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:targeting|seeking|looking\s+for|open\s+to)\s+students?\s+"
        r"(?:who|that)\s+are\s*$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(r"\s+students?\b", suffix, flags=re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:you|applicants?|candidates?|successful\s+candidate|interns?)\b"
        r"[^.;\n]{0,60}$",
        prefix,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:must|should|required|eligible|qualifications?|requirements?)\b",
            prefix,
            flags=re.IGNORECASE,
        )
    )


_COOP_ENROLLMENT = re.compile(
    r"\b(?:enrolled|registered|participating)\s+in\s+(?:an?\s+)?"
    r"(?:(?:official|recognized|post[-\s]?secondary)\s+)*"
    r"co[-\s]?op(?:erative\s+education)?\s+program\b|"
    r"\bco[-\s]?op\s+(?:program\s+)?enrollment\s+(?:is\s+)?required\b",
    re.IGNORECASE,
)
_RETURN_TO_STUDIES = r"return(?:ing)?\s+(?:back\s+)?to\s+(?:your\s+)?(?:studies|school)"


def _co_op_predicate_necessity(text: str) -> bool | None:
    if _COOP_ENROLLMENT.search(text):
        return True
    if re.match(rf"\s*(?:you\s+)?(?:should\s+be\s+)?{_RETURN_TO_STUDIES}\b", text, re.I):
        return False
    return None


@dataclass(frozen=True)
class EligibilityResolution:
    eligibility: ParsedEligibility
    ambiguous_fields: tuple[str, ...] = ()


def resolve_eligibility(
    posting_text: str,
    role_type: str,
    role_evidence: list[RoleEvidence] | None = None,
) -> EligibilityResolution:
    states = []
    for match in _COOP_ENROLLMENT.finditer(posting_text):
        mention = EvidenceMention.from_scoped_posting(posting_text, match.start(), match.end())
        state = "required"
        if re.search(r"\b(?:are|were)\s+you\s+(?:currently\s+)?$", mention.prefix, re.I):
            state = "unknown"
        elif mention.is_negated_or_nonmandatory():
            state = "not_required"
        elif re.search(r"\bpreference\s+for\s+(?:students?\s+)?$", mention.prefix, re.I):
            state = "preferred"
        elif mention.has_coordinated_alternative(_RETURN_TO_STUDIES):
            state = "alternative"
        else:
            for evidence in role_evidence or []:
                if not (
                    evidence.evidence_start is not None
                    and evidence.evidence_end is not None
                    and evidence.evidence_start
                    <= match.start()
                    < match.end()
                    <= evidence.evidence_end
                ):
                    continue
                expression = evidence.requirement_expression
                if (
                    expression is not None
                    and expression.children
                    and requirement_is_necessary(expression, _co_op_predicate_necessity) is False
                ):
                    state = "alternative"
                    break
                for claim in evidence.claims:
                    if (
                        claim.source_start is not None
                        and claim.source_end is not None
                        and claim.source_start <= match.start() < match.end() <= claim.source_end
                        and claim.modality in {"preferred", "optional"}
                    ):
                        state = "preferred" if claim.modality == "preferred" else "not_required"
        states.append(state)
        record_decision(
            "eligibility.co_op_enrollment_required",
            "scoped_enrollment_claim",
            state=state,
            source_start=match.start(),
            source_end=match.end(),
        )
    required = "required" in states
    ambiguous = (
        ("eligibility.co_op_enrollment_required",) if not required and "unknown" in states else ()
    )
    return EligibilityResolution(
        ParsedEligibility(
            current_enrollment_required=_posting_requires_current_enrollment(posting_text),
            co_op_enrollment_required=required,
            internship_eligible_only=role_type == "internship",
        ),
        ambiguous_fields=ambiguous,
    )


def _eligibility_from_posting(posting_text: str, role_type: str) -> ParsedEligibility:
    return resolve_eligibility(posting_text, role_type).eligibility
