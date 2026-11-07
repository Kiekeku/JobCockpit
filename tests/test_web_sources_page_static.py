from pathlib import Path


def test_sources_page_manages_gmail_alert_sources():
    page = Path("apps/web/app/sources/page.tsx").read_text(encoding="utf-8")

    assert 'queryKey: ["job-alert-sources"]' in page
    assert 'fetchJson<ApiJobAlertSource[]>("/api/job-alert-sources")' in page
    assert 'fetchJson<ApiUnresolvedJobAlertLink[]>("/api/job-alert-links/unresolved")' in page
    assert 'fetch(`/api/job-alert-links/${linkId}/retry`' in page
    assert 'fetch("/api/job-alert-sources"' in page
    assert 'fetch(`/api/job-alert-sources/${sourceId}`' in page
    assert 'method: "PUT"' in page
    assert 'method: "DELETE"' in page
    assert 'fetch("/api/job-alerts/gmail/preview"' in page
    assert 'fetch("/api/job-alerts/gmail/import"' in page
    assert 'fetchJson<AtsProviderCatalog>("/api/ats/provider-defaults")' in page
    assert 'fetchJson<ApiIngestionRun[]>("/api/ingestion-runs")' in page
    assert 'fetch("/api/jobs/ingest/ats"' in page
    assert "display_name" in page
    assert "gmail_query" in page
    assert "sender_allowlist" in page
    assert "enabled" in page
    assert "Ingestion sources" in page
    assert "Gmail query" in page
    assert "Unresolved job links" in page
    assert "Job portal import" in page
    assert "Recent portal runs" in page
    assert 'run.alert_level === "warning"' in page


def test_sources_page_loads_provider_forms_from_catalog_and_exposes_retry():
    page = Path("apps/web/app/sources/page.tsx").read_text(encoding="utf-8")

    assert 'portalFormForProvider(catalog, "workday")' in page
    assert "portalFormForProvider(providersQuery.data ?? {}, provider)" in page
    assert "setPortalProvider(event.target.value)" in page
    assert "Portal providers are unavailable." in page
    assert "providersQuery.refetch()" in page
    assert "Loading providers..." in page


def test_sources_page_preserves_provider_import_bounds():
    page = Path("apps/web/app/sources/page.tsx").read_text(encoding="utf-8")

    assert 'max={portalForm.provider === "csjobs" ? 75 : 50}' in page


def test_dashboard_labels_canadian_tech_2027_source():
    dashboard = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert (
        'ats_canadian_tech_internships_2027: "Canadian Tech Internships 2027"'
        in dashboard
    )


def test_dashboard_labels_csjobs_source():
    dashboard = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert 'ats_csjobs: "CS Jobs"' in dashboard
