from __future__ import annotations

import html
import re

from jobcockpit.ingestion.posting_document import session_cached
from jobcockpit.ingestion.posting_fields import (
    _PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN,
    FIELD_BOUNDARY_LABELS,
    _field_label,
    _is_provenance_field_candidate,
    _provenance_field_can_be_inline,
    _provenance_field_semantic,
)
from jobcockpit.ingestion.timing_constants import MONTH_NAMES
from jobcockpit.ingestion.timing_models import (
    EmploymentTimingContentScope,
    PostingContentScope,
    PostingFieldSpan,
)

_INTERLEAVED_PORTAL_PANEL_START = re.compile(
    r"^get\s+noti(?:fied|[\ue000-\uf8ff\ufffd]ed)(?:\s+for\b.*)?$",
    re.IGNORECASE,
)
_INTERLEAVED_APPLICATION_FOOTER_START = re.compile(
    r"^apply\s+now(?:\s*[»>])?$",
    re.IGNORECASE,
)
_PRINTED_PAGE_MARKER = re.compile(
    r"^\s*(?:\d+\s+of\s+\d+|\d+\s*/\s*\d+)"
    r"(?:\s+\d{1,2}/\d{1,2}/\d{4}(?:,?\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)?\s*$",
    re.IGNORECASE,
)
_COOKIE_PANEL_CONTROL = re.compile(
    r"(?:reject\s+all\s+cookies\s+)?accept\s+all\s+cookies\b",
    re.IGNORECASE,
)
_COOKIE_PANEL_HEADING = re.compile(r"^we\s+use\s+cookies\s*$", re.IGNORECASE)
_EMBEDDED_PAGE_CONFIGURATION_START = re.compile(
    r'^\{\s*"themeOptions"\s*:\s*\{',
)
_ELUTA_REPORT_DIALOG_PREFIX = re.compile(
    r"^report\s+job:\s+.*?"
    r"\beluta\s+indexes\s+job\s+postings\s+directly\s+from\s+employer\s+websites\b"
    r".*?\badditional\s+information\s+report\s+job\s+",
    re.IGNORECASE,
)
_FLATTENED_NON_ROLE_TAIL_STARTS = (
    re.compile(
        r"\bhere['’]s\s+what\s+you\s+can\s+expect\s+when\s+you\s+join\s+our\s+team\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"\binterested\s+candidates\s+(?:are\s+)?"
        r"(?:(?:asked|required|invited)\s+to\s+)?submit\s+a\s+resume\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breport\s+job\s*\+\s*read\s+more\s+apply\s+now\s+share\s+this\s+job\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btravel\s+requirements\s+(?:none|not\s+required|no\s+travel)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsimilar\s+jobs\s+(?=(?:summer|winter|fall|spring)\s+"
        r"(?:20\d{2}\b|intern(?:ship)?\b|co-?op\b|student\b))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?i:(?<!to )\bsimilar\s+jobs)\s+(?=(?:20\d{2}\b|[#A-Z]))"
    ),
)

