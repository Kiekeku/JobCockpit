# JobCockpit

JobCockpit is a local-first workstation for collecting job postings, tracking applications, and drafting materials grounded in a candidate’s own profile. It also includes a Chrome side panel for reviewing answers and filling supported fields.

This repository hosts a public source snapshot. The software is credited to the JobCockpit contributors under the [MIT license](LICENSE).

## How it works

- The [FastAPI backend](apps/api) ingests postings and tracks jobs, applications, and document drafts in SQLite. Files are stored locally under `var/`.
- The [Next.js dashboard](apps/web) is the main workspace for reviewing and tracking applications.
- The [Chrome extension](apps/chrome-extension) helps draft answers and fill reviewed fields. The [local ATS mock](apps/ats-mock-site) provides a test site.

## Quick start

Install Python 3.12, uv, Node.js 22, and pnpm 9. From the repository root:

```bash
uv sync --locked
pnpm install --frozen-lockfile
cp .env.example .env
pnpm dev
```

On Windows PowerShell, use `Copy-Item .env.example .env` in place of `cp`. Open the dashboard at `http://localhost:3000`. The sample configuration uses synthetic Gmail alerts and disables optional AI services and outbound publication. Keep the app on loopback, since it is intended for a single user and has no user authentication.

## Tests and guides

Run `pnpm test` for the Python suite and `pnpm build` for the dashboard and extension builds. See the [user and developer guide](docs/user-guide.md#validation) for browser setup and additional checks.

- [User and developer guide](docs/user-guide.md)
- [Architecture](docs/architecture.md)
- [Chrome extension](docs/chrome-extension.md)
- [Privacy and security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
