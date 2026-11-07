# Workday Job Ingestion

JobCockpit supports adapter-backed, read-only ATS ingestion. This page covers the Workday provider. See `docs/ats-provider-ingestion.md` for the cross-provider adapter contract and the Phenom/Oracle provider.

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/jobs/ingest/ats `
  -ContentType "application/json" `
  -Body '{"provider":"workday","source_url":"https://suncor.wd1.myworkdayjobs.com/Suncor_External","search_text":"engineer","limit":5}'
```

The generic endpoint dispatches by `provider`. The Workday adapter accepts either a Workday listing URL, such as `https://suncor.wd1.myworkdayjobs.com/Suncor_External`, or a specific Workday job URL under that site. Use `search_text` for targeted searches rather than importing the whole careers site; `limit` caps non-excluded detail links attempted per run and therefore also caps imported jobs. `POST /api/jobs/ingest/workday` remains available as a compatibility wrapper around the `workday` adapter.

Adapter structure:

- `jobcockpit.ingestion.ats_adapters.base` defines `AtsFetchRequest`, `AtsJob`, `AtsProviderAdapter`, and `AtsProviderError`.
- `jobcockpit.ingestion.ats_adapters.registry` maps provider names to adapters.
- `jobcockpit.ingestion.ats_adapters.workday` implements the Workday CXS adapter.
- New providers should implement the base adapter contract and register themselves in `registry.py`.

Implementation behavior:

- Resolves the requested provider through the ATS adapter registry.
- Parses the Workday tenant and site from the public URL.
- Reads the Workday CXS listing endpoint: `/wday/cxs/{tenant}/{site}/jobs`.
- Sends `search_text` as Workday `searchText` and pages through offsets until the detail-attempt budget, Workday `total`, or an empty page stops the import.
- Reads each selected detail endpoint from the returned `externalPath`.
- Converts Workday HTML job descriptions to plain text.
- Persists each job through the normal artifact-backed job ingestion path.
- Stores imported rows with `source_type="workday"`.
- Commits each successful job independently and records a durable per-link ingestion item, so one bad posting does not discard the rest of the run.
- Marks mixed-result runs as `completed_with_warnings`; per-link details remain available from `GET /api/ingestion-runs/{run_id}/items`.

Safety behavior:

- Only `myworkdayjobs.com` URLs are accepted.
- The standard URL fetch policy still blocks unsupported protocols and private-network destinations.
- Imported job URLs are deduplicated by exact snapshot hash and by canonical source URL before new artifacts are written.
- Each run records an ingestion-run audit trail with seen, created, duplicate, status, alert, and error fields.
- The adapter does not apply, submit, authenticate, or mutate any Workday state.
