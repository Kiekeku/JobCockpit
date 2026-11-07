"use client";

import { FormEvent, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "../lib/api";
import { AtsProviderCatalog, PortalForm, portalFormForProvider, portalRequestBody } from "../lib/ats-provider-defaults";

type ApiJobAlertSource = {
  job_alert_source_id: string;
  provider: string;
  display_name: string;
  gmail_query: string;
  sender_allowlist: string[];
  enabled: boolean;
  skip_interest_filter: boolean;
  skip_language_filter: boolean;
};

type ApiIngestionRun = {
  ingestion_run_id: string;
  provider: string;
  source_url: string | null;
  search_text: string | null;
  status: string;
  alert_level: string;
  jobs_seen_count: number;
  jobs_created_count: number;
  jobs_duplicate_count: number;
  jobs_skipped_count: number;
  jobs_language_skipped_count: number;
  error_message: string | null;
  started_at: string;
};

type ApiUnresolvedJobAlertLink = {
  job_alert_link_id: string;
  job_alert_source_id: string;
  source_display_name: string;
  email_subject: string;
  received_at: string;
  link_text: string;
  original_url: string;
  resolved_url: string;
  canonical_url: string;
  classification: string;
  ingestion_status: string;
  error_message: string;
  updated_at: string;
};

type ApiJobAlertLinkRetryResult = {
  job_alert_link_id: string;
  ingestion_status: string;
  linked_job_id: string | null;
  created: boolean;
  error_message: string;
};

type SourceForm = {
  display_name: string;
  gmail_query: string;
  sender_allowlist: string;
  enabled: boolean;
  skip_interest_filter: boolean;
  skip_language_filter: boolean;
};

type PreviewLink = {
  classification?: string;
};

type PreviewMessage = {
  message_review_id: string;
  subject: string;
  processing_status: string;
  links: PreviewLink[];
};

type PreviewPayload = {
  messages: PreviewMessage[];
  mailbox_mutations: string[];
};

type ImportPayload = {
  email_ingestion_run_id: string;
  processed_messages: Array<{ message_review_id: string; processing_status: string }>;
  jobs_created_count: number;
  mailbox_mutations: string[];
};

const EMPTY_FORM: SourceForm = {
  display_name: "",
  gmail_query: "",
  sender_allowlist: "",
  enabled: true,
  skip_interest_filter: false,
  skip_language_filter: false
};

function sourceToForm(source: ApiJobAlertSource): SourceForm {
  return {
    display_name: source.display_name,
    gmail_query: source.gmail_query,
    sender_allowlist: source.sender_allowlist.join("\n"),
    enabled: source.enabled,
    skip_interest_filter: source.skip_interest_filter,
    skip_language_filter: source.skip_language_filter
  };
}

function sourceRequestBody(form: SourceForm) {
  return {
    display_name: form.display_name,
    gmail_query: form.gmail_query,
    sender_allowlist: form.sender_allowlist
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean),
    enabled: form.enabled,
    skip_interest_filter: form.skip_interest_filter,
    skip_language_filter: form.skip_language_filter
  };
}

function previewIds(preview: PreviewPayload | undefined): string[] {
  return preview?.messages.map((message) => message.message_review_id) ?? [];
}

function formatRunTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function urlHost(value: string): string {
  try {
    return new URL(value).hostname;
  } catch {
    return value;
  }
}

function ExternalUrlLink({ value, label }: { value: string; label?: string }) {
  if (!value) {
    return <span>Not recorded</span>;
  }
  try {
    const url = new URL(value);
    return (
      <a className="metadataUrlLink" href={url.toString()} rel="noreferrer" target="_blank">
        {label ?? value}
      </a>
    );
  } catch {
    return <span>{value}</span>;
  }
}

