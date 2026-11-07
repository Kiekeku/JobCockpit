# Parser validation workflow

The golden corpus remains the correctness gate: it contains approved expected fields.
Saved-posting validation is a separate, read-only check for regressions and source gaps.
A stable parser result is not proof that it is correct. Provider/manual values may also
differ from the deterministic parser; saved-value differences never authorize repairs.

## Dataset map: what each corpus is for

Paths below are relative to the repository root. Counts in historical reports are
snapshots, not selection limits; use the current runner's counts as the corpus grows.

| Dataset or artifact | Location | Purpose | What it does not establish |
| --- | --- | --- | --- |
| Golden parser corpus | `data/fixtures/parser/golden_cases.json` | Committed, privacy-safe minimized cases with approved field-level expectations. Reproduce a demonstrated source error and prevent its return. | Whole-posting correctness beyond the asserted fields, or accuracy on unseen postings. |
| Saved real posting corpus | `active_job_postings` in `var/jobcockpit.db`, joined to `stored_artifacts`; snapshot bytes through `ArtifactStorageService` at the validator's configured `--artifact-root` | Source evidence for grammar changes and realistic whole-posting regression checks. Use the referenced artifact, not just the displayed summary. | Ground truth: persisted fields can contain older parser output, provider enrichment, or manual edits. The active view also excludes inactive records. |
| Matching source contexts | A task-specific report under `var/reports/` | Every real occurrence relevant to a proposed behavior change, including legitimate counterexamples. Record match counts and error counts separately. | A representative sample is not a substitute for inspecting these matches. |
| Representative selection | Computed from the current saved corpus by `--mode representative`; membership/reasons in the report | Faster development checks covering structural features, historical failures, and a holdout. | A second golden corpus, a fixed list of companies, or a fresh parse of every saved posting. |
| Incremental selection and reused outputs | `--mode incremental` plus a compatible prior saved-validator report | Parse new/changed inputs and reuse exact guarded results when the parser is unchanged; verify every active artifact. | Permission to reuse results across parser changes or skip artifact failures. |
| Saved-parser baseline/report | A unique JSON file under `var/reports/` from `validate_saved_parser.py` | Record inputs' guards, parser fingerprint, fresh/reused/skipped coverage, outputs, differences, and attention. Compare parser versions on identical inputs. | Approved expected answers. A report may contain failures, skipped rows, or unresolved observations. |
| Golden output snapshot | `parser_corpus_diff.py --write-current var/...json` | Inspect selected golden outputs before/after a change. | A baseline accepted by `validate_saved_parser.py`; the formats and purposes differ. |
| Historical failure pins | `var/parser-validation/pins.json`, golden source hashes, and historical flags in prior reports | Keep known failure sources represented even when they resemble other postings. | Expected outputs or proof that a past failure remains fixed; golden assertions supply that check. |
| Adjudications | Private JSON passed with `--reviewed` | Reuse a source-reviewed explanation only for the exact guarded observation. | Correcting database values, authorizing reconciliation, or suppressing read/parse failures. |
| Other test fixtures | `tests/` and `data/fixtures/email-alerts/synthetic-alert.html` | Verify ingestion, provider contracts, storage, API, UI, and validation infrastructure in focused tests and `pnpm test`. | Real-source grounding for a parser defect. Synthetic infrastructure probes are not evidence of a real posting error. |

The seeded holdout is drawn from the existing development corpus. It helps detect
selection gaps, but is not an independently labelled evaluation set. No independent
unseen-template accuracy corpus is established by this workflow.

## Decision table: when to test what

