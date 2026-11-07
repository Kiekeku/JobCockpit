from __future__ import annotations

import math
import re

from jobcockpit.ingestion.evidence_grammar import EvidenceMention
from jobcockpit.ingestion.location_parser import (
    CANADIAN_PROVINCES,
    US_STATE_CODES,
    US_STATE_NAMES,
)
from jobcockpit.ingestion.parser_models import ParsedCompensation, ParsedLocation
from jobcockpit.ingestion.text_patterns import NUMERIC_RANGE_SEPARATOR_PATTERN


def _compensation_currency(pay_context: str, location: ParsedLocation) -> str | None:
    if re.search(r"\bcad\b", pay_context, re.IGNORECASE):
        return "CAD"
    if re.search(r"\busd\b", pay_context, re.IGNORECASE):
        return "USD"

    country = (location.country or "").strip().casefold()
    if country == "canada":
        return "CAD"
    if country == "united states":
        return "USD"

    region = (location.province_state or "").strip()
    if region.upper() in US_STATE_CODES or region.casefold() in US_STATE_NAMES:
        return "USD"
    if region in CANADIAN_PROVINCES:
        return "CAD"
    return None


_COMPENSATION_HOURLY_PERIOD_PATTERN = (
    r"(?:/\s*(?:per\s+hour|hourly|hr|hour|h\b)|per\s+hour|an?\s+hour|hourly)"
)
_COMPENSATION_PERIOD_TOKEN_PATTERN = (
    rf"(?:{_COMPENSATION_HOURLY_PERIOD_PATTERN}|"
    r"/\s*(?:wk|week|mo|month|yr|year)|"
    r"/\s*p\.?\s*a\.?|per\s+(?:week|month|year|annum)|"
    r"an?\s+(?:week|month|year)|(?:/\s*)?(?:gross\s+)?bi[- ]?weekly|"
    r"weekly|monthly|annually|annual)"
)
_COMPENSATION_NUMBER_PATTERN = (
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,6}(?:\.\d+)?)"
)
_COMPENSATION_AMOUNT_PATTERN = rf"{_COMPENSATION_NUMBER_PATTERN}(?:\s*[kK])?"
_COMPENSATION_ANCHOR_PATTERN = (
    r"pay\s+transparency(?:\s*[-–—]\s*North\s+America)?|"
    r"(?:can|canada|canadian)\s+"
    r"(?:base\s+pay|salary|pay|hourly|weekly|monthly|annual)\s+range|"
    r"range\s+for\s+(?:this|the)\s+(?:role|position|job)|"
    r"(?:hiring|current)\s+range|"
    r"base\s+pay\s+range|salary\s+(?:range|bands?)|salary|"
    r"pay\s+range|pay|compensation|hourly\s+rate|weekly\s+pay|wage(?=\s*:)"
)


def _compensation_pattern(
    *,
    between_and: bool,
    bare_monetary_amount_pattern: str,
    bare_range_with_money_suffix: str,
    bare_scaled_range: str,
) -> re.Pattern[str]:
    """Build one pay-claim production with a single, owned range grammar."""

    between_prefix = r"\bbetween\s+" if between_and else ""
    range_separator = r"\s+and\s+" if between_and else rf"\s*{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
    maximum = (
        rf"{range_separator}(?:(?:CAD|USD)\s*)?\$?\s*"
        rf"(?P<maximum>{_COMPENSATION_AMOUNT_PATTERN})"
    )
    if not between_and:
        maximum = rf"(?:{maximum})?"
    return re.compile(
        rf"\b(?:{_COMPENSATION_ANCHOR_PATTERN})\b"
        r"[^$]{0,180}?"
        rf"{between_prefix}"
        r"(?<![\w/])"
        r"(?:(?P<currency>CAD|USD)\s*(?P<currency_symbol>\$)?|(?P<symbol>\$)|"
        rf"(?=(?:{bare_monetary_amount_pattern}|{bare_range_with_money_suffix}|"
        rf"{bare_scaled_range})))\s*"
        rf"(?P<minimum>{_COMPENSATION_AMOUNT_PATTERN})"
        rf"(?:\s*(?P<minimum_period>{_COMPENSATION_PERIOD_TOKEN_PATTERN})\s*)?"
        rf"{maximum}",
        flags=re.IGNORECASE,
    )


