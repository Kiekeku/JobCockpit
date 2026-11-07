import json
import sqlite3
from dataclasses import replace

import pytest

from jobcockpit.ingestion.saved_parser_validation import (
    PostingInput,
    capture_postings,
    coverage_tokens,
    input_key,
    run_validation,
    select_representatives,
    structure,
)
from jobcockpit.storage.artifacts import ArtifactStorageService


def posting(id="one", text="Requirements: Currently enrolled. Duration: 8 months."):
    return PostingInput(id, "artifact", "snapshot", "bytes", "paste", None, "Intern", text, {})


def test_structure_retains_grammar_risks_and_ignores_company_identity():
    base = posting(text="Company: Example A\nRequirements: Currently enrolled.\nDuration: 8 months.")
    other = replace(base, text=base.text.replace("Example A", "Example B"))
    assert structure(base) == structure(other)
    assert structure(base) != structure(replace(base, text=base.text + " Or recently graduated."))
    assert structure(base) != structure(replace(base, text=base.text + " (Preference for co-op.)"))
    assert structure(base) != structure(replace(base, source_url="https://jobs.example.test/role"))


def test_selection_is_deterministic_covers_every_shape_and_preserves_pins():
    rows = [posting(str(i)) for i in range(40)]
    rows.append(posting("rare", "Requirements: Must not graduate before the internship."))
    selected = select_representatives(rows, pinned={"37"}, seed="a", holdout_fraction=0.1)
    assert selected == select_representatives(list(reversed(rows)), pinned={"37"}, seed="a", holdout_fraction=0.1)
    assert {"37", "rare"} <= set(selected)
    assert set().union(*(coverage_tokens(r) for r in rows)) == set().union(
        *(coverage_tokens(r) for r in rows if r.id in selected)
    )
    assert any("holdout" in reasons for reasons in selected.values())
    assert len(selected) < len(rows)


def test_input_guards_include_metadata_and_source_bytes():
    row = posting()
    for changed in (replace(row, title_hint="Other"), replace(row, content_hash="changed"),
                    replace(row, source_type="url"), replace(row, snapshot_hash="changed")):
        assert input_key(changed) != input_key(row)


def fake_parse(row):
    return {"output": {"employment_timing": {"start_date": None}}, "signals": [], "error": None}


def test_incremental_reuses_only_exact_inputs_and_parser_version():
    rows = [posting("a"), posting("b")]
    baseline = run_validation(rows, [], {"parser.py": "v1"}, mode="full", parse=fake_parse)
    run = run_validation(rows, [], {"parser.py": "v1"}, mode="incremental", baseline=baseline, parse=fake_parse)
    assert run["summary"]["parsed_count"] == 0
    assert run["summary"]["reused_count"] == 2
    changed = [rows[0], replace(rows[1], title_hint="New title")]
    run = run_validation(changed, [], {"parser.py": "v1"}, mode="incremental", baseline=baseline, parse=fake_parse)
    assert run["summary"]["parsed_count"] == 1
    run = run_validation(rows, [], {"parser.py": "v2"}, mode="incremental", baseline=baseline, parse=fake_parse)
    assert run["summary"]["parsed_count"] == 2


def test_shared_changes_force_full_scan_and_changes_are_not_auto_approved():
    rows = [posting(str(i)) for i in range(10)]
    baseline = run_validation(rows, [], {"parser.py": "v1"}, mode="full", parse=fake_parse)
    def changed_parse(row):
        return {"output": {"employment_timing": {"start_date": "2027-01-01"}}, "signals": [], "error": None}
    run = run_validation(rows, [], {"parser.py": "v2"}, mode="representative", baseline=baseline, parse=changed_parse)
    assert run["summary"]["effective_mode"] == "full"
    assert run["summary"]["changed_count"] == 10
    assert run["summary"]["attention_count"] == 10
    again = run_validation(rows, [], {"parser.py": "v2"}, mode="incremental", baseline=run, parse=changed_parse)
    assert again["summary"]["attention_count"] == 10


def test_failures_and_unsampled_rows_are_accounted_for():
    rows = [posting(str(i)) for i in range(20)]
    run = run_validation(rows, [{"id": "bad", "error": "unreadable"}], {"parser.py": "v1"},
                         mode="representative", parse=fake_parse)
    s = run["summary"]
    assert s["active_count"] == s["parsed_count"] + s["skipped_count"] + s["artifact_failure_count"]
    assert s["skipped_count"] > 0
    assert not s["complete_current_coverage"]
    assert s["attention_count"] >= 1