| Change or task | During investigation/development | Before completing the task |
| --- | --- | --- |
| A reported extraction miss or a leaf extractor change | Search **all matching real source contexts**; add a minimized observed-posting golden case; demonstrate its failing field with `--case CASE_ID --workers 1`; run focused contract tests. | All golden cases, representative saved-posting comparison against the pre-change baseline, and inspection of changed outputs/source signals. Run the full application suite for parser integration regressions. |
| Shared grammar, source cleaning, section boundaries, parser orchestration, model/ontology changes | Same source-grounding and focused regression steps; retain a pre-change full baseline. | All golden cases, focused contracts, full application suite, and a **full saved-posting scan**. Inspect every difference and failure. |
| New postings with unchanged parser code | Incremental scan against a compatible prior report; inspect new profiles and flagged evidence. | Account for every active row and triage new observations. New data alone does not require rerunning unrelated application tests. |
| No compatible baseline, periodic corpus audit, or substantial change in source mix | Full scan to establish current coverage. | Inspect saved-value differences and source signals; retain the report with its review status. Do not label the initial output correct merely because there is nothing older to compare. |
| Approved saved-record reconciliation | Full read-only inspection and a separate guarded repair plan/backup. | **Explicit `--mode full` after the repair**, even if the parser fingerprint is unchanged. Require zero fresh mismatches and zero artifact/parse failures for a clean full reconciliation claim; disclose remaining drift instead of treating exit 0 as proof. |
| UI-only term formatting/filtering | Focused frontend helper/dashboard tests using observed saved-field projections; inspect relevant real examples. | Appropriate frontend build/type checks. Parser golden or full database reruns are unnecessary unless extraction or persisted data changes too. |
| Validator selection, cache, fingerprint, or reporting changes | Focused infrastructure tests for coverage, invalidation, pins, ordering, failures, and report handling. | Full versus representative/incremental saved-corpus checks, historical-failure coverage, and the full suite. Demonstrate savings and disclose coverage limits. |
| Documentation-only changes | Verify documented paths, options, and claims against the implementation. | Check links, diff formatting, and the privacy scan, which also checks documentation examples; parsing the entire database adds no useful validation. |
| Test/fixture consolidation with unchanged parser behavior | Preserve the original assertions and compare input options, including title hints, provider behavior, and URL/no-URL handling. Move approved assertions into existing golden cases before removing repeated unit cases. | All golden cases and affected contract/runner tests, Ruff for touched Python, and privacy checks. A saved-posting scan is unnecessary when parser and validator behavior are unchanged. |

Use `pnpm test` for the full suite; its golden chunks already execute all golden cases.
That successful run satisfies the golden gate; do not also run the complete standalone
golden runner against unchanged code and fixtures. The standalone runner remains useful
for field-level failure output, focused before/after snapshots, and tasks that require
the golden gate without the full application suite. Run full jobs sequentially to avoid
oversubscribing Windows.
For touched Python files run Ruff; follow the privacy/build guidance in `AGENTS.md`
where applicable. Source adapter/provider changes also need their own contract tests:
the saved-posting runner does not exercise live fetching or LLM enrichment.

Automatic escalation needs a prior `--baseline` to detect shared-parser changes.
Without one, choose `--mode full` explicitly for broad changes. A representative run
can reuse unchanged-parser outputs for unselected rows; report that reuse separately
from the rows freshly parsed in this run.

## Baseline lifecycle and handoff

1. Before editing parser code, locate a prior full report for the intended database,
   or create one. Preserve it through the change; report files are immutable outputs.
2. Pass that report as `--baseline` for development comparisons. Do not repeatedly
   replace it with a sampled report: unselected old-version outputs are not current
   coverage, and a sample is not a full pre-change reference.
3. Inspect `changed_fields`, saved-value differences, read/parse failures, and source
   signals. Record source-grounded adjudications with exact `review_key` values.
   Do not generate approvals automatically from the fresh parser's own output.
4. Retain the accepted run, its unresolved issues, pins, and review-file path in a
   private handoff report under `var/`. Pass the review file again on subsequent runs.
   Never silently bless outstanding differences by changing the baseline.
5. For future agents, identify the baseline path, mode, parser fingerprint, fresh/
   reused/skipped/failed counts, golden result, and unresolved observations. Report
   structural coverage separately from accuracy and stored-value reconciliation.

Prefer the supported command below over one-off scripts in `var/maintenance/`.
Those scripts and older reports may contain task-specific field scopes, fixed IDs,
or incompatible schemas. Read them as historical evidence, not reusable approval
or a replacement for the current validator.

## Commands

Run focused contract tests during development:

```powershell
pnpm test:fast --serial tests/test_employment_timing_matrix.py
pnpm test:fast --serial tests/test_job_parser_codex.py::test_parse_job_text_resolves_exact_term_schedule_independently_of_field_order
```

