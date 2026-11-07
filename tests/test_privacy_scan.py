from pathlib import Path
import subprocess
import sys

import pytest

from scripts.privacy_scan import iter_files, main, scan_git, scan_text


def test_privacy_scan_flags_email_tokens_and_private_artifacts(tmp_path: Path):
    sample = tmp_path / "sample.txt"
    sample.write_text(
        "real.person@example.com\n"
        "ya29.access-token\n"
        "refresh_token=secret\n"
        "var/private/storage/generated-documents/resume.pdf\n",
        encoding="utf-8",
    )

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)
    finding_types = {finding.kind for finding in findings}

    assert {"email", "gmail_token", "refresh_token", "private_artifact"} <= finding_types


def test_privacy_scan_allows_public_fixture_sender_domains(tmp_path: Path):
    sample = tmp_path / "docs.md"
    sample.write_text(
        "Use jobs@mapleradar.com for the demo source, "
        "jobalerts-noreply@linkedin.com for LinkedIn alerts, and "
        "donotreply@jobalert.indeed.com for Indeed alerts, and "
        "jane@example.invalid for local examples.",
        encoding="utf-8",
    )

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert {finding.kind for finding in findings} == set()


def test_privacy_scan_ignores_unformatted_job_ids_as_phone_numbers(tmp_path: Path):
    sample = tmp_path / "links.txt"
    sample.write_text("https://www.linkedin.com/jobs/view/4425618748", encoding="utf-8")

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert "phone" not in {finding.kind for finding in findings}


def test_privacy_scan_flags_formatted_phone_numbers(tmp_path: Path):
    sample = tmp_path / "phone.txt"
    sample.write_text("Call 202-555-0142 for details.", encoding="utf-8")

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert "phone" in {finding.kind for finding in findings}


def test_privacy_scan_allows_synthetic_thread_ids(tmp_path: Path):
    sample = tmp_path / "gmail_tests.py"
    sample.write_text('provider_thread_id_hash="thread_retry_api_failure"', encoding="utf-8")

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert "message_id" not in {finding.kind for finding in findings}


def test_privacy_scan_allows_refresh_token_attribute_access(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text("if credentials.refresh_token:\n    refresh()\n", encoding="utf-8")

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert {finding.kind for finding in findings} == set()


def test_privacy_scan_flags_quoted_refresh_token_json_key(tmp_path: Path):
    sample = tmp_path / "token.json"
    sample.write_text('{"refresh_token": "secret"}\n', encoding="utf-8")

    findings = scan_text(sample.read_text(encoding="utf-8"), path=sample)

    assert "refresh_token" in {finding.kind for finding in findings}


def test_privacy_scan_includes_fixture_paths(tmp_path: Path):
    fixture = tmp_path / "data" / "fixtures" / "email-alerts"
    fixture.mkdir(parents=True)
    sample = fixture / "raw.eml"
    sample.write_text("real.person@example.com", encoding="utf-8")

    scanned = iter_files(tmp_path)

    assert sample in scanned


def test_privacy_scan_ignores_generated_tool_directories(tmp_path: Path):
    samples = []
    for relative_path in (
        Path(".pytest_cache/v/cache"),
        Path(".tmp/uv-cache"),
        Path(".tmp_job_posting_scan"),
    ):
        generated = tmp_path / relative_path
        generated.mkdir(parents=True)
        sample = generated / "leak.txt"
        sample.write_text("real.person@example.com", encoding="utf-8")
        samples.append(sample)

    scanned = iter_files(tmp_path)

    assert all(sample not in scanned for sample in samples)


def test_privacy_scan_cli_does_not_echo_sensitive_values(tmp_path: Path, monkeypatch, capsys):
    sample = tmp_path / "leak.txt"
    sample.write_text(
        "real.person@example.com\nya29.access-token\nrefresh_token=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["privacy_scan.py", str(tmp_path)])

    exit_code = main()
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "email" in output
    assert "gmail_token" in output
    assert "refresh_token" in output
    assert "real.person@example.com" not in output
    assert "ya29.access-token" not in output
    assert "secret" not in output


@pytest.mark.parametrize(("value", "kind"), [
    ("ghp_" + "A" * 36, "github_token"),
    ("github_pat_" + "A" * 60, "github_token"),
    ("sk-proj-" + "a" * 48, "api_key"),
    ("AKIA" + "A" * 16, "aws_access_key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key"),
    ("SERVICE_API_KEY=" + "a1" * 20, "credential_assignment"),
])
def test_privacy_scan_flags_common_credentials(value, kind):
    assert kind in {finding.kind for finding in scan_text(value, Path("sample.txt"))}


def test_git_scan_reads_committed_bytes_and_detects_deleted_history(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git("init")
    git("config", "user.name", "Synthetic Contributor")
    git("config", "user.email", "contributor@example.invalid")
    (tmp_path / ".gitignore").write_text("var/\n", encoding="utf-8")
    (tmp_path / "var").mkdir()
    private = tmp_path / "var" / "local.db"
    private.write_bytes(b"\xff\x00private database")
    sample = tmp_path / "config.txt"
    sample.write_text("ghp_" + "A" * 36, encoding="utf-8")
    git("add", ".gitignore", "config.txt")
    git("add", "-f", "var/local.db")
    git("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic leak")
    # Working-tree mode respects ignored local files but includes tracked files
    # even if they match an ignore rule.
    ignored = tmp_path / "var" / "ignored.txt"
    ignored.write_text("local secret", encoding="utf-8")
    assert private in iter_files(tmp_path)
    assert ignored not in iter_files(tmp_path)
    # Cleaning the working file must not conceal the committed credential.
    sample.write_text("sanitized", encoding="utf-8")
    findings = scan_git(tmp_path, "HEAD")
    assert {finding.kind for finding in findings} == {"github_token", "private_file"}
    git("rm", "var/local.db")
    git("add", "config.txt")
    git("-c", "commit.gpgsign=false", "commit", "-m", "Remove synthetic leak")
    assert scan_git(tmp_path, "HEAD") == []
    assert {finding.kind for finding in scan_git(tmp_path, "HEAD", history=True)} == {
        "github_token", "private_file"
    }


def test_git_scan_cli_fails_closed_for_unknown_revision(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["privacy_scan.py", str(tmp_path), "--git-ref", "missing"])
    assert main() == 2
    assert "Could not scan Git revision" in capsys.readouterr().err
