from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from jobcockpit.utils.ids import new_id


class StoredArtifactRecord(BaseModel):
    artifact_id: str
    candidate_id: str | None = None
    application_id: str | None = None
    job_id: str | None = None
    artifact_type: str
    storage_backend: str = "local"
    storage_uri: str
    original_file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    privacy_class: str
    source_kind: str | None = None


ARTIFACT_DIRS = {
    "candidate_document": "candidate-documents",
    "generated_document": "generated-documents",
    "job_snapshot": "job-snapshots",
    "job_source_archive": "job-source-archives",
    "email_snapshot": "email-snapshots",
    "extension_download": "extension-downloads",
}


class ArtifactStorageService:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_bytes(
        self,
        *,
        artifact_type: str,
        content: bytes,
        original_file_name: str,
        content_type: str,
        candidate_id: str | None = None,
        application_id: str | None = None,
        job_id: str | None = None,
        privacy_class: str,
        source_kind: str | None = None,
    ) -> StoredArtifactRecord:
        artifact_id = new_id("artifact")
        digest = hashlib.sha256(content).hexdigest()
        directory = ARTIFACT_DIRS.get(artifact_type, "misc")
        target_dir = self.root / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = original_file_name.replace("\\", "_").replace("/", "_")
        target = target_dir / f"{artifact_id}-{safe_name}"
        target.write_bytes(content)
        return StoredArtifactRecord(
            artifact_id=artifact_id,
            candidate_id=candidate_id,
            application_id=application_id,
            job_id=job_id,
            artifact_type=artifact_type,
            storage_uri=f"local://{directory}/{target.name}",
            original_file_name=original_file_name,
            content_type=content_type,
            size_bytes=len(content),
            sha256=digest,
            privacy_class=privacy_class,
            source_kind=source_kind,
        )

    def read_bytes(self, storage_uri: str) -> bytes:
        return self._path_for_uri(storage_uri).read_bytes()

    def delete(self, storage_uri: str) -> None:
        path = self._path_for_uri(storage_uri)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _path_for_uri(self, storage_uri: str) -> Path:
        prefix = "local://"
        if not storage_uri.startswith(prefix):
            raise ValueError("Only local storage URIs are supported")
        relative = storage_uri.removeprefix(prefix)
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Artifact path escapes storage root")
        return path


class ArtifactStorageWriteTracker:
    """Track artifact writes so one failed ingestion item can compensate them."""

    def __init__(self, storage: ArtifactStorageService) -> None:
        self.storage = storage
        self.storage_uris: list[str] = []

    @property
    def root(self) -> Path:
        return self.storage.root

    def write_bytes(self, **kwargs) -> StoredArtifactRecord:
        record = self.storage.write_bytes(**kwargs)
        self.storage_uris.append(record.storage_uri)
        return record

    def read_bytes(self, storage_uri: str) -> bytes:
        return self.storage.read_bytes(storage_uri)

    def delete(self, storage_uri: str) -> None:
        self.storage.delete(storage_uri)

    def rollback(self) -> None:
        for storage_uri in reversed(self.storage_uris):
            self.storage.delete(storage_uri)
        self.storage_uris.clear()
