"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { fetchJson } from "./lib/api";
import { BrowserHealthPanel } from "./browser-health";
import {
  formatJobStartTerm,
  isMissingActualTerm,
  jobTermFilterText
} from "./lib/employment-timing";
import { formatDurationMonths } from "./lib/format";
import {
  formatJobOpportunityStatus,
  JOB_OPPORTUNITY_STATUSES,
  type JobOpportunityStatus
} from "./lib/jobs";

type ApiJob = {
  id: string;
  application_summary: { application_id: string | null; application_status: string | null; applied_at: string | null };
  created_at: string;
  status: JobOpportunityStatus;
  status_validation: ActiveValidationState;
  availability: {
    state: AvailabilityState;
    label: string;
    source_repair_required: boolean;
    last_open_at: string | null;
    last_open_evidence_label: string | null;
    deadline_based: boolean;
  };
  status_check?: {
    result: string | null;
    evidence_label: string | null;
    category: VerificationCategory;
    category_label: string;
    failure_count: number;
  } | null;
  source: {
    source_type?: string;
    source_url?: string | null;
    source_archive_download_url?: string | null;
  };
  employer: {
    company_name?: string | null;
  };
  role: {
    job_title?: string | null;
    normalized_title?: string | null;
  };
  duration_months?: number | null;
  employment_timing: {
    raw_label?: string | null;
    timing_type?: string | null;
    start_date?: string | null;
    duration_months?: number | null;
    duration_label?: string | null;
  };
  location: {
    city?: string | null;
    province_state?: string | null;
    country?: string | null;
  };
  application: {
    documents_required?: string[];
  };
  fit_analysis: {
    fit_score?: number | null;
  };
  data_quality: {
    needs_manual_review?: boolean;
  };
};

type JobSummaryPage = {
  items: ApiJob[]; total: number; page: number; page_size: number;
  stats: { total: number; availability: Record<string, number>; needs_review: number;
    status_needs_review: number; tracked: number; draft_applications: number };
};

type ApiApplication = {
  application_id: string;
  job_id: string;
  status: string;
  decision: string;
  applied_at?: string | null;
  follow_up_date?: string | null;
};

type ApiIngestionRun = {
  ingestion_run_id: string;
  provider: string;
  source_url: string | null;
  search_text: string;
  status: string;
  alert_level: string;
  jobs_seen_count: number;
  jobs_created_count: number;
  jobs_duplicate_count: number;
  error_count: number;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
};

type ApiJobStatusHealth = {
  observed_at: string;
  active_total: number;
  due: number;
  source_repair_required: number;
  provider_confirmed_open: number;
  page_present: number;
  recently_observed_open: number;
  unverified_active: number;
  latest_run: {
    status: string;
    started_at: string;
    finished_at: string | null;
    checked: number;
    open: number;
    closed: number;
    expired: number;
    inconclusive: number;
    failed: number;
  } | null;
};

type JobRow = {
  id: string;
  title: string;
  company: string;
  source: string;
  sourceUrl: string | null;
  sourceArchiveUrl: string | null;
  detailUrl: string;
  createdAt: string;
  createdAtTimestamp: number;
  fit: string;
  fitScore: number | null;
  opportunityStatus: JobOpportunityStatus;
  statusValidation: ActiveValidationState;
  availabilityState: AvailabilityState;
  availabilityLabel: string;
  verificationCategory: VerificationCategory;
  verificationReason: string | null;
  verificationFailures: number;
  workflowStatus: string;
  isApplied: boolean;
  needsReview: boolean;
  timing: string;
  startTerm: string;
  termFilterText: string;
  missingActualTerm: boolean;
  duration: string;
  location: string;
  documents: string;
};

type JobSortKey = "created_at" | "fit" | "term";
type ActiveValidationState = "confirmed_open" | "unverified_active" | "not_active";
type AvailabilityState =
  | "provider_confirmed_open"
  | "page_present"
  | "recently_observed_open"
  | "source_repair_required"
  | "unverified_active"
  | "not_active";
