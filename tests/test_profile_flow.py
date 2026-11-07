from __future__ import annotations

from conftest import finish_generation

from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from jobcockpit.api.main import create_app
from jobcockpit.core.config import Settings
from jobcockpit.db.models import CandidateProfile, EvidenceItem, StoredArtifact
from jobcockpit.db.session import init_database, session_scope
from jobcockpit.profiles import MAX_RESUME_UPLOAD_BYTES, extract_resume_evidence
from jobcockpit.storage.artifacts import ArtifactStorageService


def make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'jobcockpit.db'}",
        artifact_root=tmp_path / "private" / "storage",
        extension_token_secret="test-secret",
        ats_mock_port=8001,
    )
    init_database(settings)
    return TestClient(create_app(settings)), settings


def profile_payload(**overrides):
    payload = {
        "first_name": "Jane",
        "last_name": "Candidate",
        "email": sample_email("jane.candidate"),
        "phone": "local-only",
        "links": ["https://portfolio.example.invalid/jane"],
        "summary": "Backend engineer focused on local-first systems.",
        "cover_letter_custom_instructions": "",
        "target_role_include_keywords": ["software", "developer"],
        "target_role_exclude_keywords": ["accounting", "full-time"],
        "language_filter_enabled": True,
        "willing_to_relocate": True,
        "work_authorized_countries": ["Canada"],
        "requires_sponsorship": False,
        "languages": [
            {"language": "English", "proficiency": "fluent"},
            {"language": "French", "proficiency": "none"},
        ],
        "skills": ["Python", "FastAPI", "SQL"],
    }
    payload.update(overrides)
    return payload


def test_profile_create_get_update_and_duplicate_create(tmp_path: Path):
    client, settings = make_client(tmp_path)

    empty = client.get("/api/profile")
    assert empty.status_code == 200
    assert empty.json() == {
        "profile": None,
        "transcript_artifacts": [],
        "latest_transcript_artifact_id": None,
        "resume_artifacts": [],
        "cover_letter_sample_artifacts": [],
        "evidence_items": [],
    }

    update_before_create = client.put("/api/profile", json=profile_payload(first_name="No", last_name="Profile Yet"))
    assert update_before_create.status_code == 404

    created = client.post("/api/profile", json=profile_payload())
    assert created.status_code == 201
    assert created.json()["profile"]["first_name"] == "Jane"
    assert created.json()["profile"]["last_name"] == "Candidate"
    assert created.json()["profile"]["full_name"] == "Jane Candidate"
    assert created.json()["profile"]["skills"] == ["Python", "FastAPI", "SQL"]
    assert created.json()["profile"]["cover_letter_custom_instructions"] == ""
    assert created.json()["profile"]["target_role_include_keywords"] == ["software", "developer"]
    assert created.json()["profile"]["target_role_exclude_keywords"] == ["accounting", "full-time"]
    assert created.json()["profile"]["language_filter_enabled"] is True
    assert created.json()["profile"]["willing_to_relocate"] is True
    assert created.json()["profile"]["work_authorized_countries"] == ["Canada"]
    assert created.json()["profile"]["requires_sponsorship"] is False
    assert created.json()["profile"]["languages"] == [
        {"language": "English", "proficiency": "fluent"},
        {"language": "French", "proficiency": "none"},
    ]

    duplicate = client.post("/api/profile", json=profile_payload(first_name="Duplicate"))
    assert duplicate.status_code == 409

    updated = client.put(
        "/api/profile",
        json=profile_payload(
            first_name="Jane",
            last_name="Updated",
            phone="local-phone-updated",
            cover_letter_custom_instructions="No em-dashes. Reference RankAlpha.io.",
            target_role_include_keywords=["Software", "software", "backend"],
            target_role_exclude_keywords=["Accounting", "accounting", "sales"],
            language_filter_enabled=False,
            willing_to_relocate=False,
            work_authorized_countries=[" Canada ", "canada", "United States", ""],
            requires_sponsorship=False,
            languages=[
                {"language": "English", "proficiency": "native"},
                {"language": "French", "proficiency": "professional"},
            ],
            skills=["Python", "React", "SQLite"],
        ),
    )
    assert updated.status_code == 200
    assert updated.json()["profile"]["full_name"] == "Jane Updated"
    assert updated.json()["profile"]["phone"] == "local-phone-updated"
    assert updated.json()["profile"]["cover_letter_custom_instructions"] == "No em-dashes. Reference RankAlpha.io."
    assert updated.json()["profile"]["target_role_include_keywords"] == ["Software", "backend"]
    assert updated.json()["profile"]["target_role_exclude_keywords"] == ["Accounting", "sales"]
    assert updated.json()["profile"]["language_filter_enabled"] is False
    assert updated.json()["profile"]["willing_to_relocate"] is False
    assert updated.json()["profile"]["work_authorized_countries"] == ["Canada", "United States"]
    assert updated.json()["profile"]["requires_sponsorship"] is False
    assert updated.json()["profile"]["languages"] == [
        {"language": "English", "proficiency": "native"},
        {"language": "French", "proficiency": "professional"},
    ]
    assert updated.json()["profile"]["skills"] == ["Python", "React", "SQLite"]

    fetched = client.get("/api/profile").json()
    assert fetched["profile"]["full_name"] == "Jane Updated"
    with session_scope(settings) as db:
        profile = db.get(CandidateProfile, "candidate_local")
        assert profile.profile_name == "Jane Updated"
        assert profile.profile_data["first_name"] == "Jane"
        assert profile.profile_data["last_name"] == "Updated"
        assert profile.profile_data["full_name"] == "Jane Updated"
        assert profile.profile_data["email"] == sample_email("jane.candidate")
        assert profile.profile_data["cover_letter_custom_instructions"] == "No em-dashes. Reference RankAlpha.io."
        assert profile.profile_data["language_filter_enabled"] is False
        assert profile.profile_data["willing_to_relocate"] is False
        assert profile.profile_data["work_authorized_countries"] == ["Canada", "United States"]
        assert profile.profile_data["requires_sponsorship"] is False
        assert profile.profile_data["languages"] == [
            {"language": "English", "proficiency": "native"},
            {"language": "French", "proficiency": "professional"},
        ]



