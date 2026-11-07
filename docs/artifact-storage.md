# Artifact Storage

File bytes are stored under `var/private/storage` and are never stored as large SQLite blobs. Database rows in `stored_artifacts` track storage URI, SHA-256, content type, size, owner IDs, and privacy class.

The local backend is implemented by `ArtifactStorageService`. Future object-storage backends should keep the same database metadata contract.

Job posting ingestion writes two different artifact types when source bytes are available:

- `job_snapshot`: normalized text parsed by JobCockpit and used for duplicate detection, fit analysis, and document generation.
- `job_source_archive`: the fetched source response body from an official posting or ATS detail endpoint, stored for audit/download with source URL, final URL, status code, content type, and a small allowlist of headers in the job source metadata.

Paste-only imports create only `job_snapshot`. URL/ATS imports create `job_source_archive` only when the processing path actually fetched source bytes.
