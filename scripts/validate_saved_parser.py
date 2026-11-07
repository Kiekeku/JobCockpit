"""Validate saved postings without changing the database or invoking an LLM provider."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from jobcockpit.ingestion.parser_corpus import load_parser_corpus
from jobcockpit.ingestion.saved_parser_validation import (
    capture_postings,
    parser_manifest,
    run_validation,
)

ROOT = Path(__file__).resolve().parents[1]


def private_output(value: Path) -> Path:
    path = value.resolve()
    if not path.is_relative_to((ROOT / "var").resolve()):
        raise ValueError("Private validation reports must be written under var/")
    if path.exists():
        raise ValueError("Report already exists; choose a new output to preserve prior evidence")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "representative", "incremental"), default="representative")
    parser.add_argument("--database", type=Path, default=ROOT / "var/jobcockpit.db")
    parser.add_argument("--artifact-root", type=Path, default=ROOT / "var/private/storage")
    parser.add_argument("--baseline", type=Path, help="Prior report to compare and reuse; never auto-overwritten")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--seed", default="jobcockpit-v1", help="Stable holdout seed; rotate for periodic spot checks")
    parser.add_argument("--holdout-fraction", type=float, default=0.05)
    parser.add_argument("--pin", action="append", default=[], help="Always include a saved job ID (repeatable)")
    parser.add_argument("--pins-file", type=Path, default=ROOT / "var/parser-validation/pins.json",
                        help="Private JSON list of historical failure IDs (inactive IDs are retained but skipped)")
    parser.add_argument("--reviewed", type=Path, help="Private JSON list of {id, review_key, note} adjudications")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        output = private_output(args.output)
        if not 1 <= args.workers <= 8 or not 0 <= args.holdout_fraction <= 1:
            raise ValueError("workers must be 1-8 and holdout fraction must be 0-1")
        if args.mode == "incremental" and not args.baseline:
            raise ValueError("Incremental validation requires --baseline; bootstrap with --mode full")
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        reviewed = json.loads(args.reviewed.read_text(encoding="utf-8")) if args.reviewed else []
        file_pins = json.loads(args.pins_file.read_text(encoding="utf-8")) if args.pins_file.exists() else []
        if not isinstance(file_pins, list) or not all(isinstance(id, str) for id in file_pins):
            raise ValueError("Pins file must contain a JSON list of job IDs")
        rows, failures = capture_postings(args.database, args.artifact_root)
        known_ids = {row.id for row in rows} | {row["id"] for row in failures}
        if set(args.pin) - known_ids:
            raise ValueError("A pinned job is not in the active view")
        grounded_hashes = {case.grounding_source_sha256 for case in load_parser_corpus().cases
                           if case.grounding_source_sha256}
        golden_pins = {row.id for row in rows if row.content_hash in grounded_hashes}
        pins = set(args.pin) | (set(file_pins) & known_ids) | golden_pins
        print(f"Read {len(rows)} postings; {len(failures)} artifact failures. Selecting/parsing...", flush=True)
        report = run_validation(rows, failures, parser_manifest(ROOT), mode=args.mode,
                                baseline=baseline, workers=args.workers, seed=args.seed,
                                holdout_fraction=args.holdout_fraction, pinned=pins, reviewed=reviewed)
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["baseline_path"] = str(args.baseline.resolve()) if args.baseline else None
        report["golden_source_pin_count"] = len(golden_pins)
        report["active_history_pin_count"] = len(set(file_pins) & known_ids)
        report["inactive_history_pins"] = sorted(set(file_pins) - known_ids)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2)
        print(json.dumps(report["summary"], indent=2))
        print(f"Private report: {output}\nElapsed: {report['elapsed_seconds']}s")
        print("Saved-value differences may reflect manual/provider edits. Coverage is not accuracy.")
        return 1 if report["summary"]["attention_count"] else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