def test_profile_defaults_relocation_willingness_to_unspecified(tmp_path: Path):
    client, settings = make_client(tmp_path)
    payload = profile_payload()
    payload.pop("willing_to_relocate")
    payload.pop("work_authorized_countries")
    payload.pop("requires_sponsorship")

    created = client.post("/api/profile", json=payload)

    assert created.status_code == 201
    assert created.json()["profile"]["willing_to_relocate"] is None
    assert created.json()["profile"]["work_authorized_countries"] == []
    assert created.json()["profile"]["requires_sponsorship"] is None
    assert client.get("/api/profile").json()["profile"]["willing_to_relocate"] is None
    assert client.get("/api/profile").json()["profile"]["work_authorized_countries"] == []
    assert client.get("/api/profile").json()["profile"]["requires_sponsorship"] is None
    with session_scope(settings) as db:
        profile = db.get(CandidateProfile, "candidate_local")
        assert profile.profile_data["willing_to_relocate"] is None
        assert profile.profile_data["work_authorized_countries"] == []
        assert profile.profile_data["requires_sponsorship"] is None

def test_profile_captures_student_ats_fields(tmp_path: Path):
    client, settings = make_client(tmp_path)

    created = client.post(
        "/api/profile",
        json=profile_payload(
            current_year="  3rd year  ",
            graduation_date="  May 2027  ",
            gpa="  3.8/4.0  ",
            co_op_program=True,
        ),
    )

    assert created.status_code == 201
    profile = created.json()["profile"]
    assert profile["current_year"] == "3rd year"
    assert profile["graduation_date"] == "May 2027"
    assert profile["graduation_month"] == "May"
    assert profile["graduation_year"] == "2027"
    assert profile["gpa"] == "3.8/4.0"
    assert profile["co_op_program"] is True

    updated = client.put(
        "/api/profile",
        json=profile_payload(
            current_year="4th year",
            graduation_month="December",
            graduation_year="2027",
            gpa="",
            co_op_program=False,
        ),
    )

    assert updated.status_code == 200
    profile = updated.json()["profile"]
    assert profile["current_year"] == "4th year"
    assert profile["graduation_date"] == "December 2027"
    assert profile["graduation_month"] == "December"
    assert profile["graduation_year"] == "2027"
    assert profile["gpa"] == ""
    assert profile["co_op_program"] is False

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["current_year"] == "4th year"
        assert stored.profile_data["graduation_date"] == "December 2027"
        assert stored.profile_data["graduation_month"] == "December"
        assert stored.profile_data["graduation_year"] == "2027"
        assert stored.profile_data["gpa"] == ""
        assert stored.profile_data["co_op_program"] is False


def test_profile_derives_structured_graduation_fields_from_legacy_date(tmp_path: Path):
    client, settings = make_client(tmp_path)
    created = client.post(
        "/api/profile",
        json=profile_payload(graduation_date="  August 2028  "),
    )

    assert created.status_code == 201
    profile = created.json()["profile"]
    assert profile["graduation_date"] == "August 2028"
    assert profile["graduation_month"] == "August"
    assert profile["graduation_year"] == "2028"

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["graduation_date"] == "August 2028"
        assert stored.profile_data["graduation_month"] == "August"
        assert stored.profile_data["graduation_year"] == "2028"


