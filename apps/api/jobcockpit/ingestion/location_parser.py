from __future__ import annotations

import ast
from dataclasses import dataclass
import re

from jobcockpit.ingestion.parser_models import ParsedLocation
from jobcockpit.ingestion.posting_fields import (
    FIELD_BOUNDARY_LABELS,
    _clean_field_value,
    _field_label,
)

CANADIAN_PROVINCE_CODES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}
CANADIAN_PROVINCES = set(CANADIAN_PROVINCE_CODES.values())
CANADIAN_PROVINCE_ALIASES = {
    "ONT": "Ontario",
    "CA-ON": "Ontario",
    "CAN-ON": "Ontario",
}
COUNTRY_CODES = {
    "CAN": "Canada",
    "CAD": "Canada",
    "GB": "United Kingdom",
    "GBR": "United Kingdom",
    "UK": "United Kingdom",
    "US": "United States",
    "USA": "United States",
}
AMBIGUOUS_COUNTRY_CODES = {"CA": "Canada"}
COUNTRY_ALIASES = {
    "australia": "Australia",
    "canada": "Canada",
    "gb": "United Kingdom",
    "gbr": "United Kingdom",
    "ireland": "Ireland",
    "pakistan": "Pakistan",
    "poland": "Poland",
    "u.k.": "United Kingdom",
    "uk": "United Kingdom",
    "united kingdom": "United Kingdom",
    "united states": "United States",
    "united states of america": "United States",
    "us": "United States",
    "u.s.": "United States",
    "usa": "United States",
}
US_STATE_CODES = {
    "AK",
    "AL",
    "AR",
    "AZ",
    "CA",
    "CO",
    "CT",
    "DC",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
}
US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}

