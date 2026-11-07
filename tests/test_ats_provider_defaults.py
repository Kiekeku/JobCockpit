from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess

import pytest

from jobcockpit import cli
from jobcockpit.ingestion.ats_defaults import ats_provider_defaults
from jobcockpit.schemas.requests import AtsIngestRequest


CLI_PROVIDERS = [
    ("ingest-hanzilla", "hanzilla"),
    ("ingest-eluta", "eluta"),
    ("ingest-csjobs", "csjobs"),
    ("ingest-rankalpha", "rankalpha_jobs"),
    ("ingest-canadian-tech", "canadian_tech_internships"),
    ("ingest-canadian-tech-2027", "canadian_tech_internships_2027"),
    ("ingest-canada-tech-2027", "canada_tech_internships_summer_2027"),
]
DEFAULT_KEYS = {"source_url", "search_text", "limit", "skip_interest_filter", "skip_language_filter"}


@pytest.fixture
def provider_catalog(jobcockpit_settings_factory, jobcockpit_client_factory):
    client = jobcockpit_client_factory(jobcockpit_settings_factory())
    response = client.get("/api/ats/provider-defaults")
    assert response.status_code == 200
    catalog = response.json()
    assert sorted(catalog) == client.get("/api/ats/providers").json()
    assert len(catalog) == 10
    for defaults in catalog.values():
        assert set(defaults) == DEFAULT_KEYS
        assert isinstance(defaults["source_url"], str)
        assert isinstance(defaults["search_text"], str)
        assert type(defaults["limit"]) is int
        assert type(defaults["skip_interest_filter"]) is bool
        assert type(defaults["skip_language_filter"]) is bool
    return catalog


@pytest.mark.parametrize("command,provider", CLI_PROVIDERS)
def test_cli_request_uses_api_catalog_defaults(provider_catalog, monkeypatch, capsys, command, provider):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jobs_seen_count": 0, "jobs_created_count": 0, "jobs_duplicate_count": 0}

    def post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.delenv("JOBCOCKPIT_API_BASE_URL", raising=False)
    monkeypatch.delenv("JOBCOCKPIT_ATS_INGEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(cli.httpx, "post", post)
    assert cli.main(["ats", command, "--json"]) == 0
    assert calls == [(
        "http://127.0.0.1:8000/api/jobs/ingest/ats",
        {"provider": provider, **provider_catalog[provider]}, 14400,
    )]
    assert json.loads(capsys.readouterr().out)["provider"] == provider


def test_cli_explicit_options_override_catalog_and_environment(monkeypatch, capsys):
    monkeypatch.setenv("JOBCOCKPIT_API_BASE_URL", "http://localhost:9000")
    monkeypatch.setenv("JOBCOCKPIT_ATS_INGEST_TIMEOUT_SECONDS", "45")
    args = cli.build_parser().parse_args([
        "ats", "ingest-hanzilla", "--source-url", "https://board.example.invalid/jobs",
        "--search-text", "data", "--limit", "7", "--skip-interest-filter", "--skip-language-filter",
        "--api-base-url", "http://localhost:9001", "--timeout-seconds", "60",
    ])
    assert args.source_url == "https://board.example.invalid/jobs"
    assert args.search_text == "data" and args.limit == 7
    assert args.skip_interest_filter and args.skip_language_filter
    assert args.api_base_url == "http://localhost:9001" and args.timeout_seconds == 60
    args = cli.build_parser().parse_args(["ats", "ingest-hanzilla"])
    assert args.api_base_url == "http://localhost:9000" and args.timeout_seconds == 45
    assert args.source_url == ats_provider_defaults("hanzilla").source_url


def test_employer_specific_defaults_require_url_and_preserve_api_request_limits(provider_catalog):
    generic = {"source_url": "", "search_text": "", "limit": 25,
               "skip_interest_filter": False, "skip_language_filter": False}
    for provider in ["workday", "oracle_candidate_experience", "phenom_oracle"]:
        assert provider_catalog[provider] == generic
    request = AtsIngestRequest(provider="workday", source_url="https://employer.example.invalid/jobs")
    assert request.limit == 10
    with pytest.raises(FrozenInstanceError):
        ats_provider_defaults("csjobs").limit = 1


def test_web_provider_switches_and_overrides_match_api_catalog(provider_catalog):
    runner = """
      const fs = require("fs");
      const assert = require("node:assert/strict");
      const ts = require("typescript");
      const source = fs.readFileSync(process.argv[1], "utf8");
      const compiled = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
      }).outputText;
      const mod = { exports: {} };
      new Function("exports", "require", "module", compiled)(mod.exports, require, mod);
      const { portalFormForProvider, portalRequestBody } = mod.exports;
      const catalog = JSON.parse(fs.readFileSync(0, "utf8"));
      const before = JSON.stringify(catalog);
      for (const provider of Object.keys(catalog)) {
        const form = portalFormForProvider(catalog, provider);
        assert.deepEqual(portalRequestBody(form), { provider, ...catalog[provider] });
        form.source_url = "https://custom.example.invalid/jobs";
        form.search_text = "data";
        form.limit = 7;
        form.skip_interest_filter = !form.skip_interest_filter;
        form.skip_language_filter = !form.skip_language_filter;
        const overridden = portalRequestBody({ ...form, unexpected: "discard" });
        assert.deepEqual(overridden, form);
        assert.equal(portalFormForProvider(catalog, provider).limit, catalog[provider].limit);
      }
      assert.equal(portalFormForProvider(catalog, "csjobs").limit, 75);
      assert.equal(portalFormForProvider(catalog, "workday").limit, 25);
      assert.equal(portalFormForProvider(catalog, "rankalpha_jobs").limit, 50);
      assert.equal(portalFormForProvider(catalog, "unknown"), null);
      assert.equal(portalFormForProvider({}, "workday"), null);
      assert.equal(JSON.stringify(catalog), before);
    """
    subprocess.run(
        ["node", "--eval", runner, str(Path("apps/web/app/lib/ats-provider-defaults.ts").resolve())],
        input=json.dumps(provider_catalog), text=True, encoding="utf-8", check=True,
    )