def test_profile_preserves_unparsed_legacy_graduation_date_detail(tmp_path: Path):
    client, settings = make_client(tmp_path)
    created = client.post(
        "/api/profile",
        json=profile_payload(graduation_date="  Spring 2028  "),
    )

    assert created.status_code == 201
    profile = created.json()["profile"]
    assert profile["graduation_date"] == "Spring 2028"
    assert profile["graduation_month"] == ""
    assert profile["graduation_year"] == "2028"

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["graduation_date"] == "Spring 2028"
        assert stored.profile_data["graduation_month"] == ""
        assert stored.profile_data["graduation_year"] == "2028"


def test_profile_update_preserves_unparsed_legacy_graduation_date_when_year_is_supplied(tmp_path: Path):
    client, settings = make_client(tmp_path)
    created = client.post(
        "/api/profile",
        json=profile_payload(graduation_date="Spring 2028"),
    )
    assert created.status_code == 201

    updated = client.put(
        "/api/profile",
        json=profile_payload(
            graduation_date="Spring 2028",
            graduation_month="",
            graduation_year="2028",
        ),
    )

    assert updated.status_code == 200
    profile = updated.json()["profile"]
    assert profile["graduation_date"] == "Spring 2028"
    assert profile["graduation_month"] == ""
    assert profile["graduation_year"] == "2028"

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["graduation_date"] == "Spring 2028"
        assert stored.profile_data["graduation_month"] == ""
        assert stored.profile_data["graduation_year"] == "2028"


def test_profile_captures_structured_address_fields(tmp_path: Path):
    client, settings = make_client(tmp_path)

    created = client.post(
        "/api/profile",
        json=profile_payload(
            street_address="  123 Campus Drive  ",
            address_line_2="  Unit 4  ",
            city="  Calgary  ",
            province_state="  AB  ",
            postal_code="  T2N 1N4  ",
            country="  Canada  ",
        ),
    )

    assert created.status_code == 201
    profile = created.json()["profile"]
    assert profile["street_address"] == "123 Campus Drive"
    assert profile["address_line_2"] == "Unit 4"
    assert profile["city"] == "Calgary"
    assert profile["province_state"] == "AB"
    assert profile["postal_code"] == "T2N 1N4"
    assert profile["country"] == "Canada"

    updated = client.put(
        "/api/profile",
        json=profile_payload(
            street_address="456 Co-op Ave",
            address_line_2="",
            city="Toronto",
            province_state="ON",
            postal_code="M5V 2T6",
            country="Canada",
        ),
    )

    assert updated.status_code == 200
    profile = updated.json()["profile"]
    assert profile["street_address"] == "456 Co-op Ave"
    assert profile["address_line_2"] == ""
    assert profile["city"] == "Toronto"
    assert profile["province_state"] == "ON"
    assert profile["postal_code"] == "M5V 2T6"
    assert profile["country"] == "Canada"

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["street_address"] == "456 Co-op Ave"
        assert stored.profile_data["address_line_2"] == ""
        assert stored.profile_data["city"] == "Toronto"
        assert stored.profile_data["province_state"] == "ON"
        assert stored.profile_data["postal_code"] == "M5V 2T6"
        assert stored.profile_data["country"] == "Canada"
        assert "location" not in stored.profile_data


def test_profile_captures_specific_profile_links(tmp_path: Path):
    client, settings = make_client(tmp_path)

    created = client.post(
        "/api/profile",
        json=profile_payload(
            linkedin_url="  https://www.linkedin.com/in/jane-candidate  ",
            github_url="  https://github.com/jane-candidate  ",
            links=[
                "https://portfolio.example.invalid/jane",
                " https://portfolio.example.invalid/jane ",
                " ",
            ],
        ),
    )

    assert created.status_code == 201
    profile = created.json()["profile"]
    assert profile["linkedin_url"] == "https://www.linkedin.com/in/jane-candidate"
    assert profile["github_url"] == "https://github.com/jane-candidate"
    assert profile["links"] == ["https://portfolio.example.invalid/jane"]

    with session_scope(settings) as db:
        stored = db.get(CandidateProfile, "candidate_local")
        assert stored.profile_data["linkedin_url"] == "https://www.linkedin.com/in/jane-candidate"
        assert stored.profile_data["github_url"] == "https://github.com/jane-candidate"


def test_profile_rejects_invalid_create_payload(tmp_path: Path):
    client, _settings = make_client(tmp_path)

    missing_first_name = client.post("/api/profile", json=profile_payload(first_name=" "))
    missing_last_name = client.post("/api/profile", json=profile_payload(last_name=" "))
    bad_email = client.post("/api/profile", json=profile_payload(email="not-an-email"))

    assert missing_first_name.status_code == 422
    assert missing_last_name.status_code == 422
    assert bad_email.status_code == 422


