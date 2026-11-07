from __future__ import annotations

import json
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobcockpit.ingestion.parser import (
    ParsedTiming,
    parse_job_text,
    parse_job_text_with_settings,
)
from jobcockpit.ingestion.parser_models import RoleClaim, RoleEvidence


DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parents[4] / "data" / "fixtures" / "parser" / "golden_cases.json"
)
MISSING_VALUE = "<missing>"
_MISSING = object()
CORPUS_SCHEMA_VERSION = 1
_CASE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_JOB_ID_PATTERN = re.compile(r"\bjob_[0-9a-f]{16,}\b", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_SCHEMELESS_HOST_PATTERN = re.compile(
    r"(?<![@\w.-])(?P<hostname>(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+[a-z]{2,})(?![\w.-])",
    re.IGNORECASE,
)
_SAFE_FIXTURE_DOMAINS = ("example.test", "example.invalid")
_SAFE_AMBIGUOUS_FIELD_PATHS = frozenset({"role.evidence"})
_DOTTED_TECHNOLOGY_NAMES = frozenset(
    {
        "ado.net",
        "asp.net",
        "node.js",
        "react.js",
        "vb.net",
        "vue.js",
    }
)


class ParserCorpusError(ValueError):
    """Raised when the parser corpus is invalid or unsafe to commit."""


class CorpusProviderTiming(ParsedTiming):
    model_config = ConfigDict(extra="forbid")


class CorpusAvailabilityCheck(BaseModel):
    """Exercise availability parsing without real employer URLs or network access."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["greenhouse", "workday", "adp_workforcenow", "generic"]
    company: str | None = None
    rendered: bool = False


class ParserCorpusCase(BaseModel):
    id: str = Field(pattern=_CASE_ID_PATTERN)
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    provenance: Literal["synthetic_minimized"]
    grounding: Literal["observed_posting"] | None = None
    grounding_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    raw_text: str = Field(min_length=1)
    title_hint: str | None = None
    source_url: str | None = None
    source_type: str = "paste"
    provider_timing: CorpusProviderTiming | None = None
    provider_role_mode: Literal[
        "baseline_evidence",
        "unsectioned_requirement_from_capabilities",
        "summarized_description",
    ] | None = None
    provider_role_normalized_text: str | None = None
    provider_role_claim_text: str | None = None
    provider_role_evidence_text: str | None = None
    availability_check: CorpusAvailabilityCheck | None = None
    expected: dict[str, Any]


class ParserCorpus(BaseModel):
    schema_version: Literal[1]
    description: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")

    cases: list[ParserCorpusCase] = Field(min_length=1)


@dataclass(frozen=True)
class _CorpusParserSettings:
    posting_parser_provider: str = "corpus_fixture"


@dataclass(frozen=True)
class _CorpusPostingParser:
    timing: ParsedTiming | None = None
    role_mode: str | None = None
    role_normalized_text: str | None = None
    role_claim_text: str | None = None
    role_evidence_text: str | None = None

    def parse(self, raw_text: str, source_url: str | None = None, baseline=None):
        if baseline is None:
            return None
        parsed = baseline.model_copy(deep=True)
        if self.timing is not None:
            parsed.employment_timing = self.timing.model_copy(deep=True)
        if self.role_mode == "summarized_description":
            parsed.role.description_raw = self.role_evidence_text or ""
            parsed.role.description_clean = self.role_evidence_text or ""
        if self.role_mode == "unsectioned_requirement_from_capabilities":
            evidence_text = self.role_evidence_text or next(
                (line.strip() for line in reversed(raw_text.splitlines()) if line.strip()),
                "",
            )
            parsed.role.evidence = [
                RoleEvidence(
                    kind="requirement",
                    modality="required",
                    source_section="unsectioned",
                    text=evidence_text,
                    evidence_text=evidence_text,
                    entities=list(parsed.role.capabilities),
                    claims=(
                        [
                            RoleClaim(
                                kind="qualification",
                                modality="mandatory",
                                owner="applicant",
                                text=self.role_claim_text or evidence_text,
                                normalized_text=self.role_normalized_text,
                            )
                        ]
                        if self.role_normalized_text is not None
                        or self.role_claim_text is not None
                        else []
                    ),
                )
            ]
        return parsed


@dataclass(frozen=True)
class CorpusDifference:
    case_id: str
    path: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class CorpusRun:
    case_ids: tuple[str, ...]
    outputs: dict[str, dict[str, Any]]
    differences: tuple[CorpusDifference, ...]

    @property
    def passed(self) -> bool:
        return not self.differences


def load_parser_corpus(path: Path = DEFAULT_CORPUS_PATH) -> ParserCorpus:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        corpus = ParserCorpus.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ParserCorpusError(f"Unable to load parser corpus {path}: {exc}") from exc

    _validate_corpus_contract(corpus)
    return corpus


def run_parser_corpus(
    corpus: ParserCorpus,
    *,
    case_ids: set[str] | None = None,
    workers: int = 1,
) -> CorpusRun:
    available_ids = {case.id for case in corpus.cases}
    unknown_ids = sorted((case_ids or set()) - available_ids)
    if unknown_ids:
        raise ParserCorpusError(f"Unknown parser corpus case(s): {', '.join(unknown_ids)}")
    if workers < 1:
        raise ParserCorpusError("Parser corpus worker count must be at least 1")

    selected_cases = [case for case in corpus.cases if case_ids is None or case.id in case_ids]
    outputs: dict[str, dict[str, Any]] = {}
    differences: list[CorpusDifference] = []

    worker_count = min(workers, len(selected_cases))
    if worker_count <= 1:
        results = map(_run_parser_corpus_case, selected_cases)
    else:
        executor = ProcessPoolExecutor(max_workers=worker_count)
        chunksize = max(1, len(selected_cases) // (worker_count * 4))
        results = executor.map(
            _run_parser_corpus_case,
            selected_cases,
            chunksize=chunksize,
        )
    try:
        for case_id, output, case_differences in results:
            outputs[case_id] = output
            differences.extend(case_differences)
    finally:
        if worker_count > 1:
            executor.shutdown()

    return CorpusRun(
        case_ids=tuple(case.id for case in selected_cases),
        outputs=outputs,
        differences=tuple(differences),
    )


def _run_parser_corpus_case(
    case: ParserCorpusCase,
) -> tuple[str, dict[str, Any], tuple[CorpusDifference, ...]]:
    if case.provider_timing is None and case.provider_role_mode is None:
        parsed = parse_job_text(
            case.raw_text,
            case.source_url,
            source_type=case.source_type,
            title_hint=case.title_hint,
        )
    else:
        parsed = parse_job_text_with_settings(
            case.raw_text,
            case.source_url,
            _CorpusParserSettings(),
            parser=_CorpusPostingParser(
                case.provider_timing,
                case.provider_role_mode,
                case.provider_role_normalized_text,
                case.provider_role_claim_text,
                case.provider_role_evidence_text,
            ),
            source_type=case.source_type,
            title_hint=case.title_hint,
        )
    actual = parsed.model_dump(mode="json")
    if case.availability_check is not None:
        actual["availability"] = _availability_projection(case)
    return (
        case.id,
        _project_actual(actual, case.expected),
        tuple(_find_differences(case.id, case.expected, actual)),
    )


def _availability_projection(case: ParserCorpusCase) -> dict[str, Any]:
    from jobcockpit.ingestion.status_reconciliation import (
        _identity_values_match,
        infer_status_from_page,
        posting_identity,
    )

    check = case.availability_check
    assert check is not None
    # Only the test harness substitutes a fixed provider host. Fixture URLs remain
    # anonymized, and this projection never fetches or persists anything.
    hosts = {
        "greenhouse": "job-boards.greenhouse.io",
        "workday": "acme.wd1.myworkdayjobs.com",
        "adp_workforcenow": "workforcenow.adp.com",
    }
    parsed_url = urlparse(case.source_url or "https://jobs.example.test/")
    source_url = parsed_url._replace(netloc=hosts.get(check.provider, parsed_url.netloc)).geturl()
    identity = posting_identity(source_url)
    decision = infer_status_from_page(
        source_url,
        case.raw_text,
        evidence_scope="rendered_page" if check.rendered else "source_page",
    )
    return {
        "is_detail": identity.is_detail,
        "posting_id": identity.posting_id,
        "result": decision.result if decision else None,
        "evidence_code": decision.evidence_code if decision else None,
        "content_identity_matches": _identity_values_match(
            check.company, case.title_hint, case.raw_text,
        ),
    }


def render_corpus_run(run: CorpusRun) -> str:
    case_count = len(run.case_ids)
    if run.passed:
        return f"Parser golden corpus passed: {case_count} case(s), 0 field differences."

    lines = [
        (
            f"Parser golden corpus failed: {case_count} case(s), "
            f"{len(run.differences)} field difference(s)."
        )
    ]
    for difference in run.differences:
        lines.extend(
            [
                f"- {difference.case_id}: {difference.path}",
                f"    expected: {_format_value(difference.expected)}",
                f"    actual:   {_format_value(difference.actual)}",
            ]
        )
    return "\n".join(lines)


def write_current_snapshot(run: CorpusRun, path: Path) -> None:
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "case_count": len(run.case_ids),
        "cases": [{"id": case_id, "actual": run.outputs[case_id]} for case_id in run.case_ids],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_corpus_contract(corpus: ParserCorpus) -> None:
    case_ids = [case.id for case in corpus.cases]
    duplicate_ids = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicate_ids:
        raise ParserCorpusError(f"Duplicate parser corpus case(s): {', '.join(duplicate_ids)}")

    for case in corpus.cases:
        if not case.expected:
            raise ParserCorpusError(f"Parser corpus case {case.id} has no expected fields")
        if case.availability_check is not None and case.grounding != "observed_posting":
            raise ParserCorpusError(
                f"Availability corpus case {case.id} is not posting-grounded"
            )
        if case.category == "role_semantics":
            if case.grounding != "observed_posting":
                raise ParserCorpusError(
                    f"Role semantics corpus case {case.id} is not posting-grounded"
                )
            if case.grounding_source_sha256 is None:
                raise ParserCorpusError(
                    f"Role semantics corpus case {case.id} has no source fingerprint"
                )
        _validate_synthetic_case(case)


def _validate_synthetic_case(case: ParserCorpusCase) -> None:
    if case.source_url:
        parsed_url = urlparse(case.source_url)
        hostname = parsed_url.hostname
        if (
            parsed_url.scheme.casefold() not in {"http", "https"}
            or not hostname
            or not _is_safe_fixture_domain(hostname)
        ):
            raise ParserCorpusError(
                f"Parser corpus case {case.id} contains a non-example URL: {case.source_url}"
            )

    searchable = json.dumps(
        case.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    if _JOB_ID_PATTERN.search(searchable):
        raise ParserCorpusError(f"Parser corpus case {case.id} contains a persisted job id")

    for match in _EMAIL_PATTERN.finditer(searchable):
        if not _is_safe_fixture_domain(match.group(1)):
            raise ParserCorpusError(
                f"Parser corpus case {case.id} contains a non-example email address"
            )

    for match in _URL_PATTERN.finditer(searchable):
        raw_url = match.group(0).rstrip(".,);]")
        hostname = urlparse(raw_url).hostname
        if not hostname or not _is_safe_fixture_domain(hostname):
            raise ParserCorpusError(
                f"Parser corpus case {case.id} contains a non-example URL: {raw_url}"
            )

    schemeless_searchable = _schemeless_privacy_searchable(case)
    for match in _SCHEMELESS_HOST_PATTERN.finditer(schemeless_searchable):
        hostname = match.group("hostname")
        if (
            not _is_safe_fixture_domain(hostname)
            and not _is_dotted_technology_name(
                hostname,
                searchable=schemeless_searchable,
                end=match.end(),
            )
        ):
            raise ParserCorpusError(
                f"Parser corpus case {case.id} contains a non-example URL: {hostname}"
            )


def _schemeless_privacy_searchable(case: ParserCorpusCase) -> str:
    payload = case.model_dump(mode="json")
    expected = payload.get("expected")
    if isinstance(expected, dict):
        data_quality = expected.get("data_quality")
        if isinstance(data_quality, dict):
            ambiguous_fields = data_quality.get("ambiguous_fields")
            if isinstance(ambiguous_fields, list):
                data_quality["ambiguous_fields"] = [
                    ""
                    if isinstance(value, str)
                    and value.casefold() in _SAFE_AMBIGUOUS_FIELD_PATHS
                    else value
                    for value in ambiguous_fields
                ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _is_dotted_technology_name(
    value: str,
    *,
    searchable: str,
    end: int,
) -> bool:
    normalized = value.casefold().rstrip(".")
    is_known_technology = normalized in _DOTTED_TECHNOLOGY_NAMES
    if not is_known_technology:
        return False
    return searchable[end : end + 1] not in {"/", "\\", "?", "#", ":"}


def _is_safe_fixture_domain(hostname: str) -> bool:
    normalized = hostname.lower().rstrip(".")
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in _SAFE_FIXTURE_DOMAINS
    )


def _find_differences(
    case_id: str,
    expected: Any,
    actual: Any,
    *,
    path: str = "",
) -> list[CorpusDifference]:
    if actual is _MISSING:
        return [
            CorpusDifference(
                case_id=case_id,
                path=path or "<root>",
                expected=expected,
                actual=MISSING_VALUE,
            )
        ]

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [
                CorpusDifference(
                    case_id=case_id,
                    path=path or "<root>",
                    expected=expected,
                    actual=actual,
                )
            ]
        differences: list[CorpusDifference] = []
        for key, expected_value in expected.items():
            child_path = f"{path}.{key}" if path else key
            actual_value = actual[key] if key in actual else _MISSING
            differences.extend(
                _find_differences(
                    case_id,
                    expected_value,
                    actual_value,
                    path=child_path,
                )
            )
        return differences

    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [
                CorpusDifference(
                    case_id=case_id,
                    path=path or "<root>",
                    expected=expected,
                    actual=actual,
                )
            ]
        differences = []
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            differences.extend(
                _find_differences(
                    case_id,
                    expected_item,
                    actual_item,
                    path=f"{path}[{index}]" if path else f"[{index}]",
                )
            )
        return differences

    if expected != actual:
        return [
            CorpusDifference(
                case_id=case_id,
                path=path or "<root>",
                expected=expected,
                actual=actual,
            )
        ]
    return []


def _project_actual(actual: Any, expected: Any) -> Any:
    if actual is _MISSING:
        return MISSING_VALUE
    if not isinstance(expected, dict):
        if isinstance(expected, list) and isinstance(actual, list):
            return [
                _project_actual(actual_item, expected_item)
                for expected_item, actual_item in zip(expected, actual)
            ]
        return actual
    if not isinstance(actual, dict):
        return actual
    return {
        key: _project_actual(actual[key] if key in actual else _MISSING, expected_value)
        for key, expected_value in expected.items()
    }


def _format_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