def _parse_compensation(
    raw_text: str,
    location: ParsedLocation,
) -> tuple[ParsedCompensation, bool]:
    raw_text = _normalize_compensation_digits(raw_text)
    bare_monetary_amount_pattern = (
        rf"(?:{_COMPENSATION_NUMBER_PATTERN}\s*[kK]|"
        r"\d{2,3}(?:,\d{3})+(?:\.\d+)?|\d{5,6}(?:\.\d+)?)"
    )
    bare_range_with_money_suffix = (
        rf"{_COMPENSATION_AMOUNT_PATTERN}\s*"
        rf"(?:{_COMPENSATION_PERIOD_TOKEN_PATTERN})?\s*"
        rf"{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
        rf"(?:CAD|USD)?\s*\$?\s*{_COMPENSATION_AMOUNT_PATTERN}\s*"
        rf"(?:CAD|USD)?\s*{_COMPENSATION_PERIOD_TOKEN_PATTERN}"
    )
    bare_scaled_range = (
        rf"(?:{_COMPENSATION_NUMBER_PATTERN}\s*"
        rf"{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
        rf"{_COMPENSATION_NUMBER_PATTERN}\s*[kK]|"
        rf"{_COMPENSATION_NUMBER_PATTERN}\s*[kK]\s*"
        rf"{NUMERIC_RANGE_SEPARATOR_PATTERN}\s*"
        rf"{_COMPENSATION_NUMBER_PATTERN}(?:\s*[kK])?)"
    )
    patterns = tuple(
        _compensation_pattern(
            between_and=between_and,
            bare_monetary_amount_pattern=bare_monetary_amount_pattern,
            bare_range_with_money_suffix=bare_range_with_money_suffix,
            bare_scaled_range=bare_scaled_range,
        )
        for between_and in (True, False)
    )
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        search_start = 0
        while match := pattern.search(raw_text, search_start):
            accepted = _compensation_match_has_owned_bridge(
                raw_text, match
            ) and not _compensation_match_is_incidental(raw_text, match)
            if accepted:
                matches.append(match)
                search_start = match.end()
            else:
                search_start = match.start() + 1
    if not matches:
        return ParsedCompensation(), False

    candidates: list[tuple[re.Match[str], ParsedCompensation]] = []
    malformed_claim = False
    for match in matches:
        context = EvidenceMention.from_posting(
            raw_text,
            match.start(),
            match.end(),
        ).clause
        period = _compensation_period_for_match(raw_text, match)
        if any(re.search(r",\d{3}\s*k$", match.group(group) or "", re.I)
               for group in ("minimum", "maximum")):
            malformed_claim = True
            continue
        prefix_period = _compensation_period(raw_text[max(0, match.start() - 12):match.start("minimum")])
        if period == "biweekly" and prefix_period == "annual":
            malformed_claim = True
            continue
        minimum, maximum = _compensation_range_amounts(match)
        amount_end = match.end("maximum") if maximum is not None else match.end("minimum")
        third = re.match(
            rf"\s+to\s+\$?\s*(?P<amount>{_COMPENSATION_AMOUNT_PATTERN})(?![\d,])",
            raw_text[amount_end:], re.I,
        )
        if maximum is not None and third:
            upper, _ = _compensation_amount_parts(third['amount'])
            if upper is not None and minimum <= maximum <= upper:
                maximum = upper
        if maximum is not None and re.match(r"\s*\+\s*/h\b", raw_text[amount_end:]):
            maximum = None
        if (
            minimum < 10_000
            and not (match.group("symbol") or match.group("currency_symbol"))
            and not match.group("currency")
            and period is None
        ):
            continue
        candidates.append(
            (
                match,
                ParsedCompensation(
                    pay_min=minimum,
                    pay_max=maximum,
                    pay_currency=_compensation_currency(context, location),
                    pay_period=period or "annual",
                ),
            )
        )
    if malformed_claim:
        return ParsedCompensation(), True
    if not candidates:
        return ParsedCompensation(), malformed_claim

    ranged_candidates = [candidate for candidate in candidates if candidate[1].pay_max is not None]
    preferred_candidates = ranged_candidates or candidates
    reference = preferred_candidates[0][1]
    if any(
        not _compensation_claims_agree(reference, candidate)
        for _match, candidate in preferred_candidates[1:]
    ):
        return ParsedCompensation(), True

    def match_rank(candidate: tuple[re.Match[str], ParsedCompensation]) -> tuple[bool, int]:
        match, compensation = candidate
        return (
            compensation.pay_currency is not None,
            -match.start(),
        )

    _match, compensation = max(preferred_candidates, key=match_rank)
    return compensation, False