_LOCATION_LABEL_ALIASES = {
    "job location": "Job Location",
    "location": "Location",
    "office location": "Office Location",
    "work location": "Work Location",
}
_TRUSTED_INLINE_OWNERS = {
    "company",
    "email row",
    "job title",
    "page title",
    "position",
    "role",
    "title",
}
_METADATA_LABEL_TERMS = {
    "application",
    "apply",
    "benefits",
    "category",
    "company",
    "compensation",
    "contract",
    "deadline",
    "description",
    "duration",
    "email",
    "employment",
    "hours",
    "job",
    "posted",
    "position",
    "qualifications",
    "requirements",
    "resolution",
    "role",
    "salary",
    "schedule",
    "source",
    "stage",
    "start",
    "term",
    "title",
    "url",
}
_METADATA_LABEL_TERMS.update(label.casefold().split()[0] for label in FIELD_BOUNDARY_LABELS)
_KNOWN_METADATA_LABELS = {
    " ".join(label.casefold().replace("_", " ").replace("-", " ").split())
    for label in FIELD_BOUNDARY_LABELS
}
_INLINE_LABEL_PATTERN = re.compile(
    rf"\s+(?P<label>(?:{'|'.join(sorted(_METADATA_LABEL_TERMS))})\b"
    r"[A-Za-z0-9 /_-]{0,32}):(?=\s|$)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class _LocationCandidate:
    primary_label: str
    country: str | None
    work_arrangement: str
    remote: bool


@dataclass(frozen=True)
class _LocationTokens:
    components: tuple[str, ...]
    country: str | None
    canadian_context: bool


@dataclass(frozen=True)
class _RemotePolicyAssertion:
    polarity: str
    targets: frozenset[str]


def extract_location_value(
    lines: list[str],
    *,
    page_title_location: str | None,
    blocked_page: bool,
) -> str | None:
    non_evidence_lines = [line for line in lines if _field_label(line) != "source evidence"]
    owned = _owned_location_value(non_evidence_lines)
    structured_page_title = _is_plausible_location_value(page_title_location)
    if _is_plausible_location_value(owned) and not (
        structured_page_title and _looks_like_location_prose(owned)
    ):
        return owned

    heading = _location_after_heading(non_evidence_lines)
    if heading:
        return heading

    if _is_plausible_location_value(page_title_location):
        return page_title_location

    evidence = _source_evidence_location_value(lines)
    if _is_plausible_location_value(evidence):
        return evidence

    if blocked_page:
        fallback = _first_labeled_location(lines)
        if _is_plausible_location_value(fallback):
            return fallback
    return None


def extract_country_value(lines: list[str]) -> str | None:
    non_evidence_lines = [line for line in lines if _field_label(line) != "source evidence"]
    for line in non_evidence_lines:
        if _field_label(line) != "country":
            continue
        if country := _recognized_country_value(_extract_labeled_value(line, "Country")):
            return country

    inline_owner_labels = {*_TRUSTED_INLINE_OWNERS, *_LOCATION_LABEL_ALIASES}
    for line in non_evidence_lines:
        if _field_label(line) not in inline_owner_labels:
            continue
        if country := _recognized_country_value(_extract_labeled_value(line, "Country")):
            return country

    for line in lines:
        if _field_label(line) != "source evidence":
            continue
        if country := _recognized_country_value(_extract_labeled_value(line, "Country")):
            return country
    return None


def _recognized_country_value(value: str | None) -> str | None:
    if not value:
        return None
    return _recognized_country_name(value, allow_ambiguous_code=True)


def looks_like_comma_separated_location(value: str) -> bool:
    components = [_clean_component(part) for part in value.split(",")]
    components = [component for component in components if component]
    if len(components) < 2:
        return False
    if _recognized_canadian_region(components[1]):
        return True
    if _recognized_us_region(components[1]):
        return True

    return _recognized_country_name(components[-1], allow_ambiguous_code=True) is not None


def parse_location(
    location_label: str | None,
    lower_text: str,
    *,
    source_type: str | None = None,
    country_label: str | None = None,
) -> ParsedLocation:
    owned_hybrid = bool(re.search(
        r"\bremote\s+(?:position|type)\s*:\s*hybrid\b|"
        r"\bhybrid\s*:\s*you\s+must\s+be\s+in\s+the\s+[^.;\n]{1,70}"
        r"\boffice\s+at\s+least\s+three\s+days\s+a\s+week\b", lower_text, re.I,
    ))
    # Conflicting role-specific on-site prose leaves the arrangement unresolved.
    hybrid_conflict = owned_hybrid and bool(re.search(
        r"\bfull[- ]time\s+on[- ]site\s+at\s+the\b", lower_text, re.I,
    ))
    remote_policy = _remote_policy_decision(lower_text)
    body_remote = remote_policy if remote_policy is not None else "remote" in lower_text
    if owned_hybrid:
        body_remote = False
    explicit_country = _recognized_country_value(country_label)
    candidate = _candidate_from_label(
        location_label,
        body_remote=body_remote,
        body_text=lower_text,
        explicit_country=explicit_country,
    )
    if candidate is None:
        country = explicit_country or ("Canada" if "canada" in lower_text else None)
        return ParsedLocation(
            work_arrangement=("unknown" if hybrid_conflict else "hybrid") if owned_hybrid
            else "remote" if body_remote else "unknown",
            country=country,
            remote_eligibility="Canada" if body_remote and country == "Canada" else None,
        )

    tokens = _tokenize_candidate(candidate, source_type=source_type)
    city, province_state = _classify_locality(tokens)
    country = tokens.country
    if not country and not _is_us_region(province_state) and "canada" in lower_text:
        country = "Canada"
    if not country and province_state in CANADIAN_PROVINCES:
        country = "Canada"
    return ParsedLocation(
        work_arrangement=("unknown" if hybrid_conflict else "hybrid") if owned_hybrid
        else candidate.work_arrangement,
        city=city,
        province_state=province_state,
        country=country,
        remote_eligibility=("Canada" if candidate.remote and not owned_hybrid
                            and country == "Canada" else None),
    )


def _owned_location_value(lines: list[str]) -> str | None:
    for line in lines:
        label = _field_label(line)
        field_name = _LOCATION_LABEL_ALIASES.get(label)
        if field_name:
            value = _extract_labeled_value(line, field_name)
            if value:
                return value
    for line in lines:
        if _field_label(line) not in _TRUSTED_INLINE_OWNERS:
            continue
        value = _extract_labeled_value(line, "Location")
        if value:
            return value
    return None


def _first_labeled_location(lines: list[str]) -> str | None:
    for line in lines:
        value = _extract_labeled_value(line, "Location")
        if value:
            return value
    return None


def _source_evidence_location_value(lines: list[str]) -> str | None:
    for line in lines:
        if _field_label(line) != "source evidence":
            continue
        value = _extract_labeled_value(line, "Location")
        if value:
            return value
    return None


def _extract_labeled_value(line: str, label: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(label)}:\s*",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = _truncate_inline_metadata(line[match.end() :])
    cleaned = _clean_field_value(value)
    return cleaned or None


def _truncate_inline_metadata(value: str) -> str:
    for match in _INLINE_LABEL_PATTERN.finditer(value):
        if _is_metadata_label(match.group("label")):
            return value[: match.start()].rstrip(" -|·")
    return value


def _is_metadata_label(value: str) -> bool:
    normalized = " ".join(value.casefold().replace("_", " ").replace("-", " ").split())
    if normalized in _KNOWN_METADATA_LABELS:
        return True
    return any(token in _METADATA_LABEL_TERMS for token in normalized.split())


def _is_plausible_location_value(value: str | None) -> bool:
    return bool(value and len(value) <= 160 and len(value.split()) <= 20)


def _looks_like_location_prose(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    sentence_subject = re.match(
        r"^(?:this|the|our|your|we|you|candidate|job|position|role)\b",
        normalized,
    )
    sentence_verb = re.search(
        r"\b(?:are|based|be|is|located|offers?|requires?|work|working)\b",
        normalized,
    )
    if sentence_subject and sentence_verb:
        return True
    if looks_like_comma_separated_location(value):
        return False

    location, arrangement = _location_label_and_arrangement(value)
    if arrangement is not None or _recognized_country_name(location) is not None:
        return False
    return bool(
        len(normalized.split()) >= 6 and value.rstrip().endswith((".", "!", "?"))
    )


def _location_after_heading(lines: list[str]) -> str | None:
    for index, line in enumerate(lines[:-1]):
        heading = " ".join(line.casefold().rstrip(":").split())
        if heading not in {"location", "locations"}:
            continue
        candidate = _truncate_inline_metadata(lines[index + 1].strip())
        if looks_like_comma_separated_location(candidate):
            return candidate
    return None


def _candidate_from_label(
    location_label: str | None,
    *,
    body_remote: bool,
    body_text: str,
    explicit_country: str | None,
) -> _LocationCandidate | None:
    if not location_label:
        return None
    bounded_label = _truncate_inline_metadata(_normalize_serialized_country(location_label)).strip()
    if not bounded_label:
        return None

    primary_label = re.split(r";|\s+/\s+", bounded_label, maxsplit=1)[0].strip()
    primary_label = _unwrap_location_relation(primary_label)
    prefixed_country, primary_label = _split_country_prefix(primary_label)
    country = explicit_country or prefixed_country
    if country == "Canada":
        primary_label = _canadian_country_path_locality(primary_label)

    primary_label, label_arrangement = _location_label_and_arrangement(primary_label)
    work_arrangement = "remote" if body_remote else "unknown"
    remote = body_remote
    if label_arrangement:
        has_remote_alternative = _has_affirmative_remote_alternative(
            bounded_label
        ) or _has_affirmative_remote_policy(body_text)
        if label_arrangement == "remote" or has_remote_alternative:
            remote = True
            work_arrangement = "remote"
        else:
            remote = False
            work_arrangement = label_arrangement
    return _LocationCandidate(
        primary_label=primary_label,
        country=country,
        work_arrangement=work_arrangement,
        remote=remote,
    )


def _normalize_serialized_country(value: str) -> str:
    """Recover Country objects stringified by older generic apply imports."""
    def replace_country(match: re.Match[str]) -> str:
        try:
            country = ast.literal_eval(match.group())
        except (SyntaxError, ValueError):
            return match.group()
        if not isinstance(country, dict) or country.get("@type") != "Country":
            return match.group()
        name = country.get("name")
        normalized = (
            _recognized_country_name(name, allow_ambiguous_code=True)
            if isinstance(name, str) else None
        )
        # Some saved regions already contain the country (e.g. "ON,CA").
        previous = value[:match.start()].rstrip(" ,").rsplit(",", 1)[-1].strip()
        if normalized and _recognized_country_name(previous, allow_ambiguous_code=True) == normalized:
            return ""
        return normalized or match.group()

    return re.sub(r"\{[^{}\n]{1,256}\}", replace_country, value)


def _unwrap_location_relation(value: str) -> str:
    current = value.strip()
    while match := re.fullmatch(
        r"(?i)(?:based|located)\s+in\s+(?P<location>.+)",
        current,
    ):
        current = match.group("location").strip()
    return current


def _split_country_prefix(value: str) -> tuple[str | None, str]:
    match = re.fullmatch(
        r"(?P<prefix>.+?)\s+[\u2013\u2014-]\s+(?P<remainder>.+)",
        value,
    )
    if not match:
        return None, value
    country = _recognized_country_name(match.group("prefix"))
    if not country:
        return None, value
    return country, match.group("remainder").strip()


def _canadian_country_path_locality(value: str) -> str:
    country_path = re.fullmatch(
        r"(?i)(?P<region>[^\u2013\u2014-]+?)\s+[\u2013\u2014-]\s+"
        r"(?P<city>.+?)(?:,\s*(?:CA|CAN|Canada))?",
        value,
    )
    if not country_path or not _recognized_canadian_region(country_path.group("region")):
        return value
    return f"{country_path.group('city')}, {country_path.group('region')}"


def _tokenize_candidate(
    candidate: _LocationCandidate,
    *,
    source_type: str | None,
) -> _LocationTokens:
    components = [_clean_component(part) for part in candidate.primary_label.split(",")]
    components = [component for component in components if component]
    while components and _is_trailing_location_metadata(components[-1]):
        components.pop()

    explicit_canadian_context = (
        candidate.country == "Canada"
        or (
            candidate.country is None
            and _has_strong_canadian_source_hint(source_type)
        )
        or any(re.search(r"\bCAD\b", part, flags=re.IGNORECASE) for part in components)
    )
    us_city_state_tail = _has_us_city_state_tail(components)
    inferred_canadian_context = not us_city_state_tail and (
        any(_is_canadian_region_code(part) for part in components[:-1])
        or (
            len(components) >= 3
            and any(_recognized_canadian_region(part) is not None for part in components[:-1])
        )
    )
    canadian_context = explicit_canadian_context or inferred_canadian_context

    country = candidate.country
    country_match = _country_component_match(
        components,
        allow_ambiguous_code=False,
    )
    if country_match is None and canadian_context:
        country_match = _country_component_match(
            components,
            allow_ambiguous_code=True,
        )
    if country_match:
        country_index, matched_country = country_match
        country = country or matched_country
        if country_index == 0:
            components = components[1:]
            while (
                components
                and _recognized_country_name(
                    components[0],
                    allow_ambiguous_code=canadian_context or country == "Canada",
                )
                == country
            ):
                components.pop(0)
        else:
            components = components[:country_index]

    return _LocationTokens(
        components=tuple(components),
        country=country,
        canadian_context=canadian_context,
    )


def _classify_locality(tokens: _LocationTokens) -> tuple[str | None, str | None]:
    components = list(tokens.components)
    city: str | None = None
    province_state: str | None = None

    if len(components) == 1:
        city_with_code = re.fullmatch(
            rf"(?P<city>.+?)\s+(?P<province>{'|'.join(CANADIAN_PROVINCE_CODES)})",
            components[0],
            flags=re.IGNORECASE,
        )
        if city_with_code:
            city = city_with_code.group("city").strip()
            province_state = _recognized_canadian_region(city_with_code.group("province"))
            components = []

    if components:
        region_candidate = components[-1]
        canadian_region = _recognized_canadian_region(region_candidate)
        us_region = _recognized_us_region(region_candidate)
        if len(components) >= 2 and canadian_region:
            province_state = canadian_region
            components.pop()
        elif len(components) >= 2 and us_region:
            province_state = us_region
            components.pop()
        elif len(components) >= 2 and (tokens.country is not None and tokens.country != "Canada"):
            province_state = _region_component_base(region_candidate)
            components.pop()
        elif len(components) >= 2 and _looks_like_region_code(region_candidate):
            province_state = _region_component_base(region_candidate)
            components.pop()
        elif len(components) == 1 and canadian_region and tokens.country in {None, "Canada"}:
            province_state = canadian_region
            components.pop()

    while province_state and len(components) >= 2 and _same_region(components[-1], province_state):
        components.pop()
    if components:
        candidate_city = _location_component_core(components[-1])
        if candidate_city and not _is_location_arrangement_only(candidate_city):
            city = candidate_city
    return city, province_state


def _clean_component(value: str) -> str:
    core = _location_component_core(value)
    if _is_postal_component(core):
        return ""
    return _without_postal_suffix(core)


def _is_postal_component(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Z]\d[A-Z]\s*\d[A-Z]\d|\d{5}(?:-\d{4})?",
            value.strip(),
            flags=re.IGNORECASE,
        )
    )


