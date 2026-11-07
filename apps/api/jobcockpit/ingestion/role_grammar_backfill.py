from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sqlite3
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from jobcockpit.db.models import AuditLogEntry, JobPosting, StoredArtifact
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.storage.artifacts import ArtifactStorageService


ROLE_GRAMMAR_FIELDS = (
    "responsibilities",
    "must_have_requirements",
    "nice_to_have_requirements",
    "evidence",
    "required_capabilities",
    "preferred_capabilities",
    "responsibility_capabilities",
    "environment_capabilities",
    "capabilities",
    "capability_domains",
    "tech_stack",
)
_MANAGED_MISSING_FIELDS = frozenset({"role.tech_stack"})
_MANAGED_AMBIGUOUS_FIELDS = frozenset({"role.evidence"})
_SAMPLE_LIMIT = 20
DEFAULT_MAX_SNAPSHOT_BYTES = 100_000


@dataclass(frozen=True)
class RoleGrammarChange:
    job: JobPosting
    role: dict[str, Any]
    data_quality: dict[str, Any]
    changed_fields: tuple[str, ...]
    previous_counts: dict[str, int]
    current_counts: dict[str, int]
    semantic_delta: dict[str, Any]


def active_role_grammar_job_ids(db: Session) -> list[str]:
    return list(
        db.execute(
            select(JobPosting.id)
            .where(JobPosting.status == "active")
            .order_by(JobPosting.id)
        ).scalars()
    )


def backfill_role_grammar(
    db: Session,
    storage: ArtifactStorageService,
    *,
    apply: bool = False,
    job_ids: list[str] | None = None,
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> dict[str, Any]:
    query = select(JobPosting).where(JobPosting.status == "active")
    if job_ids is not None:
        if not job_ids:
            return empty_role_grammar_backfill_result(apply=apply)
        query = query.where(JobPosting.id.in_(job_ids))
    jobs = list(db.execute(query.order_by(JobPosting.id)).scalars())

    changes: list[RoleGrammarChange] = []
    errors: list[dict[str, str]] = []
    skips: list[dict[str, Any]] = []
    changed_fields: Counter[str] = Counter()
    gained_evidence_count = 0
    gained_claims_count = 0
    samples: list[dict[str, Any]] = []

    for job in jobs:
        try:
            artifact = db.get(StoredArtifact, job.raw_content_artifact_id)
            if artifact is None or artifact.deleted_at is not None:
                raise ValueError("raw snapshot artifact is unavailable")
            if artifact.size_bytes > max_snapshot_bytes:
                skips.append(
                    {
                        "job_id": job.id,
                        "reason": "snapshot_too_large",
                        "size_bytes": artifact.size_bytes,
                    }
                )
                continue
            raw_text = storage.read_bytes(artifact.storage_uri).decode("utf-8")
            parsed = parse_job_text(
                raw_text,
                job.source_url,
                source_type=job.source_type,
            )
            role, data_quality = _updated_payloads(job, parsed)
            field_names = tuple(
                [
                    f"role.{field}"
                    for field in ROLE_GRAMMAR_FIELDS
                    if role.get(field) != (job.role or {}).get(field)
                ]
                + (["data_quality"] if data_quality != (job.data_quality or {}) else [])
            )
            if not field_names:
                continue

            previous_counts = _role_counts(job.role or {})
            current_counts = _role_counts(role)
            if previous_counts["evidence"] == 0 and current_counts["evidence"] > 0:
                gained_evidence_count += 1
            if previous_counts["claims"] == 0 and current_counts["claims"] > 0:
                gained_claims_count += 1
            changed_fields.update(field_names)
            change = RoleGrammarChange(
                job=job,
                role=role,
                data_quality=data_quality,
                changed_fields=field_names,
                previous_counts=previous_counts,
                current_counts=current_counts,
                semantic_delta=_semantic_delta(job.role or {}, role),
            )
            changes.append(change)
            if len(samples) < _SAMPLE_LIMIT:
                samples.append(_change_sample(change))
        except (AttributeError, OSError, UnicodeDecodeError, ValueError) as error:
            errors.append({"job_id": job.id, "error": type(error).__name__})

    if apply and not errors:
        _apply_changes(db, changes)

    return {
        "mode": "apply" if apply else "dry_run",
        "scanned_count": len(jobs),
        "changed_count": len(changes),
        "unchanged_count": len(jobs) - len(changes) - len(errors) - len(skips),
        "error_count": len(errors),
        "errors": errors,
        "skipped_count": len(skips),
        "skips": skips,
        "changed_fields": dict(sorted(changed_fields.items())),
        "gained_evidence_count": gained_evidence_count,
        "gained_claims_count": gained_claims_count,
        "samples": samples,
        "applied": apply and not errors,
    }


def empty_role_grammar_backfill_result(*, apply: bool) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry_run",
        "scanned_count": 0,
        "changed_count": 0,
        "unchanged_count": 0,
        "error_count": 0,
        "errors": [],
        "skipped_count": 0,
        "skips": [],
        "changed_fields": {},
        "gained_evidence_count": 0,
        "gained_claims_count": 0,
        "samples": [],
        "applied": apply,
    }


