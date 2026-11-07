from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from jobcockpit.ingestion.parser_corpus import (
    DEFAULT_CORPUS_PATH,
    ParserCorpusError,
    load_parser_corpus,
    render_corpus_run,
    run_parser_corpus,
    write_current_snapshot,
)


def _worker_count(value: str) -> int:
    if value.casefold() == "auto":
        return min(8, os.cpu_count() or 1)
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be 'auto' or a positive integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be 'auto' or a positive integer")
    return workers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare current parser output with the anonymized golden corpus.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS_PATH,
        help=f"Corpus JSON path (default: {DEFAULT_CORPUS_PATH})",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run one case by id; repeat to select multiple cases.",
    )
    parser.add_argument(
        "--write-current",
        type=Path,
        help="Write current selected output to a JSON snapshot for before/after review.",
    )
    parser.add_argument(
        "--workers",
        type=_worker_count,
        default=_worker_count("auto"),
        help="Parallel parser workers: 'auto' (default, capped at 8) or a positive integer.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        corpus = load_parser_corpus(args.corpus)
        run = run_parser_corpus(
            corpus,
            case_ids=set(args.case_ids) if args.case_ids else None,
            workers=args.workers,
        )
        if args.write_current:
            write_current_snapshot(run, args.write_current)
    except ParserCorpusError as exc:
        print(f"Parser corpus error: {exc}")
        return 2

    print(render_corpus_run(run))
    if args.write_current:
        print(f"Current parser snapshot written to {args.write_current}")
    return 0 if run.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