def test_capture_verifies_artifact_integrity_and_leaves_database_unchanged(tmp_path):
    storage = ArtifactStorageService(tmp_path / "artifacts")
    artifact = storage.write_bytes(artifact_type="job_snapshot", content=b"Intern", job_id="one",
                                   original_file_name="posting.txt", content_type="text/plain", privacy_class="private")
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE active_job_postings (id, raw_content_artifact_id, posting_snapshot_hash, source_type, source_url, role, employment_timing, eligibility, compensation, application, location, employer, data_quality)")
        con.execute("INSERT INTO active_job_postings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("one", artifact.artifact_id, "snapshot", "paste", None, json.dumps({"job_title": "Intern"}), *(["{}"] * 7)))
        con.execute("CREATE TABLE stored_artifacts (artifact_id, job_id, artifact_type, storage_uri, sha256, size_bytes, deleted_at)")
        con.execute("INSERT INTO stored_artifacts VALUES (?,?,?,?,?,?,?)",
                    (artifact.artifact_id, "one", "job_snapshot", artifact.storage_uri, artifact.sha256, artifact.size_bytes, None))
    before = db.read_bytes()
    rows, failures = capture_postings(db, storage.root)
    assert len(rows) == 1 and not failures
    assert db.read_bytes() == before
    storage._path_for_uri(artifact.storage_uri).write_bytes(b"broken")
    rows, failures = capture_postings(db, storage.root)
    assert not rows and failures[0]["id"] == "one"


def test_invalid_baseline_is_rejected():
    with pytest.raises(ValueError, match="baseline"):
        run_validation([posting()], [], {}, mode="incremental", baseline={"schema_version": 999})


def test_serial_and_parallel_reports_have_identical_order_and_results():
    rows = [posting("b"), posting("a")]
    serial = run_validation(rows, [], {}, mode="full", parse=fake_parse)
    parallel = run_validation(rows, [], {}, mode="full", workers=2, parse=fake_parse)
    assert serial["records"] == parallel["records"]
    assert serial["summary"] == parallel["summary"]
    assert [r["id"] for r in parallel["records"]] == ["b", "a"]


def test_adjudication_requires_exact_output_and_preserves_failure_pin():
    row = posting()
    baseline = run_validation([row], [], {}, mode="full", parse=fake_parse)
    baseline["records"][0]["attention"] = ["parser_output_changed"]
    baseline["records"][0]["historical_failure"] = True
    review = {"id": row.id, "review_key": baseline["records"][0]["review_key"], "note": "Source checked"}
    run = run_validation([row], [], {}, mode="representative", baseline=baseline,
                         reviewed=[review], parse=fake_parse)
    assert not run["records"][0]["attention"]
    assert run["records"][0]["historical_failure"]
    assert "historical_failure" in run["records"][0]["selection_reasons"]
    changed = replace(row, persisted={"manually_changed": True})
    run = run_validation([changed], [], {}, mode="incremental", baseline=baseline,
                         reviewed=[review], parse=fake_parse)
    assert run["records"][0]["attention"]


def test_leaf_change_samples_but_never_relabels_old_outputs_as_current():
    rows = [posting(str(i)) for i in range(20)]
    baseline = run_validation(rows, [], {"eligibility_parser.py": "v1"}, mode="full", parse=fake_parse)
    run = run_validation(rows, [], {"eligibility_parser.py": "v2"}, mode="representative",
                         baseline=baseline, parse=fake_parse)
    assert run["summary"]["effective_mode"] == "representative"
    assert run["summary"]["skipped_count"] > 0
    assert not run["summary"]["complete_current_coverage"]
    assert all(r["parser_fingerprint"] is None for r in run["records"] if r["result"] is None)


def test_parse_errors_are_retried_and_pin_the_failed_record():
    def failed_parse(row):
        return {"output": None, "signals": [], "error": "broken parser"}
    baseline = run_validation([posting()], [], {}, mode="full", parse=failed_parse)
    assert baseline["summary"]["parse_failure_count"] == 1
    run = run_validation([posting()], [], {}, mode="incremental", baseline=baseline, parse=fake_parse)
    assert run["summary"]["parsed_count"] == 1
    assert not run["summary"]["parse_failure_count"]
    assert run["records"][0]["historical_failure"]


def test_cli_keeps_reports_private_immutable_and_loads_history_pins(tmp_path, monkeypatch):
    from scripts import validate_saved_parser as cli

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "capture_postings", lambda *_: ([posting()], []))
    monkeypatch.setattr(cli, "parser_manifest", lambda *_: {})
    monkeypatch.setattr(cli, "run_validation", lambda *a, **kw: run_validation(*a, **kw, parse=fake_parse))
    pins = tmp_path / "var/parser-validation/pins.json"
    pins.parent.mkdir(parents=True)
    pins.write_text(json.dumps(["one", "archived"]), encoding="utf-8")
    output = tmp_path / "var/report.json"
    args = ["--mode", "full", "--workers", "1", "--output", str(output)]
    assert cli.main(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["active_history_pin_count"] == 1
    assert report["inactive_history_pins"] == ["archived"]
    assert report["records"][0]["historical_failure"]
    with pytest.raises(SystemExit) as existing:
        cli.main(args)
    assert existing.value.code == 2
    with pytest.raises(ValueError, match="under var"):
        cli.private_output(tmp_path / "public.json")