_EMPLOYMENT_START_FIELD_ALIASES = (
    "Anticipated Start Date",
    "Commencement Date",
    "Desired Start Date",
    "Employment Start Date",
    "Expected Start Date",
    "Internship Start Date",
    "Position Start Date",
    "Start Date",
    "Start Date Of Co-op",
    "Start Date Of Internship",
    "Start Date Of Work Term",
    "Term Start",
    "Work Term Start Date",
)
_EMPLOYMENT_END_FIELD_ALIASES = (
    "Completion Date",
    "Employment End Date",
    "End Date",
    "End Date Of Co-op",
    "End Date Of Internship",
    "End Date Of Work Term",
    "Internship End Date",
    "Position End Date",
    "Work Term End Date",
)
_EMPLOYMENT_ACADEMIC_TERM_FIELD_ALIASES = (
    "Academic Term",
    "Co-op Term",
    "Coop Term",
    "Employment Term",
    "Internship Term",
    "Job Term",
    "Placement Term",
    "Position Term",
    "Season",
    "Term",
    "Work Term",
)
POSTING_FIELD_LABELS = (
    *FIELD_BOUNDARY_LABELS,
    *_EMPLOYMENT_START_FIELD_ALIASES,
    *_EMPLOYMENT_END_FIELD_ALIASES,
    *_EMPLOYMENT_ACADEMIC_TERM_FIELD_ALIASES,
    "Application Date",
    "Application Deadline",
    "Application End Date",
    "Application Start Date",
    "Apply By",
    "Close Date",
    "Closing Date",
    "Contract Length",
    "Contract Duration",
    "Contract Type",
    "Contract Type And Duration",
    "Date Posted",
    "Details",
    "Duration",
    "Education Type",
    "Employment Type",
    "Employment Status",
    "Expected Start Date To End Date",
    "Enrollment",
    "Enrollment Status",
    "Interview Date",
    "Interview Window",
    "Job Post End Date",
    "Job Type",
    "Length Of Term",
    "Note",
    "Open Date",
    "Position Type",
    "Posted",
    "Posted Date",
    "Posted On",
    "Posting Closing Date",
    "Posting End Date",
    "Posting Opening Date",
    "Posting Start Date",
    "Requisition Date",
    "Schedule Type",
    "Student Status",
    "Target Date",
    "Term Duration",
    "Term Length",
    "Work Type",
    "Worker Type",
    "Workplace Type",
)
_POSTING_FIELD_LABEL_ALTERNATION = "|".join(
    re.escape(label) for label in sorted(set(POSTING_FIELD_LABELS), key=len, reverse=True)
)
_POSTING_FIELD_MARKER_PATTERN = re.compile(
    rf"(?<![\w/.-])(?P<label>(?:{_PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN}|"
    rf"{_POSTING_FIELD_LABEL_ALTERNATION}))\s*:\s*",
    flags=re.IGNORECASE,
)
_EMPLOYMENT_START_FIELD_LABELS = {
    " ".join(label.casefold().split()) for label in _EMPLOYMENT_START_FIELD_ALIASES
}
_EMPLOYMENT_END_FIELD_LABELS = {
    " ".join(label.casefold().split()) for label in _EMPLOYMENT_END_FIELD_ALIASES
}
_EMPLOYMENT_SCHEDULE_FIELD_LABELS = {
    "contract type",
    "contract type and duration",
    "employment type",
    "employment status",
    "job type",
    "position type",
    "schedule",
    "schedule type",
    "work type",
    "workplace type",
}
_EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS = {
    "contract duration",
    "contract length",
    "contract type and duration",
    "duration",
    "expected start date to end date",
    "length of term",
    "term duration",
    "term length",
}
_EMPLOYMENT_ACADEMIC_TERM_FIELD_LABELS = {
    " ".join(label.casefold().split())
    for label in _EMPLOYMENT_ACADEMIC_TERM_FIELD_ALIASES
}
_EMPLOYMENT_DESCRIPTIVE_SCHEDULE_FIELD_LABELS = {
    "details",
    "job title",
    "note",
    "page title",
    "position",
    "role",
    "title",
}


@session_cached
def _posting_content_scope(raw_text: str) -> PostingContentScope:
    lines: list[str] = []
    source_lines = html.unescape(raw_text).replace("\u00a0", " ").splitlines()
    line_index = 0
    while line_index < len(source_lines):
        line = source_lines[line_index].strip()
        line = _without_eluta_report_dialog_prefix(line)
        line = _without_flattened_non_role_tail(line)
        if not line:
            line_index += 1
            continue
        if _EMBEDDED_PAGE_CONFIGURATION_START.match(line) is not None:
            break
        cookie_panel = _printed_cookie_panel(source_lines, line_index)
        if cookie_panel is not None:
            content_prefix, panel_end = cookie_panel
            if content_prefix:
                lines.append(content_prefix)
            line_index = panel_end + 1
            continue
        panel_end = _interleaved_portal_panel_end(source_lines, line_index)
        if panel_end is not None:
            line_index = panel_end + 1
            continue
        footer_end = _interleaved_application_footer_end(source_lines, line_index)
        if footer_end is not None:
            line_index = footer_end + 1
            continue
        if _is_related_jobs_heading(line):
            break
        lines.append(line)
        line_index += 1
    scoped_lines = tuple(lines)
    return PostingContentScope(text="\n".join(scoped_lines), lines=scoped_lines)


def _without_eluta_report_dialog_prefix(value: str) -> str:
    match = _ELUTA_REPORT_DIALOG_PREFIX.match(value)
    if match is None:
        return value
    return value[match.end() :].lstrip()


