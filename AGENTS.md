# AGENTS.md

Guidance for future coding agents working in this repository.

## Project Shape

JobCockpit is a single-user, local-first monorepo:

- Backend: FastAPI, Pydantic v2, SQLAlchemy, Alembic, SQLite MVP.
- Frontend: Next.js, TypeScript, Tailwind-style CSS, TanStack Query.
- Browser helper: Manifest V3 side panel with grounded answer drafts and reviewed native-field insertion on Workday and localhost mock flows.
- Runtime state belongs under `var/` and must stay out of git.

Use `uv` for Python workflows and `pnpm` for Node workflows.

Write source files as UTF-8. Use explicit `encoding="utf-8"` in Python file
operations and `-Encoding utf8` in PowerShell; Windows PowerShell's default
encoding can corrupt non-ASCII source text. Prefer patch tools for source edits.

## Persistence And Storage

- Keep SQLite as the MVP database: `sqlite:///./var/jobcockpit.db`.
- Keep SQLAlchemy models and Alembic migrations in sync.
- Use portable SQLAlchemy types and SQLite-friendly migrations.
- Store file bytes only through `ArtifactStorageService`.
- Database rows should store metadata, ownership, SHA-256, content type, size, privacy class, and storage URI.
- Do not commit generated documents, private artifacts, Gmail tokens, OAuth secrets, browser profiles, screenshots, or local databases.

## Safety Rules

- The app has no user authentication in the MVP; assume local-only, single-user operation.
- Gmail integration must remain read-only. Do not label, archive, delete, star, send, draft, or mark messages read/unread.
- Extension behavior must stay review-first. It may fill safe fields only after preview/review.
- The extension must not click submit, continue, apply, or e-signature controls.
- Only approved package documents may be exposed to extension download paths.
- External insertion is limited to the explicit Workday native-control boundary. Keep custom/unknown controls manual; expand adapters only with browser evidence. The local mock and submit guard have runtime regression coverage.

## Development Workflow

Before changing behavior, inspect coverage of the affected contract. Add or update
focused tests when existing coverage does not establish the intended behavior.
Preserve the regression-first parser requirements below. For documentation-only
or low-impact presentation changes, use relevant existing checks rather than
adding tests that merely mirror the edit.

Choose checks for the affected scope; run the full suite for integration changes.
Use the [parser validation decision table](docs/parser-validation.md#decision-table-when-to-test-what)
for parser work. Do not repeat
passing checks unless further changes or unresolved concerns justify it.

- Use `pnpm test:fast --serial tests/test_file.py` or a `tests/test_file.py::test_id`
  selector for focused checks. With no selectors, the test commands retain their
  existing full/fast/slow suite scope.
- A successful `pnpm test` already runs every golden parser case. Count it as the
  golden gate; use the standalone runner for focused before/after diagnostics or
  when the full suite is unnecessary. Do not run both complete golden checks for
  the same unchanged code and fixtures.
- UI-only and documentation-only changes use the decision table's scoped checks.
  Parser changes still require every golden case; saved-posting checks use the
  appropriate representative, incremental, or full mode.

Available checks:

```bash
uv run python scripts/parser_corpus_diff.py
pnpm test
uv run python scripts/privacy_scan.py .
pnpm build
```

## Parser Evidence And Corpus Grounding

- In the internship/co-op domain, the canonical planning terms are Winter
  (January-April), Summer (May-August), and Fall (September-December). Some
  employers incorrectly call a May-August term "Spring"; retain that wording
  only as source evidence when useful, but normalize the user-facing term to
  Summer. Do not introduce Spring as a distinct canonical term.
- Every parser behavior change, golden fixture, and actionable parser review
  finding must be grounded in at least one concrete real job posting from the
  saved corpus or other user-supplied source evidence.
- Before changing or reviewing parser behavior, search the applicable real
  posting corpus and inspect every matching source context. Record the number
  of real matches and distinguish legitimate matches from demonstrated parser
  errors. A synthetic probe alone is not evidence of a repository defect.
- Purely hypothetical parser scenarios may be used for exploration, but they
  must not by themselves justify a parser change, a committed fixture, or a
  blocking/P1/P2 review finding. Treat unmatched future-hardening ideas as
  optional unless the user explicitly asks for them.
- For each source-grounded parser fix, first add a privacy-safe minimized case
  to `data/fixtures/parser/golden_cases.json` with `grounding` set to
  `observed_posting` and the approved expected fields. Synthetic fixture text
  must preserve the relevant structure of the observed posting.
- The corpus runner must show the field-level failure before the fix and zero
  differences afterward. Never copy a persisted job ID, real employer URL,
  personal data, or an entire private posting into the committed corpus.

## Parallel Parser Validation

For routine saved-posting checks, use `scripts/validate_saved_parser.py` and the
dataset map and decision table in `docs/parser-validation.md` before choosing
test data. Golden fixtures hold approved expected fields; saved artifacts supply
real-source evidence; representative/incremental runs select or reuse validation
work; reports, pins, and adjudications are private operational state. A saved-parser
report and a golden `--write-current` snapshot are different formats and must not
be substituted for each other. Keep all golden cases as the correctness
gate. When consolidating duplicate tests, retain every approved assertion and
meaningful input variant in the golden corpus or a focused contract test.
Use representative scans during development and incremental scans for new
inputs with an unchanged parser. Preserve historical failures and inspect all
source contexts matching a proposed fix; a representative sample does not waive
the grounding rule. Shared-parser changes and post-reconciliation verification
require full scans. Report fresh, reused, skipped, and failed rows separately;
never describe representative coverage as full validation or parser accuracy.

Before full validation, validator implementation changes, or reconciliation, read
[Parallel execution and guarded reconciliation](docs/parser-validation.md#parallel-execution-and-guarded-reconciliation).
Keep parsing workers read-only, account for every active row and failure, and use
a single guarded writer with backup and audit entries for approved reconciliation.
Post-reconciliation verification requires a full scan with zero fresh mismatches,
artifact failures, and parse failures; adjudicate remaining source signals.

For touched Python files, also run:

```bash
uv run ruff check <paths>
```

Use Alembic for schema changes:

```bash
uv run alembic upgrade head
```

## Review Priorities

When reviewing changes, prioritize:

- Extension approval/download bypasses.
- Gmail mailbox mutation risks.
- URL fetch policy bypasses for private networks or unsupported protocols.
- Artifact path traversal or arbitrary local file exposure.
- Generated document claims that are not evidence-backed.
- Drift between SQLAlchemy models, Alembic migrations, and tests.
