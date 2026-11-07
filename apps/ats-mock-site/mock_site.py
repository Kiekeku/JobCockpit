from __future__ import annotations

from html import escape
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="JobCockpit ATS Mock")

JOBS = [
    {
        "title": "Software Developer Intern",
        "externalPath": "calgary-ab-software-developer-intern-jr-0001",
        "bulletFields": [{"fieldName": "Location", "text": "Calgary, AB"}],
        "source_job_id": "JR-0001",
    },
    {
        "title": "Data Engineer",
        "externalPath": "remote-canada-data-engineer-jr-0002",
        "bulletFields": [{"fieldName": "Location", "text": "Remote Canada"}],
        "source_job_id": "JR-0002",
    },
]

PROVIDERS = {
    "generic": "Generic ATS",
    "oracle": "Oracle-style ATS",
}


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    provider_links = "".join(
        f'<li><a href="/ats/{_path_segment(provider)}/acme">{_html(label)} - Acme</a></li>'
        for provider, label in PROVIDERS.items()
    )
    return f"<h1>JobCockpit ATS Mock</h1><ul>{provider_links}</ul>"


@app.get("/ats/{provider}/{tenant}", response_class=HTMLResponse)
def listing_page(provider: str, tenant: str) -> str:
    provider_label = PROVIDERS.get(provider, "Unknown ATS")
    provider_path = _path_segment(provider)
    tenant_path = _path_segment(tenant)
    links = "".join(
        (
            f'<li><a href="/ats/{provider_path}/{tenant_path}/jobs/{_path_segment(job["externalPath"])}">'
            f'{_html(job["title"])}</a></li>'
        )
        for job in _tenant_jobs(provider, tenant)
    )
    return f"<h1>{_html(provider_label)} Careers - {_html(tenant.title())}</h1><ul>{links}</ul>"


@app.post("/ats/{provider}/{tenant}/api/jobs")
def search_jobs(provider: str, tenant: str, limit: int = 20, offset: int = 0) -> dict:
    jobs = _tenant_jobs(provider, tenant)
    return {
        "provider": provider,
        "tenant": tenant,
        "total": len(jobs),
        "jobPostings": jobs[offset : offset + limit],
    }


@app.get("/ats/{provider}/{tenant}/jobs/{path:path}")
def job_detail(provider: str, tenant: str, path: str) -> dict:
    for job in _tenant_jobs(provider, tenant):
        if job["externalPath"] == path or job["source_job_id"].lower() == path.lower():
            return {
                **job,
                "provider": provider,
                "tenant": tenant,
                "description": "Build Python and React services in a review-first application flow.",
                "applyUrl": f"http://localhost:8001/apply/native-file-input?job={job['source_job_id']}",
            }
    return {"error": "not_found", "provider": provider, "tenant": tenant}


@app.get("/apply/native-file-input", response_class=HTMLResponse)
def native_file_input() -> str:
    return """
    <h1>Apply to Acme</h1>
    <form id="application-form">
      <label for="firstName">First name</label>
      <input id="firstName" name="firstName" />
      <label for="email">Email address</label>
      <input id="email" name="email" type="email" />
      <label for="streetAddress">Street address</label>
      <input id="streetAddress" name="streetAddress" autocomplete="address-line1" />
      <label for="city">City</label>
      <input id="city" name="city" autocomplete="address-level2" />
      <label for="province">Province</label>
      <input id="province" name="province" autocomplete="address-level1" />
      <label for="postalCode">Postal code</label>
      <input id="postalCode" name="postalCode" autocomplete="postal-code" />
      <label for="country">Country</label>
      <input id="country" name="country" autocomplete="country-name" />
      <label for="linkedinUrl">LinkedIn URL</label>
      <input id="linkedinUrl" name="linkedinUrl" />
      <label for="githubUrl">GitHub URL</label>
      <input id="githubUrl" name="githubUrl" />
      <label for="resume">Upload resume</label>
      <input id="resume" name="resume" type="file" accept=".pdf,.doc,.docx,.md" />
      <button type="submit">Submit application</button>
    </form>
    <script>
      window.__submitTriggered = false;
      document.getElementById('application-form').addEventListener('submit', function(event) {
        window.__submitTriggered = true;
        event.preventDefault();
      });
    </script>
    """


@app.get("/apply/drop-zone", response_class=HTMLResponse)
def drop_zone() -> str:
    return """
    <h1>Upload documents</h1>
    <div data-upload-target="resume" style="border:1px dashed #176a4d;padding:24px">Drop resume here</div>
    <button>Submit</button>
    """


def _tenant_jobs(provider: str, tenant: str) -> list[dict]:
    if provider not in PROVIDERS or tenant != "acme":
        return []
    return JOBS


def _html(value: object) -> str:
    return escape(str(value), quote=True)


def _path_segment(value: object) -> str:
    return quote(str(value), safe="")