def _without_flattened_non_role_tail(value: str) -> str:
    starts = [
        match.start()
        for pattern in _FLATTENED_NON_ROLE_TAIL_STARTS
        if (match := pattern.search(value)) is not None
    ]
    if not starts:
        return value
    return value[: min(starts)].rstrip()


def _printed_cookie_panel(
    source_lines: list[str],
    start: int,
) -> tuple[str, int] | None:
    line = source_lines[start].strip()
    control = _COOKIE_PANEL_CONTROL.search(line)
    heading = _COOKIE_PANEL_HEADING.fullmatch(line)
    if control is None and heading is None:
        return None

    for line_index in range(start + 1, min(len(source_lines), start + 64)):
        if _PRINTED_PAGE_MARKER.fullmatch(source_lines[line_index].strip()) is None:
            continue
        content_prefix = line[: control.start()].rstrip() if control is not None else ""
        normalized_prefix = " ".join(content_prefix.casefold().split())
        if "cookie" in normalized_prefix or normalized_prefix.startswith("by clicking"):
            content_prefix = ""
        return content_prefix, line_index
    return None


def _interleaved_portal_panel_end(
    source_lines: list[str],
    start: int,
) -> int | None:
    if _INTERLEAVED_PORTAL_PANEL_START.fullmatch(source_lines[start].strip()) is None:
        return None

    related_listings_seen = False
    for line_index in range(start + 1, min(len(source_lines), start + 32)):
        line = source_lines[line_index].strip()
        if _PRINTED_PAGE_MARKER.fullmatch(line) is not None:
            return line_index if related_listings_seen else None
        if _is_related_jobs_heading(line):
            related_listings_seen = True
    return None


def _interleaved_application_footer_end(
    source_lines: list[str],
    start: int,
) -> int | None:
    if (
        _INTERLEAVED_APPLICATION_FOOTER_START.fullmatch(
            source_lines[start].strip()
        )
        is None
    ):
        return None

    first_material_index: int | None = None
    for line_index in range(start + 1, min(len(source_lines), start + 4)):
        if source_lines[line_index].strip():
            first_material_index = line_index
            break
    if first_material_index is None:
        return None
    first_heading = " ".join(
        source_lines[first_material_index].casefold().rstrip(" :").split()
    )
    if first_heading != "find similar jobs":
        return None

    for line_index in range(
        first_material_index + 1,
        min(len(source_lines), first_material_index + 11),
    ):
        line = source_lines[line_index].strip()
        if re.match(
            r"^(?:copyright\s*)?©\s*\S",
            line,
            re.IGNORECASE,
        ):
            return line_index
    return None


def _is_related_jobs_heading(line: str) -> bool:
    heading = " ".join(line.casefold().rstrip(" :").split())
    return bool(
        re.fullmatch(
            r"(?:similar jobs(?: based on your profile)?|people also viewed|"
            r"show more jobs like this)(?:\s*\(\d+\))?",
            heading,
        )
    )


def _posting_content_lines(raw_text: str) -> list[str]:
    return list(_posting_content_scope(raw_text).lines)


def _posting_content_text(raw_text: str) -> str:
    return _posting_content_scope(raw_text).text


def _strip_timing_note_label(segment: str) -> str:
    label = _field_label(segment)
    if label in {"note", "details"} and ":" in segment:
        return segment.split(":", 1)[1].strip()
    return segment.strip()


_NON_EMPLOYMENT_DATE_FIELD_QUALIFIER_PATTERN = re.compile(
    r"(?:applications?|background\s+check|fiscal\s+year|onboarding|orientation|"
    r"degree|graduation|(?:(?:academic|school|university)\s+)?program|"
    r"training|course|interviews?|postings?|requisition)\s+$",
    flags=re.IGNORECASE,
)
_EMPLOYMENT_TIMING_FIELD_LABELS = (
    _EMPLOYMENT_START_FIELD_LABELS
    | _EMPLOYMENT_END_FIELD_LABELS
    | _EMPLOYMENT_ACADEMIC_TERM_FIELD_LABELS
    | _EMPLOYMENT_SCHEDULE_FIELD_LABELS
    | _EMPLOYMENT_TIMING_CONTEXT_FIELD_LABELS
)
_FLATTENED_TIMING_SCOPE_BARRIER_LABELS = {
    "apply url",
    "email row",
    "indeed source url",
    "resolution evidence",
    "source evidence",
    "source url",
}
_FLATTENED_TIMING_WRAPPER_LABELS = {"details", "note"}


