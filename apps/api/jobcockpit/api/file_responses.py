from __future__ import annotations

from urllib.parse import quote, unquote

from jobcockpit.db.models import StoredArtifact
from jobcockpit.ingestion.ats_adapters.base import SourceArchive


def content_type_for_extension(extension: str) -> str:
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
    }.get(extension, "application/octet-stream")


def attachment_headers(original_file_name: str) -> dict[str, str]:
    safe_name = safe_download_filename(original_file_name)
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"}


def inline_headers(original_file_name: str) -> dict[str, str]:
    safe_name = safe_download_filename(original_file_name)
    return {"Content-Disposition": f"inline; filename*=UTF-8''{quote(safe_name, safe='')}"}


def safe_download_filename(original_file_name: str) -> str:
    original_file_name = unquote(original_file_name)
    safe = "".join(
        "_" if character in {'"', "\\", "/", ";"} or ord(character) < 32 else character
        for character in original_file_name
    )
    safe = safe.strip().strip(".")
    return safe or "download"


def source_archive_file_name(archive: SourceArchive) -> str:
    content_type = archive.content_type.split(";", 1)[0].strip().lower()
    extension = {
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "application/json": ".json",
        "text/json": ".json",
        "text/plain": ".txt",
    }.get(content_type, ".bin")
    return f"job-source-archive{extension}"


def source_archive_metadata(archive: SourceArchive, artifact: StoredArtifact) -> dict:
    return {
        "artifact_id": artifact.artifact_id,
        "source_url": archive.source_url,
        "final_url": archive.final_url,
        "status_code": archive.status_code,
        "content_type": archive.content_type,
        "headers": dict(archive.headers),
    }
