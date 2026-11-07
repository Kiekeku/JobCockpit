from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import select

from jobcockpit.core.config import Settings
from jobcockpit.db.models import JobAlertSource
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.utils.ids import new_id


DEFAULT_CLIENT_SECRET_PATH = Path("var/private/gmail/client_secret.json")
DEFAULT_TOKEN_PATH = Path("var/private/gmail/token.json")
DEFAULT_MAX_RESULTS = 25
DEFAULT_SCAN_MAX_RESULTS = 250


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        sender = normalize_sender(args.sender)
    except ValueError as error:
        parser.error(str(error))
    domain = sender.rsplit("@", 1)[1]
    gmail_query = args.gmail_query or f"from:{sender} newer_than:{args.newer_than}"
    display_name = args.display_name or f"{domain} jobs"
    sender_allowlist = args.sender_allowlist or [domain]

    env_file = Path(args.env_file)
    client_secret_path = Path(args.client_secret_path)
    token_path = Path(args.token_path)

    env_updates = {
        "JOBCOCKPIT_GMAIL_PROVIDER": "google",
        "JOBCOCKPIT_GMAIL_OAUTH_CLIENT_SECRET_PATH": client_secret_path.as_posix(),
        "JOBCOCKPIT_GMAIL_OAUTH_TOKEN_PATH": token_path.as_posix(),
        "JOBCOCKPIT_GMAIL_MAX_RESULTS": str(args.max_results),
        "JOBCOCKPIT_GMAIL_SCAN_MAX_RESULTS": str(args.scan_max_results),
    }
    update_env_file(env_file, env_updates)
    placeholder_created = ensure_client_secret_placeholder(client_secret_path)

    settings = Settings(_env_file=env_file)
    init_database(settings)
    source = upsert_gmail_source(
        settings=settings,
        display_name=display_name,
        gmail_query=gmail_query,
        sender_allowlist=sender_allowlist,
    )

    print(
        json.dumps(
            {
                "status": "configured",
                "env_file": str(env_file),
                "client_secret_path": str(client_secret_path),
                "client_secret_placeholder_created": placeholder_created,
                "token_path": str(token_path),
                "source": source,
                "next_step": "Replace the placeholder client secret JSON, then run `pnpm --silent cli gmail auth`.",
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_gmail_ingestion.py",
        description="Configure read-only Gmail ingestion and a sender-scoped job alert source.",
    )
    parser.add_argument("--sender", required=True, help="Sender email to ingest, e.g. jobs@example.invalid.")
    parser.add_argument("--display-name", help="Display name for the JobCockpit alert source.")
    parser.add_argument("--gmail-query", help="Full Gmail query. Defaults to from:SENDER newer_than:30d.")
    parser.add_argument("--newer-than", default="30d", help="Gmail newer_than value for the default query.")
    parser.add_argument(
        "--sender-allowlist",
        action="append",
        help="Allowed sender domain. Defaults to the sender email domain. Can be repeated.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to the local env file to update.")
    parser.add_argument(
        "--client-secret-path",
        default=str(DEFAULT_CLIENT_SECRET_PATH),
        help="Path where the Google OAuth desktop-app client JSON should live.",
    )
    parser.add_argument(
        "--token-path",
        default=str(DEFAULT_TOKEN_PATH),
        help="Path where the read-only OAuth token should be saved by `jobcockpit gmail auth`.",
    )
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--scan-max-results", type=int, default=DEFAULT_SCAN_MAX_RESULTS)
    return parser


def normalize_sender(sender: str) -> str:
    cleaned = sender.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
        raise ValueError("--sender must be an email address")
    return cleaned


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    next_lines: list[str] = []
    key_pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in lines:
        match = key_pattern.match(line)
        if not match:
            next_lines.append(line)
            continue
        key = match.group(1)
        if key in updates:
            next_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            next_lines.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if next_lines and next_lines[-1] != "":
            next_lines.append("")
        next_lines.append("# Real Gmail read-only ingestion.")
        for key in missing:
            next_lines.append(f"{key}={updates[key]}")

    path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def ensure_client_secret_placeholder(path: Path) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "REPLACE_WITH_GOOGLE_OAUTH_DESKTOP_CLIENT_ID.apps.googleusercontent.com",
                    "project_id": "REPLACE_WITH_GOOGLE_CLOUD_PROJECT_ID",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": "REPLACE_WITH_GOOGLE_OAUTH_CLIENT_SECRET",
                    "redirect_uris": ["http://localhost"],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return True


def upsert_gmail_source(
    *,
    settings: Settings,
    display_name: str,
    gmail_query: str,
    sender_allowlist: list[str],
) -> dict:
    with session_scope(settings) as db:
        existing_sources = list(
            db.execute(
                select(JobAlertSource)
                .where(
                    JobAlertSource.provider == "gmail",
                    JobAlertSource.gmail_query == gmail_query,
                )
                .order_by(JobAlertSource.job_alert_source_id)
            ).scalars()
        )
        source = existing_sources[0] if existing_sources else None
        created = source is None
        if source is None:
            source = JobAlertSource(
                job_alert_source_id=new_id("alertsrc"),
                candidate_id="candidate_local",
                provider="gmail",
                display_name=display_name,
                gmail_query=gmail_query,
                sender_allowlist=sender_allowlist,
                enabled=True,
            )
            db.add(source)
        else:
            source.display_name = display_name
            source.sender_allowlist = sender_allowlist
            source.enabled = True
        db.flush()
        return {
            **source.to_api(),
            "created": created,
            "duplicate_source_ids": [
                duplicate.job_alert_source_id for duplicate in existing_sources[1:]
            ],
        }


if __name__ == "__main__":
    raise SystemExit(main())
