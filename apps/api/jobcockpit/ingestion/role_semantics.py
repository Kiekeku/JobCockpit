from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from jobcockpit.ingestion.capability_ontology import load_capability_ontology
from jobcockpit.ingestion.location_parser import (
    CANADIAN_PROVINCE_ALIASES,
    CANADIAN_PROVINCE_CODES,
    CANADIAN_PROVINCES,
    COUNTRY_ALIASES,
    COUNTRY_CODES,
    US_STATE_CODES,
    US_STATE_NAMES,
)
from jobcockpit.ingestion.parser_models import (
    RequirementExpression,
    RoleClaim,
    RoleEvidence,
)
from jobcockpit.ingestion.posting_document import document_for_text, session_cached
from jobcockpit.ingestion.posting_fields import _is_structured_field_label
from jobcockpit.ingestion.qualification_grammar import (
    academic_completion_condition,
    academic_predicate_spans,
)
from jobcockpit.ingestion.role_heading_patterns import DYNAMIC_INLINE_HEADINGS

RoleSection = Literal[
    "unsectioned",
    "responsibilities",
    "qualifications",
    "preferred_qualifications",
    "role_overview",
    "capabilities",
    "company",
    "boundary",
    "posting_identity",
    "continuation",
    "privacy",
    "terminal",
]
ClauseOwner = Literal["applicant", "company", "role", "unspecified"]
ApplicantPredicate = Literal[
    "hard_requirement",
    "qualification",
    "descriptive",
    "unspecified",
]
NegativeModalForce = Literal["none", "epistemic", "prohibited"]
DistributedModality = Literal["mandatory", "expected", "preferred"]
ConditionType = Literal[
    "geographic",
    "temporal",
    "applicant_subset",
    "contingency",
    "schedule",
    "unknown",
]


_RESPONSIBILITY_HEADINGS = {
    "additional responsibilities",
    "assigned responsibilities",
    "responsibilities",
    "key responsibilities",
    "major responsibilities",
    "regular responsibilities may include",
    "duties and responsibilities",
    "job responsibilities",
    "main responsibilities",
    "how you will contribute",
    "how you will create an impact",
    "what are your contributions to the team",
    "how will you make an impact",
    "in this role you can expect to",
    "in this role you will",
    "meaningful work you will be a part of",
    "problems youll address",
    "role responsibilities",
    "role and responsibilities",
    "responsibilities and duties",
    "roles and responsibilities",
    "role description",
    "what you will do",
    "what you ll do",
    "you will do",
    "what will you do",
    "what youll do",
    "what you will be doing",
    "what youll be doing",
    "what youll be expected to own",
    "what youll work on",
    "what you will be working on",
    "what you will do day to day",
    "what youll be working on",
    "what youll do day to day",
    "what success looks like",
    "what will your typical day look like",
    "what your day to day will look like",
    "what does your day to day look like",
    "what will your day to day look like",
    "your responsibilities",
    "your role and responsibilities",
    "how youll make an impact",
    "help desk and employee support",
    "user accounts and access",
    "device and it operations",
    "documentation and continuous improvement",
    "about the job",
    "that includes things like",
    "you will have the opportunity to",
    "you will be responsible for",
    "essential functions",
}
_QUALIFICATION_HEADINGS = {
    "about you",
    "basic qualifications",
    "basic requirements",
    "education and experience",
    "education and experience required",
    "education and training",
    "education recommendation",
    "education qualifications",
    "ideal attributes education and experience",
    "ideal candidate",
    "our ideal hire",
    "how to thrive in this role",
    "enough about us lets talk about you",
    "minimum job requirements",
    "minimum qualifications",
    "minimum requirements",
    "must have",
    "must haves",
    "candidate requirements",
    "critical success factors",
    "experience and qualifications",
    "knowledge skills and abilities",
    "job requirements",
    "key qualifications",
    "key requirements",
    "language requirements",
    "language requirements in quebec",
    "qualifications",
    "required experience",
    "required",
    "required qualifications",
    "required skills and experience",
    "required skills and qualifications",
    "requirements",
    "requirements qualifications",
    "position requirements",
    "skills and competencies",
    "specialized skills knowledge and abilities",
    "skills and experience",
    "skills and qualifications",
    "skills and attributes",
    "hardware and systems skills",
    "technical background",
    "technical skills",
    "the person",
    "the value you bring",
    "the must haves",
    "these skills will make you successful",
    "what we are looking for",
    "what were looking for",
    "what you bring",
    "what you bring to the table",
    "what you bring to this role",
    "what you bring to the role",
    "what it takes",
    "what you need",
    "what do you need to succeed",
    "what do you need to be successful",
    "what do you bring to the team",
    "what youll need",
    "what youll need to have",
    "work environment",
    "what you need to succeed",
    "what you need to succeed required",
    "what you will bring",
    "what youll bring",
    "what makes you stand out",
    "who we are looking for",
    "who were looking for",
    "who you are",
    "to succeed in this role",
    "your experience",
    "your experience and education",
    "your profile",
    "required qualifications to be successful in this role",
    "your qualifications",
    "competencies",
    "skills",
    "skills and abilities",
    "suggested qualifications experience",
    "required education",
    "academic background",
    "a successful candidate will demonstrate",
    "certification and experience",
    "design skills",
    "other skills",
    "you will be successful in this role because you possess these attributes",
    "you will be a good fit if you",
    "you will be a strong candidate if you",
    "eligibility",
    "eligibility requirements",
    "skill set",
    "were looking for an individual who",
    "you will bring",
    "you are someone who",
    "this role is for you if",
    "in this role you are someone who is has",
    "you should be comfortable with",
    "physical demands",
    "environmental conditions",
    "behavioral traits",
}
_PREFERRED_HEADINGS = {
    "additional assets",
    "additional skills desired for the role",
    "assets",
    "bonus points",
    "bonus points if",
    "bonus points if you have",
    "it would be great if",
    "nice if you have",
    "nice to have",
    "nice to haves",
    "nice to have assets",
    "nice to have skills",
    "nice to have not required",
    "preferred",
    "preferred qualifications",
    "preferred education and experience",
    "preferred education",
    "preferred but not required",
    "preferred skills",
    "following qualifications are a plus",
    "knowledge of the following technologies would give you an edge",
    "what will put you ahead",
}
_OVERVIEW_HEADINGS = {
    "about the role",
    "description",
    "english follows",
    "job description",
    "position overview",
    "role overview",
    "the opportunity",
    "what is the opportunity",
    "the role",
    "what this role involves",
    "your impact",
    "your role and key responsibilities",
    "examples of problems were tackling",
}
_CAPABILITY_HEADINGS = {
    "our tech stack",
    "tech stack",
    "technologies",
    "technology stack",
    "tools and technologies",
    "our stack",
    "this software includes",
    "technical environment",
}
_COMPANY_HEADINGS = {
    "about the company",
    "about us",
    "company description",
    "company overview",
    "our company",
    "our purpose",
    "who we are",
}
_BOUNDARY_HEADINGS = {
    "additional information",
    "additional information hours",
    "about the team",
    "accommodations process",
    "application details",
    "application instructions",
    "boarding information",
    "benefits",
    "compensation",
    "compensation and equity",
    "compensation and benefits",
    "in office expectations",
    "learn more about our culture and values",
    "location",
    "myrewards mnp",
    "diversity mnp",
    "pay transparency",
    "perks",
    "potential placements",
    "pre employment requirements",
    "salary",
    "supervision",
    "total rewards",
    "the perks of joining our team we offer",
    "what we offer",
    "what we bring to the table",
    "what youll take away",
    "travailler chez pratt and whitney canada",
    "a note on experience",
    "what you bring to the team",
    "what youll earn",
    "what youll gain",
    "what youll learn",
    "what to look forward to",
    "what to expect during the internship",
    "what youll love about us",
    "what is in it for you",
    "whats in it for you",
    "why join us",
    "why join",
    "why gateway",
    "why work for us",
    "why youll love working here",
    "awareness unity empowerment",
}
_STANDALONE_SECTION_HEADINGS: dict[str, RoleSection] = {
    "assigned responsibilities": "responsibilities",
    "application": "boundary",
    "business intelligence and data analytics": "continuation",
    "digital support": "continuation",
    "develop and implement commercial growth strategies": "continuation",
    "master the art and science of pricing": "continuation",
    "turn analysis into actionable recommendations": "continuation",
    "communicate insights and drive change": "continuation",
    "thrive in a collaborative and entrepreneurial culture": "continuation",
    "english follows": "role_overview",
    "education": "continuation",
    "experience": "continuation",
    "business sense": "qualifications",
    "business relations": "qualifications",
    "curiosity": "qualifications",
    "knowledge and skills": "qualifications",
    "knowledge skills and abilities": "qualifications",
    "other requirements and information": "qualifications",
    "qualifications and professional experience": "qualifications",
    "success factors": "qualifications",
    "problem solving capacity": "qualifications",
    "the company": "company",
}
_BARE_HEADINGS_REQUIRING_COMPLETE_LINE = {
    "eligibility",
    "experience",
    "in this role you will",
    "job openings",
    "must have",
    "required",
    "the role",
    "your experience",
    "your experience and education",
}
_OVERLAPPING_OCR_HEADING_ALLOWLIST = {"total rewards"}
_LEGAL_TERMINAL_HEADINGS = {
    "disclaimer",
    "eeo statement",
    "equal employment opportunity",
    "equal opportunity",
    "equal opportunity employer",
    "inclusion and equal opportunity employment",
    "inclusivity and equal opportunity",
}
_RELATED_LISTING_HEADINGS = {
    "jobseekers also viewed",
    "jobs you may be interested in",
    "more jobs like this",
    "people also viewed",
    "related jobs",
    "similar jobs",
}
_PORTAL_TERMINAL_HEADINGS = {
    "accessibility accommodations",
    "apply for this job",
    "frequently asked questions",
    "how to apply",
    "job openings",
    "report job",
    "share apply apply for this position",
}
_TERMINAL_HEADINGS = (
    _LEGAL_TERMINAL_HEADINGS
    | _RELATED_LISTING_HEADINGS
    | _PORTAL_TERMINAL_HEADINGS
)
_OCR_REPAIRABLE_HEADING_WORDS = frozenset(
    word
    for heading in (
        _RESPONSIBILITY_HEADINGS
        | _QUALIFICATION_HEADINGS
        | _PREFERRED_HEADINGS
        | _OVERVIEW_HEADINGS
        | _CAPABILITY_HEADINGS
        | _COMPANY_HEADINGS
        | _BOUNDARY_HEADINGS
        | _TERMINAL_HEADINGS
        | set(_STANDALONE_SECTION_HEADINGS)
    )
    for word in heading.split()
)
_OCR_UNKNOWN_HEADING_GLYPH = re.compile(
    r"(?P<prefix>[a-z]+)[\ue000-\uf8ff\ufffd](?P<suffix>[a-z]+)",
    re.IGNORECASE,
)
_PRIVACY_FOOTER_HEADINGS = {
    ("privacy", "and", "cookies"),
    ("privacy", "centre", "and", "ad", "choices"),
    ("privacy", "notice"),
    ("privacy", "policy"),
    ("privacy", "policy", "and", "terms"),
    ("privacy", "preference", "center"),
    ("privacy", "statement"),
}
_PRIVACY_ROLE_SUBSECTION_MODIFIERS = ((), ("data",), ("global",))
_PRIVACY_ROLE_SUBSECTION_DOMAINS = (
    ("and", "compliance"),
    ("certifications",),
    ("controls",),
    ("engineering",),
    ("governance", "and", "strategy"),
    ("impact", "assessments"),
    ("incident", "management"),
    ("operations",),
    ("program", "management"),
    ("risk", "management"),
    ("training", "and", "awareness"),
)
_PORTAL_COLLECTION_PREFIXES = (
    ("create", "job", "alert"),
    ("edit", "personalization"),
    ("edit", "profile"),
    ("find",),
    ("get", "notified", "for"),
    ("manage", "alerts"),
    ("reset", "personalization"),
    ("sign", "in", "to", "create", "job", "alert"),
)
_PORTAL_ACTION_PREFIXES = (
    ("apply", "now"),
    ("candidate", "home"),
    ("click", "here"),
    ("create", "alert"),
    ("create", "your", "profile"),
    ("current", "employees", "must", "apply", "from"),
    ("dont", "miss", "out"),
    ("help", "me", "stand", "out"),
    ("people", "you", "can", "reach", "out", "to"),
    ("search", "for", "jobs"),
    ("sign", "in"),
)
_COMPENSATION_METADATA_PREFIXES = (
    ("base", "pay", "range"),
    ("base", "salary", "range"),
    ("compensation", "range"),
    ("pay", "range"),
    ("salary", "range"),
)
_COMPENSATION_RANGE_PHRASES = (
    ("base", "pay", "range"),
    ("base", "pay", "ranges"),
    ("base", "salary", "range"),
    ("base", "salary", "ranges"),
    ("compensation", "range"),
    ("compensation", "ranges"),
    ("pay", "range"),
    ("pay", "ranges"),
    ("salary", "range"),
    ("salary", "ranges"),
)
_APPLICATION_DOCUMENT_PHRASES = (
    ("cover", "letter"),
    ("resume",),
    ("transcript",),
)
_APPLICATION_CHANNEL_PHRASES = (
    ("application",),
    ("email",),
    ("recruitment",),
    ("website",),
)
_APPLICATION_PROCESS_OBJECT_PHRASES = (
    *_APPLICATION_DOCUMENT_PHRASES,
    ("application",),
    ("email", "address"),
    ("email", "addresses"),
    ("job", "posting"),
    ("opportunity",),
    ("opportunities",),
    ("position",),
)
_APPLICATION_PROCESS_ACTIONS = {
    "apply",
    "create",
    "include",
    "submit",
    "withdraw",
}
_FOOTER_NAVIGATION_PREFIXES = (
    ("accessibility",),
    ("cookie", "preferences"),
    ("cookie", "settings"),
    ("legal",),
    ("terms",),
    ("terms", "and", "conditions"),
    ("terms", "of", "use"),
    ("your", "privacy", "choices"),
)
_FLATTENED_ROLE_TRANSITION_SECTIONS: frozenset[RoleSection] = frozenset(
    {
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
    }
)
_UNSECTIONED_METADATA_LABELS = {
    "company",
    "job title",
    "location",
    "title",
}
_POSTING_IDENTITY_LABELS = {
    "indeed source url",
    "job title",
    "linkedin source url",
    "official discovery status",
    "position title",
    "resolution evidence",
    "source evidence",
    "source resolution kind",
    "source url",
    "title",
}

# Regex is limited to lexical boundaries. Section, list, ownership, and modality
# are resolved by the parser state and phrase grammar below.
_LIST_MARKER = re.compile(r"(?:[-*|\u2022\u25cf\u25aa\ufffd]|\d+[.)])\s+")
_PRINTED_PAGE_MARKER = re.compile(
    r"^\s*(?:\d+\s+of\s+\d+|\d+\s*/\s*\d+)"
    r"(?:\s+\d{1,2}/\d{1,2}/\d{4}(?:,?\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)?\s*$",
    re.IGNORECASE,
)
_BROWSER_PRINT_TIMESTAMP_HEADER = re.compile(
    r"^\s*\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*(?:am|pm)\s+.+\|.+$",
    re.IGNORECASE,
)
_BROWSER_PRINT_URL_PAGE_MARKER = re.compile(
    r"^\s*https?://\S+\s+\d+\s*(?:/|of)\s*\d+\s*$",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"c\+\+|c#|\.[a-z0-9]+|[a-z0-9]+(?:\.[a-z0-9]+)*", re.IGNORECASE)
_HEADING_TOKEN = re.compile(r"&|[a-z0-9]+(?:['\u2019][a-z0-9]+)*", re.IGNORECASE)

_MAX_HEADING_TOKENS = 16
_RESPONSIBILITY_MODIFIERS = (
    (),
    ("job",),
    ("key",),
    ("role",),
    ("your",),
    ("your", "future"),
)
_RESPONSIBILITY_HEADS = (
    ("responsibilities",),
    ("duties", "and", "responsibilities"),
)
_RESPONSIBILITY_TAILS = ((), ("will", "include"))
_QUALIFICATION_MODIFIERS: dict[str, RoleSection] = {
    "basic": "qualifications",
    "essential": "qualifications",
    "mandatory": "qualifications",
    "minimum": "qualifications",
    "required": "qualifications",
    "desired": "preferred_qualifications",
    "optional": "preferred_qualifications",
    "preferred": "preferred_qualifications",
}
_QUALIFICATION_HEADS = (("qualification",), ("qualifications",))
_QUALIFICATION_DOMAIN_HEADS = (
    ("core", "skills"),
    ("education", "and", "certification"),
    ("professional", "attributes"),
)
_QUALIFICATION_TAILS = (
    (),
    ("and", "skills"),
    ("and", "proficiency", "with", "the", "following", "tools"),
    ("to", "be", "successful", "in", "this", "role"),
    ("proficiency", "with", "the", "following", "tools"),
)
_INLINE_LIST_MARKERS = {"*", "-", "|", "\u2022", "\u25cf", "\u25aa", "\ufffd"}
_UNAMBIGUOUS_LIST_MARKERS = {"*", "|", "\u2022", "\u25cf", "\u25aa", "\ufffd"}
_NOMINAL_COORDINATORS = {"and", "or"}
_DOTTED_ABBREVIATION = re.compile(r"(?:[A-Za-z]{1,5}\.){2,}$")
_DEPENDENT_CLAUSE_OPENERS = (
    ("although",),
    ("because",),
    ("by",),
    ("especially",),
    ("for", "example"),
    ("for", "instance"),
    ("including",),
    ("particularly",),
    ("such", "as"),
    ("though",),
    ("through",),
    ("unless",),
    ("until",),
    ("using",),
    ("via",),
    ("when",),
    ("where",),
    ("which",),
    ("while",),
    ("who",),
    ("whose",),
    ("with",),
    ("without",),
)

_EXPLICIT_REQUIRED_PHRASES = (
    ("must",),
    ("require",),
    ("requires",),
    ("need", "to"),
    ("needs", "to"),
    ("shall",),
    ("minimum",),
    ("eligible", "to"),
)
_PREFERRED_PHRASES = (
    ("preferred",),
    ("preference",),
    ("nice", "to", "have"),
    ("advantageous",),
    ("desired",),
    ("ideally",),
)
_SOFT_REQUIRED_PHRASES = (
    ("working", "toward"),
    ("working", "towards"),
    ("pursuing",),
    ("proficiency", "with"),
)
_NON_REQUIREMENT_COMPLEMENT_PATTERN = (
    r"(?:required|mandatory|essential|necessary|needed|"
    r"a\s+(?:prerequisite|requirement|must(?:[-\s]+have)?))"
)
_OPTIONALITY_PATTERNS = (
    re.compile(r"\boptional\b", re.IGNORECASE),
    re.compile(r"\bnot\s+expected\s+to\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:may|might)\s+not\s+be\s+{_NON_REQUIREMENT_COMPLEMENT_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnot\s+(?:(?:always|necessarily|strictly)\s+)*"
        rf"{_NON_REQUIREMENT_COMPLEMENT_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bneed\s+not\b", re.IGNORECASE),
    re.compile(
        r"\bdo(?:es)?\s+not\s+(?:(?:always|necessarily|strictly)\s+)*need\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:do(?:es)?n|didn)['\u2019]t\s+"
        r"(?:(?:always|necessarily|strictly)\s+)*need\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:isn|aren|wasn|weren)['\u2019]t\s+"
        r"(?:(?:always|necessarily|strictly)\s+)*"
        rf"{_NON_REQUIREMENT_COMPLEMENT_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\b(?!\s+(?:less|fewer|more)\s+than\b)"
        r"(?:(?!\b(?:but|however|yet|although)\b)[^.!?;\r\n]){0,80}?"
        r"\b(?:necessary|required|needed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwithout\s+requiring\b", re.IGNORECASE),
    re.compile(r"\bhelpful\s+but\s+not\s+required\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+expect\b.{0,80}?\bto\s+know\b", re.IGNORECASE),
)
_POSITIVE_MAY_PATTERN = re.compile(r"\bmay\b", re.IGNORECASE)
_POSITIVE_PASSIVE_PERMISSION_PATTERN = re.compile(
    r"\b(?:am|are|is|was|were|be|been|being)\s+"
    r"(?:(?:explicitly|expressly|generally|legally|specifically)\s+)*"
    r"(?:allowed|permitted)\s+to\b",
    re.IGNORECASE,
)
_MATRIX_OBLIGATION_BEFORE_PERMISSION = re.compile(
    r"\b(?:must|shall|need(?:s)?\s+to|(?:has|have|had)\s+to|"
    r"(?:required|expected)\s+to)\b"
    r"(?:(?!\b(?:although|and|but|or|though|whereas|while)\b)[^.!?;\r\n])*$",
    re.IGNORECASE,
)
_DEONTIC_SHOULD_BEFORE_LEGAL_WORK_PERMISSION = re.compile(
    r"\bshould\b"
    r"(?:(?!\b(?:although|and|but|or|though|whereas|while)\b)[^.!?;\r\n])*$",
    re.IGNORECASE,
)
_CALENDAR_MONTH_NAME_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
_CALENDAR_MAY_AFTER = re.compile(
    rf"^(?:\s+(?:(?:(?:\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|"
    rf"\d{{4}})\b|(?:through|to|until)\s+{_CALENDAR_MONTH_NAME_PATTERN}\b)|"
    rf"[-\u2013\u2014]\s*{_CALENDAR_MONTH_NAME_PATTERN}\b)",
    re.IGNORECASE,
)
_CALENDAR_MAY_CONTEXT_BEFORE = re.compile(
    r"\b(?:available|availability|begin(?:ning|s)?|between|commenc(?:e|es|ing)|from|in|on|"
    r"start(?:ing|s)?|through|until)\s*$",
    re.IGNORECASE,
)
_CALENDAR_MAY_NOUN_AFTER = re.compile(
    r"^\s+(?:availability|cohort|intake|placement|session|start|term)\b",
    re.IGNORECASE,
)
_MODAL_SCOPE_INTERVENERS = {
    "additionally",
    "also",
    "commonly",
    "even",
    "frequently",
    "generally",
    "legally",
    "occasionally",
    "often",
    "periodically",
    "possibly",
    "potentially",
    "rarely",
    "reasonably",
    "routinely",
    "sometimes",
    "still",
    "subsequently",
    "temporarily",
    "typically",
    "usually",
}
_POSITIVE_MAY_OBLIGATION_PREFIXES = (
    ("be", "essential"),
    ("be", "expected", "to"),
    ("be", "mandatory"),
    ("be", "necessary"),
    ("be", "needed"),
    ("be", "required"),
    ("have", "to"),
    ("need",),
    ("needs",),
    ("require",),
    ("requires",),
)
_NEGATIVE_POLARITY_PATTERNS = (
    re.compile(
        r"\b(?:can|could|may|must|shall|should|will|would)\s+not\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can['\u2019]t|couldn['\u2019]t|mightn['\u2019]t|mustn['\u2019]t|"
        r"shalln['\u2019]t|shouldn['\u2019]t|won['\u2019]t|wouldn['\u2019]t)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:do|does|did)\s+not\b", re.IGNORECASE),
    re.compile(r"\b(?:do(?:es)?n|didn)['\u2019]t\b", re.IGNORECASE),
    re.compile(
        r"\b(?:isn|aren|wasn|weren)['\u2019]t\s+"
        r"(?:(?:always|necessarily|strictly)\s+)*"
        rf"{_NON_REQUIREMENT_COMPLEMENT_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bneed\s+not\b", re.IGNORECASE),
    re.compile(
        r"\bnot\s+(?:required|mandatory|essential|necessary|needed|a\s+prerequisite)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:forbidden|prohibited)\s+from\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:allowed|permitted)\s+to\b", re.IGNORECASE),
    re.compile(
        r"\b(?:isn|aren|wasn|weren)['\u2019]t\s+(?:allowed|permitted)\s+to\b",
        re.IGNORECASE,
    ),
)
_MANDATORY_PATTERNS = (
    re.compile(r"\bmust\b", re.IGNORECASE),
    re.compile(r"\bshall\b", re.IGNORECASE),
    re.compile(r"\brequired\b", re.IGNORECASE),
    re.compile(r"\brequires?\b", re.IGNORECASE),
    re.compile(r"\bneed(?:s)?\s+to\b", re.IGNORECASE),
    re.compile(r"\bhave\s+to\b", re.IGNORECASE),
    re.compile(r"\bminimum\b", re.IGNORECASE),
    re.compile(r"\beligible\s+to\b", re.IGNORECASE),
    re.compile(
        r"\b(?:is|are|was|were|be)\s+(?:strictly\s+)?"
        r"(?:mandatory|essential|necessary)\b",
        re.IGNORECASE,
    ),
)
_EXPECTED_PATTERNS = (
    re.compile(r"(?<!not\s)\bexpected\s+to\b", re.IGNORECASE),
    re.compile(r"\bshould\b", re.IGNORECASE),
)
_APPLICANT_CONDITION_NP_PATTERN = (
    r"(?:(?:the\s+only|only\s+the|all|any|each|every|only|the)\s+)?"
    r"(?:(?:eligible|ideal|new|prospective|qualified|successful)\s+)*"
    r"(?:applicants?|candidates?)\b"
)
_CONDITION_PROPER_NAME_WORD_PATTERN = r"[A-Z][\w.'-]*"
_CONDITION_PROPER_NAME_PARTICLE_PATTERN = (
    r"(?i:and|da|de|del|della|des|di|do|dos|du|la|las|le|los|of|the|upon|van|von)"
)
_CONDITION_PROPER_NAME_PATTERN = (
    rf"{_CONDITION_PROPER_NAME_WORD_PATTERN}"
    rf"(?:\s+(?:{_CONDITION_PROPER_NAME_PARTICLE_PATTERN}\s+)*"
    rf"{_CONDITION_PROPER_NAME_WORD_PATTERN})*"
)
_CONDITION_TEMPORAL_PERIOD_PATTERN = (
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)(?:\s+\d{4})?|"
    r"(?:spring|summer|fall|autumn|winter)(?:\s+\d{4})?|"
    r"(?:q[1-4]|(?:first|second|third|fourth)\s+quarter)"
    r"(?:\s+(?:of\s+)?\d{4})?|"
    r"\d{4}"
)
_PREPOSED_CONDITION_PATTERN = re.compile(
    r"^\s*(?P<condition>(?:"
    rf"(?i:for\s+{_APPLICANT_CONDITION_NP_PATTERN})[^,;:]*|"
    rf"(?i:in)\s+(?:(?i:the)\s+)?(?:{_CONDITION_PROPER_NAME_PATTERN}|"
    rf"(?i:{_CONDITION_TEMPORAL_PERIOD_PATTERN}))|"
    r"(?i:when|where|if|unless|during)\b[^,;:]*|"
    r"(?i:as\s+needed)"
    r"))\s*,"
)
_TAIL_CONDITION_START_PATTERN = re.compile(
    rf"\b(?:for\s+{_APPLICANT_CONDITION_NP_PATTERN}|"
    r"in\s+(?:the\s+)?(?:[A-Za-z][\w.-]*|\d{4})|"
    r"when|where|if|unless|during|as\s+needed)\b",
    re.IGNORECASE,
)
_KNOWN_CONDITION_CITIES = {
    "calgary",
    "edmonton",
    "halifax",
    "montreal",
    "ottawa",
    "quebec city",
    "regina",
    "rio de janeiro",
    "saskatoon",
    "toronto",
    "vancouver",
    "victoria",
    "waterloo",
    "winnipeg",
}
_NON_LOCATION_PROPER_CONDITION_NAMES = {
    "accounting",
    "customer support",
    "design",
    "development",
    "engineering",
    "finance",
    "human resources",
    "information technology",
    "legal",
    "marketing",
    "operations",
    "product",
    "production",
    "procurement",
    "recruiting",
    "research",
    "review",
    "sales",
    "security",
    "support",
}
_CONDITION_CAPABILITY_GOVERNORS = {
    "experienced",
    "fluent",
    "proficient",
    "skilled",
    "work",
}
_CONDITION_GEOGRAPHIC_GOVERNORS = {
    "based",
    "live",
    "located",
    "reside",
}
_CONDITION_GENERIC_QUALIFICATION_MODIFIERS = {
    "a",
    "an",
    "any",
    "bachelor",
    "bachelors",
    "demonstrated",
    "equivalent",
    "extensive",
    "minimum",
    "of",
    "prior",
    "previous",
    "related",
    "relevant",
    "strong",
    "the",
}
_SCALAR_COMPARATOR_PATTERN = (
    r"(?:above|below|better|fewer|greater|higher|less|lower|more|older|over|under|"
    r"worse|younger)"
)
_SCALAR_BOUND_FOLLOWER_PATTERN = (
    r"(?:$|[.,;:)]|(?:of|than)\b|"
    r"(?:am|are|is|was|were|be|been|being|will|would)\b|"
    r"(?:required|preferred|desired|accepted)\b|"
    r"to\s+(?:apply|be|qualify|work)\b|"
    r"(?:at|by|for|in|on|upon|when)\b)"
)
_SCALAR_OR_BOUND_PATTERN = re.compile(
    rf"\bor\s+{_SCALAR_COMPARATOR_PATTERN}\b"
    rf"(?=\s*{_SCALAR_BOUND_FOLLOWER_PATTERN})",
    re.IGNORECASE,
)
_SUPPLEMENTAL_ALTERNATIVE_MODIFIER_PATTERN = re.compile(
    r"^(?:preferably|ideally|especially|particularly|including|for example|e\.g\.)\b",
    re.IGNORECASE,
)
_SCALAR_QUANTITY_PATTERN = (
    r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"
)
_SCALAR_UNIT_PATTERN = (
    r"(?:courses?|days?|hours?|months?|projects?|semesters?|shifts?|terms?|weeks?|years?)"
)
_QUANTIFIED_SCALAR_OR_BOUND_PATTERN = re.compile(
    rf"\b{_SCALAR_QUANTITY_PATTERN}\s+"
    rf"or\s+(?:(?:fewer|less|more)|{_SCALAR_QUANTITY_PATTERN})\s+"
    rf"{_SCALAR_UNIT_PATTERN}\b",
    re.IGNORECASE,
)
_SCALAR_OR_BOUND_PATTERNS = (
    _SCALAR_OR_BOUND_PATTERN,
    _QUANTIFIED_SCALAR_OR_BOUND_PATTERN,
)
_PROHIBITED_PATTERNS = (
    re.compile(
        r"\b(?:can|could|may|might|must|shall|should|will|would)\s+not\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can['\u2019]t|couldn['\u2019]t|mightn['\u2019]t|mustn['\u2019]t|"
        r"shalln['\u2019]t|shouldn['\u2019]t|won['\u2019]t|wouldn['\u2019]t)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:do|does|did)\s+not\b", re.IGNORECASE),
    re.compile(r"\b(?:do(?:es)?n|didn)['\u2019]t\b", re.IGNORECASE),
    re.compile(
        r"\b(?:is|are|be)\s+(?:strictly\s+)?(?:forbidden|prohibited)\s+from\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:is|are|was|were|be)\s+not\s+(?:allowed|permitted)\s+to\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:isn|aren|wasn|weren)['\u2019]t\s+(?:allowed|permitted)\s+to\b",
        re.IGNORECASE,
    ),
)
_PREFERENCE_PATTERNS = (
    re.compile(r"\bisconsideredan\s+asset\b", re.IGNORECASE),
    re.compile(r"^\s*preferred(?:\s+qualifications?)?\b", re.IGNORECASE),
    re.compile(r"^\s*prefer\s+to\s+have\b", re.IGNORECASE),
    re.compile(r"\b(?:highly\s+)?desirable\b", re.IGNORECASE),
    re.compile(r"^\s*Relevant\s+exposure\s+may\s+include\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are|was|were|would\s+be)\s+preferred\b", re.IGNORECASE),
    re.compile(
        r"\b(?:preferred|desired|advantageous)\b"
        r"(?=\s+(?:for|in|when|where|if)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:qualifications?|experience|skills?|knowledge|background|familiarity|"
        r"proficiency|designation|degree|platforms?)\s+preferred\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:preferred|desired|advantageous)\s*[.!?]?$", re.IGNORECASE),
    re.compile(
        r"\bpreference(?!\s+(?:optimization|learning|model(?:ing|ling)))"
        r"(?:\s+will\s+be\s+given)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*Strong\s+assets?\s+include\b", re.IGNORECASE),
    re.compile(r"\bnice\s+to\s+have\b", re.IGNORECASE),
    re.compile(r"\bnice[-\s]+to[-\s]+have\b", re.IGNORECASE),
    re.compile(r"\bbonus\s+points\s+for\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are|would\s+be|considered)\s+(?:an?\s+)?(?:assets?|plus|advantage)\b", re.IGNORECASE),
    re.compile(r"\bwould\s+be\s+a\s+benefit\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are)\s+strong\s+assets?\b", re.IGNORECASE),
    re.compile(r"^\s*(?:an\s+asset|a\s+plus|an\s+advantage)\b", re.IGNORECASE),
    re.compile(r"(?<!as\s)\b(?:an\s+(?:asset|advantage)|a\s+plus)\s*[.!?]?$", re.IGNORECASE),
    re.compile(
        r"^\s*desired\s+(?:qualifications?|experience|skills?|knowledge|background|"
        r"familiarity|proficiency)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:qualifications?|experience|skills?|knowledge|background|familiarity|"
        r"proficiency)\s+(?:is\s+)?desired\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:is|are|would\s+be)\s+advantageous\b", re.IGNORECASE),
    re.compile(
        r"\b(?:experience|background|education|degree|skills?)\s+may\s+be\s+considered\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*ideally\b", re.IGNORECASE),
)
_APPLICANT_NOUN_HEADS = {
    "applicant",
    "applicants",
    "candidate",
    "candidates",
    "employee",
    "employees",
    "hire",
    "hires",
    "participant",
    "participants",
}
_APPLICANT_PRENOMINAL_MODIFIERS = {
    "hired",
    "ideal",
    "incoming",
    "new",
    "newly",
    "prospective",
    "successful",
}
_APPLICANT_PRONOUNS = (("you",),)
_COMPANY_NOUN_PHRASES = (
    ("company",),
    ("organization",),
    ("team",),
)
_COMPANY_PRONOUNS = (("we",), ("our",))
_ROLE_NOUN_PHRASES = (
    ("job",),
    ("opportunity",),
    ("position",),
    ("role",),
)
_DETERMINERS = {"a", "an", "that", "the", "this"}
_DESCRIPTIVE_PREDICATE_PREFIXES = (
    ("will",),
    ("would",),
    ("can",),
    ("could",),
    ("may",),
    ("might",),
    ("get", "to"),
)
_HARD_REQUIREMENT_PREDICATE_PREFIXES = (
    ("must",),
    ("shall",),
    ("need", "to"),
    ("needs", "to"),
    ("is", "required"),
    ("are", "required"),
    ("will", "be", "required"),
    ("would", "be", "required"),
)
_POSSESSION_PREDICATE_PREFIXES = (
    ("have",),
    ("has",),
    ("hold",),
    ("holds",),
    ("possess",),
    ("possesses",),
    ("bring",),
    ("brings",),
    ("with",),
)
_CONTEXTUAL_POSSESSION_COMPLEMENT_PREFIXES = (
    ("access", "to"),
    ("an", "opportunity", "to"),
    ("the", "opportunity", "to"),
    ("opportunities", "to"),
)
_DISQUALIFYING_POSSESSION_COMPLEMENTS = (
    ("a", "criminal", "record"),
    ("criminal", "record"),
    ("criminal", "records"),
    ("a", "criminal", "conviction"),
    ("criminal", "conviction"),
    ("criminal", "convictions"),
    ("a", "felony", "conviction"),
    ("felony", "conviction"),
    ("felony", "convictions"),
    ("a", "conflict", "of", "interest"),
    ("conflict", "of", "interest"),
    ("conflicts", "of", "interest"),
)
_DISQUALIFYING_COMPLEMENT_LICENSERS = (
    ("have",),
    ("has",),
    ("hold",),
    ("holds",),
    ("possess",),
    ("possesses",),
    ("no",),
    ("without",),
    ("free", "of"),
    ("free", "from"),
    ("devoid", "of"),
    ("clear", "of"),
)
_DISQUALIFYING_COMPLEMENT_DETERMINERS = {"a", "an", "any", "no", "the"}
_EXCLUDED_LOCATION_STATES = (
    ("be", "based", "outside"),
    ("be", "located", "outside"),
    ("live", "outside"),
    ("reside", "outside"),
)
_MINIMUM_AGE_LOWER_BOUND_PREDICATES = (
    ("be", "under"),
    ("be", "below"),
    ("be", "younger", "than"),
    ("be", "less", "than"),
)
_UNDERAGE_STATE_PREDICATES = (
    ("be", "a", "minor"),
    ("be", "minors"),
    ("be", "underage"),
)
_AGE_BOUNDARY_HEADS = {"at", "by", "during", "on", "upon", "when"}
_AGE_MEASURE_HEADS = {"age", "old", "year", "years"}
_AGE_VALUE_PREFIXES = (("be",), ("be", "age"), ("be", "aged"))
_AGE_VALUE_MEASURE_PHRASES = (
    ("year", "old"),
    ("years", "old"),
    ("year", "of", "age"),
    ("years", "of", "age"),
)
_MINIMUM_AGE_COORDINATED_BOUND_SUFFIXES = (
    ("or", "below"),
    ("or", "under"),
    ("or", "younger"),
)
_QUALIFICATION_COMPLEMENT_HEADS = {
    "abilities",
    "ability",
    "authorization",
    "availability",
    "background",
    "certificate",
    "certification",
    "clearance",
    "credential",
    "credentials",
    "degree",
    "designation",
    "diploma",
    "education",
    "eligibility",
    "experience",
    "expertise",
    "familiar",
    "familiarity",
    "fluency",
    "interest",
    "knowledge",
    "licence",
    "license",
    "mobility",
    "proficiency",
    "skill",
    "skills",
    "transportation",
    "understanding",
    "vehicle",
    "willingness",
}
_UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS = {
    "application",
    "applications",
    "codebase",
    "codebases",
    "component",
    "components",
    "infrastructure",
    "platform",
    "platforms",
    "product",
    "products",
    "service",
    "services",
    "software",
    "solution",
    "solutions",
    "system",
    "systems",
    "tool",
    "tools",
    "website",
    "websites",
}
_UNSECTIONED_FINITE_PREDICATE_PATTERN = re.compile(
    r"\b(?:am|are|is|was|were|be|been|being|can|could|did|do|does|get|gets|got|"
    r"had|has|have|may|might|must|shall|should|will|would|builds|contains|"
    r"delivers|enables|handles|includes|needs|operates|powers|processes|"
    r"provides|requires|runs|serves|supports|uses)\b",
    re.IGNORECASE,
)
_UNSECTIONED_SUBJECT_PREFIX_BLOCKERS = {
    "about",
    "against",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "onto",
    "through",
    "to",
    "toward",
    "towards",
    "using",
    "via",
    "with",
    "without",
}
_UNSECTIONED_QUALIFICATION_NOMINAL_MODIFIERS = {
    "administration",
    "analytics",
    "architecture",
    "automation",
    "deployment",
    "design",
    "development",
    "engineering",
    "implementation",
    "integration",
    "leadership",
    "maintenance",
    "management",
    "migration",
    "operations",
    "quality",
    "security",
    "support",
    "testing",
    "usage",
    "user",
}
_APPLICANT_ATTRIBUTE_PHRASES = (
    ("attendance",),
    ("professional", "conduct"),
    ("professionalism",),
    ("punctuality",),
)
_EMPLOYMENT_CONDITION_PREMODIFIERS = {
    "any",
    "domestic",
    "extensive",
    "frequent",
    "international",
    "limited",
    "local",
    "occasional",
    "periodic",
    "regular",
    "some",
}
_EMPLOYMENT_CONDITION_COMPLEMENTS = (
    ("overtime",),
    ("relocation",),
    ("travel",),
    ("evening", "work"),
    ("holiday", "work"),
    ("night", "work"),
    ("overnight", "work"),
    ("shift", "work"),
    ("weekend", "work"),
    ("work", "evenings"),
    ("work", "holidays"),
    ("work", "nights"),
    ("work", "weekends"),
)
_EMPLOYMENT_CONDITION_COORDINATORS = {"and", "or"}
_EMPLOYMENT_CONDITION_ADJUNCT_HEADS = {"if", "when", "whenever", "where"}
_UNSECTIONED_RESPONSIBILITY_HEADS = {
    "adapt",
    "adhere",
    "analyze",
    "analyse",
    "assist",
    "build",
    "clean",
    "collaborate",
    "collect",
    "conduct",
    "contribute",
    "coordinate",
    "create",
    "deliver",
    "design",
    "develop",
    "diagnose",
    "document",
    "drive",
    "employ",
    "engage",
    "ensure",
    "escalate",
    "estimate",
    "expand",
    "follow",
    "help",
    "identify",
    "implement",
    "improve",
    "incorporate",
    "install",
    "investigate",
    "lead",
    "leverage",
    "liaise",
    "maintain",
    "manage",
    "monitor",
    "model",
    "organize",
    "own",
    "partner",
    "participate",
    "perform",
    "prepare",
    "provide",
    "produce",
    "research",
    "resolve",
    "respond",
    "review",
    "ship",
    "summarize",
    "support",
    "test",
    "troubleshoot",
    "understand",
    "use",
    "validate",
    "write",
}
_ROLE_ENVIRONMENT_SUBJECT_PREFIXES = (
    ("these", "platforms"),
    ("these", "services"),
    ("these", "systems"),
    ("these", "tools"),
)
_ROLE_ENVIRONMENT_PREDICATE_PHRASES = (
    ("are", "built", "on"),
    ("are", "built", "with"),
    ("are", "powered", "by"),
    ("leverage",),
    ("run", "on"),
    ("use",),
    ("utilize",),
    ("utilizes",),
)
_UNSECTIONED_EMPLOYER_PROMISE_PREDICATES = (
    ("are", "eligible", "for"),
    ("are", "offered"),
    ("are", "provided", "with"),
    ("be", "eligible", "for"),
    ("be", "offered"),
    ("be", "provided", "with"),
    ("benefit", "from"),
    ("have", "access", "to"),
    ("is", "eligible", "for"),
    ("is", "offered"),
    ("is", "provided", "with"),
)
_UNSECTIONED_EMPLOYER_PROMISE_TRANSPARENT_PREFIXES = (
    ("expect", "to"),
)
_UNSECTIONED_BENEFIT_PROMISE_PREDICATES = {
    "earn",
    "enjoy",
    "get",
    "receive",
}
_UNSECTIONED_BENEFIT_COMPLEMENTS = (
    ("benefit",),
    ("benefits",),
    ("bonus",),
    ("bonuses",),
    ("coaching",),
    ("company", "laptop"),
    ("compensation",),
    ("coverage",),
    ("dental",),
    ("equity",),
    ("experience",),
    ("flexible", "scheduling"),
    ("gym", "membership"),
    ("health", "insurance"),
    ("insurance",),
    ("leave",),
    ("learning", "opportunities"),
    ("medical",),
    ("mentoring",),
    ("mentorship",),
    ("paid", "time", "off"),
    ("pension",),
    ("perks",),
    ("professional", "development"),
    ("reimbursement",),
    ("retirement",),
    ("salary",),
    ("stipend",),
    ("stock", "options"),
    ("training",),
    ("vacation",),
    ("vision",),
    ("wellness",),
)
_MATRIX_SCOPE_COMPLEMENT_HEADS = {
    "be",
    "communicate",
    "confirm",
    "demonstrate",
    "ensure",
    "establish",
    "have",
    "hold",
    "maintain",
    "possess",
    "provide",
    "prove",
    "show",
    "support",
    "travel",
    "verify",
    "work",
}
_REQUIREMENT_COORDINATE_PREDICATE_HEADS = {
    *_UNSECTIONED_RESPONSIBILITY_HEADS,
    *_MATRIX_SCOPE_COMPLEMENT_HEADS,
    "bring",
    "complete",
    "earn",
    "know",
    "obtain",
    "receive",
    "use",
}
_ATTRIBUTIVE_PREDICATE_HOMONYMS = {
    "design",
    "lead",
    "research",
    "support",
    "test",
    "travel",
    "work",
}
_NOMINAL_LIST_GOVERNOR_HEADS = {
    "demonstrate",
    "have",
    "hold",
    "know",
    "possess",
    "prove",
    "show",
    "use",
}
_NOMINAL_COORDINATE_HEADS = {
    *_QUALIFICATION_COMPLEMENT_HEADS,
    *_UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS,
    "analyses",
    "analysis",
    "approaches",
    "concepts",
    "frameworks",
    "methodologies",
    "methods",
    "operations",
    "practices",
    "principles",
    "procedures",
    "processes",
    "standards",
    "techniques",
    "technologies",
    "technology",
}
_NOMINAL_COORDINATE_POSTMODIFIER_HEADS = {
    "across",
    "among",
    "around",
    "at",
    "by",
    "during",
    "for",
    "from",
    "in",
    "of",
    "on",
    "over",
    "that",
    "through",
    "to",
    "under",
    "which",
    "with",
    "within",
    "without",
}
_MATRIX_PREDICATE_CHAIN_TOKENS = {
    "able",
    "also",
    "expected",
    "ideally",
    "not",
    "prepared",
    "ready",
    "required",
    "successfully",
    "to",
    "typically",
    "usually",
    "willing",
}
_EMBEDDED_PREDICATE_SCOPE_MARKERS = {
    "that",
    "where",
    "which",
    "who",
    "whom",
    "whose",
}
_OPEN_PREDICATE_COMPLEMENT_HEADS = {
    "a",
    "an",
    "any",
    "as",
    "at",
    "for",
    "from",
    "in",
    "into",
    "its",
    "my",
    "on",
    "onto",
    "our",
    "some",
    "that",
    "the",
    "their",
    "these",
    "this",
    "those",
    "to",
    "with",
    "without",
    "your",
}
_STRONG_OPEN_ROLE_TAILS = {
    "about",
    "apply",
    "that",
    "undergo",
    "which",
    "who",
    "whom",
    "whose",
}
_OPEN_NOMINAL_MODIFIER_TAILS = {
    "additional",
    "more",
    "vector",
}
_ROLE_CHANGE_OF_STATE_HEADS = {
    "change",
    "evolve",
    "expand",
    "grow",
    "mature",
    "scale",
}
_ROLE_PROPERTY_HEADS = {
    "exist",
    "remain",
}
_ROLE_CHANGE_PROPERTY_COMPLEMENT_HEADS = {
    "big",
    "better",
    "bigger",
    "broad",
    "broader",
    "complex",
    "far",
    "farther",
    "fast",
    "faster",
    "further",
    "high",
    "higher",
    "large",
    "larger",
    "long",
    "longer",
    "low",
    "lower",
    "mature",
    "short",
    "shorter",
    "small",
    "smaller",
    "strong",
    "stronger",
    "wide",
    "wider",
    "worse",
}
_ROLE_CHANGE_PROPERTY_LINK_HEADS = {"and", "or", "than"}
_PREPOSITION_HEADS = {
    "about",
    "above",
    "across",
    "after",
    "against",
    "along",
    "alongside",
    "amid",
    "among",
    "around",
    "as",
    "at",
    "before",
    "behind",
    "below",
    "beneath",
    "beside",
    "between",
    "beyond",
    "by",
    "despite",
    "down",
    "during",
    "for",
    "from",
    "in",
    "inside",
    "into",
    "near",
    "of",
    "off",
    "on",
    "onto",
    "opposite",
    "outside",
    "over",
    "past",
    "per",
    "through",
    "throughout",
    "to",
    "toward",
    "towards",
    "under",
    "underneath",
    "until",
    "up",
    "upon",
    "via",
    "with",
    "within",
    "without",
}
_DEGREE_MODIFIER_HEADS = {"less", "more", "much"}
_ORGANIZATIONAL_APPLICANT_TARGET_PREDICATES = (
    ("ask",),
    ("asks",),
    ("expect",),
    ("expects",),
    ("invite",),
    ("invites",),
    ("require",),
    ("requires",),
    ("seek",),
    ("seeks",),
)
_IMPLICIT_APPLICANT_REQUIREMENT_PREDICATES = (
    ("require",),
    ("requires",),
    ("seek",),
    ("seeks",),
)
_APPLICANT_TARGET_DETERMINERS = {
    "a",
    "all",
    "an",
    "any",
    "each",
    "every",
    "our",
    "the",
    "their",
}
_NON_NEGATING_NO_PREFIXES = (
    ("earlier", "than"),
    ("fewer", "than"),
    ("later", "than"),
    ("less", "than"),
    ("more", "than"),
)
_PRIVATIVE_PREDICATES = (
    ("without",),
    ("free", "of"),
    ("free", "from"),
    ("devoid", "of"),
    ("clear", "of"),
    ("lack",),
    ("lacking",),
    ("lacks",),
    ("neither",),
)
_PRIVATIVE_NEGATION_INTERVENERS = {
    "be",
    "been",
    "being",
    "completely",
    "currently",
    "entirely",
    "fully",
    "generally",
    "legally",
    "necessarily",
    "typically",
    "usually",
    "wholly",
}
_PRIVATIVE_COMPOUND_HYPHENS = frozenset({"-", "\u2010", "\u2011"})
_NEGATIVE_ACTION_AUXILIARY_PREFIXES = (
    ("can", "not"),
    ("can", "t"),
    ("cannot",),
    ("could", "not"),
    ("couldn", "t"),
    ("do", "not"),
    ("does", "not"),
    ("did", "not"),
    ("don", "t"),
    ("doesn", "t"),
    ("didn", "t"),
    ("may", "not"),
    ("might", "not"),
    ("mightn", "t"),
    ("must", "not"),
    ("mustn", "t"),
    ("shall", "not"),
    ("shalln", "t"),
    ("should", "not"),
    ("shouldn", "t"),
    ("will", "not"),
    ("won", "t"),
    ("would", "not"),
    ("wouldn", "t"),
    ("are", "forbidden", "from"),
    ("are", "not", "allowed", "to"),
    ("are", "not", "permitted", "to"),
    ("aren", "t", "allowed", "to"),
    ("aren", "t", "permitted", "to"),
    ("are", "prohibited", "from"),
    ("is", "forbidden", "from"),
    ("is", "not", "allowed", "to"),
    ("is", "not", "permitted", "to"),
    ("isn", "t", "allowed", "to"),
    ("isn", "t", "permitted", "to"),
    ("is", "prohibited", "from"),
    ("was", "not", "allowed", "to"),
    ("was", "not", "permitted", "to"),
    ("wasn", "t", "allowed", "to"),
    ("wasn", "t", "permitted", "to"),
    ("were", "not", "allowed", "to"),
    ("were", "not", "permitted", "to"),
    ("weren", "t", "allowed", "to"),
    ("weren", "t", "permitted", "to"),
)
_SUBJECT_TRAILING_AUXILIARIES = {
    "are",
    "be",
    "been",
    "being",
    "can",
    "could",
    "is",
    "may",
    "might",
    "must",
    "shall",
    "should",
    "was",
    "were",
    "will",
    "would",
}
_RESUMPTIVE_CLAIM_SUBJECTS = {"both", "it", "they", "these", "those"}
_EMBEDDED_APPLICANT_CONDITION_PATTERN = re.compile(
    rf"^\s*{_APPLICANT_CONDITION_NP_PATTERN}\s*"
    r"(?P<condition>(?:(?:who|that)\b|(?:based|located)\s+in\b|"
    r"(?:from|in|outside|within)\b)[^,;:]*)$",
    re.IGNORECASE,
)
_PREPOSED_INVERTED_SHOULD_CONDITION_PATTERN = re.compile(
    r"^\s*(?P<condition>should\b[^,;:]+),\s+(?P<matrix>\S.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextFragment:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class PropositionFragment:
    text: str
    start: int
    end: int
    inherits_subject: bool = False
    inherited_modality: DistributedModality | None = None
    explicit_owner: Literal["applicant"] | None = None


@dataclass(frozen=True)
class HeadingToken:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class HeadingMatch:
    section: RoleSection
    start: int
    end: int


@dataclass(frozen=True)
class ModalityCue:
    modality: Literal["mandatory", "expected", "preferred", "optional", "prohibited"]
    cue_start: int
    cue_end: int
    scope_start: int
    scope_end: int
    confidence: float


@dataclass(frozen=True)
class ClauseSignals:
    tokens: tuple[str, ...]
    owner: ClauseOwner
    applicant_predicate: ApplicantPredicate
    explicit_required: bool
    expected: bool
    soft_required: bool
    preferred: bool
    non_requirement: bool
    prohibited: bool
    negative_modal_force: NegativeModalForce
    modality_cues: tuple[ModalityCue, ...]

    @property
    def has_modality(self) -> bool:
        return any(
            (
                self.explicit_required,
                self.expected,
                self.soft_required,
                self.preferred,
                self.non_requirement,
                self.prohibited,
            )
        )


@dataclass(frozen=True)
class RoleDocument:
    evidence: list[RoleEvidence]
    unsectioned_text: str
    source_fragments: tuple[tuple[RoleSection, TextFragment], ...] = ()


@dataclass(frozen=True)
class RoleSourceLine:
    text: str
    start: int
    next_physical_index: int


def parse_role_document(posting_text: str) -> RoleDocument:
    evidence: list[RoleEvidence] = []
    unsectioned: list[str] = []
    source_fragments: list[tuple[RoleSection, TextFragment]] = []
    section: RoleSection = "unsectioned"
    company_applicant_scope = False
    candidate_role_content_seen = False
    bilingual_continuation_expected = False
    skipping_interleaved_page_chrome = False
    interleaved_page_marker_seen = False
    source_lines = list(document_for_text(posting_text).physical_lines)
    logical_lines = _logical_role_source_lines(posting_text, source_lines)
    skip_initial_title = _has_unlabelled_aggregator_title(logical_lines)

    for line_index, source_line in enumerate(logical_lines):
        stripped_source_line = source_line.text.strip()
        if re.search(
            r"\bEnglish\s+will\s+follow\b",
            stripped_source_line,
            re.IGNORECASE,
        ):
            bilingual_continuation_expected = True
        if line_index == 0 and skip_initial_title:
            continue
        if skipping_interleaved_page_chrome:
            if _is_printed_page_marker(stripped_source_line):
                interleaved_page_marker_seen = True
                continue
            if not interleaved_page_marker_seen or not stripped_source_line:
                continue
            skipping_interleaved_page_chrome = False
            interleaved_page_marker_seen = False
            if section == "boundary":
                inferred_section = _role_page_continuation_section(
                    stripped_source_line
                )
                if inferred_section is not None:
                    section = inferred_section

        if (
            section
            in {
                "responsibilities",
                "qualifications",
                "preferred_qualifications",
                "role_overview",
                "capabilities",
            }
            and _is_interleaved_portal_panel_start(stripped_source_line)
            and _portal_panel_precedes_printed_page_marker(
                source_lines,
                source_line.next_physical_index,
            )
        ):
            skipping_interleaved_page_chrome = True
            interleaved_page_marker_seen = False
            continue

        segments = _section_segments(
            source_line.text,
            source_line.start,
            inherited_section=section,
        )
        if not segments:
            if section == "company":
                company_applicant_scope = False
            continue

        for heading_section, fragment in segments:
            if heading_section is not None:
                if heading_section == "posting_identity":
                    if (
                        bilingual_continuation_expected
                        and re.match(
                            r"-*\s*Job\s+Title\s*:",
                            stripped_source_line,
                            re.IGNORECASE,
                        )
                    ):
                        section = "boundary"
                        company_applicant_scope = False
                        bilingual_continuation_expected = False
                        continue
                    if any(
                        item.kind in {"responsibility", "requirement"}
                        for item in evidence
                    ):
                        return RoleDocument(
                            evidence=evidence,
                            unsectioned_text="\n".join(unsectioned),
                            source_fragments=tuple(source_fragments),
                        )
                    section = "unsectioned"
                    company_applicant_scope = False
                    continue
                if heading_section == "terminal":
                    if (
                        _normalize_heading(source_line.text) == "job openings"
                        and not candidate_role_content_seen
                    ):
                        section = "unsectioned"
                        company_applicant_scope = False
                        continue
                    return RoleDocument(
                        evidence=evidence,
                        unsectioned_text="\n".join(unsectioned),
                        source_fragments=tuple(source_fragments),
                    )
                if heading_section == "privacy":
                    if (
                        fragment is None
                        and (
                            candidate_role_content_seen
                            or section
                            in {
                                "responsibilities",
                                "qualifications",
                                "preferred_qualifications",
                                "role_overview",
                            }
                        )
                        and _privacy_heading_precedes_role_page_continuation(
                            source_lines,
                            source_line.next_physical_index,
                        )
                    ):
                        skipping_interleaved_page_chrome = True
                        interleaved_page_marker_seen = False
                        continue
                    if _privacy_heading_is_terminal(
                        section=section,
                        fragment=fragment,
                        next_material=_next_material_text(
                            source_lines,
                            source_line.next_physical_index,
                        ),
                    ):
                        return RoleDocument(
                            evidence=evidence,
                            unsectioned_text="\n".join(unsectioned),
                            source_fragments=tuple(source_fragments),
                        )
                elif heading_section != "continuation":
                    section = heading_section
                    company_applicant_scope = False
            if fragment is None or section == "boundary":
                continue

            if _is_non_role_posting_content(fragment.text):
                continue

            signals = _clause_signals(fragment.text)
            if section in {
                "responsibilities",
                "qualifications",
                "preferred_qualifications",
                "role_overview",
                "capabilities",
            } or (
                section == "unsectioned"
                and not _is_unsectioned_metadata(fragment.text)
            ):
                source_fragments.append((section, fragment))
            if section == "unsectioned":
                role_environment_statement = bool(
                    candidate_role_content_seen
                    and _is_role_environment_statement(fragment.text, signals.tokens)
                )
                if (
                    not _is_unsectioned_metadata(fragment.text)
                    and not (
                        signals.tokens
                        and signals.tokens[0] in _UNSECTIONED_RESPONSIBILITY_HEADS
                        and _is_unmarked_lowercase_physical_continuation(
                            posting_text,
                            fragment,
                        )
                    )
                    and (
                        _is_unsectioned_candidate(fragment.text, signals)
                        or role_environment_statement
                    )
                ):
                    if not role_environment_statement:
                        unsectioned.append(fragment.text)
                    candidate_role_content_seen = True
                    if _is_high_confidence_unsectioned_evidence(
                        fragment.text,
                        signals,
                    ) or role_environment_statement:
                        item = _section_evidence(
                            fragment,
                            section="unsectioned",
                            signals=signals,
                        )
                        if item is not None:
                            evidence.append(item)
                continue
            if section == "capabilities":
                evidence.append(_capability_evidence(fragment, signals=signals))
                candidate_role_content_seen = True
                continue

            if section == "company":
                item, company_applicant_scope = _company_evidence(
                    fragment,
                    signals=signals,
                    applicant_scope=company_applicant_scope,
                )
            else:
                item = _section_evidence(fragment, section=section, signals=signals)
            if item is not None:
                evidence.append(item)
                if item.kind != "context":
                    candidate_role_content_seen = True

    return RoleDocument(
        evidence=evidence,
        unsectioned_text="\n".join(unsectioned),
        source_fragments=tuple(source_fragments),
    )


def _has_unlabelled_aggregator_title(
    logical_lines: list[RoleSourceLine],
) -> bool:
    if len(logical_lines) < 2:
        return False
    first = logical_lines[0].text.strip()
    if (
        not first
        or first.endswith(('.', '!', '?'))
        or re.match(r"https?://", first, re.IGNORECASE)
        or _line_heading_section(first) is not None
    ):
        return False
    return any(
        re.match(r"\s*(?:Company|Location)\s*:", line.text, re.IGNORECASE)
        for line in logical_lines[1:4]
    )


def _logical_role_source_lines(
    posting_text: str,
    source_lines: list[str],
) -> list[RoleSourceLine]:
    starts: list[int] = []
    cursor = 0
    for source_line in source_lines:
        starts.append(cursor)
        cursor += len(source_line)

    logical_lines: list[RoleSourceLine] = []
    physical_index = 0
    reflow_wrapped_list_items = False
    while physical_index < len(source_lines):
        first_index = physical_index
        last_index = physical_index
        first_physical_line = source_lines[first_index].rstrip("\r\n")
        stripped_first_line = first_physical_line.lstrip()
        list_marker = _LIST_MARKER.match(stripped_first_line)
        starts_list_item = list_marker is not None
        starts_unambiguous_list_item = bool(
            list_marker is not None
            and stripped_first_line[:1] in _UNAMBIGUOUS_LIST_MARKERS
        )
        if not starts_list_item:
            heading_section = _line_heading_section(first_physical_line)
            if heading_section is not None:
                reflow_wrapped_list_items = heading_section in {
                    "capabilities",
                    "preferred_qualifications",
                    "qualifications",
                    "responsibilities",
                    "role_overview",
                }
        starts_reflowable_list_item = starts_list_item and (
            reflow_wrapped_list_items or starts_unambiguous_list_item
        )
        continue_to_sentence_boundary = False
        while last_index + 1 < len(source_lines):
            current_end = starts[last_index] + len(
                source_lines[last_index].rstrip("\r\n")
            )
            current = posting_text[starts[first_index] : current_end]
            following = source_lines[last_index + 1].rstrip("\r\n")
            if starts_reflowable_list_item:
                continues = _physical_list_item_continues(current, following)
            elif continue_to_sentence_boundary:
                continues = _physical_wrapped_role_sentence_continues(
                    current,
                    following,
                )
            else:
                continues = _physical_role_line_continues(current, following)
            if not continues:
                break
            if _role_line_ends_in_incomplete_candidate_modal(current):
                continue_to_sentence_boundary = True
            last_index += 1

        start = starts[first_index]
        end = starts[last_index] + len(source_lines[last_index].rstrip("\r\n"))
        combined = posting_text[start:end]
        if last_index > first_index and _role_line_has_open_tail(
            combined,
            lexical_tokens(combined),
        ):
            last_index = first_index
            end = start + len(source_lines[first_index].rstrip("\r\n"))
            combined = posting_text[start:end]
        logical_lines.append(
            RoleSourceLine(
                text=combined,
                start=start,
                next_physical_index=last_index + 1,
            )
        )
        physical_index = last_index + 1
    return logical_lines


def _physical_list_item_continues(current: str, following: str) -> bool:
    current = current.strip()
    following = following.strip()
    if not current or not following or current[-1] in ".?!;:":
        return False
    if _is_document_boundary(current) or _is_document_boundary(following):
        return False
    if _line_heading_section(current) is not None:
        return False
    if _line_heading_section(following) is not None:
        return False
    if _LIST_MARKER.match(following) is not None:
        return False
    if _is_non_role_posting_content(following):
        return False
    if _is_unsectioned_metadata(following) or re.match(
        r"https?://",
        following,
        re.IGNORECASE,
    ):
        return False
    return True


def _physical_role_line_continues(current: str, following: str) -> bool:
    current = current.strip()
    following = following.strip()
    if not current or not following:
        return False
    if current[-1] in ".?!;:":
        return False
    if _is_document_boundary(current) or _is_document_boundary(following):
        return False
    if _line_heading_section(current) is not None:
        return False
    if _line_heading_section(following) is not None:
        return False
    if _LIST_MARKER.match(following) is not None:
        return False
    if (
        re.search(r"\bincluding\b[^;.!?]*,\s*$", current, re.IGNORECASE)
        and re.match(r"[A-Za-z][^;.!?]{0,80};?\s*$", following)
    ):
        return True
    if (
        re.search(r"\(for\s+working\s+in\s*$", current, re.IGNORECASE)
        and re.match(r"[A-Za-z0-9][^;.!?]{0,80}\)\s*;?\s*$", following)
    ):
        return True
    if (
        re.search(r"\(for\s*$", current, re.IGNORECASE)
        and re.match(r"working\s+in\s+[^;.!?]{1,80}\)\s*;?\s*$", following, re.IGNORECASE)
    ):
        return True
    if re.search(
        r"\b(?:pursuing|completed|enrolled|studying|working\s+towards?)\b"
        r"[^.!?;:]*\b(?:a|an)\s*$",
        current,
        re.IGNORECASE,
    ) and re.match(
        r"(?:bachelor\s*['\u2019]?\s*s|degree|diploma)\b",
        following,
        re.IGNORECASE,
    ):
        return True

    continuation_clause = _trailing_sentence_clause(current)
    signals = _clause_signals(continuation_clause)
    if not _role_continuation_has_candidate_scope(continuation_clause, signals):
        continuation_clause = current
        signals = _clause_signals(current)
    if (
        _is_unsectioned_metadata(current) and continuation_clause == current
    ) or re.match(r"https?://", current, re.I):
        return False
    if not _role_continuation_has_candidate_scope(continuation_clause, signals):
        return False
    if not _role_line_needs_continuation(
        continuation_clause,
        following,
        signals.tokens,
    ):
        return False
    if _role_line_has_unclosed_delimiter(continuation_clause):
        return True
    if _role_line_has_strong_open_tail(continuation_clause, signals.tokens):
        return True
    first_word = re.search(r"[A-Za-z]", following)
    return bool(first_word is not None and first_word.group(0).islower())


def _physical_wrapped_role_sentence_continues(
    current: str,
    following: str,
) -> bool:
    current = current.strip()
    following = following.strip()
    if not current or not following or current[-1] in ".?!;:":
        return False
    if _is_document_boundary(current) or _is_document_boundary(following):
        return False
    if _line_heading_section(following) is not None:
        return False
    if _LIST_MARKER.match(following) is not None:
        return False
    if _is_non_role_posting_content(following):
        return False
    if _is_unsectioned_metadata(following) or re.match(
        r"https?://",
        following,
        re.IGNORECASE,
    ):
        return False
    return True


def _role_line_ends_in_incomplete_candidate_modal(value: str) -> bool:
    clause = _trailing_sentence_clause(value.strip())
    return _is_incomplete_candidate_modal_proposition(
        clause,
        _clause_signals(clause),
    )


def _trailing_sentence_clause(value: str) -> str:
    boundaries = tuple(re.finditer(r"[.!?]\s+(?=[A-Z])", value))
    if not boundaries:
        return value
    return value[boundaries[-1].end() :].strip()


def _role_continuation_has_candidate_scope(
    clause: str,
    signals: ClauseSignals,
) -> bool:
    starts_applicant_or_role_clause = bool(
        signals.owner in {"applicant", "role"}
        and signals.tokens
        and signals.tokens[0] not in {"if", "unless", "when", "where"}
    )
    starts_conditional_applicant_clause = bool(
        signals.owner == "applicant"
        and signals.tokens[:2]
        in {
            ("if", "you"),
            ("unless", "you"),
            ("when", "you"),
            ("where", "you"),
        }
    )
    starts_modal_clause = bool(
        signals.has_modality
        and _unsectioned_modal_has_candidate_scope(clause, signals)
    )
    starts_responsibility_clause = bool(
        signals.tokens
        and signals.tokens[0]
        in {
            *_UNSECTIONED_RESPONSIBILITY_HEADS,
            "participates",
        }
    )
    starts_possessive_qualification = bool(
        signals.tokens
        and signals.tokens[0] == "your"
        and any(
            token in _QUALIFICATION_COMPLEMENT_HEADS
            for token in signals.tokens[1:5]
        )
    )
    contains_role_clause = bool(
        re.search(r"\b(?:the|this)\s+role\b", clause, re.IGNORECASE)
    )
    return any(
        (
            starts_applicant_or_role_clause,
            starts_conditional_applicant_clause,
            starts_modal_clause,
            starts_responsibility_clause,
            starts_possessive_qualification,
            contains_role_clause,
        )
    )


def _is_unmarked_lowercase_physical_continuation(
    posting_text: str,
    fragment: TextFragment,
) -> bool:
    first_letter = next(
        (character for character in fragment.text if character.isalpha()),
        "",
    )
    if not first_letter.islower():
        return False

    line_start = max(
        posting_text.rfind("\n", 0, fragment.start),
        posting_text.rfind("\r", 0, fragment.start),
    ) + 1
    if posting_text[line_start : fragment.start].strip():
        return False

    previous_end = line_start
    while previous_end > 0 and posting_text[previous_end - 1] in "\r\n":
        previous_end -= 1
    if previous_end == 0:
        return False
    previous_start = max(
        posting_text.rfind("\n", 0, previous_end),
        posting_text.rfind("\r", 0, previous_end),
    ) + 1
    previous = posting_text[previous_start:previous_end].strip()
    if not previous or previous[-1] in ".?!;:":
        return False
    previous_clause = _trailing_sentence_clause(previous)
    if _role_continuation_has_candidate_scope(
        previous_clause,
        _clause_signals(previous_clause),
    ):
        return False

    physical_line_end = min(
        (
            boundary
            for boundary in (
                posting_text.find("\n", fragment.end),
                posting_text.find("\r", fragment.end),
            )
            if boundary >= 0
        ),
        default=len(posting_text),
    )
    physical_line = posting_text[line_start:physical_line_end]
    return bool(
        re.search(
            r"(?:^|[.!?]\s+)(?:our|we)\b|\bwe(?:['\u2019]re|\s+are)\b",
            physical_line,
            re.IGNORECASE,
        )
    )


def _role_line_needs_continuation(
    current: str,
    following: str,
    current_tokens: tuple[str, ...],
) -> bool:
    if not current_tokens:
        return False
    if _role_line_has_strong_open_tail(current, current_tokens):
        return True
    if _role_line_has_open_nominal_modifier_tail(current, current_tokens):
        return True
    following_tokens = lexical_tokens(following)
    return bool(
        following_tokens
        and (
            following_tokens[0] in {"and", "or"}
            or (
                len(current_tokens[-1]) <= 3
                and len(following_tokens[0]) >= 4
            )
        )
    )


def _role_line_has_open_tail(
    current: str,
    current_tokens: tuple[str, ...],
) -> bool:
    if not current_tokens:
        return False
    return _role_line_has_unclosed_delimiter(current) or current.rstrip().endswith(
        (",", "-", "/")
    ) or current_tokens[-1] in {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "can",
        "could",
        "for",
        "from",
        "in",
        "into",
        "may",
        "might",
        "must",
        "multiple",
        "of",
        "on",
        "or",
        "provide",
        "shall",
        "should",
        "the",
        "to",
        "will",
        "with",
        "would",
    } or len(current_tokens[-1]) == 1


def _role_line_has_strong_open_tail(
    current: str,
    current_tokens: tuple[str, ...],
) -> bool:
    if _role_line_has_open_tail(current, current_tokens):
        return True
    return bool(
        current_tokens[-1] in _STRONG_OPEN_ROLE_TAILS
        or re.search(r"\bpre[- ]employment\s*$", current, re.IGNORECASE)
    )


def _role_line_has_open_nominal_modifier_tail(
    current: str,
    current_tokens: tuple[str, ...],
) -> bool:
    del current
    return bool(current_tokens[-1] in _OPEN_NOMINAL_MODIFIER_TAILS)


def _role_line_has_unclosed_delimiter(value: str) -> bool:
    return any(
        value.count(opening) > value.count(closing)
        for opening, closing in (("(", ")"), ("[", "]"))
    )


def parse_role_evidence(posting_text: str) -> list[RoleEvidence]:
    return parse_role_document(posting_text).evidence


def derive_role_semantic_lists(
    evidence: Iterable[RoleEvidence],
) -> tuple[list[str], list[str], list[str]]:
    responsibilities: list[str] = []
    must_have: list[str] = []
    nice_to_have: list[str] = []
    for item in evidence:
        if item.claims:
            wrapped_bucket = _wrapped_evidence_output_bucket(item)
            if wrapped_bucket is not None:
                display_text = _single_line_role_text(item.text)
                if (
                    wrapped_bucket in {"responsibility", "must_have", "nice_to_have"}
                    and _is_requirement_heading_noise(display_text)
                ):
                    continue
                if wrapped_bucket == "responsibility":
                    _append_unique(responsibilities, display_text)
                elif wrapped_bucket == "must_have":
                    _append_unique(must_have, display_text)
                else:
                    _append_unique(nice_to_have, display_text)
                continue
            for claim in item.claims:
                display_text = claim.normalized_text or claim.text
                display_text = _restore_flattened_role_list_surface(display_text)
                if claim.kind == "responsibility":
                    if not _is_requirement_heading_noise(display_text):
                        _append_unique(responsibilities, display_text)
                elif claim.kind in {"qualification", "eligibility"}:
                    if _is_requirement_heading_noise(display_text):
                        continue
                    if claim.modality in {"mandatory", "expected"}:
                        _append_unique(must_have, display_text)
                    elif claim.modality == "preferred":
                        _append_unique(nice_to_have, display_text)
            continue
        if item.kind == "responsibility":
            if not _is_requirement_heading_noise(item.text):
                _append_unique(responsibilities, item.text)
        elif item.kind == "requirement" and item.modality == "required":
            if not _is_requirement_heading_noise(item.text):
                _append_unique(must_have, item.text)
        elif item.kind == "requirement" and item.modality == "preferred":
            if not _is_requirement_heading_noise(item.text):
                _append_unique(nice_to_have, item.text)
    return _deduplicate_semantic_output_lists(
        _merge_semantic_output_fragments(responsibilities, merge_dash_labels=True),
        _merge_semantic_output_fragments(_merge_wrapped_requirement_fragments(must_have)),
        _merge_semantic_output_fragments(
            _merge_wrapped_requirement_fragments(nice_to_have)
        ),
    )


def _deduplicate_semantic_output_lists(
    responsibilities: list[str],
    must_have: list[str],
    nice_to_have: list[str],
) -> tuple[list[str], list[str], list[str]]:
    def deduplicate(values: list[str], blocked: set[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            key = _semantic_output_key(value)
            if not key or key in blocked:
                continue
            blocked.add(key)
            result.append(value)
        return result

    seen: set[str] = set()
    responsibilities = deduplicate(responsibilities, seen)
    must_have = deduplicate(must_have, seen)
    nice_to_have = deduplicate(nice_to_have, seen)
    return responsibilities, must_have, nice_to_have


def _semantic_output_key(value: str) -> str:
    return " ".join(re.sub(r"[^\w+#]+", " ", value.casefold()).split())


def _merge_semantic_output_fragments(
    values: list[str],
    *,
    merge_dash_labels: bool = False,
) -> list[str]:
    """Rejoin source list labels/connectors and remove scraped bullet dashes."""

    merged: list[str] = []
    pending: str | None = None
    for raw_value in values:
        value = re.sub(r"[\s…\u200e\u3164]+$", "", raw_value).strip()
        if not value:
            continue
        if (
            merged
            and re.search(r"\bATS\s*$", merged[-1], re.IGNORECASE)
            and re.fullmatch(r"Code\s+of\s+Business", value, re.IGNORECASE)
        ):
            merged[-1] = f"{merged[-1].rstrip()} {value}"
            continue
        if (
            merged
            and re.search(r"\bATS\s+Code\s+of\s+Business\s*$", merged[-1], re.IGNORECASE)
            and re.fullmatch(r"Conduct[.!]?", value, re.IGNORECASE)
        ):
            merged[-1] = f"{merged[-1].rstrip()} {value}"
            continue
        if (
            merged
            and re.search(
                r"\bCORE\s+values\s*:\s*Have\s+a\s*$",
                merged[-1],
                re.IGNORECASE,
            )
            and re.match(r"Can[- ]Do\s+Attitude\b", value, re.IGNORECASE)
        ):
            merged[-1] = f"{merged[-1].rstrip()} {value.lstrip()}"
            continue
        if (
            merge_dash_labels
            and merged
            and re.search(
                r"\bContinuously\s+investigate\s+new\s+tools\.?$",
                merged[-1],
                re.IGNORECASE,
            )
            and re.fullmatch(r"technologies\.?", value, re.IGNORECASE)
        ):
            merged[-1] = (
                merged[-1].rstrip(".") + " and " + value.lstrip()
            )
            continue
        if pending is not None:
            pending_is_complete_responsibility = bool(
                merge_dash_labels
                and re.search(r"\bwith\s*$", pending, re.IGNORECASE)
                and re.match(r"Make\s+effective\s+tradeoffs\b", value)
            )
            if pending_is_complete_responsibility:
                _append_unique(merged, pending.strip())
                pending = None
            else:
                value = f"{pending.rstrip()} {value.lstrip()}"
                pending = None

        ends_label_dash = bool(re.search(r"\s[-–—]\s*$", value))
        first_token_match = re.match(r"[A-Za-zÀ-ÿ]+", value)
        starts_with_action = bool(
            first_token_match
            and first_token_match.group(0).casefold()
            in {
                *_UNSECTIONED_RESPONSIBILITY_HEADS,
                "applying",
                "assisting",
                "creating",
                "designing",
                "developing",
                "helping",
                "learning",
                "monitoring",
                "participating",
                "preparing",
                "responding",
                "supporting",
                "working",
            }
        )
        short_label_dash = (
            merge_dash_labels
            and ends_label_dash
            and len(value.split()) <= 7
            and not starts_with_action
        )
        connector_tail = bool(
            re.search(r"(?:\s&|\b(?:and|or|with|to|of))\s*$", value, re.IGNORECASE)
        )
        short_colon_label = value.endswith(":") and len(value.split()) <= 7
        if short_colon_label or short_label_dash or connector_tail:
            pending = value
            continue

        value = re.sub(r"\s+[-–—]\s*$", "", value).strip()
        if value and not _is_requirement_heading_noise(value):
            _append_unique(merged, value)

    if pending is not None:
        pending = re.sub(r"\s+[-–—]\s*$", "", pending).strip()
        if pending and not _is_requirement_heading_noise(pending):
            _append_unique(merged, pending)
    return merged


def _merge_wrapped_requirement_fragments(values: list[str]) -> list[str]:
    """Rejoin narrowly recognized source-line wraps after semantic projection."""

    merged: list[str] = []
    for value in values:
        if merged and (
            (
                re.search(r"\(for\s+working\s+in(?:\s+[A-Za-z]+)?\s*$", merged[-1], re.IGNORECASE)
                and re.match(r"^[A-Za-z0-9][^;.!?]{0,100}\)\s*;?\s*$", value)
            )
            or (
                re.search(r"\(for\s*$", merged[-1], re.IGNORECASE)
                and re.match(r"^working\s+in\s+[^;.!?]{1,100}\)\s*;?\s*$", value, re.IGNORECASE)
            )
            or (
                re.search(r"\band\s*$", merged[-1], re.IGNORECASE)
                and re.match(r"^[a-z][^;.!?]{1,160}[.!?]?\s*$", value)
            )
            or (
                re.search(r"\bor\s*$", merged[-1], re.IGNORECASE)
                and re.match(
                    r"^(?:Bachelor['’]?s|Master['’]?s|college|degree|diploma)\b",
                    value,
                    re.IGNORECASE,
                )
            )
            or (
                re.fullmatch(r"All", merged[-1], re.IGNORECASE)
                and re.match(
                    r"successful\s+applicants?\b",
                    value,
                    re.IGNORECASE,
                )
            )
            or (
                re.fullmatch(
                    r"One\s+of\s+the\s+following\s*:\s*A",
                    merged[-1],
                    re.IGNORECASE,
                )
                and re.match(r"Bachelor['’]?s\b", value, re.IGNORECASE)
            )
            or (
                re.fullmatch(
                    r"Current\s+enrollment\s+in\s+an?",
                    merged[-1],
                    re.IGNORECASE,
                )
                and re.match(
                    r"(?:undergraduate|graduate|degree|program)\b",
                    value,
                    re.IGNORECASE,
                )
            )
            or (
                re.search(
                    r"\b(?:undergraduate|graduate|degree|program)\s*$",
                    merged[-1],
                    re.IGNORECASE,
                )
                and re.match(
                    r"with\s+an?\b[^.!?]{1,180}\bfocus\b",
                    value,
                    re.IGNORECASE,
                )
            )
            or (
                re.search(
                    r"\bCORE\s+values\s*:\s*Have\s+a\s*$",
                    merged[-1],
                    re.IGNORECASE,
                )
                and re.match(r"Can[- ]Do\s+Attitude\b", value, re.IGNORECASE)
            )
            or (
                re.fullmatch(r"[A-Z][A-Za-z]{1,5}\.?", merged[-1])
                and re.match(
                    r"[A-Z][A-Za-z]{1,5}\s+candidates?\s+are\s+not\s+eligible\b",
                    value,
                )
            )
        ):
            separator = (
                " and "
                if re.fullmatch(r"[A-Z][A-Za-z]{1,5}\.?", merged[-1])
                and re.match(r"[A-Z][A-Za-z]{1,5}\s+candidates?\s+are\s+not\s+eligible\b", value)
                else " "
            )
            left = (
                merged[-1].rstrip(".")
                if separator == " and "
                else merged[-1].rstrip()
            )
            merged[-1] = left + separator + value.lstrip()
            continue
        merged.append(value)
    return merged


def _is_requirement_heading_noise(value: str) -> bool:
    """Reject a heading surface that survived flattened source segmentation."""

    return bool(
        re.fullmatch(
            r"\s*(?:who\s+are\s+we\s+looking\s+for|"
            r"experience|skills?|for|full[- ]time[.]?|"
            r"relocation\s+provided\s*:\s*(?:none|no)|language\s+skills|"
            r"to\s+be\s+successful\s+in\s+this\s+role\s+you\s+will\s+need|"
            r"required\s+qualifications|must[- ]have\s+qualifications|"
            r"additional\s+requirements|and\s+qualifications|"
            r"Experience\s+or\s+Interest\s+in\s*/|"
            r"In\s+this\s+role\s*,?\s+you\s+are\s+someone\s+who\s+is\s*/\s*has\s*:|"
            r"and\s*/?\s*or|successfully\s+completed\s*:|"
            r"As\s+an?\s+[^:]{1,100}\bteam\s*,\s*you\s+will\s*:|"
            r"If\s+you\s+identify\s+with\s+any\s+of\s+the\s+following\s*,?\s*"
            r"we\s+encourage\s+you\s+to\s+apply[.!?]?|"
            r"Please\s+indicate\s+your\s+availability\s+in\s+your\s+application[.!?]?|"
            r"Please\s+attach\s+your\s+most\s+recent\s+transcripts?\s+with\s+your\s+application[.!?]?|"
            r"Requisition\s*:\s*[A-Z0-9_.-]+[.!?]?|"
            r"You['’]?ll\s+be\s+most\s+successful\s+in\s+this\s+role\s+if\s+you\s*:|"
            r"How\s+We\s+Define\s+Success\s+In\s+This\s+Role|"
            r"Success\s+in\s+this\s+role\s+is\s+about\s+both\s+your\s+contribution\s+and\s+your\s+growth\s*:|"
            r"Apply\s+Here\s*:\s*[^.!?]{1,160}|"
            r"We\s+strongly\s+encourage\s+you\s+to\s+still\s+apply[.!?]?|"
            r"(?:The\s+)?(?:target\s+annual\s+compensation|annualized\s+base\s+salary\s+offered)\b[^.!?]{1,260}[.!?]?|"
            r"Office\s+workers\s+receive\s+daily\s+lunch\b[^.!?]{0,180}[.!?]?|"
            r"and\s+level\s+of\s+autonomy\s+will\s+be\s+(?:tailored|aligned)\b[^.!?]*[.!?]?|"
            r"If\s+you\s+are\s+detail[- ]oriented\b[^.!?]{0,220}"
            r"we\s+would\s+love\s+to\s+hear\s+from\s+you[.!?]?|"
            r"(?:[A-Z][A-Za-z&.'’-]*\s+)?encourages\s+all\s+qualified\s+applicants\s+"
            r"to\s+apply\b[^.!?]*[.!?]?|"
            r"Experience\s+We\s+realize\s+that\s+(?:the\s+set\s+of\s+skills(?:\s+and\s+responsibilities)?|the\s+requirements)\s+listed\s+here\s+are\s+large[^.!?]*[.!?]?|"
            r"We\s+care\s+more\s+about\s+what\s+you['’]?ve\s+built\s+than\s+where\s+you\s+learned\s+it[.!?]?|"
            r"If\s+your\s+field\s+of\s+study\s+differs\s*,?\s*apply\s*:"
            r"\s*we[^.!?]{0,180}\b(?:good\s+fit|fit)\b[^.!?]*[.!?]?)\s*[?:]?\s*",
            value,
            re.IGNORECASE,
        )
    )


def _restore_flattened_role_list_surface(value: str) -> str:
    """Restore a semantic subject consumed by an embedded flattened heading."""

    value = re.sub(r"^\s*&\s*Duties\s*", "", value, flags=re.IGNORECASE)
    for pattern, replacement in (
        (r"\bAssistwith(?=\S)", "Assist with "),
        (r"\bAssistwith\b", "Assist with"),
        (r"\bHelpidentify(?=\S)", "Help identify "),
        (r"\bHelpidentify\b", "Help identify"),
        (r"\bandmaintain(?=\S)", "and maintain "),
        (r"\bandmaintain\b", "and maintain"),
        (r"\bspreadsheets,reportsand\b", "spreadsheets, reports and"),
        (r"\bprojectmanagementand\b", "project management and"),
        (r"\bourSAPProfessionalServicesTeam\b", "our SAP Professional Services Team"),
        (r"\bcampusleadershipor\b", "campus leadership or"),
        (r"\bprocessimprovementand\b", "process improvement and"),
        (r"\bOperationsor\b", "Operations or"),
        (r"\bisconsideredan\b", "is considered an"),
        (r"\bStrongorganizational\b", "Strong organizational"),
        (r"\bStrongwritten\b", "Strong written"),
        (r"\bStrongproblem\b", "Strong problem"),
        (r"\bComfortableworking\b", "Comfortable working"),
        (r"\bawilinessto\b", "a willingness to"),
        (r"\bExperiencewith\b", "Experience with"),
        (r"\bFamiliaritywith\b", "Familiarity with"),
        (r"\bExposureto\b", "Exposure to"),
        (r"\bInterestin\b", "Interest in"),
    ):
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    if re.match(
        r"^is\s+occasionally\s+outdoors\s+with\s+exposure\b",
        value,
        re.IGNORECASE,
    ):
        return f"Physical work environment {value}"
    value = re.sub(
        r"^Successfully\s+completed\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+Physical$", "", value, flags=re.IGNORECASE)
    return value


def _wrapped_evidence_output_bucket(
    item: RoleEvidence,
) -> Literal["responsibility", "must_have", "nice_to_have"] | None:
    preserves_application_choice = bool(
        re.search(
            r"\bIndicate\s+if\b.*\bor\b.*\bor\s+both\b",
            item.text,
            re.IGNORECASE,
        )
    )
    preserves_preferred_capability_list = bool(
        re.search(
            r"\bExperience\s+with\s+any\s+of\s+the\s+following\s+is\s+"
            r"(?:a\s+)?(?:plus|preferred|an?\s+asset)\b[^:]*:",
            item.text,
            re.IGNORECASE,
        )
    )
    if (
        "\n" not in item.text
        and "\r" not in item.text
        and not preserves_application_choice
        and not preserves_preferred_capability_list
    ):
        return None
    buckets: list[Literal["responsibility", "must_have", "nice_to_have"]] = []
    for claim in item.claims:
        if claim.kind == "responsibility":
            buckets.append("responsibility")
        elif claim.kind in {"qualification", "eligibility"} and claim.modality in {
            "mandatory",
            "expected",
        }:
            buckets.append("must_have")
        elif claim.kind in {"qualification", "eligibility"} and claim.modality == "preferred":
            buckets.append("nice_to_have")
        elif (
            preserves_preferred_capability_list
            and claim.kind in {"qualification", "eligibility"}
            and claim.modality == "optional"
        ):
            buckets.append("nice_to_have")
        else:
            return None
    return buckets[0] if buckets and len(set(buckets)) == 1 else None


def _single_line_role_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return re.sub(
        r"\bfloorparticipating\b",
        "floor participating",
        normalized,
        flags=re.IGNORECASE,
    )


@session_cached
def lexical_tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN.finditer(value))


def _section_segments(
    value: str,
    absolute_start: int,
    *,
    inherited_section: RoleSection | None = None,
) -> list[tuple[RoleSection | None, TextFragment | None]]:
    if _is_document_boundary(value):
        return [("boundary", None)]

    fragment = _content_fragment(value, absolute_start)
    if fragment is None:
        return []
    section = _line_heading_section(fragment.text)
    if section is not None:
        return [(section, None)]

    matches = _inline_heading_matches(value, inherited_section=inherited_section)
    if not matches:
        return [
            (None, item)
            for item in _clause_fragments(
                value,
                absolute_start,
                section=inherited_section,
            )
        ]

    segments: list[tuple[RoleSection | None, TextFragment | None]] = []
    segments.extend(
        (None, item)
        for item in _clause_fragments(
            value[: matches[0].start],
            absolute_start,
            section=inherited_section,
        )
    )
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start if index + 1 < len(matches) else len(value)
        bodies = _clause_fragments(
            value[match.end : body_end],
            absolute_start + match.end,
            section=match.section,
        )
        if not bodies:
            segments.append((match.section, None))
            continue
        segments.append((match.section, bodies[0]))
        segments.extend((None, body) for body in bodies[1:])
    return segments


def _inline_heading_matches(
    value: str,
    *,
    inherited_section: RoleSection | None = None,
) -> list[HeadingMatch]:
    tokens = _heading_tokens(value)
    parenthetical_spans = _balanced_parenthetical_spans(value)
    contextual_candidates: list[HeadingMatch] = []
    if inherited_section == "responsibilities" or re.search(
        r"\bWhat\s+You(?:['’]?ll|\s+Will)\s+Work\s+On\b",
        value,
        re.IGNORECASE,
    ):
        contextual_candidates.extend(
            HeadingMatch(
                section="responsibilities",
                start=match.start(),
                end=match.end(),
            )
            for match in re.finditer(
                r"\b(?:Application\s+Development|Integration\s+Development|"
                r"Application\s+Support|Continuous\s+Improvement)\b",
                value,
                re.IGNORECASE,
            )
        )
    for match in re.finditer(
        r"\bWhat['’]?s\s+In\s+It\s+For\s+You\b",
        value,
        re.IGNORECASE,
    ):
        if inherited_section not in {
            "qualifications",
            "preferred_qualifications",
        } and re.search(
            r"\b(?:Bonus\s+Points|Nice\s+to\s+Have|Preferred\s+Qualifications)\b",
            value[: match.start()],
            re.IGNORECASE,
        ) is None:
            continue
        contextual_candidates.append(
            HeadingMatch(
                section="boundary",
                start=match.start(),
                end=match.end(),
            )
        )
    candidates = [
        candidate
        for candidate in [
        *_colon_heading_matches(value, tokens),
        *_bare_heading_matches(value, tokens),
        *_dynamic_inline_heading_matches(
            value,
            inherited_section=inherited_section,
        ),
        *contextual_candidates,
        ]
        if not any(
            start < candidate.start < end
            for start, end in parenthetical_spans
        )
    ]
    matches: list[HeadingMatch] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.start, -(item.end - item.start)),
    ):
        if matches and candidate.start < matches[-1].end:
            continue
        matches.append(candidate)
    return matches


def _dynamic_inline_heading_matches(
    value: str,
    *,
    inherited_section: RoleSection | None = None,
) -> list[HeadingMatch]:
    matches: list[HeadingMatch] = []
    for pattern, section in DYNAMIC_INLINE_HEADINGS:
        matches.extend(
            HeadingMatch(
                section=section,
                start=match.start(),
                end=match.end(),
            )
            for match in pattern.finditer(value)
        )
    for match in re.finditer(r"\bReporting\s+to\s+(?:the|an?)\b", value):
        if inherited_section == "responsibilities":
            continue
        if match.start() == 0 or re.search(
            r"\b(?:base\s+salary|company|full[- ]time|location|salary)\b",
            value[: match.start()],
            re.IGNORECASE,
        ):
            matches.append(
                HeadingMatch(
                    section="role_overview",
                    start=match.start(),
                    end=match.start(),
                )
            )
    return matches


def _colon_heading_matches(
    value: str,
    tokens: tuple[HeadingToken, ...],
) -> list[HeadingMatch]:
    matches: list[HeadingMatch] = []
    for colon in (
        index for index, character in enumerate(value) if character in ":?"
    ):
        token_end = next(
            (
                index
                for index in range(len(tokens) - 1, -1, -1)
                if tokens[index].end <= colon
                and not value[tokens[index].end : colon].strip()
            ),
            None,
        )
        if token_end is None:
            continue

        first = max(0, token_end - _MAX_HEADING_TOKENS + 1)
        candidates: list[HeadingMatch] = []
        for token_start in range(first, token_end + 1):
            label = tokens[token_start : token_end + 1]
            normalized_label = " ".join(token.text for token in label)
            section = _heading_section_for_tokens(tuple(token.text for token in label))
            if normalized_label == "job title" and re.search(
                r"-{2,}\s*$", value[: label[0].start]
            ):
                section = "boundary"
            contextual_boundary = bool(
                (
                    normalized_label == "job title"
                    and re.search(r"-{2,}\s*$", value[: label[0].start])
                )
                or (
                    normalized_label
                    in {
                        "compensation",
                        "compensation and benefits",
                        "compensation and equity",
                        "total rewards",
                        "what we offer",
                        "what is in it for you",
                        "whats in it for you",
                        "why work for us",
                    }
                    and re.search(
                        r"\b(?:qualifications|requirements|responsibilities|who\s+you\s+are|"
                        r"what\s+you\s+bring(?:\s+to\s+(?:this|the)\s+role)?)\b",
                        value[: label[0].start],
                        re.IGNORECASE,
                    )
                    and re.search(
                        r"\b(?:about|in|with)(?:\s+\w+){0,2}\s*$",
                        value[: label[0].start],
                        re.IGNORECASE,
                    )
                    is None
                )
                or (
                    normalized_label == "awareness unity empowerment"
                    and re.search(
                        r"\b(?:work\s+environment|manufacturing\s+operation|requirements|responsibilities)\b",
                        value[: label[0].start],
                        re.IGNORECASE,
                    )
                )
                or (
                    normalized_label == "how to apply"
                    and (
                        _is_complete_flattened_bullet_item(
                            value[: label[0].start].strip()
                        )
                        or re.search(
                            r"\b(?:qualifications|requirements|what\s+you(?:['’]?ll|\s+will)\s+bring|who\s+you\s+are)\b",
                            value[: label[0].start],
                            re.IGNORECASE,
                        )
                    )
                )
            )
            if section is None or not (
                _is_inline_heading_boundary(
                    value,
                    label[0].start,
                    section=section,
                )
                or contextual_boundary
                or (
                    section in _FLATTENED_ROLE_TRANSITION_SECTIONS
                    and _has_contextual_bare_heading_boundary(
                        value=value,
                        tokens=tokens,
                        token_start=token_start,
                        label=label,
                        section=section,
                    )
                )
            ):
                continue
            candidates.append(
                HeadingMatch(
                    section=section,
                    start=label[0].start,
                    end=colon + 1,
                )
            )
        if candidates:
            complete_label = min(candidates, key=lambda item: item.start)
            if not matches or complete_label.start >= matches[-1].end:
                matches.append(complete_label)
    return matches


@lru_cache(maxsize=1)
def _heading_start_tokens() -> frozenset[str]:
    """Index grammar productions; this cache holds grammar constants only."""
    phrases = {
        *_STANDALONE_SECTION_HEADINGS,
        *_POSTING_IDENTITY_LABELS,
        *_RESPONSIBILITY_HEADINGS,
        *_QUALIFICATION_HEADINGS,
        *_PREFERRED_HEADINGS,
        *_OVERVIEW_HEADINGS,
        *_CAPABILITY_HEADINGS,
        *_COMPANY_HEADINGS,
        *_BOUNDARY_HEADINGS,
        *_TERMINAL_HEADINGS,
    }
    starts = {phrase.split()[0] for phrase in phrases if phrase}
    starts.update({"privacy", "what", "why", "life"})
    starts.update(_QUALIFICATION_MODIFIERS)
    for productions in (
        _PRIVACY_FOOTER_HEADINGS,
        _PRIVACY_ROLE_SUBSECTION_MODIFIERS,
        _RESPONSIBILITY_MODIFIERS,
        _RESPONSIBILITY_HEADS,
        _QUALIFICATION_HEADS,
        _QUALIFICATION_DOMAIN_HEADS,
    ):
        starts.update(tokens[0] for tokens in productions if tokens)
    return frozenset(starts)


def _bare_heading_matches(
    value: str,
    tokens: tuple[HeadingToken, ...],
) -> list[HeadingMatch]:
    matches: list[HeadingMatch] = []
    for token_start, token in enumerate(tokens):
        if token.text not in _heading_start_tokens():
            continue
        upper_bound = min(len(tokens), token_start + _MAX_HEADING_TOKENS)
        candidates: list[HeadingMatch] = []
        for token_end in range(token_start + 1, upper_bound + 1):
            label = tokens[token_start:token_end]
            section = _heading_section_for_tokens(tuple(item.text for item in label))
            if section is None:
                continue
            normalized_label = " ".join(item.text for item in label)
            if (
                normalized_label == "what you bring to the team"
                and token_end < len(tokens)
                and tokens[token_end].text in _UNSECTIONED_RESPONSIBILITY_HEADS
            ):
                section = "responsibilities"
            if normalized_label in {
                "benefits",
                "compensation",
                "compensation and benefits",
            }:
                prefix = value[: label[0].start]
                suffix = value[label[-1].end :]
                if re.search(
                    r"\b(?:across|including)\b[^.!?]*$",
                    prefix,
                    re.IGNORECASE,
                ) and (
                    suffix.lstrip().startswith(",")
                    or re.match(
                        r"\s*(?:&|and)\s+Benefits\s*,",
                        suffix,
                        re.IGNORECASE,
                    )
                ):
                    continue
            if (
                normalized_label == "application"
                and token_end < len(tokens)
                and tokens[token_end].text
                in {
                    "architecture",
                    "development",
                    "engineering",
                    "onboarding",
                    "programming",
                    "security",
                    "support",
                }
            ):
                continue
            if normalized_label == "additional information" and re.match(
                r"\s+Language\s+Requirements\b",
                value[label[-1].end :],
                re.IGNORECASE,
            ):
                section = "continuation"
            if (
                normalized_label == "you will have the opportunity to"
                and re.match(
                    r"\s+(?:learn\s+from|receive\s+(?:training|coaching)|"
                    r"develop\s+your\s+skills)\b",
                    value[label[-1].end :],
                    re.IGNORECASE,
                )
            ):
                continue
            has_boundary = _is_bare_heading_boundary(
                value, token.start
            ) or _has_contextual_bare_heading_boundary(
                value=value,
                tokens=tokens,
                token_start=token_start,
                label=label,
                section=section,
            )
            if not has_boundary or not _is_bare_heading_surface(value, label):
                continue
            candidates.append(
                HeadingMatch(
                    section=section,
                    start=label[0].start,
                    end=label[-1].end,
                )
            )
        company_heading = _proper_name_about_heading(value, tokens, token_start)
        if company_heading is not None:
            candidates.append(company_heading)
        if candidates:
            matches.append(
                max(
                    candidates,
                    key=lambda item: (item.end, item.end - item.start),
                )
            )
    return matches


def _proper_name_about_heading(
    value: str,
    tokens: tuple[HeadingToken, ...],
    token_start: int,
) -> HeadingMatch | None:
    if token_start + 1 >= len(tokens) or tokens[token_start].text != "about":
        return None
    name = tokens[token_start + 1]
    name_surface = value[name.start : name.end]
    if (
        not name_surface[:1].isupper()
        or name.text in {"our", "the", "this", "you", "your"}
        or not _is_bare_heading_boundary(value, tokens[token_start].start)
    ):
        return None
    return HeadingMatch(
        section="company",
        start=tokens[token_start].start,
        end=name.end,
    )


def _heading_tokens(value: str) -> tuple[HeadingToken, ...]:
    return tuple(
        HeadingToken(
            text=(
                match.group(0)
                .casefold()
                .replace("&", "and")
                .replace("\u2019", "'")
                .replace("'", "")
            ),
            start=match.start(),
            end=match.end(),
        )
        for match in _HEADING_TOKEN.finditer(value)
    )


def _is_inline_heading_boundary(
    value: str,
    start: int,
    *,
    section: RoleSection,
) -> bool:
    prefix = value[:start].rstrip()
    if not prefix or prefix[-1] in ":.;!?|*\u2022\u25cf\u25aa":
        return True
    bullet_item = _flattened_bullet_item_before(value, start)
    if bullet_item is not None and _is_complete_flattened_bullet_item(bullet_item):
        return True
    if section not in _FLATTENED_ROLE_TRANSITION_SECTIONS:
        return False
    if section == "qualifications" and re.search(
        r"\bwhat\s+you(?:['’]?ll|\s+will)\s+gain\s*:[^:;.!?]{0,120}$",
        prefix,
        re.IGNORECASE,
    ):
        return True
    return _preceding_colon_heading_section(value, start) == "capabilities"


def _flattened_bullet_item_before(value: str, start: int) -> str | None:
    prefix = value[:start]
    last_bullet = max((prefix.rfind(marker) for marker in "*\u2022\u25cf\u25aa"), default=-1)
    if last_bullet < 0:
        return None
    last_boundary = max((prefix.rfind(marker) for marker in ":.;!?|"), default=-1)
    item_text = prefix[last_bullet + 1 :].strip()
    if last_bullet <= last_boundary or not item_text:
        return None
    return item_text


def _is_complete_flattened_bullet_item(
    item_text: str,
) -> bool:
    signals = _clause_signals(item_text)
    if not signals.tokens:
        return False
    if signals.has_modality or _has_responsibility_predicate(signals.tokens):
        return True
    if signals.tokens[0] in {
        "available",
        "enrolled",
        "experienced",
        "proficient",
    }:
        return True
    if signals.tokens[:2] == ("be", "available"):
        return True
    if signals.owner == "applicant" and signals.applicant_predicate in {
        "hard_requirement",
        "qualification",
    }:
        return True
    surface_terms = [match.group(0) for match in _HEADING_TOKEN.finditer(item_text)]
    if len(surface_terms) == 1:
        term = surface_terms[0]
        return term[0].isupper() or term.isupper()
    if _is_nominal_capability_list(item_text, signals.tokens):
        return True
    if signals.tokens[-1] in _QUALIFICATION_COMPLEMENT_HEADS:
        return True
    if signals.tokens[0] not in _QUALIFICATION_COMPLEMENT_HEADS:
        return False
    return (
        signals.tokens[-1] in _QUALIFICATION_COMPLEMENT_HEADS
        or signals.tokens[:2] == ("ability", "to")
        or bool(surface_terms and surface_terms[-1][0].isupper())
    )


def _preceding_colon_heading_section(value: str, start: int) -> RoleSection | None:
    prefix = value[:start].rstrip()
    for colon in range(len(prefix) - 1, -1, -1):
        if prefix[colon] != ":":
            continue
        tokens = _heading_tokens(prefix[:colon])
        token_end = next(
            (
                index
                for index in range(len(tokens) - 1, -1, -1)
                if not prefix[tokens[index].end : colon].strip()
            ),
            None,
        )
        if token_end is None:
            continue
        first = max(0, token_end - _MAX_HEADING_TOKENS + 1)
        for token_start in range(first, token_end + 1):
            section = _heading_section_for_tokens(
                tuple(token.text for token in tokens[token_start : token_end + 1])
            )
            if section is not None:
                return section
    return None


def _is_bare_heading_boundary(value: str, start: int) -> bool:
    prefix = value[:start].rstrip()
    return not prefix or prefix[-1] in ".;!?|*\u2022\u25cf\u25aa"


def _has_contextual_bare_heading_boundary(
    *,
    value: str,
    tokens: tuple[HeadingToken, ...],
    token_start: int,
    label: tuple[HeadingToken, ...],
    section: RoleSection,
) -> bool:
    normalized = " ".join(item.text for item in label)
    prefix = value[: label[0].start]
    if normalized == "preferred qualifications" and re.search(
        r"\b(?:basic|minimum|required)\s+(?:qualifications|requirements)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "in office expectations" and re.search(
        r"\bpreferred\s+qualifications\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "skills and experience" and re.search(
        r"\b(?:responsibilities|what\s+you(?:['’]?ll|\s+will)\s+do)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "skills and competencies" and re.search(
        r"\b(?:job\s+requirements|minimum\s+job\s+requirements|qualifications|requirements)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized in _PREFERRED_HEADINGS and re.search(
        r"\b(?:qualifications|requirements|skills\s+and\s+experience|"
        r"what\s+do\s+you\s+bring(?:\s+to\s+the\s+team)?)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "who you are" and re.search(
        r"\b(?:responsibilities|what\s+you(?:['’]?ll|\s+will)\s+do)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "minimum requirements" and re.search(
        r"\b(?:who\s+you\s+are|meet(?:s|ing)?\s+(?:these|the\s+minimum)\s+requirements)\b",
        prefix[-600:],
        re.IGNORECASE,
    ):
        return True
    if normalized in _RESPONSIBILITY_HEADINGS and re.search(
        r"\b(?:skills\s+and\s+competencies|qualifications|requirements)\s*:[^:]*$",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if (
        normalized in _RESPONSIBILITY_HEADINGS
        and _is_complete_flattened_bullet_item(prefix.strip())
    ):
        return True
    if normalized in _RESPONSIBILITY_HEADINGS and re.search(
        r"\b(?:Adhere|Analyze|Analyse|Analyzing|Applying|Assist|Assisting|Be|Build|"
        r"Code|Collaborate|Collect|Conduct|Contribute|Coordinate|Create|Debug|Deliver|"
        r"Design|Designing|Develop|Diagnose|Document|Drive|Employ|Engage|Ensure|Escalate|"
        r"Expand|Follow|Help|Helping|Identify|Implement|Improve|Improving|Install|Investigate|"
        r"Leverage|Liaise|Maintain|Manage|Monitor|Monitoring|Own|Partner|Participate|Perform|"
        r"Prepare|Produce|Provide|Research|Resolve|Respond|Review|Ship|Summarize|Support|"
        r"Supporting|Test|Troubleshoot|Understand|Use|Work|Write)\b[^:;.!?]{0,120}$",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized in _RESPONSIBILITY_HEADINGS and re.search(
        r"\b(?:Benefits|Hybrid\s+work\s+at\s+Stripe|What\s+We\s+Offer)\b"
        r"\s*:?[\s\w$€£/+&-]{0,200}$",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "work environment" and re.search(
        r"\b(?:duties|job\s+responsibilities|responsibilities)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "how to apply" and re.search(
        r"\b(?:qualifications|requirements|what\s+you(?:['’]?ll|\s+will)\s+bring|who\s+you\s+are)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized == "awareness unity empowerment" and re.search(
        r"\b(?:work\s+environment|manufacturing\s+operation|requirements|responsibilities)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized in {
        "about you",
        "basic qualifications",
        "education and experience required",
        "ideal attributes education and experience",
        "job requirements",
        "minimum qualifications",
        "qualifications",
        "required qualifications",
        "required skills and experience",
        "required skills and qualifications",
        "requirements",
        "requirements qualifications",
        "technical background",
        "technical skills",
        "the person",
        "the value you bring",
        "what do you bring to the team",
        "what we are looking for",
        "what were looking for",
        "what you bring",
        "what you bring to this role",
        "what you bring to the role",
        "what youll bring",
        "what makes you stand out",
        "what you need",
        "what youll need",
        "what youll need to have",
        "you will be a good fit if you",
        "who we are looking for",
        "who were looking for",
        "academic background",
        "certification and experience",
        "design skills",
        "other skills",
    } and re.search(
        r"\b(?:responsibilities|responsible\s+for|how\s+(?:will\s+you|you\s+will)\s+"
        r"(?:create\s+an|make\s+an)\s+impact|"
        r"what\s+you\s*(?:['’]?\s*ll|will)\s+(?:be\s+doing|do)|"
        r"in\s+this\s+role\s*,?\s+you\s+can\s+expect\s+to|problems\s+you(?:['’]?ll|\s+will)\s+address)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized in {"additional information", "language requirements"} and re.search(
        r"\b(?:additional\s+information|qualifications|requirements)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if normalized in {
        "additional information",
        "benefits",
        "compensation",
        "compensation and equity",
        "diversity mnp",
        "myrewards mnp",
        "pay transparency",
        "the company",
        "total rewards",
        "what is in it for you",
        "what we offer",
        "what to look forward to",
        "what to expect during the internship",
        "what youll love about us",
        "why work for us",
        "whats in it for you",
    } and re.search(
        r"\b(?:skills\s*(?:and|&)\s*experience|qualifications|requirements|"
        r"what\s+you(?:['’]?ll|\s+will)?\s+(?:bring|need)|who\s+you\s+are)\b",
        prefix,
        re.IGNORECASE,
    ):
        return True
    if (
        section in _FLATTENED_ROLE_TRANSITION_SECTIONS
        and (
            _preceding_colon_heading_section(value, label[0].start)
            == "posting_identity"
            or re.search(
                r"\bdescription\s*:[^:;.!?]*$",
                value[: label[0].start],
                re.IGNORECASE,
            )
            is not None
        )
    ):
        return True
    if section == "terminal" and normalized in _RELATED_LISTING_HEADINGS:
        prefix_tokens = tuple(item.text for item in tokens[:token_start])
        return any(
            len(prefix_tokens) >= len(prefix)
            and prefix_tokens[-len(prefix) :] == prefix
            for prefix in _PORTAL_COLLECTION_PREFIXES
        )
    if normalized in {"apply for this job", "report job"}:
        return bool(
            re.search(
                r"\b(?:qualifications|requirements|responsibilities|what\s+you(?:['’]?ll|\s+will)\s+do)\b",
                prefix,
                re.IGNORECASE,
            )
        )
    if (
        section != "continuation"
        and normalized not in _STANDALONE_SECTION_HEADINGS
    ):
        return False

    first = max(0, token_start - _MAX_HEADING_TOKENS)
    for prior_start in range(first, token_start):
        prior_label = tokens[prior_start:token_start]
        prior_section = _heading_section_for_tokens(
            tuple(item.text for item in prior_label)
        )
        if prior_section not in {
            "responsibilities",
            "qualifications",
            "preferred_qualifications",
            "role_overview",
            "capabilities",
        }:
            continue
        if not _is_bare_heading_boundary(value, prior_label[0].start):
            continue
        interstitial = value[prior_label[-1].end : label[0].start].strip()
        if interstitial in {"", ":"}:
            return True
    return False


def _is_bare_heading_surface(
    value: str,
    label: tuple[HeadingToken, ...],
) -> bool:
    first_character = value[label[0].start : label[0].end].lstrip()[:1]
    suffix = value[label[-1].end :]
    normalized_label = " ".join(item.text for item in label)
    if (
        not first_character.isupper()
        and normalized_label != "work environment"
    ) or suffix.lstrip().startswith(":"):
        return False
    label_tokens = tuple(item.text for item in label)
    if label_tokens == ("requirements",) and re.match(
        r"\s+Management\b",
        suffix,
    ):
        return False
    if label_tokens == ("the", "company"):
        company_surface = value[label[1].start : label[1].end]
        if not company_surface[:1].isupper():
            return False
    if (
        label_tokens[:2] == ("life", "at")
        or " ".join(label_tokens) in _BARE_HEADINGS_REQUIRING_COMPLETE_LINE
    ) and suffix.strip():
        return False
    if not suffix:
        return True
    if suffix[0] in {"*", "\u2026", "\ufffd"}:
        return len(suffix) == 1 or suffix[1].isspace()
    return suffix[0].isspace()


def _is_document_boundary(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) >= 3 and all(character in "=-_*" for character in stripped)


def _heading_section(normalized: str) -> RoleSection | None:
    return _heading_section_for_tokens(tuple(normalized.split()))


def _line_heading_section(value: str) -> RoleSection | None:
    normalized = _normalize_heading(value)
    if normalized == "bonus" and re.fullmatch(
        r"\s*Bonus\s*:?\s*", value, re.IGNORECASE
    ):
        return "preferred_qualifications"
    if normalized.startswith("in order to be eligible ") and normalized.endswith(
        " you must either"
    ):
        return "qualifications"
    if normalized.startswith("as a ") and normalized.endswith(" you will"):
        return "responsibilities"
    if (
        normalized.startswith("full time employees at ")
        and normalized.endswith(" enjoy these perks")
    ):
        return "boundary"
    if _looks_like_working_at_employer_heading(value):
        return "terminal"
    standalone_section = _STANDALONE_SECTION_HEADINGS.get(normalized)
    if standalone_section is not None and _has_standalone_heading_surface(value):
        return standalone_section
    return _heading_section(normalized)


def _looks_like_working_at_employer_heading(value: str) -> bool:
    surface = value.strip().removesuffix(":").strip()
    match = re.fullmatch(r"Working\s+at\s+(.+)", surface)
    if match is None or surface[-1:] in ".;!?":
        return False
    name_tokens = re.findall(r"&|[A-Za-z][A-Za-z'\u2019.-]*", match.group(1))
    lexical_name_tokens = [token for token in name_tokens if token != "&"]
    if not lexical_name_tokens:
        return False
    connector_tokens = {"and", "of", "the"}
    return all(
        token.lower() in connector_tokens
        or token.isupper()
        or token[:1].isupper()
        for token in lexical_name_tokens
    )


def _has_standalone_heading_surface(value: str) -> bool:
    surface = value.strip()
    if surface.endswith(":"):
        surface = surface[:-1].rstrip()
    first_letter = re.search(r"[A-Za-z]", surface)
    return bool(
        surface
        and surface[-1] not in ".;!?"
        and first_letter is not None
        and first_letter.group(0).isupper()
    )


@session_cached
def _heading_section_for_tokens(tokens: tuple[str, ...]) -> RoleSection | None:
    normalized = " ".join(tokens)
    standalone_section = _STANDALONE_SECTION_HEADINGS.get(normalized)
    if standalone_section is not None:
        return standalone_section
    if normalized in _POSTING_IDENTITY_LABELS:
        return "posting_identity"
    if tokens == ("privacy",):
        return "privacy"
    if tokens in _PRIVACY_FOOTER_HEADINGS:
        return "terminal"
    if _matches_privacy_role_subsection(tokens):
        return "continuation"
    if normalized in _RESPONSIBILITY_HEADINGS:
        return "responsibilities"
    if normalized in _QUALIFICATION_HEADINGS:
        return "qualifications"
    if normalized in _PREFERRED_HEADINGS:
        return "preferred_qualifications"
    if normalized in _OVERVIEW_HEADINGS:
        return "role_overview"
    if normalized in _CAPABILITY_HEADINGS:
        return "capabilities"
    if normalized in _COMPANY_HEADINGS:
        return "company"
    if normalized in _BOUNDARY_HEADINGS:
        return "boundary"
    if (
        len(tokens) >= 5
        and tokens[0] == "what"
        and tokens[-3:] == ("has", "to", "offer")
    ):
        return "boundary"
    if normalized in _TERMINAL_HEADINGS:
        return "terminal"
    if _matches_heading_production(
        tokens,
        modifiers=_RESPONSIBILITY_MODIFIERS,
        heads=_RESPONSIBILITY_HEADS,
        tails=_RESPONSIBILITY_TAILS,
    ):
        return "responsibilities"
    modifier = tokens[0] if tokens else ""
    qualification_section = _QUALIFICATION_MODIFIERS.get(modifier)
    qualification_tokens = tokens[1:] if qualification_section is not None else tokens
    if _matches_heading_production(
        qualification_tokens,
        modifiers=((),),
        heads=_QUALIFICATION_HEADS,
        tails=_QUALIFICATION_TAILS,
    ):
        return qualification_section or "qualifications"
    if tokens in _QUALIFICATION_DOMAIN_HEADS:
        return "qualifications"
    if len(tokens) <= 8 and (
        tokens[:1] == ("why",) or tokens[:2] == ("life", "at")
    ):
        return "boundary"
    return None


def _matches_privacy_role_subsection(tokens: tuple[str, ...]) -> bool:
    return any(
        tokens == (*modifier, "privacy", *domain)
        for modifier in _PRIVACY_ROLE_SUBSECTION_MODIFIERS
        for domain in _PRIVACY_ROLE_SUBSECTION_DOMAINS
    )


def _matches_heading_production(
    tokens: tuple[str, ...],
    *,
    modifiers: tuple[tuple[str, ...], ...],
    heads: tuple[tuple[str, ...], ...],
    tails: tuple[tuple[str, ...], ...],
) -> bool:
    return any(tokens == (*modifier, *head, *tail) for modifier in modifiers for head in heads for tail in tails)


def _normalize_heading(value: str) -> str:
    normalized = _repair_unknown_ocr_heading_glyphs(value.casefold().strip()).rstrip(
        ":"
    )
    normalized = normalized.replace("\u2019", "'").replace("\u2018", "'")
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9']+", " ", normalized)
    normalized = normalized.replace("'", "")
    tokens = tuple(normalized.split())
    collapsed_tokens = _collapse_overlapping_ocr_heading_tokens(tokens)
    if " ".join(collapsed_tokens) in _OVERLAPPING_OCR_HEADING_ALLOWLIST:
        return " ".join(collapsed_tokens)
    return " ".join(tokens)


def _repair_unknown_ocr_heading_glyphs(value: str) -> str:
    def repair(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        candidates = [
            word
            for word in _OCR_REPAIRABLE_HEADING_WORDS
            if word.startswith(prefix)
            and word.endswith(suffix)
            and 0 < len(word) - len(prefix) - len(suffix) <= 3
        ]
        return candidates[0] if len(candidates) == 1 else match.group(0)

    return _OCR_UNKNOWN_HEADING_GLYPH.sub(repair, value)


def _collapse_overlapping_ocr_heading_tokens(
    tokens: tuple[str, ...],
) -> tuple[str, ...]:
    collapsed: list[str] = []
    token_index = 0
    while token_index < len(tokens):
        first = tokens[token_index]
        if len(first) != 1:
            collapsed.append(first)
            token_index += 1
            continue

        word = first
        next_index = token_index + 1
        overlap_count = 0
        while next_index < len(tokens):
            token = tokens[next_index]
            if len(token) == 2 and token[0] == word[-1]:
                word += token[1]
                overlap_count += 1
                next_index += 1
                continue
            if len(token) == 1 and token == word[-1] and overlap_count:
                next_index += 1
            break

        if overlap_count and next_index - token_index >= 3:
            collapsed.append(word)
            token_index = next_index
            continue
        collapsed.append(first)
        token_index += 1
    return tuple(collapsed)


def _is_unsectioned_metadata(value: str) -> bool:
    label, separator, _field_value = value.partition(":")
    normalized_label = _normalize_heading(label)
    return bool(
        separator
        and (
            normalized_label in _UNSECTIONED_METADATA_LABELS
            or _is_structured_field_label(normalized_label)
        )
    )


def _is_non_role_posting_content(value: str) -> bool:
    return any(
        (
            _is_portal_action(value),
            _is_compensation_metadata(value),
            _is_posting_administration(value),
        )
    )


def _is_portal_action(value: str) -> bool:
    tokens = tuple(_normalize_heading(value).split())
    if tokens in {("apply",), ("opportunities",)}:
        return True
    if tokens[:1] == ("apply",) and any(
        token in {"opportunities", "settings"} for token in tokens[1:]
    ):
        return True
    if tokens[:1] == ("settings",) and "@" in value:
        return True
    if any(
        tokens[: len(prefix)] == prefix for prefix in _PORTAL_ACTION_PREFIXES
    ):
        return True
    if _starts_with(tokens, ("please", "apply")):
        return _contains_any_phrase(
            tokens,
            (*_APPLICATION_DOCUMENT_PHRASES, *_APPLICATION_CHANNEL_PHRASES),
        )
    if _starts_with(tokens, ("please", "include")):
        return _contains_any_phrase(tokens, _APPLICATION_DOCUMENT_PHRASES)
    if tokens[:1] in {("upload",), ("uploading",)}:
        return _contains_any_phrase(tokens, _APPLICATION_DOCUMENT_PHRASES)
    return False


def _is_compensation_metadata(value: str) -> bool:
    tokens = tuple(_normalize_heading(value).split())
    if _matching_prefix_width(tokens, _COMPENSATION_METADATA_PREFIXES):
        return True
    if not _contains_any_phrase(tokens, _COMPENSATION_RANGE_PHRASES):
        return False
    return bool(
        _contains_any_phrase(
            tokens,
            (
                ("candidates", "can", "expect"),
                ("for", "this", "role"),
                ("for", "this", "position"),
                ("listed", "below"),
                ("listed", "range"),
                ("recruiter", "will", "advise"),
            ),
        )
        or re.search(r"(?:[$€£]|\b(?:cad|usd)\b)\s*\d", value, re.IGNORECASE)
    )


def _is_posting_administration(value: str) -> bool:
    stripped = value.strip()
    if stripped and not any(character.isalnum() for character in stripped) and any(
        "\ue000" <= character <= "\uf8ff" for character in stripped
    ):
        return True
    tokens = tuple(_normalize_heading(value).split())
    if not tokens:
        return False
    if (
        _starts_with(tokens, ("direct", "navigation", "encounters"))
        and _contains_any_phrase(tokens, (("caller", "review", "is", "required"),))
    ):
        return True
    if tokens in {("additionally",), ("and",), ("or",)}:
        return True
    if tokens in {
        ("build", "bold"),
        ("deliver", "impact"),
        ("win", "together"),
    }:
        return True
    if (
        _contains_any_phrase(tokens, (("use", "ai", "to", "assess", "how", "you", "fit"),))
        and _contains_any_phrase(tokens, (("build", "bold"),))
    ):
        return True
    if (
        _starts_with(tokens, ("please", "don", "t", "hesitate", "to", "apply"))
        or _starts_with(tokens, ("please", "do", "not", "hesitate", "to", "apply"))
    ):
        return True
    if tokens in {("varies", "by", "placement"), ("varies", "by", "placements")}:
        return True
    if (
        _starts_with(tokens, ("please", "ensure"))
        and _contains_any_phrase(tokens, (("eligibility", "requirements"),))
        and _contains_any_phrase(tokens, (("before", "applying"),))
    ):
        return True

    structured_posting_metadata = bool(
        _starts_with(tokens, ("work", "location"))
        or _starts_with(tokens, ("worker", "type"))
        or _starts_with(tokens, ("rate", "of", "pay"))
        or (
            _starts_with(tokens, ("pay",))
            and re.search(r"(?:[$€£]|\b(?:cad|usd)\b)\s*\d", value, re.IGNORECASE)
        )
        or (
            _starts_with(tokens, ("start", "date"))
            and _contains_any_phrase(tokens, (("end", "date"),))
        )
    )
    candidate_privacy_notice = bool(
        _contains_any_phrase(
            tokens,
            (
                ("candidate", "privacy", "statement"),
                ("privacy", "of", "your", "personal", "information"),
                ("protect", "your", "personal", "information"),
            ),
        )
    )
    website_cookie_notice = bool(
        _contains_any_phrase(
            tokens,
            (
                ("this", "website", "uses", "cookies"),
                ("website", "activity"),
                ("analytics", "technologies"),
            ),
        )
        and _contains_any_phrase(tokens, (("cookies",), ("website",)))
    )
    applicant_accommodation_notice = bool(
        _contains_any_phrase(
            tokens,
            (("accommodation",), ("accommodations",), ("adjustment",), ("adjustments",)),
        )
    ) or bool(
        _contains_any_phrase(
            tokens,
            (("accommodation",), ("accommodations",), ("adjustment",), ("adjustments",)),
        )
        and (
            (
                tokens[:2] in {("if", "you"), ("should", "you")}
                and _contains_any_phrase(
                    tokens,
                    (("contact",), ("reach", "out"), ("request",)),
                )
            )
            or _contains_any_phrase(
                tokens,
                (
                    ("job", "opportunity"),
                    ("testing",),
                    ("accessibility", "needs"),
                    ("talent", "team"),
                ),
            )
            or _starts_with(tokens, ("all", "accommodation", "information"))
            or _starts_with(tokens, ("accommodations", "to", "help", "you"))
        )
        and _contains_any_phrase(
            tokens,
            (
                ("applicant",),
                ("applicants",),
                ("application", "process"),
                ("candidate", "experience"),
                ("candidates",),
                ("hiring", "process"),
                ("interview",),
                ("interviews",),
                ("recruitment", "process"),
                ("selection", "process"),
            ),
        )
    )
    ai_hiring_process_notice = bool(
        _contains_any_phrase(tokens, (("ai",), ("artificial", "intelligence")))
        and _contains_any_phrase(
            tokens,
            (
                ("applicant", "data"),
                ("application", "review"),
                ("hiring", "decision"),
                ("hiring", "decisions"),
                ("hiring", "manager"),
                ("recruiters",),
                ("recruitment", "process"),
            ),
        )
    ) or bool(
        _starts_with(tokens, ("final", "decisions"))
        and _contains_any_phrase(tokens, (("hiring", "manager"), ("hiring", "team")))
    ) or bool(
        _starts_with(tokens, ("every", "hiring", "decision"))
        and _contains_any_phrase(
            tokens,
            (("hiring", "managers"), ("recruitment", "professionals")),
        )
    )
    employer_culture_promise = bool(
        _starts_with(tokens, ("we", "believe", "that", "your", "workplace"))
        and _contains_any_phrase(tokens, (("should", "empower", "you"),))
    )
    website_promotion = bool(
        _starts_with(tokens, ("visit", "our", "website"))
        or _starts_with(tokens, ("its", "website", "is"))
    )
    compensation_or_benefit_disclosure = bool(
        _starts_with(tokens, ("actual", "compensation"))
        or _starts_with(tokens, ("the", "salary", "for", "this", "position"))
        or _starts_with(tokens, ("starting", "compensation"))
        or _starts_with(tokens, ("any", "compensation", "range"))
        or _starts_with(tokens, ("extended", "health", "and", "dental", "benefits"))
        or (
            _starts_with(tokens, ("along", "with", "a", "competitive", "salary"))
            and _contains_any_phrase(tokens, (("total", "compensation", "package"),))
        )
        or (
            _starts_with(tokens, ("you", "will", "benefit", "from"))
            and _contains_any_phrase(tokens, (("compensation", "package"), ("benefits",)))
        )
        or (
            _contains_any_phrase(tokens, (("competitive", "salary", "benefits"),))
            and _contains_any_phrase(tokens, (("we", "are", "excited", "to", "be", "working"),))
        )
        or (
            _starts_with(tokens, ("if", "you", "have", "questions"))
            and _contains_any_phrase(tokens, (("recruiter",), ("compensation", "philosophy")))
        )
        or (
            _contains_any_phrase(tokens, (("employment", "type"),))
            and _contains_any_phrase(tokens, (("compensation",),))
            and _contains_any_phrase(tokens, (("company", "description"),))
        )
    )
    portal_scrape_description = bool(
        _starts_with(tokens, ("the", "pages", "apply", "now", "control"))
        or tokens in {("share", "this", "job"), ("apply", "now", "share")}
        or _starts_with(tokens, ("toggle", "navigation"))
        or (
            _contains_any_phrase(tokens, (("apply", "now"), ("job", "details")))
            and _contains_any_phrase(tokens, (("qualifications",),))
        )
    )
    recruitment_marketing = bool(
        (
            _contains_any_phrase(tokens, (("cookie", "cutter"),))
            and _contains_any_phrase(tokens, (("hiring", "process"),))
        )
        or _starts_with(tokens, ("we", "thank", "all", "applicants"))
        or _starts_with(tokens, ("we", "welcome", "all", "qualified", "candidates"))
        or _starts_with(tokens, ("we", "appreciate", "the", "interest"))
        or (
            _contains_any_phrase(tokens, (("believe", "you", "have", "relevant"),))
            and _contains_any_phrase(tokens, (("please", "apply"),))
        )
        or tokens in {("ready", "to", "dive", "deeper"), ("ready", "to", "dive", "deeper", "?")}
        or (
            _starts_with(tokens, ("however", "only", "those", "chosen"))
            and _contains_any_phrase(tokens, (("interview",),))
        )
    )
    employer_brand_statement = bool(
        re.match(r"^At\s+[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*)*\s*,", stripped)
        and not _contains_any_phrase(
            tokens,
            (
                ("applicants", "must"),
                ("candidates", "must"),
                ("you", "must"),
                ("you", "will"),
            ),
        )
    )
    diversity_recruitment_notice = bool(
        _contains_any_phrase(
            tokens,
            (
                ("committed", "to", "employment", "equity"),
                ("committed", "to", "fostering", "equal", "opportunities"),
            ),
        )
        or (
            _contains_any_phrase(tokens, (("committed", "to"),))
            and _contains_any_phrase(tokens, (("diversity",), ("equal", "opportunity")))
        )
        or _contains_any_phrase(
            tokens,
            (("invites", "applications", "from", "all", "qualified", "individuals"),),
        )
    )
    if any(
        (
            structured_posting_metadata,
            candidate_privacy_notice,
            website_cookie_notice,
            applicant_accommodation_notice,
            ai_hiring_process_notice,
            employer_culture_promise,
            website_promotion,
            compensation_or_benefit_disclosure,
            portal_scrape_description,
            recruitment_marketing,
            employer_brand_statement,
            diversity_recruitment_notice,
        )
    ):
        return True

    printed_page_marker = _is_printed_page_marker(value)
    portal_navigation = _contains_any_phrase(tokens, (("login", "register"),))
    cookie_banner = bool(
        _contains_any_phrase(
            tokens,
            (
                ("cookies", "are", "used", "on", "this", "site"),
                ("cookie", "consent", "manager"),
            ),
        )
        or (
            tokens[0] in {"cookie", "cookies"}
            and _contains_any_phrase(tokens, (("candidate", "experience"),))
        )
    )
    cookie_control_label = tokens in {("okay",), ("x",)}
    browser_print_header = bool(
        _BROWSER_PRINT_TIMESTAMP_HEADER.fullmatch(stripped)
        or _BROWSER_PRINT_URL_PAGE_MARKER.fullmatch(stripped)
        or (
            re.search(r"https?://", value, re.IGNORECASE)
            and (
                "|" in value
                or tokens[0] in {"careers", "opportunities"}
                or _contains_any_phrase(
                    tokens,
                    (
                        ("candidate", "home"),
                        ("search", "for", "jobs"),
                    ),
                )
            )
        )
    )
    application_deadline = bool(
        tokens[:2] == ("application", "deadline") and ":" in stripped
    )
    privacy_rights_notice = bool(
        _starts_with(tokens, ("learn", "more", "about", "your", "rights"))
        and _contains_any_phrase(tokens, (("privacy", "policy"),))
    )

    cookie_control = bool(
        _contains_any_phrase(tokens, (("cookie", "settings"), ("cookie", "preferences")))
        and (
            _contains_any_phrase(tokens, (("web", "browser"), ("at", "any", "time")))
            or _starts_with(tokens, ("you", "can", "manage", "your"))
            or _contains_any_phrase(
                tokens,
                (("click", "on"), ("clicking", "on"), ("opt", "out")),
            )
            or _contains_any_phrase(
                tokens,
                (("cookie", "policy"), ("cookies", "policy"), ("tax", "strategy")),
            )
        )
    ) or _starts_with(tokens, ("manage", "cookies"))
    if cookie_control:
        return True

    application_status_notice = bool(
        _starts_with(tokens, ("please", "note"))
        and _contains_any_phrase(tokens, (("hear", "back"),))
    ) or bool(
        _contains_any_phrase(
            tokens,
            (("application", "status"), ("application", "statuses")),
        )
        and _contains_any_phrase(
            tokens,
            (("hear", "back"), ("inquire", "about"), ("used", "to", "inquire")),
        )
    )
    accessibility_contact_notice = bool(
        _contains_any_phrase(
            tokens,
            (("accessibility", "request"), ("accessibility", "requests")),
        )
        and _contains_any_phrase(
            tokens,
            (("contact", "information"), ("application", "status"), ("application", "statuses")),
        )
    ) or bool(
        _contains_any_phrase(
            tokens,
            (
                ("if", "you", "require", "accommodation"),
                ("if", "you", "require", "an", "accommodation"),
                ("if", "you", "are", "vision", "impaired"),
            ),
        )
        and _contains_any_phrase(
            tokens,
            (("contact",), ("talent", "acquisition"), ("recruiting",)),
        )
    )
    profile_redirect = bool(
        _starts_with(tokens, ("we", "ask", "instead"))
        and _contains_any_phrase(tokens, (("create", "a", "profile"), ("create", "your", "profile")))
    )
    equal_opportunity_notice = bool(
        _contains_any_phrase(
            tokens,
            (
                ("all", "qualified", "applicants"),
                ("all", "qualified", "candidates"),
            ),
        )
        and _contains_any_phrase(
            tokens,
            (("receive", "consideration"), ("consideration", "for", "employment")),
        )
    ) or _contains_any_phrase(
        tokens,
        (
            ("equal", "opportunity", "employer"),
            ("believes", "in", "equal", "employment", "opportunity"),
        ),
    ) or bool(
        _starts_with(tokens, ("it", "is", "the", "policy", "of"))
        and _contains_any_phrase(
            tokens,
            (
                ("no", "discrimination"),
                ("there", "will", "be", "no", "discrimination"),
            ),
        )
    )
    data_processing_notice = bool(
        _starts_with(tokens, ("if", "you", "would", "like", "more"))
        and _contains_any_phrase(
            tokens,
            (("data", "is", "processed"), ("how", "your", "data"), ("contact", "us")),
        )
    )
    ai_hiring_disclosure = bool(
        _starts_with(tokens, ("these", "tools", "support"))
        and _contains_any_phrase(tokens, (("do", "not", "replace"),))
        and _contains_any_phrase(tokens, (("human", "judgment"),))
    ) or bool(
        _contains_any_phrase(tokens, (("ai", "based", "tools"),))
        and _contains_any_phrase(
            tokens,
            (("review", "of", "applications"), ("hiring", "decisions")),
        )
    )
    compensation_scope_disclosure = bool(
        _starts_with(tokens, ("please", "note"))
        and _contains_any_phrase(
            tokens,
            (
                ("compensation", "details", "listed"),
                ("base", "salary", "only"),
            ),
        )
        and _contains_any_phrase(tokens, (("role", "postings", "reflect"),))
    )
    return any(
        (
            printed_page_marker,
            portal_navigation,
            cookie_banner,
            cookie_control_label,
            browser_print_header,
            application_deadline,
            privacy_rights_notice,
            application_status_notice,
            accessibility_contact_notice,
            profile_redirect,
            equal_opportunity_notice,
            data_processing_notice,
            ai_hiring_disclosure,
            compensation_scope_disclosure,
        )
    )


def _is_printed_page_marker(value: str) -> bool:
    return _PRINTED_PAGE_MARKER.fullmatch(value.strip()) is not None


def _privacy_heading_precedes_role_page_continuation(
    source_lines: list[str],
    start: int,
) -> bool:
    marker_index: int | None = None
    for line_index in range(start, min(len(source_lines), start + 32)):
        if _is_printed_page_marker(source_lines[line_index]):
            marker_index = line_index
            break
    if marker_index is None:
        return False

    for source_line in source_lines[marker_index + 1 : marker_index + 9]:
        stripped = source_line.strip()
        if not stripped:
            continue
        if _LIST_MARKER.match(stripped) is not None:
            return True
        return _line_heading_section(stripped) in {
            "responsibilities",
            "qualifications",
            "preferred_qualifications",
            "role_overview",
            "capabilities",
        }
    return False


def _is_interleaved_portal_panel_start(value: str) -> bool:
    repaired = re.sub(
        r"\bnoti[\ue000-\uf8ff\ufffd]ed\b",
        "notified",
        value.casefold(),
    )
    tokens = tuple(_normalize_heading(repaired).split())
    return tokens[:2] == ("get", "notified") and len(tokens) <= 5


def _portal_panel_precedes_printed_page_marker(
    source_lines: list[str],
    start: int,
) -> bool:
    related_listings_seen = False
    for source_line in source_lines[start : start + 32]:
        stripped = source_line.strip()
        if _is_printed_page_marker(stripped):
            return related_listings_seen
        if _normalize_heading(stripped) in _RELATED_LISTING_HEADINGS:
            related_listings_seen = True
    return False


def _role_page_continuation_section(value: str) -> RoleSection | None:
    fragment = _content_fragment(value, 0)
    if fragment is None:
        return None
    signals = _clause_signals(fragment.text)
    if not signals.tokens:
        return None
    if signals.has_modality or signals.tokens[0] in _QUALIFICATION_COMPLEMENT_HEADS:
        return "qualifications"
    if _has_responsibility_predicate(signals.tokens):
        return "responsibilities"
    return None


def _next_material_text(source_lines: list[str], start: int) -> str | None:
    for source_line in source_lines[start:]:
        fragment = _content_fragment(source_line.rstrip("\r\n"), 0)
        if fragment is not None:
            return fragment.text
    return None


def _privacy_heading_is_terminal(
    *,
    section: RoleSection,
    fragment: TextFragment | None,
    next_material: str | None,
) -> bool:
    if section not in {
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
    }:
        return True

    candidate = fragment.text if fragment is not None else next_material
    if candidate is None:
        return True

    next_section = _line_heading_section(candidate)
    if next_section in {
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
        "capabilities",
    }:
        return False
    if fragment is None:
        return True
    candidate_tokens = tuple(_normalize_heading(candidate).split())
    return any(
        candidate_tokens[: len(prefix)] == prefix
        for prefix in _FOOTER_NAVIGATION_PREFIXES
    )


def _is_unsectioned_candidate(content: str, signals: ClauseSignals) -> bool:
    if signals.owner == "company":
        return False
    if signals.owner == "applicant" or signals.has_modality:
        return True
    if signals.tokens and signals.tokens[0] in _UNSECTIONED_RESPONSIBILITY_HEADS:
        return True
    if any(token in _QUALIFICATION_COMPLEMENT_HEADS for token in signals.tokens):
        return True
    return _is_nominal_capability_list(content, signals.tokens)


def _is_high_confidence_unsectioned_evidence(
    content: str,
    signals: ClauseSignals,
) -> bool:
    if _unsectioned_clause_has_non_candidate_subject(content, signals):
        return False
    promise_coordination = _unsectioned_employer_promise_coordination(content, 0)
    if promise_coordination is not None:
        return bool(promise_coordination)
    if _is_unsectioned_employer_promise(signals):
        return False
    if signals.owner == "role":
        return True
    if signals.owner == "applicant" and (
        _has_responsibility_predicate(signals.tokens)
        or signals.applicant_predicate in {"hard_requirement", "qualification"}
    ):
        return True
    if signals.has_modality and _unsectioned_modal_has_candidate_scope(
        content,
        signals,
    ):
        return True
    if signals.tokens and signals.tokens[0] in _UNSECTIONED_RESPONSIBILITY_HEADS:
        return True
    if signals.applicant_predicate in {"hard_requirement", "qualification"}:
        return True
    has_qualification_head = any(
        token in _QUALIFICATION_COMPLEMENT_HEADS for token in signals.tokens
    )
    has_grounding_shape = bool(
        re.search(
            r"\b(?:bachelor|degree|diploma|certification|credential|"
            r"\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
            content,
            re.IGNORECASE,
        )
    )
    return has_qualification_head and has_grounding_shape


def _is_role_environment_statement(
    content: str,
    tokens: tuple[str, ...],
) -> bool:
    subject_width = _matching_prefix_width(tokens, _ROLE_ENVIRONMENT_SUBJECT_PREFIXES)
    if not subject_width:
        return False
    if not _contains_any_phrase(
        tokens[subject_width:],
        _ROLE_ENVIRONMENT_PREDICATE_PHRASES,
    ):
        return False
    return any(
        definition.entity_type in {"framework", "language", "platform", "tool"}
        and any(
            re.search(pattern, content, re.IGNORECASE) is not None
            for pattern in definition.patterns
        )
        for definition in load_capability_ontology()
    )


def _is_unsectioned_employer_promise(signals: ClauseSignals) -> bool:
    if signals.owner not in {"applicant", "role"}:
        return False
    owner, owner_end = _clause_owner(signals.tokens)
    if owner not in {"applicant", "role"}:
        return False
    predicate = signals.tokens[owner_end:]
    auxiliary_width = _matching_prefix_width(
        predicate,
        (
            ("can",),
            ("could",),
            ("may",),
            ("might",),
            ("shall",),
            ("should",),
            ("will",),
            ("would",),
        ),
    )
    predicate = predicate[auxiliary_width:]
    if predicate[:1] in {("expect",), ("expects",)} and _contains_any_phrase(
        predicate[1:],
        (("experience",), ("opportunity",), ("role",)),
    ):
        return True
    transparent_width = _matching_prefix_width(
        predicate,
        _UNSECTIONED_EMPLOYER_PROMISE_TRANSPARENT_PREFIXES,
    )
    predicate = predicate[transparent_width:]
    if owner == "role":
        return bool(
            predicate
            and predicate[0] in {"offer", "offers", "provide", "provides"}
            and _contains_any_phrase(
                predicate[1:],
                _UNSECTIONED_BENEFIT_COMPLEMENTS,
            )
        )
    promise_width = _matching_prefix_width(
        predicate,
        _UNSECTIONED_EMPLOYER_PROMISE_PREDICATES,
    )
    if promise_width:
        return True
    if signals.applicant_predicate != "descriptive":
        return False
    return bool(
        predicate
        and predicate[0] in _UNSECTIONED_BENEFIT_PROMISE_PREDICATES
        and _contains_any_phrase(
            predicate[1:],
            _UNSECTIONED_BENEFIT_COMPLEMENTS,
        )
    )


def _unsectioned_employer_promise_coordination(
    content: str,
    absolute_start: int,
) -> list[tuple[TextFragment, str]] | None:
    subject_heads = {
        *_APPLICANT_NOUN_HEADS,
        "incumbent",
        "incumbents",
        "individual",
        "individuals",
    }
    determiner_pattern = "|".join(
        re.escape(value) for value in sorted({*_DETERMINERS, "our"})
    )
    modifier_pattern = "|".join(
        re.escape(value) for value in sorted(_APPLICANT_PRENOMINAL_MODIFIERS)
    )
    subject_head_pattern = "|".join(
        re.escape(value) for value in sorted(subject_heads)
    )
    subject = re.match(
        rf"\s*(?P<subject>you|(?:(?:{determiner_pattern})\s+)?"
        rf"(?:(?:{modifier_pattern})\s+)*(?:{subject_head_pattern}))\b"
        r"(?:\s+(?P<auxiliary>am|are|can|could|is|may|might|shall|should|"
        r"was|were|will|would))?\b",
        content,
        re.IGNORECASE,
    )
    if subject is None:
        return None

    subject_text = subject.group("subject")
    inherited_auxiliary = subject.group("auxiliary")
    modal_auxiliaries = {
        "can",
        "could",
        "may",
        "might",
        "shall",
        "should",
        "will",
        "would",
    }
    full_subject_prefix = subject_text
    if inherited_auxiliary:
        full_subject_prefix = f"{subject_text} {inherited_auxiliary}"
    inherited_prefix = (
        full_subject_prefix
        if inherited_auxiliary
        and inherited_auxiliary.casefold() in modal_auxiliaries
        else subject_text
    )

    predicate_start = subject.end()
    while predicate_start < len(content) and content[predicate_start].isspace():
        predicate_start += 1
    if predicate_start >= len(content):
        return None

    auxiliary_pattern = (
        r"am|are|can|could|is|may|might|shall|should|was|were|will|would"
    )
    separator_pattern = re.compile(
        r",\s*(?:(?:and|but)\s+)?|\s+(?:and|but)\s+",
        re.IGNORECASE,
    )
    delimiter_depths = _expression_delimiter_depths(content)
    raw_separators = [
        match
        for match in separator_pattern.finditer(content, predicate_start)
        if delimiter_depths[match.start()] == delimiter_depths[predicate_start]
    ]
    accepted_separators: list[re.Match[str]] = []
    coordinate_start = predicate_start
    for separator in raw_separators:
        coordinate_end = next(
            (
                following.start()
                for following in raw_separators
                if following.start() > separator.start()
            ),
            len(content),
        )
        left = _expression_text_span(content, coordinate_start, separator.start())
        right = _expression_text_span(content, separator.end(), coordinate_end)
        if left is None or right is None:
            continue
        right_text, right_start, _right_end = right
        explicit_auxiliary = re.match(
            rf"(?P<auxiliary>{auxiliary_pattern})\s+",
            right_text,
            re.IGNORECASE,
        )
        predicate_text = (
            right_text[explicit_auxiliary.end() :]
            if explicit_auxiliary is not None
            else right_text
        )
        if _coordinate_is_inside_relative_subject_scope(
            content,
            predicate_start=predicate_start,
            separator_start=separator.start(),
            right_text=right_text,
        ):
            continue
        if (
            explicit_auxiliary is None
            and not _is_coordinated_predicate_complement(
                predicate_text,
                antecedent_text=left[0],
                antecedent_inherits_modality=inherited_auxiliary is not None,
            )
            and not _is_open_class_promise_coordinate_predicate(predicate_text)
        ):
            continue
        accepted_separators.append(separator)
        coordinate_start = right_start

    coordinate_spans = _expression_members_from_separators(
        content,
        start=predicate_start,
        end=len(content),
        separators=[match.span() for match in accepted_separators],
    )
    if not coordinate_spans:
        single = _expression_text_span(content, predicate_start, len(content))
        coordinate_spans = (single,) if single is not None else ()

    has_employer_promise = False
    duties: list[tuple[TextFragment, str]] = []
    for index, (coordinate_text, coordinate_start, coordinate_end) in enumerate(
        coordinate_spans
    ):
        explicit_auxiliary = re.match(
            rf"(?P<auxiliary>{auxiliary_pattern})\s+",
            coordinate_text,
            re.IGNORECASE,
        )
        coordinate_prefix = full_subject_prefix if index == 0 else inherited_prefix
        if explicit_auxiliary is not None:
            auxiliary = explicit_auxiliary.group("auxiliary")
            if auxiliary.casefold() in modal_auxiliaries:
                coordinate_start += explicit_auxiliary.end()
                coordinate_text = coordinate_text[explicit_auxiliary.end() :]
                coordinate_prefix = f"{subject_text} {auxiliary}"
            else:
                coordinate_prefix = subject_text

        candidate_prefixes = [coordinate_prefix]
        if (
            index > 0
            and explicit_auxiliary is None
            and full_subject_prefix != coordinate_prefix
        ):
            candidate_prefixes.insert(0, full_subject_prefix)
        candidate_signals = [
            (prefix, _clause_signals(f"{prefix} {coordinate_text}"))
            for prefix in candidate_prefixes
        ]
        if any(
            _is_unsectioned_employer_promise(signals)
            for _prefix, signals in candidate_signals
        ):
            has_employer_promise = True
            continue
        coordinate_tokens = lexical_tokens(coordinate_text)
        duty_prefix = next(
            (
                prefix
                for prefix, signals in candidate_signals
                if _has_responsibility_predicate(signals.tokens)
            ),
            coordinate_prefix,
        )
        is_duty = any(
            _has_responsibility_predicate(signals.tokens)
            for _prefix, signals in candidate_signals
        ) or bool(
            coordinate_tokens
            and (
                coordinate_tokens[0] in _UNSECTIONED_BENEFIT_PROMISE_PREDICATES
                or _is_open_class_promise_coordinate_predicate(coordinate_text)
            )
        )
        if is_duty:
            duties.append(
                (
                    TextFragment(
                        text=coordinate_text,
                        start=absolute_start + coordinate_start,
                        end=absolute_start + coordinate_end,
                    ),
                    duty_prefix,
                )
            )
    return duties if has_employer_promise else None


def _is_open_class_promise_coordinate_predicate(value: str) -> bool:
    if re.match(
        r"^\s*(?:[A-Za-z][\w-]*[\u2019']s|"
        r"(?:[A-Z][\w-]*\s+)+[A-Z][\w-]*[\u2019']s)\b",
        value,
    ):
        return False
    tokens = lexical_tokens(value)
    if not tokens:
        return False
    head = tokens[0]
    return bool(
        len(tokens) >= 2
        and head
        not in {
            *_DETERMINERS,
            "although",
            "because",
            "if",
            "our",
            "that",
            "though",
            "unless",
            "when",
            "where",
            "whereas",
            "which",
            "who",
            "whom",
            "whose",
            "your",
        }
        and not head.endswith(
            (
                "al",
                "ary",
                "ed",
                "ful",
                "ible",
                "ic",
                "ical",
                "ing",
                "ive",
                "less",
                "ous",
            )
        )
        and not _contains_any_phrase(tokens, _UNSECTIONED_BENEFIT_COMPLEMENTS)
    )


def _coordinate_is_inside_relative_subject_scope(
    content: str,
    *,
    predicate_start: int,
    separator_start: int,
    right_text: str,
) -> bool:
    if _owner_signal_has_finite_predicate(_clause_signals(right_text)):
        return False
    prefix = content[predicate_start:separator_start]
    relative_subjects = tuple(
        re.finditer(r"\b(?:that|which|who|whom|whose)\b", prefix, re.IGNORECASE)
    )
    if not relative_subjects:
        return False
    relative_tail = prefix[relative_subjects[-1].end() :]
    return not re.search(r"[,;:]", relative_tail)


def _unsectioned_clause_has_non_candidate_subject(
    content: str,
    signals: ClauseSignals,
) -> bool:
    if signals.owner == "company":
        return True
    if signals.owner in {"applicant", "role"}:
        return False
    if _is_unsectioned_system_qualification_nominal(signals.tokens):
        return False
    subject_tokens, has_finite_predicate = _unsectioned_subject_tokens(
        content,
        signals,
    )
    if not subject_tokens:
        return False
    if any(token in _QUALIFICATION_COMPLEMENT_HEADS for token in subject_tokens):
        return False
    if any(
        token in _UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS
        for token in subject_tokens
    ):
        return True
    # A finite, non-modal assertion with no applicant or role subject describes
    # the named subject, not a qualification imposed on the candidate.
    return has_finite_predicate and not signals.modality_cues


def _is_unsectioned_system_qualification_nominal(
    tokens: tuple[str, ...],
) -> bool:
    system_head = next(
        (
            index
            for index, token in enumerate(tokens[:9])
            if token in _UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS
        ),
        None,
    )
    if system_head is None:
        return False
    qualification_head = next(
        (
            index
            for index in range(system_head + 1, min(len(tokens), system_head + 6))
            if tokens[index] in _QUALIFICATION_COMPLEMENT_HEADS
        ),
        None,
    )
    if qualification_head is None:
        return False
    modifiers = tokens[system_head + 1 : qualification_head]
    return all(
        modifier in _UNSECTIONED_QUALIFICATION_NOMINAL_MODIFIERS
        or modifier in _UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS
        for modifier in modifiers
    )


def _unsectioned_subject_tokens(
    content: str,
    signals: ClauseSignals,
) -> tuple[tuple[str, ...], bool]:
    has_finite_predicate = False
    if signals.modality_cues:
        subject_end = signals.modality_cues[0].cue_start
    else:
        predicate_match = _UNSECTIONED_FINITE_PREDICATE_PATTERN.search(content)
        if predicate_match is None:
            return _unsectioned_initial_subject_tokens(signals.tokens), False
        subject_end = predicate_match.start()
        has_finite_predicate = True
    subject_text = content[:subject_end].strip()
    subject_text = re.sub(
        r"\b(?:am|are|is|was|were|be|been|being)\s*$",
        "",
        subject_text,
        flags=re.IGNORECASE,
    ).strip()
    return lexical_tokens(subject_text), has_finite_predicate


def _unsectioned_initial_subject_tokens(
    tokens: tuple[str, ...],
) -> tuple[str, ...]:
    subject_head = next(
        (
            index
            for index, token in enumerate(tokens[:9])
            if token in _UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS
        ),
        None,
    )
    if subject_head is None:
        return ()
    prefix = tokens[:subject_head]
    if any(
        token in _UNSECTIONED_SUBJECT_PREFIX_BLOCKERS
        or token in _QUALIFICATION_COMPLEMENT_HEADS
        or token in _UNSECTIONED_RESPONSIBILITY_HEADS
        or _UNSECTIONED_FINITE_PREDICATE_PATTERN.fullmatch(token) is not None
        for token in prefix
    ):
        return ()
    subject_end = subject_head + 1
    if (
        subject_end < len(tokens)
        and tokens[subject_end] in _QUALIFICATION_COMPLEMENT_HEADS
    ):
        subject_end += 1
    return tokens[:subject_end]


def _unsectioned_modal_has_candidate_scope(
    content: str,
    signals: ClauseSignals,
) -> bool:
    if not signals.modality_cues:
        return False
    first_cue = signals.modality_cues[0]
    subject_tokens, _ = _unsectioned_subject_tokens(content, signals)
    if not subject_tokens:
        return True
    if any(token in _QUALIFICATION_COMPLEMENT_HEADS for token in subject_tokens):
        return True
    cue_text = content[first_cue.cue_start : first_cue.cue_end]
    return bool(
        re.search(
            r"\b(?:asset|mandatory|preferred|required)\b",
            cue_text,
            re.IGNORECASE,
        )
    )


def _is_nominal_capability_list(content: str, tokens: tuple[str, ...]) -> bool:
    if not 2 <= len(tokens) <= 24:
        return False
    terms = [
        match.group(0)
        for match in _HEADING_TOKEN.finditer(content)
        if match.group(0).casefold() not in _NOMINAL_COORDINATORS
    ]
    if len(terms) < 2:
        return False
    if any(separator in content for separator in ",/|"):
        return True
    return all(term[0].isupper() or term.isupper() for term in terms)


def _content_fragment(value: str, absolute_start: int) -> TextFragment | None:
    leading = len(value) - len(value.lstrip())
    content = value[leading:]
    start = absolute_start + leading
    marker = _LIST_MARKER.match(content)
    if marker is not None:
        content = content[marker.end() :]
        start += marker.end()
    extra_leading = len(content) - len(content.lstrip())
    content = content[extra_leading:].rstrip()
    start += extra_leading
    if content.endswith(" *"):
        content = content[:-2].rstrip()
    if not content:
        return None
    return TextFragment(text=content, start=start, end=start + len(content))


def _clause_fragments(
    value: str,
    absolute_start: int,
    *,
    section: RoleSection | None = None,
) -> list[TextFragment]:
    parenthetical_preference = _parenthetical_preference_fragments(
        value,
        absolute_start,
    )
    if parenthetical_preference is not None:
        return parenthetical_preference

    parenthetical_preference = _degree_parenthetical_preference_fragments(
        value,
        absolute_start,
    )
    if parenthetical_preference is not None:
        return parenthetical_preference

    separators = _clause_separator_spans(value, section=section)
    if not separators:
        fragment = _content_fragment(value, absolute_start)
        return [fragment] if fragment is not None else []

    fragments: list[TextFragment] = []
    cursor = 0
    for start, end in separators:
        fragment = _content_fragment(value[cursor:start], absolute_start + cursor)
        if fragment is not None:
            fragments.append(fragment)
        cursor = end
    fragment = _content_fragment(value[cursor:], absolute_start + cursor)
    if fragment is not None:
        fragments.append(fragment)
    split_fragments: list[TextFragment] = []
    for fragment in fragments:
        split_fragments.extend(
            _parenthetical_preference_fragments(
                fragment.text,
                fragment.start,
            )
            or _degree_parenthetical_preference_fragments(
                fragment.text,
                fragment.start,
            )
            or [fragment]
        )
    return split_fragments


def _parenthetical_preference_fragments(
    value: str,
    absolute_start: int,
) -> list[TextFragment] | None:
    """Separate a contrastively preferred parenthetical from a required item."""

    spans = _balanced_parenthetical_spans(value)
    if len(spans) != 1:
        return None
    parenthetical_start, parenthetical_end = spans[0]
    outer = value[:parenthetical_start].rstrip()
    parenthetical = value[parenthetical_start + 1 : parenthetical_end - 1].strip()
    outer_signals = _clause_signals(outer)
    parenthetical_modalities = {
        cue.modality for cue in _modality_cues(parenthetical)
    }
    if (
        not outer
        or outer_signals.has_modality
        or not {"optional", "preferred"}.issubset(parenthetical_modalities)
    ):
        return None

    outer_fragment = _content_fragment(outer, absolute_start)
    preferred_fragment = _content_fragment(
        parenthetical,
        absolute_start + parenthetical_start + 1,
    )
    if outer_fragment is None or preferred_fragment is None:
        return None
    fragments = [outer_fragment, preferred_fragment]
    trailing_start = parenthetical_end
    while trailing_start < len(value) and value[trailing_start].isspace():
        trailing_start += 1
    trailing = value[trailing_start:]
    if trailing.strip() not in {"", "."}:
        fragments.extend(
            _clause_fragments(
                trailing,
                absolute_start + trailing_start,
                section=None,
            )
        )
    return fragments


def _degree_parenthetical_preference_fragments(
    value: str,
    absolute_start: int,
) -> list[TextFragment] | None:
    """Separate a preferred degree field from its mandatory degree requirement."""

    spans = _balanced_parenthetical_spans(value)
    if len(spans) != 1:
        return None
    parenthetical_start, parenthetical_end = spans[0]

    degree_requirement = value[:parenthetical_start].rstrip()
    preferred_field = value[parenthetical_start + 1 : parenthetical_end - 1].strip()
    if re.search(
        r"\b(?:degree|diploma)\s+in\b",
        degree_requirement,
        re.IGNORECASE,
    ) is None:
        return None
    if re.fullmatch(
        r"(?:[A-Za-z][\w&/-]*\s+){0,4}(?:and|or)\s+"
        r"(?:[A-Za-z][\w&/-]*\s+){0,4}"
        r"(?:major|concentration|speciali[sz]ation)\s+is\s+preferred",
        preferred_field,
        re.IGNORECASE,
    ) is None:
        return None

    mandatory = _content_fragment(degree_requirement, absolute_start)
    preferred = _content_fragment(
        preferred_field,
        absolute_start + parenthetical_start + 1,
    )
    if mandatory is None or preferred is None:
        return None
    fragments = [mandatory, preferred]
    trailing_start = parenthetical_end
    while trailing_start < len(value) and value[trailing_start].isspace():
        trailing_start += 1
    trailing = value[trailing_start:]
    if trailing.strip() not in {"", "."}:
        fragments.extend(
            _clause_fragments(
                trailing,
                absolute_start + trailing_start,
                section=None,
            )
        )
    return fragments


def _clause_separator_spans(
    value: str,
    *,
    section: RoleSection | None = None,
) -> list[tuple[int, int]]:
    separators: list[tuple[int, int]] = list(
        _flattened_modality_item_boundary_spans(value, section=section)
    )
    parenthetical_spans = _balanced_parenthetical_spans(value)
    index = 0
    while index < len(value):
        parenthetical_end = next(
            (
                end
                for start, end in parenthetical_spans
                if start <= index < end
            ),
            None,
        )
        if parenthetical_end is not None:
            index = parenthetical_end
            continue
        character = value[index]
        if character in ".?!" and _sentence_starts_independent_item(value, index):
            next_material = index + 1
            while next_material < len(value) and value[next_material].isspace():
                next_material += 1
            separators.append((index + 1, next_material))
            index = next_material
            continue
        if character in "-–—" and _contrastive_dash_starts_candidate_requirement(
            value,
            index,
        ):
            separators.append((index, index + 1))
            index += 1
            continue
        if (
            character == ";"
            and _semicolon_starts_independent_item(value, index)
        ) or _is_symbol_list_marker(value, index):
            separators.append((index, index + 1))
            index += 1
            continue
        numbered_marker = _numbered_list_marker(value, index)
        if numbered_marker is not None and _has_list_boundary_before(value, index):
            _number, end = numbered_marker
            separators.append((index, end))
            index = end
            continue
        if numbered_marker is not None:
            index = numbered_marker[1]
            continue
        index += 1
    return sorted(set(separators))


def _balanced_parenthetical_spans(value: str) -> tuple[tuple[int, int], ...]:
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(value):
        if character == "(":
            stack.append(index)
        elif character == ")" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
    return tuple(spans)


def _role_overview_action_sequence_started(value: str, boundary_start: int) -> bool:
    prefix = value[:boundary_start].rstrip()
    if not prefix:
        return False
    if prefix[-1] in ".!?":
        return True
    parenthetical_spans = _balanced_parenthetical_spans(prefix)
    sentence_boundary = max(
        (
            index
            for index, character in enumerate(prefix)
            if character in ".!?"
            and not any(start <= index < end for start, end in parenthetical_spans)
        ),
        default=-1,
    )
    sentence_start = sentence_boundary + 1
    sentence_tokens = lexical_tokens(prefix[sentence_start:])
    if not sentence_tokens:
        return False
    return sentence_tokens[0] in _UNSECTIONED_RESPONSIBILITY_HEADS or sentence_tokens[0] in {
        "analyzing",
        "applying",
        "assisting",
        "creates",
        "creating",
        "designing",
        "developing",
        "escalates",
        "helping",
        "improving",
        "incorporate",
        "installs",
        "investigates",
        "maintains",
        "monitoring",
        "participating",
        "preparing",
        "provides",
        "responding",
        "supporting",
        "working",
    }


def _flattened_modality_item_boundary_spans(
    value: str,
    *,
    section: RoleSection | None = None,
) -> tuple[tuple[int, int], ...]:
    modality_matches = re.finditer(
        r"(?:"
        r"\bnot\s+(?:a\s+)?requirement"
        r"|\bis\s+(?:a\s+)?(?:must|plus)"
        r"|\brequired"
        r"|\)(?!\s+required\b)"
        r")(?P<separator>\s+)"
        r"(?=(?:Ability|Currently|Demonstrated|Effective|Excellent|Experience|Good|Knowledge|"
        r"Must|Previous|Proven|Required|Strong)\b)",
        value,
        flags=re.IGNORECASE,
    )
    preference_item_matches = re.finditer(
        r"\b(?:desirable|preferred|a\s+plus)"
        r"(?P<separator>\s+)"
        r"(?=(?:Effective|Excellent|Experience|Familiarity|Good|Knowledge|Physical\s+work\s+environment|Strong)\b)",
        value,
        flags=re.IGNORECASE,
    )
    item_matches = re.finditer(
        r"\b(?:AI|ai|applications?|asset|clearance|communication|degree|effectively|environment|"
        r"experience|field|independently|internship|language|learning|methodologies|mindset|peers|preferred|prerequisites|program|"
        r"reports?|responsibilit(?:y|ies)|skills|success|technologies|technology|work|Workday)"
        r"(?P<separator>\s+)"
        r"(?=(?:Ability|Applicants?|Basic|Candidates?|Currently|Decision|Demonstrated|Detail-oriented|"
        r"Effective|Excellent|Experience|Exposure|Familiarity|Good|Interest|Knowledge|Must|"
        r"Customer|Previous|Proficien(?:cy|t)|Proven|Required|Solid|Strong|Successful|Well-rounded|Working|You)\b)",
        value,
    )
    physical_demand_matches = re.finditer(
        r"\benvironments(?P<separator>\s+)(?=The\s+physical\s+demands\b)",
        value,
        re.IGNORECASE,
    )
    scalar_threshold_matches = re.finditer(
        r"\b(?:\d+(?:\.\d+)?%?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"or\s+(?:higher|more|lower|fewer|less)"
        r"(?P<separator>\s+)(?=(?:Ability|Applicants?|Candidates?|Experience|Must|Required)\b)",
        value,
        re.IGNORECASE,
    )
    administrative_prefix_matches = re.finditer(
        r"\baccommodations?\s+(?:is\s+)?available\s+upon\s+request"
        r"(?P<separator>\s+)"
        r"(?=(?:Ability|Basic|Excellent|Experience|Knowledge|Proficient|Strong)\b)",
        value,
        re.IGNORECASE,
    )
    candidate_transition_matches = re.finditer(
        r"\bassignments(?P<separator>\s+)"
        r"(?=The\s+successful\s+applicants?\b)",
        value,
        re.IGNORECASE,
    )
    transcript_instruction_matches = re.finditer(
        r"(?P<separator>\s*,\s*)"
        r"(?=Uploading\s+an?\s+unofficial\s+copy\s+of\s+your\s+transcript\b)",
        value,
        re.IGNORECASE,
    )
    prior_experience_matches = re.finditer(
        r"\bknowledge(?P<separator>\s+)(?=Previous\s+experience\b)",
        value,
        re.IGNORECASE,
    )
    compressed_item_matches = re.finditer(
        r"(?P<separator>(?:(?<=[a-z])(?=(?:Assistwith|Helpidentify|Coordinate|(?-i:Conduct)|Take(?=ownership)|"
        r"Support(?=(?:finance|marketing))|WhoYou|What\s*SkillsYou|WhatYou|"
        r"Strongorganizational|Strongwritten|Strongproblem|Comfortableworking|Self-starter|"
        r"Experiencewith|Familiaritywith|Exposure(?=to)|Interestin))|"
        r"(?<=[a-z])(?=(?:(?-i:Assist)\s+with|(?-i:Support)\s+(?:finance|marketing)|"
        r"(?-i:Take)\s+ownership|(?-i:Strong)\s+(?:organizational|written|problem)|"
        r"(?-i:Comfortable)\s+working|(?-i:Exposure)\s+to|(?-i:Interest)\s+in))|"
        r"(?<=[a-z)])(?=(?-i:Familiarity)\s+with)))",
        value,
        re.IGNORECASE,
    )
    compressed_internship_matches = re.finditer(
        r"(?P<separator>(?<=[a-z])(?=Internship\b))",
        value,
        re.IGNORECASE,
    )
    flattened_stripe_action_matches = (
        re.finditer(
            r"(?P<separator>\s+)"
            r"(?=(?:(?-i:Make)\s+effective\s+tradeoffs|"
            r"(?-i:Mentor)\s+(?:engineers|and\s+support)\b))",
            value,
            re.IGNORECASE,
        )
        if section == "responsibilities"
        else ()
    )
    functional_area_matches = (
        re.finditer(
            r"(?P<separator>\s+)"
            r"(?=(?:Talent\s+Acquisition|Total\s+Rewards|"
            r"HR\s+Data\s*,\s*Analytics\s*(?:&|and)\s*Reporting|"
            r"HR\s+Administration|Projects\s*(?:&|and)\s*Initiatives)\s*[-–—])",
            value,
            re.IGNORECASE,
        )
        if section == "responsibilities"
        else ()
    )
    flattened_imperative_matches = [
        match
        for match in re.finditer(
        r"(?P<separator>\s+)"
        r"(?=(?:Act(?=\s+(?:as|on|to|upon|with)\b)|Ad\s+hoc(?=\s+(?:(?:off-cycle\s+)?financial\s+)?(?:analysis|projects?|reporting|support)\b)|Adapt|Adheres?|Analyze|Analyse|Analyzing|Applying|Assists?|Assisting|Be|Build|"
        r"Cattle(?=\s+handling\b)|Code|Collaborate|Collaborer|Collect|Communicate|Conduct|Contribute|Contribuer|Continuously|Coordinate|Creates?|Data\s+entry|Debug|Deliver|Design|Designing|Develop|Developing|Développer|Diagnose|Drafting|Field(?=\s+work\b)|"
        r"Document|Draft|Drive|Employ|Engage|Ensure|Escalate|Expand|Follows?|Gain|Help|Helping|Identify|Implement|Improve|Improving|"
        r"Escalates?|Evaluate|Incorporate|Installs?|Investigates?|Leverage|Liaise|Log|Maintains?|Manage|Monitor|Monitoring|Own|Partner|Participate|Participation(?=\s+in\b)|Perform|Prepare|"
        r"Gather|Implémenter|Intégrer|Learn|Make|Other\s+duties|Participate|Participating|Participer|Performance(?=\s+of\b)|Preparing|Produce|Provides?|Research|Resolve|Respond|Responding|Review|Scrum|Shape|Ship|Solve|Summarize|Support|Supporting|Teach|Test|"
        r"Time\s+series\s+forecasting|Training(?=\s+on\b)|Troubleshoot|Understands?|Use|Validate|Work|Working|Write)\b)",
        value,
        )
        if (
            section == "responsibilities"
            or (
                section == "role_overview"
                and _role_overview_action_sequence_started(value, match.start())
            )
        )
        and re.search(r"(?:^|[.;:*!?])\s*Lead$", value[: match.start()]) is None
        and re.match(
            r"Code\s+of\s+Conduct\b",
            value[match.end("separator") :],
            re.IGNORECASE,
        )
        is None
        and not (
            re.match(
                r"Test\s+Management\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bincluding\s*:[^.;!?]*$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.search(r"\bCode\s+of$", value[: match.start()], re.IGNORECASE)
            and re.match(
                r"Conduct\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Design\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:Domain[- ]Driven|Systems?)$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Scrum\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:a|in)\s*$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Help\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\bthe\s*$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Support\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:Logistic|Integrated\s+Logistics)$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and _preceding_colon_heading_section(value, match.start())
        != "posting_identity"
    ]
    flattened_qualification_matches = [
        match
        for match in re.finditer(
        r"(?P<separator>\s+)"
        r"(?=(?:A\s+(?:self[- ]starter|strategic\s+thinker|strong\b)|AI[- ]serious(?=\s*:)|An\s+(?:ability|aptitude)|Ability|Additional\s+BI\s+tools|Applicants?(?=\s+(?:must|wanting|will)\b)|"
        r"Bachelor\s*['’]?\s*s|Able|Advanced|Analytics(?=\s+and\s+dashboard\b)|Aptitude(?=\s+for\b)|Are|Available(?=\s+for\b)|Background(?=\s+in\b)|Basic|Be(?=\s+(?:able|authorized|available|eligible|safety|trained|willing|WHMIS)\b)|Bias(?=\s+for\s+action\b)|Bilingual|Can|Candidates?(?=\s+(?:are|must|will)\b)|Collaborative(?=\s+problem[- ]solver\b)|Committed|Comfortable|Computer\s+skills|Confirm(?=\s+you\b)|Consulting|Currently|Customer\s+service\s+experience|Data\s+visualization|Demonstrates?|Display|Drives|Eligible|Exemplify|Exceptional|"
        r"Customer[- ]service\s+experience|Computer\s+literate|Completion(?=\s+of\b)|Conduct(?=\s+oneself\b)|Confident|Creativity|Demonstrated|Direct(?=\s+project\s+leadership\b)|Enrolled(?=\s+in\b)|Enthusiastic|Excellent|Expertise|Experience|Experienced|Exposure|Familiar(?:ity)?|Final|Fluent|Fundamental|General|Good|Have|High(?=\s+attention\b)|In[- ]office\s+availability(?=\s*:)|"
        r"Autonome|Capacité|Connaissance|Compréhension|Curiosity|Curiosité|Étudiant(?:e)?|Expérience|Exposition|Familiarité|Focused|Highly\s+(?:developed|organized|proficient)|If(?=\s+you\s+are\s+selected\b)|Image\s+processing|Interest|Interested(?=\s+in\s+working\s+as\s+a\s+generalist\b)|Intérêt|Knowledge|Knowledgeable|Lifting|Marketing\s+automation|MATLAB(?=\s+or\b)|Moderate(?=\s+(?:computer|level)\b)|Must|Network(?=\s+fundamentals\b)|Notions|Open(?=\s+to\s+shift\b)|Photography|Post[- ]secondary(?=\s+(?:degree|diploma)\b)|Quebec(?=\s*:)|Quebec\s+Location(?=\s*:)|Relevant(?=\s+(?:cattle|industry|experience|knowledge)\b)|Residency|Sense\s+of\s+urgency|Team(?=\s+collaboration\b)|Test(?=\s+automation\b)|"
        r"Adept|Autonomous|Comfort(?=\s+working\b)|Indicate|Jira(?=\s+(?:experience|knowledge|skills)\b)|Logical(?=\s*,)|Office\s+environment|Oil\s+and\s+gas\s+industry\s+experience|Outstanding|Passion|Personable|Personal(?=\s+projects\b)|Physical\s+ability|Portfolio(?=\s+of\s+projects\b)|Positive|Priority(?=\s+will\b)|Proactive|Proficien(?:cy|t)|Publications|Refined|Reliable|Resilien(?:t|cy)|Selected\s+candidates?|Self[- ](?:starter|motivated)|Sensitivity|Showcases?|Solid|SQL(?=\s+(?:experience|knowledge|skills)\b)|Standard\s+\d+|Startup(?=\s+experience\b)|Strong|Superior|Understands?|Valid(?=\s+(?:driver|Class)\b)|Exposed|Working|Works(?=\s+collaboratively\b)|Written|"
        r"Understanding|Well[- ]organized|Well[- ]versed|Willingness(?=\s+to\b)|You(?=\s+(?:are|demonstrate|have|work)\b)|"
        r"Identifies(?=\s+opportunities\b)|This\s+position\s+is\s+open\b|0\s+years?)\b)",
        value,
        )
        if section in {"qualifications", "preferred_qualifications"}
        and not (
            re.match(
                r"You\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and value[: match.start()].rstrip().endswith(":")
        )
        and not (
            re.match(
                r"A\s+strong\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bBonus\s*:\s*$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"(?:(?:Jira|SQL)(?=\s+(?:experience|knowledge|skills)\b)|Network(?=\s+fundamentals\b))",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:and|basic|in|or|strong|windows|with)\s*$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Solid\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bExperience\s+with$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"Ability\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bDecision\s+Making$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"Knowledge\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:Experience|Exposure|Knowledge)\s*/$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Proficient\s+in\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bFluent\s+or$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"Understanding\s+of\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"[.!?]\s*$", value[: match.start()])
        )
        and not (
            re.match(
                r"Basic\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bSQL\s*/$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"Successful\s+applicants?\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(r"\bAll$", value[: match.start()], re.IGNORECASE)
        )
        and not (
            re.match(
                r"Bachelor\s*['’]?\s*s\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:of|in|for|toward|towards)(?:\s+an?)?$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Bachelor\s*['’]?\s*s\s+program\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:DEC|AEC|certificate)(?:\s*,\s*(?:DEC|AEC|certificate))*\.\s*$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Bachelor\s*['’]?\s*s\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:DEC|AEC|certificate|diploma)(?:\s*,\s*(?:DEC|AEC|certificate|diploma))*\s*,\s*$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
        and not (
            re.match(
                r"Bachelor\s*['’]?\s*s\b",
                value[match.end("separator") :],
                re.IGNORECASE,
            )
            and re.search(
                r"\b(?:complete|pursuing|completed|enrolled|studying|working\s+towards?)\b"
                r"[^.!?;:]*\b(?:a|an)?$",
                value[: match.start()],
                re.IGNORECASE,
            )
        )
    ]
    return tuple(
        sorted(
            {
                *(match.span("separator") for match in modality_matches),
                *(match.span("separator") for match in preference_item_matches),
                *(match.span("separator") for match in item_matches),
                *(match.span("separator") for match in physical_demand_matches),
                *(match.span("separator") for match in scalar_threshold_matches),
                *(match.span("separator") for match in administrative_prefix_matches),
                *(match.span("separator") for match in candidate_transition_matches),
                *(match.span("separator") for match in transcript_instruction_matches),
                *(match.span("separator") for match in prior_experience_matches),
                *(match.span("separator") for match in compressed_item_matches),
                *(match.span("separator") for match in compressed_internship_matches),
                *(match.span("separator") for match in flattened_stripe_action_matches),
                *(match.span("separator") for match in functional_area_matches),
                *(match.span("separator") for match in flattened_imperative_matches),
                *(match.span("separator") for match in flattened_qualification_matches),
            }
        )
    )


def _contrastive_dash_starts_candidate_requirement(value: str, index: int) -> bool:
    if index == 0 or index + 1 >= len(value):
        return False
    if not value[index - 1].isspace() or not value[index + 1].isspace():
        return False
    left = value[:index]
    right = value[index + 1 :].lstrip()
    if re.search(
        r"\b(?:do(?:es)?\s+not|don['’]t|doesn['’]t)\s+need\b",
        left,
        re.IGNORECASE,
    ) is None:
        return False
    return bool(
        re.match(
            r"(?:you|the\s+(?:applicant|candidate))\s+"
            r"(?:have\s+to|must|need(?:s)?\s+to|required\s+to)\b",
            right,
            re.IGNORECASE,
        )
    )


def _sentence_starts_independent_item(value: str, index: int) -> bool:
    if index + 1 >= len(value):
        return False
    next_material = index + 1
    if not value[next_material].isspace():
        joined_sentence = bool(
            value[index] == "."
            and index > 0
            and (value[index - 1].islower() or value[index - 1] == ")")
            and (value[next_material].isupper() or value[next_material].isdigit())
        )
        if not joined_sentence:
            return False
    else:
        while next_material < len(value) and value[next_material].isspace():
            next_material += 1
    if next_material >= len(value):
        return False

    following = value[next_material:]
    following_tokens = lexical_tokens(following)
    if not following_tokens:
        return False

    if (
        value[index] == "."
        and re.search(r"\blbs$", value[:index], re.IGNORECASE)
        and re.match(r"occasionally\b", following, re.IGNORECASE)
    ):
        return False

    if (
        value[index] == "."
        and re.search(
            r"\b(?:DEC|AEC|certificate)(?:\s*,\s*(?:DEC|AEC|certificate))*$",
            value[:index],
            re.IGNORECASE,
        )
        and re.match(r"Bachelor\s*['’]?s\s+program\b", following, re.IGNORECASE)
    ):
        return False

    if value[index] == ".":
        if index > 0 and index + 1 < len(value):
            if value[index - 1].isdigit() and value[index + 1].isdigit():
                return False
        prefix_match = re.search(r"[A-Za-z](?:[A-Za-z.]*)\.$", value[: index + 1])
        if (
            prefix_match
            and _DOTTED_ABBREVIATION.fullmatch(prefix_match.group(0))
            and not _dotted_abbreviation_can_end_sentence(
                value,
                index=index,
                following=following,
            )
        ):
            return False

    if any(_starts_with(following_tokens, opener) for opener in _DEPENDENT_CLAUSE_OPENERS):
        return _clause_signals(following).has_modality
    first = value[next_material]
    return (
        first.isupper()
        or first.isdigit()
        or first in _UNAMBIGUOUS_LIST_MARKERS
        or _clause_signals(following).has_modality
    )


def _dotted_abbreviation_can_end_sentence(
    value: str,
    *,
    index: int,
    following: str,
) -> bool:
    if not following[:1].isupper():
        return False
    following_signals = _clause_signals(following)
    following_is_independent = bool(
        following_signals.has_modality
        or following_signals.owner != "unspecified"
        or following_signals.applicant_predicate != "unspecified"
        or _has_responsibility_predicate(following_signals.tokens)
    )
    if not following_is_independent:
        return False

    prior_start = max(
        (value.rfind(boundary, 0, index) for boundary in ";!?|\u2022\u25cf\u25aa"),
        default=-1,
    )
    prior = value[prior_start + 1 : index + 1].strip()
    prior_signals = _clause_signals(prior)
    return bool(
        _is_eligibility_claim(prior)
        or prior_signals.owner != "unspecified"
        or prior_signals.applicant_predicate != "unspecified"
        or _has_responsibility_predicate(prior_signals.tokens)
    )


def _semicolon_starts_independent_item(value: str, index: int) -> bool:
    next_semicolon = value.find(";", index + 1)
    immediate_item = value[index + 1 : next_semicolon if next_semicolon >= 0 else None]
    following_tokens = lexical_tokens(immediate_item)
    if not following_tokens:
        return False
    left = value[:index].rstrip()
    if (
        re.search(
            r"\b(?:Outstanding\s+attention\s+to\s+detail|"
            r"Exceptional\s+organizational\s+skills|"
            r"Collaborative\s+problem[- ]solver|"
            r"Strong\s+collaboration\s+skills|"
            r"Excellent\s+(?:communication\s+and\s+interpersonal|"
            r"leadership\s+and\s+interpersonal|communication|interpersonal)\s+skills"
            r"(?:\s+with\s+demonstrated\s+ability\s+to\s+work\s+(?:with\s+others\s+)?effectively\s+in\s+teams)?)$",
            left,
            re.IGNORECASE,
        )
        and re.match(
            r"\s*(?:able\s+to|along\s+with|and\s+self[- ]driven\b|can\s+work\b|comfortable\b|solutions?\s+and\s+action\s+oriented\b)",
            immediate_item,
            re.IGNORECASE,
        )
    ):
        return False
    dependent_opener = any(
        _starts_with(following_tokens, opener)
        for opener in _DEPENDENT_CLAUSE_OPENERS
    )
    return not dependent_opener or _clause_signals(immediate_item).has_modality


def _is_symbol_list_marker(value: str, index: int) -> bool:
    character = value[index]
    if character not in _INLINE_LIST_MARKERS:
        return False
    if (
        character == "|"
        and _BROWSER_PRINT_TIMESTAMP_HEADER.fullmatch(value.strip()) is not None
    ):
        return False
    before_boundary = index == 0 or value[index - 1].isspace()
    after_boundary = index + 1 == len(value) or value[index + 1].isspace()
    if not before_boundary or not after_boundary:
        return False
    return character in _UNAMBIGUOUS_LIST_MARKERS or _has_list_boundary_before(
        value, index
    )


def _numbered_list_marker(value: str, index: int) -> tuple[int, int] | None:
    if not value[index].isdigit():
        return None
    if index > 0 and not value[index - 1].isspace():
        return None
    end = index + 1
    while end < len(value) and value[end].isdigit():
        end += 1
    if end - index == 4 and 1900 <= int(value[index:end]) <= 2099:
        return None
    if (
        end >= len(value)
        or value[end] not in ".)"
        or end + 1 >= len(value)
        or not value[end + 1].isspace()
    ):
        return None
    return int(value[index:end]), end + 1


def _has_list_boundary_before(value: str, index: int) -> bool:
    prefix = value[:index].rstrip()
    return not prefix or prefix[-1] in ":;.!?|\u2022\u25cf\u25aa"


@session_cached
def _clause_signals(content: str) -> ClauseSignals:
    tokens = lexical_tokens(content)
    owner, owner_width = _clause_owner(tokens)
    applicant_predicate = _applicant_predicate(tokens[owner_width:])
    negative_modal_readings = _negative_modal_readings(
        content,
        applicant_predicate=applicant_predicate,
    )
    modality_cues = _modality_cues(
        content,
        applicant_predicate=applicant_predicate,
        negative_modal_readings=negative_modal_readings,
    )
    non_requirement = any(cue.modality == "optional" for cue in modality_cues)
    explicit_required = any(cue.modality == "mandatory" for cue in modality_cues)
    soft_required = _contains_any_phrase(tokens, _SOFT_REQUIRED_PHRASES) or any(
        token == "strong"
        and any(
            complement in _QUALIFICATION_COMPLEMENT_HEADS
            for complement in tokens[index + 1 : index + 8]
        )
        for index, token in enumerate(tokens)
    )
    return ClauseSignals(
        tokens=tokens,
        owner=owner,
        applicant_predicate=applicant_predicate,
        explicit_required=explicit_required,
        expected=any(cue.modality == "expected" for cue in modality_cues),
        soft_required=soft_required,
        preferred=any(cue.modality == "preferred" for cue in modality_cues),
        non_requirement=non_requirement,
        prohibited=any(cue.modality == "prohibited" for cue in modality_cues),
        negative_modal_force=_dominant_negative_modal_force(negative_modal_readings),
        modality_cues=modality_cues,
    )


def _modality_cues(
    content: str,
    *,
    applicant_predicate: ApplicantPredicate | None = None,
    negative_modal_readings: tuple[
        tuple[int, int, NegativeModalForce], ...
    ]
    | None = None,
) -> tuple[ModalityCue, ...]:
    if applicant_predicate is None:
        tokens = lexical_tokens(content)
        _, owner_width = _clause_owner(tokens)
        applicant_predicate = _applicant_predicate(tokens[owner_width:])
    if negative_modal_readings is None:
        negative_modal_readings = _negative_modal_readings(
            content,
            applicant_predicate=applicant_predicate,
        )
    optional_spans = [
        (match.start(), match.end())
        for pattern in _OPTIONALITY_PATTERNS
        for match in pattern.finditer(content)
    ]
    if re.search(
        r"\bmust\s+be\s+legally\s+authorized\s+to\s+work\b"
        r"[^.!?;\r\n]{0,160}\bwithout\s+requiring\b"
        r"[^.!?;\r\n]{0,80}\bsponsorship\b",
        content,
        re.IGNORECASE,
    ):
        optional_spans = [
            span
            for span in optional_spans
            if content[span[0] : span[1]].casefold() != "without requiring"
        ]
    optional_spans.extend(_positive_permission_spans(content))
    prohibited_spans = [
        (start, end)
        for start, end, force in negative_modal_readings
        if force == "prohibited"
    ]
    cues = [
        ModalityCue(
            modality="optional",
            cue_start=start,
            cue_end=end,
            scope_start=0,
            scope_end=len(content),
            confidence=0.98,
        )
        for start, end in optional_spans
    ]
    cues.extend(
        ModalityCue(
            modality="prohibited",
            cue_start=start,
            cue_end=end,
            scope_start=0,
            scope_end=len(content),
            confidence=0.98,
        )
        for start, end in prohibited_spans
        if not any(_spans_overlap((start, end), optional) for optional in optional_spans)
    )
    for modality, patterns, confidence in (
        ("mandatory", _MANDATORY_PATTERNS, 0.97),
        ("expected", _EXPECTED_PATTERNS, 0.96),
        ("preferred", _PREFERENCE_PATTERNS, 0.95),
    ):
        for pattern in patterns:
            for match in pattern.finditer(content):
                if modality == "mandatory" and prohibited_spans:
                    continue
                if _modality_cue_is_conditional_adjunct(content, modality, match):
                    continue
                if _modality_cue_has_non_candidate_relative_subject(
                    content,
                    modality,
                    match,
                ):
                    continue
                span = (match.start(), match.end())
                if any(_spans_overlap(span, optional) for optional in optional_spans):
                    continue
                if any(_spans_overlap(span, prohibited) for prohibited in prohibited_spans):
                    continue
                cues.append(
                    ModalityCue(
                        modality=modality,
                        cue_start=match.start(),
                        cue_end=match.end(),
                        scope_start=0,
                        scope_end=len(content),
                        confidence=confidence,
                    )
                )
    return tuple(sorted(cues, key=lambda cue: (cue.cue_start, cue.cue_end)))


def _positive_permission_spans(content: str) -> list[tuple[int, int]]:
    spans = [
        (match.start(), match.end())
        for match in _POSITIVE_MAY_PATTERN.finditer(content)
        if _positive_may_is_non_obligation_modal(content, match)
    ]
    spans.extend(
        (match.start(), match.end())
        for match in _POSITIVE_PASSIVE_PERMISSION_PATTERN.finditer(content)
        if _passive_permission_has_positive_subject(content, match)
    )
    return spans


def _passive_permission_has_positive_subject(
    content: str,
    match: re.Match[str],
) -> bool:
    clause_prefix = re.split(r"[.!?;\r\n]", content[: match.start()])[-1]
    subject_tokens = lexical_tokens(clause_prefix)
    if subject_tokens and subject_tokens[0] in {"neither", "no"}:
        return False

    permission_tokens = lexical_tokens(match.group(0))
    if not permission_tokens:
        return False
    if _MATRIX_OBLIGATION_BEFORE_PERMISSION.search(clause_prefix):
        return False
    if _matrix_should_governs_permission(clause_prefix):
        return False
    if (
        "legally" in permission_tokens
        and _DEONTIC_SHOULD_BEFORE_LEGAL_WORK_PERMISSION.search(clause_prefix)
        and lexical_tokens(content[match.end() :])[:1] == ("work",)
    ):
        # In job-posting grammar, `should ... be legally permitted to work`
        # describes an applicant eligibility expectation. It is not the
        # positive permission reading found in `may be permitted to use X`.
        return False
    permission_auxiliary = permission_tokens[0]
    if permission_auxiliary in {"am", "are", "is", "was", "were"}:
        return True

    # Non-finite passive permission inherits its modality from the immediately
    # governing auxiliary. Thus `must be permitted` remains an obligation while
    # `may be permitted` remains a permission. Perfect/progressive passives are
    # accepted only when their adjacent auxiliary is itself finite.
    governor_tokens = list(subject_tokens)
    while governor_tokens and _is_modal_scope_intervener(governor_tokens[-1]):
        governor_tokens.pop()
    if not governor_tokens:
        return False
    governor = governor_tokens[-1]
    if permission_auxiliary == "be":
        return governor in {"can", "could", "may", "might", "should", "will", "would"}
    if permission_auxiliary == "been":
        return governor in {"had", "has", "have"}
    if permission_auxiliary == "being":
        return governor in {"am", "are", "is", "was", "were"}
    return False


def _matrix_should_governs_permission(clause_prefix: str) -> bool:
    tokens = list(lexical_tokens(clause_prefix))
    should_positions = [index for index, token in enumerate(tokens) if token == "should"]
    if not should_positions:
        return False
    scoped_tokens = tokens[should_positions[-1] + 1 :]
    if any(
        token in {"although", "but", "though", "whereas", "while"}
        for token in scoped_tokens
    ):
        return False
    return any(token in _MATRIX_SCOPE_COMPLEMENT_HEADS for token in scoped_tokens)


def _positive_may_is_non_obligation_modal(
    content: str,
    match: re.Match[str],
) -> bool:
    if _may_is_calendar_reference(content, match):
        return False
    if re.search(
        r"\bwhy\s+(?:an?\s+)?(?:model|process|service|solution|system)\s*$",
        content[: match.start()],
        re.IGNORECASE,
    ):
        return False
    predicate = tuple(
        token
        for token in lexical_tokens(content[match.end() :])
        if not _is_modal_scope_intervener(token)
    )
    if not predicate or predicate[0] == "not":
        return False
    if re.match(
        r"^\s*Relevant\s+exposure\s+may\s+include\b",
        content,
        re.IGNORECASE,
    ):
        return False
    if predicate[:2] == ("be", "considered") and re.match(
        r"^\s*Should\b.*\bnot\s+be\s+available\b",
        content,
        re.IGNORECASE,
    ) is None:
        return False
    return not _matching_prefix_width(predicate, _POSITIVE_MAY_OBLIGATION_PREFIXES)


def _may_is_calendar_reference(content: str, match: re.Match[str]) -> bool:
    before = content[: match.start()]
    after = content[match.end() :]
    if _CALENDAR_MAY_AFTER.match(after):
        return True
    return match.group(0) == "May" and bool(
        _CALENDAR_MAY_CONTEXT_BEFORE.search(before)
        or _CALENDAR_MAY_NOUN_AFTER.match(after)
    )


def _is_modal_scope_intervener(token: str) -> bool:
    return token in _MODAL_SCOPE_INTERVENERS


def _negative_modal_readings(
    content: str,
    *,
    applicant_predicate: ApplicantPredicate,
) -> tuple[tuple[int, int, NegativeModalForce], ...]:
    return tuple(
        (
            match.start(),
            match.end(),
            _negative_modal_force(
                content,
                match,
                applicant_predicate=applicant_predicate,
            ),
        )
        for pattern in _PROHIBITED_PATTERNS
        for match in pattern.finditer(content)
        if not _is_focus_negation(content, match)
        and not _negation_is_inside_embedded_clause(content, match)
    )


def _is_focus_negation(content: str, negative_match: re.Match[str]) -> bool:
    suffix = content[negative_match.end() :]
    return bool(re.match(r"\s+(?:only|just|merely)\b", suffix, re.IGNORECASE))


def _negation_is_inside_embedded_clause(
    content: str,
    negative_match: re.Match[str],
) -> bool:
    prefix = content[: negative_match.start()]
    markers = tuple(
        re.finditer(
            r"\b(?:how|what|when|whether)\b",
            prefix,
            re.IGNORECASE,
        )
    )
    if not markers:
        return False
    marker = markers[-1]
    if not prefix[: marker.start()].strip(" ,;:-"):
        return False
    embedded_subject = prefix[marker.end() :]
    if re.search(r"[,;:.!?]", embedded_subject):
        return False
    if re.fullmatch(
        r"\s*(?:he|i|it|she|they|we|you)\s*",
        embedded_subject,
        re.IGNORECASE,
    ):
        return True
    if marker.group(0).casefold() != "when":
        return False
    return bool(
        re.fullmatch(
            r"\s*(?:(?:a|an|the)\s+)?[A-Za-z][\w'-]*"
            r"(?:\s+[A-Za-z][\w'-]*){0,3}\s*",
            embedded_subject,
            re.IGNORECASE,
        )
    )


def _negative_modal_force(
    content: str,
    match: re.Match[str],
    *,
    applicant_predicate: ApplicantPredicate,
) -> NegativeModalForce:
    """Resolve negative auxiliaries by modal force and governed predicate.

    In job postings, strong deontic auxiliaries (must/shall/should) and explicit
    permission language introduce prohibitions. Weak possibility auxiliaries are
    epistemic when they govern a state or a candidate qualification, but `may not`
    remains deontic before an eventive action such as disclosing customer data.
    """
    cue_tokens = lexical_tokens(match.group(0))
    if not cue_tokens:
        return "none"
    auxiliary = {
        "aren": "are",
        "couldn": "could",
        "didn": "did",
        "doesn": "does",
        "don": "do",
        "isn": "is",
        "mightn": "might",
        "mustn": "must",
        "shalln": "shall",
        "shouldn": "should",
        "wasn": "was",
        "weren": "were",
        "won": "will",
        "wouldn": "would",
    }.get(cue_tokens[0], cue_tokens[0])
    if auxiliary == "cannot":
        auxiliary = "can"
    if auxiliary in {
        "is",
        "are",
        "was",
        "were",
        "be",
        "do",
        "does",
        "did",
        "must",
        "shall",
        "should",
    }:
        return "prohibited"

    predicate_tokens = lexical_tokens(content[match.end() :])
    if auxiliary in {"might", "could", "will", "would"}:
        return "epistemic"
    if auxiliary == "may":
        if _is_disqualifying_negative_complement(predicate_tokens):
            return "prohibited"
        if applicant_predicate == "qualification" or _is_epistemic_negative_predicate(
            predicate_tokens
        ):
            return "epistemic"
    if auxiliary == "can" and _has_epistemic_quantifier(predicate_tokens):
        return "epistemic"
    return "prohibited"


def _is_epistemic_negative_predicate(tokens: tuple[str, ...]) -> bool:
    predicate = _without_epistemic_adverbs(tokens)
    return bool(
        predicate
        and predicate[0]
        in {
            "appear",
            "be",
            "have",
            "know",
            "lack",
            "meet",
            "seem",
            "satisfy",
            "understand",
        }
    )


def _is_disqualifying_negative_complement(tokens: tuple[str, ...]) -> bool:
    predicate = _without_epistemic_adverbs(tokens)
    return any(
        (
            _has_governed_disqualifying_complement(predicate),
            _contains_any_phrase(predicate, _EXCLUDED_LOCATION_STATES),
            _contains_minimum_age_exclusion(predicate),
        )
    )


def _has_governed_disqualifying_complement(tokens: tuple[str, ...]) -> bool:
    """Require legal-status complements to be governed by possession or absence."""

    for index in range(len(tokens)):
        licenser_width = _matching_prefix_width(
            tokens[index:],
            _DISQUALIFYING_COMPLEMENT_LICENSERS,
        )
        if not licenser_width:
            continue
        complement = tokens[index + licenser_width :]
        if complement[:1] and complement[0] in _DISQUALIFYING_COMPLEMENT_DETERMINERS:
            complement = complement[1:]
        if _matching_prefix_width(
            complement,
            _DISQUALIFYING_POSSESSION_COMPLEMENTS,
        ):
            return True
    return False


def _contains_minimum_age_exclusion(tokens: tuple[str, ...]) -> bool:
    for index in range(len(tokens)):
        predicate = tokens[index:]
        if _matching_prefix_width(predicate, _UNDERAGE_STATE_PREDICATES):
            return True
        if _has_coordinated_minimum_age_bound(predicate):
            return True
        predicate_width = _matching_prefix_width(
            predicate,
            _MINIMUM_AGE_LOWER_BOUND_PREDICATES,
        )
        if not predicate_width:
            continue
        value_start = index + predicate_width
        if tokens[value_start : value_start + 3] == ("the", "age", "of"):
            value_start += 3
        if value_start >= len(tokens) or not tokens[value_start].isdigit():
            continue
        remainder = tokens[value_start + 1 :]
        if not remainder or remainder[0] in _AGE_BOUNDARY_HEADS:
            return True
        if any(item in _AGE_MEASURE_HEADS for item in remainder[:4]):
            return True
    return False


def _has_coordinated_minimum_age_bound(tokens: tuple[str, ...]) -> bool:
    predicate_width = _matching_prefix_width(tokens, _AGE_VALUE_PREFIXES)
    if not predicate_width:
        return False
    value_start = predicate_width
    if value_start >= len(tokens) or not tokens[value_start].isdigit():
        return False
    suffix_start = value_start + 1
    measure_width = _matching_prefix_width(
        tokens[suffix_start:],
        _AGE_VALUE_MEASURE_PHRASES,
    )
    suffix_start += measure_width
    return bool(
        _matching_prefix_width(
            tokens[suffix_start:],
            _MINIMUM_AGE_COORDINATED_BOUND_SUFFIXES,
        )
    )


def _has_epistemic_quantifier(tokens: tuple[str, ...]) -> bool:
    return bool(tokens and tokens[0] in {"always", "necessarily"})


def _without_epistemic_adverbs(tokens: tuple[str, ...]) -> tuple[str, ...]:
    cursor = 0
    while cursor < len(tokens) and tokens[cursor] in {
        "always",
        "necessarily",
        "typically",
        "usually",
        "yet",
    }:
        cursor += 1
    return tokens[cursor:]


def _dominant_negative_modal_force(
    readings: tuple[tuple[int, int, NegativeModalForce], ...],
) -> NegativeModalForce:
    forces = {force for _, _, force in readings}
    if "prohibited" in forces:
        return "prohibited"
    if "epistemic" in forces:
        return "epistemic"
    return "none"


def _modality_cue_is_conditional_adjunct(
    content: str,
    modality: str,
    match: re.Match[str],
) -> bool:
    if modality != "mandatory":
        return False
    if match.group(0).casefold() == "should":
        inverted = _PREPOSED_INVERTED_SHOULD_CONDITION_PATTERN.match(content)
        return bool(
            inverted is not None
            and match.start() == inverted.start("condition")
        )
    if match.group(0).casefold() != "required":
        return False
    return re.search(r"\b(?:as|if|when|where)\s*$", content[: match.start()], re.I) is not None


def _modality_cue_has_non_candidate_relative_subject(
    content: str,
    modality: str,
    match: re.Match[str],
) -> bool:
    if modality != "mandatory":
        return False
    cue_tokens = lexical_tokens(match.group(0))
    if not cue_tokens or cue_tokens[0] not in {"require", "requires"}:
        return False
    prefix_tokens = lexical_tokens(content[: match.start()])
    if len(prefix_tokens) < 2 or prefix_tokens[-1] not in {"that", "which", "who"}:
        return False
    return prefix_tokens[-2] in _UNSECTIONED_NON_CANDIDATE_SUBJECT_HEADS


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _clause_owner(tokens: tuple[str, ...]) -> tuple[ClauseOwner, int]:
    if len(tokens) >= 3 and tokens[0] == "our" and tokens[1:3] in {
        ("ideal", "candidate"),
        ("ideal", "applicant"),
        ("successful", "candidate"),
        ("successful", "applicant"),
    }:
        return "applicant", 3

    determiner_width = 1 if tokens and tokens[0] in _DETERMINERS else 0
    noun_phrase = tokens[determiner_width:]
    applicant_width = _applicant_noun_phrase_width(noun_phrase)
    if applicant_width:
        return "applicant", determiner_width + applicant_width
    applicant_pronoun_width = _matching_prefix_width(tokens, _APPLICANT_PRONOUNS)
    if applicant_pronoun_width:
        return "applicant", applicant_pronoun_width

    company_width = _matching_prefix_width(noun_phrase, _COMPANY_NOUN_PHRASES)
    if company_width:
        subject_end = determiner_width + company_width
        applicant_end = _organizational_requirement_applicant_target(
            tokens,
            subject_end=subject_end,
        )
        if applicant_end:
            return "applicant", applicant_end
        return "company", subject_end
    if tokens[:1] == ("our",):
        possessed_company_width = _matching_prefix_width(
            tokens[1:],
            _COMPANY_NOUN_PHRASES,
        )
        if possessed_company_width:
            subject_end = 1 + possessed_company_width
            applicant_end = _organizational_requirement_applicant_target(
                tokens,
                subject_end=subject_end,
            )
            if applicant_end:
                return "applicant", applicant_end
            return "company", subject_end
    company_pronoun_width = _matching_prefix_width(tokens, _COMPANY_PRONOUNS)
    if company_pronoun_width:
        applicant_end = _organizational_requirement_applicant_target(
            tokens,
            subject_end=company_pronoun_width,
        )
        if applicant_end:
            return "applicant", applicant_end
        if not _introductory_applicant_owner(tokens):
            return "company", company_pronoun_width
    introductory_owner = _introductory_applicant_owner(tokens)
    if introductory_owner:
        return "applicant", introductory_owner
    role_width = _matching_prefix_width(noun_phrase, _ROLE_NOUN_PHRASES)
    if role_width:
        return "role", determiner_width + role_width
    return "unspecified", 0


def _applicant_noun_phrase_width(tokens: tuple[str, ...]) -> int:
    """Return the width of an applicant-denoting noun phrase.

    Applicant heads admit ordinary stacked premodifiers in job postings, as in
    `successful candidate`, `new employees`, and `newly hired employee`.
    """

    cursor = 0
    while cursor < len(tokens) and tokens[cursor] in _APPLICANT_PRENOMINAL_MODIFIERS:
        cursor += 1
    if cursor < len(tokens) and tokens[cursor] in _APPLICANT_NOUN_HEADS:
        return cursor + 1
    return 0


def _organizational_requirement_applicant_target(
    tokens: tuple[str, ...],
    *,
    subject_end: int,
) -> int:
    predicate = tokens[subject_end:]
    predicate_width = _matching_prefix_width(
        predicate,
        _ORGANIZATIONAL_APPLICANT_TARGET_PREDICATES,
    )
    if not predicate_width:
        return 0

    target_start = subject_end + predicate_width
    if tokens[target_start : target_start + 1] == ("that",):
        target_start += 1
    target_determiner_width = (
        1
        if tokens[target_start : target_start + 1]
        and tokens[target_start] in _APPLICANT_TARGET_DETERMINERS
        else 0
    )
    noun_start = target_start + target_determiner_width
    applicant_width = _applicant_noun_phrase_width(tokens[noun_start:])
    if applicant_width:
        return noun_start + applicant_width
    if tokens[target_start : target_start + 1] == ("you",):
        return target_start + 1
    if _organizational_requirement_has_applicant_complement(
        tokens,
        predicate=predicate,
        target_start=target_start,
    ):
        return target_start
    return 0


def _organizational_requirement_has_applicant_complement(
    tokens: tuple[str, ...],
    *,
    predicate: tuple[str, ...],
    target_start: int,
) -> bool:
    if not _matching_prefix_width(
        predicate,
        _IMPLICIT_APPLICANT_REQUIREMENT_PREDICATES,
    ):
        return False
    complement = tokens[target_start:]
    return any(
        token in _QUALIFICATION_COMPLEMENT_HEADS for token in complement
    ) or _contains_any_phrase(
        complement,
        _APPLICANT_ATTRIBUTE_PHRASES,
    ) or _has_applicant_employment_condition(complement)


def _has_applicant_employment_condition(tokens: tuple[str, ...]) -> bool:
    """Recognize work-condition complements with an implicit applicant argument."""

    cursor = _employment_condition_width(tokens)
    if not cursor:
        return False
    while cursor < len(tokens) and tokens[cursor] in _EMPLOYMENT_CONDITION_COORDINATORS:
        coordinated_width = _employment_condition_width(tokens[cursor + 1 :])
        if not coordinated_width:
            return False
        cursor += 1 + coordinated_width
    remainder = tokens[cursor:]
    return not remainder or remainder[0] in {
        *_PREPOSITION_HEADS,
        *_EMPLOYMENT_CONDITION_ADJUNCT_HEADS,
    }


def _employment_condition_width(tokens: tuple[str, ...]) -> int:
    cursor = 0
    while cursor < len(tokens) and tokens[cursor] in _EMPLOYMENT_CONDITION_PREMODIFIERS:
        cursor += 1
    condition_width = _matching_prefix_width(
        tokens[cursor:],
        _EMPLOYMENT_CONDITION_COMPLEMENTS,
    )
    return cursor + condition_width if condition_width else 0


def _introductory_applicant_owner(tokens: tuple[str, ...]) -> int:
    for index in range(min(len(tokens), 8)):
        if any(token in _UNSECTIONED_RESPONSIBILITY_HEADS for token in tokens[:index]):
            break
        width = _applicant_noun_phrase_width(tokens[index:])
        if width:
            return index + width
        if tokens[index : index + 1] == ("you",):
            return index + 1
    role_requires_you = (
        ("this", "role", "requires", "you"),
        ("the", "role", "requires", "you"),
        ("this", "position", "requires", "you"),
        ("the", "position", "requires", "you"),
    )
    for phrase in role_requires_you:
        for index in range(len(tokens) - len(phrase) + 1):
            if tokens[index : index + len(phrase)] == phrase:
                return index + len(phrase)
    return 0


def _applicant_predicate(tokens: tuple[str, ...]) -> ApplicantPredicate:
    while tokens[:1] in {("and",), ("or",)}:
        tokens = tokens[1:]
    if tokens[:1] == ("ll",):
        tokens = ("will", *tokens[1:])
    if _matching_prefix_width(tokens, _HARD_REQUIREMENT_PREDICATE_PREFIXES):
        return "hard_requirement"
    predicate_tokens = tokens
    had_future_auxiliary = False
    if predicate_tokens[:1] in {
        ("will",),
        ("would",),
        ("can",),
        ("could",),
        ("may",),
        ("might",),
    }:
        had_future_auxiliary = True
        predicate_tokens = predicate_tokens[1:]
        if predicate_tokens[:1] in {("ideally",), ("typically",), ("usually",)}:
            predicate_tokens = predicate_tokens[1:]
    if _matching_prefix_width(
        predicate_tokens,
        (
            ("be", "required"),
            ("be", "experienced"),
            ("be", "familiar"),
            ("be", "proficient"),
            ("have", "to"),
            ("need", "to"),
            ("needs", "to"),
        ),
    ):
        return "hard_requirement"
    possession_width = _matching_prefix_width(
        predicate_tokens, _POSSESSION_PREDICATE_PREFIXES
    )
    if possession_width:
        complement = predicate_tokens[possession_width:]
        if _matching_prefix_width(complement, _CONTEXTUAL_POSSESSION_COMPLEMENT_PREFIXES):
            return "descriptive"
        if any(token in _QUALIFICATION_COMPLEMENT_HEADS for token in complement):
            return "qualification"
    if had_future_auxiliary or _matching_prefix_width(
        predicate_tokens, _DESCRIPTIVE_PREDICATE_PREFIXES
    ):
        return "descriptive"
    return "unspecified"


def _section_evidence(
    fragment: TextFragment,
    *,
    section: RoleSection,
    signals: ClauseSignals,
) -> RoleEvidence | None:
    if re.search(
        r"\b(?:accommodations?\s+(?:can|will)\s+be\s+provided|"
        r"if\s+you\s+require\s+an?\s+accommodation)\b",
        fragment.text,
        re.IGNORECASE,
    ):
        return None
    if re.match(
        r"^The\s+physical\s+demands\s+described\s+are\s+representative\b",
        fragment.text,
        re.IGNORECASE,
    ):
        return None
    if section == "unsectioned":
        promise_coordination = _unsectioned_employer_promise_coordination(
            fragment.text,
            fragment.start,
        )
        if promise_coordination is None:
            if _is_unsectioned_employer_promise(signals):
                return None
        elif not promise_coordination:
            return None
        else:
            claims: list[RoleClaim] = []
            quality_flags: list[str] = []
            for duty_fragment, subject_prefix in promise_coordination:
                duty_claims, duty_flags = _claims_for_fragment(
                    duty_fragment,
                    section=section,
                )
                for flag in duty_flags:
                    if flag not in quality_flags:
                        quality_flags.append(flag)
                for claim in duty_claims:
                    claim.kind = "responsibility"
                    claim.modality = "descriptive"
                    claim.owner = "applicant"
                    display_text = (claim.normalized_text or claim.text).strip()
                    display_text = display_text.rstrip(",;:")
                    if display_text and display_text[-1] not in ".?!":
                        display_text = f"{display_text}."
                    claim.normalized_text = f"{subject_prefix} {display_text}"
                    claim.decision_trace = _claim_decision_trace(
                        section=section,
                        kind=claim.kind,
                        modality=claim.modality,
                        polarity=claim.polarity,
                        condition_type=claim.condition_type,
                        coordination=claim.coordination,
                    )
                    claim.decision_trace.append(
                        "subject:inherited_after_employer_promise"
                    )
                claims.extend(duty_claims)
            return _evidence_from_claims(
                fragment,
                section=section,
                claims=claims,
                quality_flags=quality_flags,
            )
    if section in {
        "unsectioned",
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
    }:
        claims, quality_flags = _claims_for_fragment(fragment, section=section)
        if claims:
            return _evidence_from_claims(
                fragment,
                section=section,
                claims=claims,
                quality_flags=quality_flags,
            )
    return None


def _company_evidence(
    fragment: TextFragment,
    *,
    signals: ClauseSignals,
    applicant_scope: bool,
) -> tuple[RoleEvidence | None, bool]:
    claims, quality_flags = _claims_for_fragment(
        fragment,
        section="company",
        applicant_scope=applicant_scope,
    )
    if not claims:
        return None, applicant_scope
    evidence = _evidence_from_claims(
        fragment,
        section="company",
        claims=claims,
        quality_flags=quality_flags,
    )
    next_scope = any(
        claim.owner == "applicant"
        and claim.kind in {"qualification", "eligibility"}
        for claim in claims
    ) or (
        signals.explicit_required
        and any(
            claim.kind in {"qualification", "eligibility"}
            for claim in claims
        )
    ) or (
        applicant_scope
        and any(claim.kind in {"qualification", "eligibility"} for claim in claims)
    )
    return evidence, next_scope


def _requirement_modality(
    signals: ClauseSignals,
    *,
    default: Literal["required", "non_requirement"],
) -> Literal["required", "preferred", "non_requirement"]:
    if signals.non_requirement:
        return "non_requirement"
    if signals.explicit_required:
        return "required"
    if signals.preferred:
        return "preferred"
    if signals.soft_required:
        return "required"
    return default


def _context_evidence(fragment: TextFragment) -> RoleEvidence:
    claim = RoleClaim(
        kind="company_context",
        modality="descriptive",
        owner="employer",
        source_start=fragment.start,
        source_end=fragment.end,
        text=fragment.text,
        confidence=0.95,
    )
    return _evidence_from_claims(fragment, section="company", claims=[claim])


def _capability_evidence(
    fragment: TextFragment,
    *,
    signals: ClauseSignals,
) -> RoleEvidence:
    polarity = _claim_polarity(fragment.text, "descriptive")
    coordination = _coordination(fragment.text)
    environment_claim = RoleClaim(
        claim_id=f"role-claim-{fragment.start}-environment",
        kind="environment",
        modality="descriptive",
        owner="team",
        polarity=polarity,
        source_start=fragment.start,
        source_end=fragment.end,
        text=fragment.text,
        coordination=coordination,
        coordination_group=_coordination_group(fragment, 0),
        confidence=0.95,
        decision_trace=_claim_decision_trace(
            section="capabilities",
            kind="environment",
            modality="descriptive",
            polarity=polarity,
            condition_type=None,
            coordination=coordination,
        ),
    )
    claims = [environment_claim]
    quality_flags: list[str] = []
    if signals.has_modality:
        requirement_claims, quality_flags = _claims_for_fragment(
            fragment,
            section="qualifications",
        )
        for claim in requirement_claims:
            claim.decision_trace = [
                "source_section:capabilities"
                if item == "source_section:qualifications"
                else item
                for item in claim.decision_trace
            ]
        claims.extend(
            claim
            for claim in requirement_claims
            if claim.kind in {"qualification", "eligibility"}
        )
    return _evidence_from_claims(
        fragment,
        section="capabilities",
        claims=claims,
        quality_flags=quality_flags,
    )


def _claims_for_fragment(
    fragment: TextFragment,
    *,
    section: RoleSection,
    applicant_scope: bool = False,
) -> tuple[list[RoleClaim], list[str]]:
    propositions = _proposition_fragments(fragment)
    claims: list[RoleClaim] = []
    evidence_flags: list[str] = []
    full_signals = _clause_signals(fragment.text)

    for index, proposition in enumerate(propositions):
        signals = _clause_signals(proposition.text)
        if _is_incomplete_candidate_modal_proposition(proposition.text, signals):
            continue
        kind = _claim_kind(
            proposition.text,
            signals=signals,
            section=section,
            applicant_scope=applicant_scope,
        )
        if (
            proposition.inherits_subject
            and full_signals.owner == "applicant"
            and kind == "company_context"
        ):
            kind = (
                "eligibility"
                if _is_eligibility_claim(proposition.text)
                else "qualification"
            )
        if (
            proposition.inherits_subject
            and full_signals.owner != "company"
            and full_signals.explicit_required
            and kind == "company_context"
        ):
            kind = (
                "eligibility"
                if _is_eligibility_claim(proposition.text)
                else "qualification"
            )
        modality = _claim_modality(
            signals,
            section=section,
            kind=kind,
            text=proposition.text,
        )
        if proposition.inherited_modality is not None:
            modality = proposition.inherited_modality
        if modality == "prohibited" and kind in {"qualification", "eligibility"}:
            modality = "mandatory"
        if modality == "unknown" and kind in {"qualification", "eligibility"}:
            modality = "mandatory"
        owner = proposition.explicit_owner or _claim_owner(signals.owner, kind)
        if proposition.inherits_subject and owner == "unknown":
            owner = _claim_owner(full_signals.owner, kind)
        claim_flags = _section_conflict_flags(
            section=section,
            kind=kind,
            modality=modality,
        )
        for flag in claim_flags:
            if flag not in evidence_flags:
                evidence_flags.append(flag)

        claim_text = proposition.text
        claim_start = proposition.start
        claim_end = proposition.end
        if proposition.inherits_subject and not _is_self_contained_inherited_predicate(
            proposition.text,
            signals.modality_cues,
        ):
            claim_text = fragment.text
            claim_start = fragment.start
            claim_end = fragment.end
        normalized_text = _normalized_proposition_text(fragment, proposition)
        condition_text, condition_type = _claim_condition(
            proposition.text,
            signals.modality_cues,
        )
        polarity = _claim_polarity(proposition.text, modality)
        coordination = _coordination(proposition.text)
        claim_id = f"role-claim-{fragment.start}-{index}"
        claim = RoleClaim(
            claim_id=claim_id,
            kind=kind,
            modality=modality,
            owner=owner,
            polarity=polarity,
            condition_text=condition_text,
            condition_type=condition_type,
            coordination=coordination,
            coordination_group=_coordination_group(fragment, index),
            source_start=claim_start,
            source_end=claim_end,
            text=claim_text,
            normalized_text=normalized_text,
            confidence=_claim_confidence(section=section, modality=modality),
            quality_flags=claim_flags,
            decision_trace=_claim_decision_trace(
                section=section,
                kind=kind,
                modality=modality,
                polarity=polarity,
                condition_type=condition_type,
                coordination=coordination,
            ),
        )
        claims.append(claim)

    modalities = {cue.modality for cue in full_signals.modality_cues}
    if len(modalities) > 1 and len(claims) == 1:
        evidence_flags.append("mixed_modality_scope")
        claims[0].quality_flags.append("mixed_modality_scope")
    return claims, evidence_flags


def _is_incomplete_candidate_modal_proposition(
    value: str,
    signals: ClauseSignals,
) -> bool:
    if signals.owner not in {"applicant", "role"}:
        return False
    tokens = signals.tokens
    if not tokens or (len(tokens) >= 2 and tokens[-2:] == ("a", "must")):
        return False
    if tokens[-1] in {
        "can",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "should",
        "will",
        "would",
    }:
        return True
    return len(tokens) >= 2 and tokens[-1] == "to" and tokens[-2] in {
        "allowed",
        "eligible",
        "expected",
        "has",
        "have",
        "need",
        "needs",
        "permitted",
        "required",
    }


def _proposition_fragments(fragment: TextFragment) -> list[PropositionFragment]:
    scoped_predicates = academic_predicate_spans(fragment.text)
    if scoped_predicates:
        return [
            PropositionFragment(
                text=span.text,
                start=fragment.start + span.start,
                end=fragment.start + span.end,
                inherits_subject=False,
                explicit_owner="applicant",
                inherited_modality=(
                    "preferred" if span.text.casefold().startswith("preference for ") else None
                ),
            )
            for span in scoped_predicates
        ]
    if re.match(
        r"^\s*The\s+international\s+student\s*,\s*who\s+is\s+not\b"
        r"[^.!?]{1,700}\bhas\s+a\s+valid\s+Study\s+Permit\b",
        fragment.text,
        re.IGNORECASE,
    ):
        return [
            PropositionFragment(
                text=fragment.text,
                start=fragment.start,
                end=fragment.end,
            )
        ]
    if re.match(
        r"^\s*This\s+role\s+will\s+require\s+you\s+to\s+actively\s+own\s+and\s+develop\b",
        fragment.text,
        re.IGNORECASE,
    ):
        return [
            PropositionFragment(
                text=fragment.text,
                start=fragment.start,
                end=fragment.end,
            )
        ]
    if re.search(
        r"^\s*(?:pursuit\s+of\s+)?(?:bachelor\s*['’]?\s*s|degree|diploma)\b",
        fragment.text,
        re.IGNORECASE,
    ) and re.search(
        r"\b(?:preferred|nice\s+to\s+have|asset|optional|not\s+required)\b",
        fragment.text,
        re.IGNORECASE,
    ) is None:
        return [
            PropositionFragment(
                text=fragment.text,
                start=fragment.start,
                end=fragment.end,
            )
        ]

    connector_pattern = re.compile(
        r",?\s+(while|whereas|although|though|but|and|or)\s+|,\s+",
        re.IGNORECASE,
    )
    optional_spans = [
        (cue.cue_start, cue.cue_end)
        for cue in _modality_cues(fragment.text)
        if cue.modality == "optional"
    ]
    parenthetical_spans = _balanced_parenthetical_spans(fragment.text)
    connector_matches = [
        match
        for match in connector_pattern.finditer(fragment.text)
        if not any(start <= match.start() < end for start, end in optional_spans)
        and not any(
            start <= match.start() < end
            for start, end in parenthetical_spans
        )
    ]
    comma_spans = [match.span() for match in re.finditer(r",", fragment.text)]
    supplemental_comma_starts, _supplemental_coordinator_starts = (
        _supplemental_coordination_separator_starts(
            fragment.text,
            comma_spans=comma_spans,
            coordinator_spans=sorted(
                [match.span() for match in _logical_or_matches(fragment.text)]
                + [match.span() for match in _logical_and_matches(fragment.text)]
            ),
        )
    )
    fragments: list[PropositionFragment] = []
    cursor = 0
    inherited_modality: DistributedModality | None = None
    unresolved_bare_coordinate = False
    inverted_should = _PREPOSED_INVERTED_SHOULD_CONDITION_PATTERN.match(
        fragment.text
    )
    inverted_should_comma = (
        inverted_should.end("condition")
        if inverted_should is not None
        else None
    )
    for match_index, match in enumerate(connector_matches):
        if match.start() < cursor:
            continue
        left_text = fragment.text[cursor : match.start()].strip().rstrip(",")
        right_text = fragment.text[match.end() :].strip()
        if not left_text or not right_text:
            continue
        left_signals = _clause_signals(left_text)
        connector = match.group(1).casefold() if match.group(1) else "comma"
        if connector == "comma" and match.start() in supplemental_comma_starts:
            continue
        if connector == "comma" and match.start() == inverted_should_comma:
            continue
        if connector == "comma" and _comma_closes_condition_adjunct(
            fragment.text,
            match.start(),
        ):
            continue
        predicate_scope_end = next(
            (
                following.start()
                for following in connector_matches[match_index + 1 :]
                if following.group(1) is None
                or following.group(1).casefold()
                in {"while", "whereas", "although", "though", "but"}
            ),
            len(fragment.text),
        )
        right_predicate_text = fragment.text[
            match.end() : predicate_scope_end
        ].strip()
        right_signals = _clause_signals(right_predicate_text)
        coordinate_scope_end = (
            connector_matches[match_index + 1].start()
            if match_index + 1 < len(connector_matches)
            else len(fragment.text)
        )
        right_coordinate_text = fragment.text[
            match.end() : coordinate_scope_end
        ].strip().rstrip(",")
        right_predicate_tokens = lexical_tokens(right_coordinate_text)
        if _coordinator_continues_governed_infinitive(
            connector,
            left_text,
            right_predicate_tokens,
        ) or _contrastive_connector_completes_preposed_condition(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_precedes_embedded_explicit_subject(
            connector,
            left_text,
            right_coordinate_text,
        ) or _is_right_node_shared_prepositional_coordinate(
            left_text,
            right_coordinate_text,
        ) or _coordinate_is_inside_finite_explanation(
            fragment.text,
            match.start(),
            right_coordinate_text,
        ) or _coordinator_shares_delayed_degree_object(
            connector,
            left_text,
            right_text,
            right_coordinate_text,
        ) or _coordinator_joins_nominal_before_serial_predicate(
            fragment.text,
            match,
            connector=connector,
            left_text=left_text,
            right_coordinate_text=right_coordinate_text,
        ) or _coordinator_joins_degree_field_coordinate(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_joins_nominal_co_subjects(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_joins_output_nouns(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_joins_related_nominal_objects(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_employer_alternative(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_cross_border_eligibility(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_team_collaboration(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_enrollment_return_plan(
            connector,
            left_text,
            right_coordinate_text,
        ) or _comma_continues_eligibility_purpose(
            connector,
            left_text,
            right_coordinate_text,
        ) or _comma_continues_required_adjective(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_controlled_goods_requirement(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_enrolled_maintenance(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_program_eligibility(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_ability_predicate_sequence(
            connector,
            left_text,
            right_coordinate_text,
        ) or _comma_continues_degree_field_coordinate(
            connector,
            left_text,
            right_coordinate_text,
        ) or _comma_continues_credential_program(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_program_field(
            connector,
            left_text,
            right_coordinate_text,
        ) or _comma_continues_candidate_skill_list(
            connector,
            left_text,
            right_coordinate_text,
        ) or _coordinator_continues_language_proficiency(
            connector,
            left_text,
            right_coordinate_text,
        ):
            continue
        right_is_coordinate_predicate = _is_coordinated_predicate_complement(
            right_coordinate_text,
            antecedent_text=left_text,
            antecedent_inherits_modality=inherited_modality is not None,
            antecedent_closes_embedded_scope=match.group(0).lstrip().startswith(","),
        )
        if (
            connector == "comma"
            and right_predicate_tokens
            and _is_bare_requirement_predicate_token(right_predicate_tokens[0])
            and not right_is_coordinate_predicate
        ):
            unresolved_bare_coordinate = True
        elif (
            connector in {"and", "or"}
            and unresolved_bare_coordinate
            and inherited_modality is None
            and _looks_like_nominal_coordinate(right_predicate_tokens)
        ):
            right_is_coordinate_predicate = False
        shares_embedded_finite_predicate = bool(
            connector in {"and", "or"}
            and _embedded_nominals_share_finite_predicate(
                left_text,
                right_predicate_text,
            )
        )
        right_owner_has_finite_predicate = _owner_signal_has_finite_predicate(
            right_signals
        )
        right_has_predicate = (
            right_owner_has_finite_predicate
            or right_signals.applicant_predicate != "unspecified"
            or right_signals.has_modality
            or right_is_coordinate_predicate
        )
        if connector == "comma":
            should_split = bool(
                not shares_embedded_finite_predicate
                and (
                    (left_signals.has_modality and right_signals.has_modality)
                    or (
                        right_is_coordinate_predicate
                        and (
                            left_signals.has_modality
                            or inherited_modality is not None
                        )
                    )
                )
            )
        else:
            should_split = bool(
                not shares_embedded_finite_predicate
                and right_has_predicate
                and (
                    connector in {"while", "whereas", "but"}
                    or left_signals.has_modality
                    or right_owner_has_finite_predicate
                    or inherited_modality is not None
                )
            )
        if not should_split:
            continue

        unresolved_bare_coordinate = False

        left_start = cursor + len(fragment.text[cursor : match.start()]) - len(
            fragment.text[cursor : match.start()].lstrip()
        )
        left_end = left_start + len(left_text)
        fragments.append(
            PropositionFragment(
                text=left_text,
                start=fragment.start + left_start,
                end=fragment.start + left_end,
                inherits_subject=inherited_modality is not None,
                inherited_modality=inherited_modality,
            )
        )
        inherited_modality = _coordinated_matrix_modality(
            connector=connector,
            left_signals=left_signals,
            right_text=right_text,
            inherited_modality=inherited_modality,
        )
        right_start = match.end()
        while right_start < len(fragment.text) and fragment.text[right_start].isspace():
            right_start += 1
        cursor = right_start

    tail = fragment.text[cursor:].strip()
    if not fragments or tail:
        tail_start = cursor + len(fragment.text[cursor:]) - len(fragment.text[cursor:].lstrip())
        tail_signals = _clause_signals(tail)
        inherits_subject = bool(
            inherited_modality is not None
            or (
                fragments
                and (
                    tail_signals.owner == "unspecified"
                    or _is_elliptical_preference(tail)
                )
                and tail_signals.has_modality
                and not _has_explicit_claim_subject(
                    tail,
                    antecedent_text=fragments[-1].text,
                )
            )
        )
        fragments.append(
            PropositionFragment(
                text=tail,
                start=fragment.start + tail_start,
                end=fragment.start + tail_start + len(tail),
                inherits_subject=inherits_subject,
                inherited_modality=inherited_modality,
            )
        )
    return fragments


def _coordinator_continues_governed_infinitive(
    connector: str,
    left_text: str,
    right_tokens: tuple[str, ...],
) -> bool:
    if connector not in {"and", "or"} or not right_tokens:
        return False
    if (
        connector == "or"
        and right_tokens[:3] == ("be", "willing", "to")
        and _clause_signals(left_text).has_modality
    ):
        return True
    if not _is_bare_requirement_predicate_token(right_tokens[0]):
        return False
    return bool(
        re.search(
            r"\b(?:ability|able|allowed|eligible|expected|permitted|required)\s+to"
            r"\s+[a-z][\w'-]*\s*$",
            left_text,
            re.IGNORECASE,
        )
    )


def _contrastive_connector_completes_preposed_condition(
    connector: str,
    left_text: str,
    right_text: str,
) -> bool:
    if connector != "but" or re.match(
        r"^\s*(?:if|unless|when|where)\b",
        left_text,
        re.IGNORECASE,
    ) is None:
        return False
    return bool(
        re.match(
            r"^\s*(?:you|the\s+(?:applicant|candidate))\s+"
            r"(?:are\s+required|must|need(?:s)?\s+to|require(?:s)?)\b",
            right_text,
            re.IGNORECASE,
        )
    )


def _coordinator_shares_delayed_degree_object(
    connector: str,
    left_text: str,
    right_text: str,
    right_coordinate_text: str,
) -> bool:
    if connector == "or":
        if (
            re.search(r"\bfrom\s+an?\s+accredited\s+college$", left_text, re.IGNORECASE)
            and re.match(r"^University\s+required\b", right_coordinate_text, re.IGNORECASE)
        ):
            return True
        return bool(
            re.search(r"\bshould\s+be\s+pursuing\s*$", left_text, re.IGNORECASE)
            and re.search(
                r"^have\s+(?:recently\s+)?completed\b.*\bdegree\b",
                right_text,
                re.IGNORECASE,
            )
        )
    if connector != "and" or not re.match(
        r"^(?:understand|demonstrate|possess)\b",
        right_coordinate_text,
        re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"\bshould\s+be\s+pursuing\s+or\s+have\s+(?:recently\s+)?"
            r"completed\b.*\bdegree\s*$",
            left_text,
            re.IGNORECASE,
        )
    )


def _coordinator_joins_nominal_before_serial_predicate(
    value: str,
    match: re.Match[str],
    *,
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    if connector != "and":
        return False
    if re.search(
        r"\b(?:using|with)\s+[A-Z][A-Za-z0-9.+#-]*\s*$",
        left_text,
    ) is None:
        return False
    if re.fullmatch(r"[A-Z][A-Za-z0-9.+#-]*", right_coordinate_text) is None:
        return False
    return bool(
        re.match(
            r"\s*[A-Z][A-Za-z0-9.+#-]*\s*,\s+and\s+"
            r"(?:demonstrate|have|possess|show)\b",
            value[match.end() :],
        )
    )


def _coordinator_joins_degree_field_coordinate(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    if connector not in {"and", "or"}:
        return False
    right_tokens = lexical_tokens(right_coordinate_text)
    if not right_tokens or _is_bare_requirement_predicate_token(right_tokens[0]):
        return False
    return bool(
        re.search(
            r"\b(?:degree|diploma)\s+in\s+[a-z][\w'-]*\s*$",
            left_text,
            re.IGNORECASE,
        )
    )


def _coordinator_joins_nominal_co_subjects(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    if connector not in {"and", "or"}:
        return False
    if re.search(
        r"\b(?:ability|experience|interest|knowledge|proficiency|skills?)\s*$",
        left_text,
        re.IGNORECASE,
    ) is None:
        return False
    return bool(
        re.fullmatch(
            r"(?:ability|experience|interest|knowledge|proficiency|skills?)\s+"
            r"(?:is|are)\s+(?:essential|mandatory|necessary|preferred|required)\.?",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_joins_output_nouns(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    if connector not in {"and", "or"}:
        return False
    if re.fullmatch(
        r"(?:documents?|presentations?|reports?)",
        right_coordinate_text,
        re.IGNORECASE,
    ) is None:
        return False
    return bool(
        re.search(
            r"\b(?:creat(?:e|ing)|develop(?:ing)?|prepar(?:e|ing))\s+"
            r"(?:documents?|presentations?|reports?)\s*$",
            left_text,
            re.IGNORECASE,
        )
    )


def _coordinator_joins_related_nominal_objects(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep paired nouns governed by the same qualification predicate together."""

    if connector != "and":
        return False
    return bool(
        re.search(r"\bunderlying\s+issues$", left_text, re.IGNORECASE)
        and re.match(
            r"^implications\s+of\s+decisions\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_continues_employer_alternative(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Do not split an employer from its wholly-owned-subsidiary alternative."""

    if connector != "or":
        return False
    return bool(
        re.search(r"\bwork\s+for\s+.+$", left_text, re.IGNORECASE)
        and re.match(
            r"^one\s+of\s+(?:the\s+)?(?:our|its)\s+wholly\s+owned\s+subsidiaries\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_continues_cross_border_eligibility(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep coordinated domestic work and cross-border travel eligibility intact."""

    if connector != "and":
        return False
    return bool(
        re.search(r"\beligible\s+to\s+work\s+in\s+.+$", left_text, re.IGNORECASE)
        and re.match(
            r"^able\s+to\s+enter\s+(?:the\s+)?(?:USA|United\s+States)\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_continues_team_collaboration(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep the peer-success predicate governed by collaboration ability."""

    if connector != "and":
        return False
    return bool(
        re.search(r"\bworkstreams\s+within\s+your\s+team$", left_text, re.IGNORECASE)
        and re.match(
            r"^contribute\s+to\s+your\s+peers?['’]?\s+success\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_continues_enrollment_return_plan(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep enrollment and the associated return-to-school condition together."""

    if connector != "and":
        return False
    return bool(
        re.search(r"\b(?:enrolled|enrollment)\s+in\s+.+\bprogram$", left_text, re.IGNORECASE)
        and re.match(
            r"^plan\s+to\s+return\s+to\s+school\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _comma_continues_eligibility_purpose(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep a criteria statement with its `to be eligible` purpose adjunct."""

    if connector != "comma":
        return False
    return bool(
        re.search(r"\b(?:criteria|requirements)$", left_text, re.IGNORECASE)
        and re.match(
            r"^to\s+be\s+eligible\s+to\s+(?:apply|work)\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _comma_continues_required_adjective(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep a required adjective with the noun phrase completed after a comma."""

    if connector == "comma":
        return bool(
            re.search(r"\bmust\s+be\s+(?:an?\s+)?[a-z-]+$", left_text, re.IGNORECASE)
            and re.match(
                r"^[a-z-]+\s+person\b",
                right_coordinate_text,
                re.IGNORECASE,
            )
        )
    if connector == "and":
        return bool(
            re.search(
                r"\bmust\s+be\s+(?:an?\s+)?[a-z-]+,\s*[a-z-]+\s+person\b",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^must\s+be\s+able\s+to\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _coordinator_continues_language_proficiency(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep `fluent or proficient in <language>` as one alternative expression."""

    if connector != "or":
        return False
    return bool(
        re.search(r"\bfluent$", left_text, re.IGNORECASE)
        and re.match(r"^proficient\s+in\s+", right_coordinate_text, re.IGNORECASE)
    )


def _coordinator_continues_controlled_goods_requirement(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep the controlled-goods and clearance requirements in one claim."""

    if connector == "comma":
        return bool(
            re.search(r"\bCanadian\s+Controlled\s+Goods$", left_text, re.IGNORECASE)
            and re.match(
                r"^government\s+security[- ]clearance\s+requirements\b",
                right_coordinate_text,
                re.IGNORECASE,
            )
        )
    if connector == "and":
        if (
            re.search(r"\bCanadian\s+Controlled\s+Goods$", left_text, re.IGNORECASE)
            and re.match(
                r"^government\s+security[- ]clearance\s+requirements$",
                right_coordinate_text,
                re.IGNORECASE,
            )
        ):
            return True
        return bool(
            re.search(
                r"\bCanadian\s+Controlled\s+Goods\s*(?:,|and)\s*"
                r"government\s+security[- ]clearance\s+requirements$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^must\s+be\s+eligible\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _coordinator_continues_enrolled_maintenance(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep enrollment, maintenance, and the delayed course-count object together."""

    if connector == "and":
        return bool(
            re.search(r"\b(?:been\s+)?enrolled\s+in$", left_text, re.IGNORECASE)
            and re.match(
                r"^maintain(?:ed)?(?:\s+(?:an?\s+)?minimum\s+of\b.*)?$",
                right_coordinate_text,
                re.IGNORECASE,
            )
        )
    if connector == "comma":
        return bool(
            re.search(
                r"\b(?:been\s+)?enrolled\s+in\s*,?\s*and\s+maintain(?:ed)?$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^(?:an?\s+)?minimum\s+of\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _coordinator_continues_program_eligibility(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep program enrollment and its internship-eligibility predicate together."""

    return bool(
        connector == "and"
        and re.search(r"\b(?:certificate|bachelor\s*['’]?s)\s+program\b[^.!?]*$", left_text, re.IGNORECASE)
        and re.match(
            r"^be\s+eligible\s+(?:for|to)\b",
            right_coordinate_text,
            re.IGNORECASE,
        )
    )


def _coordinator_continues_ability_predicate_sequence(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep a governed analyze-identify-ensure ability sequence together."""

    if connector == "comma":
        return bool(
            re.search(r"\bStrong\s+ability\s+to\s+analy[sz]e\b[^.!?]*$", left_text, re.IGNORECASE)
            and re.match(r"^identify\b", right_coordinate_text, re.IGNORECASE)
        )
    if connector == "and":
        return bool(
            re.search(
                r"\bStrong\s+ability\s+to\s+analy[sz]e\b[^.!?]*,\s*identify\b[^.!?]*$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^ensure\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _comma_continues_degree_field_coordinate(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep comma-delimited degree fields under one degree governor."""

    if connector == "comma":
        return bool(
            re.search(r"\bbachelor\s*['’]?s\s+degree\s+in\s+business(?:\s*\([^)]*\))?$", left_text, re.IGNORECASE)
            and re.match(r"^(?:math|mathematics)$", right_coordinate_text, re.IGNORECASE)
        )
    if connector == "or":
        return bool(
            re.search(
                r"\bbachelor\s*['’]?s\s+degree\s+in\s+business(?:\s*\([^)]*\))?\s*,\s*(?:math|mathematics)$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^(?:an?\s+)?technical\s+field\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _comma_continues_credential_program(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep DEC/AEC/certificate alternatives with the following degree program."""

    return bool(
        connector == "comma"
        and re.search(
            r"\b(?:DEC|AEC|certificate)(?:\s*,\s*(?:DEC|AEC|certificate))*$",
            left_text,
            re.IGNORECASE,
        )
        and re.match(r"^Bachelor\s*['’]?s\s+program\b", right_coordinate_text, re.IGNORECASE)
    )


def _coordinator_continues_program_field(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep `Computer Science or related program` under one program phrase."""

    return bool(
        connector == "or"
        and re.search(
            r"\bBachelor\s*['’]?s\s+program\s+in\s+Computer\s+Science$",
            left_text,
            re.IGNORECASE,
        )
        and re.match(r"^related\s+program\b", right_coordinate_text, re.IGNORECASE)
    )


def _comma_continues_candidate_skill_list(
    connector: str,
    left_text: str,
    right_coordinate_text: str,
) -> bool:
    """Keep comma-delimited applicant skill nouns under `should have`."""

    if connector == "comma":
        return bool(
            re.search(
                r"\bApplicants?\s+should\s+have\s+strong\s+[^.!?]*\bcommunication$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^teamwork\b", right_coordinate_text, re.IGNORECASE)
        )
    if connector == "and":
        if (
            re.search(
                r"\bApplicants?\s+should\s+have\s+strong\s+[^.!?]*\bindependent[- ]working\s+skills$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(
                r"^be\s+pursuing\s+a\s+degree\b",
                right_coordinate_text,
                re.IGNORECASE,
            )
        ):
            return True
        return bool(
            re.search(
                r"\bApplicants?\s+should\s+have\s+strong\s+[^.!?]*\bcommunication\s*,\s*teamwork$",
                left_text,
                re.IGNORECASE,
            )
            and re.match(r"^independent[- ]working\s+skills\b", right_coordinate_text, re.IGNORECASE)
        )
    return False


def _coordinator_precedes_embedded_explicit_subject(
    connector: str,
    left_text: str,
    right_text: str,
) -> bool:
    if connector not in {"and", "or"}:
        return False
    left_signals = _clause_signals(left_text)
    if left_signals.has_modality or left_signals.owner != "unspecified":
        return False
    subject = re.search(
        r"\b(?:you|the\s+(?:applicant|candidate))\s+"
        r"(?:are|can|could|have|must|need|should|will|would)\b",
        right_text,
        re.IGNORECASE,
    )
    if subject is None or subject.start() == 0:
        return False
    nominal_prefix = lexical_tokens(right_text[: subject.start()])
    return bool(
        1 <= len(nominal_prefix) <= 6
        and not _is_bare_requirement_predicate_token(nominal_prefix[0])
    )


def _is_right_node_shared_prepositional_coordinate(
    left_text: str,
    right_text: str,
) -> bool:
    left_tokens = lexical_tokens(left_text)
    right_tokens = lexical_tokens(right_text)
    return bool(
        left_tokens
        and right_tokens
        and left_tokens[-1] in _PREPOSITION_HEADS
        and right_tokens[0]
        in {
            *_PREPOSITION_HEADS,
            *_QUALIFICATION_COMPLEMENT_HEADS,
            *_NOMINAL_COORDINATE_HEADS,
        }
    )


def _coordinate_is_inside_finite_explanation(
    text: str,
    separator_start: int,
    right_text: str,
) -> bool:
    if _owner_signal_has_finite_predicate(_clause_signals(right_text)):
        return False
    prefix = text[:separator_start]
    delimiters = tuple(re.finditer(r"(?:\s[-–—]\s|:)", prefix))
    if not delimiters:
        return False
    explanation = prefix[delimiters[-1].end() :].strip()
    explanation_signals = _clause_signals(explanation)
    return bool(
        explanation
        and not explanation_signals.modality_cues
        and _owner_signal_has_finite_predicate(explanation_signals)
    )


def _comma_closes_condition_adjunct(value: str, comma_start: int) -> bool:
    return bool(
        re.search(
            r"\b(?:where|when|if|unless)\b[^,;:]*$",
            value[:comma_start],
            re.IGNORECASE,
        )
    )


def _owner_signal_has_finite_predicate(
    signals: ClauseSignals,
) -> bool:
    if signals.owner == "unspecified":
        return False
    owner, owner_end = _clause_owner(signals.tokens)
    if owner == "unspecified":
        return False
    predicate = signals.tokens[owner_end:]
    while predicate and predicate[0] in {
        "also",
        "always",
        "generally",
        "often",
        "typically",
        "usually",
    }:
        predicate = predicate[1:]
    if not predicate:
        return False
    head = predicate[0]
    if _UNSECTIONED_FINITE_PREDICATE_PATTERN.fullmatch(head) is not None:
        return True
    if _is_bare_requirement_predicate_token(head):
        return True
    return bool(
        head.endswith("s")
        and len(head) > 1
        and _is_bare_requirement_predicate_token(head[:-1])
    )


def _embedded_nominals_share_finite_predicate(
    left_text: str,
    right_text: str,
) -> bool:
    complementizers = list(re.finditer(r"\bthat\b", left_text, re.IGNORECASE))
    if not complementizers:
        return False
    embedded_left = left_text[complementizers[-1].end() :].strip()
    embedded_left_tokens = lexical_tokens(embedded_left)
    if (
        not _tokens_form_nominal_member(embedded_left_tokens)
        or _UNSECTIONED_FINITE_PREDICATE_PATTERN.search(embedded_left) is not None
    ):
        return False
    right_predicate = _UNSECTIONED_FINITE_PREDICATE_PATTERN.search(right_text)
    if right_predicate is None:
        return False
    return _tokens_form_nominal_member(
        lexical_tokens(right_text[: right_predicate.start()])
    )


def _tokens_form_nominal_member(tokens: tuple[str, ...]) -> bool:
    return bool(
        tokens
        and (len(tokens) == 1 or tokens[-1] in _NOMINAL_COORDINATE_HEADS)
    )


def _is_elliptical_preference(value: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:considered\s+)?(?:an?\s+)?asset\b",
            value,
            re.IGNORECASE,
        )
    )


def _normalized_proposition_text(
    fragment: TextFragment,
    proposition: PropositionFragment,
) -> str | None:
    is_full_fragment = (
        proposition.start == fragment.start
        and proposition.end == fragment.end
        and not proposition.inherits_subject
    )
    display = _single_line_role_text(proposition.text)
    if is_full_fragment:
        return display if display != proposition.text.strip() else None
    if proposition.inherits_subject:
        full_cues = _modality_cues(fragment.text)
        local_cues = _modality_cues(proposition.text)
        prefix = ""
        if proposition.inherited_modality is not None and full_cues:
            prefix = _single_line_role_text(fragment.text[: full_cues[0].cue_end])
        elif local_cues and full_cues:
            prefix = _single_line_role_text(fragment.text[: full_cues[0].cue_start])
            display_tokens = lexical_tokens(display)
            if display_tokens and display_tokens[0] in {
                *_SUBJECT_TRAILING_AUXILIARIES,
                "am",
                "did",
                "do",
                "does",
                "had",
                "has",
                "have",
            }:
                prefix = re.sub(
                    r"\b(?:am|are|be|been|being|can|could|did|do|does|had|has|"
                    r"have|is|may|might|must|shall|should|was|were|will|would)\s*$",
                    "",
                    prefix,
                    flags=re.IGNORECASE,
                ).strip()
        if prefix:
            display = re.sub(
                r"^(?:it|they|these|those|both)\s+(?:is|are|was|were)\s+",
                "",
                display,
                flags=re.IGNORECASE,
            )
            display = f"{prefix} {display}"
    display = display.strip().rstrip(",;:")
    if display and display[-1] not in ".?!":
        display = f"{display}."
    return display


def _coordinated_matrix_modality(
    *,
    connector: str,
    left_signals: ClauseSignals,
    right_text: str,
    inherited_modality: DistributedModality | None,
) -> DistributedModality | None:
    """Carry a shared matrix modal only into a coordinated predicate/complement.

    A bare control predicate such as `ensure` remains governed by `must` in
    `must provide credentials and ensure ...`. A finite or explicitly modalized
    conjunct (`and are permitted`, `and may use`) starts a new clause instead.
    """

    if connector not in {"and", "or", "comma"} or not _is_coordinated_predicate_complement(
        right_text
    ):
        return None
    if inherited_modality is not None:
        return inherited_modality
    if left_signals.explicit_required or left_signals.soft_required:
        return "mandatory"
    if left_signals.expected:
        return "expected"
    if left_signals.preferred:
        return "preferred"
    return None


def _is_coordinated_predicate_complement(
    value: str,
    *,
    antecedent_text: str | None = None,
    antecedent_inherits_modality: bool = False,
    antecedent_closes_embedded_scope: bool = False,
) -> bool:
    tokens = lexical_tokens(value)
    return bool(
        tokens
        and (
            tokens[0] == "that"
            or (
                _is_bare_requirement_predicate_token(tokens[0])
                and not _is_nominal_coordinate_under_shared_governor(
                    tokens,
                    antecedent_text=antecedent_text,
                    antecedent_inherits_modality=antecedent_inherits_modality,
                    antecedent_closes_embedded_scope=(
                        antecedent_closes_embedded_scope
                    ),
                )
                and (
                    len(tokens) > 1
                    or tokens[0] in {"communicate", "travel", "work"}
                )
            )
        )
    )


def _is_nominal_coordinate_under_shared_governor(
    tokens: tuple[str, ...],
    *,
    antecedent_text: str | None,
    antecedent_inherits_modality: bool = False,
    antecedent_closes_embedded_scope: bool = False,
) -> bool:
    if (
        not antecedent_text
        or not _looks_like_nominal_coordinate(tokens)
    ):
        return False
    return bool(
        _active_matrix_predicate_head(
            antecedent_text,
            inherits_modality=antecedent_inherits_modality,
            closes_embedded_scope=antecedent_closes_embedded_scope,
        )
        in _NOMINAL_LIST_GOVERNOR_HEADS
    )


def _looks_like_nominal_coordinate(tokens: tuple[str, ...]) -> bool:
    if not tokens or tokens[0] not in _ATTRIBUTIVE_PREDICATE_HOMONYMS:
        return False
    if len(tokens) == 1:
        return True
    postmodifier_start = next(
        (
            index
            for index, token in enumerate(tokens[1:], start=1)
            if token in _NOMINAL_COORDINATE_POSTMODIFIER_HEADS
        ),
        len(tokens),
    )
    core_end = postmodifier_start
    while core_end > 1 and _is_nominal_adverb_modifier(tokens[core_end - 1]):
        core_end -= 1
    return bool(
        core_end > 1
        and tokens[core_end - 1] in _NOMINAL_COORDINATE_HEADS
        and _tokens_form_nominal_postmodifier(tokens[core_end:])
    )


def _is_nominal_adverb_modifier(token: str) -> bool:
    return token in {"more", "most", "quite", "very"} or token.endswith("ly")


def _tokens_form_nominal_postmodifier(tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return True
    if tokens[0] in _NOMINAL_COORDINATE_POSTMODIFIER_HEADS:
        return True
    return all(_is_nominal_adverb_modifier(token) for token in tokens)


def _active_matrix_predicate_head(
    value: str,
    *,
    inherits_modality: bool,
    closes_embedded_scope: bool,
) -> str | None:
    modality_cues = _modality_cues(value)
    if modality_cues:
        predicate_tokens = lexical_tokens(
            value[max(cue.cue_end for cue in modality_cues) :]
        )
    elif inherits_modality:
        predicate_tokens = lexical_tokens(value)
    else:
        return None
    predicate_indexes = [
        index
        for index, token in enumerate(predicate_tokens)
        if _is_bare_requirement_predicate_token(token)
    ]
    if not predicate_indexes:
        return None
    first_index = predicate_indexes[0]
    if not closes_embedded_scope:
        marker_index = max(
            (
                index
                for index, token in enumerate(predicate_tokens)
                if index > first_index
                and token in _EMBEDDED_PREDICATE_SCOPE_MARKERS
                and any(
                    predicate_index > index
                    for predicate_index in predicate_indexes
                )
            ),
            default=None,
        )
        if marker_index is not None:
            first_index = next(
                index for index in predicate_indexes if index > marker_index
            )
    first_head = predicate_tokens[first_index]
    if first_head != "be":
        return first_head
    for next_index in (
        index for index in predicate_indexes if index > first_index
    ):
        if all(
            token in _MATRIX_PREDICATE_CHAIN_TOKENS
            for token in predicate_tokens[first_index + 1 : next_index]
        ):
            return predicate_tokens[next_index]
        break
    return first_head


def _has_explicit_claim_subject(
    value: str,
    *,
    antecedent_text: str | None = None,
) -> bool:
    tokens = lexical_tokens(value)
    if not tokens:
        return False
    if tokens[:2] in {("a", "asset"), ("an", "asset")}:
        return False
    if _clause_owner(tokens)[0] != "unspecified":
        return True
    first_cue = min((cue.cue_start for cue in _modality_cues(value)), default=len(value))
    subject_text = value[:first_cue]
    subject_tokens = list(lexical_tokens(subject_text))
    while subject_tokens and subject_tokens[-1] in _SUBJECT_TRAILING_AUXILIARIES:
        subject_tokens.pop()
    if subject_tokens in (["a"], ["an"]):
        return False
    if len(subject_tokens) == 1 and subject_tokens[0] in _RESUMPTIVE_CLAIM_SUBJECTS:
        # A bare resumptive pronoun in a coordinated contrast normally resumes
        # the preceding qualification (`Python and Java ... though they are
        # preferred`). Applicant-denoting antecedents are different: in
        # `Candidates ... though they ...`, person/number agreement resolves
        # `they` to the candidates rather than to a capability complement. A
        # following that-clause instead marks extraposition.
        cue_end = min(
            (cue.cue_end for cue in _modality_cues(value)),
            default=len(value),
        )
        complement_tokens = lexical_tokens(value[cue_end:])
        if complement_tokens and complement_tokens[0] == "that":
            return True
        if (
            subject_tokens[0] in {"both", "they", "these", "those"}
            and antecedent_text is not None
            and _clause_owner(lexical_tokens(antecedent_text))[0] == "applicant"
        ):
            return True
        return False
    return bool(subject_tokens)


def _is_self_contained_inherited_predicate(
    value: str,
    modality_cues: tuple[ModalityCue, ...],
) -> bool:
    if any(start == 0 for start, _end in _positive_permission_spans(value)):
        return True
    if not modality_cues:
        return False
    predicate_tokens = lexical_tokens(value[max(cue.cue_end for cue in modality_cues) :])
    if not predicate_tokens:
        return False
    if predicate_tokens[0] == "to":
        return len(predicate_tokens) > 1
    return predicate_tokens[0] not in _PREPOSITION_HEADS


def _claim_modality(
    signals: ClauseSignals,
    *,
    section: RoleSection,
    kind: str,
    text: str,
) -> Literal[
    "mandatory",
    "expected",
    "preferred",
    "optional",
    "descriptive",
    "prohibited",
    "unknown",
]:
    if kind in {"company_context", "environment"}:
        return "descriptive"
    if (
        kind in {"qualification", "eligibility"}
        and re.match(r"^The\s+international\s+student\b", text, re.IGNORECASE)
        and re.search(r"\bhas\s+a\s+valid\s+Study\s+Permit\b", text, re.IGNORECASE)
    ):
        # A quoted permit endorsement such as "May accept employment" is
        # document text, not optionality attached to the candidate condition.
        return "mandatory"
    if signals.non_requirement:
        return "optional"
    if academic_completion_condition(text) is not None and kind in {"qualification", "eligibility"}:
        if signals.preferred or section == "preferred_qualifications":
            return "preferred"
        if section == "qualifications":
            return "mandatory"
    if signals.negative_modal_force == "epistemic":
        if kind in {"qualification", "eligibility"}:
            return "optional"
        return "descriptive"
    if signals.negative_modal_force == "prohibited":
        if kind == "responsibility":
            return "prohibited"
        return "mandatory"
    if signals.explicit_required:
        return "mandatory"
    if section == "preferred_qualifications":
        return "preferred"
    if signals.expected:
        return "expected"
    if signals.preferred:
        return "preferred"
    if signals.soft_required:
        return "mandatory"
    if kind in {"qualification", "eligibility"} and signals.tokens[:2] == ("currently", "enrolled"):
        return "mandatory"
    if kind == "responsibility":
        return "descriptive"
    if section == "qualifications":
        return "mandatory"
    if section in {"responsibilities", "capabilities"}:
        return "descriptive"
    return "unknown"


def _claim_kind(
    text: str,
    *,
    signals: ClauseSignals,
    section: RoleSection,
    applicant_scope: bool,
) -> Literal[
    "responsibility",
    "qualification",
    "eligibility",
    "environment",
    "company_context",
]:
    if section == "capabilities":
        return "environment"
    if re.match(
        r"^\s*(?:office|hybrid|remote|workplace)\s+environment\b",
        text,
        re.IGNORECASE,
    ):
        return "environment"
    if _is_role_environment_statement(text, signals.tokens):
        return "environment"
    if re.match(
        r"^\s*The\s+(?:internship|placement|term)\s+runs\b",
        text,
        re.IGNORECASE,
    ):
        return "company_context"
    requirement_kind = "eligibility" if _is_eligibility_claim(text) else "qualification"
    if re.match(r"^\s*Currently\s+enrolled\b", text, re.IGNORECASE):
        return requirement_kind
    if section in {"qualifications", "preferred_qualifications"} and re.match(
        r"^\s*Team\s+collaboration\b",
        text,
        re.IGNORECASE,
    ):
        return requirement_kind
    if section == "responsibilities" and (
        re.match(
            r"^\s*Have\s+a\s+thorough\s+understanding\b"
            r"[^.!?;\r\n]{0,260}\b(?:maintain|deliver|provide|support)\b",
            text,
            re.IGNORECASE,
        )
        or re.match(r"^\s*Field\s+work\b", text, re.IGNORECASE)
        or re.search(
            r"\b(?:intern|co-op|student|analyst|role)\b"
            r"[^.!?;\r\n]{0,100}\bwill\s+perform\s+a\s+variety\s+of\s+activities\b"
            r"[^.!?;\r\n]{0,80}\bmay\s+include\b",
            text,
            re.IGNORECASE,
        )
        or re.match(
            r"^\s*(?:Your\s+)?typical\s+responsibilities\s+may\s+include\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"\bMust\s+(?:understand|use|wear|work)\b",
            text,
            re.IGNORECASE,
        )
    ):
        return "responsibility"
    if re.match(
        r"^\s*This\s+role\s+will\s+require\s+you\s+to\s+actively\s+"
        r"(?:own|develop|build|maintain|support)\b",
        text,
        re.IGNORECASE,
    ):
        return "responsibility"
    if signals.owner == "role":
        if _role_subject_imposes_applicant_requirement(signals.tokens):
            return requirement_kind
        if _has_responsibility_predicate(signals.tokens):
            return "responsibility"
        return "company_context"
    if (
        section == "role_overview"
        and signals.tokens[:1] == ("join",)
        and any(
            token in {"support", "supporting", "work", "working"}
            for token in signals.tokens
        )
    ):
        return "responsibility"
    if signals.owner == "company":
        return "company_context"
    if re.match(
        r"^\s*The\s+[^.!?;]{0,100}(?:Student\s+Assistant|role|position)\s+"
        r"(?:assists?|helps?|provides?|supports?)\b",
        text,
        re.IGNORECASE,
    ):
        return "company_context"
    if re.match(
        r"^\s*Do\s+you\s+have\s+(?:an?\s+)?(?:interest|passion)\s+in\b",
        text,
        re.IGNORECASE,
    ):
        return "company_context"
    if re.match(
        r"^\s*(?:where\s+our\s+products\s+need\s+to\s+go\s+next|"
        r"Relocation\s+assistance\s+is\s+available)\s*[.!?]?\s*$",
        text,
        re.IGNORECASE,
    ):
        return "company_context"
    if re.match(
        r"^\s*This\s+is\s+a\s+paid\s*,\s*\d+[- ]week\s+co-op\s+placement\b",
        text,
        re.IGNORECASE,
    ):
        return "company_context"
    if section == "role_overview" and re.match(
        r"^\s*[A-Z][A-Za-z0-9&/ -]{1,60}\s+-\s+"
        r"(?:Assist|Build|Create|Design|Develop|Perform|Prepare|Provide|Support|Work)\b",
        text,
        re.IGNORECASE,
    ):
        return "responsibility"
    if (
        signals.applicant_predicate == "hard_requirement"
        and _is_candidate_requirement_introducer(signals.tokens)
    ):
        return requirement_kind
    if section in {"qualifications", "preferred_qualifications"}:
        return requirement_kind
    if section == "responsibilities":
        if re.match(
            r"^\s*Following\b[^.!?;\r\n]{0,180}\b"
            r"(?:candidates?|interns?|students?)\s+may\s*:\s*"
            r"(?:Analyze|Assist|Build|Collect|Create|Design|Develop|Document|"
            r"Implement|Maintain|Manage|Perform|Prepare|Provide|Review|Support|"
            r"Test|Update|Work)\b",
            text,
            re.IGNORECASE,
        ):
            return "responsibility"
        if _has_responsibility_predicate(signals.tokens):
            return "responsibility"
        if requirement_kind == "eligibility":
            return requirement_kind
        if signals.applicant_predicate == "qualification":
            return requirement_kind
        if signals.modality_cues:
            return requirement_kind
        return "responsibility"
    if section == "role_overview":
        if text.rstrip().endswith("?") and re.match(
            r"^\s*(?:are|can|could|do|does|have|would)\s+you\b",
            text,
            re.IGNORECASE,
        ):
            return "company_context"
        if _has_responsibility_predicate(signals.tokens):
            return "responsibility"
        if signals.has_modality or signals.applicant_predicate in {
            "hard_requirement",
            "qualification",
        }:
            return requirement_kind
        return "company_context"
    if section == "unsectioned":
        if (
            signals.owner == "applicant"
            and _is_application_process_constraint(signals.tokens)
        ):
            return requirement_kind
        if (
            signals.applicant_predicate == "hard_requirement"
            and requirement_kind == "eligibility"
        ):
            return requirement_kind
        if (
            requirement_kind == "eligibility"
            and signals.owner == "applicant"
            and signals.has_modality
        ):
            return requirement_kind
        if _has_responsibility_predicate(signals.tokens) or (
            signals.tokens and signals.tokens[0] in _UNSECTIONED_RESPONSIBILITY_HEADS
        ):
            return "responsibility"
        if (
            signals.owner in {"applicant", "role"}
            or signals.has_modality
            or signals.applicant_predicate in {"hard_requirement", "qualification"}
            or any(token in _QUALIFICATION_COMPLEMENT_HEADS for token in signals.tokens)
        ):
            return requirement_kind
        return "company_context"
    if section == "company":
        if signals.owner == "company":
            return "company_context"
        if signals.owner == "applicant":
            if signals.applicant_predicate == "descriptive" and not any(
                (
                    signals.explicit_required,
                    signals.non_requirement,
                )
            ):
                return "company_context"
            if signals.applicant_predicate in {"hard_requirement", "qualification"}:
                return requirement_kind
            if signals.has_modality:
                return requirement_kind
            return "company_context"
        if signals.explicit_required and signals.applicant_predicate == "hard_requirement":
            return requirement_kind
        if applicant_scope and signals.has_modality:
            return requirement_kind
        return "company_context"
    return "company_context"


def _is_application_process_constraint(tokens: tuple[str, ...]) -> bool:
    return bool(
        _APPLICATION_PROCESS_ACTIONS.intersection(tokens)
        and _contains_any_phrase(tokens, _APPLICATION_PROCESS_OBJECT_PHRASES)
    )


def _is_candidate_requirement_introducer(tokens: tuple[str, ...]) -> bool:
    return any(
        tokens[index] == "meet"
        and any(
            complement in {"criteria", "qualification", "qualifications", "requirement", "requirements"}
            for complement in tokens[index + 1 : index + 7]
        )
        for index in range(len(tokens))
    )


def _role_subject_imposes_applicant_requirement(tokens: tuple[str, ...]) -> bool:
    owner, owner_end = _clause_owner(tokens)
    if owner != "role":
        return False
    return bool(
        _matching_prefix_width(
            tokens[owner_end:],
            (
                ("calls", "for"),
                ("demands",),
                ("need",),
                ("needs",),
                ("require",),
                ("requires",),
            ),
        )
    )


def _has_responsibility_predicate(tokens: tuple[str, ...]) -> bool:
    owner, owner_end = _clause_owner(tokens)
    predicate = tokens[owner_end:]
    if predicate[:1] == ("ll",):
        predicate = ("will", *predicate[1:])
    negative_auxiliary_width = _matching_prefix_width(
        predicate,
        _NEGATIVE_ACTION_AUXILIARY_PREFIXES,
    )
    auxiliary_width = negative_auxiliary_width or _matching_prefix_width(
        predicate,
        (
            ("will", "be", "expected", "to"),
            ("would", "be", "expected", "to"),
            ("are", "expected", "to"),
            ("is", "expected", "to"),
            ("be", "expected", "to"),
            ("expected", "to"),
            ("need", "to"),
            ("needs", "to"),
            ("have", "to"),
            ("has", "to"),
            ("will",),
            ("would",),
            ("can",),
            ("could",),
            ("may",),
            ("might",),
            ("must",),
            ("shall",),
            ("should",),
        ),
    )
    predicate = predicate[auxiliary_width:]
    if predicate[:1] in {("always",), ("typically",), ("usually",)}:
        predicate = predicate[1:]
    negated = bool(negative_auxiliary_width) or predicate[:1] == ("not",)
    if predicate[:1] == ("not",):
        predicate = predicate[1:]
    if not predicate:
        return False
    if owner == "applicant" and predicate[:1] == ("to",):
        predicate = predicate[1:]
        if not predicate:
            return False
    if _role_predicate_describes_property(owner, predicate):
        return False
    if predicate[:2] == ("ad", "hoc") and any(
        token in {"analysis", "project", "projects", "reporting", "support"}
        for token in predicate[2:7]
    ):
        return True
    if predicate[0] in _UNSECTIONED_RESPONSIBILITY_HEADS:
        return True
    if not auxiliary_width:
        return False
    non_action_width = _matching_prefix_width(
        predicate,
        (
            *_POSSESSION_PREDICATE_PREFIXES,
            ("be",),
        ),
    )
    if non_action_width:
        return False
    if negated and owner == "applicant" and predicate[0] in {
        "need",
        "needs",
        "require",
        "requires",
    }:
        return False
    return True


def _role_predicate_describes_property(
    owner: ClauseOwner,
    predicate: tuple[str, ...],
) -> bool:
    if owner != "role" or not predicate:
        return False
    if predicate[0] in _ROLE_PROPERTY_HEADS:
        return True
    if predicate[0] not in _ROLE_CHANGE_OF_STATE_HEADS:
        return False
    complement = predicate[1:]
    return not _role_change_has_direct_object(complement)


def _role_change_has_direct_object(complement: tuple[str, ...]) -> bool:
    if not complement:
        return False
    head = complement[0]
    if head in _PREPOSITION_HEADS:
        return False
    tail = complement[1:]
    if head in _DEGREE_MODIFIER_HEADS:
        if not tail:
            return False
        modified_head = tail[0]
        return not (
            modified_head in _PREPOSITION_HEADS
            or modified_head in _ROLE_CHANGE_PROPERTY_COMPLEMENT_HEADS
            or modified_head in _ROLE_CHANGE_PROPERTY_LINK_HEADS
            or modified_head.endswith("ly")
        )
    if head in _ROLE_CHANGE_PROPERTY_COMPLEMENT_HEADS:
        return bool(tail) and tail[0] not in {
            *_PREPOSITION_HEADS,
            *_ROLE_CHANGE_PROPERTY_LINK_HEADS,
        }
    # Change-of-state verbs license manner adjuncts (`grow organically`) in
    # addition to transitive noun phrases (`grow revenue`).
    if head.endswith("ly"):
        return False
    return True


def _is_eligibility_claim(value: str) -> bool:
    if academic_completion_condition(value) is not None:
        return True
    tokens = lexical_tokens(value)
    employment_condition = any(
        token in {"accept", "arrange", "complete", "undergo"}
        and _has_applicant_employment_condition(tokens[index + 1 :])
        for index, token in enumerate(tokens)
    )
    return employment_condition or _is_disqualifying_negative_complement(tokens) or bool(
        re.search(
            r"\b(?:authori[sz]ed\s+to\s+work|work\s+authori[sz]ation|legally\s+eligible|"
            r"driver'?s?\s+licen[cs]e|available\s+(?:for|to|on|during)|willing\s+to\s+travel|"
            r"able\s+to\s+lift|security\s+clearance|return\s+to\s+(?:school|studies)|"
            r"reside\s+(?:in|within)|work\s+within\s+(?:(?:their|your)\s+)?"
            r"(?:current\s+)?jurisdiction)\b",
            value,
            re.IGNORECASE,
        )
    )


def _claim_owner(
    owner: ClauseOwner,
    kind: str,
) -> Literal["applicant", "role", "employer", "team", "unknown"]:
    if owner == "applicant":
        return "applicant"
    if owner == "company":
        return "employer"
    if owner == "role":
        return "role"
    if kind == "responsibility":
        return "role"
    if kind == "environment":
        return "team"
    if kind == "company_context":
        return "employer"
    return "unknown"


def _claim_polarity(text: str, modality: str) -> Literal["positive", "negative"]:
    if re.search(
        r"\bmust\s+be\s+legally\s+authorized\s+to\s+work\b"
        r"[^.!?;\r\n]{0,160}\bwithout\s+requiring\b"
        r"[^.!?;\r\n]{0,80}\bsponsorship\b",
        text,
        re.IGNORECASE,
    ):
        return "positive"
    polarity_text = _matrix_polarity_text(text)
    tokens = lexical_tokens(polarity_text)
    privative_polarity = _privative_polarity(tokens, source_text=polarity_text)
    if privative_polarity is not None:
        return privative_polarity
    if any(
        not _is_focus_negation(polarity_text, match)
        and not _negation_is_inside_embedded_clause(polarity_text, match)
        for pattern in _NEGATIVE_POLARITY_PATTERNS
        for match in pattern.finditer(polarity_text)
    ):
        return "negative"
    if _has_negative_nominal_determiner(tokens):
        return "negative"
    if modality == "optional" and re.search(
        r"\b(?:no|not|without|need\s+not)\b",
        polarity_text,
        re.I,
    ):
        return "negative"
    return "positive"


def _matrix_polarity_text(text: str) -> str:
    """Exclude a concessive subordinate clause from matrix-claim polarity."""

    marker = re.search(
        r"\b(?:even\s+(?:if|though)|regardless\s+of\s+whether)\b",
        text,
        re.IGNORECASE,
    )
    if marker is None:
        return text
    matrix_prefix = text[: marker.start()].rstrip(" ,;:")
    if matrix_prefix:
        return matrix_prefix
    matrix_suffix = re.search(r"[,;]\s*(?P<matrix>\S.*)$", text[marker.end() :])
    if matrix_suffix is not None:
        return matrix_suffix.group("matrix")
    return text


def _has_negative_nominal_determiner(tokens: tuple[str, ...]) -> bool:
    for index, token in enumerate(tokens):
        if token != "no":
            continue
        if _matching_prefix_width(tokens[index + 1 :], _NON_NEGATING_NO_PREFIXES):
            continue
        return True
    return False


def _privative_polarity(
    tokens: tuple[str, ...],
    *,
    source_text: str,
) -> Literal["positive", "negative"] | None:
    """Resolve lexical and productive morphological absence predicates."""

    free_compound_starts = _privative_free_compound_starts(source_text)
    for index in range(len(tokens)):
        if index not in free_compound_starts and not _matching_prefix_width(
            tokens[index:],
            _PRIVATIVE_PREDICATES,
        ):
            continue
        return "positive" if _predicate_is_locally_negated(tokens, index) else "negative"
    return None


def _privative_free_compound_starts(source_text: str) -> frozenset[int]:
    """Return noun starts for surface `X-free` derivational adjectives.

    The written compound boundary distinguishes productive privative adjectives
    (`drug-free`) from predicative `free` with an infinitival complement
    (`free to travel`).
    """

    token_matches = list(_TOKEN.finditer(source_text))
    starts: set[int] = set()
    for index, (left, right) in enumerate(zip(token_matches, token_matches[1:])):
        if right.group(0).casefold() != "free":
            continue
        separator = source_text[left.end() : right.start()]
        if separator in _PRIVATIVE_COMPOUND_HYPHENS:
            starts.add(index)
    return frozenset(starts)


def _predicate_is_locally_negated(tokens: tuple[str, ...], predicate_start: int) -> bool:
    cursor = predicate_start - 1
    while cursor >= 0 and tokens[cursor] in _PRIVATIVE_NEGATION_INTERVENERS:
        cursor -= 1
    return cursor >= 0 and tokens[cursor] in {"no", "not"}


def _claim_condition(
    text: str,
    cues: tuple[ModalityCue, ...],
) -> tuple[str | None, ConditionType | None]:
    completion = academic_completion_condition(text)
    if completion is not None:
        return completion.text.rstrip(".,;:"), "temporal"
    inverted_should = _PREPOSED_INVERTED_SHOULD_CONDITION_PATTERN.match(text)
    if inverted_should is not None:
        condition = inverted_should.group("condition").strip().rstrip(".,;:")
        return (condition, "contingency") if condition else (None, None)
    preposed = _PREPOSED_CONDITION_PATTERN.match(text)
    if preposed is not None:
        return _classified_condition(
            preposed.group("condition"),
            allow_proper_location=False,
        )
    if not cues:
        return None, None
    cue_start = cues[0].cue_start
    subject_prefix = re.sub(
        r"\b(?:am|are|is|was|were|be|been|being)\s*$",
        "",
        text[:cue_start].strip(),
        flags=re.IGNORECASE,
    ).strip()
    embedded = _EMBEDDED_APPLICANT_CONDITION_PATTERN.match(subject_prefix)
    if embedded is not None:
        condition = embedded.group("condition").strip().rstrip(".,;:")
        return (condition, "applicant_subset") if condition else (None, None)
    cue_end = cues[-1].cue_end
    tail = text[cue_end:]
    for match in _TAIL_CONDITION_START_PATTERN.finditer(tail):
        predicate_prefix_tokens = lexical_tokens(tail[: match.start()])
        has_geographic_governor = any(
            token in _CONDITION_GEOGRAPHIC_GOVERNORS
            for token in predicate_prefix_tokens
        )
        condition = _classified_condition(
            tail[match.start() :],
            allow_proper_location=(
                has_geographic_governor
                and not _condition_is_capability_complement(
                    subject_tokens=lexical_tokens(subject_prefix),
                    predicate_prefix_tokens=predicate_prefix_tokens,
                )
            ),
            allow_unanchored_coordinated_location=has_geographic_governor,
        )
        if condition[0] is not None:
            return condition
    return None, None


def _classified_condition(
    value: str,
    *,
    allow_proper_location: bool = False,
    allow_unanchored_coordinated_location: bool = False,
) -> tuple[str | None, ConditionType | None]:
    condition = value.strip().rstrip(".,;:")
    if not condition:
        return None, None
    normalized = re.sub(r"\s+", " ", condition).casefold()
    if normalized.startswith("for ") and re.search(
        r"\b(?:candidates?|applicants?)\b",
        normalized,
    ):
        return condition, "applicant_subset"
    if normalized.startswith(("when ", "if ", "unless ", "as needed")):
        return condition, "contingency"
    if normalized.startswith("during "):
        condition_type: ConditionType = (
            "schedule"
            if re.search(r"\b(?:shift|overnight|weekend|evening|night)\b", normalized)
            else "temporal"
        )
        return condition, condition_type
    if normalized.startswith("in "):
        location_value = normalized.removeprefix("in ").removeprefix("the ").strip()
        temporal_type = _in_condition_temporal_type(location_value)
        if temporal_type is not None:
            return condition, temporal_type
        if (
            _is_known_condition_location(location_value)
            or _is_structured_condition_location(condition)
            or _is_coordinated_condition_location(
                condition,
                allow_proper_location=allow_proper_location,
                allow_unanchored_proper_location=(
                    allow_unanchored_coordinated_location
                ),
            )
            or (
                allow_proper_location
                and _is_proper_condition_location(condition)
            )
        ):
            return condition, "geographic"
        return None, None
    if normalized.startswith("where "):
        return condition, "unknown"
    return condition, "unknown"


def _in_condition_temporal_type(value: str) -> ConditionType | None:
    normalized = re.sub(r"\s+", " ", value).strip(" .,:;").casefold()
    if re.fullmatch(_CONDITION_TEMPORAL_PERIOD_PATTERN, normalized, re.IGNORECASE):
        return "temporal"
    if re.fullmatch(
        r"(?:the\s+)?(?:morning|afternoon|evening|night|overnight|"
        r"weekdays?|weekends?|(?:day|evening|night|overnight)\s+shifts?)",
        normalized,
        re.IGNORECASE,
    ):
        return "schedule"
    return None


def _is_known_condition_location(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip(" .,:;").casefold()
    return normalized in {
        *_known_condition_regions(),
        *_KNOWN_CONDITION_CITIES,
    }


def _known_condition_regions() -> set[str]:
    return {
        *(item.casefold() for item in CANADIAN_PROVINCES),
        *(item.casefold() for item in CANADIAN_PROVINCE_CODES),
        *(item.casefold() for item in CANADIAN_PROVINCE_ALIASES),
        *(item.casefold() for item in US_STATE_CODES),
        *(item.casefold() for item in US_STATE_NAMES),
        *(item.casefold() for item in COUNTRY_CODES),
        *(item.casefold() for item in COUNTRY_CODES.values()),
        *(item.casefold() for item in COUNTRY_ALIASES),
    }


def _is_known_condition_region(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip(" .,:;").casefold()
    return normalized in _known_condition_regions()


def _is_coordinated_condition_location(
    condition: str,
    *,
    allow_proper_location: bool,
    allow_unanchored_proper_location: bool,
) -> bool:
    location = re.sub(
        r"^in\s+(?:the\s+)?",
        "",
        condition.strip().rstrip(".,;:"),
        flags=re.IGNORECASE,
    )
    if re.search(r"\b(?:and|or)\b", location, re.IGNORECASE) is None:
        return False
    members = [
        member.strip()
        for member in re.split(
            r"\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+",
            location,
            flags=re.IGNORECASE,
        )
        if member.strip()
    ]
    anchored_members = [
        _is_known_condition_location(member)
        or _is_structured_condition_location(f"in {member}")
        for member in members
    ]
    return bool(
        len(members) >= 2
        and all(
            anchored
            or (
                allow_proper_location
                and _is_proper_condition_city(member, allow_uppercase=True)
            )
            for member, anchored in zip(members, anchored_members)
        )
        and (any(anchored_members) or allow_unanchored_proper_location)
    )


def _condition_is_capability_complement(
    *,
    subject_tokens: tuple[str, ...],
    predicate_prefix_tokens: tuple[str, ...],
) -> bool:
    predicate_head_index = len(predicate_prefix_tokens) - 1
    while predicate_head_index >= 0 and predicate_prefix_tokens[
        predicate_head_index
    ] in {
        *_CONDITION_GENERIC_QUALIFICATION_MODIFIERS,
        "ideally",
        "preferably",
    }:
        predicate_head_index -= 1
    if predicate_head_index >= 0 and (
        predicate_prefix_tokens[predicate_head_index]
        in _QUALIFICATION_COMPLEMENT_HEADS
        or predicate_prefix_tokens[predicate_head_index]
        in _CONDITION_CAPABILITY_GOVERNORS
    ):
        return True
    qualification_subject = any(
        token in _QUALIFICATION_COMPLEMENT_HEADS for token in subject_tokens
    )
    if not qualification_subject:
        return False
    meaningful_modifiers = [
        token
        for token in subject_tokens
        if token not in _QUALIFICATION_COMPLEMENT_HEADS
        and token not in _CONDITION_GENERIC_QUALIFICATION_MODIFIERS
        and not token.isdigit()
        and token
        not in {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}
    ]
    return not meaningful_modifiers


def _is_proper_condition_location(condition: str) -> bool:
    location = re.sub(
        r"^in\s+(?:the\s+)?",
        "",
        condition.strip().rstrip(".,;:"),
        flags=re.IGNORECASE,
    )
    if re.search(r"\b(?:and|or)\b", location, re.IGNORECASE) is not None:
        return False
    components = [component.strip() for component in location.split(",")]
    if len(components) > 1:
        return _is_structured_condition_location(condition)
    return _is_proper_condition_city(location)


def _is_structured_condition_location(condition: str) -> bool:
    location = re.sub(
        r"^in\s+(?:the\s+)?",
        "",
        condition.strip().rstrip(".,;:"),
        flags=re.IGNORECASE,
    )
    components = [component.strip() for component in location.split(",")]
    return bool(
        len(components) > 1
        and all(
            _is_known_condition_location(component.replace(".", ""))
            for component in components[1:]
        )
        and _is_proper_condition_city(
            components[0],
            allow_uppercase=True,
        )
    )


def _is_proper_condition_city(
    location: str,
    *,
    allow_uppercase: bool = False,
) -> bool:
    normalized = re.sub(r"\s+", " ", location).strip(" .,:;").casefold()
    if any(
        normalized == name or normalized.endswith(f" {name}")
        for name in _NON_LOCATION_PROPER_CONDITION_NAMES
    ):
        return False
    letters = "".join(character for character in location if character.isalpha())
    if not allow_uppercase and len(letters) > 1 and letters.isupper():
        return False
    if not re.fullmatch(_CONDITION_PROPER_NAME_PATTERN, location):
        return False
    return not any(
        re.fullmatch(pattern, location, flags=re.IGNORECASE)
        for definition in load_capability_ontology()
        for pattern in definition.patterns
    )


def _coordination(
    text: str,
) -> Literal["single", "all_of", "any_of", "alternative"]:
    if re.search(
        r"\bnot\s+(?:only|just|merely)\b.+\bbut\s+also\b",
        text,
        re.IGNORECASE,
    ):
        return "all_of"
    if re.search(r"\bor\s+equivalent\b|\bequivalent\s+(?:education|experience)\b", text, re.I):
        return "alternative"
    if _logical_or_matches(text):
        return "any_of"
    if _logical_and_matches(text):
        return "all_of"
    return "single"


def _logical_or_matches(text: str) -> list[re.Match[str]]:
    scalar_bound_spans = [
        match.span()
        for pattern in _SCALAR_OR_BOUND_PATTERNS
        for match in pattern.finditer(text)
    ]
    return [
        match
        for match in re.finditer(r"\bor\b", text, re.IGNORECASE)
        if not any(
            start <= match.start() < end
            for start, end in scalar_bound_spans
        )
    ]


def _logical_and_matches(text: str) -> list[re.Match[str]]:
    between_complement_starts = {
        match.start("conjunction")
        for match in re.finditer(
            r"\bbetween\b[^.;:\n]*?\b(?P<conjunction>and)\b",
            text,
            re.IGNORECASE,
        )
    }
    return [
        match
        for match in re.finditer(r"\band\b", text, re.IGNORECASE)
        if match.start() not in between_complement_starts
    ]


def _coordination_group(fragment: TextFragment, index: int) -> str | None:
    return f"role-claim-{fragment.start}-{index}"


def _section_conflict_flags(
    *,
    section: RoleSection,
    kind: str,
    modality: str,
) -> list[str]:
    flags: list[str] = []
    if section == "preferred_qualifications" and modality == "mandatory":
        flags.append("section_modality_conflict")
    if section == "responsibilities" and kind in {"qualification", "eligibility"}:
        flags.append("responsibility_section_requirement")
    return flags


def _claim_decision_trace(
    *,
    section: RoleSection,
    kind: str,
    modality: str,
    polarity: str,
    condition_type: ConditionType | None,
    coordination: str,
) -> list[str]:
    trace = [
        f"source_section:{section}",
        f"kind:{kind}",
        f"modality:{modality}",
        f"polarity:{polarity}",
        f"coordination:{coordination}",
    ]
    if condition_type is not None:
        trace.append(f"condition:{condition_type}")
    return trace


def _claim_confidence(*, section: RoleSection, modality: str) -> float:
    if modality == "optional":
        return 0.9
    if section in {"qualifications", "preferred_qualifications", "responsibilities"}:
        return 0.92
    if section == "capabilities":
        return 0.95
    return 0.82


def _evidence_from_claims(
    fragment: TextFragment,
    *,
    section: Literal[
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
        "capabilities",
        "company",
        "unsectioned",
    ],
    claims: list[RoleClaim],
    quality_flags: list[str] | None = None,
) -> RoleEvidence:
    primary = min(claims, key=_claim_priority)
    kind, modality = _legacy_semantics(primary)
    evidence = _make_evidence(
        fragment,
        kind=kind,
        modality=modality,
        section=section,
        confidence=primary.confidence,
        claims=claims,
        quality_flags=quality_flags or [],
    )
    evidence.requirement_expression = _requirement_expression(fragment, claims)
    return evidence


def _requirement_expression(
    fragment: TextFragment,
    claims: list[RoleClaim],
) -> RequirementExpression | None:
    requirement_claims = [
        claim for claim in claims if claim.kind in {"qualification", "eligibility"}
    ]
    if not requirement_claims:
        return None
    explicit_and_either = _explicit_and_either_requirement_expression(fragment)
    if explicit_and_either is not None:
        return _link_exact_requirement_claims(
            explicit_and_either,
            requirement_claims,
        )
    explicit_both = _explicit_both_requirement_expression(fragment)
    if explicit_both is not None:
        return _link_exact_requirement_claims(explicit_both, requirement_claims)
    structural = _structural_requirement_branch(
        fragment,
        (fragment.text, 0, len(fragment.text)),
        allow_plain_coordination=True,
    )
    if structural.operator != "claim":
        return _link_exact_requirement_claims(structural, requirement_claims)
    nested = _nested_requirement_expression(fragment)
    if nested is not None:
        return _link_exact_requirement_claims(nested, requirement_claims)
    if len(requirement_claims) == 1:
        claim = requirement_claims[0]
        return RequirementExpression(
            node_id=_requirement_node_id(
                operator="claim",
                source_start=claim.source_start,
                source_end=claim.source_end,
            ),
            operator="claim",
            claim_id=claim.claim_id,
            text=claim.text,
            source_start=claim.source_start,
            source_end=claim.source_end,
        )
    operator = _top_level_requirement_operator(
        fragment,
        claims,
    )
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator=operator,
            source_start=fragment.start,
            source_end=fragment.end,
        ),
        operator=operator,
        text=fragment.text,
        source_start=fragment.start,
        source_end=fragment.end,
        children=[
            RequirementExpression(
                node_id=_requirement_node_id(
                    operator="claim",
                    source_start=claim.source_start,
                    source_end=claim.source_end,
                ),
                operator="claim",
                claim_id=claim.claim_id,
                text=claim.text,
                source_start=claim.source_start,
                source_end=claim.source_end,
            )
            for claim in requirement_claims
        ],
    )


def _top_level_requirement_operator(
    fragment: TextFragment,
    claims: list[RoleClaim],
) -> Literal["all_of", "any_of"]:
    propositions = _proposition_fragments(fragment)
    proposition_claims = [claim for claim in claims if claim.kind != "environment"]
    if len(propositions) != len(proposition_claims):
        return "all_of"
    requirement_propositions = [
        proposition
        for proposition, claim in zip(propositions, proposition_claims)
        if claim.kind in {"qualification", "eligibility"}
    ]
    for left, right in zip(
        requirement_propositions,
        requirement_propositions[1:],
    ):
        gap_start = left.end - fragment.start
        gap_end = right.start - fragment.start
        connector = fragment.text[gap_start:gap_end]
        if _logical_or_matches(connector):
            return "any_of"
    return "all_of"


def _nested_requirement_expression(
    fragment: TextFragment,
) -> RequirementExpression | None:
    or_matches = _logical_or_matches(fragment.text)
    and_matches = _logical_and_matches(fragment.text)
    if not or_matches or not and_matches:
        return None
    split = next(
        (match for match in reversed(and_matches) if match.start() > or_matches[0].start()),
        None,
    )
    if split is None:
        return None
    left = fragment.text[: split.start()].strip().rstrip(",")
    right = fragment.text[split.end() :].strip()
    if not left or not right:
        return None
    # This fallback handles a serial nominal alternative followed by a separate
    # requirement ("Science, Engineering, or a related field and two years...").
    # Without a preceding list comma, ordinary Boolean precedence applies.
    if "," not in left:
        return None
    alternatives = [
        item.strip()
        for item in re.split(
            r"\s*,\s*(?:or\s+)?|\s+or\s+",
            left,
            flags=re.IGNORECASE,
        )
        if item.strip()
    ]
    if len(alternatives) < 2:
        return None
    left_start = fragment.text.find(left)
    right_start = fragment.text.find(right, split.end())
    alternative_children: list[RequirementExpression] = []
    search_from = 0
    for alternative in alternatives:
        relative_start = fragment.text.find(alternative, search_from, split.start())
        if relative_start < 0:
            return None
        search_from = relative_start + len(alternative)
        alternative_children.append(
            RequirementExpression(
                node_id=_requirement_node_id(
                    operator="claim",
                    source_start=fragment.start + relative_start,
                    source_end=fragment.start + relative_start + len(alternative),
                ),
                operator="claim",
                text=alternative,
                source_start=fragment.start + relative_start,
                source_end=fragment.start + relative_start + len(alternative),
            )
        )
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator="all_of",
            source_start=fragment.start,
            source_end=fragment.end,
        ),
        operator="all_of",
        text=fragment.text,
        source_start=fragment.start,
        source_end=fragment.end,
        children=[
            RequirementExpression(
                node_id=_requirement_node_id(
                    operator="any_of",
                    source_start=fragment.start + left_start,
                    source_end=fragment.start + left_start + len(left),
                ),
                operator="any_of",
                text=left,
                source_start=fragment.start + left_start,
                source_end=fragment.start + left_start + len(left),
                children=alternative_children,
            ),
            RequirementExpression(
                node_id=_requirement_node_id(
                    operator="claim",
                    source_start=fragment.start + right_start,
                    source_end=fragment.start + right_start + len(right),
                ),
                operator="claim",
                text=right,
                source_start=fragment.start + right_start,
                source_end=fragment.start + right_start + len(right),
            ),
        ],
    )


def _explicit_and_either_requirement_expression(
    fragment: TextFragment,
) -> RequirementExpression | None:
    conjunctions = _matches_at_expression_depth(
        fragment.text,
        re.finditer(
            r"\band\s+either\s+",
            fragment.text,
            re.IGNORECASE,
        ),
    )
    if not conjunctions:
        return None
    first_conjunction = conjunctions[0]
    either_match = re.search(
        r"\beither\b",
        first_conjunction.group(0),
        re.IGNORECASE,
    )
    if either_match is None:
        return None
    required = _expression_text_span(fragment.text, 0, first_conjunction.start())
    if required is None:
        return None
    groups: list[RequirementExpression] = []
    for index, conjunction in enumerate(conjunctions):
        either_match = re.search(r"\beither\b", conjunction.group(0), re.IGNORECASE)
        if either_match is None:
            return None
        group_start = conjunction.start() + either_match.start()
        group_end = (
            conjunctions[index + 1].start()
            if index + 1 < len(conjunctions)
            else len(fragment.text)
        )
        alternative_group = _expression_text_span(
            fragment.text,
            group_start,
            group_end,
        )
        if alternative_group is None:
            return None
        group = _structural_requirement_branch(fragment, alternative_group)
        if group.operator != "any_of":
            return None
        groups.append(group)
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator="all_of",
            source_start=fragment.start,
            source_end=fragment.end,
        ),
        operator="all_of",
        text=fragment.text,
        source_start=fragment.start,
        source_end=fragment.end,
        children=[
            _structural_requirement_branch(fragment, required),
            *groups,
        ],
    )


def _explicit_both_requirement_expression(
    fragment: TextFragment,
) -> RequirementExpression | None:
    or_both = re.search(r"\bor\s+both\s+", fragment.text, re.IGNORECASE)
    if or_both is not None:
        both_match = re.search(r"\bboth\b", or_both.group(0), re.IGNORECASE)
        if both_match is None:
            return None
        group_start = or_both.start() + both_match.start()
        conjunction = next(
            iter(
                _matches_at_expression_depth(
                    fragment.text,
                    _logical_and_matches(fragment.text),
                    start=or_both.end(),
                )
            ),
            None,
        )
        if conjunction is None:
            return None
        and_start = conjunction.start()
        and_end = conjunction.end()
        left = _expression_text_span(fragment.text, 0, or_both.start())
        middle = _expression_text_span(fragment.text, or_both.end(), and_start)
        right = _expression_text_span(fragment.text, and_end, len(fragment.text))
        group = _expression_text_span(fragment.text, group_start, len(fragment.text))
        if any(item is None for item in (left, middle, right, group)):
            return None
        assert left is not None
        assert middle is not None
        assert right is not None
        assert group is not None
        return _any_of_with_all_of_branch(
            fragment,
            alternative=left,
            group=group,
            group_members=(middle, right),
            group_first=False,
        )

    both = re.search(r"\bboth\s+", fragment.text, re.IGNORECASE)
    if both is None:
        return None
    conjunction = next(
        iter(
            _matches_at_expression_depth(
                fragment.text,
                _logical_and_matches(fragment.text),
                start=both.end(),
            )
        ),
        None,
    )
    if conjunction is None:
        return None
    and_start = conjunction.start()
    and_end = conjunction.end()
    disjunction = next(
        (
            match
            for match in _logical_or_matches(fragment.text)
            if match.start() >= and_end
        ),
        None,
    )
    if disjunction is None:
        return None
    or_start = disjunction.start()
    or_end = disjunction.end()
    first = _expression_text_span(fragment.text, both.end(), and_start)
    second = _expression_text_span(fragment.text, and_end, or_start)
    alternative = _expression_text_span(fragment.text, or_end, len(fragment.text))
    group = _expression_text_span(fragment.text, both.start(), or_start)
    if any(item is None for item in (first, second, alternative, group)):
        return None
    assert first is not None
    assert second is not None
    assert alternative is not None
    assert group is not None
    return _any_of_with_all_of_branch(
        fragment,
        alternative=alternative,
        group=group,
        group_members=(first, second),
        group_first=True,
    )


def _expression_text_span(
    source: str,
    start: int,
    end: int,
) -> tuple[str, int, int] | None:
    while start < end and (source[start].isspace() or source[start] == ","):
        start += 1
    while end > start and (source[end - 1].isspace() or source[end - 1] == ","):
        end -= 1
    if start >= end:
        return None
    return source[start:end], start, end


def _any_of_with_all_of_branch(
    fragment: TextFragment,
    *,
    alternative: tuple[str, int, int],
    group: tuple[str, int, int],
    group_members: tuple[tuple[str, int, int], tuple[str, int, int]],
    group_first: bool,
) -> RequirementExpression:
    alternative_node = _structural_requirement_branch(
        fragment,
        alternative,
        allow_plain_coordination=True,
    )
    group_node = RequirementExpression(
        node_id=_requirement_node_id(
            operator="all_of",
            source_start=fragment.start + group[1],
            source_end=fragment.start + group[2],
        ),
        operator="all_of",
        text=group[0],
        source_start=fragment.start + group[1],
        source_end=fragment.start + group[2],
        children=[
            _structural_requirement_branch(fragment, member)
            for member in group_members
        ],
    )
    children = (
        [group_node, alternative_node]
        if group_first
        else [alternative_node, group_node]
    )
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator="any_of",
            source_start=fragment.start,
            source_end=fragment.end,
        ),
        operator="any_of",
        text=fragment.text,
        source_start=fragment.start,
        source_end=fragment.end,
        children=children,
    )


def _structural_requirement_branch(
    fragment: TextFragment,
    span: tuple[str, int, int],
    *,
    allow_plain_coordination: bool = False,
    allow_bare_coordination: bool = False,
) -> RequirementExpression:
    unwrapped = _unwrapped_expression_span(fragment.text, span)
    if unwrapped is not None:
        return _structural_requirement_branch(
            fragment,
            unwrapped,
            allow_plain_coordination=True,
            allow_bare_coordination=True,
        )
    text, start, end = span
    branch = TextFragment(
        text=text,
        start=fragment.start + start,
        end=fragment.start + end,
    )
    explicit_and_either = _explicit_and_either_requirement_expression(branch)
    if explicit_and_either is not None:
        return explicit_and_either
    either = next(
        iter(
            _matches_at_expression_depth(
                branch.text,
                re.finditer(r"\beither\s+", branch.text, re.IGNORECASE),
            )
        ),
        None,
    )
    both = next(
        iter(
            _matches_at_expression_depth(
                branch.text,
                re.finditer(r"\bboth\s+", branch.text, re.IGNORECASE),
            )
        ),
        None,
    )
    if either is not None and (both is None or either.start() < both.start()):
        alternatives = _explicit_or_member_spans(
            branch.text,
            either.end(),
            len(branch.text),
        )
        if len(alternatives) >= 2:
            return _structural_requirement_operator(
                branch,
                operator="any_of",
                members=alternatives,
                allow_plain_coordination=True,
                allow_bare_coordination=True,
            )

    if both is not None:
        conjunction = next(
            iter(
                _matches_at_expression_depth(
                    branch.text,
                    _logical_and_matches(branch.text),
                    start=both.end(),
                )
            ),
            None,
        )
        if conjunction is not None:
            first = _expression_text_span(
                branch.text,
                both.end(),
                conjunction.start(),
            )
            second = _expression_text_span(
                branch.text,
                conjunction.end(),
                len(branch.text),
            )
            if first is not None and second is not None:
                return _structural_requirement_operator(
                    branch,
                    operator="all_of",
                    members=(first, second),
                    allow_plain_coordination=True,
                    allow_bare_coordination=True,
                )

    if allow_plain_coordination:
        alternatives = _explicit_or_member_spans(
            branch.text,
            0,
            len(branch.text),
            require_requirement_separator=not allow_bare_coordination,
        )
        if len(alternatives) >= 2:
            return _structural_requirement_operator(
                branch,
                operator="any_of",
                members=alternatives,
                allow_plain_coordination=True,
                allow_bare_coordination=allow_bare_coordination,
            )
        conjuncts = _explicit_and_member_spans(
            branch.text,
            0,
            len(branch.text),
            allow_bare_members=allow_bare_coordination,
        )
        if len(conjuncts) >= 2:
            return _structural_requirement_operator(
                branch,
                operator="all_of",
                members=conjuncts,
                allow_plain_coordination=True,
                allow_bare_coordination=allow_bare_coordination,
            )

    return _structural_requirement_leaf(fragment, span)


def _explicit_or_member_spans(
    source: str,
    start: int,
    end: int,
    *,
    require_requirement_separator: bool = False,
) -> tuple[tuple[str, int, int], ...]:
    logical_or_spans = [
        match.span()
        for match in _matches_at_expression_depth(
            source,
            _logical_or_matches(source),
            start=start,
            end=end,
        )
        if not require_requirement_separator
        or _plain_or_is_requirement_separator(
            source,
            start=start,
            end=end,
            disjunction=match.span(),
        )
    ]
    if not logical_or_spans:
        return ()
    delimiter_depths = _expression_delimiter_depths(source)
    comma_spans = [
        (start + match.start(), start + match.end())
        for match in re.finditer(r",", source[start:end])
        if delimiter_depths[start + match.start()] == delimiter_depths[start]
    ]
    protected_commas, protected_ors = _supplemental_coordination_separator_starts(
        source,
        comma_spans=comma_spans,
        coordinator_spans=logical_or_spans,
    )
    conjunctive_commas = _serial_conjunction_comma_starts(
        source,
        start=start,
        end=end,
        comma_spans=comma_spans,
        logical_or_spans=logical_or_spans,
    )
    separators = sorted(
        [
            (*span, False)
            for span in logical_or_spans
            if span[0] not in protected_ors
        ]
        + [
            (*span, True)
            for span in comma_spans
            if span[0] not in protected_commas
            and span[0] not in conjunctive_commas
        ],
        key=lambda item: (item[0], item[1]),
    )
    members: list[tuple[str, int, int]] = []
    cursor = start
    for index, (separator_start, separator_end, is_comma) in enumerate(separators):
        if separator_start < cursor:
            continue
        next_separator_start = (
            separators[index + 1][0] if index + 1 < len(separators) else end
        )
        if is_comma and _expression_comma_belongs_to_location(
            source,
            member_start=cursor,
            comma_start=separator_start,
            comma_end=separator_end,
            next_separator_start=next_separator_start,
        ):
            continue
        member = _expression_text_span(source, cursor, separator_start)
        if member is not None:
            members.append(member)
        cursor = separator_end
    tail = _expression_text_span(source, cursor, end)
    if tail is not None:
        members.append(tail)
    return tuple(members)


def _serial_conjunction_comma_starts(
    source: str,
    *,
    start: int,
    end: int,
    comma_spans: list[tuple[int, int]],
    logical_or_spans: list[tuple[int, int]],
) -> frozenset[int]:
    conjunctions = _matches_at_expression_depth(
        source,
        _logical_and_matches(source),
        start=start,
        end=end,
    )
    protected: set[int] = set()
    for conjunction in conjunctions:
        previous_or_end = max(
            (
                span[1]
                for span in logical_or_spans
                if span[1] <= conjunction.start()
            ),
            default=start,
        )
        protected.update(
            comma[0]
            for comma in comma_spans
            if previous_or_end <= comma[0] < conjunction.start()
        )
    return frozenset(protected)


def _explicit_and_member_spans(
    source: str,
    start: int,
    end: int,
    *,
    allow_bare_members: bool = False,
) -> tuple[tuple[str, int, int], ...]:
    logical_and_spans = [
        match.span()
        for match in _matches_at_expression_depth(
            source,
            _logical_and_matches(source),
            start=start,
            end=end,
        )
        if _plain_and_is_requirement_separator(
            source,
            start=start,
            end=end,
            conjunction=match.span(),
            allow_predicate_ellipsis=allow_bare_members,
        )
    ]
    if not logical_and_spans:
        return ()
    delimiter_depths = _expression_delimiter_depths(source)
    comma_spans = [
        (start + match.start(), start + match.end())
        for match in re.finditer(r",", source[start:end])
        if delimiter_depths[start + match.start()] == delimiter_depths[start]
    ]
    alternative_commas = _alternative_constituent_comma_starts(
        source,
        start=start,
        end=end,
        comma_spans=comma_spans,
        logical_and_spans=logical_and_spans,
    )
    protected_commas, protected_ands = _supplemental_coordination_separator_starts(
        source,
        comma_spans=comma_spans,
        coordinator_spans=logical_and_spans,
    )
    separators = sorted(
        [
            (*span, False)
            for span in logical_and_spans
            if span[0] not in protected_ands
        ]
        + [
            (*span, True)
            for span in comma_spans
            if span[0] not in protected_commas
            and span[0] not in alternative_commas
        ],
        key=lambda item: (item[0], item[1]),
    )
    members: list[tuple[str, int, int]] = []
    cursor = start
    for index, (separator_start, separator_end, is_comma) in enumerate(separators):
        if separator_start < cursor:
            continue
        next_separator_start = (
            separators[index + 1][0] if index + 1 < len(separators) else end
        )
        if is_comma and _expression_comma_belongs_to_location(
            source,
            member_start=cursor,
            comma_start=separator_start,
            comma_end=separator_end,
            next_separator_start=next_separator_start,
        ):
            continue
        member = _expression_text_span(source, cursor, separator_start)
        if member is not None:
            members.append(member)
        cursor = separator_end
    tail = _expression_text_span(source, cursor, end)
    if tail is not None:
        members.append(tail)
    return tuple(members)


def _alternative_constituent_comma_starts(
    source: str,
    *,
    start: int,
    end: int,
    comma_spans: list[tuple[int, int]],
    logical_and_spans: list[tuple[int, int]],
) -> frozenset[int]:
    logical_or_spans = [
        match.span()
        for match in _matches_at_expression_depth(
            source,
            _logical_or_matches(source),
            start=start,
            end=end,
        )
    ]
    protected: set[int] = set()
    for disjunction_start, disjunction_end in logical_or_spans:
        constituent_start = max(
            (
                conjunction_end
                for _conjunction_start, conjunction_end in logical_and_spans
                if conjunction_end <= disjunction_start
            ),
            default=start,
        )
        preceding_commas = [
            comma
            for comma in comma_spans
            if constituent_start <= comma[0] < disjunction_start
        ]
        for index, comma in enumerate(preceding_commas):
            member_end = (
                preceding_commas[index + 1][0]
                if index + 1 < len(preceding_commas)
                else disjunction_start
            )
            if _expression_span_starts_requirement_member(
                source,
                comma[1],
                member_end,
            ):
                constituent_start = comma[1]
        protected.update(
            comma_start
            for comma_start, _comma_end in preceding_commas
            if constituent_start <= comma_start < disjunction_end
        )
    return frozenset(protected)


def _plain_and_is_requirement_separator(
    source: str,
    *,
    start: int,
    end: int,
    conjunction: tuple[int, int],
    allow_predicate_ellipsis: bool = False,
) -> bool:
    left = _expression_text_span(source, start, conjunction[0])
    right = _expression_text_span(source, conjunction[1], end)
    if left is None or right is None:
        return False
    left_tokens = lexical_tokens(left[0])
    right_tokens = lexical_tokens(right[0])
    if not left_tokens or not right_tokens:
        return False
    if _plain_and_has_postpositive_shared_noun_head(left_tokens, right_tokens):
        return False
    if _is_requirement_predicate_token(right_tokens[0]):
        return True
    predicate_index = next(
        (
            index
            for index, token in enumerate(left_tokens)
            if _is_requirement_predicate_token(token)
        ),
        None,
    )
    if predicate_index is None:
        return bool(
            (len(left_tokens) == 1 and len(right_tokens) == 1)
            or ("," in left[0] and len(right_tokens) == 1)
            or (
                _expression_span_starts_requirement_member(
                    source,
                    left[1],
                    left[2],
                )
                and _expression_span_starts_requirement_member(
                    source,
                    right[1],
                    right[2],
                )
            )
        )
    left_complement = left_tokens[predicate_index + 1 :]
    if not left_complement:
        return False
    right_has_shared_head = right_tokens[-1] in _QUALIFICATION_COMPLEMENT_HEADS
    left_has_parallel_head = any(
        token in _QUALIFICATION_COMPLEMENT_HEADS for token in left_complement
    )
    if right_has_shared_head and not left_has_parallel_head:
        return False
    if allow_predicate_ellipsis:
        return True
    return _expression_span_starts_requirement_member(
        source,
        right[1],
        right[2],
    )


def _plain_and_has_postpositive_shared_noun_head(
    left_tokens: tuple[str, ...],
    right_tokens: tuple[str, ...],
) -> bool:
    right_nominal = _tokens_before_requirement_suffix(right_tokens)
    if (
        not _looks_like_nominal_coordinate(right_nominal)
    ):
        return False
    governor_index = next(
        (
            index
            for index in range(len(left_tokens) - 2, -1, -1)
            if left_tokens[index] in _QUALIFICATION_COMPLEMENT_HEADS
            and left_tokens[index + 1] in {"in", "of", "using", "with"}
        ),
        None,
    )
    return bool(
        governor_index is not None
        and len(left_tokens[governor_index + 2 :]) == 1
    )


def _tokens_before_requirement_suffix(tokens: tuple[str, ...]) -> tuple[str, ...]:
    suffixes = (
        ("are", "desired"),
        ("are", "preferred"),
        ("are", "required"),
        ("is", "desired"),
        ("is", "preferred"),
        ("is", "required"),
        ("was", "required"),
        ("were", "required"),
        ("preferred",),
        ("required",),
    )
    for suffix in suffixes:
        if len(tokens) > len(suffix) and tokens[-len(suffix) :] == suffix:
            return tokens[: -len(suffix)]
    return tokens


def _plain_or_is_requirement_separator(
    source: str,
    *,
    start: int,
    end: int,
    disjunction: tuple[int, int],
) -> bool:
    left = _expression_text_span(source, start, disjunction[0])
    right = _expression_text_span(source, disjunction[1], end)
    if left is None or right is None:
        return False
    right_member_end = _plain_coordination_member_end(
        source,
        start=right[1],
        end=right[2],
    )
    right_member = _expression_text_span(source, right[1], right_member_end)
    if right_member is None:
        return False
    if _plain_or_has_shared_qualification_head(
        left=left,
        right=right_member,
    ):
        return False
    return _expression_span_starts_requirement_member(
        source,
        right_member[1],
        right_member[2],
    )


def _plain_coordination_member_end(
    source: str,
    *,
    start: int,
    end: int,
) -> int:
    boundaries = [
        match.start()
        for match in _matches_at_expression_depth(
            source,
            re.finditer(r",", source),
            start=start,
            end=end,
        )
    ]
    boundaries.extend(
        match.start()
        for match in _matches_at_expression_depth(
            source,
            [*_logical_or_matches(source), *_logical_and_matches(source)],
            start=start,
            end=end,
        )
    )
    return min(boundaries, default=end)


def _plain_or_has_shared_qualification_head(
    *,
    left: tuple[str, int, int],
    right: tuple[str, int, int],
) -> bool:
    left_tokens = lexical_tokens(left[0])
    predicate_index = next(
        (
            index
            for index in range(len(left_tokens) - 1, -1, -1)
            if index < len(left_tokens) - 1
            and _is_requirement_predicate_token(left_tokens[index])
        ),
        None,
    )
    if predicate_index is None:
        return False
    left_complement = left_tokens[predicate_index + 1 :]
    if not left_complement or any(
        token in _QUALIFICATION_COMPLEMENT_HEADS for token in left_complement
    ):
        return False
    right_tokens = lexical_tokens(right[0])
    return bool(
        right_tokens
        and right_tokens[-1] in _QUALIFICATION_COMPLEMENT_HEADS
        and not any(_is_requirement_predicate_token(token) for token in right_tokens)
    )


def _is_requirement_predicate_token(token: str) -> bool:
    normalized = token.casefold()
    if _is_bare_requirement_predicate_token(normalized):
        return True
    if normalized.endswith("ing"):
        stem = normalized[:-3]
        return bool(
            stem in _REQUIREMENT_COORDINATE_PREDICATE_HEADS
            or f"{stem}e" in _REQUIREMENT_COORDINATE_PREDICATE_HEADS
        )
    if normalized.endswith("ed"):
        stem = normalized[:-2]
        return bool(
            stem in _REQUIREMENT_COORDINATE_PREDICATE_HEADS
            or f"{stem}e" in _REQUIREMENT_COORDINATE_PREDICATE_HEADS
        )
    return False


def _is_bare_requirement_predicate_token(token: str) -> bool:
    return token.casefold() in _REQUIREMENT_COORDINATE_PREDICATE_HEADS


def _expression_span_starts_requirement_member(
    source: str,
    start: int,
    end: int,
) -> bool:
    span = _expression_text_span(source, start, end)
    if span is None:
        return False
    tokens = list(lexical_tokens(span[0]))
    while tokens and tokens[0] in {
        "a",
        "an",
        "both",
        "either",
        "the",
    }:
        tokens.pop(0)
    if not tokens:
        return False
    return bool(
        _is_requirement_predicate_token(tokens[0])
        or (
            len(tokens) >= 2
            and tokens[1] in _OPEN_PREDICATE_COMPLEMENT_HEADS
        )
        or any(
            token in _QUALIFICATION_COMPLEMENT_HEADS
            for token in tokens[:4]
        )
    )


def _matches_at_expression_depth(
    source: str,
    matches: Iterable[re.Match[str]],
    *,
    start: int = 0,
    end: int | None = None,
) -> list[re.Match[str]]:
    expression_end = len(source) if end is None else end
    depths = _expression_delimiter_depths(source)
    target_depth = depths[start]
    return [
        match
        for match in matches
        if start <= match.start()
        and match.end() <= expression_end
        and depths[match.start()] == target_depth
    ]


def _expression_members_from_separators(
    source: str,
    *,
    start: int,
    end: int,
    separators: list[tuple[int, int]],
) -> tuple[tuple[str, int, int], ...]:
    if not separators:
        return ()
    members: list[tuple[str, int, int]] = []
    cursor = start
    for separator_start, separator_end in separators:
        member = _expression_text_span(source, cursor, separator_start)
        if member is not None:
            members.append(member)
        cursor = separator_end
    tail = _expression_text_span(source, cursor, end)
    if tail is not None:
        members.append(tail)
    return tuple(members)


def _expression_delimiter_depths(source: str) -> tuple[int, ...]:
    closer_for = {"(": ")", "[": "]", "{": "}"}
    opener_stack: list[tuple[int, str]] = []
    matched_openers: set[int] = set()
    matched_closers: set[int] = set()
    for index, character in enumerate(source):
        if character in closer_for:
            opener_stack.append((index, closer_for[character]))
        elif opener_stack and character == opener_stack[-1][1]:
            opener_index, _expected_closer = opener_stack.pop()
            matched_openers.add(opener_index)
            matched_closers.add(index)

    expected_closers: list[str] = []
    depths: list[int] = []
    for index, character in enumerate(source):
        depths.append(len(expected_closers))
        if index in matched_openers:
            expected_closers.append(closer_for[character])
        elif index in matched_closers:
            expected_closers.pop()
    depths.append(len(expected_closers))
    return tuple(depths)


def _unwrapped_expression_span(
    source: str,
    span: tuple[str, int, int],
) -> tuple[str, int, int] | None:
    _text, start, end = span
    while end > start and source[end - 1] in ".?!":
        end -= 1
    if end - start < 2:
        return None
    closer_for = {"(": ")", "[": "]", "{": "}"}
    opening = source[start]
    expected_closer = closer_for.get(opening)
    if expected_closer is None or source[end - 1] != expected_closer:
        return None
    stack = [expected_closer]
    for index in range(start + 1, end):
        character = source[index]
        if character in closer_for:
            stack.append(closer_for[character])
        elif stack and character == stack[-1]:
            stack.pop()
            if not stack and index != end - 1:
                return None
    if stack:
        return None
    return _expression_text_span(source, start + 1, end - 1)


def _supplemental_coordination_separator_starts(
    source: str,
    *,
    comma_spans: list[tuple[int, int]],
    coordinator_spans: list[tuple[int, int]],
) -> tuple[frozenset[int], frozenset[int]]:
    protected_commas: set[int] = set()
    protected_coordinators: set[int] = set()
    for opening_index, opening in enumerate(comma_spans):
        supplemental = source[opening[1] :].strip()
        if _SUPPLEMENTAL_ALTERNATIVE_MODIFIER_PATTERN.match(supplemental) is None:
            continue
        structural_boundaries = sorted(
            [
                span
                for span in comma_spans[opening_index + 1 :]
            ]
            + [span for span in coordinator_spans if span[0] >= opening[1]],
            key=lambda span: span[0],
        )
        outer_candidates: list[
            tuple[tuple[int, int], tuple[int, int]]
        ] = []
        for coordinator in coordinator_spans:
            if coordinator[0] < opening[1]:
                continue
            closing = next(
                (
                    comma
                    for comma in reversed(comma_spans[opening_index + 1 :])
                    if comma[1] <= coordinator[0]
                    and not source[comma[1] : coordinator[0]].strip()
                ),
                None,
            )
            if closing is None:
                continue
            member_end = next(
                (
                    boundary[0]
                    for boundary in structural_boundaries
                    if boundary[0] >= coordinator[1]
                ),
                len(source),
            )
            if _expression_span_starts_requirement_member(
                source,
                coordinator[1],
                member_end,
            ):
                outer_candidates.append((closing, coordinator))
        if outer_candidates:
            closing, outer_coordinator = outer_candidates[0]
        else:
            closing = (len(source), len(source))
            outer_coordinator = closing
        protected_commas.update(
            span[0]
            for span in comma_spans
            if opening[0] <= span[0] <= closing[0]
        )
        protected_coordinators.update(
            span[0]
            for span in coordinator_spans
            if opening[1] <= span[0] < outer_coordinator[0]
        )
    return frozenset(protected_commas), frozenset(protected_coordinators)


def _expression_comma_belongs_to_location(
    source: str,
    *,
    member_start: int,
    comma_start: int,
    comma_end: int,
    next_separator_start: int,
) -> bool:
    region = source[comma_end:next_separator_start].strip()
    if not _is_known_condition_region(region):
        return False
    city_source = source[member_start:comma_start].strip()
    in_matches = list(re.finditer(r"\bin\s+(?:the\s+)?", city_source, re.IGNORECASE))
    city = city_source[in_matches[-1].end() :] if in_matches else city_source
    final_city = re.split(r"\s+(?:and|or)\s+", city, flags=re.IGNORECASE)[-1]
    return _is_proper_condition_city(final_city.strip(), allow_uppercase=True)


def _structural_requirement_operator(
    fragment: TextFragment,
    *,
    operator: Literal["all_of", "any_of"],
    members: tuple[tuple[str, int, int], ...],
    allow_plain_coordination: bool = False,
    allow_bare_coordination: bool = False,
) -> RequirementExpression:
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator=operator,
            source_start=fragment.start,
            source_end=fragment.end,
        ),
        operator=operator,
        text=fragment.text,
        source_start=fragment.start,
        source_end=fragment.end,
        children=[
            _structural_requirement_branch(
                fragment,
                member,
                allow_plain_coordination=allow_plain_coordination,
                allow_bare_coordination=allow_bare_coordination,
            )
            for member in members
        ],
    )


def _structural_requirement_leaf(
    fragment: TextFragment,
    span: tuple[str, int, int],
) -> RequirementExpression:
    text, start, end = span
    return RequirementExpression(
        node_id=_requirement_node_id(
            operator="claim",
            source_start=fragment.start + start,
            source_end=fragment.start + end,
        ),
        operator="claim",
        text=text,
        source_start=fragment.start + start,
        source_end=fragment.start + end,
    )


def _link_exact_requirement_claims(
    expression: RequirementExpression,
    claims: list[RoleClaim],
) -> RequirementExpression:
    if expression.operator == "claim":
        matches = [
            claim
            for claim in claims
            if claim.source_start == expression.source_start
            and claim.source_end == expression.source_end
            and claim.text == expression.text
        ]
        expression.claim_id = matches[0].claim_id if len(matches) == 1 else None
        return expression
    expression.claim_id = None
    for child in expression.children:
        _link_exact_requirement_claims(child, claims)
    return expression


def _requirement_node_id(
    *,
    operator: str,
    source_start: int | None,
    source_end: int | None,
) -> str:
    return f"role-requirement-{source_start}-{source_end}-{operator}"


def _claim_priority(claim: RoleClaim) -> int:
    return {
        "mandatory": 0,
        "expected": 1,
        "preferred": 2,
        "optional": 3,
        "descriptive": 4,
        "prohibited": 5,
        "unknown": 6,
    }[claim.modality]


def _legacy_semantics(
    claim: RoleClaim,
) -> tuple[
    Literal["responsibility", "requirement", "context"],
    Literal["required", "preferred", "descriptive", "non_requirement"],
]:
    if claim.kind == "responsibility":
        return "responsibility", "descriptive"
    if claim.kind in {"qualification", "eligibility"}:
        modality = {
            "mandatory": "required",
            "expected": "required",
            "preferred": "preferred",
            "optional": "non_requirement",
            "prohibited": "non_requirement",
            "descriptive": "non_requirement",
            "unknown": "non_requirement",
        }[claim.modality]
        return "requirement", modality
    return "context", "non_requirement"


def _make_evidence(
    fragment: TextFragment,
    *,
    kind: Literal["responsibility", "requirement", "context"],
    modality: Literal["required", "preferred", "descriptive", "non_requirement"],
    section: Literal[
        "responsibilities",
        "qualifications",
        "preferred_qualifications",
        "role_overview",
        "capabilities",
        "company",
        "unsectioned",
    ],
    confidence: float,
    claims: list[RoleClaim] | None = None,
    quality_flags: list[str] | None = None,
) -> RoleEvidence:
    return RoleEvidence(
        kind=kind,
        modality=modality,
        source_section=section,
        text=fragment.text,
        evidence_text=fragment.text,
        evidence_start=fragment.start,
        evidence_end=fragment.end,
        confidence=confidence,
        claims=claims or [],
        quality_flags=quality_flags or [],
    )


def _contains_any_phrase(tokens: tuple[str, ...], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(_contains_phrase(tokens, phrase) for phrase in phrases)


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(tokens[index : index + width] == phrase for index in range(len(tokens) - width + 1))


def _starts_with(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return tokens[: len(phrase)] == phrase


def _matching_prefix_width(
    tokens: tuple[str, ...],
    phrases: tuple[tuple[str, ...], ...],
) -> int:
    return max((len(phrase) for phrase in phrases if _starts_with(tokens, phrase)), default=0)


def _contains_no_required(tokens: tuple[str, ...]) -> bool:
    for index, token in enumerate(tokens):
        if token != "no":
            continue
        if "required" in tokens[index + 1 : index + 7]:
            return True
    return False


def _required_is_negated(tokens: tuple[str, ...], index: int) -> bool:
    if index > 0 and tokens[index - 1] == "not":
        return True
    lower_bound = max(0, index - 6)
    return "no" in tokens[lower_bound:index]


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)
