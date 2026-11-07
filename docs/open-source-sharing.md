# Sharing the source

This standalone repository begins with a reviewed source snapshot from
October 17, 2026. It includes the MIT license, public setup defaults, contributor
guidance, privacy checks, and synthetic fixtures.

For future snapshots, export only the reviewed file tree from the source repository.
Review personal names, employment narratives, workstation paths, configuration,
binary assets, and commit metadata manually as well as running the scanner.
Use a public contributor identity and never include local audit reports.

## Source snapshot and runtime data

Share files from the reviewed commit. Keep `.env`, `var/`, browser
profiles, private reports, generated documents, and local tool state out of any
archive. Git's archive command includes only committed files:

```bash
git archive --format=zip --output=var/jobcockpit-source.zip HEAD
```

Create `var/` first if necessary. This archive contains a source snapshot and
license, without Git history. Do not zip the entire working directory.

Before sharing a new commit, inspect it and run both privacy checks:

```bash
git diff --cached
uv run python scripts/privacy_scan.py .
uv run python scripts/privacy_scan.py . --git-ref HEAD
uv run python scripts/privacy_scan.py . --git-ref HEAD --history
```

The history check scans file contents reachable from the selected revision.
Historical findings can persist after the current file is cleaned. The scanner
reports categories and file paths without printing matched secret values. It
does not audit GitHub issues, pull requests, Actions logs, releases, other refs,
commit messages, author metadata, binary document contents, or external services.
Review those separately before a public release. Dependency licenses remain
with their respective projects; preserve their notices when distributing builds.

## Repository visibility and history

GitHub visibility applies to the repository, not to an individual branch.
Making this repository public also exposes `main`, other accessible branches,
and their reachable history. A separate root commit on the sharing branch does
not remove earlier versions, commit messages, or author/committer names and
email addresses from the other branches. The original history includes private
profile examples and must remain private unless separately cleaned.

For a public copy without the existing history, create a separate repository
from the reviewed source archive and choose the commit identity for that copy.
Keep the original repository private. This is a separate publishing action;
branch preparation does not perform it or rewrite `main`.

If a real credential is found in history, revoke or rotate it before publication.
Deleting the current file alone is insufficient. Review the affected history and
other GitHub surfaces before deciding how to publish a clean copy.