def _has_us_city_state_tail(components: list[str]) -> bool:
    if len(components) < 2 or not _recognized_us_region(components[-1]):
        return False
    if len(components) == 2:
        return not _is_canadian_region_code(components[0])
    return any(_looks_like_street_address(part) for part in components[:-2])


def _has_strong_canadian_source_hint(source_type: str | None) -> bool:
    return source_type in {
        "ats_eluta",
        "ats_hanzilla",
        "ats_canadian_tech_internships",
        "ats_canadian_tech_internships_2027",
        "ats_canada_tech_internships_summer_2027",
    }


def _is_canadian_region_code(value: str) -> bool:
    code = _normalized_region_code(_region_component_base(value))
    return code in CANADIAN_PROVINCE_CODES or code in CANADIAN_PROVINCE_ALIASES


def _recognized_canadian_region(value: str) -> str | None:
    cleaned = _region_component_base(value)
    code = _normalized_region_code(cleaned)
    if code in CANADIAN_PROVINCE_CODES:
        return CANADIAN_PROVINCE_CODES[code]
    if code in CANADIAN_PROVINCE_ALIASES:
        return CANADIAN_PROVINCE_ALIASES[code]
    normalized = cleaned.casefold()
    return next(
        (province for province in CANADIAN_PROVINCES if province.casefold() == normalized),
        None,
    )


