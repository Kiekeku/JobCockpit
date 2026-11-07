# Candidate transcripts

Profile → Documents → Transcripts accepts PDFs up to 5 MB after a profile is
created. Upload a new version whenever the candidate receives an updated
transcript. The most recently uploaded version is the default; uploading an older
academic record later also makes it the default. Previous versions remain listed
with upload timestamps and individual download links.

Files must be readable PDFs with at least one page that open without a password.
Scanned PDFs and issuer PDFs with editing restrictions are accepted and stored
unchanged. Transcript content is not parsed into GPA, academic claims, or evidence.

## Storage and selection

Each upload creates a private `candidate_document` through
`ArtifactStorageService`, with `source_kind=profile_transcript_upload`. Existing
`stored_artifacts` columns retain candidate ownership, timestamp, filename, MIME
type, size, SHA-256, and storage URI. No schema migration is needed.

- `POST /api/profile/transcript`: store a new version and return transcript state.
- `GET /api/profile/transcript`: list versions newest first and identify the latest.
- `GET /api/profile`: also includes transcript state.
- `GET /api/profile/transcript/latest/download`: resolve the latest on each request.
- `GET /api/profile/transcript/{artifact_id}/download`: retrieve a specific version.

Selection is candidate-scoped and excludes deleted artifacts and other document
kinds. It orders by server upload timestamp, with artifact ID as a deterministic
tie-breaker. Downloads disable caching. A missing latest file returns an error
instead of silently downloading an older transcript.

## Reference audit

The repository-wide transcript search found these existing reference groups:

| Location | Existing role | Result |
| --- | --- | --- |
| `ingestion/application_documents.py`, `parser_models.py` | Recognize and normalize transcript requirements | No parser change needed |
| `ingestion/codex_posting_parser.py` | Include transcript in allowed required-document values | No parser change needed |
| `ingestion/role_heading_patterns.py`, `role_semantics.py` | Separate application instructions and incidental transcript mentions from qualifications | No parser change needed |
| `ingestion/saved_parser_validation.py` | Select document-related corpus cases | No validator change needed |
| `data/fixtures/parser/golden_cases.json` | Real-source-grounded transcript requirement and role-boundary regression cases | Preserved |
| `tests/test_required_document_parsing.py`, `test_required_document_backfill.py` | Test extracted and backfilled document requirements | Preserved |
| `apps/web/app/jobs/[id]/page.tsx` | Display Transcript in required documents | Now offers latest download or a Profile upload link |

Before this change, Profile supported resumes and cover-letter samples only.
Application document generation handles resumes/cover letters, and there was no
transcript consumer in generation or extension code. This change supplies manual
transcript downloads; it does not attach transcripts to generated packages or
grant extension approval. Existing package approval and download guards continue
to control extension access.
