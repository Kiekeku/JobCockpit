"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "./lib/api";

type BrowserHealth = {
  enabled: boolean;
  configured_provider: string;
  observed_at: string;
  total: number;
  counts: { running: number; succeeded: number; failed: number; needs_review: number; overdue: number };
  active_issue_counts: { failed: number; needs_review: number; overdue: number };
  dismissed_issue_count: number;
  recovered_failure_count: number;
  last_success_at: string | null;
  incomplete: boolean;
  failure_reasons: { reason: string; count: number }[];
  recent_issues: {
    attempt_id: string; source_url: string; title: string; status: string; reason: string;
    workflow: string; started_at: string; finished_at: string | null; job_id: string | null;
    error_code?: string; signals?: string[];
  }[];
};

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "None recorded in the last 7 days";
}

export function BrowserHealthPanel() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["browser-health"],
    queryFn: () => fetchJson<BrowserHealth>("/api/browser-health"),
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
  });
  const dismissal = useMutation({
    mutationFn: (attemptId: string | null) => fetchJson<{ dismissed: number }>(
      attemptId
        ? `/api/browser-health/issues/${encodeURIComponent(attemptId)}/dismiss`
        : "/api/browser-health/issues/dismiss-all",
      { method: "POST" },
    ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["browser-health"] }),
  });
  const data = query.data;
  const attention = data ? data.active_issue_counts.failed + data.active_issue_counts.needs_review + data.active_issue_counts.overdue : 0;
  return (
    <section className="panel browserHealthPanel" aria-label="Browser resolution health">
      <div className="panelHeader">
        <h2>Browser resolution health</h2>
        <span>{data && attention > 0 ? `${attention} ${attention === 1 ? "issue" : "issues"} in 24 hours` : "Last 24 hours"}</span>
      </div>
      <div className="browserHealthBody">
      {query.isError ? <p className="errorState" role="alert">Browser health is unavailable. Previous results may be stale.</p> : null}
      {dismissal.isError ? <p className="errorState" role="alert">Could not dismiss browser issues. Try again.</p> : null}
      {!data ? <p className="mutedText">{query.isLoading ? "Loading browser activity…" : "Unable to load browser activity."}</p> : (
        <>
          {!data.enabled ? <p className="errorState">Browser monitoring is disabled. These are previously recorded results.</p> : null}
          {data.configured_provider !== "invisible_playwright" ? <p className="mutedText">Configured browser: {data.configured_provider.replaceAll("_", " ")}. This panel tracks Invisible Playwright attempts only.</p> : null}
          {data.incomplete ? <p className="errorState">Some monitoring records could not be read. Counts may be incomplete.</p> : null}
          <div className="browserHealthCounts">
            <span><strong>{data.counts.running}</strong> Running</span>
            <span><strong>{data.counts.succeeded}</strong> Succeeded</span>
            <span className={data.active_issue_counts.failed ? "browserHealthIssue" : ""}><strong>{data.active_issue_counts.failed}</strong> Open failures</span>
            <span><strong>{data.active_issue_counts.needs_review}</strong> Needs review</span>
            <span className={data.active_issue_counts.overdue ? "browserHealthIssue" : ""}><strong>{data.active_issue_counts.overdue}</strong> No completion reported</span>
          </div>
          {data.total === 0 ? <p className="mutedText">No browser attempts recorded in the last 24 hours. Monitoring starts with new activity; older runs are not included.</p> : null}
          <p className="mutedText">Last successful resolution: {timestamp(data.last_success_at)}</p>
          {data.dismissed_issue_count ? <p className="mutedText">{data.dismissed_issue_count} {data.dismissed_issue_count === 1 ? "issue" : "issues"} dismissed in the last 24 hours; diagnostic records are retained.</p> : null}
          {data.recovered_failure_count ? <p className="mutedText">{data.recovered_failure_count} {data.recovered_failure_count === 1 ? "failed attempt recovered" : "failed attempts recovered"} after a later successful attempt for the same link.</p> : null}
          {data.failure_reasons.length ? <ul className="browserHealthReasons">{data.failure_reasons.map(item => (
            <li key={item.reason}>{item.reason} <strong>({item.count})</strong></li>
          ))}</ul> : null}
          {data.recent_issues.length ? (
            <details>
              <summary>Recent issues ({data.recent_issues.length}{attention > data.recent_issues.length ? ` of ${attention}` : ""})</summary>
              <button className="secondaryAction browserHealthDismissAll" type="button" disabled={dismissal.isPending} onClick={() => dismissal.mutate(null)}>Dismiss all issues</button>
              <ul className="browserHealthAttempts">{data.recent_issues.map(attempt => (
                <li key={attempt.attempt_id}>
                  <strong>{attempt.title || (attempt.source_url ? new URL(attempt.source_url).hostname : "Posting")}</strong>
                  <span>{attempt.reason}</span>
                  {attempt.error_code ? <small>Code: {attempt.error_code}</small> : null}
                  {attempt.signals?.map((signal, index) => <small key={`${attempt.attempt_id}-signal-${index}`}>{signal}</small>)}
                  <small>{attempt.workflow === "availability_scan" ? "Availability scan" : "Ingestion / source discovery"} · {timestamp(attempt.finished_at || attempt.started_at)}</small>
                  <div>
                    {attempt.job_id ? <a href={`/jobs/${encodeURIComponent(attempt.job_id)}`}>View job</a> : <a href="/sources">Review sources</a>}
                    {attempt.source_url ? <a href={attempt.source_url} target="_blank" rel="noreferrer">Open posting</a> : null}
                    <button className="secondaryAction" type="button" disabled={dismissal.isPending} onClick={() => dismissal.mutate(attempt.attempt_id)}>Dismiss</button>
                  </div>
                </li>
              ))}</ul>
            </details>
          ) : null}
          <p className="mutedText browserHealthFootnote">Updates every 30 seconds. Open failures clear after a later successful attempt for the same link. Open issue counts exclude dismissed attempts; totals and diagnostic records are retained. New failures remain visible.</p>
        </>
      )}
      </div>
    </section>
  );
}
