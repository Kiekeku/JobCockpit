from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrivacyFinding:
    kind: str
    path: Path
    snippet: str


PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"\b(?:\+?1[-.\s])?(?:\(\d{3}\)|\d{3}[-.\s])\d{3}[-.\s]\d{4}\b")),
    ("gmail_token", re.compile(r"\bya29\.[A-Za-z0-9._-]+")),
    (
        "refresh_token",
        re.compile(r"(?<!\.)['\"]?\brefresh_token\b['\"]?\s*[:=]\s*\S+", re.IGNORECASE),
    ),
    ("google_client_secret", re.compile(r"GOCSPX-[A-Za-z0-9_-]+")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("api_key", re.compile(r"\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    (
        "credential_assignment",
        re.compile(
            r"\b[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|TOKEN_SECRET|PASSWORD)"
            r"\b['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_+/=-]{32,}",
            re.IGNORECASE,
        ),
    ),
    ("message_id", re.compile(r"\b(?:msg|thread)_[A-Za-z0-9_-]{8,}\b")),
    ("private_artifact", re.compile(r"var[/\\]private[/\\]storage[/\\]", re.IGNORECASE)),
]

ALLOWED_EMAIL_DOMAINS = {
    "example.invalid",
    "example.test",
}
ALLOWED_EMAILS = {
    "donotreply@jobalert.indeed.com",
    "jobalerts-noreply@linkedin.com",
    "jobs@mapleradar.com",
    "support@mapleradar.com",
}
ALLOWED_MESSAGE_ID_PREFIXES = ("thread_retry_",)
ALLOWLISTED_FINDING_PATHS = {
    Path("tests/test_privacy_scan.py"),
}


def _is_allowed_finding(kind: str, snippet: str) -> bool:
    value = snippet.lower()
    if kind == "email":
        if value in ALLOWED_EMAILS:
            return True
        _, _, domain = value.rpartition("@")
        return domain in ALLOWED_EMAIL_DOMAINS
    if kind == "message_id":
        return value.startswith(ALLOWED_MESSAGE_ID_PREFIXES)
    return False


def scan_text(text: str, path: Path) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if _is_allowed_finding(kind, snippet):
                continue
            findings.append(PrivacyFinding(kind=kind, path=path, snippet=snippet[:80]))
    return findings


def iter_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        # Include tracked files and new source files, but not ignored local
        # credentials. The committed-ref mode separately checks the saved bytes.
        output = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            check=True, capture_output=True,
        ).stdout
        return [
            path for name in output.decode("utf-8").split("\0")
            if name and (path := root / name).is_file()
        ]
    ignored = {
        ".git",
        ".agents",
        ".codex",
        ".mypy_cache",
        ".playwright-cli",
        ".venv",
        ".pytest_cache",
        ".pytest-tmp",
        ".ruff_cache",
        ".tmp",
        ".tmp_job_posting_scan",
        "node_modules",
        ".pnpm-store",
        ".next",
        "dist",
        "var",
        "__pycache__",
    }
    files: list[Path] = []
    # Prune before descending: private artifact trees and dependency caches can
    # contain millions of files, none of which belong in a source-directory scan.
    for directory, children, names in os.walk(root):
        children[:] = [name for name in children if name not in ignored]
        files.extend(Path(directory) / name for name in names)
    return files


def private_path(path: Path) -> bool:
    """Reject tracked runtime/credential files even when their bytes are binary."""
    name = path.name.lower()
    return (
        any(part.lower() in {"var", ".env", ".codex", ".agents"} for part in path.parts)
        or (name.startswith(".env.") and name != ".env.example")
        or name in {"credentials.json", "token.json", "client_secret.json"}
        or name.startswith("client_secret_")
        or name.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx",
                          ".eml", ".mbox", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm"))
    )


def scan_git(root: Path, ref: str, *, history: bool = False) -> list[PrivacyFinding]:
    """Read committed blobs directly, without touching the index or working tree.

    History visits each unique reachable object once. Git associates one path
    with each historical blob; commit metadata is intentionally outside this scan.
    """
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        ).stdout

    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    if history:
        objects = [
            (parts[0], Path(parts[1]))
            for line in git("rev-list", "--objects", commit).decode("utf-8").splitlines()
            if len(parts := line.split(" ", 1)) == 2
        ]
    else:
        objects = []
        for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            _, kind, oid = metadata.split()
            if kind == b"blob":
                objects.append((oid.decode("ascii"), Path(raw_path.decode("utf-8"))))

    findings: list[PrivacyFinding] = []
    with subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for oid, path in objects:
                process.stdin.write(oid.encode("ascii") + b"\n")
                process.stdin.flush()
                _, kind, size = process.stdout.readline().split()
                content = process.stdout.read(int(size))
                process.stdout.read(1)
                if kind != b"blob":
                    continue
                if private_path(path):
                    findings.append(PrivacyFinding("private_file", path, ""))
                if path in ALLOWLISTED_FINDING_PATHS:
                    continue
                try:
                    findings.extend(scan_text(content.decode("utf-8"), path))
                except UnicodeDecodeError:
                    continue
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Git object reader failed")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check source files for private data and credentials.")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--git-ref", help="Scan committed files at this Git revision, including ignored paths")
    parser.add_argument("--history", action="store_true", help="With --git-ref, scan reachable file history")
    args = parser.parse_args()
    if args.history and not args.git_ref:
        parser.error("--history requires --git-ref")
    root = args.root
    if args.git_ref:
        try:
            findings = scan_git(root, args.git_ref, history=args.history)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
            print("Could not scan Git revision; verify the repository and ref.", file=sys.stderr)
            return 2
    else:
        findings = scan_directory(root)
    for path, kind in sorted({(str(f.path), f.kind) for f in findings}):
        print(f"{path}: {kind}")
    return 1 if findings else 0


def scan_directory(root: Path) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for path in iter_files(root):
        try:
            path_findings = scan_text(path.read_text(encoding="utf-8"), path)
            relative = path.relative_to(root)
            if relative in ALLOWLISTED_FINDING_PATHS:
                continue
            findings.extend(path_findings)
        except UnicodeDecodeError:
            continue
    return findings


if __name__ == "__main__":
    raise SystemExit(main())