def test_profile_reads_legacy_full_name_as_split_fields(tmp_path: Path):
    client, settings = make_client(tmp_path)
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Legacy Candidate",
                profile_data={
                    "full_name": "Jane Legacy Candidate",
                    "email": sample_email("jane.legacy"),
                    "phone": "",
                    "links": [],
                    "summary": "",
                    "skills": [],
                },
            )
        )

    profile = client.get("/api/profile").json()["profile"]

    assert profile["first_name"] == "Jane"
    assert profile["last_name"] == "Legacy Candidate"
    assert profile["full_name"] == "Jane Legacy Candidate"
    assert profile["current_year"] == ""
    assert profile["graduation_date"] == ""
    assert profile["gpa"] == ""
    assert profile["co_op_program"] is None


def test_profile_reads_missing_structured_address_fields_as_defaults(tmp_path: Path):
    client, settings = make_client(tmp_path)
    with session_scope(settings) as db:
        db.add(
            CandidateProfile(
                candidate_id="candidate_local",
                profile_name="Jane Legacy Candidate",
                profile_data={
                    "first_name": "Jane",
                    "last_name": "Legacy Candidate",
                    "full_name": "Jane Legacy Candidate",
                    "email": sample_email("jane.legacy"),
                    "phone": "",
                    "links": [],
                    "summary": "",
                    "skills": [],
                },
            )
        )

    profile = client.get("/api/profile").json()["profile"]

    assert profile["street_address"] == ""
    assert profile["address_line_2"] == ""
    assert profile["city"] == ""
    assert profile["province_state"] == ""
    assert profile["postal_code"] == ""
    assert profile["country"] == ""
    assert profile["linkedin_url"] == ""
    assert profile["github_url"] == ""


