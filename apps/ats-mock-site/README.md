# JobCockpit ATS Mock Site

This local FastAPI app emulates small ATS job discovery and application surfaces. The routes are provider-aware so adapters such as `generic`, `oracle`, or other ATS styles can be added without changing the rest of JobCockpit.

Run it with:

```bash
uv run uvicorn mock_site:app --app-dir apps/ats-mock-site --port 8001
```

Example pages:

- `http://localhost:8001/ats/generic/acme`
- `http://localhost:8001/ats/generic/acme/jobs/remote-canada-data-engineer-jr-0002`
- `http://localhost:8001/apply/native-file-input`