Selectors accept repository test files and pytest test IDs. The `--fast`/`--slow`
filters still apply; with no selectors the existing suite scope is unchanged.
Choose the decision table's full checks before completing integration work.

Run all golden cases before accepting any parser change. A successful `pnpm test`
includes this gate; when the full application suite is unnecessary, use:

```powershell
pnpm --silent python scripts/parser_corpus_diff.py
```

Bootstrap a full saved-posting baseline, and retain the report:

```powershell
pnpm --silent python scripts/validate_saved_parser.py --mode full --output var/reports/parser-full.json
```

Use representatives during development, comparing against that baseline:

```powershell
pnpm --silent python scripts/validate_saved_parser.py --mode representative --baseline var/reports/parser-full.json --output var/reports/parser-sample.json
```

Check new/changed inputs with an unchanged parser:

```powershell
pnpm --silent python scripts/validate_saved_parser.py --mode incremental --baseline var/reports/parser-full.json --output var/reports/parser-incremental.json
```

Outputs must have unique names under `var/`; the command never replaces prior evidence
or writes to SQLite. It does not invoke an LLM, fetch postings, or reconcile records.
Use `--workers 1` for debugging, or up to eight workers for validation.

## Selection and coverage

Each posting gets a descriptive profile of source host/type, flattened/line/HTML
layout, bullet presence, recognized heading order, and grammar-risk features. Features
include alternatives, conjunctions, negation, preferences, parentheses, temporal conditions,
dates, durations, academic terms, enrollment, compensation, documents, French, and damaged
text. Employer identity is not the grouping key. These are heuristic profiles, not a
complete inventory of English grammar or guaranteed template equivalence.

Selection uses deterministic greedy coverage rather than one job per company or exact
profile. It covers every observed source-type/ATS-family/layout/bullet combination,
adjacent heading transition within each layout, complete risk-feature combination,
source-type/risk pair, and layout/text-length bucket. Tenant-specific ATS hosts share
their platform family; unknown websites remain in an explicit other family. Different
companies can cover the same features. This reduces redundancy without claiming all
interactions among those features have been tested.

Retain every known failure pin, then add postings that cover the most uncovered features
(stable ID breaks ties), followed by a seeded 5% holdout of the remaining postings.
`--seed` rotates the holdout and `--holdout-fraction` adjusts it.
Golden fixtures with a matching `grounding_source_sha256` pin their original saved sources;
`--pin` adds explicit job IDs. Prior output changes and parse failures remain historical
pins even after adjudication. All new or changed inputs are selected when a baseline exists.
The optional private `var/parser-validation/pins.json` list preserves known historical job
IDs across independent runs, including failures predating this validator. Override it with
`--pins-file`. Archived IDs remain in the list but are reported as inactive, not silently lost.

Incremental reuse requires identical source bytes, artifact ID, snapshot hash, source
metadata, title hint, and parser fingerprint. Fingerprints include backend Python sources,
ontology YAML, dependency lock, project configuration, and Python runtime. The command still
reads and verifies every active artifact and compares reused output against current saved
values. It saves parsing work, not artifact integrity checks.

Representative runs automatically expand to full scans when baseline fingerprints show
changes outside the explicitly listed leaf extractors. Shared grammar, models, source
cleaning, and section boundaries therefore force full scans. Leaf changes still require
inspection of *all matching real source contexts* when diagnosing/fixing a behavior; the
representative runner does not supersede the corpus-grounding rule.

Use full scans periodically, before accepting broad changes, and after every saved-record
reconciliation. Sampling never replaces the post-repair full validation requirement.

## Reading a report

- `parsed_count`, `reused_count`, `skipped_count`, artifact failures, and parse failures
  account for every active row. Reuse is reported separately from fresh execution.
- `complete_current_coverage` requires a current result for every row without read/parse
  failures; it does not mean that output has been adjudicated or is accurate.
- `changed_fields` compares parser outputs only when the complete input guard matches.
- `persisted_different_fields` separately identifies differences from saved values.
- `novel_structure` identifies profiles absent from the prior report.
- `source_signal` flags explicit start/beginning-date phrasing without a parsed start. It is deliberately
  a triage hint: company history, graduation dates, and page chrome can be legitimate matches.