export default function SourcesPage() {
  const queryClient = useQueryClient();
  const sourcesQuery = useQuery({
    queryKey: ["job-alert-sources"],
    queryFn: () => fetchJson<ApiJobAlertSource[]>("/api/job-alert-sources")
  });
  const providersQuery = useQuery({
    queryKey: ["ats-provider-defaults"],
    queryFn: () => fetchJson<AtsProviderCatalog>("/api/ats/provider-defaults")
  });
  const runsQuery = useQuery({
    queryKey: ["ingestion-runs"],
    queryFn: () => fetchJson<ApiIngestionRun[]>("/api/ingestion-runs")
  });
  const unresolvedLinksQuery = useQuery({
    queryKey: ["unresolved-job-alert-links"],
    queryFn: () => fetchJson<ApiUnresolvedJobAlertLink[]>("/api/job-alert-links/unresolved")
  });
  const [createForm, setCreateForm] = useState<SourceForm>(EMPTY_FORM);
  const [portalForm, setPortalForm] = useState<PortalForm | null>(null);
  const [sourceForms, setSourceForms] = useState<Record<string, SourceForm>>({});
  const [statusBySource, setStatusBySource] = useState<Record<string, string>>({});
  const [previewBySource, setPreviewBySource] = useState<Record<string, PreviewPayload>>({});
  const [retryStatusByLink, setRetryStatusByLink] = useState<Record<string, string>>({});

  async function refreshIngestionCaches() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["jobs"] }),
      queryClient.invalidateQueries({ queryKey: ["applications"] }),
      queryClient.invalidateQueries({ queryKey: ["ingestion-runs"] }),
      queryClient.invalidateQueries({ queryKey: ["unresolved-job-alert-links"] })
    ]);
  }
  const [createStatus, setCreateStatus] = useState("");
  const [portalStatus, setPortalStatus] = useState("");

  useEffect(() => {
    const catalog = providersQuery.data;
    if (catalog) {
      setPortalForm((current) => current ?? portalFormForProvider(catalog, "workday"));
    }
  }, [providersQuery.data]);

  useEffect(() => {
    const nextForms: Record<string, SourceForm> = {};
    for (const source of sourcesQuery.data ?? []) {
      nextForms[source.job_alert_source_id] = sourceToForm(source);
    }
    setSourceForms(nextForms);
  }, [sourcesQuery.data]);

  function setCreateField<Field extends keyof SourceForm>(field: Field, value: SourceForm[Field]) {
    setCreateForm((current) => ({ ...current, [field]: value }));
  }

  function setPortalField<Field extends keyof PortalForm>(field: Field, value: PortalForm[Field]) {
    setPortalForm((current) => current ? { ...current, [field]: value } : null);
  }

  function setPortalProvider(provider: string) {
    const nextForm = portalFormForProvider(providersQuery.data ?? {}, provider);
    if (nextForm) setPortalForm(nextForm);
  }

  function setSourceField<Field extends keyof SourceForm>(
    sourceId: string,
    field: Field,
    value: SourceForm[Field]
  ) {
    setSourceForms((current) => ({
      ...current,
      [sourceId]: {
        ...current[sourceId],
        [field]: value
      }
    }));
  }

  async function createSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateStatus("Creating source...");
    const response = await fetch("/api/job-alert-sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sourceRequestBody(createForm))
    });
    if (response.ok) {
      setCreateForm(EMPTY_FORM);
      setCreateStatus("Source created.");
      await sourcesQuery.refetch();
    } else {
      setCreateStatus(`Create failed: ${response.status}`);
    }
  }

  async function saveSource(sourceId: string) {
    const form = sourceForms[sourceId];
    if (!form) {
      return;
    }
    setStatusBySource((current) => ({ ...current, [sourceId]: "Saving source..." }));
    const response = await fetch(`/api/job-alert-sources/${sourceId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sourceRequestBody(form))
    });
    if (response.ok) {
      setStatusBySource((current) => ({ ...current, [sourceId]: "Source saved." }));
      await sourcesQuery.refetch();
    } else {
      setStatusBySource((current) => ({ ...current, [sourceId]: `Save failed: ${response.status}` }));
    }
  }

  async function deleteSource(sourceId: string) {
    setStatusBySource((current) => ({ ...current, [sourceId]: "Deleting source..." }));
    const response = await fetch(`/api/job-alert-sources/${sourceId}`, {
      method: "DELETE"
    });
    if (response.ok) {
      setStatusBySource((current) => {
        const next = { ...current };
        delete next[sourceId];
        return next;
      });
      setPreviewBySource((current) => {
        const next = { ...current };
        delete next[sourceId];
        return next;
      });
      await sourcesQuery.refetch();
    } else {
      setStatusBySource((current) => ({ ...current, [sourceId]: `Delete failed: ${response.status}` }));
    }
  }

  async function previewSource(sourceId: string) {
    setStatusBySource((current) => ({ ...current, [sourceId]: "Previewing source..." }));
    const response = await fetch("/api/job-alerts/gmail/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId })
    });
    if (response.ok) {
      const payload = (await response.json()) as PreviewPayload;
      setPreviewBySource((current) => ({ ...current, [sourceId]: payload }));
      setStatusBySource((current) => ({ ...current, [sourceId]: `${payload.messages.length} messages previewed.` }));
    } else {
      setStatusBySource((current) => ({ ...current, [sourceId]: `Preview failed: ${response.status}` }));
    }
  }

  async function importPreviewed(sourceId: string) {
    const approvedIds = previewIds(previewBySource[sourceId]);
    if (approvedIds.length === 0) {
      setStatusBySource((current) => ({ ...current, [sourceId]: "Preview first." }));
      return;
    }
    setStatusBySource((current) => ({ ...current, [sourceId]: "Importing messages..." }));
    const response = await fetch("/api/job-alerts/gmail/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, approved_message_review_ids: approvedIds })
    });
    if (response.ok) {
      const payload = (await response.json()) as ImportPayload;
      setStatusBySource((current) => ({
        ...current,
        [sourceId]: `${payload.processed_messages.length} messages imported; ${payload.jobs_created_count} jobs created.`
      }));
      await refreshIngestionCaches();
      await runsQuery.refetch();
      await unresolvedLinksQuery.refetch();
    } else {
      setStatusBySource((current) => ({ ...current, [sourceId]: `Import failed: ${response.status}` }));
    }
  }

  async function retryUnresolvedLink(linkId: string) {
    setRetryStatusByLink((current) => ({ ...current, [linkId]: "Retrying..." }));
    const response = await fetch(`/api/job-alert-links/${linkId}/retry`, {
      method: "POST"
    });
    if (response.ok) {
      const payload = (await response.json()) as ApiJobAlertLinkRetryResult;
      setRetryStatusByLink((current) => ({
        ...current,
        [linkId]:
          payload.ingestion_status === "imported"
            ? payload.created
              ? "Imported as new opportunity."
              : "Linked to existing opportunity."
            : payload.error_message || payload.ingestion_status
      }));
      await refreshIngestionCaches();
      await unresolvedLinksQuery.refetch();
    } else {
      setRetryStatusByLink((current) => ({ ...current, [linkId]: `Retry failed: ${response.status}` }));
    }
  }

  async function importPortal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!portalForm || !providersQuery.data?.[portalForm.provider] || providersQuery.isError) return;
    setPortalStatus("Importing portal...");
    const response = await fetch("/api/jobs/ingest/ats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(portalRequestBody(portalForm))
    });
    if (response.ok) {
      const payload = (await response.json()) as {
        jobs_seen_count: number;
        jobs_created_count: number;
        jobs_duplicate_count: number;
      };
      setPortalStatus(
        `${payload.jobs_seen_count} jobs seen; ${payload.jobs_created_count} created; ${payload.jobs_duplicate_count} duplicates.`
      );
      await refreshIngestionCaches();
      await runsQuery.refetch();
    } else {
      setPortalStatus(`Portal import failed: ${response.status}`);
    }
  }

  const sources = sourcesQuery.data ?? [];
  const providers = Object.keys(providersQuery.data ?? {}).sort();
  const runs = runsQuery.data ?? [];
  const unresolvedLinks = unresolvedLinksQuery.data ?? [];

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">JobCockpit</div>
        <nav>
          <a href="/">Dashboard</a>
          <a href="/profile">Profile</a>
          <a className="active" href="/sources">Sources</a>
        </nav>
      </aside>
      <section className="workspace sourceWorkspace">
        <header className="topbar">
          <div>
            <h1>Ingestion sources</h1>
            <p>Saved alerts and portal imports.</p>
          </div>
          <a className="secondaryAction" href="/">Dashboard</a>
        </header>

        <section className="sourceGrid">
          <div className="panel">
            <div className="panelHeader">
              <h2>New Gmail source</h2>
              <span>Read-only mailbox access</span>
            </div>
            <form className="sourceForm" onSubmit={createSource}>
              <label>
                Name
                <input
                  value={createForm.display_name}
                  onChange={(event) => setCreateField("display_name", event.target.value)}
                />
              </label>
              <label>
                Gmail query
                <textarea
                  value={createForm.gmail_query}
                  onChange={(event) => setCreateField("gmail_query", event.target.value)}
                />
              </label>
              <label>
                Sender allowlist
                <textarea
                  value={createForm.sender_allowlist}
                  onChange={(event) => setCreateField("sender_allowlist", event.target.value)}
                />
              </label>
              <label className="checkControl">
                <input
                  checked={createForm.enabled}
                  onChange={(event) => setCreateField("enabled", event.target.checked)}
                  type="checkbox"
                />
                Enabled
              </label>
              <label className="checkRow">
                <input
                  checked={createForm.skip_interest_filter}
                  onChange={(event) => setCreateField("skip_interest_filter", event.target.checked)}
                  type="checkbox"
                />
                Skip role-interest filter
              </label>
              <label className="checkRow">
                <input
                  checked={createForm.skip_language_filter}
                  onChange={(event) => setCreateField("skip_language_filter", event.target.checked)}
                  type="checkbox"
                />
                Skip language filter
              </label>
              <button type="submit">Create source</button>
              <span className="formStatus">{createStatus}</span>
            </form>
          </div>

          <div className="panel wide">
            <div className="panelHeader">
              <h2>Configured sources</h2>
              <span>{sources.length} sources</span>
            </div>
            {sourcesQuery.isLoading ? (
              <div className="emptyState">Loading sources...</div>
            ) : sourcesQuery.isError ? (
              <div className="errorState">Sources are unavailable.</div>
            ) : sources.length === 0 ? (
              <div className="emptyState">No sources configured.</div>
            ) : (
              <ul className="sourceList">
                {sources.map((source) => {
                  const form = sourceForms[source.job_alert_source_id] ?? sourceToForm(source);
                  const preview = previewBySource[source.job_alert_source_id];
                  return (
                    <li key={source.job_alert_source_id}>
                      <div className="sourceHeader">
                        <div>
                          <strong>{source.display_name}</strong>
                          <small>{source.provider} - {source.enabled ? "enabled" : "paused"}</small>
                        </div>
                        <span className={`runStatus ${source.enabled ? "" : "paused"}`}>
                          {source.enabled ? "Enabled" : "Paused"}
                        </span>
                      </div>
                      <div className="sourceEditor">
                        <label>
                          Name
                          <input
                            value={form.display_name}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "display_name", event.target.value)
                            }
                          />
                        </label>
                        <label>
                          Gmail query
                          <textarea
                            value={form.gmail_query}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "gmail_query", event.target.value)
                            }
                          />
                        </label>
                        <label>
                          Sender allowlist
                          <textarea
                            value={form.sender_allowlist}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "sender_allowlist", event.target.value)
                            }
                          />
                        </label>
                      </div>
                      <div className="sourceActions">
                        <label className="checkControl">
                          <input
                            checked={form.enabled}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "enabled", event.target.checked)
                            }
                            type="checkbox"
                          />
                          Enabled
                        </label>
                        <label className="checkRow">
                          <input
                            checked={form.skip_interest_filter}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "skip_interest_filter", event.target.checked)
                            }
                            type="checkbox"
                          />
                          Skip role-interest filter
                        </label>
                        <label className="checkRow">
                          <input
                            checked={form.skip_language_filter}
                            onChange={(event) =>
                              setSourceField(source.job_alert_source_id, "skip_language_filter", event.target.checked)
                            }
                            type="checkbox"
                          />
                          Skip language filter
                        </label>
                        <button onClick={() => void saveSource(source.job_alert_source_id)} type="button">Save</button>
                        <button onClick={() => void previewSource(source.job_alert_source_id)} type="button">Preview</button>
                        <button onClick={() => void importPreviewed(source.job_alert_source_id)} type="button">
                          Import previewed
                        </button>
                        <button onClick={() => void deleteSource(source.job_alert_source_id)} type="button">
                          Delete
                        </button>
                        <span className="formStatus">{statusBySource[source.job_alert_source_id]}</span>
                      </div>
                      {preview ? (
                        <ul className="previewList">
                          {preview.messages.map((message) => (
                            <li key={message.message_review_id}>
                              <strong>{message.subject}</strong>
                              <span>{message.processing_status}</span>
                              <span>{message.links.length} links</span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="panel wide">
            <div className="panelHeader">
              <h2>Unresolved job links</h2>
              <span>{unresolvedLinks.length} links</span>
            </div>
            {unresolvedLinksQuery.isLoading ? (
              <div className="emptyState">Loading unresolved links...</div>
            ) : unresolvedLinksQuery.isError ? (
              <div className="errorState">Unresolved links are unavailable.</div>
            ) : unresolvedLinks.length === 0 ? (
              <div className="emptyState">No unresolved job links.</div>
            ) : (
              <ul className="sourceList unresolvedList">
                {unresolvedLinks.map((link) => (
                  <li key={link.job_alert_link_id}>
                    <div className="sourceHeader">
                      <div>
                        <strong>{link.link_text || "Untitled link"}</strong>
                        <small>{link.source_display_name} - {link.email_subject}</small>
                      </div>
                      <span className="runStatus paused">Needs resolution</span>
                    </div>
                    <div className="sourceActions">
                      <a href={link.canonical_url} rel="noreferrer" target="_blank">
                        Open {urlHost(link.canonical_url)}
                      </a>
                      {link.resolved_url !== link.canonical_url ? (
                        <a href={link.resolved_url} rel="noreferrer" target="_blank">
                          Open resolved
                        </a>
                      ) : null}
                      {link.original_url !== link.canonical_url && link.original_url !== link.resolved_url ? (
                        <a href={link.original_url} rel="noreferrer" target="_blank">
                          Open original
                        </a>
                      ) : null}
                      <button type="button" onClick={() => retryUnresolvedLink(link.job_alert_link_id)}>
                        Retry
                      </button>
                      <span>{formatRunTime(link.updated_at)}</span>
                      {retryStatusByLink[link.job_alert_link_id] ? (
                        <span>{retryStatusByLink[link.job_alert_link_id]}</span>
                      ) : null}
                    </div>
                    <dl className="unresolvedMeta">
                      <div>
                        <dt>Status</dt>
                        <dd>{link.ingestion_status}</dd>
                      </div>
                      <div>
                        <dt>Error</dt>
                        <dd>{link.error_message || "No resolver details recorded."}</dd>
                      </div>
                      <div>
                        <dt>Posting URL</dt>
                        <dd><ExternalUrlLink value={link.canonical_url} /></dd>
                      </div>
                      {link.resolved_url !== link.canonical_url ? (
                        <div>
                          <dt>Resolved URL</dt>
                          <dd><ExternalUrlLink value={link.resolved_url} /></dd>
                        </div>
                      ) : null}
                      {link.original_url !== link.canonical_url && link.original_url !== link.resolved_url ? (
                        <div>
                          <dt>Original URL</dt>
                          <dd><ExternalUrlLink value={link.original_url} /></dd>
                        </div>
                      ) : null}
                    </dl>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="panel">
            <div className="panelHeader">
              <h2>Job portal import</h2>
              <span>{providers.length} providers</span>
            </div>
            {providersQuery.isError ? (
              <div className="errorState">
                Portal providers are unavailable.
                <button type="button" onClick={() => providersQuery.refetch()}>Retry</button>
              </div>
            ) : providersQuery.isPending ? (
              <div className="emptyState">Loading providers...</div>
            ) : !portalForm || !providers.includes(portalForm.provider) ? (
              <div className="emptyState">No portal providers available.</div>
            ) : (
            <form className="sourceForm" onSubmit={importPortal}>
              <label>
                Provider
                <select
                  value={portalForm.provider}
                  onChange={(event) => setPortalProvider(event.target.value)}
                >
                  {providers.map((provider) => (
                    <option key={provider} value={provider}>{provider}</option>
                  ))}
                </select>
              </label>
              <label>
                Portal URL
                <input
                  value={portalForm.source_url}
                  onChange={(event) => setPortalField("source_url", event.target.value)}
                />
              </label>
              <label>
                Search text
                <input
                  value={portalForm.search_text}
                  onChange={(event) => setPortalField("search_text", event.target.value)}
                />
              </label>
              <label>
                Link limit
                <input
                  min="1"
                  max={portalForm.provider === "csjobs" ? 75 : 50}
                  type="number"
                  value={portalForm.limit}
                  onChange={(event) => setPortalField("limit", Number(event.target.value))}
                />
              </label>
              <label className="checkRow">
                <input
                  checked={portalForm.skip_interest_filter}
                  onChange={(event) => setPortalField("skip_interest_filter", event.target.checked)}
                  type="checkbox"
                />
                Skip role-interest filter for this import
              </label>
              <label className="checkRow">
                <input
                  checked={portalForm.skip_language_filter}
                  onChange={(event) => setPortalField("skip_language_filter", event.target.checked)}
                  type="checkbox"
                />
                Skip language filter for this import
              </label>
              <button type="submit">Import portal</button>
              <span className="formStatus">{portalStatus}</span>
            </form>
            )}
          </div>

          <div className="panel wide">
            <div className="panelHeader">
              <h2>Recent portal runs</h2>
              <span>{runs.length} runs</span>
            </div>
            {runsQuery.isLoading ? (
              <div className="emptyState">Loading runs...</div>
            ) : runsQuery.isError ? (
              <div className="errorState">Runs are unavailable.</div>
            ) : runs.length === 0 ? (
              <div className="emptyState">No portal runs recorded.</div>
            ) : (
              <ul className="sourceList">
                {runs.map((run) => (
                  <li key={run.ingestion_run_id}>
                    <div className="sourceHeader">
                      <div>
                        <strong>{run.provider}</strong>
                        <small>{run.source_url ?? "No source URL"}</small>
                      </div>
                      <span
                        className={`runStatus ${
                          run.alert_level === "error"
                            ? "paused"
                            : run.alert_level === "warning" ? "warning" : ""
                        }`}
                      >
                        {run.status}
                      </span>
                    </div>
                    <div className="sourceActions">
                      <span>{run.jobs_seen_count} seen</span>
                      <span>{run.jobs_created_count} created</span>
                      <span>{run.jobs_duplicate_count} duplicates</span>
                      <span>{run.jobs_skipped_count} skipped</span>
                      <span>{run.jobs_language_skipped_count} language skipped</span>
                      <span>{formatRunTime(run.started_at)}</span>
                    </div>
                    {run.error_message ? <div className="errorState">{run.error_message}</div> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </section>
    </main>
  );
}