def _recognized_us_region(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _region_component_base(value)
    code = _normalized_region_code(cleaned)
    if code in US_STATE_CODES:
        return code
    if cleaned.casefold() in US_STATE_NAMES:
        return cleaned
    return None


def _normalized_region_code(value: str) -> str:
    cleaned = value.strip()
    compact = re.sub(r"[.\s]", "", cleaned)
    if 2 <= len(compact) <= 3 and compact.isalpha():
        return compact.upper()
    return cleaned.upper()


def _country_component_match(
    components: list[str],
    *,
    allow_ambiguous_code: bool,
) -> tuple[int, str] | None:
    if not components:
        return None

    first_country = _recognized_country_name(components[0])
    if first_country in {"Canada", "United States"}:
        return 0, first_country

    for index in range(len(components) - 1, -1, -1):
        recognized = _recognized_country_name(components[index])
        if recognized and (
            index == len(components) - 1
            or "(" in components[index]
            or "[" in components[index]
            or re.search(r"\s+[\u2013\u2014-]\s+", components[index])
            or all(_is_trailing_location_metadata(part) for part in components[index + 1 :])
        ):
            return index, recognized

    if allow_ambiguous_code:
        recognized = _recognized_country_name(
            components[-1],
            allow_ambiguous_code=True,
        )
        if recognized:
            return len(components) - 1, recognized
    return None


def _recognized_country_name(
    value: str,
    *,
    allow_ambiguous_code: bool = False,
) -> str | None:
    cleaned = _location_component_core(value)
    candidates = (
        cleaned,
        re.split(r"\s+[\u2013\u2014-]\s+", cleaned, maxsplit=1)[0],
        cleaned.split("(", 1)[0].strip(),
        cleaned.split("[", 1)[0].strip(),
    )
    for candidate in candidates:
        normalized = candidate.casefold()
        code = re.sub(r"[.\s]", "", candidate).upper()
        if code in COUNTRY_CODES:
            return COUNTRY_CODES[code]
        if normalized in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[normalized]
        if allow_ambiguous_code and code in AMBIGUOUS_COUNTRY_CODES:
            return AMBIGUOUS_COUNTRY_CODES[code]
    return None


def _is_trailing_location_metadata(value: str) -> bool:
    core = _location_component_core(value).strip(" ·")
    normalized = " ".join(re.sub(r"[^a-z]+", " ", value.casefold()).split())
    if normalized in {
        "contract",
        "full time",
        "hybrid",
        "part time",
        "remote",
        "temporary",
    }:
        return True
    if re.fullmatch(
        r"\d+(?:\.\d+)?\s*(?:hrs?|hours?)\s*(?:/|per)?\s*week",
        core,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(r"[A-Z]\d[A-Z]\s*\d[A-Z]\d\b", core, flags=re.IGNORECASE)
        or (
            re.search(r"\d", core)
            and re.fullmatch(
                r"[A-Z0-9][A-Z0-9 -]{4,9}",
                core,
                flags=re.IGNORECASE,
            )
        )
    )


def _location_label_and_arrangement(value: str) -> tuple[str, str | None]:
    normalized = " ".join(re.sub(r"[^a-z]+", " ", value.casefold()).split())
    exact_arrangements = {
        "fully remote": "remote",
        "hybrid": "hybrid",
        "in office": "onsite",
        "on site": "onsite",
        "onsite": "onsite",
        "remote": "remote",
        "virtual": "remote",
        "work from home": "remote",
    }
    if arrangement := exact_arrangements.get(normalized):
        return "", arrangement

    arrangement_prefix = (
        r"hybrid|(?:fully\s+)?remote|virtual|work\s+from\s+home|"
        r"on[- ]?site|onsite|in[- ]office"
    )
    parenthesized_scope = False
    prefixed = re.fullmatch(
        rf"(?i)(?P<arrangement>{arrangement_prefix})"
        r"\s+(?:in|at|within(?:\s+the)?|[\u2013\u2014-])\s+"
        r"(?P<location>.+)",
        value.strip(),
    )
    if not prefixed:
        prefixed = re.fullmatch(
            rf"(?i)(?P<arrangement>{arrangement_prefix})\s*"
            r"\((?P<location>[^()]+)\)",
            value.strip(),
        )
        parenthesized_scope = prefixed is not None
    if not prefixed:
        return value, None

    arrangement = prefixed.group("arrangement").casefold()
    if arrangement in {"virtual", "fully remote", "work from home"}:
        arrangement = "remote"
    elif arrangement in {"on-site", "on site", "onsite", "in-office", "in office"}:
        arrangement = "onsite"
    location = prefixed.group("location")
    if parenthesized_scope:
        location = _parenthetical_arrangement_scope(location)
    location = re.sub(
        r"(?i)\s*\((?=[^)]*\b(?:days?|weekly|remote|hybrid|office|"
        r"on[- ]?site|onsite|in\s+person)\b)[^)]*\)\s*$",
        "",
        location,
    ).strip()
    location = re.split(r"\s*\|\s*", location, maxsplit=1)[0].strip()
    components = [component.strip() for component in location.split(",")]
    if components:
        components[0] = re.sub(r"(?i)\s+office$", "", components[0]).strip()
    return ", ".join(component for component in components if component), arrangement


def _parenthetical_arrangement_scope(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if re.search(
        r"\b\d+\s*(?:days?|hrs?|hours?)\b"
        r"|\b(?:daily|weekly|weekdays?|business hours?)\b"
        r"|\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        r".*\b(?:to|through|-)\s*"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        r"|\b(?:eastern|central|mountain|pacific|atlantic)\s+"
        r"time(?:\s+zone)?\b"
        r"|\b(?:e[ds]t|c[ds]t|m[ds]t|p[ds]t|utc(?:[+-]\d{1,2})?)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return ""
    return re.sub(r"(?i)\s+only$", "", value).strip()


def _has_affirmative_remote_policy(value: str) -> bool:
    return _remote_policy_decision(value) is True


def _remote_policy_decision(value: str) -> bool | None:
    all_assertions = _remote_policy_assertions(value)
    assertions = [
        assertion
        for assertion in all_assertions
        if assertion.targets != frozenset({"other_roles"})
    ]
    if not assertions:
        return False if all_assertions else None
    affirmative_indexes = [
        index
        for index, assertion in enumerate(assertions)
        if assertion.polarity == "affirmative"
    ]
    if not affirmative_indexes:
        return False
    blocking_indexes = [
        index
        for index, assertion in enumerate(assertions)
        if assertion.polarity == "current_role_excluded"
        or assertion.polarity == "negative"
    ]
    return not blocking_indexes or affirmative_indexes[-1] > blocking_indexes[-1]


def _remote_policy_assertions(value: str) -> tuple[_RemotePolicyAssertion, ...]:
    normalized = _normalize_remote_policy_text(value)
    assertions: list[_RemotePolicyAssertion] = []
    prior_remote_policy_subject = False
    for clause in re.split(r"[\n.;]+", normalized):
        clause = clause.strip()
        if not clause:
            continue
        remote_subject = prior_remote_policy_subject and _continues_remote_subject(clause)
        clause_has_remote_policy_subject = remote_subject
        for segment in re.split(r"\b(?:but|however|yet)\b", clause, flags=re.IGNORECASE):
            segment = segment.strip()
            if not segment:
                continue
            if re.search(r"\bremote(?:ly)?\b", segment, flags=re.IGNORECASE):
                remote_subject = True
                if _starts_with_remote_policy_subject(segment):
                    clause_has_remote_policy_subject = True
            if not remote_subject:
                continue
            if _remote_policy_excludes_current_role(segment):
                assertions.append(
                    _RemotePolicyAssertion(
                        polarity="current_role_excluded",
                        targets=frozenset({"current_role"}),
                    )
                )
                continue
            if polarity := _remote_policy_segment_polarity(segment):
                assertions.append(
                    _RemotePolicyAssertion(
                        polarity=polarity,
                        targets=_remote_assertion_targets(segment),
                    )
                )
        prior_remote_policy_subject = clause_has_remote_policy_subject
    return tuple(assertions)


def _starts_with_remote_policy_subject(value: str) -> bool:
    return bool(
        re.match(
            r"(?i)^(?:(?:for|in)\s+(?:this|the)\s+"
            r"(?:role|position|job),?\s+)?"
            r"(?:remote\s+(?:work|working|option|arrangement|policy|"
            r"eligibility|schedule)\b"
            r"|the\s+remote\s+(?:option|arrangement|policy|schedule)\b"
            r"|remote\s+(?:is|was|will|would|can|could|may|might)\b)",
            value,
        )
    )


def _remote_assertion_targets(value: str) -> frozenset[str]:
    targets: set[str] = set()
    if re.search(
        r"(?i)\b(?:other|different)\s+"
        r"(?:roles?|positions?|jobs?|departments?|teams?|employees?|"
        r"candidates?|interns?|workers?|people)\b"
        r"|\bemployees?\s+(?:in|from)\s+(?:other|different)\s+"
        r"(?:departments?|teams?)\b",
        value,
    ):
        targets.add("other_roles")
    if re.search(
        r"(?i)\b(?:this|current|the(?:\s+current)?)\s+"
        r"(?:role|position|job)\b",
        value,
    ):
        targets.add("current_role")
    return frozenset(targets or {"unspecified"})


def _continues_remote_subject(value: str) -> bool:
    normalized = re.sub(
        r"(?i)^(?:(?:however|nevertheless|nonetheless|still|conversely|instead)"
        r"\s*[,;:\u2014-]?\s*)+",
        "",
        value.strip(),
    )
    return bool(
        re.match(
            r"(?i)^(?:it|this|that)\s+"
            r"(?:is|was|will|would|can|could|may|might|remains?|becomes?)\b"
            r"|^(?:this|that|the|such)\s+(?:arrangement|option|policy)\b",
            normalized,
        )
    )


def _normalize_remote_policy_text(value: str) -> str:
    normalized = re.sub(r"\bcannot\b", "can not", value, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\b(is|are|was|were|do|does|did|can|could|would|should|must|need|"
        r"has|have|had)n['’]t\b",
        r"\1 not",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\bwon['’]t\b", "will not", normalized, flags=re.IGNORECASE)


def _remote_policy_excludes_current_role(value: str) -> bool:
    return bool(
        re.search(
            r"(?i)\bexcept(?:\s+for)?\s+(?:this|the)\s+(?:role|position|job)\b"
            r"|\bnot\s+(?:available\s+)?(?:for\s+)?(?:this|the)\s+"
            r"(?:role|position|job)\b",
            value,
        )
    )


def _remote_policy_segment_polarity(value: str) -> str | None:
    tokens = re.findall(r"[a-z]+", value.casefold())
    if not tokens:
        return None
    if re.search(r"(?i)\b(?:no|without)\s+remote(?:ly)?\b", value):
        return "negative"
    if re.search(
        r"(?i)\b(?:is|are|was|were)\s+"
        r"(?:not\s+(?:a\s+)?|no\s+longer\s+|non[- ]?)remote\b",
        value,
    ):
        return "negative"
    if re.search(
        r"(?i)\b(?:can|could|may|might|must|shall|should|will|would)\s+not\s+"
        r"(?:be\s+)?(?:work(?:ing)?\s+)?remote(?:ly)?\b",
        value,
    ):
        return "negative"

    negative_outcomes = {"prohibited", "unavailable"}
    positive_outcomes = {
        "allowed",
        "available",
        "eligible",
        "offered",
        "option",
        "permitted",
        "supported",
    }
    has_negative = any(token in negative_outcomes for token in tokens)
    has_affirmative = False
    for index, token in enumerate(tokens):
        if token not in positive_outcomes:
            continue
        if _remote_outcome_is_negated(tokens, index):
            has_negative = True
        else:
            has_affirmative = True

    if not has_affirmative and re.search(
        r"(?i)\b(?:fully|entirely|100%)\s+remote\b"
        r"|\b(?:can|may)\s+(?:also\s+)?work\s+remotely\b"
        r"|\bremote(?:ly)?\b[^.;]{0,40}\b(?:elsewhere|across|within)\b",
        value,
    ):
        has_affirmative = True
    if has_affirmative:
        return "affirmative"
    return "negative" if has_negative else None


def _remote_outcome_is_negated(tokens: list[str], outcome_index: int) -> bool:
    window_start = max(0, outcome_index - 6)
    for index in range(window_start, outcome_index):
        if tokens[index] == "not":
            if index + 1 < outcome_index and tokens[index + 1] == "only":
                continue
            return True
        if (
            tokens[index] == "no"
            and index + 1 < outcome_index
            and tokens[index + 1] == "longer"
        ):
            return True
    return False


def _has_affirmative_remote_alternative(value: str) -> bool:
    segments = re.split(r";|\s+/\s+", value, maxsplit=1)
    return len(segments) == 2 and _has_affirmative_remote_policy(segments[1])

def _is_location_arrangement_only(value: str) -> bool:
    location, arrangement = _location_label_and_arrangement(value)
    return arrangement is not None and not location


def _location_component_core(value: str) -> str:
    cleaned = re.sub(
        r"(?i)\s*\((?:fully\s+)?(?:remote|hybrid|on[- ]?site|onsite|"
        r"in[- ]office|in\s+person|remote\s+local)[^)]*\)\s*$",
        "",
        value.strip(),
    ).strip()
    return re.sub(
        r"(?i)\s+[\u2013\u2014-]\s+(?:fully\s+)?(?:remote|hybrid|"
        r"on[- ]?site|in[- ]office).*$",
        "",
        cleaned,
    ).strip()


def _metadata_component_base(value: str) -> str:
    return re.sub(
        r"\s*\([^)]*\)\s*$",
        "",
        _without_postal_suffix(_location_component_core(value)),
    ).strip()


def _region_component_base(value: str) -> str:
    return re.sub(
        r"(?i)\s+(?:full[- ]?time|part[- ]?time|contract|temporary)\b.*$",
        "",
        _metadata_component_base(value),
    ).strip()


def _without_postal_suffix(value: str) -> str:
    without_canadian = re.sub(
        r"\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return re.sub(r"\s+\d{5}(?:-\d{4})?$", "", without_canadian).strip()


def _looks_like_region_code(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:[A-Z]\.?){2,3}",
            _region_component_base(value),
            flags=re.IGNORECASE,
        )
    )


def _is_us_region(value: str | None) -> bool:
    return _recognized_us_region(value) is not None


def _same_region(value: str, province_state: str) -> bool:
    canadian = _recognized_canadian_region(value)
    if canadian:
        return canadian == province_state
    us_region = _recognized_us_region(value)
    if us_region:
        return us_region.casefold() == province_state.casefold()
    return _location_component_core(value).casefold() == province_state.casefold()


def _looks_like_street_address(value: str) -> bool:
    core = _location_component_core(value)
    return bool(
        re.match(
            r"(?i)^(?:\d+[A-Z]?(?:[-/]\d+)?\s+|P\.?\s*O\.?\s+Box\s+\d+\b)",
            core,
        )
    )