- `attention` carries unresolved parser-output changes forward; changing the baseline
  cannot silently approve them. Exit 1 means attention is needed, 2 means command error,
  and 0 means no flagged attention within the reported coverage. Inspect saved-value
  differences separately, especially on the initial full baseline.

After inspecting the actual source, a private `--reviewed` JSON list can record explicit
adjudications. Each entry needs `id`, the report's exact `review_key`, and a nonempty `note`.
The key binds source inputs, output, saved values, and signals; a stale review cannot clear
a changed result. Pass the review file on subsequent runs to retain the adjudication.
Parse/artifact errors cannot be adjudicated away. This acknowledges a
validation observation only, never grants permission to update saved records.

## Initial saved-corpus measurement

On the September 4, 2026 snapshot of 1,875 active postings, the final representative
selection parsed 716 postings (61.8% fewer), covering all 1,126 selector features,
111 sources linked to golden fixtures, and all eight retained historical repair IDs.
The remaining 1,159 outputs were reused from an identical-parser full baseline.
The full run took 68 seconds with eight workers; the final representative run took
48 seconds with four workers. An unchanged-input incremental run took seven seconds
and reused all outputs after verifying all artifacts. These are local measurements,
not a performance guarantee or an accuracy estimate.

All 1,875 full-run outputs matched the preceding saved audit under the artifact/snapshot
guards. There were zero artifact or parse failures. Four source signals were adjudicated
as metadata dates or unresolved alternative dates in a private review file. The report
also exposes 1,558 rows differing from stored values; these differences are separate from
parser-to-parser regressions and have not been bulk-approved or reconciled by this tool.

## Parallel execution and guarded reconciliation

Use the parallel entry points for full validation; keep focused debugging
serial so failures remain easy to reproduce.

- `pnpm --silent python scripts/parser_corpus_diff.py` validates the golden corpus in
  a bounded process pool. The default is `auto`, capped at 8 workers.
- Use `--workers N` to set an explicit corpus worker count and `--workers 1`
  when debugging a failure. Parallel and serial runs must produce the same
  case order, projected outputs, and field differences.
- `pnpm test` is the preferred full-suite command. `scripts/test.mjs` runs
  pytest-xdist with at most 16 workers, and the golden corpus test is divided
  into 16 deterministic chunks so no single pytest worker owns the full
  corpus. A plain `uv run pytest` invocation is serial unless `-n` is supplied.
- Do not combine high pytest-xdist concurrency with high per-test process
  concurrency. The golden pytest chunks run serially inside each xdist worker;
  the standalone corpus command owns its own process pool.

Full validation against saved active job postings is a read-only scan followed
by an optional, separately guarded reconciliation. Local reports and source
contexts belong under `var/` and must not be committed.

- Read the active view, posting metadata, persisted parser-owned fields, and
  artifact bytes once in the parent process. Do not pass SQLAlchemy sessions,
  ORM objects, database connections, or `ArtifactStorageService` instances to
  workers.
- Close the read session before starting a bounded process pool (normally no
  more than 8 workers on Windows). Pass only serializable immutable inputs such
  as job ID, artifact ID, snapshot hash, source metadata, title hint, and raw
  text. Keep worker functions at module scope and process-pool startup behind
  an `if __name__ == "__main__"` guard for Windows spawn compatibility.
- Workers perform parsing only and return the job ID, input guards, fresh
  parser projection, signal summary, or a bounded error. Preserve the active
  job ordering when merging results; report every artifact/read/parse failure
  rather than silently dropping a row.
- Confirm that the active-view count is fully accounted for and inspect every
  fresh-versus-persisted mismatch and every strong unparsed source signal.
  Zero parser differences alone is not sufficient evidence when source reads
  or parses failed.
- Never write to SQLite from parser workers. If reconciliation is approved,
  re-query in one writer, verify the exact target ID set plus artifact IDs,
  snapshot hashes, and old persisted values, write a private backup under
  `var/`, then update in one guarded transaction with audit-log entries.
- After reconciliation, rerun the complete parallel read-only validation and
  require zero fresh mismatches, zero artifact failures, and zero parse
  failures. Adjudicate remaining source-signal rows against their real posting
  contexts instead of treating the heuristic signal detector as truth.
