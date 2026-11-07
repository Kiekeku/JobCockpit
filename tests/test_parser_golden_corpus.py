from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jobcockpit.ingestion.parser_corpus import (
    DEFAULT_CORPUS_PATH,
    ParserCorpus,
    ParserCorpusCase,
    ParserCorpusError,
    load_parser_corpus,
    render_corpus_run,
    run_parser_corpus,
    write_current_snapshot,
    _find_differences,
)
from scripts.parser_corpus_diff import main as parser_corpus_main


_GOLDEN_CORPUS = load_parser_corpus()
_GOLDEN_CORPUS_CHUNK_COUNT = 16


def _golden_corpus_case_ids_for_chunk(chunk_index: int) -> set[str]:
    return {
        case.id
        for index, case in enumerate(_GOLDEN_CORPUS.cases)
        if index % _GOLDEN_CORPUS_CHUNK_COUNT == chunk_index
    }


@pytest.mark.parametrize(
    "chunk_index",
    range(_GOLDEN_CORPUS_CHUNK_COUNT),
    ids=lambda index: f"chunk-{index + 1:02d}",
)
def test_parser_golden_corpus_matches_approved_outputs(chunk_index: int) -> None:
    case_ids = _golden_corpus_case_ids_for_chunk(chunk_index)

    run = run_parser_corpus(_GOLDEN_CORPUS, case_ids=case_ids)

    assert run.passed, render_corpus_run(run)
    assert len(run.case_ids) >= 15


def test_corpus_list_items_allow_additive_object_fields_but_preserve_shape() -> None:
    expected = [{"kind": "qualification"}]
    actual = [{"kind": "qualification", "decision_trace": ["kind:qualification"]}]

    assert _find_differences("additive-list-object", expected, actual) == []
    assert _find_differences("list-length", expected, [*actual, actual[0]])


def test_role_semantics_cases_are_marked_as_observed_posting_grounded() -> None:
    cases = [
        case for case in load_parser_corpus().cases if case.category == "role_semantics"
    ]

    assert len(cases) == 32
    assert {case.grounding for case in cases} == {"observed_posting"}
    assert all(case.grounding_source_sha256 is not None for case in cases)


def test_role_semantics_contrast_cases_are_synthetic_and_ungrounded() -> None:
    cases = [
        case
        for case in load_parser_corpus().cases
        if case.category == "role_semantics_contrast"
    ]

    assert len(cases) >= 17
    assert {case.provenance for case in cases} == {"synthetic_minimized"}
    assert {case.grounding for case in cases} == {None}
    assert {case.grounding_source_sha256 for case in cases} == {None}


