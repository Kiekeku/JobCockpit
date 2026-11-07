from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


_ONTOLOGY_PATH = Path(__file__).with_name("capability_ontology.yaml")
_COMPATIBILITY_ORDER = (
    "Python",
    "TypeScript",
    "JavaScript",
    "PHP",
    "Laravel",
    "Vue",
    "MySQL",
    "SQL",
    "FastAPI",
    "React",
    "Next.js",
    "Docker",
    "Kubernetes",
    "Spark",
    "Airflow",
    "dbt",
    "Snowflake",
    "Tableau",
    "Power BI",
    "Excel",
    "Cadence Allegro",
    "ECAD",
    "FPGA",
    "PCB Design",
    "Oscilloscope",
    "Verilog",
    "VHDL",
    "Signal Integrity",
    "High-Speed Digital",
    "Analog Circuits",
    "DC/DC Power Circuits",
    "Op-Amp",
    "Transmission Line Theory",
    "Optical Measurements",
    "Test Equipment",
    "SIEM",
    "Splunk",
    "Wireshark",
    "Selenium",
    "Playwright",
    "Java",
    "C#",
    "C++",
    "Rust",
    "Go",
    "R",
    ".NET",
    "Angular",
    "Node.js",
    "Django",
    "Flask",
    "Spring",
    "PostgreSQL",
    "MongoDB",
    "Redis",
    "Kafka",
    "RabbitMQ",
    "AWS",
    "Azure",
    "GCP",
    "Terraform",
    "Git",
    "Linux",
    "PyTorch",
    "TensorFlow",
)
_CONTEXT_BEFORE = re.compile(
    r"(?:background|build|code|configure|debug|deploy|design|develop|development|experience|"
    r"expertise|exposure|familiar|familiarity|framework|implement|integrate|knowledge|maintain|"
    r"migrate|operate|proficiency|proficient|programming|script(?:ing)?|skills?|test|troubleshoot|use|using|work(?:ing)?)\W+"
    r"(?:(?:in|of|to|with)\W+)?"
    r"(?:(?:e\.?\s*g\.?|for\s+example|including|such\s+as)\W+)?"
    r"(?:(?:both|either)\W+)?$",
    re.IGNORECASE,
)
_CONTEXT_AFTER = re.compile(
    r"^\W*(?:apis?|applications?|codebases?|components?|development|experience|expertise|"
    r"exposure|familiarity|framework|knowledge|modules?|proficiency|programming|"
    r"services?|skills?|spreadsheets?|stack|workbooks?|"
    r"(?:(?:are|is|was|were)\s+|(?:must|should|will|would)\s+be\s+)?"
    r"(?:required|preferred|desired|advantageous))\b",
    re.IGNORECASE,
)
_TITLE_ROLE_HEAD = re.compile(
    r"\b(?:administrators?|analysts?|architects?|consultants?|developers?|engineers?|"
    r"interns?|programmers?|scientists?|specialists?)\b",
    re.IGNORECASE,
)
_TITLE_CONTEXT_WORD = re.compile(r"[A-Za-z0-9]+")
_TITLE_COORDINATION_QUALIFIERS = {"and", "or"}
_TITLE_LEVEL_QUALIFIERS = {
    "associate",
    "junior",
    "lead",
    "principal",
    "senior",
    "staff",
}
_TITLE_TECHNICAL_AREA_QUALIFIERS = {
    "application",
    "backend",
    "cloud",
    "data",
    "database",
    "devops",
    "embedded",
    "end",
    "front",
    "frontend",
    "full",
    "fullstack",
    "mobile",
    "native",
    "platform",
    "reporting",
    "software",
    "stack",
    "system",
    "systems",
    "ui",
    "ux",
    "web",
}
_TITLE_PLATFORM_QUALIFIERS = {"android", "ios"}
_TITLE_CONTEXT_QUALIFIERS = frozenset(
    _TITLE_COORDINATION_QUALIFIERS
    | _TITLE_LEVEL_QUALIFIERS
    | _TITLE_PLATFORM_QUALIFIERS
    | _TITLE_TECHNICAL_AREA_QUALIFIERS
)
_CONTEXT_BOUNDARY = re.compile(
    r"[\r\n!?;|\u2022\u25cf\u25aa]|\.(?=\s|$)"
)
_COORDINATED_TECHNOLOGY_GAP = re.compile(
    r"^\s*(?:&|(?:[,/]\s*)?(?:(?:and|or)(?:\s*/\s*(?:and|or))?)?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TechnologyDefinition:
    canonical_name: str
    entity_type: str
    patterns: tuple[str, ...]
    non_technology_patterns: tuple[str, ...]
    capability_context_patterns: tuple[str, ...]
    context_free_patterns: tuple[str, ...]
    domains: tuple[str, ...]
    include_in_tech_stack: bool = True
    requires_context: bool = False
    title_requires_domain_context: bool = False


@dataclass(frozen=True)
class TechnologySourceMatch:
    surface_text: str
    canonical_name: str
    entity_type: str
    start: int
    end: int


@lru_cache(maxsize=1)
def load_capability_ontology() -> tuple[TechnologyDefinition, ...]:
    payload = yaml.safe_load(_ONTOLOGY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported capability ontology schema")
    raw_technologies = payload.get("technologies")
    if not isinstance(raw_technologies, list) or not raw_technologies:
        raise RuntimeError("Capability ontology must contain technologies")

    definitions: list[TechnologyDefinition] = []
    names: set[str] = set()
    for raw in raw_technologies:
        if not isinstance(raw, dict):
            raise RuntimeError("Capability ontology entries must be mappings")
        name = str(raw.get("canonical_name") or "").strip()
        patterns = tuple(str(item) for item in raw.get("patterns") or [])
        non_technology_patterns = tuple(
            str(item) for item in raw.get("non_technology_patterns") or []
        )
        capability_context_patterns = tuple(
            str(item) for item in raw.get("capability_context_patterns") or []
        )
        context_free_patterns = tuple(
            str(item) for item in raw.get("context_free_patterns") or []
        )
        domains = tuple(str(item) for item in raw.get("domains") or [])
        entity_type = str(raw.get("entity_type") or "unknown")
        if not name or not patterns or not domains:
            raise RuntimeError(f"Incomplete capability ontology entry: {name or '<unnamed>'}")
        if name.casefold() in names:
            raise RuntimeError(f"Duplicate capability ontology name: {name}")
        for pattern in (
            *patterns,
            *non_technology_patterns,
            *capability_context_patterns,
            *context_free_patterns,
        ):
            re.compile(pattern, re.IGNORECASE)
        names.add(name.casefold())
        definitions.append(
            TechnologyDefinition(
                canonical_name=name,
                entity_type=entity_type,
                patterns=patterns,
                non_technology_patterns=non_technology_patterns,
                capability_context_patterns=capability_context_patterns,
                context_free_patterns=context_free_patterns,
                domains=domains,
                include_in_tech_stack=bool(raw.get("include_in_tech_stack", True)),
                requires_context=bool(raw.get("requires_context", False)),
                title_requires_domain_context=bool(
                    raw.get("title_requires_domain_context", False)
                ),
            )
        )
    compatibility_index = {
        name: index for index, name in enumerate(_COMPATIBILITY_ORDER)
    }
    return tuple(
        sorted(
            definitions,
            key=lambda item: (
                compatibility_index.get(item.canonical_name, len(compatibility_index)),
                definitions.index(item),
            ),
        )
    )


def technology_pattern_rows() -> tuple[tuple[str, str], ...]:
    return tuple(
        (definition.canonical_name, pattern)
        for definition in load_capability_ontology()
        for pattern in definition.patterns
    )


def tech_stack_pattern_rows() -> tuple[tuple[str, str], ...]:
    return tuple(
        (definition.canonical_name, pattern)
        for definition in load_capability_ontology()
        if definition.include_in_tech_stack
        for pattern in definition.patterns
    )


def non_technical_capability_pattern_rows() -> tuple[tuple[str, str], ...]:
    return tuple(
        (definition.canonical_name, pattern)
        for definition in load_capability_ontology()
        if not definition.include_in_tech_stack
        for pattern in definition.patterns
    )


def technology_domains() -> dict[str, tuple[str, ...]]:
    return {
        definition.canonical_name: definition.domains
        for definition in load_capability_ontology()
    }


def technology_entity_type(canonical_name: str) -> str:
    normalized = canonical_name.casefold()
    for definition in load_capability_ontology():
        if definition.canonical_name.casefold() == normalized:
            return definition.entity_type
    return "unknown"


def technology_source_match(
    canonical_name: str,
    raw_text: str,
    *,
    assume_capability_context: bool = False,
) -> TechnologySourceMatch | None:
    definition = next(
        (
            item
            for item in load_capability_ontology()
            if item.canonical_name.casefold() == canonical_name.casefold()
        ),
        None,
    )
    if definition is None:
        return None

    matches = [
        match
        for pattern in definition.patterns
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        if technology_is_supported(
            definition.canonical_name,
            raw_text,
            match,
            assume_capability_context=assume_capability_context,
        )
    ]
    if not matches:
        return None
    match = min(matches, key=lambda item: (item.start(), -(item.end() - item.start())))
    return TechnologySourceMatch(
        surface_text=match.group(0),
        canonical_name=definition.canonical_name,
        entity_type=definition.entity_type,
        start=match.start(),
        end=match.end(),
    )


def technology_is_supported(
    canonical_name: str,
    raw_text: str,
    match: re.Match[str],
    *,
    assume_capability_context: bool = False,
    assume_title_context: bool = False,
    title_supporting_domains: Collection[str] = (),
    require_candidate_scope: bool = False,
) -> bool:
    definition = next(
        item
        for item in load_capability_ontology()
        if item.canonical_name == canonical_name
    )
    if _technology_match_has_non_technology_usage(definition, raw_text, match):
        return False
    if assume_capability_context:
        return True
    if assume_title_context:
        if not _technology_match_has_title_context(raw_text, match):
            return False
        if not definition.title_requires_domain_context:
            return True
        if _technology_match_has_context_free_surface(definition, match):
            return True
        supported_domains = {domain.casefold() for domain in title_supporting_domains}
        return any(domain.casefold() in supported_domains for domain in definition.domains)
    if not definition.requires_context:
        return True
    candidate_surface_scope = (
        not require_candidate_scope
        or _technology_surface_has_candidate_scope(raw_text, match)
    )
    if (
        _technology_match_has_context_free_surface(definition, match)
        and candidate_surface_scope
    ):
        return True
    if _technology_match_has_capability_usage(
        definition,
        raw_text,
        match,
        require_candidate_scope=require_candidate_scope,
    ):
        return True
    if candidate_surface_scope and _technology_match_has_direct_context(raw_text, match):
        return True
    return bool(
        candidate_surface_scope
        and _coordinated_technology_group_has_context(
            canonical_name,
            raw_text,
            match,
        )
    )


def _technology_match_has_context_free_surface(
    definition: TechnologyDefinition,
    match: re.Match[str],
) -> bool:
    surface = match.group(0)
    return any(
        re.fullmatch(pattern, surface, flags=re.IGNORECASE)
        for pattern in definition.context_free_patterns
    )


def _technology_match_has_non_technology_usage(
    definition: TechnologyDefinition,
    raw_text: str,
    match: re.Match[str],
) -> bool:
    return any(
        usage.start() <= match.start() and match.end() <= usage.end()
        for pattern in definition.non_technology_patterns
        for usage in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
    )


def _technology_match_has_capability_usage(
    definition: TechnologyDefinition,
    raw_text: str,
    match: re.Match[str],
    *,
    require_candidate_scope: bool = False,
) -> bool:
    for pattern in definition.capability_context_patterns:
        for usage in re.finditer(pattern, raw_text, re.IGNORECASE):
            if not (usage.start() <= match.start() and match.end() <= usage.end()):
                continue
            if not require_candidate_scope or _capability_usage_has_candidate_scope(
                raw_text,
                usage,
            ):
                return True
    return False


def _capability_usage_has_candidate_scope(
    raw_text: str,
    usage: re.Match[str],
) -> bool:
    return _capability_prefix_has_candidate_scope(raw_text, usage.start())


def _technology_surface_has_candidate_scope(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    return _capability_prefix_has_candidate_scope(raw_text, match.start())


def _capability_prefix_has_candidate_scope(raw_text: str, end: int) -> bool:
    prefix_start = max(
        (boundary.end() for boundary in _CONTEXT_BOUNDARY.finditer(raw_text, 0, end)),
        default=0,
    )
    prefix = raw_text[prefix_start:end].strip()
    if not prefix:
        return True
    normalized = " ".join(re.findall(r"[a-z0-9]+", prefix.casefold()))
    if not normalized:
        return True
    if re.search(
        r"\b(?:applicants?|candidates?|employees?|incumbents?|individuals?|"
        r"persons?|you|your)\b",
        normalized,
    ):
        return True
    if re.search(
        r"\b(?:abilities|ability|background|experience|expertise|familiarity|"
        r"knowledge|proficienc(?:y|ies)|skills?)\b",
        normalized,
    ):
        return True
    return bool(
        re.match(
            r"^(?:must|need(?:s)? to|required to|shall|should|will|would)\b",
            normalized,
        )
    )


def _technology_match_has_direct_context(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    return _technology_span_has_direct_context(raw_text, match.start(), match.end())


def _technology_match_has_title_context(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    after = raw_text[match.end() :]
    role_head = _TITLE_ROLE_HEAD.search(after)
    if role_head is not None and _title_context_segment_is_supported(
        raw_text[: match.start()]
    ) and _title_context_segment_is_supported(after[: role_head.start()]):
        return True

    before = raw_text[: match.start()]
    preceding_role_heads = tuple(_TITLE_ROLE_HEAD.finditer(before))
    return any(
        _title_context_segment_is_supported(before[: preceding.start()])
        and _title_context_segment_is_supported(before[preceding.end() :])
        and _title_context_segment_is_supported(after)
        for preceding in reversed(preceding_role_heads)
    )


def _title_context_segment_is_supported(value: str) -> bool:
    if not value.strip():
        return True

    occupied: list[tuple[int, int]] = []
    for definition in load_capability_ontology():
        for pattern in definition.patterns:
            for candidate in re.finditer(pattern, value, flags=re.IGNORECASE):
                if any(
                    candidate.start() < end and start < candidate.end()
                    for start, end in occupied
                ):
                    continue
                occupied.append((candidate.start(), candidate.end()))
    residual = list(value)
    for start, end in occupied:
        residual[start:end] = " " * (end - start)
    words = {
        match.group(0).casefold()
        for match in _TITLE_CONTEXT_WORD.finditer("".join(residual))
    }
    return words <= _TITLE_CONTEXT_QUALIFIERS


def _technology_span_has_direct_context(
    raw_text: str,
    start: int,
    end: int,
) -> bool:
    before = raw_text[max(0, start - 80) : start]
    after = raw_text[end : min(len(raw_text), end + 80)]
    before = _CONTEXT_BOUNDARY.split(before)[-1]
    after = _CONTEXT_BOUNDARY.split(after)[0]
    return bool(_CONTEXT_BEFORE.search(before) or _CONTEXT_AFTER.search(after))


def _coordinated_technology_group_has_context(
    canonical_name: str,
    raw_text: str,
    target: re.Match[str],
) -> bool:
    preceding_boundaries = tuple(
        _CONTEXT_BOUNDARY.finditer(raw_text, 0, target.start())
    )
    clause_start = preceding_boundaries[-1].end() if preceding_boundaries else 0
    following_boundary = _CONTEXT_BOUNDARY.search(raw_text, target.end())
    clause_end = following_boundary.start() if following_boundary else len(raw_text)
    candidates = [
        (
            definition.canonical_name,
            clause_start + candidate.start(),
            clause_start + candidate.end(),
        )
        for definition in load_capability_ontology()
        for pattern in definition.patterns
        for candidate in re.finditer(
            pattern,
            raw_text[clause_start:clause_end],
            flags=re.IGNORECASE,
        )
    ]
    candidates.sort(key=lambda item: (item[1], -(item[2] - item[1])))

    non_overlapping: list[tuple[str, int, int]] = []
    for candidate in candidates:
        if non_overlapping and candidate[1] < non_overlapping[-1][2]:
            continue
        non_overlapping.append(candidate)

    target_index = next(
        (
            index
            for index, (name, start, end) in enumerate(non_overlapping)
            if name == canonical_name and start == target.start() and end == target.end()
        ),
        None,
    )
    if target_index is None:
        return False

    group_start = target_index
    while group_start > 0:
        previous = non_overlapping[group_start - 1]
        current = non_overlapping[group_start]
        if not _COORDINATED_TECHNOLOGY_GAP.fullmatch(raw_text[previous[2] : current[1]]):
            break
        group_start -= 1

    group_end = target_index + 1
    while group_end < len(non_overlapping):
        previous = non_overlapping[group_end - 1]
        current = non_overlapping[group_end]
        if not _COORDINATED_TECHNOLOGY_GAP.fullmatch(raw_text[previous[2] : current[1]]):
            break
        group_end += 1

    return any(
        _technology_span_has_direct_context(raw_text, start, end)
        for _name, start, end in non_overlapping[group_start:group_end]
    )
