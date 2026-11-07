from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from jobcockpit.api.transcripts import MAX_TRANSCRIPT_BYTES, TRANSCRIPT_SOURCE_KIND
from jobcockpit.db.models import AuditLogEntry, StoredArtifact
from jobcockpit.db.session import session_scope
from test_profile_flow import make_client, profile_payload


def pdf_bytes(width=200):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_transcript_versions_latest_download_and_profile_edits(tmp_path):
    client, settings = make_client(tmp_path)
    assert client.get("/api/profile/transcript/latest/download").status_code == 404
    assert client.post("/api/profile/transcript", files={
        "file": ("transcript.pdf", pdf_bytes(), "application/pdf"),
    }).status_code == 404
    client.post("/api/profile", json=profile_payload())
    ids = []
    for width in (200, 300):
        response = client.post("/api/profile/transcript", files={
            "file": ("same-name.pdf", pdf_bytes(width), "text/plain"),
        })
        assert response.status_code == 201
        ids.append(response.json()["latest_transcript_artifact_id"])
    assert ids[0] != ids[1]
    edited = client.put("/api/profile", json=profile_payload(gpa="3.5")).json()
    assert edited["latest_transcript_artifact_id"] == ids[1]
    assert edited["evidence_items"] == []
    assert [a["is_latest"] for a in edited["transcript_artifacts"]] == [True, False]
    assert edited["transcript_artifacts"][0]["created_at"].endswith("+00:00")
    latest = client.get("/api/profile/transcript/latest/download")
    assert latest.content == pdf_bytes(300)
    assert latest.headers["cache-control"] == "no-store"
    assert latest.headers["content-type"] == "application/pdf"
    assert client.get(f"/api/profile/transcript/{ids[0]}/download").content == pdf_bytes(200)
    with session_scope(settings) as db:
        artifact = db.get(StoredArtifact, ids[1])
        assert artifact.source_kind == TRANSCRIPT_SOURCE_KIND
        assert artifact.privacy_class == "private"
        assert artifact.candidate_id == "candidate_local"
        assert len(artifact.sha256) == 64
        assert artifact.size_bytes == len(latest.content)
        assert artifact.storage_uri.startswith("local://candidate-documents/")
        assert len(list(db.scalars(select(AuditLogEntry).where(
            AuditLogEntry.event_type == "profile.transcript_uploaded",
        )))) == 2


@pytest.mark.parametrize("filename,content,status", [
    ("transcript.txt", b"text", 400),
    ("transcript.pdf", b"not a pdf", 400),
    ("transcript.pdf", b"", 400),
    ("transcript.pdf", b"x" * (MAX_TRANSCRIPT_BYTES + 1), 413),
], ids=["wrong-extension", "invalid-pdf", "empty", "too-large"])
def test_invalid_transcripts_do_not_replace_latest(tmp_path, filename, content, status):
    client, _ = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    original = client.post("/api/profile/transcript", files={
        "file": ("transcript.pdf", pdf_bytes(), "application/pdf"),
    }).json()
    response = client.post("/api/profile/transcript", files={"file": (filename, content)})
    assert response.status_code == status
    assert client.get("/api/profile/transcript").json() == original


def test_latest_is_candidate_scoped_and_ignores_deleted_and_other_documents(tmp_path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    original = client.post("/api/profile/transcript", files={
        "file": ("transcript.pdf", pdf_bytes(), "application/pdf"),
    }).json()["latest_transcript_artifact_id"]
    with session_scope(settings) as db:
        for index, overrides in enumerate([
            {"candidate_id": "someone_else"},
            {"source_kind": "profile_resume_upload"},
            {"deleted_at": datetime.now(UTC)},
            {"artifact_type": "generated_document"},
        ]):
            row = StoredArtifact(
                artifact_id=f"excluded_{index}", candidate_id="candidate_local",
                artifact_type="candidate_document", source_kind=TRANSCRIPT_SOURCE_KIND,
                storage_uri="local://missing.pdf", original_file_name="transcript.pdf",
                content_type="application/pdf", size_bytes=1, sha256="0" * 64,
                privacy_class="private", created_at=datetime.now(UTC) + timedelta(days=1),
            )
            for key, value in overrides.items():
                setattr(row, key, value)
            db.add(row)
    assert client.get("/api/profile/transcript").json()["latest_transcript_artifact_id"] == original
    for index in range(4):
        assert client.get(f"/api/profile/transcript/excluded_{index}/download").status_code == 404
    assert client.get(f"/api/profile/resume/{original}/download").status_code == 404


def test_encrypted_pdf_is_rejected_and_missing_latest_never_falls_back(tmp_path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("password")
    encrypted = BytesIO()
    writer.write(encrypted)
    assert client.post("/api/profile/transcript", files={
        "file": ("locked.pdf", encrypted.getvalue()),
    }).status_code == 400
    for width in (200, 300):
        payload = client.post("/api/profile/transcript", files={
            "file": ("transcript.pdf", pdf_bytes(width)),
        }).json()
    with session_scope(settings) as db:
        latest = db.get(StoredArtifact, payload["latest_transcript_artifact_id"])
        client.app.state.storage.delete(latest.storage_uri)
    assert client.get("/api/profile/transcript/latest/download").status_code == 404


def test_permission_encrypted_pdf_without_open_password_is_stored_unchanged(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="", owner_password="issuer-only")
    output = BytesIO()
    writer.write(output)
    content = output.getvalue()
    response = client.post("/api/profile/transcript", files={
        "file": ("unofficial.pdf", content, "application/pdf"),
    })
    assert response.status_code == 201
    assert response.json()["latest_transcript_artifact_id"]
    assert client.get("/api/profile/transcript/latest/download").content == content