def test_role_semantics_case_requires_source_fingerprint(tmp_path: Path) -> None:
    corpus_path = tmp_path / "missing-role-source-fingerprint.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Role semantics provenance probe.",
                "cases": [
                    {
                        "id": "missing_role_source_fingerprint",
                        "category": "role_semantics",
                        "provenance": "synthetic_minimized",
                        "grounding": "observed_posting",
                        "raw_text": "Title: Engineer\nQualifications\nPython experience.",
                        "source_url": "https://jobs.example.test/provenance-probe",
                        "expected": {"role": {"required_capabilities": ["Python"]}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="source fingerprint"):
        load_parser_corpus(corpus_path)


@pytest.mark.parametrize("hostname", ["role.jobs", "role.evidence"])
def test_parser_corpus_rejects_hostname_shaped_model_prefixes(
    tmp_path: Path,
    hostname: str,
) -> None:
    corpus_path = tmp_path / "hostname-shaped-model-prefix.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Privacy-boundary probe.",
                "cases": [
                    {
                        "id": "hostname_shaped_model_prefix",
                        "category": "privacy_probe",
                        "provenance": "synthetic_minimized",
                        "raw_text": f"Apply at {hostname} for details.",
                        "source_url": "https://jobs.example.test/privacy-probe",
                        "expected": {"role": {"job_title": "Probe"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match=hostname):
        load_parser_corpus(corpus_path)


def test_parser_corpus_reports_exact_drift_path() -> None:
    corpus = ParserCorpus(
        schema_version=1,
        description="Synthetic drift probe.",
        cases=[
            ParserCorpusCase(
                id="drift_probe",
                category="test",
                provenance="synthetic_minimized",
                raw_text="Summer Intern\nThis is a 6 month internship.",
                source_url="https://jobs.example.test/drift-probe",
                expected={"employment_timing": {"duration_months": 99}},
            )
        ],
    )

    run = run_parser_corpus(corpus)

    assert not run.passed
    assert len(run.differences) == 1
    assert run.differences[0].case_id == "drift_probe"
    assert run.differences[0].path == "employment_timing.duration_months"
    assert run.differences[0].expected == 99
    assert run.differences[0].actual == 6


def test_parser_corpus_parallel_run_matches_serial_order_and_results() -> None:
    source = load_parser_corpus()
    corpus = source.model_copy(update={"cases": source.cases[:4]})

    serial = run_parser_corpus(corpus)
    parallel = run_parser_corpus(corpus, workers=2)

    assert parallel == serial


def test_parser_corpus_cli_exits_nonzero_and_prints_field_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus_path = tmp_path / "drift.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Synthetic CLI drift probe.",
                "cases": [
                    {
                        "id": "cli_drift_probe",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Summer Intern\nThis is a 6 month internship.",
                        "source_url": "https://jobs.example.test/cli-drift-probe",
                        "source_type": "paste",
                        "expected": {"employment_timing": {"duration_months": 99}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = parser_corpus_main(["--corpus", str(corpus_path)])

    assert exit_code == 1
    assert "cli_drift_probe: employment_timing.duration_months" in capsys.readouterr().out


def test_parser_corpus_can_write_bounded_current_snapshot(tmp_path: Path) -> None:
    corpus = load_parser_corpus()
    run = run_parser_corpus(corpus, case_ids={"academic_term_from_title"})
    snapshot_path = tmp_path / "current.json"

    write_current_snapshot(run, snapshot_path)

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["case_count"] == 1
    assert snapshot["cases"] == [
        {
            "id": "academic_term_from_title",
            "actual": {
                "role": {"job_title": "Software Developer Co-Op - Fall 2026"},
                "employment_timing": {
                    "raw_label": "Fall 2026",
                    "timing_type": "fixed_term",
                    "start_date": "2026-09-01",
                    "end_date": "2026-12-31",
                    "duration_months": 4,
                    "hours_per_week_min": None,
                    "hours_per_week_max": None,
                    "schedule_type": "full_time",
                },
            },
        }
    ]


def test_committed_parser_corpus_is_anonymized() -> None:
    raw_corpus = DEFAULT_CORPUS_PATH.read_text(encoding="utf-8")

    assert "localhost" not in raw_corpus
    assert re.search(r"\bjob_[0-9a-f]{16,}\b", raw_corpus, re.IGNORECASE) is None
    assert "var/private" not in raw_corpus


def test_parser_corpus_rejects_non_example_urls(tmp_path: Path) -> None:
    corpus_path = tmp_path / "unsafe.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Unsafe corpus probe.",
                "cases": [
                    {
                        "id": "unsafe_url",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Software Intern",
                        "source_url": "https://jobs.real-company.com/posting",
                        "source_type": "paste",
                        "expected": {"role": {"job_title": "Software Intern"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="non-example URL"):
        load_parser_corpus(corpus_path)


@pytest.mark.parametrize(
    "private_url",
    [
        "jobs.real-company.com/posting/123",
        "www.linkedin.com/jobs/view/123",
        "asp.net/private/path",
        r"asp.net\private\path",
    ],
)
def test_parser_corpus_rejects_scheme_less_non_example_urls(
    tmp_path: Path,
    private_url: str,
) -> None:
    corpus_path = tmp_path / "unsafe-scheme-less.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Unsafe scheme-less corpus probe.",
                "cases": [
                    {
                        "id": "unsafe_scheme_less_url",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": f"Software Intern\nApply at {private_url}",
                        "expected": {"role": {"job_title": "Software Intern"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="non-example URL"):
        load_parser_corpus(corpus_path)


@pytest.mark.parametrize(
    ("private_value", "expected_error"),
    [
        ("https://jobs." + "real-company.com/posting", "non-example URL"),
        ("candidate" + "@real-company.com", "non-example email address"),
        ("job_" + "a" * 32, "persisted job id"),
    ],
)
def test_parser_corpus_rejects_private_values_inside_expected_outputs(
    tmp_path: Path,
    private_value: str,
    expected_error: str,
) -> None:
    corpus_path = tmp_path / "unsafe-expected.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Unsafe expected-output probe.",
                "cases": [
                    {
                        "id": "unsafe_expected",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Software Intern",
                        "source_url": "https://jobs.example.test/posting",
                        "source_type": "paste",
                        "expected": {"role": {"job_title": private_value}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match=expected_error):
        load_parser_corpus(corpus_path)


def test_parser_corpus_rejects_unknown_provider_timing_fields(tmp_path: Path) -> None:
    corpus_path = tmp_path / "unknown-provider-field.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Strict provider timing fixture probe.",
                "cases": [
                    {
                        "id": "unknown_provider_field",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Software Intern",
                        "provider_timing": {"description": "must not be silently ignored"},
                        "expected": {"role": {"job_title": "Software Intern"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="provider_timing.description"):
        load_parser_corpus(corpus_path)

@pytest.mark.parametrize("technology", ["Node.js", "Vue.js", "React.js", "ASP.NET"])
def test_parser_corpus_accepts_dotted_technology_names(
    tmp_path: Path,
    technology: str,
) -> None:
    corpus_path = tmp_path / "dotted-technology.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Dotted technology fixture probe.",
                "cases": [
                    {
                        "id": "dotted_technology",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": f"{technology} Developer",
                        "source_url": "https://jobs.example.test/dotted-technology",
                        "source_type": "paste",
                        "expected": {"role": {"job_title": f"{technology} Developer"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    corpus = load_parser_corpus(corpus_path)

    assert corpus.cases[0].raw_text == f"{technology} Developer"

def test_parser_corpus_rejects_non_allowlisted_dotted_js_hostname(
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "dotted-js-hostname.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Unsafe bare hostname probe.",
                "cases": [
                    {
                        "id": "dotted_js_hostname",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Apply at acme.js",
                        "source_url": "https://jobs.example.test/dotted-js-hostname",
                        "source_type": "paste",
                        "expected": {"role": {"job_title": "Apply at acme.js"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="non-example URL"):
        load_parser_corpus(corpus_path)


@pytest.mark.parametrize(
    "source_url",
    ["asp.net", "node.js", "ftp://jobs.example.test/posting"],
)
def test_parser_corpus_rejects_invalid_source_url(
    tmp_path: Path,
    source_url: str,
) -> None:
    corpus_path = tmp_path / "dotted-source-url.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Unsafe dotted source URL probe.",
                "cases": [
                    {
                        "id": "dotted_source_url",
                        "category": "test",
                        "provenance": "synthetic_minimized",
                        "raw_text": "Software Developer",
                        "source_url": source_url,
                        "source_type": "paste",
                        "expected": {"role": {"job_title": "Software Developer"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ParserCorpusError, match="non-example URL"):
        load_parser_corpus(corpus_path)
