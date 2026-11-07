from __future__ import annotations

import re


FIELD_BOUNDARY_LABELS = (
    "Apply URL",
    "Company",
    "Country",
    "Date Posted",
    "Department",
    "Duration",
    "Email Card",
    "Email Row",
    "Employment Type",
    "End Date",
    "Hours",
    "Hours of Work",
    "Hours per Week",
    "Heures par semaine",
    "Nombre d’heures par semaine",
    "Indeed Source URL",
    "Job Type",
    "Job Title",
    "Location",
    "Official Discovery Error",
    "Official Discovery Status",
    "Page Title",
    "Position",
    "Posted",
    "Posted Date",
    "Posted On",
    "Resolution Evidence",
    "Role",
    "Schedule",
    "Schedule Type",
    "Scheduled Weekly Hours",
    "Source Evidence",
    "Source Resolution Kind",
    "Source URL",
    "Start Date",
    "Title",
    "URL",
    "Work Arrangement",
    "Work Hours",
    "Working Hours",
    "Work Schedule",
    "Work Type",
    "Worker Type",
)
EMPLOYMENT_HOURS_FIELD_LABELS = (
    "Hours",
    "Hours of Work",
    "Hours per Week",
    "Heures par semaine",
    "Nombre d’heures par semaine",
    "Work Hours",
    "Working Hours",
    "Work Schedule",
    "Scheduled Weekly Hours",
)
FIELD_BOUNDARY_PATTERN = "|".join(re.escape(label) for label in FIELD_BOUNDARY_LABELS)
FIELD_BOUNDARY_LABEL_KEYS = {label.casefold() for label in FIELD_BOUNDARY_LABELS}
LIST_MARKER_PATTERN = r"(?:[-*\u2022]|\d+[.)])"

# Source adapters preserve discovery metadata as qualified fields such as
# ``LinkedIn Hint Role`` and ``ATS Eluta Company``.  Treat the source qualifier
# as provenance and the terminal noun as the field's semantic type.  Parsers
# can then consume or exclude the whole family without knowing every adapter.
_PROVENANCE_FIELD_PREFIX_PATTERN = (
    r"(?:(?:linkedin|indeed|github|gmail|outlook|ats)"
    r"(?:\s+[a-z0-9_-]+){0,2}|"
    r"ats\s+canadian\s+tech\s+internships(?:\s+\d{4})?)"
)
_PROVENANCE_FIELD_CANDIDATE_PREFIX_PATTERN = (
    r"(?:linkedin|indeed|github|gmail|outlook|ats)"
    r"(?:\s+[a-z0-9_-]+){0,4}"
)
_PROVENANCE_FIELD_LABEL_PATTERN = re.compile(
    rf"^{_PROVENANCE_FIELD_PREFIX_PATTERN}\s+"
    r"(?P<semantic>role|company|location|date\s+posted|"
    r"apply\s+url|source\s+url|url)$",
    flags=re.IGNORECASE,
)
_PROVENANCE_FIELD_MARKER_PATTERN = (
    rf"{_PROVENANCE_FIELD_PREFIX_PATTERN}\s+"
    r"(?:role|company|location|date\s+posted|apply\s+url|source\s+url|url)"
)
_PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN = (
    rf"{_PROVENANCE_FIELD_CANDIDATE_PREFIX_PATTERN}\s+"
    r"(?:role|company|location|date\s+posted|apply\s+url|source\s+url|url)"
)
_INLINE_PROVENANCE_FIELD_LABEL_PATTERN = re.compile(
    r"^(?:linkedin|indeed|github|gmail|outlook|ats)"
    r"(?:\s+[a-z0-9_-]+){0,2}\s+"
    r"(?:role|company|location|date\s+posted|apply\s+url|source\s+url|url)$",
    flags=re.IGNORECASE,
)


def _field_value(
    lines: list[str],
    *labels: str,
    allow_following_line: bool = False,
) -> str | None:
    values = _field_values(
        lines,
        *labels,
        allow_following_line=allow_following_line,
    )
    return values[0] if values else None