@session_cached
def _employment_timing_content_scope(raw_text: str) -> EmploymentTimingContentScope:
    posting_scope = _posting_content_scope(raw_text)
    posting_text = posting_scope.text
    field_markers = _posting_field_marker_matches(posting_text)
    characters = list(posting_text)
    for marker_index, match in enumerate(field_markers):
        if (
            _normalized_field_marker_label(match)
            not in _FLATTENED_TIMING_SCOPE_BARRIER_LABELS
        ):
            continue
        line_end = posting_text.find("\n", match.end())
        if line_end == -1:
            line_end = len(posting_text)
        next_field_start = next(
            (
                candidate.start()
                for candidate in field_markers[marker_index + 1 :]
                if candidate.start() < line_end
                and _field_marker_starts_new_owned_segment(
                    posting_text,
                    owner=match,
                    candidate=candidate,
                )
            ),
            line_end,
        )
        for index in range(match.start(), next_field_start):
            if characters[index] not in {"\r", "\n"}:
                characters[index] = " "
    timing_text = _posting_content_scope("".join(characters)).text
    return EmploymentTimingContentScope(
        timing_text=timing_text,
        posted_date_text=posting_text,
    )


def _field_marker_starts_new_owned_segment(
    raw_text: str,
    *,
    owner: re.Match[str],
    candidate: re.Match[str],
) -> bool:
    intervening = raw_text[owner.end() : candidate.start()].rstrip()
    if not intervening:
        return False
    delimiter_index = len(intervening) - 1
    if intervening[-1] in {"|", "*", "\u2022"}:
        return True
    if intervening.endswith(" -"):
        return True
    return delimiter_index in _clause_separator_positions(intervening)


def _normalized_field_marker_label(match: re.Match[str]) -> str:
    return " ".join(match.group("label").casefold().split())


def _field_marker_can_follow_flattened_field(
    raw_text: str,
    clause_fields: tuple[re.Match[str], ...],
    match: re.Match[str],
) -> bool:
    label = _normalized_field_marker_label(match)
    if label not in _EMPLOYMENT_TIMING_FIELD_LABELS:
        return True

    barrier_fields = tuple(
        field
        for field in clause_fields
        if _normalized_field_marker_label(field)
        in _FLATTENED_TIMING_SCOPE_BARRIER_LABELS
    )
    if barrier_fields:
        latest_barrier = barrier_fields[-1]
        ownership_was_released = any(
            field.start() > latest_barrier.start()
            and _field_marker_starts_new_owned_segment(
                raw_text,
                owner=latest_barrier,
                candidate=field,
            )
            for field in clause_fields
        )
        if not ownership_was_released and not _field_marker_starts_new_owned_segment(
            raw_text,
            owner=latest_barrier,
            candidate=match,
        ):
            return False

    previous = clause_fields[-1]
    intervening_value = raw_text[previous.end() : match.start()]
    if (
        label in _EMPLOYMENT_START_FIELD_LABELS | _EMPLOYMENT_END_FIELD_LABELS
        and _NON_EMPLOYMENT_DATE_FIELD_QUALIFIER_PATTERN.search(intervening_value)
    ):
        return False

    previous_label = _normalized_field_marker_label(previous)
    if previous_label in _FLATTENED_TIMING_WRAPPER_LABELS:
        return not intervening_value.strip()
    return True