def merge_role_grammar_backfill_results(
    results: list[dict[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    merged = empty_role_grammar_backfill_result(apply=apply)
    field_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for result in results:
        for key in (
            "scanned_count",
            "changed_count",
            "unchanged_count",
            "error_count",
            "skipped_count",
            "gained_evidence_count",
            "gained_claims_count",
        ):
            merged[key] += int(result.get(key) or 0)
        merged["errors"].extend(result.get("errors") or [])
        merged["skips"].extend(result.get("skips") or [])
        field_counts.update(result.get("changed_fields") or {})
        samples.extend((result.get("samples") or [])[: _SAMPLE_LIMIT - len(samples)])
    merged["changed_fields"] = dict(sorted(field_counts.items()))
    merged["samples"] = samples
    merged["applied"] = apply and not merged["error_count"]
    return merged


def _updated_payloads(job: JobPosting, parsed: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_role = job.role if isinstance(job.role, dict) else {}
    parsed_role = parsed.role.model_dump(mode="json")
    role = dict(existing_role)
    for field in ROLE_GRAMMAR_FIELDS:
        role[field] = parsed_role[field]
    data_quality = _updated_data_quality(
        job,
        parsed.data_quality.model_dump(mode="json"),
    )
    return role, data_quality


def _updated_data_quality(
    job: JobPosting,
    parsed_data_quality: dict[str, Any],
) -> dict[str, Any]:
    existing = dict(job.data_quality or {})
    previous_missing = [str(value) for value in existing.get("missing_fields") or []]
    previous_ambiguous = [str(value) for value in existing.get("ambiguous_fields") or []]
    previous_inferred = [str(value) for value in existing.get("inferred_fields") or []]

    missing = [value for value in previous_missing if value not in _MANAGED_MISSING_FIELDS]
    missing.extend(
        value
        for value in parsed_data_quality.get("missing_fields") or []
        if value in _MANAGED_MISSING_FIELDS and value not in missing
    )
    ambiguous = [
        value for value in previous_ambiguous if value not in _MANAGED_AMBIGUOUS_FIELDS
    ]
    ambiguous.extend(
        value
        for value in parsed_data_quality.get("ambiguous_fields") or []
        if value in _MANAGED_AMBIGUOUS_FIELDS and value not in ambiguous
    )
    existing["missing_fields"] = missing
    existing["ambiguous_fields"] = ambiguous

    pending_hash = existing.get("manual_review_pending_snapshot_hash")
    completed_at = existing.get("manual_review_completed_at")
    completed_hash = existing.get("manual_review_completed_snapshot_hash")
    if pending_hash == job.posting_snapshot_hash:
        existing["needs_manual_review"] = True
        return existing
    if completed_at and completed_hash == job.posting_snapshot_hash:
        existing["needs_manual_review"] = False
        return existing
    if any(value in _MANAGED_AMBIGUOUS_FIELDS for value in ambiguous):
        existing["needs_manual_review"] = True
        return existing

    previous_reasons = [*previous_missing, *previous_ambiguous, *previous_inferred]
    current_reasons = [*missing, *ambiguous, *previous_inferred]
    removed_managed_reason = any(
        value in _MANAGED_MISSING_FIELDS or value in _MANAGED_AMBIGUOUS_FIELDS
        for value in previous_reasons
    )
    only_managed_previous_reasons = bool(previous_reasons) and all(
        value in _MANAGED_MISSING_FIELDS or value in _MANAGED_AMBIGUOUS_FIELDS
        for value in previous_reasons
    )
    if (
        existing.get("needs_manual_review") is True
        and removed_managed_reason
        and only_managed_previous_reasons
        and not current_reasons
        and float(existing.get("extraction_confidence", 0.7)) >= 0.7
    ):
        existing["needs_manual_review"] = False
    return existing


def _role_counts(role: dict[str, Any]) -> dict[str, int]:
    evidence = [item for item in role.get("evidence") or [] if isinstance(item, dict)]
    return {
        "responsibilities": len(role.get("responsibilities") or []),
        "must_have_requirements": len(role.get("must_have_requirements") or []),
        "nice_to_have_requirements": len(role.get("nice_to_have_requirements") or []),
        "evidence": len(evidence),
        "claims": sum(len(item.get("claims") or []) for item in evidence),
        "capabilities": len(role.get("capabilities") or []),
        "tech_stack": len(role.get("tech_stack") or []),
    }


def _change_sample(change: RoleGrammarChange) -> dict[str, Any]:
    return {
        "job_id": change.job.id,
        "title": (change.job.role or {}).get("job_title") or change.job.normalized_title,
        "changed_fields": list(change.changed_fields),
        "previous_counts": change.previous_counts,
        "current_counts": change.current_counts,
        "semantic_delta": change.semantic_delta,
    }


def _apply_changes(db: Session, changes: list[RoleGrammarChange]) -> None:
    changed_at = datetime.now(UTC)
    for change in changes:
        change.job.role = change.role
        change.job.data_quality = change.data_quality
        change.job.updated_at = changed_at
        db.add(
            AuditLogEntry.create(
                "job.role_grammar_backfilled",
                "job_posting",
                change.job.id,
                payload={
                    "changed_fields": list(change.changed_fields),
                    "previous_counts": change.previous_counts,
                    "current_counts": change.current_counts,
                    "semantic_delta": change.semantic_delta,
                    "posting_snapshot_hash": change.job.posting_snapshot_hash,
                },
            )
        )


def _semantic_delta(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    previous_required = _semantic_text_map(previous.get("must_have_requirements"))
    previous_preferred = _semantic_text_map(previous.get("nice_to_have_requirements"))
    current_required = _semantic_text_map(current.get("must_have_requirements"))
    current_preferred = _semantic_text_map(current.get("nice_to_have_requirements"))
    previous_requirements = {**previous_required, **previous_preferred}
    current_requirements = {**current_required, **current_preferred}
    previous_capabilities = _semantic_text_map(previous.get("capabilities"))
    current_capabilities = _semantic_text_map(current.get("capabilities"))
    previous_claims = _claim_semantics(previous)
    current_claims = _claim_semantics(current)
    changed_kinds, changed_modalities = _claim_semantic_changes(
        previous_claims,
        current_claims,
    )
    previous_flags = _role_quality_flags(previous)
    current_flags = _role_quality_flags(current)
    previous_required_keys = set(previous_required)
    previous_preferred_keys = set(previous_preferred)
    current_required_keys = set(current_required)
    current_preferred_keys = set(current_preferred)
    moved_to_required = (previous_preferred_keys - previous_required_keys) & (
        current_required_keys - current_preferred_keys
    )
    moved_to_preferred = (previous_required_keys - previous_preferred_keys) & (
        current_preferred_keys - current_required_keys
    )
    return {
        "requirements_added": _semantic_values(
            current_requirements,
            current_requirements.keys() - previous_requirements.keys(),
        ),
        "requirements_removed": _semantic_values(
            previous_requirements,
            previous_requirements.keys() - current_requirements.keys(),
        ),
        "requirements_moved_to_required": _semantic_values(
            current_required,
            moved_to_required,
        ),
        "requirements_moved_to_preferred": _semantic_values(
            current_preferred,
            moved_to_preferred,
        ),
        "capabilities_added": _semantic_values(
            current_capabilities,
            current_capabilities.keys() - previous_capabilities.keys(),
        ),
        "capabilities_removed": _semantic_values(
            previous_capabilities,
            previous_capabilities.keys() - current_capabilities.keys(),
        ),
        "claims_with_changed_kind": changed_kinds,
        "claims_with_changed_modality": changed_modalities,
        "quality_flags_added": sorted(current_flags - previous_flags),
        "quality_flags_removed": sorted(previous_flags - current_flags),
    }


def _semantic_text_map(values: Any) -> dict[str, str]:
    return {
        _normalized_semantic_text(value): value.strip()
        for value in values or []
        if isinstance(value, str) and value.strip()
    }


def _normalized_semantic_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _semantic_values(mapping: dict[str, str], keys: Any) -> list[Any]:
    selected = set(keys)
    return [_bounded_semantic_text(value) for key, value in mapping.items() if key in selected]


def _bounded_semantic_text(value: str) -> Any:
    if len(value) <= 160:
        return value
    return {
        "preview": f"{value[:157]}...",
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _claim_semantics(role: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    claims: dict[str, list[dict[str, str]]] = {}
    for evidence in role.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        for claim in evidence.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            if not text:
                continue
            key = "|".join(
                (
                    _normalized_semantic_text(text),
                    str(claim.get("source_start") or ""),
                    str(claim.get("source_end") or ""),
                )
            )
            claims.setdefault(key, []).append(
                {
                    "text": text,
                    "kind": str(claim.get("kind") or "unknown"),
                    "modality": str(claim.get("modality") or "unknown"),
                }
            )
    return claims


def _claim_semantic_changes(
    previous: dict[str, list[dict[str, str]]],
    current: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kind_changes: list[dict[str, Any]] = []
    modality_changes: list[dict[str, Any]] = []
    for key in sorted(previous.keys() & current.keys()):
        previous_unmatched, current_unmatched = _unmatched_claim_semantics(
            previous[key],
            current[key],
        )
        for pair_index, (previous_claim, current_claim) in enumerate(
            _minimum_cost_claim_pairs(previous_unmatched, current_unmatched)
        ):
            pair_key = f"{key}|{pair_index}"
            if previous_claim["kind"] != current_claim["kind"]:
                kind_changes.append(
                    _claim_change(pair_key, previous_claim, current_claim, "kind")
                )
            if previous_claim["modality"] != current_claim["modality"]:
                modality_changes.append(
                    _claim_change(
                        pair_key,
                        previous_claim,
                        current_claim,
                        "modality",
                    )
                )
    return kind_changes, modality_changes


def _unmatched_claim_semantics(
    previous: list[dict[str, str]],
    current: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    current_unmatched = list(current)
    previous_unmatched: list[dict[str, str]] = []
    for previous_claim in previous:
        exact_index = next(
            (
                index
                for index, current_claim in enumerate(current_unmatched)
                if (
                    previous_claim["kind"],
                    previous_claim["modality"],
                )
                == (
                    current_claim["kind"],
                    current_claim["modality"],
                )
            ),
            None,
        )
        if exact_index is None:
            previous_unmatched.append(previous_claim)
        else:
            current_unmatched.pop(exact_index)
    previous_unmatched.sort(key=lambda item: (item["kind"], item["modality"]))
    current_unmatched.sort(key=lambda item: (item["kind"], item["modality"]))
    return previous_unmatched, current_unmatched


def _minimum_cost_claim_pairs(
    previous: list[dict[str, str]],
    current: list[dict[str, str]],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    if not previous or not current:
        return []
    if len(previous) <= len(current):
        assignment = _minimum_cost_assignment(previous, current)
        return [
            (previous[left_index], current[right_index])
            for left_index, right_index in enumerate(assignment)
        ]
    assignment = _minimum_cost_assignment(current, previous)
    return [
        (previous[right_index], current[left_index])
        for left_index, right_index in enumerate(assignment)
    ]


def _minimum_cost_assignment(
    smaller: list[dict[str, str]],
    larger: list[dict[str, str]],
) -> tuple[int, ...]:
    row_count = len(smaller)
    column_count = len(larger)
    if not row_count:
        return ()

    # Rectangular Hungarian assignment. Inputs are already sorted, and scanning
    # columns from left to right gives deterministic choices when semantic costs tie.
    row_potential = [0] * (row_count + 1)
    column_potential = [0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)

    for row in range(1, row_count + 1):
        matched_row[0] = row
        current_column = 0
        minimum_slack = [float("inf")] * (column_count + 1)
        visited = [False] * (column_count + 1)
        while True:
            visited[current_column] = True
            current_row = matched_row[current_column]
            delta = float("inf")
            next_column = 0
            for column in range(1, column_count + 1):
                if visited[column]:
                    continue
                reduced_cost = (
                    _claim_pair_cost(
                        smaller[current_row - 1],
                        larger[column - 1],
                    )
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced_cost < minimum_slack[column]:
                    minimum_slack[column] = reduced_cost
                    predecessor[column] = current_column
                if minimum_slack[column] < delta:
                    delta = minimum_slack[column]
                    next_column = column
            for column in range(column_count + 1):
                if visited[column]:
                    row_potential[matched_row[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum_slack[column] -= delta
            current_column = next_column
            if matched_row[current_column] == 0:
                break
        while current_column:
            prior_column = predecessor[current_column]
            matched_row[current_column] = matched_row[prior_column]
            current_column = prior_column

    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column]:
            assignment[matched_row[column] - 1] = column - 1
    assert all(column >= 0 for column in assignment)
    return tuple(assignment)


def _claim_pair_cost(
    left_claim: dict[str, str],
    right_claim: dict[str, str],
) -> int:
    return int(left_claim["kind"] != right_claim["kind"]) + int(
        left_claim["modality"] != right_claim["modality"]
    )


def _claim_change(
    key: str,
    previous: dict[str, str],
    current: dict[str, str],
    field: str,
) -> dict[str, Any]:
    return {
        "claim": _bounded_semantic_text(current.get("text") or previous["text"]),
        "previous": previous[field],
        "current": current[field],
        "identity_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
    }


def _role_quality_flags(role: dict[str, Any]) -> set[str]:
    flags: set[str] = set()
    for evidence in role.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        flags.update(str(value) for value in evidence.get("quality_flags") or [])
        for claim in evidence.get("claims") or []:
            if isinstance(claim, dict):
                flags.update(str(value) for value in claim.get("quality_flags") or [])
    return flags


def create_role_grammar_sqlite_backup(database_url: str) -> Path:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise ValueError("role-grammar backfill backups require a file-backed SQLite database")
    if url.database == ":memory:":
        raise ValueError("cannot back up an in-memory SQLite database")

    source_path = Path(url.database).resolve()
    if not source_path.is_file():
        raise ValueError("SQLite database file is unavailable")
    backup_dir = source_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = source_path.suffix or ".db"
    backup_path = backup_dir / f"{source_path.stem}-role-grammar-{timestamp}{suffix}"

    try:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True) as source,
            sqlite3.connect(backup_path) as destination,
        ):
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("SQLite backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path