def _normalize_compensation_digits(text: str) -> str:
    """Repair observed monetary typography without joining unrelated numeric facts."""
    text = re.sub(r"(?<=\$)(\d)\s*\n\s*(\d{2})\s*\n\s*(,\d{3})(?!\d)",
                  lambda m: ''.join(m.groups()), text)
    text = re.sub(r"(?<=\$)(\d{1,3}),[ \t]+(\d)[ \t]+(\d{2})(?!\d)",
                  lambda m: f"{m[1]},{m[2]}{m[3]}", text)
    text = re.sub(r"(?<=\$)(\d+),(\d{2})(?!\d)", r"\1.\2", text)
    # This bilingual duplicate supplies the unit for the identical English band.
    bilingual = re.compile(
        r"(?P<header>Hiring Range)\s*(?:/\s*)?Échelle Salariale à L[’']embauche\s*:?\s*"
        r"\$(?P<low>\d+\.\d{2})\s*[-–—]\s*\$(?P<high>\d+\.\d{2})\s*(?:/\s*)?"
        r"(?P<frlow>\d+,\d{2})\$\s*[-–—]\s*(?P<frhigh>\d+,\d{2})\$"
        r"\s*\(per hour\s*/\s*de l[’']heure\)", re.I,
    )

    def band(match: re.Match[str]) -> str:
        if (match['low'], match['high']) != (match['frlow'].replace(',', '.'),
                                             match['frhigh'].replace(',', '.')):
            return match[0]
        return f"{match['header']}: ${match['low']} - ${match['high']} per hour"

    return bilingual.sub(band, text)


def _compensation_claims_agree(
    left: ParsedCompensation,
    right: ParsedCompensation,
) -> bool:
    if left.pay_currency != right.pay_currency or left.pay_period != right.pay_period:
        return False
    return _compensation_amounts_agree(
        left.pay_min,
        right.pay_min,
    ) and _compensation_amounts_agree(left.pay_max, right.pay_max)


def _compensation_amounts_agree(
    left: int | float | None,
    right: int | float | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-5, abs_tol=0.01)