type VerificationCategory =
  | "identity"
  | "access"
  | "response_limit"
  | "network"
  | "unsupported"
  | "stale"
  | "confirmed"
  | "terminal"
  | "unknown";
type JobStatusFilter = JobOpportunityStatus | AvailabilityState | "all";
type SortDirection = "asc" | "desc";
type JobSortState = {
  key: JobSortKey;
  direction: SortDirection;
};

const SOURCE_LABELS: Record<string, string> = {
  paste: "Pasted posting",
  file: "Uploaded file",
  url: "URL import",
  gmail_alert: "Gmail alert",
  ats_mock: "ATS mock",
  ats_canadian_tech_internships: "Canadian Tech Internships",
  ats_canadian_tech_internships_2027: "Canadian Tech Internships 2027",
  ats_canada_tech_internships_summer_2027: "Canada Tech Internships Summer 2027",
  ats_csjobs: "CS Jobs",
  ats_eluta: "Eluta",
  ats_hanzilla: "Hanzilla",
  ats_phenom_oracle: "Phenom / Oracle",
  workday: "Workday"
};

const NAV_ITEMS = [
  ["Dashboard", "#dashboard"],
  ["Profile", "/profile"],
  ["Jobs", "#jobs"],
  ["Sources", "/sources"],
  ["Gmail alerts", "#gmail-alerts"],
  ["Extension", "#extension"]
] as const;

const MAX_BULK_STATUS_JOBS = 500;


function formatRunTime(value: string | null): string {
  if (!value) {
    return "In progress";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatJobCreatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric"
  }).format(date);
}

