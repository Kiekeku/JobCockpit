from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from fastapi.testclient import TestClient


def test_ats_mock_listing_escapes_route_segments() -> None:
    module = _load_mock_site_module()
    client = TestClient(module.app)

    response = client.get("/ats/generic/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")

    assert response.status_code == 200
    assert "<img" not in response.text
    assert "&lt;Img Src=X Onerror=Alert(1)&gt;" in response.text


def test_ats_mock_listing_encodes_dynamic_job_links() -> None:
    module = _load_mock_site_module()
    module.JOBS = [
        {
            "title": 'Senior "Platform" <Engineer>',
            "externalPath": 'role "x" <tag>',
            "bulletFields": [],
            "source_job_id": "JR-X",
        }
    ]
    client = TestClient(module.app)

    response = client.get("/ats/generic/acme")

    assert response.status_code == 200
    assert 'href="/ats/generic/acme/jobs/role%20%22x%22%20%3Ctag%3E"' in response.text
    assert 'Senior &quot;Platform&quot; &lt;Engineer&gt;' in response.text
    assert "<Engineer>" not in response.text


def _load_mock_site_module() -> ModuleType:
    module_path = Path("apps/ats-mock-site/mock_site.py")
    spec = importlib.util.spec_from_file_location("ats_mock_site_for_tests", module_path)
    if not spec or not spec.loader:
        raise RuntimeError("Could not load ATS mock site module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