def _field_values(
    lines: list[str],
    *labels: str,
    allow_following_line: bool = False,
) -> tuple[str, ...]:
    values: list[str] = []
    for index, line in enumerate(lines):
        line_label = _field_label(_without_list_marker(line))
        for label in labels:
            label_key = label.casefold()
            if label_key == "title" and line_label in {"job title", "page title"}:
                continue
            match = re.search(
                rf"(?:^|\s){re.escape(label)}:\s*",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            if re.search(
                rf"(?:^|\s){_PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN}\s*:\s*$",
                line[: match.end()],
                flags=re.IGNORECASE,
            ):
                continue
            if (
                line_label != label_key
                and line_label not in FIELD_BOUNDARY_LABEL_KEYS
                and not _is_list_marker_prefix(line[: match.start()])
            ):
                continue
            value_start = match.end()
            value_end = _next_field_boundary(line, value_start)
            value = _clean_field_value(line[value_start:value_end])
            if value:
                values.append(value)
                break
            if allow_following_line and index + 1 < len(lines):
                following_line = _without_list_marker(lines[index + 1])
                if not _field_label(following_line):
                    value = _clean_field_value(following_line)
                    if value:
                        values.append(value)
                        break
    return tuple(values)


def _is_list_marker_prefix(value: str) -> bool:
    return re.fullmatch(rf"\s*(?:{LIST_MARKER_PATTERN})?\s*", value) is not None


def _without_list_marker(value: str) -> str:
    return re.sub(rf"^\s*{LIST_MARKER_PATTERN}\s+", "", value, count=1)


def _field_label(line: str) -> str:
    if ":" not in line:
        return ""
    label = line.split(":", 1)[0].strip().casefold()
    if label in FIELD_BOUNDARY_LABEL_KEYS:
        return label
    if _PROVENANCE_FIELD_LABEL_PATTERN.fullmatch(label):
        return label
    return label if re.fullmatch(r"[a-z][a-z\s_-]{0,40}", label) else ""


def _provenance_field_semantic(label: str) -> str | None:
    match = _PROVENANCE_FIELD_LABEL_PATTERN.fullmatch(label.strip())
    return " ".join(match.group("semantic").casefold().split()) if match else None


def _provenance_field_can_be_inline(label: str) -> bool:
    return _INLINE_PROVENANCE_FIELD_LABEL_PATTERN.fullmatch(label.strip()) is not None


def _is_provenance_field_candidate(label: str) -> bool:
    return (
        re.fullmatch(
            _PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN,
            label.strip(),
            flags=re.IGNORECASE,
        )
        is not None
    )


def _next_field_boundary(line: str, value_start: int) -> int:
    for match in re.finditer(
        rf"\s+(?P<label>{_PROVENANCE_FIELD_CANDIDATE_MARKER_PATTERN}|"
        rf"{FIELD_BOUNDARY_PATTERN})\s*:",
        line[value_start:],
        flags=re.IGNORECASE,
    ):
        candidate_label = match.group("label")
        provenance_semantic = _provenance_field_semantic(candidate_label)
        if (
            _is_provenance_field_candidate(candidate_label)
            and (
                provenance_semantic is None
                or not _provenance_field_can_be_inline(candidate_label)
            )
        ):
            continue
        return value_start + match.start()
    return len(line)


def _provenance_field_value(lines: list[str], semantic: str) -> str | None:
    semantic_key = " ".join(semantic.casefold().split())
    for line in lines:
        unmarked = _without_list_marker(line)
        for match in re.finditer(
            rf"(?:^|\s)(?P<label>{_PROVENANCE_FIELD_MARKER_PATTERN})\s*:\s*",
            unmarked,
            flags=re.IGNORECASE,
        ):
            prefix = unmarked[: match.start()]
            if prefix.strip() and not _is_structured_field_label(_field_label(unmarked)):
                continue
            if prefix.strip() and not _provenance_field_can_be_inline(match.group("label")):
                continue
            if _provenance_field_semantic(match.group("label")) != semantic_key:
                continue
            value_end = _next_field_boundary(unmarked, match.end())
            value = _clean_field_value(unmarked[match.end() : value_end])
            if value:
                return value
    return None


def _is_structured_field_label(label: str) -> bool:
    return label in FIELD_BOUNDARY_LABEL_KEYS or _provenance_field_semantic(label) is not None


def _clean_field_value(value: str) -> str:
    return " ".join(value.strip(" -|").split())
