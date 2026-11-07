"""Read-only saved-posting validation. Sampling is coverage, never an accuracy claim."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from jobcockpit.storage.artifacts import ArtifactStorageService

SCHEMA_VERSION = 1
FIELDS = ("employer", "role", "employment_timing", "location", "eligibility",
          "compensation", "application", "data_quality")
# Changes outside these leaf extractors may alter every field or posting boundary.
LEAF_MODULES = {"eligibility_parser.py", "employment_timing.py", "month_range_timing.py",
                "compensation_parser.py", "application_documents.py", "location_parser.py"}
RISK_PATTERNS = {
    "alternative": r"\b(?:or|either|alternatively)\b",
    "conjunction": r"\band\b",
    "negation": r"\b(?:not|no|never|without)\b",
    "preference": r"\b(?:preferred|preference|asset|optional)\b",
    "parenthetical": r"\([^)]*\)",
    "condition": r"\b(?:if|unless|before|after|during|until)\b",
    "duration": r"\b\d+\s*[-–—]?\s*(?:months?|weeks?)\b",
    "date": r"\b(?:20\d{2}|January|February|March|April|May|June|July|August|September|October|November|December)\b",
    "season": r"\b(?:winter|spring|summer|fall)\b",
    "enrollment": r"\b(?:enrolled|enrollment|student|graduat\w*|co-?op)\b",
    "money": r"[$€£]|\b(?:CAD|USD|salary|hourly)\b",
    "documents": r"\b(?:resume|transcript|cover letter|portfolio)\b",
    "damaged_text": "\ufffd",
    "french": r"\b(?:stage|stagiaire|exigences|compétences)\b",
}
RISK_PATTERNS = {key: re.compile(value, re.I) for key, value in RISK_PATTERNS.items()}
HEADINGS = re.compile(
    r"\b(responsibilities|qualifications|requirements|what you(?:'|’)ll (?:do|bring|need)|"
    r"about (?:the role|the job|us)|benefits|preferred qualifications|"
    r"expected start date|duration|location|how to apply)\b", re.I,
)
START_SIGNAL = re.compile(
    r"\b(?:start date|expected start(?: date)?|beginning|commencing)\s*(?::|on\b|in\b)?\s*"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December|20\d{2}-\d{2})\b",
    re.I,
)


@dataclass(frozen=True)
class PostingInput:
    id: str
    artifact_id: str
    snapshot_hash: str
    content_hash: str
    source_type: str
    source_url: str | None
    title_hint: str | None
    text: str
    persisted: dict


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def input_key(row: PostingInput) -> str:
    return digest([row.artifact_id, row.snapshot_hash, row.content_hash,
                   row.source_type, row.source_url, row.title_hint])


def parser_manifest(root: Path) -> dict[str, str]:
    """Conservative invalidation: include Python sources, ontology, lock and runtime."""
    paths = sorted((root / "apps/api/jobcockpit").rglob("*.py"))
    paths += sorted((root / "apps/api/jobcockpit").rglob("*.yaml"))
    paths += [root / "uv.lock", root / "pyproject.toml"]
    result = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in paths if p.is_file()}
    result["python"] = sys.version
    return result


def capture_postings(database: Path, artifact_root: Path) -> tuple[list[PostingInput], list[dict]]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        jobs = [dict(row) for row in connection.execute("SELECT * FROM active_job_postings ORDER BY id")]
        artifacts = {row["artifact_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM stored_artifacts WHERE artifact_id IN "
            "(SELECT raw_content_artifact_id FROM active_job_postings)"
        )}
        storage = ArtifactStorageService(artifact_root)
        inputs, failures = [], []
        for job in jobs:
            try:
                artifact = artifacts[job["raw_content_artifact_id"]]
                if (artifact["deleted_at"] or artifact["job_id"] != job["id"]
                        or artifact["artifact_type"] != "job_snapshot"):
                    raise ValueError("missing, deleted, or incorrectly owned snapshot")
                content = storage.read_bytes(artifact["storage_uri"])
                content_hash = hashlib.sha256(content).hexdigest()
                if content_hash != artifact["sha256"] or len(content) != artifact["size_bytes"]:
                    raise ValueError("artifact integrity mismatch")
                persisted = {field: json.loads(job[field]) for field in FIELDS}
                inputs.append(PostingInput(
                    job["id"], artifact["artifact_id"], job["posting_snapshot_hash"],
                    content_hash, job["source_type"], job["source_url"],
                    persisted["role"].get("job_title"), content.decode("utf-8"), persisted,
                ))
            except Exception as exc:
                failures.append({"id": job["id"], "artifact_id": job["raw_content_artifact_id"],
                                 "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        return inputs, failures
    finally:
        connection.close()  # No live database/storage objects ever enter the pool.


def structure(row: PostingInput) -> tuple[str, dict]:
    text = row.text
    headings = [m.group().casefold().replace("’", "'") for m in HEADINGS.finditer(text)]
    # Collapse repeats from duplicate page chrome without using employer identity.
    headings = list(dict.fromkeys(headings))
    features = sorted(key for key, pattern in RISK_PATTERNS.items() if pattern.search(text))
    lines = [line for line in text.splitlines() if line.strip()]
    profile = {
        "source_type": row.source_type,
        "source_host": (urlsplit(row.source_url or "").hostname or "local").lower(),
        "layout": "html" if re.search(r"<(?:p|div|li|br)\b", text, re.I) else (
            "flattened" if any(len(line) > 800 for line in lines) else "lines"
        ),
        "bullets": bool(re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s", text)),
        "headings": headings,
        "features": features,
    }
    return digest(profile), profile


def coverage_tokens(row: PostingInput) -> frozenset[str]:
    """Cover structures and interactions without insisting on every Cartesian product."""
    _, profile = structure(row)
    host = profile["source_host"]
    families = ("linkedin.com", "indeed.com", "myworkdayjobs.com", "myworkdaysite.com",
                "recruitee.com", "oraclecloud.com", "ashbyhq.com", "lever.co",
                "greenhouse.io", "icims.com", "successfactors.com", "ultipro.ca",
                "dayforcehcm.com")
    family = next((name for name in families if host == name or host.endswith("." + name)), "other")
    layout = profile["layout"]
    source = profile["source_type"]
    tokens = {digest(["format", source, family, layout, profile["bullets"]]),
              digest(["risk_combination", profile["features"]])}
    for left, right in pairwise(["<start>", *profile["headings"], "<end>"]):
        tokens.add(digest(["heading_transition", layout, left, right]))
    for feature in profile["features"]:
        tokens.add(digest(["source_risk", source, feature]))
    length = len(row.text)
    bucket = next((limit for limit in (500, 2000, 8000, 20000) if length <= limit), "long")
    tokens.add(digest(["length", layout, bucket]))
    return frozenset(tokens)


def select_representatives(rows, *, pinned=(), seed="jobcockpit-v1", holdout_fraction=0.05):
    if not 0 <= holdout_fraction <= 1:
        raise ValueError("holdout fraction must be between 0 and 1")
    reasons = defaultdict(set)
    features = {row.id: coverage_tokens(row) for row in rows}
    uncovered = set().union(*features.values())
    for row in rows:
        if row.id in pinned:
            reasons[row.id].add("historical_failure")
            uncovered.difference_update(features[row.id])
    candidates = sorted(features)
    while uncovered:
        best = max(candidates, key=lambda id: len(features[id] & uncovered))
        reasons[best].add("coverage_representative")
        uncovered.difference_update(features[best])
        candidates.remove(best)
    remaining = sorted((row for row in rows if row.id not in reasons),
                       key=lambda row: (digest([seed, row.id]), row.id))
    for row in remaining[:math.ceil(len(remaining) * holdout_fraction)]:
        reasons[row.id].add("holdout")
    return {id: sorted(why) for id, why in sorted(reasons.items())}


def differences(before, after, prefix="") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(differences(before[key], after[key], path))
        return result
    return [prefix] if before != after else []


def parse_posting(row: PostingInput) -> dict:
    from jobcockpit.ingestion.parser import parse_job_text

    try:
        output = parse_job_text(row.text, row.source_url, source_type=row.source_type,
                                title_hint=row.title_hint).model_dump(mode="json")
        output = {field: output[field] for field in FIELDS}
        signals = []
        # A triage hint, not a claim that this date necessarily belongs to the role.
        timing = output["employment_timing"]
        date_match = START_SIGNAL.search(row.text)
        if date_match and not timing.get("start_date"):
            signals.append({"kind": "start_signal_without_date", "context": row.text[
                max(0, date_match.start() - 80):date_match.end() + 120]})
        return {"output": output, "signals": signals, "error": None}
    except Exception as exc:
        return {"output": None, "signals": [],
                "error": f"{type(exc).__name__}: {str(exc)[:250]}"}


def validate_baseline(baseline):
    if baseline is None:
        return {}
    try:
        if baseline["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported version")
        records = baseline["records"]
        indexed = {r["id"]: r for r in records}
        if len(indexed) != len(records) or not isinstance(baseline["parser_manifest"], dict):
            raise ValueError("duplicate IDs or missing parser manifest")
        for r in records:
            for key in ("input_key", "parser_fingerprint", "result", "attention", "structure_id"):
                if key not in r:
                    raise ValueError(f"missing {key}")
            if r["result"] is not None and r["result"]["output"] is not None:
                if r["output_hash"] != digest(r["result"]["output"]):
                    raise ValueError("output hash mismatch")
        return indexed
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid validation baseline: {exc}") from exc


def run_validation(rows, failures, manifest, *, mode="representative", baseline=None,
                   workers=1, seed="jobcockpit-v1", holdout_fraction=0.05, pinned=(),
                   reviewed=(), parse=parse_posting):
    if mode not in {"full", "representative", "incremental"} or not 1 <= workers <= 8:
        raise ValueError("invalid validation mode or worker count (1-8)")
    old = validate_baseline(baseline)
    fingerprint = digest(manifest)
    previous_manifest = baseline["parser_manifest"] if baseline else manifest
    changed_modules = differences(previous_manifest, manifest)
    shared_change = any(Path(path).name not in LEAF_MODULES for path in changed_modules)
    effective = "full" if shared_change and mode == "representative" else mode
    old_shapes = {record["structure_id"] for record in old.values()}
    pinned = set(pinned) | {id for id, r in old.items() if r["attention"] or r.get("historical_failure")}
    reasons = select_representatives(rows, pinned=pinned, seed=seed,
                                     holdout_fraction=holdout_fraction) if effective == "representative" else {}
    for row in rows:
        prior = old.get(row.id)
        exact = prior and prior["input_key"] == input_key(row)
        if effective == "full":
            reasons[row.id] = ["full_scan"]
        elif effective == "incremental":
            if not (exact and prior["parser_fingerprint"] == fingerprint
                    and prior["result"] and not prior["result"]["error"]):
                reasons[row.id] = ["new_changed_or_uncached"]
        elif baseline and not exact:
            reasons.setdefault(row.id, []).append("new_or_changed_input")
    selected = [row for row in rows if row.id in reasons]
    # Workers receive immutable inputs with no persisted DB projections.
    from dataclasses import replace
    worker_rows = [replace(row, persisted={}) for row in selected]
    if workers > 1 and worker_rows:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(parse, worker_rows))
    else:
        results = [parse(row) for row in worker_rows]
    fresh = dict(zip((row.id for row in selected), results, strict=True))
    reviewed_keys = {(r["id"], r["review_key"]) for r in reviewed if r.get("note", "").strip()}
    records = []
    for row in rows:
        key = input_key(row)
        prior = old.get(row.id)
        exact = bool(prior and prior["input_key"] == key)
        result = fresh.get(row.id)
        cached = False
        if result is None and exact and prior["parser_fingerprint"] == fingerprint:
            result = prior["result"]
            cached = bool(result and not result["error"])
        output = result["output"] if result else None
        old_output = prior["result"]["output"] if exact and prior["result"] else None
        changes = differences(old_output, output) if old_output is not None and output is not None else []
        persisted = differences(row.persisted, output) if output is not None else []
        attention = set(prior["attention"] if exact else [])
        if changes:
            attention.add("parser_output_changed")
        if result and result["error"]:
            attention.add("parse_error")
        else:
            attention.discard("parse_error")
        if result and result["signals"]:
            attention.add("source_signal")
        else:
            attention.discard("source_signal")
        review_key = digest([key, output, row.persisted, result["signals"] if result else None])
        if (row.id, review_key) in reviewed_keys and result and not result["error"]:
            attention.clear()
        shape, profile = structure(row)
        records.append({
            "id": row.id, "artifact_id": row.artifact_id, "snapshot_hash": row.snapshot_hash,
            "input_key": key, "parser_fingerprint": fingerprint if result else None,
            "structure_id": shape, "structure": profile,
            "novel_structure": bool(baseline and shape not in old_shapes),
            "selection_reasons": reasons.get(row.id, []), "reused": cached,
            "result": result, "output_hash": digest(output) if output is not None else None,
            "changed_fields": changes, "persisted_different_fields": persisted,
            "attention": sorted(attention), "review_key": review_key,
            "historical_failure": bool(row.id in pinned or changes or (result and result["error"])),
        })
    summary = {
        "requested_mode": mode, "effective_mode": effective,
        "forced_full_reason": "shared_parser_change" if effective != mode else None,
        "active_count": len(rows) + len(failures), "readable_count": len(rows),
        "structure_count": len({r["structure_id"] for r in records}),
        "selected_structure_count": len({r["structure_id"] for r in records if r["selection_reasons"]}),
        "parsed_count": len(selected), "reused_count": sum(r["reused"] for r in records),
        "skipped_count": sum(r["result"] is None for r in records),
        "artifact_failure_count": len(failures),
        "parse_failure_count": sum(bool(r["result"] and r["result"]["error"]) for r in records),
        "changed_count": sum(bool(r["changed_fields"]) for r in records),
        "persisted_difference_count": sum(bool(r["persisted_different_fields"]) for r in records),
        "source_signal_count": sum(bool(r["result"] and r["result"]["signals"]) for r in records),
        "attention_count": len(failures) + sum(bool(r["attention"]) for r in records),
        "novel_structure_count": len({r["structure_id"] for r in records if r["novel_structure"]}),
    }
    all_tokens = set().union(*(coverage_tokens(row) for row in rows))
    selected_tokens = set().union(*(coverage_tokens(row) for row in selected))
    summary["coverage_token_count"] = len(all_tokens)
    summary["selected_coverage_token_count"] = len(selected_tokens)
    summary["complete_current_coverage"] = not (
        summary["skipped_count"] or summary["artifact_failure_count"] or summary["parse_failure_count"]
    )
    return {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(UTC).isoformat(),
            "parser_manifest": manifest, "summary": summary, "records": records,
            "artifact_failures": failures, "seed": seed, "holdout_fraction": holdout_fraction,
            "changed_modules": changed_modules}
