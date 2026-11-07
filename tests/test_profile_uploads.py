from hashlib import sha256

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from jobcockpit.db.models import AuditLogEntry, CandidateProfile, EvidenceItem, StoredArtifact
from jobcockpit.db.session import get_engine, session_scope
from jobcockpit.profiles import MAX_RESUME_UPLOAD_BYTES
from jobcockpit.services import profile_uploads


UPLOAD_KINDS = [
    ("resume", "resume", "resume"),
    ("cover-letter-sample", "cover letter sample", "cover_letter_sample"),
]


@pytest.fixture
def upload_client(jobcockpit_settings_factory, jobcockpit_client_factory):
    settings = jobcockpit_settings_factory()
    client = jobcockpit_client_factory(settings)
    assert client.post("/api/profile", json={
        "first_name": "Sample", "last_name": "Candidate",
        "email": "sample" + "@example.invalid", "skills": ["Python"],
    }).status_code == 201
    return client, settings


@pytest.mark.parametrize("endpoint,label,kind", UPLOAD_KINDS)
def test_upload_validates_type_and_size_before_creating_state(upload_client, endpoint, label, kind):
    client, settings = upload_client
    for filename, content, status, detail in [
        ("sample.exe", b"binary", 400, f"Unsupported {label} file type"),
        ("sample.txt", b"x" * (MAX_RESUME_UPLOAD_BYTES + 1), 413,
         f"{label.capitalize()} upload exceeds 5 MB"),
    ]:
        response = client.post(f"/api/profile/{endpoint}", files={"file": (filename, content)})
        assert response.status_code == status
        assert response.json() == {"detail": detail}
    with session_scope(settings) as db:
        assert list(db.scalars(select(StoredArtifact))) == []
        assert f"latest_{kind}_artifact_id" not in db.get(CandidateProfile, "candidate_local").profile_data
    assert not any(path.is_file() for path in settings.artifact_root.rglob("*"))


@pytest.mark.parametrize("endpoint,label,kind", UPLOAD_KINDS)
def test_upload_requires_profile_before_file_validation(
    jobcockpit_settings_factory, jobcockpit_client_factory, endpoint, label, kind,
):
    client = jobcockpit_client_factory(jobcockpit_settings_factory())
    response = client.post(f"/api/profile/{endpoint}", files={"file": ("sample.exe", b"binary")})
    assert response.status_code == 404
    assert response.json() == {"detail": "Profile not found"}


@pytest.mark.parametrize("endpoint,label,kind", UPLOAD_KINDS)
def test_upload_bounds_read_and_preserves_private_bytes(upload_client, monkeypatch, endpoint, label, kind):
    client, settings = upload_client
    calls = []
    original_read = UploadFile.read

    async def bounded_read(file, size=-1):
        calls.append(size)
        return await original_read(file, size)

    monkeypatch.setattr(UploadFile, "read", bounded_read)
    content = "Built Python services for Montréal\n".encode("utf-8")
    response = client.post(
        f"/api/profile/{endpoint}", files={"file": ("sample.TXT", content, "text/custom")},
    )
    assert response.status_code == 201
    assert calls == [MAX_RESUME_UPLOAD_BYTES + 1]
    payload = response.json()
    artifact = payload["artifact"]
    assert payload["parse_status"] == "parsed"
    assert artifact["candidate_id"] == "candidate_local"
    assert artifact["privacy_class"] == "private"
    assert artifact["source_kind"] == f"profile_{kind}_upload"
    assert artifact["original_file_name"] == "sample.TXT"
    assert artifact["content_type"] == "text/custom"
    assert artifact["sha256"] == sha256(content).hexdigest()
    assert artifact["size_bytes"] == len(content)
    assert "storage_uri" not in artifact
    with session_scope(settings) as db:
        row = db.get(StoredArtifact, artifact["artifact_id"])
        assert client.app.state.storage.read_bytes(row.storage_uri) == content
        profile = db.get(CandidateProfile, "candidate_local")
        assert profile.profile_data[f"latest_{kind}_artifact_id"] == row.artifact_id
        assert profile.profile_data[f"latest_{kind}_parse_status"] == "parsed"
        evidence = list(db.scalars(select(EvidenceItem)))
        assert len(evidence) == (1 if kind == "resume" else 0)
        if kind == "resume":
            assert payload["evidence_created_count"] == 1
            assert evidence[0].source_artifact_id == row.artifact_id
            assert evidence[0].private and evidence[0].allowed_for_resume and evidence[0].allowed_for_cover_letter
        else:
            assert "evidence_items" not in payload
            assert "evidence_created_count" not in payload


@pytest.mark.parametrize("filename,content,warning", [
    ("sample.pdf", b"unreadable PDF", "parse_failed"),
    ("sample.txt", b"", "no_text_extracted"),
])
def test_unparsed_cover_letter_sample_stays_private_and_creates_no_evidence(
    upload_client, filename, content, warning,
):
    client, settings = upload_client
    response = client.post("/api/profile/cover-letter-sample", files={"file": (filename, content)})
    assert response.status_code == 201
    payload = response.json()
    assert payload["parse_status"] == "stored_unparsed"
    assert payload["warnings"] == [warning]
    with session_scope(settings) as db:
        row = db.get(StoredArtifact, payload["artifact"]["artifact_id"])
        assert client.app.state.storage.read_bytes(row.storage_uri) == content
        assert list(db.scalars(select(EvidenceItem))) == []


def upload_state(client, settings):
    with session_scope(settings) as db:
        return (
            dict(db.get(CandidateProfile, "candidate_local").profile_data),
            [(row.artifact_id, client.app.state.storage.read_bytes(row.storage_uri))
             for row in db.scalars(select(StoredArtifact).order_by(StoredArtifact.artifact_id))],
            [row.evidence_id for row in db.scalars(select(EvidenceItem).order_by(EvidenceItem.evidence_id))],
            [row.audit_log_entry_id for row in db.scalars(
                select(AuditLogEntry).order_by(AuditLogEntry.audit_log_entry_id)
            )],
            sorted((str(path), path.read_bytes()) for path in settings.artifact_root.rglob("*") if path.is_file()),
        )


@pytest.mark.parametrize("endpoint,label,kind", UPLOAD_KINDS)
@pytest.mark.parametrize("failure_phase", ["parse", "commit"])
def test_failed_upload_rolls_back_new_bytes_and_all_state(
    upload_client, monkeypatch, endpoint, label, kind, failure_phase,
):
    client, settings = upload_client
    assert client.post("/api/profile/resume", files={"file": (
        "previous.txt", b"Built Python services", "text/plain",
    )}).status_code == 201
    assert client.post("/api/profile/cover-letter-sample", files={"file": (
        "previous-sample.txt", b"Prior writing sample", "text/plain",
    )}).status_code == 201
    before = upload_state(client, settings)

    def fail(*args):
        raise TimeoutError("injected upload failure")

    def fail_commit(db):
        if db.get_bind() is get_engine(settings):
            fail()

    if failure_phase == "parse":
        monkeypatch.setattr(profile_uploads, "parse_resume_bytes", fail)
    else:
        event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(TimeoutError, match="injected upload failure"):
            client.post(f"/api/profile/{endpoint}", files={"file": (
                "new.txt", b"Developed Python systems", "text/plain",
            )})
    finally:
        if failure_phase == "commit":
            event.remove(Session, "before_commit", fail_commit)
    assert upload_state(client, settings) == before