def _compensation_match_has_owned_bridge(raw_text: str, match: re.Match[str]) -> bool:
    bridge = raw_text[match.start() : match.start("minimum")]
    anchor = re.match(
        rf"\b(?:{_COMPENSATION_ANCHOR_PATTERN})\b",
        bridge,
        flags=re.IGNORECASE,
    )
    remainder = bridge[anchor.end() :] if anchor else bridge
    anchor_text = anchor.group(0).casefold() if anchor else ""
    if anchor_text.startswith("pay transparency"):
        # This heading owns the following geographic band, including its
        # location qualification and bullet. Other facts remain separate.
        return bool(re.fullmatch(
            r"\s*[*•]?\s*British\s+Columbia"
            r"(?:\s*\(depending\s+on\s+location\s+e\.g\.\s*"
            r"Vancouver\s+vs\.\s*Victoria\))?\s*[*•]?\s*"
            r"(?:(?:CAD|USD)\s*)?\$?\s*",
            remainder, re.I,
        ))
    prefix = raw_text[max(0, match.start() - 24) : match.start()]
    aggregate_compensation = anchor_text in {"compensation", "pay"} and bool(
        re.search(r"\btotal\s*$", prefix, flags=re.IGNORECASE)
    )
    minimum_text = match.group("minimum")
    if (
        match.group("maximum") is None
        and minimum_text.casefold().endswith("k")
        and not (
            match.group("symbol")
            or match.group("currency_symbol")
            or match.group("currency")
            or match.group("minimum_period")
        )
        and not re.fullmatch(
            r"\s*(?:(?:is|of|at|will\s+be|starts?\s+at|starting\s+at|"
            r"approximately|about|up\s+to)\s+)?(?:(?:CAD|USD)\s*)?\$?\s*",
            remainder,
            flags=re.IGNORECASE,
        )
    ):
        return False
    amount_end = (
        match.end("maximum") if match.group("maximum") else match.end("minimum")
    )
    attached_suffix = re.split(
        r"[,;.!?\r\n]|\b(?:and|plus|with)\b",
        raw_text[amount_end : amount_end + 100],
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    competing_monetary_noun = (
        r"(?:benefits?|bonus|allowance|stipend|reimbursement|commission|equity|"
        r"learning\s+budget|wellness\s+budget)"
    )
    if not aggregate_compensation and re.search(
        rf"\b{competing_monetary_noun}\s*:\s*"
        r"(?:(?:CAD|USD)\s*)?\$?\s*$",
        remainder,
        flags=re.IGNORECASE,
    ):
        return False
    if not aggregate_compensation and match.group("maximum") is None and re.match(
        rf"\s*(?:(?:annual|yearly|monthly|weekly|one[-\s]?time|learning|wellness|"
        rf"education|relocation|signing|performance|cash|professional\s+development)\s+){{0,3}}"
        rf"(?:on\s+average\s+spent\s+on\s+)?{competing_monetary_noun}\b",
        attached_suffix,
        flags=re.IGNORECASE,
    ):
        return False
    # A single terminal colon belongs to labels such as "Salary (annual):".
    # Any other colon introduces a different labeled fact.
    colons = tuple(re.finditer(r":", remainder))
    if colons:
        if len(colons) != 1 or not re.fullmatch(
            r"[^:]*:\s*(?:(?:CAD|USD)\s*)?\$?\s*",
            remainder,
            flags=re.IGNORECASE,
        ):
            return False
        remainder = remainder[: colons[0].start()] + remainder[colons[0].end() :]
    boundary_text = re.sub(r"\b(?:[A-Za-z]\.){2,}", "", remainder)
    boundary_text = re.sub(
        r"\.\s*(?=(?:(?:CAD|USD)\s*)?\$?\s*$)",
        "",
        boundary_text,
        flags=re.IGNORECASE,
    )
    if re.search(r"[!?;]|\.(?:\s|$)", boundary_text):
        return False
    lines = bridge.splitlines()
    if len(lines) <= 1:
        return True
    continuation = " ".join(lines[1:]).strip()
    return bool(re.fullmatch(r"(?:(?:CAD|USD)\s*)?\$?", continuation, re.IGNORECASE))


def _compensation_match_is_incidental(raw_text: str, match: re.Match[str]) -> bool:
    end = match.end('maximum') if match.group('maximum') else match.end('minimum')
    if re.match(r"\s*/?\s*bi[- ]?weekly[A-Z]", raw_text[end:]):
        return True  # A truncated page preview is not the complete owned claim.
    context = " ".join(
        raw_text[max(0, match.start() - 120) : match.end()].casefold().split()
    )
    return bool(
        re.search(
            r"\b(?:what people ask|typical salary range|minimum salary all salaries|"
            r"related (?:jobs?|roles?))\b",
            context,
        )
    )


def _compensation_amount_parts(value: str | None) -> tuple[int | float | None, bool]:
    if value is None:
        return None, False
    normalized = value.replace(",", "").strip()
    scaled = normalized.casefold().endswith("k")
    if scaled:
        normalized = normalized[:-1].strip()
    amount: int | float = float(normalized) if "." in normalized else int(normalized)
    if scaled:
        amount *= 1_000
        if isinstance(amount, float) and amount.is_integer():
            amount = int(amount)
    return amount, scaled


def _compensation_range_amounts(
    match: re.Match[str],
) -> tuple[int | float, int | float | None]:
    minimum, minimum_scaled = _compensation_amount_parts(match.group("minimum"))
    maximum, maximum_scaled = _compensation_amount_parts(match.group("maximum"))
    assert minimum is not None
    if maximum is not None and minimum_scaled != maximum_scaled:
        if not minimum_scaled and minimum < 1_000:
            minimum *= 1_000
        if not maximum_scaled and maximum < 1_000:
            maximum *= 1_000
    return minimum, maximum


def _compensation_period(context: str) -> str | None:
    patterns = (
        ("hourly", rf"{_COMPENSATION_HOURLY_PERIOD_PATTERN}\b"),
        ("biweekly", r"\bbi[- ]?weekly\b"),
        ("weekly", r"(?:/\s*(?:wk|week)|per\s+week|an?\s+week|weekly)\b"),
        ("monthly", r"(?:/\s*(?:mo|month)|per\s+month|an?\s+month|monthly)\b"),
        (
            "annual",
            r"(?:/\s*(?:yr|year)|/\s*p\.?\s*a\.?|"
            r"per\s+(?:year|annum)|an?\s+year|annually|annual)(?:\b|(?<=\.))",
        ),
    )
    for period, pattern in patterns:
        if re.search(pattern, context, flags=re.IGNORECASE):
            return period
    return None


def _compensation_period_for_match(raw_text: str, match: re.Match[str]) -> str | None:
    amount_end = (
        match.end("maximum") if match.group("maximum") is not None else match.end("minimum")
    )
    suffix = raw_text[amount_end : amount_end + 48]
    adjacent_period = re.match(
        r"[\s)+]*(?:(?:CAD|USD)\s*)?"
        rf"(?P<period>{_COMPENSATION_PERIOD_TOKEN_PATTERN})(?:\b|(?<=\.))",
        suffix,
        flags=re.IGNORECASE,
    )
    if adjacent_period:
        return _compensation_period(adjacent_period.group("period"))

    minimum_period = match.groupdict().get("minimum_period")
    if minimum_period:
        return _compensation_period(minimum_period)

    anchor_prefix = raw_text[max(0, match.start() - 32) : match.start("minimum")]
    anchor_prefix = re.split(r"[.;!?\r\n]", anchor_prefix)[-1]
    return _compensation_period(anchor_prefix)