@session_cached
def _posting_field_marker_matches(raw_text: str) -> tuple[re.Match[str], ...]:
    candidates = tuple(_POSTING_FIELD_MARKER_PATTERN.finditer(raw_text))
    accepted: list[re.Match[str]] = []
    for match in candidates:
        clause_start = max(
            raw_text.rfind(separator, 0, match.start())
            for separator in (".", ";", "\n")
        ) + 1
        prefix = raw_text[clause_start : match.start()]
        earlier_clause_candidates = tuple(
            candidate
            for candidate in candidates
            if clause_start <= candidate.start() < match.start()
        )
        embedded_timing_field = bool(
            _normalized_field_marker_label(match) in _EMPLOYMENT_TIMING_FIELD_LABELS
            and len(earlier_clause_candidates) >= 2
        )
        begins_field = bool(
            re.fullmatch(r"\s*(?:(?:[-*|]|\u2022)\s*)?", prefix)
            or re.fullmatch(
                r"\s*parameters\s*(?:and|&)\s*perks!?\s*",
                prefix,
                flags=re.IGNORECASE,
            )
            or embedded_timing_field
        )
        label = _normalized_field_marker_label(match)
        if (
            _is_provenance_field_candidate(label)
            and (
                _provenance_field_semantic(label) is None
                or (not _provenance_field_can_be_inline(label) and not begins_field)
            )
        ):
            continue
        clause_fields = tuple(
            candidate for candidate in accepted if candidate.start() >= clause_start
        )
        follows_flattened_field = bool(clause_fields) and (
            _field_marker_can_follow_flattened_field(raw_text, clause_fields, match)
        )
        if begins_field or follows_flattened_field:
            accepted.append(match)
    return tuple(accepted)


@session_cached
def _posting_field_spans(raw_text: str) -> tuple[PostingFieldSpan, ...]:
    matches = list(_posting_field_marker_matches(raw_text))
    separators = _clause_separator_positions(raw_text)
    fields: list[PostingFieldSpan] = []
    for index, match in enumerate(matches):
        line_end = raw_text.find("\n", match.end())
        if line_end == -1:
            line_end = len(raw_text)
        next_field_start = (
            matches[index + 1].start()
            if index + 1 < len(matches) and matches[index + 1].start() < line_end
            else line_end
        )
        clause_end = min(
            (position for position in separators if match.end() <= position < line_end),
            default=line_end,
        )
        value_end = min(next_field_start, clause_end)
        value = raw_text[match.end() : value_end].strip()
        fields.append(
            PostingFieldSpan(
                label=" ".join(match.group("label").casefold().split()),
                value=value,
                start=match.start(),
                end=value_end,
                value_start=match.end(),
                value_end=value_end,
            )
        )
    return tuple(fields)


def _text_without_labeled_fields(
    raw_text: str,
    fields: tuple[PostingFieldSpan, ...],
) -> str:
    characters = list(raw_text)
    for field in fields:
        for index in range(field.start, field.end):
            if characters[index] not in {"\r", "\n"}:
                characters[index] = " "
    return "".join(characters)


_CLAUSE_PERIOD_ABBREVIATIONS = {
    "approx",
    "dept",
    "dr",
    "etc",
    "hr",
    "hrs",
    "inc",
    "ltd",
    "mr",
    "mrs",
    "ms",
    "st",
    "vs",
}


def _period_is_clause_separator(segment: str, index: int) -> bool:
    previous = segment[index - 1] if index > 0 else ""
    following = segment[index + 1] if index + 1 < len(segment) else ""
    if previous.isdigit() and following.isdigit():
        return False
    if previous.isalpha() and following.isalpha():
        return False

    prefix = segment[max(0, index - 16) : index + 1]
    following_text = segment[index + 1 :].lstrip()
    if re.search(r"(?:\b[A-Za-z]\.){2,}$", prefix):
        return not _abbreviation_continues_clause(None, following_text)

    word_match = re.search(r"(?P<word>[A-Za-z]+)\.$", prefix)
    if not word_match:
        return True
    word = word_match.group("word").casefold()
    if word in MONTH_NAMES and following_text[:1].isdigit():
        return False
    if word in _CLAUSE_PERIOD_ABBREVIATIONS:
        return not _abbreviation_continues_clause(word, following_text)

    measurement_abbreviation = re.search(
        r"(?:\b\d+(?:\.\d+)?\s+|/)[A-Za-z]{1,4}\.$",
        prefix,
    )
    return not (measurement_abbreviation and _abbreviation_continues_clause(word, following_text))


def _abbreviation_continues_clause(
    word: str | None,
    following_text: str,
) -> bool:
    if not following_text:
        return False
    if following_text[0].islower():
        return True
    return bool(word in {"dr", "mr", "mrs", "ms"} and following_text[0].isupper())


def _clause_separator_positions(segment: str) -> list[int]:
    return [
        index
        for index, character in enumerate(segment)
        if character in {";", "\n"}
        or (character == "." and _period_is_clause_separator(segment, index))
    ]
