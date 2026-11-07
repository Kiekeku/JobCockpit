# Contributing

JobCockpit is a single-user workstation for local use. Start with the setup in
[README.md](README.md) and the repository conventions in [AGENTS.md](AGENTS.md).
Use Python 3.12+, uv, Node.js 22+, and pnpm 9. Install with `uv sync --locked`
and `pnpm install --frozen-lockfile`, then copy `.env.example` to `.env`.

## Changes and verification

Create a branch and keep each pull request focused. Describe the problem, the
resulting behavior, and the checks you actually ran. Add focused regression
coverage when changing a contract. Run the full suite for integration changes:

```bash
uv run playwright install chromium
pnpm test
pnpm build
uv run python scripts/privacy_scan.py .
uv run python scripts/privacy_scan.py . --git-ref HEAD
```

The first privacy check covers local source edits; the second checks committed
files, including files that would otherwise be ignored. Run it after committing.
For touched Python files, run `uv run ruff check <paths>`. Schema changes need an
Alembic migration and a matching model change. Use an empty development database
to verify migrations; never use someone's private runtime state as test data.

For parser changes, follow the source-grounding and validation requirements in
[docs/parser-validation.md](docs/parser-validation.md). Public clones include the
minimized golden cases, not the maintainer's private saved-posting database.
Use your own permitted source evidence and record all matching contexts before
changing parsing behavior. Do not upload whole private postings to a PR.

## Privacy and review boundaries

Use synthetic candidate names and `example.invalid` or `example.test` addresses.
Keep resumes, transcripts, application answers, email, screenshots, logs, browser
profiles, OAuth files, API keys, and databases under ignored local storage.
Review `git diff --cached` before every commit; `.gitignore` does not protect
files already tracked in Git. Share only minimized, redacted reproduction cases.

Preserve Gmail read-only access, reviewed extension insertion, document approval
checks, URL fetch restrictions, and artifact ownership checks. The extension
must never click submit, continue, apply, or signature controls.

Report vulnerabilities according to [SECURITY.md](SECURITY.md). For ordinary bugs,
include your OS, tool versions, expected and actual behavior, and sanitized steps
to reproduce. Feature requests should describe the workflow and its limits.

## License

Contributions are provided under the repository's [MIT license](LICENSE).
Only contribute material you have permission to share. Preserve third-party
copyright and license notices when adding or distributing third-party code.