def test_markdown_resume_upload_stores_private_artifact_and_creates_evidence(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())

    response = client.post(
        "/api/profile/resume",
        files={
            "file": (
                "resume.md",
                b"- Built FastAPI services with Python and SQL\n- Led local-first document storage",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["parse_status"] == "parsed"
    assert payload["evidence_created_count"] == 2
    assert payload["artifact"]["artifact_type"] == "candidate_document"
    assert payload["artifact"]["privacy_class"] == "private"
    assert payload["artifact"]["original_file_name"] == "resume.md"
    assert "storage_uri" not in payload["artifact"]

    profile_state = client.get("/api/profile").json()
    assert profile_state["profile"]["latest_resume_artifact_id"] == payload["artifact"]["artifact_id"]
    assert profile_state["profile"]["latest_resume_parse_status"] == "parsed"
    assert len(profile_state["resume_artifacts"]) == 1
    assert "storage_uri" not in profile_state["resume_artifacts"][0]
    claims = {item["claim"] for item in profile_state["evidence_items"]}
    assert "Built FastAPI services with Python and SQL" in claims
    assert "Led local-first document storage" in claims
    assert {
        item["source_artifact_id"]
        for item in profile_state["evidence_items"]
        if item["source_type"] == "resume_upload"
    } == {payload["artifact"]["artifact_id"]}

    download = client.get(f"/api/profile/resume/{payload['artifact']['artifact_id']}/download")
    assert download.status_code == 200
    assert download.headers["content-disposition"] == "attachment; filename*=UTF-8''resume.md"

    updated = client.put("/api/profile", json=profile_payload(first_name="Jane", last_name="After Upload"))
    assert updated.status_code == 200
    assert updated.json()["profile"]["latest_resume_artifact_id"] == payload["artifact"]["artifact_id"]
    assert updated.json()["profile"]["latest_resume_parse_status"] == "parsed"

    with session_scope(settings) as db:
        artifact = db.get(StoredArtifact, payload["artifact"]["artifact_id"])
        assert artifact.candidate_id == "candidate_local"
        assert artifact.artifact_type == "candidate_document"
        evidence = list(db.execute(select(EvidenceItem)).scalars())
        assert {item.source_type for item in evidence} == {"resume_upload"}
        assert all(item.private for item in evidence)
        assert {item.source_artifact_id for item in evidence} == {payload["artifact"]["artifact_id"]}


def test_cover_letter_sample_upload_is_separate_from_resume_artifacts(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    resume_response = client.post(
        "/api/profile/resume",
        files={"file": ("resume.pdf", _pdf_bytes("Built Python services"), "application/pdf")},
    )

    response = client.post(
        "/api/profile/cover-letter-sample",
        files={
            "file": (
                "sample-cover-letter.txt",
                b"Dear hiring team,\nI am excited about the role.",
                "text/plain",
            )
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["parse_status"] == "parsed"
    assert payload["artifact"]["artifact_type"] == "candidate_document"
    assert payload["artifact"]["privacy_class"] == "private"
    profile_state = client.get("/api/profile").json()
    assert profile_state["profile"]["latest_cover_letter_sample_artifact_id"] == payload["artifact"]["artifact_id"]
    assert profile_state["profile"]["latest_cover_letter_sample_parse_status"] == "parsed"
    assert [artifact["artifact_id"] for artifact in profile_state["resume_artifacts"]] == [
        resume_response.json()["artifact"]["artifact_id"]
    ]
    assert [artifact["artifact_id"] for artifact in profile_state["cover_letter_sample_artifacts"]] == [
        payload["artifact"]["artifact_id"]
    ]
    assert client.get(f"/api/profile/resume/{payload['artifact']['artifact_id']}/download").status_code == 404
    with session_scope(settings) as db:
        sample_artifact = db.get(StoredArtifact, payload["artifact"]["artifact_id"])
        assert sample_artifact.source_kind == "profile_cover_letter_sample_upload"
        assert list(db.execute(select(EvidenceItem)).scalars())


def test_profile_resume_delete_hides_artifact_and_clears_latest_pointer(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    first = client.post(
        "/api/profile/resume",
        files={"file": ("first.md", b"- Built first resume service", "text/markdown")},
    ).json()
    second = client.post(
        "/api/profile/resume",
        files={"file": ("second.md", b"- Built second resume service", "text/markdown")},
    ).json()

    latest_delete = client.delete(f"/api/profile/resume/{second['artifact']['artifact_id']}")
    assert latest_delete.status_code == 200
    assert [artifact["artifact_id"] for artifact in latest_delete.json()["resume_artifacts"]] == [
        first["artifact"]["artifact_id"]
    ]
    assert latest_delete.json()["profile"]["latest_resume_artifact_id"] == first["artifact"]["artifact_id"]
    assert latest_delete.json()["profile"]["latest_resume_parse_status"] == "parsed"
    assert {
        item["source_artifact_id"]
        for item in latest_delete.json()["evidence_items"]
        if item["source_type"] == "resume_upload"
    } == {first["artifact"]["artifact_id"]}
    assert client.get(f"/api/profile/resume/{second['artifact']['artifact_id']}/download").status_code == 404

    final_delete = client.delete(f"/api/profile/resume/{first['artifact']['artifact_id']}")
    assert final_delete.status_code == 200
    assert final_delete.json()["resume_artifacts"] == []
    assert final_delete.json()["evidence_items"] == []
    assert final_delete.json()["profile"]["latest_resume_artifact_id"] is None
    assert final_delete.json()["profile"]["latest_resume_parse_status"] == ""
    assert client.get(f"/api/profile/resume/{first['artifact']['artifact_id']}/download").status_code == 404

    with session_scope(settings) as db:
        first_artifact = db.get(StoredArtifact, first["artifact"]["artifact_id"])
        second_artifact = db.get(StoredArtifact, second["artifact"]["artifact_id"])
        assert first_artifact.deleted_at is not None
        assert second_artifact.deleted_at is not None
        assert list(db.execute(select(EvidenceItem)).scalars()) == []
        storage = ArtifactStorageService(settings.artifact_root)
        try:
            storage.read_bytes(second_artifact.storage_uri)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("deleted resume bytes should be removed from private storage")


def test_profile_resume_delete_removes_legacy_unlinked_resume_evidence(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    legacy = client.post(
        "/api/profile/resume",
        files={"file": ("legacy.md", b"- Built legacy resume service", "text/markdown")},
    ).json()
    latest = client.post(
        "/api/profile/resume",
        files={"file": ("latest.md", b"- Built latest resume service", "text/markdown")},
    ).json()
    with session_scope(settings) as db:
        db.execute(
            update(EvidenceItem)
            .where(EvidenceItem.source_artifact_id == legacy["artifact"]["artifact_id"])
            .values(source_artifact_id=None)
        )

    response = client.delete(f"/api/profile/resume/{legacy['artifact']['artifact_id']}")

    assert response.status_code == 200
    assert response.json()["profile"]["latest_resume_artifact_id"] == latest["artifact"]["artifact_id"]
    assert {
        item["source_artifact_id"]
        for item in response.json()["evidence_items"]
        if item["source_type"] == "resume_upload"
    } == {latest["artifact"]["artifact_id"]}


def test_cover_letter_sample_delete_hides_artifact_and_clears_latest_pointer(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    first = client.post(
        "/api/profile/cover-letter-sample",
        files={"file": ("first-sample.txt", b"Dear hiring team", "text/plain")},
    ).json()
    second = client.post(
        "/api/profile/cover-letter-sample",
        files={"file": ("second-sample.txt", b"Dear hiring manager", "text/plain")},
    ).json()

    latest_delete = client.delete(f"/api/profile/cover-letter-sample/{second['artifact']['artifact_id']}")

    assert latest_delete.status_code == 200
    assert [artifact["artifact_id"] for artifact in latest_delete.json()["cover_letter_sample_artifacts"]] == [
        first["artifact"]["artifact_id"]
    ]
    assert (
        latest_delete.json()["profile"]["latest_cover_letter_sample_artifact_id"]
        == first["artifact"]["artifact_id"]
    )
    assert latest_delete.json()["profile"]["latest_cover_letter_sample_parse_status"] == "parsed"

    final_delete = client.delete(f"/api/profile/cover-letter-sample/{first['artifact']['artifact_id']}")
    assert final_delete.status_code == 200
    assert final_delete.json()["cover_letter_sample_artifacts"] == []
    assert final_delete.json()["profile"]["latest_cover_letter_sample_artifact_id"] is None
    assert final_delete.json()["profile"]["latest_cover_letter_sample_parse_status"] == ""
    assert client.get(f"/api/profile/resume/{first['artifact']['artifact_id']}/download").status_code == 404
    with session_scope(settings) as db:
        first_artifact = db.get(StoredArtifact, first["artifact"]["artifact_id"])
        second_artifact = db.get(StoredArtifact, second["artifact"]["artifact_id"])
        assert first_artifact.deleted_at is not None
        assert second_artifact.deleted_at is not None


def test_resume_evidence_extraction_filters_formatting_and_merges_wrapped_claims():
    text = "\n".join(
        [
            "::: center",
            "[Jane Candidate]{.smallcaps}**\\",
            "local-phone $|$",
            "[[jane.candidate@example.invalid]{.underline}](mailto:jane.candidate@example.invalid)",
            "[[linkedin.com/in/jane-candidate]{.underline}](https://www.linkedin.com/in/jane-candidate/)",
            "# Experience",
            "Software Developer** May 2024 -- Aug. 2024",
            "Example Labs ([[example.invalid]{.underline}](https://example.invalid))*",
            "Built Python services for a synthetic",
            "library catalogue, exposing search endpoints",
            "and test fixtures through FastAPI.",
            "Designed a PostgreSQL star schema",
            "and SQL views for sample catalogue reports,",
            "supporting 2 years of invented data across",
            "$\\sim$`<!-- -->`{=html}1,200 books.",
            "Containerized demo services with Docker, reducing",
            "setup time for classroom exercises.",
            "Learning Assistant** May 2021 -- Aug. 2023",
            "Example Tutoring* *Example City*",
            "# Technical Skills",
            "Programming**: Java, Python, SQL, C++, Assembly, C\\",
        ]
    )

    evidence = extract_resume_evidence(text, ["Python", "FastAPI", "PostgreSQL", "SQL"])

    assert evidence == [
        {
            "claim": (
                "Built Python services for a synthetic library catalogue, exposing search endpoints "
                "and test fixtures through FastAPI."
            ),
            "skills": ["Python", "FastAPI"],
        },
        {
            "claim": (
                "Designed a PostgreSQL star schema and SQL views for sample catalogue reports, "
                "supporting 2 years of invented data across ~1,200 books."
            ),
            "skills": ["PostgreSQL", "SQL"],
        },
        {
            "claim": "Containerized demo services with Docker, reducing setup time for classroom exercises.",
            "skills": [],
        },
    ]


def test_resume_evidence_extraction_keeps_common_accomplishment_verbs():
    text = "\n".join(
        [
            "Maintained CI pipelines for production services.",
            "Deployed Docker services to Azure.",
            "Resolved release blockers across staging environments.",
            "Documented onboarding workflows for new developers.",
        ]
    )

    evidence = extract_resume_evidence(text, ["Docker", "Azure"])

    assert evidence == [
        {"claim": "Maintained CI pipelines for production services.", "skills": []},
        {"claim": "Deployed Docker services to Azure.", "skills": ["Docker", "Azure"]},
        {"claim": "Resolved release blockers across staging environments.", "skills": []},
        {"claim": "Documented onboarding workflows for new developers.", "skills": []},
    ]


def test_resume_evidence_extraction_handles_pandoc_wrapped_markdown_resume():
    text = "\n".join(
        [
            "-   -------------------------------------- ----------------------",
            "      **Developer**                            Sep. 2025 -- Present",
            "      *UBC Unmanned Aircraft Systems Club*          *Vancouver, BC*",
            "      -------------------------------------- ----------------------",
            "",
            "    -    Building a ROS-based orchestrator on Jetson Nano to coordinate",
            "        drone image capture workflows, correlate photos with flight",
            "        metadata, and reliably transmit packaged results over LTE to a",
            "        backend (GCOM) service.",
            "",
            "    -    Designed and implemented a responsive club website using React,",
            "        replacing a legacy Go system and reducing compile time from",
            "        hours to seconds.",
        ]
    )

    evidence = extract_resume_evidence(text, ["ROS", "React"])

    assert evidence == [
        {
            "claim": (
                "Building a ROS-based orchestrator on Jetson Nano to coordinate drone image capture workflows, "
                "correlate photos with flight metadata, and reliably transmit packaged results over LTE to a "
                "backend (GCOM) service."
            ),
            "skills": ["ROS"],
        },
        {
            "claim": (
                "Designed and implemented a responsive club website using React, replacing a legacy Go system "
                "and reducing compile time from hours to seconds."
            ),
            "skills": ["React"],
        },
    ]


def test_resume_download_sanitizes_uploaded_filename(tmp_path: Path):
    client, _settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())

    upload = client.post(
        "/api/profile/resume",
        files={"file": ('resume%2F"name.md', b"Built Python systems", "text/markdown")},
    ).json()
    download = client.get(f"/api/profile/resume/{upload['artifact']['artifact_id']}/download")

    assert download.status_code == 200
    assert download.headers["content-disposition"] == "attachment; filename*=UTF-8''resume__name.md"


def test_docx_and_pdf_resume_uploads_parse_text_evidence(tmp_path: Path):
    client, _settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload(skills=["Python", "FastAPI"]))

    docx_response = client.post(
        "/api/profile/resume",
        files={
            "file": (
                "resume.docx",
                _docx_bytes(["Built Python APIs", "Delivered FastAPI services"]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    pdf_response = client.post(
        "/api/profile/resume",
        files={"file": ("resume.pdf", _pdf_bytes("Built Python PDF services"), "application/pdf")},
    )

    assert docx_response.status_code == 201
    assert docx_response.json()["parse_status"] == "parsed"
    assert docx_response.json()["evidence_created_count"] == 2
    assert pdf_response.status_code == 201
    assert pdf_response.json()["parse_status"] == "parsed"
    assert pdf_response.json()["evidence_created_count"] >= 1

    claims = {item["claim"] for item in client.get("/api/profile").json()["evidence_items"]}
    assert "Built Python APIs" in claims
    assert "Delivered FastAPI services" in claims
    assert any("Built Python PDF services" in claim for claim in claims)


def test_unreadable_pdf_is_stored_without_evidence(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())

    response = client.post(
        "/api/profile/resume",
        files={"file": ("scan.pdf", b"%PDF-1.4\nnot a readable text pdf", "application/pdf")},
    )

    assert response.status_code == 201
    assert response.json()["parse_status"] == "stored_unparsed"
    assert response.json()["evidence_created_count"] == 0
    with session_scope(settings) as db:
        assert db.execute(select(StoredArtifact)).scalar_one().original_file_name == "scan.pdf"
        assert list(db.execute(select(EvidenceItem)).scalars()) == []


def test_resume_upload_rejects_unsupported_and_oversized_files(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())

    unsupported = client.post(
        "/api/profile/resume",
        files={"file": ("resume.exe", b"binary", "application/octet-stream")},
    )
    oversized = client.post(
        "/api/profile/resume",
        files={"file": ("resume.txt", b"x" * (MAX_RESUME_UPLOAD_BYTES + 1), "text/plain")},
    )

    assert unsupported.status_code == 400
    assert oversized.status_code == 413
    with session_scope(settings) as db:
        assert list(db.execute(select(StoredArtifact)).scalars()) == []


def test_evidence_edit_and_delete_affect_generated_resume(tmp_path: Path):
    client, _settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload(skills=["Python", "FastAPI"]))
    upload = client.post(
        "/api/profile/resume",
        files={"file": ("resume.txt", b"Built Python services\nOperated legacy systems", "text/plain")},
    ).json()
    evidence_id = upload["evidence_items"][0]["evidence_id"]

    blank_patch = client.patch(f"/api/profile/evidence/{evidence_id}", json={"claim": "   "})
    assert blank_patch.status_code == 422

    patched = client.patch(
        f"/api/profile/evidence/{evidence_id}",
        json={
            "claim": "Built Python services for local-first workflows",
            "skills": ["Python"],
            "private": False,
            "allowed_for_resume": True,
            "allowed_for_cover_letter": True,
        },
    )
    assert patched.status_code == 200
    assert patched.json()["claim"] == "Built Python services for local-first workflows"

    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Developer\nPython FastAPI", "source_url": None},
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    resume = client.post(f"/api/application-packages/{package['application_package_id']}/resume-draft").json()

    assert resume["content_preview"].startswith("# Jane Candidate")
    assert sample_email("jane.candidate") in resume["content_preview"]
    assert "Built Python services for local-first workflows" in resume["content_preview"]
    assert resume["claim_mapping_count"] == 2

    delete_response = client.delete(f"/api/profile/evidence/{evidence_id}")
    assert delete_response.status_code == 204
    next_package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    next_resume = client.post(
        f"/api/application-packages/{next_package['application_package_id']}/resume-draft"
    ).json()
    assert "Built Python services for local-first workflows" not in next_resume["content_preview"]
    assert next_resume["claim_mapping_count"] == 1


def test_cover_letter_uses_only_allowed_cover_letter_evidence(tmp_path: Path):
    client, _settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload(skills=["Python", "Communication"]))
    upload = client.post(
        "/api/profile/resume",
        files={
            "file": (
                "resume.txt",
                b"Built Python services\nManaged communication across teams",
                "text/plain",
            )
        },
    ).json()
    blocked_evidence_id = upload["evidence_items"][1]["evidence_id"]
    patch_response = client.patch(
        f"/api/profile/evidence/{blocked_evidence_id}",
        json={"allowed_for_cover_letter": False},
    )
    assert patch_response.status_code == 200

    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Developer\nPython Communication", "source_url": None},
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    cover = finish_generation(client, client.post(f"/api/application-packages/{package['application_package_id']}/cover-letter-draft")).json()

    assert "Built Python services" in cover["content_preview"]
    assert "Managed communication across teams" not in cover["content_preview"]
    assert cover["claim_mapping_count"] == 1


def test_cover_letter_and_truthfulness_use_saved_profile_contact_info(tmp_path: Path):
    client, _settings = make_client(tmp_path)
    client.post(
        "/api/profile",
        json=profile_payload(
            city="Calgary",
            province_state="AB",
            country="Canada",
            linkedin_url="https://www.linkedin.com/in/jane-candidate",
            github_url="https://github.com/jane-candidate",
            links=[
                "https://www.linkedin.com/in/jane-candidate",
                "https://github.com/jane-candidate",
                "https://portfolio.example.invalid/jane",
            ],
        ),
    )
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Acme Backend Developer\nPython APIs", "source_url": None},
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()

    resume = client.post(f"/api/application-packages/{package['application_package_id']}/resume-draft").json()
    cover = finish_generation(client, client.post(f"/api/application-packages/{package['application_package_id']}/cover-letter-draft")).json()
    check = client.post(f"/api/application-packages/{package['application_package_id']}/truthfulness-check")

    assert "Jane Candidate" in resume["content_preview"]
    assert sample_email("jane.candidate") in resume["content_preview"]
    assert "Calgary, AB, Canada" in resume["content_preview"]
    assert "https://www.linkedin.com/in/jane-candidate" in resume["content_preview"]
    assert "https://github.com/jane-candidate" in resume["content_preview"]
    assert resume["content_preview"].count("https://www.linkedin.com/in/jane-candidate") == 1
    assert resume["content_preview"].count("https://github.com/jane-candidate") == 1
    assert "https://portfolio.example.invalid/jane" in resume["content_preview"]
    assert "Jane Candidate" in cover["content_preview"]
    assert check.status_code == 200
    assert check.json()["status"] == "passed"


def test_truthfulness_blocks_unsaved_contact_leaks(tmp_path: Path):
    client, settings = make_client(tmp_path)
    client.post("/api/profile", json=profile_payload())
    job = client.post(
        "/api/jobs/ingest",
        json={"source_type": "paste", "raw_text": "Backend Developer\nPython APIs", "source_url": None},
    ).json()
    application = client.post(f"/api/jobs/{job['id']}/applications").json()
    package = finish_generation(client, client.post(f"/api/applications/{application['application_id']}/packages")).json()
    document = client.post(f"/api/application-packages/{package['application_package_id']}/resume-draft").json()
    with session_scope(settings) as db:
        document_row = db.get(StoredArtifact, document["artifact_id"])
        assert document_row is not None
    with session_scope(settings) as db:
        from jobcockpit.db.models import GeneratedDocument

        generated = db.get(GeneratedDocument, document["document_id"])
        generated.content_preview = f"Contact me at {sample_email('someone.else')}"

    check = client.post(f"/api/application-packages/{package['application_package_id']}/truthfulness-check")

    assert check.status_code == 200
    assert check.json()["status"] == "blocked"
    assert "private_data_leak" in check.json()["blocking_errors"]


def _docx_bytes(paragraphs: list[str]) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def sample_email(local_part: str) -> str:
    return local_part + "@example.invalid"


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT\n/F1 18 Tf\n72 720 Td\n({text}) Tj\nET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_offset = sum(len(part) for part in parts)
    parts.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    parts.append(("0" * 10 + " 65535 f \n").encode("ascii"))
    for offset in offsets[1:]:
        parts.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    parts.append(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return b"".join(parts)