function jobCreatedAtTimestamp(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function formatJobLocation(location: ApiJob["location"]): string {
  return [location.city, location.province_state, location.country]
    .map((part) => part?.trim())
    .filter(Boolean)
    .join(", ");
}

function formatOpportunityValidation(job: JobRow): string {
  if (job.opportunityStatus === "active") {
    return {
      provider_confirmed_open: "Provider-confirmed",
      page_present: "Page present",
      recently_observed_open: "Recently observed",
      source_repair_required: "Source repair",
      unverified_active: "Unverified active",
      not_active: "Not active"
    }[job.availabilityState];
  }
  return formatJobOpportunityStatus(job.opportunityStatus);
}


export default function Home() {
  const [jobSearch, setJobSearch] = useState("");
  const [termFilter, setTermFilter] = useState("");
  const [missingActualTermOnly, setMissingActualTermOnly] = useState(false);
  const [statusFilter, setStatusFilter] = useState<JobStatusFilter>("active");
  const [verificationFilter, setVerificationFilter] = useState<VerificationCategory | "all">("all");
  const [needsReviewOnly, setNeedsReviewOnly] = useState(false);
  const [hideApplied, setHideApplied] = useState(true);
  const [selectedJobIds, setSelectedJobIds] = useState<Set<string>>(new Set());
  const [bulkStatus, setBulkStatus] = useState<JobOpportunityStatus | "">("");
  const [jobSort, setJobSort] = useState<JobSortState>({
    key: "created_at",
    direction: "desc"
  });

  const [page, setPage] = useState(1);
  const filters = new URLSearchParams({
    q: jobSearch, term: termFilter, status: statusFilter, verification: verificationFilter,
    missing_term: String(missingActualTermOnly), needs_review: String(needsReviewOnly),
    hide_applied: String(hideApplied), sort: jobSort.key, direction: jobSort.direction,
  }).toString();
  useEffect(() => { setPage(1); }, [filters]);
  const jobsQuery = useQuery({
    queryKey: ["jobs", "summary", filters, page],
    queryFn: () => fetchJson<JobSummaryPage>(`/api/job-summaries?${filters}&page=${page}&page_size=100`)
  });
  const ingestionRunsQuery = useQuery({
    queryKey: ["ingestion-runs"],
    queryFn: () => fetchJson<ApiIngestionRun[]>("/api/ingestion-runs"),
    refetchOnWindowFocus: false
  });
  const statusHealthQuery = useQuery({
    queryKey: ["job-status-health"],
    queryFn: () => fetchJson<ApiJobStatusHealth>("/api/job-status/health"),
    refetchOnWindowFocus: false
  });
  const markReviewedMutation = useMutation({
    mutationFn: (jobId: string) =>
      fetchJson<ApiJob>(`/api/jobs/${jobId}/reviewed`, {
        method: "POST"
      }),
    onSuccess: () => {
      void jobsQuery.refetch();
    }
  });
  const markAppliedMutation = useMutation({
    mutationFn: (jobId: string) =>
      fetchJson<ApiApplication>(`/api/jobs/${jobId}/applications/applied`, {
        method: "POST"
      }),
    onSuccess: () => {
      void jobsQuery.refetch();
    }
  });

  const rows = useMemo<JobRow[]>(() => {
    return (jobsQuery.data?.items ?? []).map((job) => {
      const application = job.application_summary.application_id ? { status: job.application_summary.application_status ?? "draft" } : null;
      const documents = job.application.documents_required?.length
        ? job.application.documents_required.join(", ")
        : "resume";
      const location = formatJobLocation(job.location);
      const needsReview = !application && job.data_quality.needs_manual_review === true;
      const workflowStatus = application
        ? application.status === "applied"
          ? "Applied"
          : application.status
        : needsReview
          ? "Needs review"
          : "Discovered";

      return {
        id: job.id,
        title: job.role.job_title ?? job.role.normalized_title ?? "Untitled role",
        company: job.employer.company_name ?? "Unknown company",
        source: SOURCE_LABELS[job.source.source_type ?? ""] ?? job.source.source_type ?? "Unknown",
        sourceUrl: job.source.source_url ?? null,
        sourceArchiveUrl: job.source.source_archive_download_url ?? null,
        detailUrl: `/jobs/${job.id}`,
        createdAt: formatJobCreatedAt(job.created_at),
        createdAtTimestamp: jobCreatedAtTimestamp(job.created_at),
        fit: job.fit_analysis.fit_score == null ? "Review" : String(job.fit_analysis.fit_score),
        fitScore: job.fit_analysis.fit_score ?? null,
        opportunityStatus: job.status,
        statusValidation: job.status_validation,
        availabilityState: job.availability.state,
        availabilityLabel: job.availability.label,
        verificationCategory: job.status_check?.category ?? "unknown",
        verificationReason:
          job.status_check?.category === "stale"
            ? job.status_check.category_label
            : job.status_check?.category === "unknown" || !job.status_check
              ? "Not checked yet"
              : job.status_check.evidence_label,
        verificationFailures: job.status_check?.failure_count ?? 0,
        workflowStatus,
        isApplied: application?.status === "applied",
        needsReview,
        timing: job.employment_timing.raw_label ?? "Unknown",
        startTerm: formatJobStartTerm(job.employment_timing),
        termFilterText: jobTermFilterText(job.employment_timing),
        missingActualTerm: isMissingActualTerm(job.employment_timing),
        duration: formatDurationMonths(
          job.duration_months ?? job.employment_timing.duration_months,
          job.employment_timing.duration_label
        ),
        location,
        documents
      };
    });
  }, [jobsQuery.data]);

  const visibleRows = rows;

  const selectedVisibleCount = visibleRows.filter((job) => selectedJobIds.has(job.id)).length;
  const selectableVisibleCount = Math.min(visibleRows.length, MAX_BULK_STATUS_JOBS);
  const allVisibleJobsSelected =
    selectableVisibleCount > 0 && selectedVisibleCount === selectableVisibleCount;
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const visibleJobIds = new Set(visibleRows.map((job) => job.id));
    setSelectedJobIds((current) => {
      const next = new Set(Array.from(current).filter((jobId) => visibleJobIds.has(jobId)));
      return next.size === current.size ? current : next;
    });
  }, [visibleRows]);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      selectAllCheckboxRef.current.indeterminate =
        selectedVisibleCount > 0 && !allVisibleJobsSelected;
    }
  }, [allVisibleJobsSelected, selectedVisibleCount]);

  const bulkStatusMutation = useMutation({
    mutationFn: ({ jobIds, status }: { jobIds: string[]; status: JobOpportunityStatus }) =>
      fetchJson<ApiJob[]>("/api/jobs/status/bulk", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: jobIds, status })
      }),
    onSuccess: () => {
      setSelectedJobIds(new Set());
      setBulkStatus("");
      void jobsQuery.refetch();
    }
  });

  const isLoading = jobsQuery.isLoading;
  const hasError = jobsQuery.isError;
  const stats = jobsQuery.data?.stats;
  const ingestionRuns = ingestionRunsQuery.data ?? [];
  const ingestionAlertCount = ingestionRuns.filter((run) => run.alert_level !== "info").length;
  const needsReview = stats?.needs_review ?? 0;
  const statusNeedsReview = stats?.status_needs_review ?? 0;
  const draftApplications = stats?.draft_applications ?? 0;
  const pipeline = [
    ["Provider-confirmed", stats?.availability.provider_confirmed_open ?? 0],
    ["Page present", stats?.availability.page_present ?? 0],
    ["Recently observed", stats?.availability.recently_observed_open ?? 0],
    ["Unverified active", stats?.availability.unverified_active ?? 0],
    ["Source repair", stats?.availability.source_repair_required ?? 0],
    ["Needs review", needsReview],
    ["Tracked", (stats?.tracked ?? 0)],
    ["Drafts", draftApplications],
    ["Ingestion alerts", ingestionAlertCount]
  ] as const;

  function refreshData() {
    void jobsQuery.refetch();
    void ingestionRunsQuery.refetch();
    void statusHealthQuery.refetch();
  }

  function setSort(key: JobSortKey) {
    setJobSort((current) =>
      current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "desc" }
    );
  }

  function toggleJobSelection(jobId: string) {
    setSelectedJobIds((current) => {
      const next = new Set(current);
      if (next.has(jobId)) {
        next.delete(jobId);
      } else {
        next.add(jobId);
      }
      return next;
    });
  }

  function toggleAllVisibleJobs() {
    setSelectedJobIds((current) => {
      const next = new Set(current);
      for (const job of visibleRows) {
        if (allVisibleJobsSelected) {
          next.delete(job.id);
        } else {
          if (next.size >= MAX_BULK_STATUS_JOBS) {
            break;
          }
          next.add(job.id);
        }
      }
      return next;
    });
  }

  function applyBulkStatus() {
    if (!bulkStatus || selectedJobIds.size === 0) {
      return;
    }
    bulkStatusMutation.mutate({
      jobIds: Array.from(selectedJobIds),
      status: bulkStatus
    });
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">JobCockpit</div>
        <nav>
          {NAV_ITEMS.map(([label, href]) => (
            <a className={label === "Dashboard" ? "active" : undefined} href={href} key={href}>
              {label}
            </a>
          ))}
        </nav>
        <div className="sidebarPanels">
          <div className="panel sidebarPanel" id="gmail-alerts">
            <div className="panelHeader">
              <h2>Ingestion runs</h2>
              <span>{ingestionRuns.length} recent</span>
            </div>
            {ingestionRunsQuery.isLoading ? (
              <div className="emptyState">Loading ingestion history...</div>
            ) : ingestionRunsQuery.isError ? (
              <div className="errorState">Ingestion history is unavailable.</div>
            ) : ingestionRuns.length === 0 ? (
              <div className="emptyState">No ingestion runs recorded.</div>
            ) : (
              <ul className="runList">
                {ingestionRuns.slice(0, 5).map((run) => (
                  <li className={`runItem ${run.alert_level}`} key={run.ingestion_run_id}>
                    <div>
                      <strong>{SOURCE_LABELS[`ats_${run.provider}`] ?? SOURCE_LABELS[run.provider] ?? run.provider}</strong>
                      <span className="runStatus">{run.status}</span>
                    </div>
                    <small>
                      {run.jobs_created_count} new / {run.jobs_seen_count} seen / {run.jobs_duplicate_count} duplicates
                    </small>
                    <small>{formatRunTime(run.finished_at ?? run.started_at)}</small>
                    {run.error_message ? <p className="runErrorText">{run.error_message}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="panel sidebarPanel" id="extension">
            <div className="panelHeader">
              <h2>Safety posture</h2>
            </div>
            <ul className="checks">
              <li>Gmail processed state is local-only</li>
              <li>Generated documents are artifact-backed</li>
              <li>Extension downloads need a session token</li>
              <li>Submit actions are blocked in the mock flow</li>
            </ul>
          </div>
        </div>
      </aside>
      <section className="workspace">
        <header className="topbar" id="dashboard">
          <div>
            <h1>Application workstation</h1>
            <p>Review discovered jobs, prepare truthful materials, and keep final submission manual.</p>
          </div>
          <button onClick={refreshData} type="button">Refresh</button>
        </header>

        <section className="metrics">
          {pipeline.map(([label, value]) => (
            <div className="metric" key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </section>

        <section className="panel statusHealthPanel" aria-label="Job availability scan health">
          <div className="panelHeader">
            <h2>Availability scan health</h2>
            <span>
              {statusHealthQuery.data?.latest_run
                ? `Last run ${formatRunTime(statusHealthQuery.data.latest_run.finished_at ?? statusHealthQuery.data.latest_run.started_at)}`
                : "No completed scan yet"}
            </span>
          </div>
          {statusHealthQuery.isError ? (
            <div className="errorState">Availability scan health is unavailable.</div>
          ) : statusHealthQuery.data ? (
            <div className="statusHealthSummary">
              <span>{statusHealthQuery.data.provider_confirmed_open} provider-confirmed</span>
              <span>{statusHealthQuery.data.page_present} matching pages</span>
              <span>{statusHealthQuery.data.recently_observed_open} recently observed</span>
              <span>{statusHealthQuery.data.due} due</span>
              <strong>{statusHealthQuery.data.source_repair_required} need source repair</strong>
            </div>
          ) : (
            <div className="emptyState">Loading availability scan health...</div>
          )}
        </section>

        <BrowserHealthPanel />

        <section className="dashboardMain">
          <div className="panel opportunityPanel" id="jobs">
            <div className="panelHeader">
              <h2>Opportunity queue</h2>
              <span>SQLite local mode</span>
            </div>
            <div className="opportunityToolbar">
              <label>
                <span>Search</span>
                <input
                  placeholder="Search role or company"
                  value={jobSearch}
                  onChange={(event) => setJobSearch(event.target.value)}
                />
              </label>
              <label>
                <span>Term</span>
                <input
                  placeholder="Filter term or start date"
                  value={termFilter}
                  onChange={(event) => {
                    setTermFilter(event.target.value);
                    setMissingActualTermOnly(false);
                  }}
                />
              </label>
              <label>
                <span>Status</span>
                <select
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value as JobStatusFilter)}
                >
                  <option value="all">All opportunities</option>
                  <option value="active">Active only</option>
                  <option value="provider_confirmed_open">Provider-confirmed</option>
                  <option value="page_present">Page present</option>
                  <option value="recently_observed_open">Recently observed</option>
                  <option value="source_repair_required">Source repair required</option>
                  <option value="unverified_active">Unverified active</option>
                  <option value="expired">Expired</option>
                  <option value="closed">Closed</option>
                  <option value="filled">Filled</option>
                  <option value="withdrawn">Withdrawn</option>
                </select>
              </label>
              <label>
                <span>Verification</span>
                <select
                  value={verificationFilter}
                  onChange={(event) =>
                    setVerificationFilter(event.target.value as VerificationCategory | "all")
                  }
                >
                  <option value="all">All verification results</option>
                  <option value="identity">Identity mismatch</option>
                  <option value="access">Access blocked</option>
                  <option value="response_limit">Response limit</option>
                  <option value="network">Network / HTTP</option>
                  <option value="unsupported">Unsupported URL</option>
                  <option value="stale">Stale confirmation</option>
                  <option value="unknown">Not checked</option>
                </select>
              </label>
              <div className="sortControls">
                <button
                  aria-pressed={missingActualTermOnly}
                  className="termFilterButton"
                  onClick={() => {
                    setTermFilter("");
                    setMissingActualTermOnly((current) => !current);
                  }}
                  type="button"
                >
                  Missing actual term
                </button>
                <button
                  aria-pressed={needsReviewOnly}
                  className="reviewFilterButton"
                  onClick={() => setNeedsReviewOnly((current) => !current)}
                  type="button"
                >
                  Needs review <span>{statusNeedsReview}</span>
                </button>
                <button
                  aria-pressed={hideApplied}
                  className="applicationFilterButton"
                  onClick={() => setHideApplied((current) => !current)}
                  type="button"
                >
                  Hide applied
                </button>
                <button onClick={() => setSort("created_at")} type="button">
                  Created {jobSort.key === "created_at" ? jobSort.direction : ""}
                </button>
                <button onClick={() => setSort("fit")} type="button">
                  Fit {jobSort.key === "fit" ? jobSort.direction : ""}
                </button>
                <button onClick={() => setSort("term")} type="button">
                  Term {jobSort.key === "term" ? jobSort.direction : ""}
                </button>
              </div>
            </div>
            {selectedJobIds.size > 0 ? (
              <div className="bulkActionBar" role="region" aria-label="Bulk job actions">
                <strong>{selectedJobIds.size} selected</strong>
                <span className="bulkSelectionLimit">
                  Up to {MAX_BULK_STATUS_JOBS} jobs per update.
                </span>
                <label>
                  <span>Change status</span>
                  <select
                    aria-label="New status for selected jobs"
                    disabled={bulkStatusMutation.isPending}
                    onChange={(event) =>
                      setBulkStatus(event.target.value as JobOpportunityStatus | "")
                    }
                    value={bulkStatus}
                  >
                    <option value="">Choose a status...</option>
                    {JOB_OPPORTUNITY_STATUSES.map((status) => (
                      <option key={status} value={status}>
                        {formatJobOpportunityStatus(status)}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  disabled={!bulkStatus || bulkStatusMutation.isPending}
                  onClick={applyBulkStatus}
                  type="button"
                >
                  {bulkStatusMutation.isPending ? "Updating..." : "Apply to selected"}
                </button>
                <button
                  className="clearSelectionButton"
                  disabled={bulkStatusMutation.isPending}
                  onClick={() => setSelectedJobIds(new Set())}
                  type="button"
                >
                  Clear selection
                </button>
              </div>
            ) : null}
            {markReviewedMutation.isError ? (
              <div className="errorState" role="alert">
                The review acknowledgment could not be saved. Please try again.
              </div>
            ) : null}
            {markAppliedMutation.isError ? (
              <div className="errorState" role="alert">
                The application status could not be saved. Please try again.
              </div>
            ) : null}
            {bulkStatusMutation.isError ? (
              <div className="errorState" role="alert">
                No statuses were changed. Check the selection and try again.
              </div>
            ) : null}
            {isLoading ? (
              <div className="emptyState">Loading local jobs...</div>
            ) : hasError ? (
              <div className="errorState">API data is unavailable.</div>
            ) : (stats?.total ?? 0) === 0 ? (
              <div className="emptyState">No jobs imported yet.</div>
            ) : visibleRows.length === 0 ? (
              <div className="emptyState">No jobs match the current filters.</div>
            ) : (
              <>
              <nav aria-label="Job pages" className="pagination">
                <button type="button" disabled={jobsQuery.isFetching || (jobsQuery.data?.page ?? 1) <= 1}
                  onClick={() => setPage((jobsQuery.data?.page ?? 1) - 1)}>Previous</button>
                <span>Page {jobsQuery.data?.page ?? 1} of {Math.max(1, Math.ceil((jobsQuery.data?.total ?? 0) / 100))}
                  {" · "}{jobsQuery.data?.total ?? 0} matching jobs</span>
                <button type="button" disabled={jobsQuery.isFetching || (jobsQuery.data?.page ?? 1) * 100 >= (jobsQuery.data?.total ?? 0)}
                  onClick={() => setPage((jobsQuery.data?.page ?? 1) + 1)}>Next</button>
              </nav>
              <table>
                <thead>
                  <tr>
                    <th className="selectionCell">
                      <input
                        aria-label={
                          visibleRows.length > MAX_BULK_STATUS_JOBS
                            ? `Select the first ${MAX_BULK_STATUS_JOBS} visible jobs`
                            : "Select all visible jobs"
                        }
                        checked={allVisibleJobsSelected}
                        disabled={bulkStatusMutation.isPending}
                        onChange={toggleAllVisibleJobs}
                        ref={selectAllCheckboxRef}
                        type="checkbox"
                      />
                    </th>
                    <th>Role</th>
                    <th>Source</th>
                    <th>Created</th>
                    <th>Term</th>
                    <th>Fit</th>
                    <th>Duration</th>
                    <th>Opportunity</th>
                    <th>Workflow</th>
                    <th>Documents</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((job) => (
                    <tr className={selectedJobIds.has(job.id) ? "selectedRow" : undefined} key={job.id}>
                      <td className="selectionCell">
                        <input
                          aria-label={`Select ${job.title} at ${job.company}`}
                          checked={selectedJobIds.has(job.id)}
                          disabled={
                            bulkStatusMutation.isPending ||
                            (!selectedJobIds.has(job.id) &&
                              selectedJobIds.size >= MAX_BULK_STATUS_JOBS)
                          }
                          onChange={() => toggleJobSelection(job.id)}
                          type="checkbox"
                        />
                      </td>
                      <td>
                        <a className="jobDetailLink" href={job.detailUrl}>{job.title}</a>
                        <small>{[job.company, job.location || job.timing].filter(Boolean).join(" - ")}</small>
                      </td>
                      <td>
                        <span>{job.source}</span>
                        {job.sourceUrl ? (
                          <a
                            className="externalJobLink"
                            href={job.sourceUrl}
                            rel="noreferrer"
                            target="_blank"
                          >
                            Open ATS
                          </a>
                        ) : null}
                        {job.sourceArchiveUrl ? (
                          <a className="sourceArchiveLink" href={job.sourceArchiveUrl}>Archive</a>
                        ) : null}
                      </td>
                      <td>{job.createdAt}</td>
                      <td>{job.startTerm}</td>
                      <td>{job.fit}</td>
                      <td>{job.duration}</td>
                      <td>
                        <span
                          className={`statusPill status-${
                            job.opportunityStatus === "active"
                              ? job.availabilityState.replaceAll("_", "-")
                              : job.opportunityStatus
                          }`}
                        >
                          {formatOpportunityValidation(job)}
                        </span>
                        {job.opportunityStatus === "active" ? (
                          <small className="verificationReason">
                            {job.availabilityLabel}
                            {job.verificationReason && job.availabilityState === "unverified_active"
                              ? ` · ${job.verificationReason}`
                              : ""}
                            {job.verificationFailures > 0
                              ? ` · ${job.verificationFailures} attempts`
                              : ""}
                          </small>
                        ) : null}
                      </td>
                      <td>
                        <div className="workflowCell">
                          <span className="statusPill">{job.workflowStatus}</span>
                          {job.needsReview ? (
                            <button
                              className="markReviewedButton"
                              disabled={
                                markReviewedMutation.isPending &&
                                markReviewedMutation.variables === job.id
                              }
                              onClick={() => markReviewedMutation.mutate(job.id)}
                              type="button"
                            >
                              {markReviewedMutation.isPending &&
                              markReviewedMutation.variables === job.id
                                ? "Saving..."
                                : "Mark reviewed"}
                            </button>
                          ) : null}
                          {!job.isApplied ? (
                            <button
                              className="markAppliedButton"
                              disabled={
                                markAppliedMutation.isPending &&
                                markAppliedMutation.variables === job.id
                              }
                              onClick={() => markAppliedMutation.mutate(job.id)}
                              type="button"
                            >
                              {markAppliedMutation.isPending &&
                              markAppliedMutation.variables === job.id
                                ? "Saving..."
                                : "Mark applied"}
                            </button>
                          ) : null}
                        </div>
                      </td>
                      <td>{job.documents}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </>
            )}
          </div>
        </section>

      </section>
    </main>
  );
}
