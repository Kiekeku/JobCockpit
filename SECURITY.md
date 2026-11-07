# Security and private data

## Supported deployment

JobCockpit is an experimental, single-user local application. The MVP has no
user authentication. Bind the API, dashboard, mock site, and browser debugging
endpoints to loopback. Do not expose them through a public host, tunnel, shared
network listener, or untrusted reverse proxy. A public source repository does
not make the application suitable for a hosted multi-user service.

Security fixes target the latest maintained source revision. There is no
separate security support commitment for older snapshots or response-time SLA.

## Reporting a vulnerability

If GitHub displays **Report a vulnerability** on this repository's Security tab,
use that private channel. Include the affected revision, impact, and a minimal
synthetic reproduction. Do not attach real candidate documents or credentials.

If private reporting is unavailable, open an issue asking for a private contact
channel without exploit details or sensitive data. Wait for a private channel
before sending the report. Never post credentials or private artifacts publicly.

## Local responsibilities

- Keep `.env`, `var/`, OAuth files, browser profiles, and generated application
  documents private, including backups and diagnostic attachments.
- Leave the extension signing secret unset to generate an installation-specific
  secret. An explicit override must be private and randomly generated.
- Optional Gmail, Serper, RankAlpha, Codex, and browser integrations use your own
  accounts. Review the data they receive before enabling them; local storage
  does not imply that enabled external services process everything offline.
- Review every generated claim, answer, and attachment before using it. External
  sites can autosave fields; local undo cannot reverse a site's saved copy.

The automated privacy scan is a heuristic check, not a complete secret detector
or security audit. See [the sharing guide](docs/open-source-sharing.md) for the
difference between a clean file snapshot and publishing Git history.
